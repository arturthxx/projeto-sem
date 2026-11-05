// =============================================================================
// sem_core/energy.h — integração de energia (docs/06 §4) e gerência lógica das
// saídas de carga (on_final = on_EMS AND allowed; contagem de comutações).
// =============================================================================
#pragma once

#include <cstdint>

#include "sem_core/ems_types.h"
#include "sem_core/params.h"

namespace sem {

// ---------------------------------------------------------------------------
// Potência das cargas
// ---------------------------------------------------------------------------

/// Potência estimada de uma carga: p_nom·(V/v_ref)^k·duty (p_src = "estimated").
/// Retorna 0 se duty <= 0 ou V <= 0.
double estimate_load_power_w(const LoadParams& load, double v, double duty);

/// Potência demandada e não atendida (cargas NÃO deslocáveis):
/// Σ max(0, d_k·P_k(V) − p_k), com P_k(V) a potência nominal corrigida pela tensão.
double unserved_power_w(const LoadSetParams& loads, const bool demand[kMaxLoads], const LoadOutput out[kMaxLoads],
                        double v);

// ---------------------------------------------------------------------------
// EnergyIntegrator (trapezoidal; contadores do dia local)
// ---------------------------------------------------------------------------

/// Potências de uma amostra (W). p_bat: + descarga.
struct PowerSample {
  double p_pv = 0.0;
  double p_bat = 0.0;
  double p_bus = 0.0;
  double p_unserved = 0.0;
  double p_load[kMaxLoads] = {};
};

/// Energias acumuladas (Wh, sempre >= 0).
struct EnergyCounters {
  double pv_in_wh = 0.0;
  double bat_chg_wh = 0.0;
  double bat_dis_wh = 0.0;
  double load_wh = 0.0;
  double unserved_wh = 0.0;
  double load_wh_by[kMaxLoads] = {};  // por carga (e_done_wh da deslocável)
};

/// E += (P_k + P_{k−1})/2·dt/3600. bat_chg/bat_dis separados pelo sinal de cada
/// amostra (trapézio das partes positiva e negativa). Zera à meia-noite local.
///
/// Sem hora sincronizada, os contadores valem "desde o boot" (day = null). Na
/// primeira data conhecida o dia é adotado SEM zerar (decisão de implementação).
class EnergyIntegrator {
 public:
  /// Acrescenta uma amostra. A primeira amostra (ou após break_continuity) só é memorizada.
  void add(double dt_s, const PowerSample& s);

  /// Informa o dia local atual (dias desde 1970-01-01). Retorna true se zerou os contadores.
  bool roll_day(int32_t day_index);

  /// Descontinuidade (ex.: sensores inválidos): a próxima amostra não forma trapézio.
  void break_continuity() { has_prev_ = false; }

  /// Restaura contadores persistidos (mesmo dia).
  void restore(int32_t day_index, const EnergyCounters& c);

  const EnergyCounters& today() const { return e_; }
  bool has_day() const { return has_day_; }
  int32_t day_index() const { return day_; }

 private:
  EnergyCounters e_{};
  PowerSample prev_{};
  bool has_prev_ = false;
  bool has_day_ = false;
  int32_t day_ = 0;
};

// ---------------------------------------------------------------------------
// LoadManager (lógico; o driver físico fica em src/hw/outputs)
// ---------------------------------------------------------------------------
class LoadManager {
 public:
  explicit LoadManager(const LoadSetParams& loads) : loads_(loads) {}

  /// Aplica on_final = on_EMS AND allowed. Atualiza last_switch_t_s nas
  /// transições on<->off e conta comutações. Retorna máscara (bit i) das cargas
  /// cuja saída física (on ou duty) mudou.
  uint32_t apply(const Decision& d, bool allowed, double t_s);

  const LoadOutput& output(int idx) const { return out_[idx]; }
  const LoadOutput* outputs() const { return out_; }
  bool ems_on(int idx) const { return ems_on_[idx]; }
  uint32_t switch_count(int idx) const { return n_switch_[idx]; }
  void set_switch_count(int idx, uint32_t n) {
    if (idx >= 0 && idx < kMaxLoads) n_switch_[idx] = n;
  }

  /// Duty físico normalizado: off => 0; não dimerizável => 1; dimerizável => [min_duty, 1].
  static double normalize_duty(const LoadParams& load, bool on, double duty);

 private:
  const LoadSetParams& loads_;
  LoadOutput out_[kMaxLoads] = {};
  bool ems_on_[kMaxLoads] = {};
  uint32_t n_switch_[kMaxLoads] = {};
};

}  // namespace sem
