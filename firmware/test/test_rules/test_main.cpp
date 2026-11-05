// Testes do controle por regras (docs/06 §3.2): histerese nas duas direções,
// ações por estado e carga deslocável (latch, excedente, prazo, permanência).
#include <unity.h>

#include "../support/fixtures.h"

using namespace sem;

void setUp() {}
void tearDown() {}

namespace {

struct Bench {
  SystemParams p = semtest::params();
  RulesEngine rules{p.rules, p.loads};
  EmsInputs in{};
  int led = p.loads.index_of_key("led");
  int fan = p.loads.index_of_key("fan");
  int res = p.loads.index_of_key("res");

  Bench() {
    for (auto& o : in.outputs) o = LoadOutput{};
  }
  /// Um passo do harness: avalia e adota a decisão como saída.
  Decision step(double t_s, double tod_s, double soc, bool valid = true, double p_bat = 0.0, bool d_led = true,
                bool d_fan = true, double e_done = 0.0) {
    in.t_s = t_s;
    in.tod_s = tod_s;
    in.soc = soc;
    in.soc_valid = valid;
    in.p_bat = p_bat;
    in.demand[led] = d_led;
    in.demand[fan] = d_fan;
    in.e_done_wh[res] = e_done;
    const Decision d = rules.decide(in);
    semtest::adopt(&in, d, t_s);
    return d;
  }
};

const char* st(const Decision& d) { return to_string(d.state); }
constexpr double kNoon = 12 * 3600.0;
constexpr double kEvening = 19 * 3600.0;

}  // namespace

static void test_first_evaluation_without_hysteresis() {
  const double cases[][2] = {{0.75, 3}, {0.70, 3}, {0.6999, 2}, {0.40, 2}, {0.30, 1}, {0.20, 1}, {0.19, 0}, {0.0, 0}};
  for (const auto& c : cases) {
    Bench b;
    const Decision d = b.step(0, kNoon, c[0]);
    TEST_ASSERT_EQUAL_INT(static_cast<int>(c[1]), static_cast<int>(d.state));
    TEST_ASSERT_FALSE(d.has_prev_state);
  }
}

static void test_hysteresis_downward_immediate_upward_needs_margin() {
  Bench b;
  TEST_ASSERT_EQUAL_STRING("NORMAL", st(b.step(0, kNoon, 0.75)));
  Decision d = b.step(1, kNoon, 0.69);
  TEST_ASSERT_EQUAL_STRING("ECONOMY", st(d));
  TEST_ASSERT_TRUE(semtest::contains(d.reason, "-> ECONOMY"));
  TEST_ASSERT_EQUAL_STRING("NORMAL", to_string(d.prev_state));
  TEST_ASSERT_EQUAL_STRING("ECONOMY", st(b.step(2, kNoon, 0.72)));
  TEST_ASSERT_EQUAL_STRING("ECONOMY", st(b.step(3, kNoon, 0.7499)));
  TEST_ASSERT_EQUAL_STRING("NORMAL", st(b.step(4, kNoon, 0.75)));
  TEST_ASSERT_EQUAL_STRING("NORMAL", st(b.step(5, kNoon, 0.70)));
}

static void test_multi_level_transitions_both_directions() {
  Bench b;
  b.step(0, kNoon, 0.80);
  TEST_ASSERT_EQUAL_STRING("PROTECTION", st(b.step(1, kNoon, 0.15)));  // desce vários níveis
  TEST_ASSERT_EQUAL_STRING("PROTECTION", st(b.step(2, kNoon, 0.22)));
  TEST_ASSERT_EQUAL_STRING("CRITICAL", st(b.step(3, kNoon, 0.25)));
  TEST_ASSERT_EQUAL_STRING("CRITICAL", st(b.step(4, kNoon, 0.44)));
  TEST_ASSERT_EQUAL_STRING("ECONOMY", st(b.step(5, kNoon, 0.45)));
  TEST_ASSERT_EQUAL_STRING("CRITICAL", st(b.step(6, kNoon, 0.39)));
  TEST_ASSERT_EQUAL_STRING("NORMAL", st(b.step(7, kNoon, 0.80)));  // sobe vários níveis
}

static void test_invalid_soc_caps_at_critical() {
  Bench b;
  b.step(0, kNoon, 0.9);
  Decision d = b.step(1, kNoon, 0.9, false);
  TEST_ASSERT_EQUAL_STRING("CRITICAL", st(d));
  TEST_ASSERT_TRUE(semtest::contains(d.reason, "soc_invalid"));
  Bench c;
  c.step(0, kNoon, 0.1);
  TEST_ASSERT_EQUAL_STRING("PROTECTION", st(c.step(1, kNoon, 0.9, false)));
}

