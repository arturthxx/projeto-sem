// Testes do validador de comandos sem.cmd.v1 (docs/05 §7): HMAC (verificador
// de teste em software), schema/tipos/faixas, min_duty, anti-replay, skew e TTL.
#include <ArduinoJson.h>
#include <unity.h>

#include <cctype>
#include <cstdio>
#include <string>

#include "../support/fixtures.h"
#include "../support/soft_hmac.h"

using namespace sem;

void setUp() {}
void tearDown() {}

namespace {

const char kKey[] = "chave-de-teste-sem";
const int64_t kNow = 1763290800000LL;
semtest::SoftHmacSha256 g_hmac;

std::string hmac_hex(const std::string& payload, const char* key = kKey) {
  uint8_t mac[kHmacLen];
  g_hmac.compute(reinterpret_cast<const uint8_t*>(key), std::strlen(key),
                 reinterpret_cast<const uint8_t*>(payload.data()), payload.size(), mac);
  char hex[2 * kHmacLen + 1];
  bytes_to_hex(mac, kHmacLen, hex);
  return hex;
}

/// Monta o envelope {"payload": <string>, "hmac": <hex>} com escape JSON correto.
std::string envelope(const std::string& payload, const std::string& mac) {
  JsonDocument doc;
  doc["payload"] = payload;
  doc["hmac"] = mac;
  std::string out;
  serializeJson(doc, out);
  return out;
}

std::string signed_env(const std::string& payload) { return envelope(payload, hmac_hex(payload)); }

std::string cmd(const char* id, const char* type, const char* args, int64_t ts = kNow, const char* ttl = "1200") {
  char buf[600];
  std::snprintf(buf, sizeof(buf), R"({"schema":"sem.cmd.v1","id":"%s","ts_ms":%lld,"ttl_s":%s,"type":"%s","args":%s})",
                id, static_cast<long long>(ts), ttl, type, args);
  return buf;
}

struct Fixture {
  SystemParams p = semtest::params();
  CommandValidator v{p.comm, p.loads, &g_hmac, reinterpret_cast<const uint8_t*>(kKey), std::strlen(kKey)};
  ValidationContext ctx{kNow, true, EmsMode::Rules};

  ValidationResult run(const std::string& env) { return v.validate(env.data(), env.size(), ctx); }
};

const char* why(const ValidationResult& r) { return to_string(r.reason); }

}  // namespace

static void test_soft_hmac_matches_rfc4231_case2() {
  const char* data = "what do ya want for nothing?";
  uint8_t mac[kHmacLen];
  g_hmac.compute(reinterpret_cast<const uint8_t*>("Jefe"), 4, reinterpret_cast<const uint8_t*>(data), std::strlen(data),
                 mac);
  char hex[65];
  bytes_to_hex(mac, kHmacLen, hex);
  TEST_ASSERT_EQUAL_STRING("5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843", hex);
}

static void test_hex_and_constant_time_helpers() {
  uint8_t b[2];
  TEST_ASSERT_TRUE(hex_to_bytes("0aff", 4, b, 2));
  TEST_ASSERT_EQUAL_HEX8(0x0a, b[0]);
  TEST_ASSERT_EQUAL_HEX8(0xff, b[1]);
  TEST_ASSERT_FALSE(hex_to_bytes("0AFF", 4, b, 2));  // contrato: hex minúsculo
  TEST_ASSERT_FALSE(hex_to_bytes("0af", 3, b, 2));
  TEST_ASSERT_FALSE(hex_to_bytes("0agf", 4, b, 2));
  const uint8_t x[3] = {1, 2, 3}, y[3] = {1, 2, 4};
  TEST_ASSERT_TRUE(constant_time_equal(x, x, 3));
  TEST_ASSERT_FALSE(constant_time_equal(x, y, 3));
}

static void test_valid_set_mode_is_accepted() {
  Fixture f;
  const ValidationResult r = f.run(signed_env(cmd("c-1", "set_mode", R"({"mode":"predictive"})")));
  TEST_ASSERT_TRUE_MESSAGE(r.accepted, r.detail);
  TEST_ASSERT_TRUE(r.has_id);
  TEST_ASSERT_EQUAL_STRING("c-1", r.cmd.id);
  TEST_ASSERT_EQUAL(static_cast<int>(CommandType::SetMode), static_cast<int>(r.cmd.type));
  TEST_ASSERT_EQUAL_STRING("predictive", to_string(r.cmd.mode));
  TEST_ASSERT_EQUAL(1200, r.cmd.ttl_s);
  TEST_ASSERT_TRUE(r.cmd.ts_ms == kNow);
}

