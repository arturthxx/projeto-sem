// Testes do supervisor de segurança (docs/06 §2): cada proteção, atrasos,
// religamento, nova tentativa, bloqueio e prioridade dos códigos.
#include <unity.h>

#include "../support/fixtures.h"

using namespace sem;

void setUp() {}
void tearDown() {}

namespace {

SafetyParams P() { return semtest::params().safety; }

/// Amostra "normal" (todos os sensores válidos, nada fora da faixa).
SafetyInputs nominal(double t_s, double dt_s) {
  SafetyInputs in{};
  in.t_s = t_s;
  in.dt_s = dt_s;
  in.v_bat_ok = true;
  in.v_bat = 13.1;
  in.soc = 0.6;
  in.soc_valid = true;
  in.v_bus_ok = true;
  in.v_bus = 13.1;
  in.i_bus_ok = true;
  in.i_bus = 1.0;
  in.t_bat_ok = true;
  in.t_bat = 25.0;
  return in;
}

const char* code(const SafetyStatus& s) { return to_string(s.code); }

}  // namespace

static void test_nominal_is_ok() {
  SafetySupervisor s(P(), false);
  const SafetyStatus st = s.update(nominal(0.0, 0.1));
  TEST_ASSERT_TRUE(st.allowed);
  TEST_ASSERT_EQUAL_STRING("OK", code(st));
}

static void test_boot_hold_counts_valid_samples() {
  const SafetyParams p = P();
  SafetySupervisor s(p, true);
  TEST_ASSERT_EQUAL_STRING("BOOT", code(s.status()));
  SafetyInputs bad = nominal(0.0, 0.1);
  bad.v_bat_ok = false;
  bad.v_bus_ok = false;
  for (int k = 0; k < 10; ++k) s.update(bad);  // inválidas não contam
  for (int k = 0; k < p.boot_settle_samples - 1; ++k) {
    TEST_ASSERT_EQUAL_STRING("BOOT", code(s.update(nominal(0.1 * k, 0.1))));
  }
  const SafetyStatus st = s.update(nominal(10.0, 0.1));
  TEST_ASSERT_TRUE(st.allowed);
  TEST_ASSERT_EQUAL_STRING("OK", code(st));
  s.reset();
  TEST_ASSERT_EQUAL_STRING("BOOT", code(s.status()));
}

static void test_lvd_by_voltage_with_delay_and_reconnect() {
  SafetySupervisor s(P(), false);
  EventList ev;
  SafetyInputs in = nominal(0.0, 0.5);
  in.v_bat = 11.5;
  for (int k = 0; k < 9; ++k) TEST_ASSERT_EQUAL_STRING("OK", code(s.update(in, &ev)));  // 4,5 s
  TEST_ASSERT_EQUAL_STRING("LVD", code(s.update(in, &ev)));                                // 5,0 s
  TEST_ASSERT_TRUE(ev.contains(EventCode::LvdTrip));
  in.v_bat = 12.8;  // abaixo de reconnect_v
  TEST_ASSERT_EQUAL_STRING("LVD", code(s.update(in, &ev)));
  in.v_bat = 12.95;
  in.soc = 0.2;  // SOC válido abaixo de reconnect_min_soc
  TEST_ASSERT_EQUAL_STRING("LVD", code(s.update(in, &ev)));
  in.soc = 0.3;
  const SafetyStatus st = s.update(in, &ev);
  TEST_ASSERT_TRUE(st.allowed);
  TEST_ASSERT_TRUE(ev.contains(EventCode::LvdClear));
}

static void test_lvd_short_dip_does_not_trip() {
  SafetySupervisor s(P(), false);
  SafetyInputs in = nominal(0.0, 1.0);
  in.v_bat = 11.4;
  for (int k = 0; k < 4; ++k) s.update(in);
  in.v_bat = 12.0;  // volta acima de lvd_v: zera o temporizador
  s.update(in);
  in.v_bat = 11.4;
  for (int k = 0; k < 4; ++k) TEST_ASSERT_EQUAL_STRING("OK", code(s.update(in)));
}

static void test_lvd_timer_is_literal_float_accumulation() {
  // Decisão documentada: 50 × 0,1 s = 4,999… < 5 s -> dispara na 51ª amostra (igual ao Python).
  SafetySupervisor s(P(), false);
  SafetyInputs in = nominal(0.0, 0.1);
  in.v_bat = 11.0;
  for (int k = 0; k < 50; ++k) TEST_ASSERT_EQUAL_STRING("OK", code(s.update(in)));
  TEST_ASSERT_EQUAL_STRING("LVD", code(s.update(in)));
}

static void test_lvd_by_soc_and_invalid_soc_release() {
  SafetySupervisor s(P(), false);
  SafetyInputs in = nominal(0.0, 0.1);
  in.soc = 0.09;
  TEST_ASSERT_EQUAL_STRING("LVD", code(s.update(in)));  // imediato
  in.soc_valid = false;
  in.soc = 0.05;  // SOC inválido não segura nem dispara
  in.v_bat = 13.0;
  TEST_ASSERT_EQUAL_STRING("OK", code(s.update(in)));
  TEST_ASSERT_EQUAL_STRING("OK", code(s.update(in)));
}

