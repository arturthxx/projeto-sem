// Testes do estimador de SOC (docs/06 §1).
#include <unity.h>

#include "../support/fixtures.h"

using namespace sem;

static SocParams P() { return semtest::params().soc; }

void setUp() {}
void tearDown() {}

static void test_ocv_init_interpolates_and_saturates() {
  const SocParams p = P();
  SocEstimator s(p);
  s.init_from_voltage(13.05);
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 0.50, s.soc());
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, p.unknown_initial_uncertainty, s.unc());
  TEST_ASSERT_EQUAL(static_cast<int>(SyncKind::OcvInit), static_cast<int>(s.last_sync()));
  TEST_ASSERT_TRUE(s.valid());
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 0.15, ocv_to_soc(p, 12.4));  // entre (12.0, 0.10) e (12.8, 0.20)
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 0.0, ocv_to_soc(p, 9.0));
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 1.0, ocv_to_soc(p, 14.5));
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 0.20, ocv_to_soc(p, 12.8));  // ponto exato da curva
}

static void test_persisted_init() {
  SocEstimator s(P());
  TEST_ASSERT_FALSE(s.initialized());
  s.init_persisted(0.6, 0.04);
  TEST_ASSERT_TRUE(s.initialized());
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 0.6, s.soc());
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 0.04, s.unc());
  TEST_ASSERT_EQUAL_STRING("persisted", to_string(s.last_sync()));
  s.init_persisted(1.7, 0.04);  // valor corrompido é limitado
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 1.0, s.soc());
}

static void test_discharge_and_drift_uncertainty() {
  SocEstimator s(P());
  s.init_persisted(0.5, 0.05);
  s.update(3600.0, 1.2, 13.0, true);  // 1,2 Ah de 6 Ah
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 0.3, s.soc());
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 1.2, s.ah_since_sync());
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 0.05 + 0.01 * 1.2, s.unc());
}

static void test_charge_uses_coulombic_efficiency() {
  const SocParams p = P();
  SocEstimator s(p);
  s.init_persisted(0.3, 0.05);
  s.update(1800.0, -2.0, 13.3, true);  // 1 Ah carregado
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 0.3 + p.coulombic_efficiency * 1.0 / 6.0, s.soc());
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 1.0, s.ah_since_sync());
}

static void test_deadband_ignores_small_currents() {
  SocEstimator s(P());
  s.init_persisted(0.5, 0.05);
  for (int k = 0; k < 100; ++k) s.update(10.0, 0.004, 13.1, true);
  for (int k = 0; k < 100; ++k) s.update(10.0, -0.0049, 13.1, true);
  TEST_ASSERT_DOUBLE_WITHIN(1e-15, 0.5, s.soc());
  TEST_ASSERT_DOUBLE_WITHIN(1e-15, 0.0, s.ah_since_sync());
  s.update(3600.0, 0.006, 13.1, true);  // acima da zona morta: integra
  TEST_ASSERT_TRUE(s.soc() < 0.5);
}

static void test_clamps_between_zero_and_one() {
  SocEstimator s(P());
  s.init_persisted(0.99, 0.05);
  s.update(36000.0, -3.0, 13.4, true);
  TEST_ASSERT_DOUBLE_WITHIN(1e-15, 1.0, s.soc());
  s.update(36000.0, 3.0, 12.5, true);
  TEST_ASSERT_DOUBLE_WITHIN(1e-15, 0.0, s.soc());
}

static void test_full_sync_after_hold_and_event_only_on_edge() {
  const SocParams p = P();
  SocEstimator s(p);
  s.init_from_voltage(13.05);
  EventList ev;
  for (int k = 0; k < 119; ++k) s.update(1.0, -0.2, 14.1, true, &ev);
  TEST_ASSERT_EQUAL_STRING("ocv_init", to_string(s.last_sync()));
  TEST_ASSERT_TRUE(s.soc() < 0.6);
  s.update(1.0, -0.2, 14.1, true, &ev);  // t_full = 120 s
  TEST_ASSERT_DOUBLE_WITHIN(1e-15, 1.0, s.soc());
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, p.sync_uncertainty, s.unc());
  TEST_ASSERT_EQUAL_STRING("full", to_string(s.last_sync()));
  TEST_ASSERT_EQUAL(1, ev.size());
  TEST_ASSERT_TRUE(ev.contains(EventCode::SocSyncFull));
  // Condição persiste: ressincroniza, mas sem novo evento.
  for (int k = 0; k < 300; ++k) s.update(1.0, -0.2, 14.1, true, &ev);
  TEST_ASSERT_EQUAL(1, ev.size());
  // Corrente acima da cauda zera t_full; nova borda gera novo evento.
  s.update(1.0, -0.5, 14.1, true, &ev);
  TEST_ASSERT_DOUBLE_WITHIN(1e-15, 0.0, s.t_full_s());
  for (int k = 0; k < 120; ++k) s.update(1.0, -0.2, 14.1, true, &ev);
  TEST_ASSERT_EQUAL(2, ev.size());
}