static void test_actions_per_state() {
  Bench b;
  // NORMAL: led e fan seguem a demanda, fan a duty 1.
  Decision d = b.step(0, kEvening, 0.8);
  TEST_ASSERT_TRUE(d.commands[b.led].on);
  TEST_ASSERT_TRUE(d.commands[b.fan].on);
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 1.0, d.commands[b.fan].duty);
  d = b.step(1, kEvening, 0.8, true, 0.0, false, false);
  TEST_ASSERT_FALSE(d.commands[b.led].on);
  TEST_ASSERT_FALSE(d.commands[b.fan].on);
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 0.0, d.commands[b.fan].duty);
  // ECONOMY: fan reduzido.
  d = b.step(2, kEvening, 0.5);
  TEST_ASSERT_TRUE(d.commands[b.led].on);
  TEST_ASSERT_TRUE(d.commands[b.fan].on);
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, b.p.rules.economy_medium_duty, d.commands[b.fan].duty);
  // CRITICAL: só high.
  d = b.step(3, kEvening, 0.3);
  TEST_ASSERT_TRUE(d.commands[b.led].on);
  TEST_ASSERT_FALSE(d.commands[b.fan].on);
  TEST_ASSERT_FALSE(d.commands[b.res].on);
  // PROTECTION: tudo desligado.
  d = b.step(4, kEvening, 0.1);
  for (int i = 0; i < d.n_loads; ++i) TEST_ASSERT_FALSE(d.commands[i].on);
}

static void test_economy_duty_respects_min_duty() {
  Bench b;
  b.p.rules.economy_medium_duty = 0.3;  // abaixo do min_duty (0,40)
  const Decision d = b.step(0, kEvening, 0.5);
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, b.p.loads.items[b.fan].min_duty, d.commands[b.fan].duty);
}

static void test_deferrable_surplus_latch_and_dwell() {
  Bench b;
  Decision d = b.step(0, kNoon, 0.91);
  TEST_ASSERT_TRUE(d.commands[b.res].on);
  TEST_ASSERT_TRUE(semtest::contains(d.reason, "res: excedente"));
  TEST_ASSERT_TRUE(b.rules.latch(b.res));
  d = b.step(10, kNoon + 10, 0.85);  // entre off (0,80) e on (0,90): latch mantém
  TEST_ASSERT_TRUE(d.commands[b.res].on);
  d = b.step(100, kNoon + 100, 0.79);  // latch cai, mas permanência mínima segura
  TEST_ASSERT_FALSE(b.rules.latch(b.res));
  TEST_ASSERT_TRUE(d.commands[b.res].on);
  TEST_ASSERT_TRUE(semtest::contains(d.reason, "permanencia"));
  d = b.step(299, kNoon + 299, 0.79);
  TEST_ASSERT_TRUE(d.commands[b.res].on);
  d = b.step(300, kNoon + 300, 0.79);
  TEST_ASSERT_FALSE(d.commands[b.res].on);
  // Religar também respeita a permanência mínima.
  d = b.step(310, kNoon + 310, 0.95);
  TEST_ASSERT_FALSE(d.commands[b.res].on);
  d = b.step(600, kNoon + 600, 0.95);
  TEST_ASSERT_TRUE(d.commands[b.res].on);
}

static void test_deferrable_deadline_forcing() {
  Bench b;
  const LoadParams& r = b.p.loads.items[b.res];
  const double t_need = r.deferrable.daily_energy_wh / r.p_nom_w * 3600.0;
  const double tod_force = r.deferrable.deadline_s - (t_need + b.p.rules.deadline_margin_s);
  Decision d = b.step(0, tod_force - 60.0, 0.75);
  TEST_ASSERT_FALSE(d.commands[b.res].on);
  d = b.step(1, tod_force + 0.5, 0.75);
  TEST_ASSERT_TRUE(d.commands[b.res].on);
  TEST_ASSERT_TRUE(semtest::contains(d.reason, "res: prazo"));
  // Energia quase cumprida: sem forçar, mas permanência segura ligada.
  d = b.step(10, tod_force + 10, 0.75, true, 0.0, true, true, 39.9);
  TEST_ASSERT_TRUE(d.commands[b.res].on);
  // Energia concluída: desliga imediatamente (sem permanência mínima).
  d = b.step(20, tod_force + 20, 0.75, true, 0.0, true, true, r.deferrable.daily_energy_wh);
  TEST_ASSERT_FALSE(d.commands[b.res].on);
  TEST_ASSERT_TRUE(semtest::contains(d.reason, "concluida"));
}

