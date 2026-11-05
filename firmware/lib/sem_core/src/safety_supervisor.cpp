// Supervisor de segurança local (docs/06 §2). Ver decisões de implementação no .h.
#include "sem_core/safety_supervisor.h"

namespace sem {

SafetySupervisor::SafetySupervisor(const SafetyParams& params, bool boot_hold) : p_(params), boot_hold_(boot_hold) {
  reset();
}

void SafetySupervisor::reset() {
  boot_ = boot_hold_;
  boot_count_ = 0;
  lvd_ = false;
  t_lvd_ = 0.0;
  ovp_ = false;
  t_ovp_ = 0.0;
  t_ovp_clear_ = 0.0;
  oc_ = false;
  oc_lockout_ = false;
  oc_trips_ = 0;
  t_oc_ = 0.0;
  t_oc_wait_ = 0.0;
  t_oc_free_ = 0.0;
  pending_reset_event_ = false;
  ot_ = false;
  sensor_ = false;
  t_sensor_ = 0.0;
  status_.code = highest();
  status_.allowed = status_.code == SafetyCode::Ok;
}

bool SafetySupervisor::active(SafetyCode c) const {
  switch (c) {
    case SafetyCode::Ok:
      return highest() == SafetyCode::Ok;
    case SafetyCode::Boot:
      return boot_;
    case SafetyCode::Lvd:
      return lvd_;
    case SafetyCode::Ovp:
      return ovp_;
    case SafetyCode::Oc:
      return oc_;
    case SafetyCode::OcLockout:
      return oc_lockout_;
    case SafetyCode::Ot:
      return ot_;
    case SafetyCode::Sensor:
      return sensor_;
  }
  return false;
}

SafetyCode SafetySupervisor::highest() const {
  if (oc_lockout_) return SafetyCode::OcLockout;
  if (oc_) return SafetyCode::Oc;
  if (ovp_) return SafetyCode::Ovp;
  if (ot_) return SafetyCode::Ot;
  if (sensor_) return SafetyCode::Sensor;
  if (lvd_) return SafetyCode::Lvd;
  if (boot_) return SafetyCode::Boot;
  return SafetyCode::Ok;
}

SafetyStatus SafetySupervisor::update(const SafetyInputs& in, EventList* ev) {
  const double dt = in.dt_s > 0.0 ? in.dt_s : 0.0;
  const bool v_ok = in.v_bat_ok || in.v_bus_ok;
  const double v_eff = in.v_bat_ok ? in.v_bat : in.v_bus;

  if (pending_reset_event_) {
    pending_reset_event_ = false;
    if (ev) ev->push(EventCode::FaultsReset, EventLevel::Info, "Falhas rearmadas manualmente");
  }

  // --- SENSOR: bat e bus inválidos por mais de sensor_timeout_s.
  if (!in.v_bat_ok && !in.v_bus_ok) {
    t_sensor_ += dt;
    if (!sensor_ && t_sensor_ > p_.sensor_timeout_s) {
      sensor_ = true;
      if (ev) ev->push(EventCode::SensorFail, EventLevel::Error, "Sensores de tensao bat e bus invalidos");
    }
  } else {
    t_sensor_ = 0.0;
    if (sensor_) {
      sensor_ = false;
      if (ev) ev->push(EventCode::SensorOk, EventLevel::Info, "Sensor de tensao restabelecido");
    }
  }

  // --- BOOT: aguarda boot_settle_samples amostras válidas.
  if (boot_) {
    if (v_ok) ++boot_count_;
    if (boot_count_ >= p_.boot_settle_samples) boot_ = false;
  }

  // --- LVD: subtensão sustentada OU SOC estimado abaixo do mínimo absoluto.
  if (!lvd_) {
    if (v_ok) t_lvd_ = v_eff < p_.lvd_v ? t_lvd_ + dt : 0.0;
    const bool by_v = t_lvd_ >= p_.lvd_delay_s;
    const bool by_soc = in.soc_valid && in.soc < p_.soc_hard_min;
    if (by_v || by_soc) {
      lvd_ = true;
      t_lvd_ = 0.0;
      if (ev) {
        if (by_v) {
          ev->push(EventCode::LvdTrip, EventLevel::Warn, "Desconexao por subtensao: v_bat=%.2f V", v_eff)
              .with_number("v_bat", v_eff);
        } else {
          ev->push(EventCode::LvdTrip, EventLevel::Warn, "Desconexao por SOC: soc=%.3f < %.2f", in.soc, p_.soc_hard_min)
              .with_number("soc", in.soc);
        }
      }
    }
  } else if (v_ok && v_eff >= p_.reconnect_v && (!in.soc_valid || in.soc >= p_.reconnect_min_soc)) {
    lvd_ = false;
    t_lvd_ = 0.0;
    if (ev) ev->push(EventCode::LvdClear, EventLevel::Info, "Religamento: v_bat=%.2f V", v_eff).with_number("v_bat", v_eff);
  }

  // --- OVP no barramento (atraso no disparo e na liberação, histerese de 0,3 V).
  if (!ovp_) {
    t_ovp_ = (in.v_bus_ok && in.v_bus > p_.bus_ovp_v) ? t_ovp_ + dt : 0.0;
    if (t_ovp_ >= p_.bus_ovp_delay_s) {
      ovp_ = true;
      t_ovp_ = 0.0;
      t_ovp_clear_ = 0.0;
      if (ev) {
        ev->push(EventCode::OvpTrip, EventLevel::Error, "Sobretensao no barramento: v_bus=%.2f V", in.v_bus)
            .with_number("v_bus", in.v_bus);
      }
    }
  } else {
    const bool release = in.v_bus_ok && in.v_bus < p_.bus_ovp_v - p_.bus_ovp_hysteresis_v;
    t_ovp_clear_ = release ? t_ovp_clear_ + dt : 0.0;
    if (t_ovp_clear_ >= p_.bus_ovp_delay_s) {
      ovp_ = false;
      t_ovp_clear_ = 0.0;
      if (ev) ev->push(EventCode::OvpClear, EventLevel::Info, "Sobretensao no barramento eliminada");
    }
  }

  // --- OC no barramento, com nova tentativa e bloqueio.
  if (!oc_lockout_) {
    if (oc_) {
      t_oc_wait_ += dt;
      if (t_oc_wait_ >= p_.overcurrent_retry_s) {
        oc_ = false;
        t_oc_wait_ = 0.0;
        t_oc_ = 0.0;
        if (ev) ev->push(EventCode::OcRetry, EventLevel::Info, "Nova tentativa apos sobrecorrente (%d)", oc_trips_);
      }
    } else {
      const bool over = (in.i_bus_ok && in.i_bus > p_.bus_overcurrent_a) || in.oc_alert;
      t_oc_ = over ? t_oc_ + dt : 0.0;
      if (t_oc_ >= p_.bus_overcurrent_delay_ms / 1000.0) {
        ++oc_trips_;
        t_oc_ = 0.0;
        t_oc_wait_ = 0.0;
        t_oc_free_ = 0.0;
        const double i_val = in.i_bus_ok ? in.i_bus : 0.0;
        if (ev) {
          ev->push(EventCode::OcTrip, EventLevel::Error, "Sobrecorrente no barramento: i_bus=%.2f A%s", i_val,
                   in.oc_alert ? " (ALERT)" : "")
              .with_number("i_bus", i_val);
        }
        if (oc_trips_ >= p_.overcurrent_max_retries) {
          oc_lockout_ = true;
          if (ev) {
            ev->push(EventCode::OcLockout, EventLevel::Critical, "%d disparos sem 10 min livres: bloqueio", oc_trips_);
          }
        } else {
          oc_ = true;
        }
      } else {
        t_oc_free_ += dt;
        if (oc_trips_ > 0 && t_oc_free_ >= p_.overcurrent_free_reset_s) oc_trips_ = 0;
      }
    }
  }

  // --- OT da bateria (com histerese, sem atraso).
  if (in.t_bat_ok) {
    if (!ot_ && in.t_bat > p_.battery_overtemp_c) {
      ot_ = true;
      if (ev) {
        ev->push(EventCode::OtTrip, EventLevel::Error, "Sobretemperatura da bateria: %.1f degC", in.t_bat)
            .with_number("t_bat", in.t_bat);
      }
    } else if (ot_ && in.t_bat < p_.battery_overtemp_release_c) {
      ot_ = false;
      if (ev) ev->push(EventCode::OtClear, EventLevel::Info, "Temperatura da bateria normalizada: %.1f degC", in.t_bat);
    }
  }

  status_.code = highest();
  status_.allowed = status_.code == SafetyCode::Ok;
  return status_;
}

void SafetySupervisor::reset_faults() {
  oc_ = false;
  oc_lockout_ = false;
  oc_trips_ = 0;
  t_oc_ = 0.0;
  t_oc_wait_ = 0.0;
  t_oc_free_ = 0.0;
  pending_reset_event_ = true;
  status_.code = highest();
  status_.allowed = status_.code == SafetyCode::Ok;
}

}  // namespace sem