static void test_lvd_uses_bus_voltage_when_bat_invalid() {
  SafetySupervisor s(P(), false);
  SafetyInputs in = nominal(0.0, 1.0);
  in.v_bat_ok = false;
  in.v_bus = 11.0;
  for (int k = 0; k < 4; ++k) s.update(in);
  TEST_ASSERT_EQUAL_STRING("LVD", code(s.update(in)));
  in.v_bus = 13.0;
  TEST_ASSERT_EQUAL_STRING("OK", code(s.update(in)));
}

static void test_ovp_with_delay_and_hysteresis() {
  SafetySupervisor s(P(), false);
  EventList ev;
  SafetyInputs in = nominal(0.0, 0.5);
  in.v_bus = 15.2;
  TEST_ASSERT_EQUAL_STRING("OK", code(s.update(in, &ev)));
  TEST_ASSERT_EQUAL_STRING("OVP", code(s.update(in, &ev)));
  TEST_ASSERT_TRUE(ev.contains(EventCode::OvpTrip));
  in.v_bus = 14.8;  // não está abaixo de 15,0 − 0,3
  for (int k = 0; k < 5; ++k) TEST_ASSERT_EQUAL_STRING("OVP", code(s.update(in, &ev)));
  in.v_bus = 14.5;
  TEST_ASSERT_EQUAL_STRING("OVP", code(s.update(in, &ev)));
  TEST_ASSERT_EQUAL_STRING("OK", code(s.update(in, &ev)));
  TEST_ASSERT_TRUE(ev.contains(EventCode::OvpClear));
}

static void test_oc_retry_then_lockout_and_manual_reset() {
  const SafetyParams p = P();
  SafetySupervisor s(p, false);
  EventList ev;
  SafetyInputs in = nominal(0.0, 0.1);
  in.i_bus = p.bus_overcurrent_a + 0.5;
  TEST_ASSERT_EQUAL_STRING("OK", code(s.update(in, &ev)));  // 100 ms
  TEST_ASSERT_EQUAL_STRING("OC", code(s.update(in, &ev)));  // 200 ms
  TEST_ASSERT_EQUAL(1, s.oc_trips());
  TEST_ASSERT_TRUE(ev.contains(EventCode::OcTrip));
  in.i_bus = 0.0;
  in.dt_s = 29.5;
  TEST_ASSERT_EQUAL_STRING("OC", code(s.update(in, &ev)));
  in.dt_s = 0.5;
  TEST_ASSERT_EQUAL_STRING("OK", code(s.update(in, &ev)));  // nova tentativa após 30 s
  TEST_ASSERT_TRUE(ev.contains(EventCode::OcRetry));
  // Segundo disparo.
  in.dt_s = 0.1;
  in.i_bus = 5.0;
  s.update(in, &ev);
  TEST_ASSERT_EQUAL_STRING("OC", code(s.update(in, &ev)));
  in.i_bus = 0.0;
  in.dt_s = 30.0;
  TEST_ASSERT_EQUAL_STRING("OK", code(s.update(in, &ev)));
  // Terceiro disparo sem 10 min livres -> bloqueio.
  in.dt_s = 0.1;
  in.i_bus = 5.0;
  s.update(in, &ev);
  TEST_ASSERT_EQUAL_STRING("OC_LOCKOUT", code(s.update(in, &ev)));
  TEST_ASSERT_TRUE(ev.contains(EventCode::OcLockout));
  in.i_bus = 0.0;
  in.dt_s = 3600.0;
  TEST_ASSERT_EQUAL_STRING("OC_LOCKOUT", code(s.update(in, &ev)));
  ev.clear();
  s.reset_faults();
  TEST_ASSERT_EQUAL(0, s.oc_trips());
  TEST_ASSERT_EQUAL_STRING("OK", code(s.update(in, &ev)));
  TEST_ASSERT_TRUE(ev.contains(EventCode::FaultsReset));  // emitido na próxima amostra
}

static void test_oc_counter_resets_after_free_period() {
  SafetySupervisor s(P(), false);
  SafetyInputs in = nominal(0.0, 0.1);
  in.i_bus = 5.0;
  s.update(in);
  s.update(in);
  TEST_ASSERT_EQUAL(1, s.oc_trips());
  in.i_bus = 0.0;
  in.dt_s = 30.0;
  s.update(in);  // nova tentativa
  in.dt_s = 600.0;
  s.update(in);  // 10 min livres
  TEST_ASSERT_EQUAL(0, s.oc_trips());
}