static void test_deferrable_window_is_half_open_and_immediate() {
  Bench b;
  const LoadParams& r = b.p.loads.items[b.res];
  Decision d = b.step(0, r.deferrable.window_start_s - 1, 0.95);
  TEST_ASSERT_FALSE(d.commands[b.res].on);
  d = b.step(1, r.deferrable.window_start_s, 0.95);
  TEST_ASSERT_TRUE(d.commands[b.res].on);
  d = b.step(2, r.deferrable.deadline_s, 0.95);  // [início, fim): fim exclusivo
  TEST_ASSERT_FALSE(d.commands[b.res].on);
  TEST_ASSERT_FALSE(b.rules.latch(b.res));
}

static void test_deferrable_in_economy_needs_pv_surplus() {
  Bench b;
  const LoadParams& r = b.p.loads.items[b.res];
  const double margin = b.p.rules.economy_surplus_margin_w;
  Decision d = b.step(0, kNoon, 0.6, true, -(r.p_nom_w + margin) + 0.1);
  TEST_ASSERT_EQUAL_STRING("ECONOMY", st(d));
  TEST_ASSERT_FALSE(d.commands[b.res].on);
  d = b.step(1, kNoon + 1, 0.6, true, -(r.p_nom_w + margin));
  TEST_ASSERT_TRUE(d.commands[b.res].on);
  // Ligada: mantém enquanto p_bat <= margem.
  d = b.step(400, kNoon + 400, 0.6, true, margin);
  TEST_ASSERT_TRUE(d.commands[b.res].on);
  d = b.step(800, kNoon + 800, 0.6, true, margin + 0.1);
  TEST_ASSERT_FALSE(d.commands[b.res].on);
}

static void test_deferrable_off_immediately_in_critical() {
  Bench b;
  Decision d = b.step(0, kNoon, 0.95);
  TEST_ASSERT_TRUE(d.commands[b.res].on);
  d = b.step(1, kNoon + 1, 0.3);  // CRITICAL: imediato
  TEST_ASSERT_FALSE(d.commands[b.res].on);
  TEST_ASSERT_FALSE(b.rules.latch(b.res));
}

static void test_dwell_does_not_apply_to_fixed_loads() {
  Bench b;
  Decision d = b.step(0, kEvening, 0.8, true, 0.0, true, true);
  TEST_ASSERT_TRUE(d.commands[b.led].on);
  d = b.step(1, kEvening + 1, 0.8, true, 0.0, false, true);
  TEST_ASSERT_FALSE(d.commands[b.led].on);
  d = b.step(2, kEvening + 2, 0.8, true, 0.0, true, true);
  TEST_ASSERT_TRUE(d.commands[b.led].on);
}

static void test_reset_forgets_level() {
  Bench b;
  b.step(0, kNoon, 0.72);
  b.step(1, kNoon, 0.65);
  TEST_ASSERT_EQUAL_STRING("ECONOMY", to_string(b.rules.state()));
  b.rules.reset();
  TEST_ASSERT_FALSE(b.rules.has_state());
  TEST_ASSERT_EQUAL_STRING("NORMAL", st(b.step(2, kNoon, 0.72)));  // reclassifica sem histerese
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_first_evaluation_without_hysteresis);
  RUN_TEST(test_hysteresis_downward_immediate_upward_needs_margin);
  RUN_TEST(test_multi_level_transitions_both_directions);
  RUN_TEST(test_invalid_soc_caps_at_critical);
  RUN_TEST(test_actions_per_state);
  RUN_TEST(test_economy_duty_respects_min_duty);
  RUN_TEST(test_deferrable_surplus_latch_and_dwell);
  RUN_TEST(test_deferrable_deadline_forcing);
  RUN_TEST(test_deferrable_window_is_half_open_and_immediate);
  RUN_TEST(test_deferrable_in_economy_needs_pv_surplus);
  RUN_TEST(test_deferrable_off_immediately_in_critical);
  RUN_TEST(test_dwell_does_not_apply_to_fixed_loads);
  RUN_TEST(test_reset_forgets_level);
  return UNITY_END();
}
