// Estimador de SOC por contagem de Coulomb com sincronizações (docs/06 §1).
#include "sem_core/soc_estimator.h"

#include <cmath>

namespace sem {

double ocv_to_soc(const SocParams& p, double v) {
  const int n = p.ocv_count;
  if (n <= 0) return 0.5;  // sem curva: valor neutro (incerteza inicial já é alta)
  if (n == 1 || v <= p.ocv_v[0]) return p.ocv_soc[0];
  if (v >= p.ocv_v[n - 1]) return p.ocv_soc[n - 1];
  // Intervalo j com xp[j] <= x < xp[j+1] (igual a ocv_inverse do Python).
  for (int j = 0; j < n - 1; ++j) {
    const double x0 = p.ocv_v[j];
    const double x1 = p.ocv_v[j + 1];
    if (v >= x0 && v < x1) {
      const double s0 = p.ocv_soc[j];
      const double s1 = p.ocv_soc[j + 1];
      return s0 + (s1 - s0) * (v - x0) / (x1 - x0);  // mesma ordem de operações do Python
    }
  }
  return p.ocv_soc[n - 1];
}

void SocEstimator::reset_common() {
  valid_ = true;
  ah_since_sync_ = 0.0;
  since_sync_s_ = 0.0;
  t_full_ = 0.0;
  t_empty_ = 0.0;
  t_invalid_ = 0.0;
  full_edge_ = false;
  empty_edge_ = false;
}

void SocEstimator::init_persisted(double soc, double unc) {
  soc_ = clamp(soc, 0.0, 1.0);
  unc_base_ = unc < 0.0 ? 0.0 : unc;
  last_sync_ = SyncKind::Persisted;
  reset_common();
}

void SocEstimator::init_from_voltage(double v) {
  soc_ = clamp(ocv_to_soc(p_, v), 0.0, 1.0);
  unc_base_ = p_.unknown_initial_uncertainty;
  last_sync_ = SyncKind::OcvInit;
  reset_common();
}

void SocEstimator::sync_manual(double soc) {
  soc_ = clamp(soc, 0.0, 1.0);
  unc_base_ = p_.sync_uncertainty;
  ah_since_sync_ = 0.0;
  since_sync_s_ = 0.0;
  last_sync_ = SyncKind::Manual;
}

double SocEstimator::unc() const {
  const double u = unc_base_ + p_.drift_uncertainty_per_ah * ah_since_sync_;
  return u < p_.unc_max ? u : p_.unc_max;
}

void SocEstimator::update(double dt_s, double i_a, double v_v, bool sensor_ok, EventList* events) {
  if (dt_s < 0.0) dt_s = 0.0;

  // --- Sensor inválido: integração suspensa; temporizadores de sincronização zerados.
  if (!sensor_ok) {
    t_invalid_ += dt_s;
    t_full_ = 0.0;
    t_empty_ = 0.0;
    full_edge_ = false;
    empty_edge_ = false;
    if (valid_ && t_invalid_ > p_.sensor_timeout_s) valid_ = false;
    return;
  }
  if (!valid_) {
    valid_ = true;
    const double u = unc_base_ + p_.invalid_unc_penalty;
    unc_base_ = u < p_.unc_max ? u : p_.unc_max;
  }
  t_invalid_ = 0.0;

  // --- Contagem de Coulomb com zona morta (I > 0 = descarga).
  const double i_eff = std::fabs(i_a) < p_.current_deadband_a ? 0.0 : i_a;
  if (i_eff > 0.0) {
    soc_ -= i_eff * dt_s / (3600.0 * p_.capacity_ah);
  } else {
    soc_ += p_.coulombic_efficiency * (-i_eff) * dt_s / (3600.0 * p_.capacity_ah);
  }
  ah_since_sync_ += std::fabs(i_eff) * dt_s / 3600.0;
  since_sync_s_ += dt_s;
  soc_ = clamp(soc_, 0.0, 1.0);

  // --- Sincronização de cheio (absorção: V alta e corrente baixa).
  if (v_v >= p_.full_voltage_v && std::fabs(i_a) <= p_.full_tail_current_a) {
    t_full_ += dt_s;
  } else {
    t_full_ = 0.0;
  }
  if (t_full_ >= p_.full_hold_s) {
    soc_ = 1.0;
    unc_base_ = p_.sync_uncertainty;
    ah_since_sync_ = 0.0;
    since_sync_s_ = 0.0;
    last_sync_ = SyncKind::Full;
    if (!full_edge_) {
      full_edge_ = true;
      if (events != nullptr) {
        events->push(EventCode::SocSyncFull, EventLevel::Info, "Sincronizacao de cheio: v=%.2f V, i=%.3f A", v_v, i_a)
            .with_number("v_bat", v_v);
      }
    }
  } else {
    full_edge_ = false;
  }

  // --- Sincronização de vazio (tensão compensada pela queda resistiva).
  const double vc = v_v + i_a * p_.r_internal_ohm;
  if (vc <= p_.empty_voltage_v) {
    t_empty_ += dt_s;
  } else {
    t_empty_ = 0.0;
  }
  if (t_empty_ >= p_.empty_hold_s) {
    soc_ = soc_ < p_.empty_soc_value ? soc_ : p_.empty_soc_value;
    unc_base_ = p_.sync_uncertainty;
    ah_since_sync_ = 0.0;
    since_sync_s_ = 0.0;
    last_sync_ = SyncKind::Empty;
    if (!empty_edge_) {
      empty_edge_ = true;
      if (events != nullptr) {
        events->push(EventCode::SocSyncEmpty, EventLevel::Warn, "SOC limitado a %.2f (Vc=%.2f V)", p_.empty_soc_value, vc)
            .with_number("v_comp", vc);
      }
    }
  } else {
    empty_edge_ = false;
  }
}

bool SocPersistPolicy::should_persist(double t_s, double soc) const {
  if (!has_) return true;
  return (t_s - last_t_ >= every_s_) || (std::fabs(soc - last_soc_) >= delta_);
}

void SocPersistPolicy::mark_persisted(double t_s, double soc) {
  last_t_ = t_s;
  last_soc_ = soc;
  has_ = true;
}

}  // namespace sem
