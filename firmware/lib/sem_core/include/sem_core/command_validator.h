// =============================================================================
// sem_core/command_validator.h — validação de comandos sem.cmd.v1 (docs/05 §7).
//
// Envelope (tópico smartenergy/cmd): {"payload": "<string JSON>", "hmac": "<hex>"}.
// Etapas (todas obrigatórias; falha => ack accepted:false + CMD_REJECTED):
//   1. HMAC-SHA256(chave, bytes exatos de 'payload') em tempo constante;
//   2. schema/tipos/faixas (schemas/command.schema.json + ids existentes,
//      min_duty de dimerizável, duty ∈ {0,1} para não dimerizável);
//   3. anti-replay: id não presente entre os últimos command_id_cache ids;
//   4. se a hora estiver sincronizada: |ts_ms − agora| ≤ command_max_skew_s;
//   5. 1 ≤ ttl_s ≤ 3600;
//   + set_loads aceito somente no modo predictive.
// O id entra no cache assim que passa nas etapas 1–3 (um id autêntico nunca é
// reaproveitado, mesmo que rejeitado depois por skew/ttl/modo).
//
// O HMAC é obtido por uma interface abstrata (HmacSha256): no ESP32,
// mbedtls (src/crypto); nos testes nativos, uma implementação de software.
// O parsing usa ArduinoJson (biblioteca portátil, sem Arduino.h).
// =============================================================================
#pragma once

#include <cstddef>
#include <cstdint>

#include "sem_core/params.h"
#include "sem_core/types.h"

namespace sem {

constexpr size_t kHmacLen = 32;
constexpr int kMaxIdCache = 32;
constexpr int kCmdIdLen = 40;           // maxLength de 'id' (schema)
constexpr size_t kMaxPayloadLen = 1024; // maxLength de 'payload' (schema do envelope)

/// Interface de HMAC-SHA256 (implementada com mbedtls no ESP32).
class HmacSha256 {
 public:
  virtual ~HmacSha256() = default;
  /// Calcula HMAC-SHA256(key, msg) em out[32]. Retorna false em erro.
  virtual bool compute(const uint8_t* key, size_t key_len, const uint8_t* msg, size_t msg_len,
                       uint8_t out[kHmacLen]) const = 0;
};

/// Comparação em tempo constante (não depende da posição da primeira diferença).
bool constant_time_equal(const uint8_t* a, const uint8_t* b, size_t n);

/// Hex minúsculo -> bytes. Exige exatamente 2·out_len caracteres [0-9a-f].
bool hex_to_bytes(const char* hex, size_t hex_len, uint8_t* out, size_t out_len);

/// Bytes -> hex minúsculo (out com 2·n+1 bytes).
void bytes_to_hex(const uint8_t* in, size_t n, char* out);

enum class CommandType : uint8_t { SetMode = 0, SetLoads, SyncSoc, ResetFaults };
const char* to_string(CommandType t);

enum class RejectReason : uint8_t { None = 0, Envelope, Hmac, Json, Schema, Args, Replay, Skew, Ttl, Mode };
const char* to_string(RejectReason r);

struct LoadCmdArg {
  int id = 0;
  int index = -1;  // índice em LoadSetParams
  bool on = false;
  double duty = 0.0;
};

/// Comando validado (conteúdo de 'payload').
struct ParsedCommand {
  char id[kCmdIdLen + 1] = {};
  int64_t ts_ms = 0;
  int32_t ttl_s = 0;
  CommandType type = CommandType::SetMode;
  EmsMode mode = EmsMode::Rules;  // set_mode
  double soc = 0.0;               // sync_soc
  LoadCmdArg loads[kMaxLoads] = {};
  int n_loads = 0;  // set_loads
};

struct ValidationContext {
  int64_t now_ms = 0;
  bool time_synced = false;
  EmsMode mode = EmsMode::Rules;  // modo atual (set_loads só em predictive)
};

struct ValidationResult {
  bool accepted = false;
  RejectReason reason = RejectReason::None;
  bool has_id = false;  // false => ack com "id": null
  char detail[96] = {};
  ParsedCommand cmd{};
};

class CommandValidator {
 public:
  /// 'key' é copiada (até 64 bytes). Com comm.command_hmac = true e chave vazia
  /// ou 'hmac' nulo, TODOS os comandos são rejeitados (falha segura).
  CommandValidator(const CommParams& comm, const LoadSetParams& loads, const HmacSha256* hmac, const uint8_t* key,
                   size_t key_len);

  /// Valida um envelope recebido (bytes do MQTT, não precisa terminar em '\0').
  ValidationResult validate(const char* envelope, size_t len, const ValidationContext& ctx);

  bool id_seen(const char* id) const;
  void clear_cache();

 private:
  bool check_payload(const char* payload, size_t len, const ValidationContext& ctx, ValidationResult* r);
  void remember(const char* id);

  const CommParams& comm_;
  const LoadSetParams& loads_;
  const HmacSha256* hmac_;
  uint8_t key_[64] = {};
  size_t key_len_ = 0;
  char cache_[kMaxIdCache][kCmdIdLen + 1] = {};
  int cache_cap_ = 16;
  int cache_n_ = 0;
  int cache_head_ = 0;
};

}  // namespace sem
