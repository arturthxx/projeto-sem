// =============================================================================
// GERADO AUTOMATICAMENTE — não editar.
// Fonte: config/system.yaml (sha256 d258bdb04a59eb96...)
// Gerador: tools/gen_firmware_params.py  (rode-o após alterar o YAML)
// Valores marcados PENDENTE no YAML são referências a confirmar em datasheet/medição.
// =============================================================================
#pragma once

#include "sem_core/params.h"

namespace sem {
namespace sysp {

// --- Identificação ---
constexpr const char* kDeviceId = "sem-esp32-01";
constexpr const char* kFirmwareVersion = "0.1.0";
constexpr const char* kMqttPrefix = "smartenergy";
constexpr const char* kPosixTz = "<-03>3";
constexpr int32_t kUtcOffsetS = -10800;

// --- Períodos ---
constexpr uint32_t kSamplePeriodMs = 100;
constexpr uint32_t kEmsPeriodMs = 1000;
constexpr uint32_t kTelemetryPeriodMs = 5000;
constexpr uint32_t kPersistEveryS = 600;

// --- Pinos (docs/04-mapa-de-pinos.md) ---
constexpr int kPinI2cSda = 21;
constexpr int kPinI2cScl = 22;
constexpr uint32_t kI2cHz = 100000;
constexpr int kPinOneWire = 4;
constexpr int kPinStatusLed = 2;
constexpr int kPinInaAlert = 34;  // entrada; pull-up externo
constexpr int kPinButton = 35;       // entrada; pull-up externo
constexpr int kPinUartMpptRx = 16;
constexpr int kPinUartMpptTx = 17;
constexpr int kNumLoads = 3;
constexpr int kLoadGpio[kNumLoads] = {25, 26, 27};

// --- Endereços I2C ---
constexpr uint8_t kIna226AddrPv = 0x40;
constexpr uint8_t kIna226AddrBat = 0x41;
constexpr uint8_t kIna226AddrBus = 0x44;
constexpr uint8_t kRtcDs3231Addr = 0x68;

}  // namespace sysp

/// Parâmetros completos do sistema (espelho de config/system.yaml).
inline SystemParams make_system_params() {
  SystemParams p{};
  // project / site
  copy_cstr(p.project.device_id, "sem-esp32-01");
  copy_cstr(p.project.firmware_version, "0.1.0");
  p.project.utc_offset_s = -10800;
  copy_cstr(p.project.posix_tz, "<-03>3");
  // battery
  p.battery.nominal_v = 12.8;
  p.battery.capacity_ah = 6.0;
  p.battery.max_charge_current_a = 3.0;
  p.battery.max_discharge_current_a = 6.0;
  p.battery.bms_ovp_v = 14.6;
  p.battery.bms_uvp_v = 10.0;
  p.battery.charge_min_temp_c = 0.0;
  // soc_estimator (+ battery, safety.sensor_timeout_s)
  p.soc.capacity_ah = 6.0;
  p.soc.coulombic_efficiency = 0.99;
  p.soc.r_internal_ohm = 0.04;
  p.soc.current_deadband_a = 0.005;
  p.soc.full_voltage_v = 14.0;
  p.soc.full_tail_current_a = 0.3;
  p.soc.full_hold_s = 120.0;
  p.soc.empty_voltage_v = 11.8;
  p.soc.empty_hold_s = 30.0;
  p.soc.empty_soc_value = 0.05;
  p.soc.unknown_initial_uncertainty = 0.3;
  p.soc.sync_uncertainty = 0.02;
  p.soc.drift_uncertainty_per_ah = 0.01;
  p.soc.persist_every_s = 600.0;
  p.soc.persist_delta_soc = 0.02;
  p.soc.sensor_timeout_s = 3.0;
  p.soc.ocv_soc[0] = 0.0;  p.soc.ocv_v[0] = 10.0;
  p.soc.ocv_soc[1] = 0.05;  p.soc.ocv_v[1] = 11.6;
  p.soc.ocv_soc[2] = 0.1;  p.soc.ocv_v[2] = 12.0;
  p.soc.ocv_soc[3] = 0.2;  p.soc.ocv_v[3] = 12.8;
  p.soc.ocv_soc[4] = 0.3;  p.soc.ocv_v[4] = 12.95;
  p.soc.ocv_soc[5] = 0.4;  p.soc.ocv_v[5] = 13.0;
  p.soc.ocv_soc[6] = 0.5;  p.soc.ocv_v[6] = 13.05;
  p.soc.ocv_soc[7] = 0.6;  p.soc.ocv_v[7] = 13.1;
  p.soc.ocv_soc[8] = 0.7;  p.soc.ocv_v[8] = 13.15;
  p.soc.ocv_soc[9] = 0.8;  p.soc.ocv_v[9] = 13.2;
  p.soc.ocv_soc[10] = 0.9;  p.soc.ocv_v[10] = 13.3;
  p.soc.ocv_soc[11] = 0.95;  p.soc.ocv_v[11] = 13.35;
  p.soc.ocv_soc[12] = 0.99;  p.soc.ocv_v[12] = 13.45;
  p.soc.ocv_soc[13] = 1.0;  p.soc.ocv_v[13] = 13.6;
  p.soc.ocv_count = 14;
  // safety
  p.safety.lvd_v = 11.6;
  p.safety.lvd_delay_s = 5.0;
  p.safety.soc_hard_min = 0.1;
  p.safety.reconnect_v = 12.9;
  p.safety.reconnect_min_soc = 0.25;
  p.safety.bus_ovp_v = 15.0;
  p.safety.bus_ovp_delay_s = 1.0;
  p.safety.bus_overcurrent_a = 4.0;
  p.safety.bus_overcurrent_delay_ms = 200.0;
  p.safety.overcurrent_retry_s = 30.0;
  p.safety.battery_overtemp_c = 55.0;
  p.safety.battery_overtemp_release_c = 45.0;
  p.safety.sensor_timeout_s = 3.0;
  p.safety.overcurrent_max_retries = 3;
  p.safety.boot_settle_samples = 20;
  // ems.rules
  p.rules.normal_min_soc = 0.7;
  p.rules.economy_min_soc = 0.4;
  p.rules.critical_min_soc = 0.2;
  p.rules.hysteresis_soc = 0.05;
  p.rules.economy_medium_duty = 0.6;
  p.rules.min_dwell_s = 300.0;
  p.rules.surplus_on_soc = 0.9;
  p.rules.surplus_off_soc = 0.8;
  p.rules.economy_surplus_margin_w = 2.0;
  p.rules.deadline_margin_s = 1800.0;
  // ems.predictive (parte usada no firmware)
  p.predictive.step_s = 900.0;
  p.predictive.replan_s = 900.0;
  p.predictive.command_ttl_s = 1200.0;
  p.predictive.soc_min = 0.2;
  p.predictive.soc_max = 1.0;
  p.predictive.horizon_steps = 96;
  p.default_mode = sem::EmsMode::Rules;
  p.aux_p_w = 0.8;
  // loads
  p.loads.count = 3;
  // [0] led: Iluminacao LED 12 V
  p.loads.items[0].id = 1;
  copy_cstr(p.loads.items[0].key, "led");
  p.loads.items[0].priority = sem::Priority::High;
  p.loads.items[0].p_nom_w = 5.0;
  p.loads.items[0].v_ref_v = 12.8;
  p.loads.items[0].voltage_exponent = 1.5;
  p.loads.items[0].dimmable = false;
  p.loads.items[0].min_duty = 0.0;
  p.loads.items[0].pwm_freq_hz = 0.0;
  p.loads.items[0].i_max_a = 0.6;
  p.loads.items[0].fuse_a = 2.0;
  p.loads.items[0].gpio = 25;
  // [1] fan: Ventilador CC 12 V
  p.loads.items[1].id = 2;
  copy_cstr(p.loads.items[1].key, "fan");
  p.loads.items[1].priority = sem::Priority::Medium;
  p.loads.items[1].p_nom_w = 6.0;
  p.loads.items[1].v_ref_v = 12.8;
  p.loads.items[1].voltage_exponent = 1.5;
  p.loads.items[1].dimmable = true;
  p.loads.items[1].min_duty = 0.4;
  p.loads.items[1].pwm_freq_hz = 100.0;
  p.loads.items[1].i_max_a = 0.8;
  p.loads.items[1].fuse_a = 2.0;
  p.loads.items[1].gpio = 26;
  // [2] res: Carga resistiva 10 ohm (simula bombeamento/aquecimento - deslocavel)
  p.loads.items[2].id = 3;
  copy_cstr(p.loads.items[2].key, "res");
  p.loads.items[2].priority = sem::Priority::Low;
  p.loads.items[2].p_nom_w = 16.4;
  p.loads.items[2].v_ref_v = 12.8;
  p.loads.items[2].voltage_exponent = 2.0;
  p.loads.items[2].dimmable = false;
  p.loads.items[2].min_duty = 0.0;
  p.loads.items[2].pwm_freq_hz = 0.0;
  p.loads.items[2].i_max_a = 1.6;
  p.loads.items[2].fuse_a = 3.0;
  p.loads.items[2].gpio = 27;
  p.loads.items[2].deferrable.enabled = true;
  p.loads.items[2].deferrable.daily_energy_wh = 40.0;
  p.loads.items[2].deferrable.window_start_s = 25200;  // 07:00
  p.loads.items[2].deferrable.deadline_s = 79200;  // 22:00
  // demand.schedule (janelas [início, fim) em hora local)
  p.demand.schedule[0].windows[0] = {19800, 25200};  // led 05:30-07:00
  p.demand.schedule[0].windows[1] = {64800, 82800};  // led 18:00-23:00
  p.demand.schedule[0].count = 2;
  p.demand.schedule[1].windows[0] = {43200, 61200};  // fan 12:00-17:00
  p.demand.schedule[1].windows[1] = {68400, 82800};  // fan 19:00-23:00
  p.demand.schedule[1].count = 2;
  // demand.conventional.<key>_schedule (operação convencional da carga deslocável)
  p.conventional.deferrable_schedule[2].windows[0] = {64800, 79200};  // res 18:00-22:00
  p.conventional.deferrable_schedule[2].count = 1;
  // comm
  copy_cstr(p.comm.mqtt_prefix, "smartenergy");
  p.comm.telemetry_period_s = 5.0;
  p.comm.sample_period_ms = 100;
  p.comm.ems_period_ms = 1000;
  p.comm.command_hmac = true;
  p.comm.command_max_skew_s = 120.0;
  p.comm.command_id_cache = 16;
  // hardware
  p.hardware.i2c_sda = 21;
  p.hardware.i2c_scl = 22;
  p.hardware.onewire_pin = 4;
  p.hardware.status_led_pin = 2;
  p.hardware.ina_alert_pin = 34;
  p.hardware.button_pin = 35;
  p.hardware.uart_mppt_rx = 16;
  p.hardware.uart_mppt_tx = 17;
  p.hardware.i2c_hz = 100000;
  p.hardware.rtc_ds3231_address = 0x68;
  copy_cstr(p.hardware.ina_pv.name, "pv");
  p.hardware.ina_pv.address = 0x40;
  p.hardware.ina_pv.shunt_ohm = 0.01;
  p.hardware.ina_pv.max_current_a = 5.0;
  p.hardware.ina_pv.sign = 1;
  copy_cstr(p.hardware.ina_bat.name, "bat");
  p.hardware.ina_bat.address = 0x41;
  p.hardware.ina_bat.shunt_ohm = 0.01;
  p.hardware.ina_bat.max_current_a = 8.0;
  p.hardware.ina_bat.sign = 1;
  copy_cstr(p.hardware.ina_bus.name, "bus");
  p.hardware.ina_bus.address = 0x44;
  p.hardware.ina_bus.shunt_ohm = 0.01;
  p.hardware.ina_bus.max_current_a = 5.0;
  p.hardware.ina_bus.sign = 1;
  return p;
}

}  // namespace sem
