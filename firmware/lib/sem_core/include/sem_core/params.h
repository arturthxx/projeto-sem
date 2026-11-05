// =============================================================================
// sem_core/params.h — estruturas de parâmetros (espelho de config/system.yaml).
//
// Todos os módulos recebem parâmetros por estas structs (nada de globais),
// para que:
//   * o firmware use os valores gerados em include/system_params.h
//     (tools/gen_firmware_params.py, a partir de config/system.yaml);
//   * os testes nativos construam os mesmos parâmetros a partir do JSON de
//     vetores golden (sem.vectors.v1), gerado pelo backend Python.
//
// Convenções: unidades SI no nome do campo; horários em segundos desde a
// meia-noite LOCAL; SOC em fração; corrente da bateria positiva = descarga.
// =============================================================================
#pragma once

#include <cstdint>
#include <cstring>

#include "sem_core/types.h"

namespace sem {

/// Intervalo [start_s, end_s) em segundos desde a meia-noite local
/// (end_s < start_s => cruza a meia-noite; start_s == end_s => vazio).
struct TimeWindow {
  int32_t start_s = 0;
  int32_t end_s = 0;
};

/// Lista de janelas de uma carga.
struct Schedule {
  TimeWindow windows[kMaxWindows] = {};
  int count = 0;
};

/// Carga deslocável: energia diária a entregar dentro de [window_start, deadline).
struct DeferrableParams {
  bool enabled = false;
  double daily_energy_wh = 0.0;
  int32_t window_start_s = 0;
  int32_t deadline_s = 0;
};

/// Uma carga experimental (loads[] do system.yaml).
struct LoadParams {
  int id = 0;
  char key[kKeyLen] = {};
  Priority priority = Priority::Low;
  double p_nom_w = 0.0;           // potência nominal em v_ref_v (PENDENTE: medir no Experimento 2)
  double v_ref_v = 12.8;
  double voltage_exponent = 1.0;  // P(V) = p_nom·(V/v_ref)^k·duty
  bool dimmable = false;
  double min_duty = 0.0;
  double pwm_freq_hz = 0.0;
  double i_max_a = 0.0;
  double fuse_a = 0.0;
  int gpio = -1;
  DeferrableParams deferrable{};
};

/// Conjunto de cargas. O ÍNDICE (0..count-1) é usado internamente por todos os módulos.
struct LoadSetParams {
  LoadParams items[kMaxLoads] = {};
  int count = 0;