static void test_oc_short_spike_and_hardware_alert() {
  SafetySupervisor s(P(), false);
  SafetyInputs in = nominal(0.0, 0.1);
  in.i_bus = 6.0;
  s.update(in);
  in.i_bus = 1.0;  // pico de 100 ms: não dispara
  TEST_ASSERT_EQUAL_STRING("OK", code(s.update(in)));
  in.oc_alert = true;  // pino ALERT do INA226 conta como sobrecorrente
  s.update(in);
  TEST_ASSERT_EQUAL_STRING("OC", code(s.update(in)));
}

static void test_ot_with_release_hysteresis() {
  SafetySupervisor s(P(), false);
  EventList ev;
  SafetyInputs in = nominal(0.0, 1.0);
  in.t_bat = 55.0;  // não é maior que o limite
  TEST_ASSERT_EQUAL_STRING("OK", code(s.update(in, &ev)));
  in.t_bat = 55.5;
  TEST_ASSERT_EQUAL_STRING("OT", code(s.update(in, &ev)));
  in.t_bat = 50.0;
  TEST_ASSERT_EQUAL_STRING("OT", code(s.update(in, &ev)));
  in.t_bat_ok = false;  // sensor de temperatura ausente: mantém o estado
  TEST_ASSERT_EQUAL_STRING("OT", code(s.update(in, &ev)));
  in.t_bat_ok = true;
  in.t_bat = 44.9;
  TEST_ASSERT_EQUAL_STRING("OK", code(s.update(in, &ev)));
  TEST_ASSERT_TRUE(ev.contains(EventCode::OtTrip));
  TEST_ASSERT_TRUE(ev.contains(EventCode::OtClear));
}

static void test_sensor_timeout_and_recovery() {
  SafetySupervisor s(P(), false);
  EventList ev;
  SafetyInputs in = nominal(0.0, 1.0);
  in.v_bat_ok = false;
  in.v_bus_ok = false;
  in.i_bus_ok = false;
  for (int k = 0; k < 3; ++k) TEST_ASSERT_EQUAL_STRING("OK", code(s.update(in, &ev)));
  const SafetyStatus st = s.update(in, &ev);
  TEST_ASSERT_FALSE(st.allowed);
  TEST_ASSERT_EQUAL_STRING("SENSOR", code(st));
  TEST_ASSERT_TRUE(ev.contains(EventCode::SensorFail));
  in.v_bus_ok = true;  // basta um voltar
  TEST_ASSERT_EQUAL_STRING("OK", code(s.update(in, &ev)));
  TEST_ASSERT_TRUE(ev.contains(EventCode::SensorOk));
}

static void test_code_priority() {
  SafetySupervisor s(P(), false);
  SafetyInputs in = nominal(0.0, 5.0);
  in.v_bat = 11.0;
  in.t_bat = 60.0;
  TEST_ASSERT_EQUAL_STRING("OT", code(s.update(in)));  // OT > LVD
  TEST_ASSERT_TRUE(s.active(SafetyCode::Lvd));
  in.v_bus = 15.5;
  TEST_ASSERT_EQUAL_STRING("OVP", code(s.update(in)));  // OVP > OT
  in.i_bus = 5.0;
  TEST_ASSERT_EQUAL_STRING("OC", code(s.update(in)));  // OC > OVP
  in.i_bus = 0.0;
  in.dt_s = 30.0;
  s.update(in);
  in.dt_s = 5.0;
  in.i_bus = 5.0;
  s.update(in);
  in.dt_s = 30.0;
  in.i_bus = 0.0;
  s.update(in);
  in.dt_s = 5.0;
  in.i_bus = 5.0;
  TEST_ASSERT_EQUAL_STRING("OC_LOCKOUT", code(s.update(in)));  // topo
  // SENSOR > LVD
  SafetySupervisor s2(P(), false);
  SafetyInputs b = nominal(0.0, 1.0);
  b.soc = 0.05;
  s2.update(b);
  b.v_bat_ok = false;
  b.v_bus_ok = false;
  for (int k = 0; k < 4; ++k) s2.update(b);
  TEST_ASSERT_EQUAL_STRING("SENSOR", code(s2.status()));
  TEST_ASSERT_TRUE(s2.active(SafetyCode::Lvd));
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_nominal_is_ok);
  RUN_TEST(test_boot_hold_counts_valid_samples);
  RUN_TEST(test_lvd_by_voltage_with_delay_and_reconnect);
  RUN_TEST(test_lvd_short_dip_does_not_trip);
  RUN_TEST(test_lvd_timer_is_literal_float_accumulation);
  RUN_TEST(test_lvd_by_soc_and_invalid_soc_release);
  RUN_TEST(test_lvd_uses_bus_voltage_when_bat_invalid);
  RUN_TEST(test_ovp_with_delay_and_hysteresis);
  RUN_TEST(test_oc_retry_then_lockout_and_manual_reset);
  RUN_TEST(test_oc_counter_resets_after_free_period);
  RUN_TEST(test_oc_short_spike_and_hardware_alert);
  RUN_TEST(test_ot_with_release_hysteresis);
  RUN_TEST(test_sensor_timeout_and_recovery);
  RUN_TEST(test_code_priority);
  return UNITY_END();
}
