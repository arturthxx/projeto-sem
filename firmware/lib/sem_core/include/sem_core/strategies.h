// =============================================================================
// sem_core/strategies.h — operação convencional (docs/06 §3.1), perfil de
// demanda programado e salvaguardas do modo preditivo (docs/06 §3.4).
// =============================================================================
#pragma once

#include "sem_core/ems_types.h"
#include "sem_core/params.h"

namespace sem {

/// Perfil de demanda programado (demand.schedule): janelas [início, fim) em hora
/// local, com cruzamento de meia-noite quando fim < início.
class DemandProfile {
 public:
  DemandProfile(const LoadSetParams& loads, const DemandParams& demand) : loads_(loads), demand_(demand) {}

  /// Pedido da carga 'idx' no instante 'tod_s' (cargas sem agenda => false).
  bool demand(int idx, double tod_s) const;

  /// Preenche out[0..count-1] com o pedido de todas as cargas.
  void fill(double tod_s, bool out[kMaxLoads]) const;

  /// Pedido "informativo" de uma carga deslocável (telemetria, docs/05 §3):
  /// true enquanto houver energia pendente dentro da janela.
  bool deferrable_pending(int idx, double tod_s, double e_done_wh) const;

 private:
  const LoadSetParams& loads_;
  const DemandParams& demand_;
};

/// Operação convencional (referência): cargas seguem o pedido; a deslocável
/// liga na agenda convencional até cumprir a energia diária. state = NORMAL.
class ConventionalStrategy {
 public:
  ConventionalStrategy(const LoadSetParams& loads, const ConventionalParams& conv) : loads_(loads), conv_(conv) {}
  Decision decide(const EmsInputs& in) const;

 private:
  const LoadSetParams& loads_;
  const ConventionalParams& conv_;
};

/// Plano recebido do MPC (comando set_loads), indexado pelo índice da carga.
struct LoadPlan {
  bool present[kMaxLoads] = {};
  LoadCommand cmd[kMaxLoads] = {};
};

/// Instante (tempo monotônico) em que um comando expira (docs/05 §7):
/// com hora sincronizada vale até ts_ms + ttl_s; sem hora, ttl_s a partir do recebimento.
double plan_expiry_t_s(int64_t cmd_ts_ms, int32_t ttl_s, bool time_synced, int64_t now_ms, double now_t_s);

/// Salvaguardas do modo preditivo (docs/06 §3.4). Mantém o plano vigente e seu TTL.
///
/// Decisões de implementação: carga ausente do último set_loads => desligada;
/// soc inválido é tratado como soc < critical_min_soc (conservador); a carga
/// deslocável segue o plano (o MPC já respeita a janela) salvo a salvaguarda de SOC.
class PredictiveGuard {
 public:
  PredictiveGuard(const RulesParams& rules, const LoadSetParams& loads) : r_(rules), loads_(loads) {}

  void set_plan(const LoadPlan& plan, double issued_t_s, double expires_t_s);
  void clear() { has_plan_ = false; }
  bool has_plan() const { return has_plan_; }
  /// Comando expirado (TTL) — válido enquanto t_s <= expires.
  bool expired(double t_s) const { return has_plan_ && t_s > expires_t_s_; }
  /// Idade do comando em segundos (desde a emissão, se a hora era conhecida; senão desde o recebimento).
  double cmd_age_s(double t_s) const { return t_s - issued_t_s_ > 0.0 ? t_s - issued_t_s_ : 0.0; }
  const LoadPlan& plan() const { return plan_; }

  /// Aplica o plano com as salvaguardas a 'out' (comandos e reason). Pré-condição: plano válido.
  void apply(const EmsInputs& in, Decision* out) const;

 private:
  const RulesParams& r_;
  const LoadSetParams& loads_;
  LoadPlan plan_{};
  bool has_plan_ = false;
  double issued_t_s_ = 0.0;
  double expires_t_s_ = 0.0;
};

}  // namespace sem