static void test_full_sync_requires_voltage_and_tail_current() {
  SocEstimator s(P());
  s.init_persisted(0.8, 0.05);
  for (int k = 0; k < 500; ++k) s.update(1.0, -0.2, 13.9, true);  // tensão insuficiente
  TEST_ASSERT_EQUAL_STRING("persisted", to_string(s.last_sync()));
  for (int k = 0; k < 500; ++k) s.update(1.0, 0.35, 14.2, true);  // |I| > cauda
  TEST_ASSERT_EQUAL_STRING("persisted", to_string(s.last_sync()));
}

static void test_empty_sync_uses_compensated_voltage() {
  const SocParams p = P();
  SocEstimator s(p);
  s.init_persisted(0.2, 0.05);
  EventList ev;
  // V = 11.78, I = 1 A -> Vc = 11.82 > 11.8: não sincroniza.
  for (int k = 0; k < 60; ++k) s.update(1.0, 1.0, 11.78, true, &ev);
  TEST_ASSERT_EQUAL_STRING("persisted", to_string(s.last_sync()));
  // V = 11.7, I = 1 A -> Vc = 11.74 <= 11.8 por 30 s.
  for (int k = 0; k < 29; ++k) s.update(1.0, 1.0, 11.7, true, &ev);
  TEST_ASSERT_EQUAL_STRING("persisted", to_string(s.last_sync()));
  s.update(1.0, 1.0, 11.7, true, &ev);
  TEST_ASSERT_DOUBLE_WITHIN(1e-15, p.empty_soc_value, s.soc());
  TEST_ASSERT_EQUAL_STRING("empty", to_string(s.last_sync()));
  TEST_ASSERT_TRUE(ev.contains(EventCode::SocSyncEmpty));
  // soc já abaixo do valor de vazio não sobe.
  SocEstimator s2(p);
  s2.init_persisted(0.03, 0.05);
  for (int k = 0; k < 30; ++k) s2.update(1.0, 0.5, 11.5, true);
  TEST_ASSERT_TRUE(s2.soc() < 0.03);
}

static void test_invalid_sensor_suspends_and_penalizes() {
  const SocParams p = P();
  SocEstimator s(p);
  s.init_persisted(0.6, 0.04);
  for (int k = 0; k < 3; ++k) s.update(1.0, 5.0, 13.0, false);
  TEST_ASSERT_TRUE(s.valid());  // 3 s não é "mais de" 3 s
  s.update(1.0, 5.0, 13.0, false);
  TEST_ASSERT_FALSE(s.valid());
  TEST_ASSERT_DOUBLE_WITHIN(1e-15, 0.6, s.soc());  // integração suspensa
  s.update(1.0, 0.0, 13.1, true);
  TEST_ASSERT_TRUE(s.valid());
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 0.04 + p.invalid_unc_penalty, s.unc());
  // Invalidez curta não penaliza.
  s.update(1.0, 0.0, 13.1, false);
  s.update(1.0, 0.0, 13.1, true);
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 0.04 + p.invalid_unc_penalty, s.unc());
}

static void test_manual_sync_and_uncertainty_cap() {
  const SocParams p = P();
  SocEstimator s(p);
  s.init_from_voltage(13.0);
  s.update(36000.0, 0.3, 13.0, true);  // 3 Ah
  s.update(36000.0, -0.3, 13.0, true);
  for (int k = 0; k < 40; ++k) {
    s.update(3600.0, 1.0, 13.0, true);
    s.update(3600.0, -1.0, 13.0, true);
  }
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, p.unc_max, s.unc());  // limitado em 0,5
  s.sync_manual(0.8);
  TEST_ASSERT_DOUBLE_WITHIN(1e-15, 0.8, s.soc());
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, p.sync_uncertainty, s.unc());
  TEST_ASSERT_EQUAL_STRING("manual", to_string(s.last_sync()));
  TEST_ASSERT_DOUBLE_WITHIN(1e-15, 0.0, s.since_sync_s());
}

static void test_persist_policy() {
  const SocParams p = P();
  SocPersistPolicy pol(p);
  TEST_ASSERT_TRUE(pol.should_persist(0.0, 0.5));
  pol.mark_persisted(0.0, 0.5);
  TEST_ASSERT_FALSE(pol.should_persist(100.0, 0.51));
  TEST_ASSERT_TRUE(pol.should_persist(100.0, 0.5 - p.persist_delta_soc));
  TEST_ASSERT_TRUE(pol.should_persist(p.persist_every_s, 0.5));
  pol.mark_persisted(600.0, 0.48);
  TEST_ASSERT_FALSE(pol.should_persist(1199.0, 0.49));
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_ocv_init_interpolates_and_saturates);
  RUN_TEST(test_persisted_init);
  RUN_TEST(test_discharge_and_drift_uncertainty);
  RUN_TEST(test_charge_uses_coulombic_efficiency);
  RUN_TEST(test_deadband_ignores_small_currents);
  RUN_TEST(test_clamps_between_zero_and_one);
  RUN_TEST(test_full_sync_after_hold_and_event_only_on_edge);
  RUN_TEST(test_full_sync_requires_voltage_and_tail_current);
  RUN_TEST(test_empty_sync_uses_compensated_voltage);
  RUN_TEST(test_invalid_sensor_suspends_and_penalizes);
  RUN_TEST(test_manual_sync_and_uncertainty_cap);
  RUN_TEST(test_persist_policy);
  return UNITY_END();
}