  /// Índice da carga com o id informado, ou -1.
  int index_of_id(int id) const {
    for (int i = 0; i < count; ++i)
      if (items[i].id == id) return i;
    return -1;
  }
  /// Índice da carga com a chave informada, ou -1.
  int index_of_key(const char* key) const {
    if (key == nullptr) return -1;
    for (int i = 0; i < count; ++i)
      if (std::strcmp(items[i].key, key) == 0) return i;
    return -1;
  }
};

/// Estimador de SOC (docs/06 §1; system.yaml: battery + soc_estimator + safety.sensor_timeout_s).
struct SocParams {
  double capacity_ah = 6.0;
  double coulombic_efficiency = 0.99;
  double r_internal_ohm = 0.04;
  double current_deadband_a = 0.005;
  double full_voltage_v = 14.0;
  double full_tail_current_a = 0.30;
  double full_hold_s = 120.0;
  double empty_voltage_v = 11.8;
  double empty_hold_s = 30.0;
  double empty_soc_value = 0.05;
  double unknown_initial_uncertainty = 0.30;
  double sync_uncertainty = 0.02;
  double drift_uncertainty_per_ah = 0.01;
  double sensor_timeout_s = 3.0;
  double invalid_unc_penalty = 0.05;  // docs/06 §1: unc_base += 0,05 quando o sensor volta
  double unc_max = 0.5;               // docs/06 §1: unc = min(0,5, ...)
  double persist_every_s = 600.0;
  double persist_delta_soc = 0.02;
  double ocv_soc[kMaxOcvPoints] = {};
  double ocv_v[kMaxOcvPoints] = {};
  int ocv_count = 0;
};

/// Supervisor de segurança (docs/06 §2; system.yaml: safety).
struct SafetyParams {
  double lvd_v = 11.6;
  double lvd_delay_s = 5.0;
  double soc_hard_min = 0.10;
  double reconnect_v = 12.9;
  double reconnect_min_soc = 0.25;
  double bus_ovp_v = 15.0;
  double bus_ovp_delay_s = 1.0;
  double bus_ovp_hysteresis_v = 0.3;  // docs/06 §2: liberação abaixo de bus_ovp_v − 0,3 V
  double bus_overcurrent_a = 4.0;
  double bus_overcurrent_delay_ms = 200.0;
  double overcurrent_retry_s = 30.0;
  int overcurrent_max_retries = 3;
  double overcurrent_free_reset_s = 600.0;  // docs/06 §2: "10 min livres" zeram a contagem
  double battery_overtemp_c = 55.0;
  double battery_overtemp_release_c = 45.0;
  double sensor_timeout_s = 3.0;
  int boot_settle_samples = 20;
};

/// Controle por regras (docs/06 §3.2; system.yaml: ems.rules).
struct RulesParams {
  double normal_min_soc = 0.70;
  double economy_min_soc = 0.40;
  double critical_min_soc = 0.20;
  double hysteresis_soc = 0.05;
  double economy_medium_duty = 0.60;
  double min_dwell_s = 300.0;
  double surplus_on_soc = 0.90;
  double surplus_off_soc = 0.80;
  double economy_surplus_margin_w = 2.0;
  double deadline_margin_s = 1800.0;
};

/// Perfil de demanda programado (demand.schedule), indexado pelo índice da carga.
struct DemandParams {
  Schedule schedule[kMaxLoads] = {};
};

/// Operação convencional (demand.conventional.<key>_schedule), indexado pelo índice da carga.
struct ConventionalParams {
  Schedule deferrable_schedule[kMaxLoads] = {};
};

/// Modo preditivo (parte relevante ao firmware: TTL e salvaguardas).
struct PredictiveParams {
  double step_s = 900.0;
  int horizon_steps = 96;
  double replan_s = 900.0;
  double command_ttl_s = 1200.0;
  double soc_min = 0.20;
  double soc_max = 1.00;
};

/// Comunicação (system.yaml: comm).
struct CommParams {
  char mqtt_prefix[24] = {};
  double telemetry_period_s = 5.0;
  int sample_period_ms = 100;
  int ems_period_ms = 1000;
  bool command_hmac = true;
  double command_max_skew_s = 120.0;
  int command_id_cache = 16;
};

/// Identificação do projeto/dispositivo e fuso horário.
struct ProjectParams {
  char device_id[32] = {};
  char firmware_version[16] = {};
  int32_t utc_offset_s = -3 * 3600;
  char posix_tz[16] = {};  // ex.: "<-03>3" (sem horário de verão)
};

/// Um sensor INA226.
struct Ina226Params {
  char name[8] = {};
  uint8_t address = 0x40;
  double shunt_ohm = 0.010;
  double max_current_a = 5.0;
  int sign = 1;  // +1: corrente positiva no sentido IN+ -> IN-
};

/// Mapa de pinos e endereços (system.yaml: hardware).
struct HardwareParams {
  int i2c_sda = 21;
  int i2c_scl = 22;
  uint32_t i2c_hz = 100000;
  int onewire_pin = 4;
  int status_led_pin = 2;
  int ina_alert_pin = 34;
  int button_pin = 35;
  int uart_mppt_rx = 16;
  int uart_mppt_tx = 17;
  Ina226Params ina_pv{};
  Ina226Params ina_bat{};
  Ina226Params ina_bus{};
  uint8_t rtc_ds3231_address = 0x68;
};

/// Bateria (subconjunto usado no firmware).
struct BatteryParams {
  double nominal_v = 12.8;
  double capacity_ah = 6.0;
  double max_charge_current_a = 3.0;
  double max_discharge_current_a = 6.0;
  double bms_ovp_v = 14.6;
  double bms_uvp_v = 10.0;
  double charge_min_temp_c = 0.0;
};

/// Conjunto completo de parâmetros (gerado em include/system_params.h).
struct SystemParams {
  ProjectParams project{};
  BatteryParams battery{};
  SocParams soc{};
  SafetyParams safety{};
  RulesParams rules{};
  PredictiveParams predictive{};
  LoadSetParams loads{};
  DemandParams demand{};
  ConventionalParams conventional{};
  CommParams comm{};
  HardwareParams hardware{};
  double aux_p_w = 0.8;
  EmsMode default_mode = EmsMode::Rules;
};

}  // namespace sem
