// Testes do integrador de energia (§4), potência estimada/não atendida, LoadManager,
// utilidades de tempo/calendário, vocabulário e lista de eventos.
#include <unity.h>

#include <cmath>

#include "../support/fixtures.h"

using namespace sem;

void setUp() {}
void tearDown() {}

static void test_trapezoid_constant_and_ramp() {
  EnergyIntegrator e;
  PowerSample s{};
  s.p_pv = 10.0;
  e.add(1.0, s);  // primeira amostra: só memoriza
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 0.0, e.today().pv_in_wh);
  for (int k = 0; k < 3600; ++k) e.add(1.0, s);
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, 10.0, e.today().pv_in_wh);
  EnergyIntegrator r;
  PowerSample a{};
  r.add(0.0, a);
  a.p_bus = 10.0;
  r.add(3600.0, a);  // rampa 0 -> 10 W em 1 h = 5 Wh
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 5.0, r.today().load_wh);
}

static void test_battery_split_by_sample_sign() {
  EnergyIntegrator e;
  PowerSample s{};
  s.p_bat = 10.0;
  e.add(0.0, s);
  s.p_bat = -10.0;
  e.add(3600.0, s);
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 5.0, e.today().bat_dis_wh);
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 5.0, e.today().bat_chg_wh);
  s.p_bat = -10.0;
  e.add(3600.0, s);
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 15.0, e.today().bat_chg_wh);
}

static void test_day_rollover_and_continuity() {
  EnergyIntegrator e;
  PowerSample s{};
  s.p_load[2] = 16.4;
  e.add(0.0, s);
  e.add(3600.0, s);
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 16.4, e.today().load_wh_by[2]);
  TEST_ASSERT_FALSE(e.roll_day(20000));  // primeira data: adota sem zerar
  TEST_ASSERT_TRUE(e.has_day());
  TEST_ASSERT_FALSE(e.roll_day(20000));
  TEST_ASSERT_TRUE(e.roll_day(20001));  // meia-noite local: zera
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 0.0, e.today().load_wh_by[2]);
  e.break_continuity();
  e.add(3600.0, s);  // sem trapézio após descontinuidade
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 0.0, e.today().load_wh_by[2]);
  EnergyCounters c{};
  c.pv_in_wh = 12.0;
  e.restore(20001, c);
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 12.0, e.today().pv_in_wh);
}

static void test_load_power_estimation_and_unserved() {
  const SystemParams p = semtest::params();
  const LoadParams& led = p.loads.items[p.loads.index_of_key("led")];
  const LoadParams& res = p.loads.items[p.loads.index_of_key("res")];
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, led.p_nom_w, estimate_load_power_w(led, led.v_ref_v, 1.0));
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, led.p_nom_w * std::pow(14.0 / 12.8, 1.5), estimate_load_power_w(led, 14.0, 1.0));
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, res.p_nom_w / 2.0, estimate_load_power_w(res, 12.8, 0.5));
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 0.0, estimate_load_power_w(res, 0.0, 1.0));
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 0.0, estimate_load_power_w(res, 12.8, 0.0));

  bool demand[kMaxLoads] = {};
  LoadOutput out[kMaxLoads] = {};
  demand[0] = true;  // led pedida e desligada
  demand[1] = true;  // fan pedido a 60 %
  out[1].on = true;
  out[1].duty = 0.6;
  demand[2] = true;  // deslocável não entra
  const double fan_nom = p.loads.items[1].p_nom_w;
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, led.p_nom_w + 0.4 * fan_nom, unserved_power_w(p.loads, demand, out, 12.8));
}

static void test_load_manager_applies_safety_and_counts() {
  const SystemParams p = semtest::params();
  LoadManager lm(p.loads);
  Decision d{};
  d.n_loads = p.loads.count;
  d.commands[0] = {true, 1.0};
  d.commands[1] = {true, 0.2};  // abaixo de min_duty -> normalizado
  d.commands[2] = {false, 0.0};
  uint32_t ch = lm.apply(d, true, 5.0);
  TEST_ASSERT_EQUAL_HEX32(0x3, ch);
  TEST_ASSERT_TRUE(lm.output(0).on);
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, p.loads.items[1].min_duty, lm.output(1).duty);
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 5.0, lm.output(0).last_switch_t_s);
  TEST_ASSERT_TRUE(std::isinf(lm.output(2).last_switch_t_s));
  TEST_ASSERT_EQUAL_UINT32(1, lm.switch_count(0));
  // Segurança: on_final = on_EMS AND allowed.
  ch = lm.apply(d, false, 6.0);
  TEST_ASSERT_FALSE(lm.output(0).on);
  TEST_ASSERT_FALSE(lm.output(1).on);
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 0.0, lm.output(1).duty);
  TEST_ASSERT_TRUE(lm.ems_on(0));
  TEST_ASSERT_EQUAL_UINT32(2, lm.switch_count(0));
  // Mudança só de duty não conta comutação, mas é sinalizada.
  lm.apply(d, true, 7.0);
  d.commands[1].duty = 0.8;
  ch = lm.apply(d, true, 8.0);
  TEST_ASSERT_EQUAL_HEX32(0x2, ch);
  TEST_ASSERT_EQUAL_UINT32(3, lm.switch_count(1));
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 7.0, lm.output(1).last_switch_t_s);
  // Carga não dimerizável ligada sempre a duty 1.
  d.commands[0].duty = 0.3;
  lm.apply(d, true, 9.0);
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 1.0, lm.output(0).duty);
}

