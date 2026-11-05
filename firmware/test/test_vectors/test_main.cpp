// =============================================================================
// Vetores golden de equivalência firmware <-> Python (docs/06 §7, sem.vectors.v1).
//
//   * test/vectors/sample_vectors.json — PEQUENO, escrito à mão, valida o harness;
//   * test/vectors/rules_vectors.json  — gerado por `python -m smartenergy gen-vectors`;
//     se não existir, o teste é IGNORADO com aviso.
//
// Harness (idêntico ao gerador Python):
//   * regras: saídas iniciais desligadas, last_switch = −inf; após cada passo as
//     saídas passam a ser as ESPERADAS e, se 'on' mudou, last_switch = t_s;
//     segurança ignorada;
//   * SOC: init {soc, unc} -> persisted, ou {v_init} -> OCV;
//   * segurança: SafetySupervisor SEM boot_hold; valores null => sensor inválido;
//     passo com "reset_faults": true chama reset_faults() antes da amostra.
// Comparação: estados/códigos/bools exatos; números 1e-9; duty 1e-6; se o passo
// trouxer "events" (lista de códigos), a sequência de eventos também é comparada.
// Parsing com test/support/mini_json.h (strtod; o ArduinoJson 7 guardaria floats).
// Diretório dos vetores: env SEM_VECTORS_DIR, senão macro SEM_VECTORS_DIR, senão "test/vectors".
// =============================================================================
#include <unity.h>

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <sstream>
#include <string>

#include "../support/mini_json.h"
#include "sem_core/sem_core.h"

#ifndef SEM_VECTORS_DIR
#define SEM_VECTORS_DIR "test/vectors"
#endif

using namespace sem;
using semtest::json::Value;

void setUp() {}
void tearDown() {}