static void test_hmac_failures_are_rejected() {
  Fixture f;
  const std::string p = cmd("c-2", "reset_faults", "{}");
  std::string mac = hmac_hex(p);
  mac[10] = mac[10] == 'a' ? 'b' : 'a';
  TEST_ASSERT_EQUAL_STRING("hmac", why(f.run(envelope(p, mac))));
  TEST_ASSERT_EQUAL_STRING("hmac", why(f.run(envelope(p, hmac_hex(p, "outra-chave")))));
  std::string upper = hmac_hex(p);
  for (char& c : upper) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
  TEST_ASSERT_EQUAL_STRING("hmac", why(f.run(envelope(p, upper))));
  TEST_ASSERT_EQUAL_STRING("hmac", why(f.run(R"({"payload":"{}x"})")));
  // Payload alterado após a assinatura.
  const std::string tampered = cmd("c-2", "set_mode", R"({"mode":"conventional"})");
  TEST_ASSERT_EQUAL_STRING("hmac", why(f.run(envelope(tampered, hmac_hex(p)))));
  // O id NÃO entra no cache quando o HMAC falha: o comando legítimo ainda é aceito.
  TEST_ASSERT_TRUE(f.run(signed_env(p)).accepted);
}

static void test_hmac_provider_errors_and_missing_key_fail_safe() {
  const SystemParams p = semtest::params();
  semtest::FailingHmac broken;
  CommandValidator v1(p.comm, p.loads, &broken, reinterpret_cast<const uint8_t*>(kKey), std::strlen(kKey));
  const std::string env = signed_env(cmd("c-3", "reset_faults", "{}"));
  const ValidationContext ctx{kNow, true, EmsMode::Rules};
  TEST_ASSERT_EQUAL_STRING("hmac", to_string(v1.validate(env.data(), env.size(), ctx).reason));
  CommandValidator v2(p.comm, p.loads, &g_hmac, nullptr, 0);
  TEST_ASSERT_EQUAL_STRING("hmac", to_string(v2.validate(env.data(), env.size(), ctx).reason));
  // Com command_hmac = false a etapa 1 é dispensada (ex.: bancada isolada).
  SystemParams q = semtest::params();
  q.comm.command_hmac = false;
  CommandValidator v3(q.comm, q.loads, nullptr, nullptr, 0);
  const std::string unsigned_env = R"({"payload":")" + std::string("{\\\"schema\\\":\\\"sem.cmd.v1\\\",\\\"id\\\":\\\"u1\\\",") +
                                   R"(\"ts_ms\":0,\"ttl_s\":10,\"type\":\"reset_faults\",\"args\":{}}"})";
  const ValidationContext no_clock{kNow, false, EmsMode::Rules};
  const ValidationResult r = v3.validate(unsigned_env.data(), unsigned_env.size(), no_clock);
  TEST_ASSERT_TRUE_MESSAGE(r.accepted, r.detail);
}

static void test_envelope_and_schema_errors() {
  Fixture f;
  TEST_ASSERT_EQUAL_STRING("envelope", why(f.run("nao-json")));
  TEST_ASSERT_EQUAL_STRING("envelope", why(f.run("[1,2]")));
  const std::string p = cmd("c-4", "reset_faults", "{}");
  TEST_ASSERT_EQUAL_STRING("envelope",
                           why(f.run(R"({"payload":"x","hmac":")" + hmac_hex(p) + R"(","extra":1})")));
  TEST_ASSERT_EQUAL_STRING("json", why(f.run(signed_env("{nao json}"))));
  std::string bad_schema = cmd("c-5", "reset_faults", "{}");
  bad_schema.replace(bad_schema.find("v1"), 2, "v2");
  TEST_ASSERT_EQUAL_STRING("schema", why(f.run(signed_env(bad_schema))));
  TEST_ASSERT_EQUAL_STRING("schema", why(f.run(signed_env(cmd("c-6", "explode", "{}")))));
  TEST_ASSERT_EQUAL_STRING("schema", why(f.run(signed_env(cmd("c-7", "reset_faults", "{}", kNow, "12.5")))));
  TEST_ASSERT_EQUAL_STRING("schema", why(f.run(signed_env(cmd("", "reset_faults", "{}")))));
  TEST_ASSERT_EQUAL_STRING(
      "schema", why(f.run(signed_env(cmd("id-com-mais-de-quarenta-caracteres-xxxxxxxxxxxx", "reset_faults", "{}")))));
  const std::string float_ts =
      R"({"schema":"sem.cmd.v1","id":"c-8","ts_ms":1.5,"ttl_s":10,"type":"reset_faults","args":{}})";
  TEST_ASSERT_EQUAL_STRING("schema", why(f.run(signed_env(float_ts))));
  const std::string no_args = R"({"schema":"sem.cmd.v1","id":"c-9","ts_ms":0,"ttl_s":10,"type":"reset_faults"})";
  TEST_ASSERT_EQUAL_STRING("schema", why(f.run(signed_env(no_args))));
}

