// Testes do perfil de demanda (janelas com cruzamento de meia-noite), operação
// convencional (§3.1), salvaguardas/TTL do modo preditivo (§3.4) e do controlador de modos.
#include <unity.h>

#include "../support/fixtures.h"

using namespace sem;

void setUp() {}
void tearDown() {}

namespace {
int32_t hhmm(const char* s) {
  int32_t v = -1;
  parse_hhmm(s, &v);
  return v;
}
}  // namespace

static void test_demand_profile_from_yaml() {
  const SystemParams p = semtest::params();
  DemandProfile dp(p.loads, p.demand);
  const int led = p.loads.index_of_key("led");
  const int fan = p.loads.index_of_key("fan");
  const int res = p.loads.index_of_key("res");
  TEST_ASSERT_TRUE(dp.demand(led, hhmm("06:00")));
  TEST_ASSERT_FALSE(dp.demand(led, hhmm("07:00")));  // fim exclusivo
  TEST_ASSERT_TRUE(dp.demand(led, hhmm("05:30")));   // início inclusivo
  TEST_ASSERT_TRUE(dp.demand(led, hhmm("22:59")));
  TEST_ASSERT_FALSE(dp.demand(led, hhmm("23:00")));
  TEST_ASSERT_TRUE(dp.demand(fan, hhmm("12:00")));
  TEST_ASSERT_FALSE(dp.demand(fan, hhmm("17:30")));
  TEST_ASSERT_FALSE(dp.demand(res, hhmm("12:00")));  // deslocável não tem agenda
  TEST_ASSERT_FALSE(dp.demand(-1, 0.0));
  bool all[kMaxLoads];
  dp.fill(hhmm("19:30"), all);
  TEST_ASSERT_TRUE(all[led] && all[fan] && !all[res]);
  TEST_ASSERT_TRUE(dp.deferrable_pending(res, hhmm("12:00"), 10.0));
  TEST_ASSERT_FALSE(dp.deferrable_pending(res, hhmm("12:00"), 40.0));
  TEST_ASSERT_FALSE(dp.deferrable_pending(res, hhmm("23:00"), 0.0));
}

static void test_windows_crossing_midnight() {
  SystemParams p = semtest::params();
  const int led = p.loads.index_of_key("led");
  p.demand.schedule[led].count = 1;
  p.demand.schedule[led].windows[0] = {hhmm("23:00"), hhmm("01:00")};
  DemandProfile dp(p.loads, p.demand);
  TEST_ASSERT_TRUE(dp.demand(led, hhmm("23:00")));
  TEST_ASSERT_TRUE(dp.demand(led, hhmm("23:59")));
  TEST_ASSERT_TRUE(dp.demand(led, 0.0));
  TEST_ASSERT_TRUE(dp.demand(led, hhmm("00:59")));
  TEST_ASSERT_FALSE(dp.demand(led, hhmm("01:00")));
  TEST_ASSERT_FALSE(dp.demand(led, hhmm("22:59")));
  TEST_ASSERT_TRUE(dp.demand(led, 86400.0 + 1800.0));  // tod reduzido módulo 24 h
  TEST_ASSERT_TRUE(dp.demand(led, -1800.0));           // 23:30 do dia anterior
  TEST_ASSERT_FALSE(in_window(100.0, 3600, 3600));     // janela vazia
}

