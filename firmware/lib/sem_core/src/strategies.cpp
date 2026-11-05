// Perfil de demanda, operação convencional e salvaguardas do modo preditivo.
#include "sem_core/strategies.h"

#include "sem_core/time_util.h"

namespace sem {

// ---------------------------------------------------------------------------
// DemandProfile
// ---------------------------------------------------------------------------
bool DemandProfile::demand(int idx, double tod_s) const {
  if (idx < 0 || idx >= loads_.count || idx >= kMaxLoads) return false;
  return in_any_window(tod_s, demand_.schedule[idx]);
}

void DemandProfile::fill(double tod_s, bool out[kMaxLoads]) const {
  for (int i = 0; i < kMaxLoads; ++i) out[i] = demand(i, tod_s);
}

bool DemandProfile::deferrable_pending(int idx, double tod_s, double e_done_wh) const {
  if (idx < 0 || idx >= loads_.count || idx >= kMaxLoads) return false;
  const DeferrableParams& D = loads_.items[idx].deferrable;
  if (!D.enabled) return false;
  return e_done_wh < D.daily_energy_wh && in_window(tod_s, D.window_start_s, D.deadline_s);
}

// ---------------------------------------------------------------------------
// ConventionalStrategy (docs/06 §3.1)
// ---------------------------------------------------------------------------
Decision ConventionalStrategy::decide(const EmsInputs& in) const {
  Decision d{};
  d.mode = EmsMode::Conventional;
  d.state = EmsState::Normal;
  d.n_loads = loads_.count;
  append_reason(d.reason, sizeof(d.reason), "convencional: cargas seguem o perfil");
  for (int i = 0; i < loads_.count && i < kMaxLoads; ++i) {
    const LoadParams& L = loads_.items[i];
    LoadCommand c{};
    if (L.deferrable.enabled) {
      c.on = in_any_window(in.tod_s, conv_.deferrable_schedule[i]) && in.e_done_wh[i] < L.deferrable.daily_energy_wh;
    } else {
      c.on = in.demand[i];
    }
    c.duty = c.on ? 1.0 : 0.0;
    d.commands[i] = c;
  }
  return d;
}

// ---------------------------------------------------------------------------
// PredictiveGuard (docs/06 §3.4)
// ---------------------------------------------------------------------------
double plan_expiry_t_s(int64_t cmd_ts_ms, int32_t ttl_s, bool time_synced, int64_t now_ms, double now_t_s) {
  if (time_synced && cmd_ts_ms > 0) {
    const double remaining_s = static_cast<double>(cmd_ts_ms + static_cast<int64_t>(ttl_s) * 1000 - now_ms) / 1000.0;
    return now_t_s + remaining_s;
  }
  return now_t_s + static_cast<double>(ttl_s);
}

void PredictiveGuard::set_plan(const LoadPlan& plan, double issued_t_s, double expires_t_s) {
  plan_ = plan;
  issued_t_s_ = issued_t_s;
  expires_t_s_ = expires_t_s;
  has_plan_ = true;
}

void PredictiveGuard::apply(const EmsInputs& in, Decision* out) const {
  if (out == nullptr) return;
  const bool soc_low = !in.soc_valid || in.soc < r_.critical_min_soc;
  append_reason(out->reason, sizeof(out->reason), "mpc: plano vigente");
  bool blocked_demand = false;
  for (int i = 0; i < loads_.count && i < kMaxLoads; ++i) {
    const LoadParams& L = loads_.items[i];
    LoadCommand c{};
    if (plan_.present[i]) c = plan_.cmd[i];
    bool on = c.on;
    if (!L.deferrable.enabled) {
      if (on && !in.demand[i]) blocked_demand = true;
      on = on && in.demand[i];  // nunca liga sem pedido
    }
    if (soc_low && (L.priority == Priority::Medium || L.priority == Priority::Low)) on = false;
    double duty = 0.0;
    if (on) {
      if (L.dimmable) {
        duty = c.duty < L.min_duty ? L.min_duty : (c.duty > 1.0 ? 1.0 : c.duty);
      } else {
        duty = 1.0;
      }
    }
    out->commands[i].on = on;
    out->commands[i].duty = duty;
  }
  if (blocked_demand) append_reason(out->reason, sizeof(out->reason), "salvaguarda: on = cmd AND demanda");
  if (soc_low) {
    append_reason(out->reason, sizeof(out->reason), "salvaguarda: soc %.2f < %.2f -> medium/low off", in.soc,
                  r_.critical_min_soc);
  }
}

}  // namespace sem