static void test_args_validation() {
  Fixture f;
  TEST_ASSERT_EQUAL_STRING("args", why(f.run(signed_env(cmd("a-1", "set_mode", R"({"mode":"turbo"})")))));
  TEST_ASSERT_EQUAL_STRING("args",
                           why(f.run(signed_env(cmd("a-2", "set_mode", R"({"mode":"rules","x":1})")))));
  TEST_ASSERT_EQUAL_STRING("args", why(f.run(signed_env(cmd("a-3", "sync_soc", R"({"soc":1.2})")))));
  TEST_ASSERT_EQUAL_STRING("args", why(f.run(signed_env(cmd("a-4", "sync_soc", R"({"soc":"0.5"})")))));
  TEST_ASSERT_EQUAL_STRING("args", why(f.run(signed_env(cmd("a-5", "reset_faults", R"({"x":1})")))));
  const ValidationResult ok = f.run(signed_env(cmd("a-6", "sync_soc", R"({"soc":0.5})")));
  TEST_ASSERT_TRUE(ok.accepted);
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 0.5, ok.cmd.soc);
}

static void test_set_loads_rules() {
  Fixture f;
  const char* good = R"({"loads":[{"id":1,"on":true,"duty":1.0},{"id":2,"on":true,"duty":0.6},{"id":3,"on":false,"duty":0}]})";
  TEST_ASSERT_EQUAL_STRING("mode", why(f.run(signed_env(cmd("l-1", "set_loads", good)))));  // só em predictive
  f.ctx.mode = EmsMode::Predictive;
  const ValidationResult r = f.run(signed_env(cmd("l-2", "set_loads", good)));
  TEST_ASSERT_TRUE_MESSAGE(r.accepted, r.detail);
  TEST_ASSERT_EQUAL(3, r.cmd.n_loads);
  TEST_ASSERT_EQUAL(1, r.cmd.loads[1].index);
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 0.6, r.cmd.loads[1].duty);
  const char* bad[] = {
      R"({"loads":[{"id":9,"on":true,"duty":1}]})",                                // id inexistente
      R"({"loads":[{"id":2,"on":true,"duty":1.5}]})",                              // duty > 1
      R"({"loads":[{"id":2,"on":true,"duty":0.3}]})",                              // < min_duty (0,40)
      R"({"loads":[{"id":1,"on":true,"duty":0.5}]})",                              // não dimerizável
      R"({"loads":[{"id":1,"on":true,"duty":1},{"id":1,"on":false,"duty":0}]})",  // id repetido
      R"({"loads":[]})",                                                           // minItems
      R"({"loads":[{"id":1,"on":1,"duty":1}]})",                                   // on não booleano
      R"({"loads":[{"id":1,"on":true}]})",                                         // duty ausente
      R"({"loads":[{"id":1,"on":true,"duty":1,"x":0}]})",                          // campo extra
      R"({"loads":[{"id":1.5,"on":true,"duty":1}]})",                              // id não inteiro
  };
  int k = 0;
  for (const char* a : bad) {
    char id[16];
    std::snprintf(id, sizeof(id), "lb-%d", k++);
    const ValidationResult rb = f.run(signed_env(cmd(id, "set_loads", a)));
    TEST_ASSERT_FALSE_MESSAGE(rb.accepted, a);
    TEST_ASSERT_EQUAL_STRING_MESSAGE("args", why(rb), a);
  }
  // Dimerizável desligada aceita duty abaixo de min_duty (ignorado).
  TEST_ASSERT_TRUE(f.run(signed_env(cmd("l-3", "set_loads", R"({"loads":[{"id":2,"on":false,"duty":0.2}]})"))).accepted);
}