static void test_conventional_strategy() {
  const SystemParams p = semtest::params();
  ConventionalStrategy conv(p.loads, p.conventional);
  const int led = p.loads.index_of_key("led");
  const int fan = p.loads.index_of_key("fan");
  const int res = p.loads.index_of_key("res");
  EmsInputs in = semtest::make_inputs(0.0, hhmm("18:30"), 0.05);  // SOC baixo: convencional ignora
  in.demand[led] = true;
  in.demand[fan] = false;
  in.e_done_wh[res] = 10.0;
  Decision d = conv.decide(in);
  TEST_ASSERT_EQUAL_STRING("NORMAL", to_string(d.state));
  TEST_ASSERT_EQUAL_STRING("conventional", to_string(d.mode));
  TEST_ASSERT_TRUE(d.commands[led].on);
  TEST_ASSERT_FALSE(d.commands[fan].on);
  TEST_ASSERT_TRUE(d.commands[res].on);
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 1.0, d.commands[res].duty);
  in.demand[fan] = true;
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 1.0, conv.decide(in).commands[fan].duty);
  in.e_done_wh[res] = 40.0;  // energia diária cumprida
  TEST_ASSERT_FALSE(conv.decide(in).commands[res].on);
  in.e_done_wh[res] = 0.0;
  in.tod_s = hhmm("17:59");
  TEST_ASSERT_FALSE(conv.decide(in).commands[res].on);
  in.tod_s = hhmm("22:00");
  TEST_ASSERT_FALSE(conv.decide(in).commands[res].on);
}

static void test_plan_expiry_time() {
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, 550.0, plan_expiry_t_s(1000000, 60, true, 1010000, 500.0));
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, 560.0, plan_expiry_t_s(1000000, 60, false, 1010000, 500.0));
}

namespace {
LoadPlan full_plan(const SystemParams& p, bool led_on, bool fan_on, double fan_duty, bool res_on) {
  LoadPlan plan{};
  const int led = p.loads.index_of_key("led");
  const int fan = p.loads.index_of_key("fan");
  const int res = p.loads.index_of_key("res");
  plan.present[led] = plan.present[fan] = plan.present[res] = true;
  plan.cmd[led] = {led_on, led_on ? 1.0 : 0.0};
  plan.cmd[fan] = {fan_on, fan_duty};
  plan.cmd[res] = {res_on, res_on ? 1.0 : 0.0};
  return plan;
}
}  // namespace

static void test_predictive_guard_safeguards_and_ttl() {
  const SystemParams p = semtest::params();
  const int led = p.loads.index_of_key("led");
  const int fan = p.loads.index_of_key("fan");
  const int res = p.loads.index_of_key("res");
  EmsController ems(p);
  EventList ev;
  ems.set_mode(EmsMode::Predictive, &ev);
  TEST_ASSERT_TRUE(ev.contains(EventCode::ModeChange));

  EmsInputs in = semtest::make_inputs(0.0, hhmm("12:30"), 0.6);
  in.demand[led] = true;
  in.demand[fan] = false;
  // Sem plano: fallback para regras.
  Decision d = ems.evaluate(in, &ev);
  TEST_ASSERT_TRUE(d.fallback);
  TEST_ASSERT_EQUAL_STRING("predictive", to_string(d.mode));

  ems.accept_plan(full_plan(p, true, true, 0.5, true), 0.0, 100.0);
  in.t_s = 10.0;
  d = ems.evaluate(in, &ev);
  TEST_ASSERT_FALSE(d.fallback);
  TEST_ASSERT_TRUE(d.commands[led].on);
  TEST_ASSERT_FALSE(d.commands[fan].on);  // on = cmd AND demanda
  TEST_ASSERT_TRUE(d.commands[res].on);   // deslocável segue o plano
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, 10.0, ems.cmd_age_s(10.0));

  in.demand[fan] = true;
  d = ems.evaluate(in, &ev);
  TEST_ASSERT_TRUE(d.commands[fan].on);
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 0.5, d.commands[fan].duty);

  // Salvaguarda de SOC: medium e low desligadas abaixo de critical_min_soc.
  in.soc = p.rules.critical_min_soc - 0.01;
  d = ems.evaluate(in, &ev);
  TEST_ASSERT_TRUE(d.commands[led].on);
  TEST_ASSERT_FALSE(d.commands[fan].on);
  TEST_ASSERT_FALSE(d.commands[res].on);
  TEST_ASSERT_TRUE(semtest::contains(d.reason, "salvaguarda"));
  in.soc = 0.6;
  in.soc_valid = false;  // SOC inválido tratado como baixo (conservador)
  TEST_ASSERT_FALSE(ems.evaluate(in, &ev).commands[fan].on);
  in.soc_valid = true;

  // Vale até o instante de expiração (inclusive); depois -> PLAN_EXPIRED + fallback.
  ev.clear();
  in.t_s = 100.0;
  TEST_ASSERT_FALSE(ems.evaluate(in, &ev).fallback);
  in.t_s = 100.5;
  d = ems.evaluate(in, &ev);
  TEST_ASSERT_TRUE(d.fallback);
  TEST_ASSERT_TRUE(ev.contains(EventCode::PlanExpired));
  TEST_ASSERT_FALSE(ems.has_plan());
  TEST_ASSERT_TRUE(ems.cmd_age_s(100.5) < 0.0);
}

