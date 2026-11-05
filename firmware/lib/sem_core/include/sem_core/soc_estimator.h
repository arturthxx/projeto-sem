// =============================================================================
// sem_core/soc_estimator.h — estimador de SOC por contagem de Coulomb com
// sincronizações de cheio/vazio (docs/06 §1). Espelho EXATO de
// backend/smartenergy/estimation/soc.py (verificado por vetores golden).
//
// Convenção: corrente I POSITIVA = DESCARGA.
//
// Detalhes de implementação fixados aqui (o texto normativo é omisso):
//   * Amostra com sensor inválido: nenhuma integração e nenhuma alteração nos
//     temporizadores de sincronização (t_full/t_empty ficam congelados); apenas
//     o tempo de invalidez é acumulado (t_inv += dt). Se t_inv > sensor_timeout_s
//     => valid = false.
//   * Na primeira amostra válida após valid == false: valid = true,
//     unc_base += invalid_unc_penalty (0,05) e a amostra é processada normalmente.
//   * sync_manual(): além do especificado (soc, unc_base, last_sync), zera
//     ah_since_sync (a incerteza de deriva conta desde a última sincronização).
//   * Eventos SOC_SYNC_FULL / SOC_SYNC_EMPTY apenas na borda (entrada na condição).
// =============================================================================
#pragma once

#include "sem_core/events.h"
#include "sem_core/params.h"
#include "sem_core/types.h"

namespace sem {

/// OCV⁻¹(V) por interpolação linear da curva (semântica de numpy.interp:
/// satura nos extremos). Curva deve ter V crescente.
double ocv_to_soc(const SocParams& p, double v);

class SocEstimator {
 public:
  explicit SocEstimator(const SocParams& params) : p_(params) {}

  /// Inicialização a partir de valor persistido (NVS): last_sync = persisted.
  void init_persisted(double soc, double unc);

  /// Inicialização de baixa confiança pela curva OCV: last_sync = ocv_init.
  void init_from_voltage(double v);

  /// Atualização a cada amostra. 'sensor_ok' = sensor da bateria válido.
  void update(double dt_s, double i_a, double v_v, bool sensor_ok, EventList* events = nullptr);

  /// Comando sync_soc: soc = valor, unc_base = sync_uncertainty, last_sync = manual.
  void sync_manual(double soc);

  bool initialized() const { return last_sync_ != SyncKind::None; }
  double soc() const { return soc_; }
  /// unc = min(unc_max, unc_base + drift·ah_since_sync)
  double unc() const;
  double unc_base() const { return unc_base_; }
  bool valid() const { return valid_; }
  SyncKind last_sync() const { return last_sync_; }
  double since_sync_s() const { return since_sync_s_; }
  double ah_since_sync() const { return ah_since_sync_; }
  double t_full_s() const { return t_full_; }
  double t_empty_s() const { return t_empty_; }

 private:
  void reset_common();

  SocParams p_;
  double soc_ = 0.0;
  double unc_base_ = 0.0;
  double ah_since_sync_ = 0.0;
  double since_sync_s_ = 0.0;
  double t_full_ = 0.0;
  double t_empty_ = 0.0;
  double t_invalid_ = 0.0;
  bool valid_ = false;
  bool full_edge_ = false;   // condição de cheio já sincronizada (borda do evento)
  bool empty_edge_ = false;  // idem, vazio
  SyncKind last_sync_ = SyncKind::None;
};

/// Regra de persistência do SOC em flash (docs/06 §1): grava a cada
/// persist_every_s OU quando |soc − soc_gravado| ≥ persist_delta_soc.
class SocPersistPolicy {
 public:
  explicit SocPersistPolicy(const SocParams& p) : every_s_(p.persist_every_s), delta_(p.persist_delta_soc) {}

  /// Deve gravar agora? (t_s monotônico)
  bool should_persist(double t_s, double soc) const;
  /// Registra que (t_s, soc) foi gravado.
  void mark_persisted(double t_s, double soc);
  bool has_persisted() const { return has_; }

 private:
  double every_s_;
  double delta_;
  double last_t_ = 0.0;
  double last_soc_ = 0.0;
  bool has_ = false;
};

}  // namespace sem