static void test_replay_cache() {
  Fixture f;
  const std::string env = signed_env(cmd("r-1", "reset_faults", "{}"));
  TEST_ASSERT_TRUE(f.run(env).accepted);
  const ValidationResult again = f.run(env);
  TEST_ASSERT_EQUAL_STRING("replay", why(again));
  TEST_ASSERT_TRUE(again.has_id);
  TEST_ASSERT_TRUE(f.v.id_seen("r-1"));
  // Após command_id_cache ids novos, o mais antigo sai do cache (janela deslizante).
  for (int k = 0; k < f.p.comm.command_id_cache; ++k) {
    char id[16];
    std::snprintf(id, sizeof(id), "r-x%d", k);
    TEST_ASSERT_TRUE(f.run(signed_env(cmd(id, "reset_faults", "{}"))).accepted);
  }
  TEST_ASSERT_FALSE(f.v.id_seen("r-1"));
  // id rejeitado por skew também é consumido (não pode ser reaproveitado).
  const std::string old = signed_env(cmd("r-2", "reset_faults", "{}", kNow - 500000));
  TEST_ASSERT_EQUAL_STRING("skew", why(f.run(old)));
  TEST_ASSERT_EQUAL_STRING("replay", why(f.run(old)));
}

static void test_clock_skew_and_ttl() {
  Fixture f;
  const int64_t max_ms = static_cast<int64_t>(f.p.comm.command_max_skew_s * 1000.0);
  TEST_ASSERT_TRUE(f.run(signed_env(cmd("s-1", "reset_faults", "{}", kNow - max_ms))).accepted);
  TEST_ASSERT_TRUE(f.run(signed_env(cmd("s-2", "reset_faults", "{}", kNow + max_ms))).accepted);
  TEST_ASSERT_EQUAL_STRING("skew", why(f.run(signed_env(cmd("s-3", "reset_faults", "{}", kNow + max_ms + 1000)))));
  f.ctx.time_synced = false;  // sem hora: skew não é verificado
  TEST_ASSERT_TRUE(f.run(signed_env(cmd("s-4", "reset_faults", "{}", 0))).accepted);
  TEST_ASSERT_EQUAL_STRING("ttl", why(f.run(signed_env(cmd("t-1", "reset_faults", "{}", kNow, "0")))));
  TEST_ASSERT_EQUAL_STRING("ttl", why(f.run(signed_env(cmd("t-2", "reset_faults", "{}", kNow, "3601")))));
  TEST_ASSERT_TRUE(f.run(signed_env(cmd("t-3", "reset_faults", "{}", kNow, "3600"))).accepted);
  TEST_ASSERT_TRUE(f.run(signed_env(cmd("t-4", "reset_faults", "{}", kNow, "1"))).accepted);
}

static void test_hmac_over_exact_payload_bytes_with_escapes() {
  Fixture f;
  // Payload com espaços e escapes: o HMAC vale para os bytes exatos da string.
  const std::string p =
      "{ \"schema\": \"sem.cmd.v1\", \"id\": \"e-\\u00e7\", \"ts_ms\": 1763290800000, \"ttl_s\": 60, "
      "\"type\": \"set_mode\", \"args\": {\"mode\": \"rules\"} }";
  const ValidationResult r = f.run(signed_env(p));
  TEST_ASSERT_TRUE_MESSAGE(r.accepted, r.detail);
  TEST_ASSERT_EQUAL_STRING("e-\xc3\xa7", r.cmd.id);  // ç decodificado em UTF-8
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_soft_hmac_matches_rfc4231_case2);
  RUN_TEST(test_hex_and_constant_time_helpers);
  RUN_TEST(test_valid_set_mode_is_accepted);
  RUN_TEST(test_hmac_failures_are_rejected);
  RUN_TEST(test_hmac_provider_errors_and_missing_key_fail_safe);
  RUN_TEST(test_envelope_and_schema_errors);
  RUN_TEST(test_args_validation);
  RUN_TEST(test_set_loads_rules);
  RUN_TEST(test_replay_cache);
  RUN_TEST(test_clock_skew_and_ttl);
  RUN_TEST(test_hmac_over_exact_payload_bytes_with_escapes);
  return UNITY_END();
}