static void test_time_utilities() {
  int32_t s = 0;
  TEST_ASSERT_TRUE(parse_hhmm("07:00", &s));
  TEST_ASSERT_EQUAL_INT32(25200, s);
  TEST_ASSERT_TRUE(parse_hhmm("24:00", &s));
  TEST_ASSERT_EQUAL_INT32(86400, s);
  TEST_ASSERT_FALSE(parse_hhmm("24:01", &s));
  TEST_ASSERT_FALSE(parse_hhmm("12:60", &s));
  TEST_ASSERT_FALSE(parse_hhmm("12:00x", &s));
  char buf[8];
  format_hhmm(79200, buf, sizeof(buf));
  TEST_ASSERT_EQUAL_STRING("22:00", buf);
  // 2026-11-16 00:00 local (UTC−3) = 2026-11-16 03:00 UTC = 1763262000000 ms.
  const int64_t midnight_ms = 1794798000000LL;
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, 0.0, tod_from_epoch_ms(midnight_ms, -10800));
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, 86399.0, tod_from_epoch_ms(midnight_ms - 1000, -10800));
  char day[12];
  format_local_day(midnight_ms, -10800, day, sizeof(day));
  TEST_ASSERT_EQUAL_STRING("2026-11-16", day);
  format_local_day(midnight_ms - 1, -10800, day, sizeof(day));
  TEST_ASSERT_EQUAL_STRING("2026-11-15", day);
  TEST_ASSERT_EQUAL_INT32(local_day_index(midnight_ms, -10800), days_from_civil(2026, 11, 16));
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, 3600.0, seconds_until(75600.0, 79200));
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, 86400.0 - 3600.0, seconds_until(82800.0, 79200));
}

static void test_vocabulary_round_trip() {
  const char* codes[] = {"OK", "BOOT", "LVD", "OVP", "OC", "OC_LOCKOUT", "OT", "SENSOR"};
  for (const char* c : codes) {
    SafetyCode sc;
    TEST_ASSERT_TRUE(parse_safety_code(c, &sc));
    TEST_ASSERT_EQUAL_STRING(c, to_string(sc));
  }
  EmsState st;
  TEST_ASSERT_TRUE(parse_state("ECONOMY", &st));
  TEST_ASSERT_EQUAL_INT(2, static_cast<int>(st));
  EmsMode m;
  TEST_ASSERT_FALSE(parse_mode("RULES", &m));  // grafia exata do contrato
  SyncKind k;
  TEST_ASSERT_TRUE(parse_sync_kind("ocv_init", &k));
  Priority pr;
  TEST_ASSERT_TRUE(parse_priority("medium", &pr));
  TEST_ASSERT_EQUAL_STRING("LVD_TRIP", to_string(EventCode::LvdTrip));
  TEST_ASSERT_EQUAL_STRING("FAULTS_RESET", to_string(EventCode::FaultsReset));
  TEST_ASSERT_EQUAL_STRING("critical", to_string(EventLevel::Critical));
}

static void test_event_list_capacity() {
  EventList ev;
  for (int k = 0; k < EventList::kCapacity + 3; ++k) {
    ev.push(EventCode::Boot, EventLevel::Info, "evento %d", k).with_number("k", k).with_text("t", "x").with_number("z", 1);
  }
  TEST_ASSERT_EQUAL(EventList::kCapacity, ev.size());
  TEST_ASSERT_EQUAL_UINT32(3, ev.dropped());
  TEST_ASSERT_EQUAL_STRING("evento 0", ev[0].msg);
  TEST_ASSERT_EQUAL(2, ev[0].n_data);  // dados além de 2 são ignorados
  ev.clear();
  TEST_ASSERT_TRUE(ev.empty());
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_trapezoid_constant_and_ramp);
  RUN_TEST(test_battery_split_by_sample_sign);
  RUN_TEST(test_day_rollover_and_continuity);
  RUN_TEST(test_load_power_estimation_and_unserved);
  RUN_TEST(test_load_manager_applies_safety_and_counts);
  RUN_TEST(test_time_utilities);
  RUN_TEST(test_vocabulary_round_trip);
  RUN_TEST(test_event_list_capacity);
  return UNITY_END();
}