namespace {

constexpr double kTolNum = 1e-9;
constexpr double kTolDuty = 1e-6;

std::string vectors_path(const char* file) {
  const char* env = std::getenv("SEM_VECTORS_DIR");
  return std::string(env != nullptr && env[0] != '\0' ? env : SEM_VECTORS_DIR) + "/" + file;
}

bool read_file(const std::string& path, std::string* out) {
  std::ifstream f(path, std::ios::binary);
  if (!f) return false;
  std::stringstream ss;
  ss << f.rdbuf();
  *out = ss.str();
  return true;
}

double num(const Value& v, double def) { return v.as_num(def); }

/// Constrói os parâmetros a partir de params do JSON (campos ausentes mantêm o padrão da struct).
void load_params(const Value& P, SystemParams* sp) {
  const Value& r = P["rules"];
  RulesParams& R = sp->rules;
  R.normal_min_soc = num(r["normal_min_soc"], R.normal_min_soc);
  R.economy_min_soc = num(r["economy_min_soc"], R.economy_min_soc);
  R.critical_min_soc = num(r["critical_min_soc"], R.critical_min_soc);
  R.hysteresis_soc = num(r["hysteresis_soc"], R.hysteresis_soc);
  R.economy_medium_duty = num(r["economy_medium_duty"], R.economy_medium_duty);
  R.min_dwell_s = num(r["min_dwell_s"], R.min_dwell_s);
  R.surplus_on_soc = num(r["surplus_on_soc"], R.surplus_on_soc);
  R.surplus_off_soc = num(r["surplus_off_soc"], R.surplus_off_soc);
  R.economy_surplus_margin_w = num(r["economy_surplus_margin_w"], R.economy_surplus_margin_w);
  R.deadline_margin_s = num(r["deadline_margin_s"], R.deadline_margin_s);

  LoadSetParams& L = sp->loads;
  L.count = 0;
  for (const Value& ld : P["loads"].arr) {
    TEST_ASSERT_TRUE_MESSAGE(L.count < kMaxLoads, "cargas demais nos vetores");
    LoadParams& x = L.items[L.count++];
    x = LoadParams{};
    x.id = static_cast<int>(ld["id"].as_num());
    copy_cstr(x.key, ld["key"].as_str());
    TEST_ASSERT_TRUE_MESSAGE(parse_priority(ld["priority"].as_str(), &x.priority), "prioridade invalida");
    x.p_nom_w = num(ld["p_nom_w"], 0.0);
    x.dimmable = ld["dimmable"].as_bool(false);
    x.min_duty = num(ld["min_duty"], 0.0);
    const Value& de = ld["deferrable"];
    if (de.is_object()) {
      x.deferrable.enabled = true;
      x.deferrable.daily_energy_wh = num(de["daily_energy_wh"], 0.0);
      x.deferrable.window_start_s = static_cast<int32_t>(de["window_start_s"].as_num());
      x.deferrable.deadline_s = static_cast<int32_t>(de["deadline_s"].as_num());
    }
  }

  const Value& s = P["soc"];
  SocParams& S = sp->soc;
  S.capacity_ah = num(s["capacity_ah"], S.capacity_ah);
  S.coulombic_efficiency = num(s["coulombic_efficiency"], S.coulombic_efficiency);
  S.r_internal_ohm = num(s["r_internal_ohm"], S.r_internal_ohm);
  S.current_deadband_a = num(s["current_deadband_a"], S.current_deadband_a);
  S.full_voltage_v = num(s["full_voltage_v"], S.full_voltage_v);
  S.full_tail_current_a = num(s["full_tail_current_a"], S.full_tail_current_a);
  S.full_hold_s = num(s["full_hold_s"], S.full_hold_s);
  S.empty_voltage_v = num(s["empty_voltage_v"], S.empty_voltage_v);
  S.empty_hold_s = num(s["empty_hold_s"], S.empty_hold_s);
  S.empty_soc_value = num(s["empty_soc_value"], S.empty_soc_value);
  S.unknown_initial_uncertainty = num(s["unknown_initial_uncertainty"], S.unknown_initial_uncertainty);
  S.sync_uncertainty = num(s["sync_uncertainty"], S.sync_uncertainty);
  S.drift_uncertainty_per_ah = num(s["drift_uncertainty_per_ah"], S.drift_uncertainty_per_ah);
  const Value& ocv_soc = s["ocv_soc"];
  const Value& ocv_v = s["ocv_v"];
  TEST_ASSERT_EQUAL_MESSAGE(ocv_soc.size(), ocv_v.size(), "curva OCV inconsistente");
  TEST_ASSERT_TRUE(ocv_v.size() <= static_cast<size_t>(kMaxOcvPoints));
  S.ocv_count = static_cast<int>(ocv_v.size());
  for (int i = 0; i < S.ocv_count; ++i) {
    S.ocv_soc[i] = ocv_soc.at(static_cast<size_t>(i)).as_num();
    S.ocv_v[i] = ocv_v.at(static_cast<size_t>(i)).as_num();
  }

  const Value& f = P["safety"];
  SafetyParams& F = sp->safety;
  F.lvd_v = num(f["lvd_v"], F.lvd_v);
  F.lvd_delay_s = num(f["lvd_delay_s"], F.lvd_delay_s);
  F.soc_hard_min = num(f["soc_hard_min"], F.soc_hard_min);
  F.reconnect_v = num(f["reconnect_v"], F.reconnect_v);
  F.reconnect_min_soc = num(f["reconnect_min_soc"], F.reconnect_min_soc);
  F.bus_ovp_v = num(f["bus_ovp_v"], F.bus_ovp_v);
  F.bus_ovp_delay_s = num(f["bus_ovp_delay_s"], F.bus_ovp_delay_s);
  F.bus_overcurrent_a = num(f["bus_overcurrent_a"], F.bus_overcurrent_a);
  F.bus_overcurrent_delay_ms = num(f["bus_overcurrent_delay_ms"], F.bus_overcurrent_delay_ms);
  F.overcurrent_retry_s = num(f["overcurrent_retry_s"], F.overcurrent_retry_s);
  F.overcurrent_max_retries = static_cast<int>(num(f["overcurrent_max_retries"], F.overcurrent_max_retries));
  F.battery_overtemp_c = num(f["battery_overtemp_c"], F.battery_overtemp_c);
  F.battery_overtemp_release_c = num(f["battery_overtemp_release_c"], F.battery_overtemp_release_c);
  F.sensor_timeout_s = num(f["sensor_timeout_s"], F.sensor_timeout_s);
  F.boot_settle_samples = static_cast<int>(num(f["boot_settle_samples"], F.boot_settle_samples));
  S.sensor_timeout_s = F.sensor_timeout_s;  // o estimador usa o timeout de safety
}

struct Counts {
  int cases = 0;
  int steps = 0;
};

void fail_at(const char* kind, const char* name, int step, const char* what) {
  char msg[320];
  std::snprintf(msg, sizeof(msg), "%s '%s' passo %d: %s", kind, name, step, what);
  TEST_FAIL_MESSAGE(msg);
}

/// Compara a sequência de códigos de eventos (se o passo trouxer "events").
void check_events(const char* kind, const char* name, int step, const Value& expected, const EventList& got) {
  if (!expected.is_array()) return;
  std::string e, g;
  for (const Value& v : expected.arr) e += std::string(v.as_str()) + ",";
  for (int i = 0; i < got.size(); ++i) g += std::string(to_string(got[i].code)) + ",";
  if (e != g) {
    const std::string w = "eventos [" + g + "] != esperado [" + e + "]";
    fail_at(kind, name, step, w.c_str());
  }
}

void run_rules(const Value& cases, const SystemParams& sp, Counts* c) {
  for (const Value& cs : cases.arr) {
    const char* name = cs["name"].as_str("?");
    RulesEngine rules(sp.rules, sp.loads);
    EmsInputs in{};
    int k = 0;
    for (const Value& st : cs["steps"].arr) {
      in.t_s = st["t_s"].as_num();
      in.tod_s = st["tod_s"].as_num();
      in.soc = st["soc"].as_num();
      in.soc_valid = st["soc_valid"].as_bool(true);
      in.p_bat = num(st["p_bat"], 0.0);
      for (int i = 0; i < sp.loads.count; ++i) {
        const char* key = sp.loads.items[i].key;
        in.demand[i] = st["d"][key].as_bool(false);
        in.e_done_wh[i] = num(st["e_done_wh"][key], 0.0);
      }
      const Decision d = rules.decide(in);
      const Value& ex = st["expect"];
      if (ex["state"].is_string() && std::strcmp(to_string(d.state), ex["state"].as_str()) != 0) {
        char w[200];
        std::snprintf(w, sizeof(w), "state %s != esperado %s (reason: %.120s)", to_string(d.state), ex["state"].as_str(),
                      d.reason);
        fail_at("rules", name, k, w);
      }
      for (int i = 0; i < sp.loads.count; ++i) {
        const char* key = sp.loads.items[i].key;
        const Value& el = ex["loads"][key];
        if (!el.is_object()) continue;
        const bool on = el["on"].as_bool();
        const double duty = num(el["duty"], on ? 1.0 : 0.0);
        if (d.commands[i].on != on || std::fabs(d.commands[i].duty - duty) > kTolDuty) {
          char w[240];
          std::snprintf(w, sizeof(w), "carga %s: on=%d duty=%.6f, esperado on=%d duty=%.6f (reason: %.100s)", key,
                        d.commands[i].on, d.commands[i].duty, on, duty, d.reason);
          fail_at("rules", name, k, w);
        }
        // Harness: as saídas passam a ser as esperadas.
        LoadOutput& o = in.outputs[i];
        if (o.on != on) o.last_switch_t_s = in.t_s;
        o.on = on;
        o.duty = duty;
      }
      ++k;
      ++c->steps;
    }
    ++c->cases;
  }
}

void run_soc(const Value& cases, const SystemParams& sp, Counts* c) {
  for (const Value& cs : cases.arr) {
    const char* name = cs["name"].as_str("?");
    SocEstimator est(sp.soc);
    const Value& init = cs["init"];
    if (init["v_init"].is_number()) {
      est.init_from_voltage(init["v_init"].as_num());
    } else {
      est.init_persisted(init["soc"].as_num(), init["unc"].as_num());
    }
    int k = 0;
    for (const Value& st : cs["steps"].arr) {
      const bool ok = st["ok"].as_bool(true) && st["i_a"].is_number() && st["v_v"].is_number();
      EventList ev;
      est.update(st["dt_s"].as_num(), num(st["i_a"], 0.0), num(st["v_v"], 0.0), ok, &ev);
      const Value& ex = st["expect"];
      char w[160];
      if (ex["soc"].is_number() && std::fabs(est.soc() - ex["soc"].num) > kTolNum) {
        std::snprintf(w, sizeof(w), "soc %.15f != %.15f", est.soc(), ex["soc"].num);
        fail_at("soc", name, k, w);
      }
      if (ex["unc"].is_number() && std::fabs(est.unc() - ex["unc"].num) > kTolNum) {
        std::snprintf(w, sizeof(w), "unc %.15f != %.15f", est.unc(), ex["unc"].num);
        fail_at("soc", name, k, w);
      }
      if (ex["last_sync"].is_string() && std::strcmp(to_string(est.last_sync()), ex["last_sync"].as_str()) != 0) {
        std::snprintf(w, sizeof(w), "last_sync %s != %s", to_string(est.last_sync()), ex["last_sync"].as_str());
        fail_at("soc", name, k, w);
      }
      if (ex["valid"].is_bool() && est.valid() != ex["valid"].b) fail_at("soc", name, k, "valid diverge");
      check_events("soc", name, k, ex["events"], ev);
      ++k;
      ++c->steps;
    }
    ++c->cases;
  }
}

void run_safety(const Value& cases, const SystemParams& sp, Counts* c) {
  for (const Value& cs : cases.arr) {
    const char* name = cs["name"].as_str("?");
    SafetySupervisor sup(sp.safety, false);
    int k = 0;
    for (const Value& st : cs["steps"].arr) {
      if (st["reset_faults"].as_bool(false)) sup.reset_faults();
      SafetyInputs in{};
      in.t_s = st["t_s"].as_num();
      in.dt_s = st["dt_s"].as_num();
      in.v_bat_ok = st["v_bat"].is_number();
      in.v_bat = num(st["v_bat"], 0.0);
      in.soc = num(st["soc"], 0.0);
      in.soc_valid = st["soc_valid"].as_bool(false);
      in.v_bus_ok = st["v_bus"].is_number();
      in.v_bus = num(st["v_bus"], 0.0);
      in.i_bus_ok = st["i_bus"].is_number();
      in.i_bus = num(st["i_bus"], 0.0);
      in.t_bat_ok = st["t_bat"].is_number();
      in.t_bat = num(st["t_bat"], 0.0);
      EventList ev;
      const SafetyStatus s = sup.update(in, &ev);
      const Value& ex = st["expect"];
      if (ex["allowed"].is_bool() && s.allowed != ex["allowed"].b) fail_at("safety", name, k, "allowed diverge");
      if (ex["code"].is_string() && std::strcmp(to_string(s.code), ex["code"].as_str()) != 0) {
        char w[96];
        std::snprintf(w, sizeof(w), "code %s != %s", to_string(s.code), ex["code"].as_str());
        fail_at("safety", name, k, w);
      }
      check_events("safety", name, k, ex["events"], ev);
      ++k;
      ++c->steps;
    }
    ++c->cases;
  }
}

/// Executa um arquivo sem.vectors.v1. Retorna false se o arquivo não existe.
bool run_file(const char* file, bool required) {
  std::string text;
  const std::string path = vectors_path(file);
  if (!read_file(path, &text)) {
    if (required) TEST_FAIL_MESSAGE(("arquivo de vetores ausente: " + path).c_str());
    return false;
  }
  Value doc;
  std::string err;
  semtest::json::Parser parser(text);
  TEST_ASSERT_TRUE_MESSAGE(parser.parse(&doc, &err), (path + ": " + err).c_str());
  TEST_ASSERT_EQUAL_STRING_MESSAGE("sem.vectors.v1", doc["schema"].as_str(), "schema dos vetores");
  SystemParams sp{};
  load_params(doc["params"], &sp);
  Counts rules, soc, safety;
  run_rules(doc["rules"], sp, &rules);
  run_soc(doc["soc"], sp, &soc);
  run_safety(doc["safety"], sp, &safety);
  std::printf("[vetores] %s: rules %d casos/%d passos, soc %d/%d, safety %d/%d\n", file, rules.cases, rules.steps,
              soc.cases, soc.steps, safety.cases, safety.steps);
  TEST_ASSERT_TRUE_MESSAGE(rules.steps + soc.steps + safety.steps > 0, "arquivo de vetores sem passos");
  return true;
}

}  // namespace

static void test_sample_vectors_handwritten() { run_file("sample_vectors.json", true); }

static void test_golden_rules_vectors_from_python() {
  if (!run_file("rules_vectors.json", false)) {
    std::printf("[vetores] AVISO: rules_vectors.json ausente — gere com `python -m smartenergy gen-vectors`\n");
    TEST_IGNORE_MESSAGE("rules_vectors.json ausente (gerado pelo backend Python)");
  }
}

int main(int, char**) {
  UNITY_BEGIN();
  RUN_TEST(test_sample_vectors_handwritten);
  RUN_TEST(test_golden_rules_vectors_from_python);
  return UNITY_END();
}
