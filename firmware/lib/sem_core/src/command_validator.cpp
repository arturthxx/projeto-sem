// Validação de comandos autenticados sem.cmd.v1 (docs/05 §7, schemas/command.schema.json).
#include "sem_core/command_validator.h"

#include <ArduinoJson.h>

#include <cmath>
#include <cstdio>
#include <cstring>

namespace sem {

// ---------------------------------------------------------------------------
// Utilidades criptográficas puras
// ---------------------------------------------------------------------------
bool constant_time_equal(const uint8_t* a, const uint8_t* b, size_t n) {
  volatile uint8_t acc = 0;
  for (size_t i = 0; i < n; ++i) acc = static_cast<uint8_t>(acc | (a[i] ^ b[i]));
  return acc == 0;
}

namespace {
int hex_nibble(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  return -1;  // maiúsculas não são aceitas (contrato: hex minúsculo)
}
}  // namespace

bool hex_to_bytes(const char* hex, size_t hex_len, uint8_t* out, size_t out_len) {
  if (hex == nullptr || out == nullptr || hex_len != 2 * out_len) return false;
  bool ok = true;
  for (size_t i = 0; i < out_len; ++i) {
    const int hi = hex_nibble(hex[2 * i]);
    const int lo = hex_nibble(hex[2 * i + 1]);
    if (hi < 0 || lo < 0) ok = false;  // continua para não vazar a posição por tempo
    out[i] = static_cast<uint8_t>(((hi < 0 ? 0 : hi) << 4) | (lo < 0 ? 0 : lo));
  }
  return ok;
}

void bytes_to_hex(const uint8_t* in, size_t n, char* out) {
  static const char kHex[] = "0123456789abcdef";
  for (size_t i = 0; i < n; ++i) {
    out[2 * i] = kHex[in[i] >> 4];
    out[2 * i + 1] = kHex[in[i] & 0x0F];
  }
  out[2 * n] = '\0';
}

const char* to_string(CommandType t) {
  switch (t) {
    case CommandType::SetMode:
      return "set_mode";
    case CommandType::SetLoads:
      return "set_loads";
    case CommandType::SyncSoc:
      return "sync_soc";
    case CommandType::ResetFaults:
      return "reset_faults";
  }
  return "?";
}

const char* to_string(RejectReason r) {
  switch (r) {
    case RejectReason::None:
      return "ok";
    case RejectReason::Envelope:
      return "envelope";
    case RejectReason::Hmac:
      return "hmac";
    case RejectReason::Json:
      return "json";
    case RejectReason::Schema:
      return "schema";
    case RejectReason::Args:
      return "args";
    case RejectReason::Replay:
      return "replay";
    case RejectReason::Skew:
      return "skew";
    case RejectReason::Ttl:
      return "ttl";
    case RejectReason::Mode:
      return "mode";
  }
  return "?";
}

// ---------------------------------------------------------------------------
// CommandValidator
// ---------------------------------------------------------------------------
CommandValidator::CommandValidator(const CommParams& comm, const LoadSetParams& loads, const HmacSha256* hmac,
                                   const uint8_t* key, size_t key_len)
    : comm_(comm), loads_(loads), hmac_(hmac) {
  key_len_ = (key != nullptr) ? (key_len > sizeof(key_) ? sizeof(key_) : key_len) : 0;
  if (key_len_ > 0) std::memcpy(key_, key, key_len_);
  cache_cap_ = comm.command_id_cache < 1 ? 1 : (comm.command_id_cache > kMaxIdCache ? kMaxIdCache : comm.command_id_cache);
}

void CommandValidator::clear_cache() {
  cache_n_ = 0;
  cache_head_ = 0;
}

bool CommandValidator::id_seen(const char* id) const {
  for (int k = 0; k < cache_n_; ++k) {
    if (std::strcmp(cache_[k], id) == 0) return true;
  }
  return false;
}

void CommandValidator::remember(const char* id) {
  copy_cstr(cache_[cache_head_], id);
  cache_head_ = (cache_head_ + 1) % cache_cap_;
  if (cache_n_ < cache_cap_) ++cache_n_;
}

namespace {

ValidationResult& reject(ValidationResult& r, RejectReason why, const char* detail) {
  r.accepted = false;
  r.reason = why;
  copy_cstr(r.detail, detail);
  return r;
}

/// Verdadeiro se todas as chaves de 'obj' pertencem a 'allowed' (additionalProperties: false).
bool only_keys(JsonObjectConst obj, const char* const* allowed, size_t n) {
  for (JsonPairConst kv : obj) {
    bool found = false;
    for (size_t i = 0; i < n; ++i) {
      if (std::strcmp(kv.key().c_str(), allowed[i]) == 0) {
        found = true;
        break;
      }
    }
    if (!found) return false;
  }
  return true;
}

bool is_number(JsonVariantConst v) { return v.is<double>() && !v.is<bool>(); }

/// ArduinoJson 7 armazena decimais curtos como float (ex.: 0.6 -> 0.600000024);
/// arredondar a 1e-6 recupera o valor decimal enviado (duty/SOC não exigem mais).
double as_decimal(JsonVariantConst v) { return std::round(v.as<double>() * 1e6) / 1e6; }

}  // namespace

ValidationResult CommandValidator::validate(const char* envelope, size_t len, const ValidationContext& ctx) {
  ValidationResult r{};
  if (envelope == nullptr || len < 2 || len > 4 * kMaxPayloadLen) return reject(r, RejectReason::Envelope, "envelope vazio ou grande demais");

  JsonDocument env;
  if (deserializeJson(env, envelope, len)) return reject(r, RejectReason::Envelope, "envelope nao e JSON");
  if (!env.is<JsonObjectConst>()) return reject(r, RejectReason::Envelope, "envelope nao e objeto");
  JsonObjectConst obj = env.as<JsonObjectConst>();
  static const char* const kEnvKeys[] = {"payload", "hmac"};
  if (!only_keys(obj, kEnvKeys, 2)) return reject(r, RejectReason::Envelope, "campos extras no envelope");
  JsonVariantConst vp = obj["payload"];
  if (!vp.is<const char*>()) return reject(r, RejectReason::Envelope, "payload ausente");
  const JsonString payload = vp.as<JsonString>();
  if (payload.size() < 2 || payload.size() > kMaxPayloadLen) return reject(r, RejectReason::Envelope, "payload fora de [2,1024]");

  // --- 1. HMAC (tempo constante) sobre os bytes exatos de 'payload'.
  if (comm_.command_hmac) {
    JsonVariantConst vh = obj["hmac"];
    if (!vh.is<const char*>()) return reject(r, RejectReason::Hmac, "hmac ausente");
    const JsonString hex = vh.as<JsonString>();
    uint8_t given[kHmacLen];
    if (!hex_to_bytes(hex.c_str(), hex.size(), given, kHmacLen)) return reject(r, RejectReason::Hmac, "hmac malformado");
    if (hmac_ == nullptr || key_len_ == 0) return reject(r, RejectReason::Hmac, "chave HMAC nao configurada");
    uint8_t expected[kHmacLen];
    if (!hmac_->compute(key_, key_len_, reinterpret_cast<const uint8_t*>(payload.c_str()), payload.size(), expected)) {
      return reject(r, RejectReason::Hmac, "falha no calculo do HMAC");
    }
    if (!constant_time_equal(given, expected, kHmacLen)) return reject(r, RejectReason::Hmac, "hmac invalido");
  }

  check_payload(payload.c_str(), payload.size(), ctx, &r);
  return r;
}

bool CommandValidator::check_payload(const char* payload, size_t len, const ValidationContext& ctx,
                                     ValidationResult* out) {
  ValidationResult& r = *out;
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return reject(r, RejectReason::Json, "payload nao e JSON").accepted;
  if (!doc.is<JsonObjectConst>()) return reject(r, RejectReason::Json, "payload nao e objeto").accepted;
  JsonObjectConst c = doc.as<JsonObjectConst>();
  ParsedCommand& cmd = r.cmd;

  // --- 2. Schema, tipos e faixas.
  JsonVariantConst v_id = c["id"];
  if (v_id.is<const char*>()) {
    const JsonString s = v_id.as<JsonString>();
    if (s.size() >= 1 && s.size() <= static_cast<size_t>(kCmdIdLen) && std::strlen(s.c_str()) == s.size()) {
      copy_cstr(cmd.id, s.c_str());
      r.has_id = true;
    }
  }
  const char* schema = c["schema"].is<const char*>() ? c["schema"].as<const char*>() : nullptr;
  if (schema == nullptr || std::strcmp(schema, "sem.cmd.v1") != 0) return reject(r, RejectReason::Schema, "schema != sem.cmd.v1").accepted;
  if (!r.has_id) return reject(r, RejectReason::Schema, "id ausente ou fora de [1,40]").accepted;
  JsonVariantConst v_ts = c["ts_ms"];
  if (!v_ts.is<int64_t>() || v_ts.as<int64_t>() < 0) return reject(r, RejectReason::Schema, "ts_ms invalido").accepted;
  cmd.ts_ms = v_ts.as<int64_t>();
  JsonVariantConst v_ttl = c["ttl_s"];
  if (!v_ttl.is<int64_t>()) return reject(r, RejectReason::Schema, "ttl_s nao inteiro").accepted;
  const int64_t ttl = v_ttl.as<int64_t>();
  const char* type = c["type"].is<const char*>() ? c["type"].as<const char*>() : "";
  if (!c["args"].is<JsonObjectConst>()) return reject(r, RejectReason::Schema, "args ausente").accepted;
  JsonObjectConst args = c["args"].as<JsonObjectConst>();

  if (std::strcmp(type, "set_mode") == 0) {
    cmd.type = CommandType::SetMode;
    static const char* const kKeys[] = {"mode"};
    const char* m = args["mode"].is<const char*>() ? args["mode"].as<const char*>() : nullptr;
    if (!only_keys(args, kKeys, 1) || !parse_mode(m, &cmd.mode)) return reject(r, RejectReason::Args, "set_mode: mode invalido").accepted;
  } else if (std::strcmp(type, "set_loads") == 0) {
    cmd.type = CommandType::SetLoads;
    static const char* const kKeys[] = {"loads"};
    if (!only_keys(args, kKeys, 1) || !args["loads"].is<JsonArrayConst>()) return reject(r, RejectReason::Args, "set_loads: loads ausente").accepted;
    JsonArrayConst arr = args["loads"].as<JsonArrayConst>();
    if (arr.size() < 1 || arr.size() > static_cast<size_t>(kMaxLoads)) return reject(r, RejectReason::Args, "set_loads: 1..8 itens").accepted;
    static const char* const kItemKeys[] = {"id", "on", "duty"};
    for (JsonVariantConst it : arr) {
      if (!it.is<JsonObjectConst>()) return reject(r, RejectReason::Args, "set_loads: item nao e objeto").accepted;
      JsonObjectConst o = it.as<JsonObjectConst>();
      if (!only_keys(o, kItemKeys, 3) || !o["id"].is<int64_t>() || !o["on"].is<bool>() || !is_number(o["duty"])) {
        return reject(r, RejectReason::Args, "set_loads: item requer id/on/duty").accepted;
      }
      const int64_t id = o["id"].as<int64_t>();
      const int idx = (id >= 1 && id <= 1000000) ? loads_.index_of_id(static_cast<int>(id)) : -1;
      if (idx < 0) return reject(r, RejectReason::Args, "set_loads: id inexistente").accepted;
      for (int k = 0; k < cmd.n_loads; ++k) {
        if (cmd.loads[k].index == idx) return reject(r, RejectReason::Args, "set_loads: id repetido").accepted;
      }
      const bool on = o["on"].as<bool>();
      const double duty = as_decimal(o["duty"]);
      if (!(duty >= 0.0 && duty <= 1.0)) return reject(r, RejectReason::Args, "set_loads: duty fora de [0,1]").accepted;
      const LoadParams& L = loads_.items[idx];
      if (L.dimmable) {
        if (on && duty < L.min_duty) return reject(r, RejectReason::Args, "set_loads: duty < min_duty").accepted;
      } else if (duty != 0.0 && duty != 1.0) {
        return reject(r, RejectReason::Args, "set_loads: carga nao dimerizavel aceita duty 0 ou 1").accepted;
      }
      LoadCmdArg& a = cmd.loads[cmd.n_loads++];
      a.id = static_cast<int>(id);
      a.index = idx;
      a.on = on;
      a.duty = duty;
    }
  } else if (std::strcmp(type, "sync_soc") == 0) {
    cmd.type = CommandType::SyncSoc;
    static const char* const kKeys[] = {"soc"};
    if (!only_keys(args, kKeys, 1) || !is_number(args["soc"])) return reject(r, RejectReason::Args, "sync_soc: soc ausente").accepted;
    cmd.soc = as_decimal(args["soc"]);
    if (!(cmd.soc >= 0.0 && cmd.soc <= 1.0)) return reject(r, RejectReason::Args, "sync_soc: soc fora de [0,1]").accepted;
  } else if (std::strcmp(type, "reset_faults") == 0) {
    cmd.type = CommandType::ResetFaults;
    if (args.size() != 0) return reject(r, RejectReason::Args, "reset_faults: args deve ser {}").accepted;
  } else {
    return reject(r, RejectReason::Schema, "type desconhecido").accepted;
  }

  // --- 3. Anti-replay.
  if (id_seen(cmd.id)) return reject(r, RejectReason::Replay, "id repetido (replay)").accepted;
  remember(cmd.id);

  // --- 4. Defasagem de relógio (só com hora sincronizada).
  if (ctx.time_synced) {
    const int64_t diff = cmd.ts_ms - ctx.now_ms;
    const double skew_s = std::fabs(static_cast<double>(diff)) / 1000.0;
    if (skew_s > comm_.command_max_skew_s) {
      char msg[64];
      std::snprintf(msg, sizeof(msg), "skew %.0f s > %.0f s", skew_s, comm_.command_max_skew_s);
      return reject(r, RejectReason::Skew, msg).accepted;
    }
  }

  // --- 5. TTL.
  if (ttl < 1 || ttl > 3600) return reject(r, RejectReason::Ttl, "ttl_s fora de [1,3600]").accepted;
  cmd.ttl_s = static_cast<int32_t>(ttl);

  // --- set_loads somente no modo preditivo.
  if (cmd.type == CommandType::SetLoads && ctx.mode != EmsMode::Predictive) {
    return reject(r, RejectReason::Mode, "set_loads exige modo predictive").accepted;
  }

  r.accepted = true;
  r.reason = RejectReason::None;
  copy_cstr(r.detail, "ok");
  return true;
}

}  // namespace sem
