// =============================================================================
// sem_core/ems_controller.h — orquestra o modo do EMS (convencional, regras,
// preditivo com salvaguardas/TTL) e produz a Decision de cada avaliação (1 s).
//
// No modo preditivo, as regras são SEMPRE avaliadas (mantêm estado e latch
// aquecidos): com plano válido, o plano (com salvaguardas §3.4) substitui os
// comandos e o 'state' informativo é o das regras; sem plano ou com TTL
// expirado -> decisão das regras com fallback = true (evento PLAN_EXPIRED na
// expiração).
// =============================================================================
#pragma once

#include "sem_core/ems_types.h"
#include "sem_core/events.h"
#include "sem_core/params.h"
#include "sem_core/rules_engine.h"
#include "sem_core/strategies.h"

namespace sem {

class EmsController {
 public:
  /// 'params' deve sobreviver ao controlador (referências internas).
  explicit EmsController(const SystemParams& params);

  void reset();

  EmsMode mode() const { return mode_; }

  /// Troca de modo (evento MODE_CHANGE se mudou). Sair do preditivo descarta o plano.
  void set_mode(EmsMode m, EventList* events = nullptr);

  /// Plano válido do MPC (set_loads aceito). issued/expires em tempo monotônico.
  void accept_plan(const LoadPlan& plan, double issued_t_s, double expires_t_s);

  /// Avalia o EMS no instante de 'in'.
  Decision evaluate(const EmsInputs& in, EventList* events = nullptr);

  bool has_plan() const { return guard_.has_plan(); }
  /// Idade do comando vigente (s) ou valor negativo se não houver plano.
  double cmd_age_s(double t_s) const { return guard_.has_plan() ? guard_.cmd_age_s(t_s) : -1.0; }
  const RulesEngine& rules() const { return rules_; }

 private:
  const SystemParams& p_;
  RulesEngine rules_;
  ConventionalStrategy conv_;
  PredictiveGuard guard_;
  EmsMode mode_;
  bool has_last_ = false;
  EmsState last_state_ = EmsState::Normal;
};

}  // namespace sem
