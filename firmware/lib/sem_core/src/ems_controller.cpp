// Orquestração dos modos do EMS (convencional / regras / preditivo).
#include "sem_core/ems_controller.h"

namespace sem {

EmsController::EmsController(const SystemParams& params)
    : p_(params),
      rules_(params.rules, params.loads),
      conv_(params.loads, params.conventional),
      guard_(params.rules, params.loads),
      mode_(params.default_mode) {}

void EmsController::reset() {
  rules_.reset();
  guard_.clear();
  mode_ = p_.default_mode;
  has_last_ = false;
}

void EmsController::set_mode(EmsMode m, EventList* ev) {
  if (m == mode_) return;
  const EmsMode old = mode_;
  mode_ = m;
  if (m != EmsMode::Predictive) guard_.clear();
  if (ev) {
    ev->push(EventCode::ModeChange, EventLevel::Info, "Modo do EMS: %s -> %s", to_string(old), to_string(m))
        .with_text("mode", to_string(m));
  }
}

void EmsController::accept_plan(const LoadPlan& plan, double issued_t_s, double expires_t_s) {
  guard_.set_plan(plan, issued_t_s, expires_t_s);
}

Decision EmsController::evaluate(const EmsInputs& in, EventList* ev) {
  Decision d{};
  switch (mode_) {
    case EmsMode::Conventional:
      d = conv_.decide(in);
      break;
    case EmsMode::Rules:
      d = rules_.decide(in);
      break;
    case EmsMode::Predictive: {
      d = rules_.decide(in);
      d.mode = EmsMode::Predictive;
      if (guard_.has_plan() && guard_.expired(in.t_s)) {
        guard_.clear();
        if (ev) ev->push(EventCode::PlanExpired, EventLevel::Warn, "Plano do MPC expirado (TTL): fallback para regras");
      }
      if (guard_.has_plan()) {
        d.fallback = false;
        d.reason[0] = '\0';
        append_reason(d.reason, sizeof(d.reason), "estado %s (soc %.2f)", to_string(d.state), in.soc);
        guard_.apply(in, &d);
      } else {
        d.fallback = true;
        char rules_reason[kReasonLen];
        copy_cstr(rules_reason, d.reason);
        d.reason[0] = '\0';
        append_reason(d.reason, sizeof(d.reason), "fallback regras: %s", rules_reason);
      }
      break;
    }
  }
  d.has_prev_state = has_last_;
  d.prev_state = last_state_;
  has_last_ = true;
  last_state_ = d.state;
  return d;
}

}  // namespace sem
