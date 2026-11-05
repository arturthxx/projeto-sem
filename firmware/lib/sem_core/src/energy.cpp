// Integração de energia (docs/06 §4), potência estimada das cargas e saídas lógicas.
#include "sem_core/energy.h"

#include <cmath>

namespace sem {

double estimate_load_power_w(const LoadParams& load, double v, double duty) {
  if (duty <= 0.0 || v <= 0.0 || load.v_ref_v <= 0.0) return 0.0;
  return load.p_nom_w * std::pow(v / load.v_ref_v, load.voltage_exponent) * duty;
}

double unserved_power_w(const LoadSetParams& loads, const bool demand[kMaxLoads], const LoadOutput out[kMaxLoads],
                        double v) {
  double total = 0.0;
  for (int i = 0; i < loads.count && i < kMaxLoads; ++i) {
    const LoadParams& L = loads.items[i];
    if (L.deferrable.enabled || !demand[i]) continue;
    const double p_dem = estimate_load_power_w(L, v, 1.0);
    const double p_srv = out[i].on ? estimate_load_power_w(L, v, out[i].duty) : 0.0;
    if (p_dem > p_srv) total += p_dem - p_srv;
  }
  return total;
}

namespace {
inline double pos(double x) { return x > 0.0 ? x : 0.0; }
inline double trap_wh(double a, double b, double dt_s) { return (a + b) / 2.0 * dt_s / 3600.0; }
}  // namespace

void EnergyIntegrator::add(double dt_s, const PowerSample& s) {
  if (has_prev_ && dt_s > 0.0) {
    e_.pv_in_wh += trap_wh(pos(s.p_pv), pos(prev_.p_pv), dt_s);
    e_.bat_dis_wh += trap_wh(pos(s.p_bat), pos(prev_.p_bat), dt_s);
    e_.bat_chg_wh += trap_wh(pos(-s.p_bat), pos(-prev_.p_bat), dt_s);
    e_.load_wh += trap_wh(pos(s.p_bus), pos(prev_.p_bus), dt_s);
    e_.unserved_wh += trap_wh(pos(s.p_unserved), pos(prev_.p_unserved), dt_s);
    for (int i = 0; i < kMaxLoads; ++i) e_.load_wh_by[i] += trap_wh(pos(s.p_load[i]), pos(prev_.p_load[i]), dt_s);
  }
  prev_ = s;
  has_prev_ = true;
}

bool EnergyIntegrator::roll_day(int32_t day_index) {
  if (!has_day_) {
    has_day_ = true;
    day_ = day_index;
    return false;
  }
  if (day_index == day_) return false;
  day_ = day_index;
  e_ = EnergyCounters{};
  return true;
}

void EnergyIntegrator::restore(int32_t day_index, const EnergyCounters& c) {
  e_ = c;
  day_ = day_index;
  has_day_ = true;
}

double LoadManager::normalize_duty(const LoadParams& load, bool on, double duty) {
  if (!on) return 0.0;
  if (!load.dimmable) return 1.0;
  if (duty < load.min_duty) return load.min_duty;
  return duty > 1.0 ? 1.0 : duty;
}

uint32_t LoadManager::apply(const Decision& d, bool allowed, double t_s) {
  uint32_t changed = 0;
  for (int i = 0; i < loads_.count && i < kMaxLoads; ++i) {
    const LoadCommand& c = d.commands[i];
    ems_on_[i] = c.on;
    const bool on = c.on && allowed;
    const double duty = normalize_duty(loads_.items[i], on, c.duty);
    LoadOutput& o = out_[i];
    if (on != o.on) {
      o.last_switch_t_s = t_s;
      ++n_switch_[i];
      changed |= (1u << i);
    } else if (std::fabs(duty - o.duty) > 1e-9) {
      changed |= (1u << i);
    }
    o.on = on;
    o.duty = duty;
  }
  return changed;
}

}  // namespace sem