static void test_predictive_duty_normalization_and_missing_loads() {
  const SystemParams p = semtest::params();
  const int led = p.loads.index_of_key("led");
  const int fan = p.loads.index_of_key("fan");
  const int res = p.loads.index_of_key("res");
  EmsController ems(p);
  ems.set_mode(EmsMode::Predictive);
  LoadPlan plan{};
  plan.present[fan] = true;
  plan.cmd[fan] = {true, 0.1};  // abaixo de min_duty: limitado
  ems.accept_plan(plan, 0.0, 1000.0);
  EmsInputs in = semtest::make_inputs(1.0, hhmm("19:00"), 0.8);
  in.demand[led] = in.demand[fan] = true;
  const Decision d = ems.evaluate(in);
  TEST_ASSERT_FALSE(d.commands[led].on);  // ausente do plano => desligada
  TEST_ASSERT_FALSE(d.commands[res].on);
  TEST_ASSERT_TRUE(d.commands[fan].on);
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, p.loads.items[fan].min_duty, d.commands[fan].duty);
}

static void test_mode_controller_switching() {
  const SystemParams p = semtest::params();
  EmsController ems(p);
  TEST_ASSERT_EQUAL_STRING(to_string(p.default_mode), to_string(ems.mode()));
  EventList ev;
  ems.set_mode(EmsMode::Rules, &ev);
  TEST_ASSERT_TRUE(ev.empty());  // sem mudança, sem evento
  ems.set_mode(EmsMode::Predictive, &ev);
  ems.accept_plan(LoadPlan{}, 0.0, 100.0);
  TEST_ASSERT_TRUE(ems.has_plan());
  ems.set_mode(EmsMode::Conventional, &ev);
  TEST_ASSERT_FALSE(ems.has_plan());  // sair do preditivo descarta o plano
  TEST_ASSERT_EQUAL(2, ev.size());

  EmsInputs in = semtest::make_inputs(0.0, hhmm("12:00"), 0.1);
  Decision d1 = ems.evaluate(in);
  TEST_ASSERT_EQUAL_STRING("NORMAL", to_string(d1.state));  // convencional: estado fixo
  TEST_ASSERT_FALSE(d1.has_prev_state);
  ems.set_mode(EmsMode::Rules);
  Decision d2 = ems.evaluate(in);
  TEST_ASSERT_EQUAL_STRING("PROTECTION", to_string(d2.state));
  TEST_ASSERT_TRUE(d2.has_prev_state);
  TEST_ASSERT_EQUAL_STRING("NORMAL", to_string(d2.prev_state));
  TEST_ASSERT_TRUE(decision_differs(d1, d2));
  TEST_ASSERT_FALSE(decision_differs(d2, d2));
  Decision d3 = d2;
  d3.commands[0].duty += 1e-9;
  TEST_ASSERT_FALSE(decision_differs(d2, d3));
  d3.commands[0].duty += 1e-3;
  TEST_ASSERT_TRUE(decision_differs(d2, d3));
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_demand_profile_from_yaml);
  RUN_TEST(test_windows_crossing_midnight);
  RUN_TEST(test_conventional_strategy);
  RUN_TEST(test_plan_expiry_time);
  RUN_TEST(test_predictive_guard_safeguards_and_ttl);
  RUN_TEST(test_predictive_duty_normalization_and_missing_loads);
  RUN_TEST(test_mode_controller_switching);
  return UNITY_END();
}
