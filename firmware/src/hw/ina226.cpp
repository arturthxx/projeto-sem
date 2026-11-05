// Driver do INA226 (registradores em hw/ina226_regs.h).
#include "hw/ina226.h"

#include "config.h"
#include "hw/ina226_regs.h"

namespace semhw {

using namespace ina226;

int Ina226::kFailThreshold() { return semcfg::kInaFailThreshold; }

bool Ina226::write_reg(uint8_t reg, uint16_t value) {
  wire_.beginTransmission(p_.address);
  wire_.write(reg);
  wire_.write(static_cast<uint8_t>(value >> 8));
  wire_.write(static_cast<uint8_t>(value & 0xFF));
  return wire_.endTransmission() == 0;
}

bool Ina226::read_reg(uint8_t reg, uint16_t* value) {
  wire_.beginTransmission(p_.address);
  wire_.write(reg);
  if (wire_.endTransmission(false) != 0) return false;  // repeated start
  if (wire_.requestFrom(static_cast<uint8_t>(p_.address), static_cast<uint8_t>(2)) != 2) return false;
  if (wire_.available() < 2) return false;
  const int hi = wire_.read();
  const int lo = wire_.read();
  if (hi < 0 || lo < 0) return false;
  *value = static_cast<uint16_t>((hi << 8) | lo);
  return true;
}

void Ina226::note_error() {
  ++error_count_;
  if (consecutive_errors_ < 1000) ++consecutive_errors_;
}

bool Ina226::begin() {
  present_ = false;
  uint16_t mid = 0, did = 0;
  if (!write_reg(kRegConfig, kConfigReset)) return false;
  if (!read_reg(kRegManufacturerId, &mid) || !read_reg(kRegDieId, &did)) return false;
  if (mid != kManufacturerId || did != kDieId) return false;  // outro dispositivo no endereço
  if (!write_reg(kRegConfig, kDefaultConfig)) return false;
  cal_ = calibration_value(current_lsb_for(p_.max_current_a), p_.shunt_ohm);
  if (!write_reg(kRegCalibration, cal_)) return false;
  uint16_t check = 0;
  if (!read_reg(kRegConfig, &check) || check != kDefaultConfig) return false;
  present_ = true;
  consecutive_errors_ = 0;
  last_conversion_ms_ = 0;
  if (alert_configured_) configure_overcurrent_alert(alert_current_a_);
  return true;
}

bool Ina226::configure_overcurrent_alert(double current_a) {
  alert_configured_ = true;
  alert_current_a_ = current_a;
  if (!present_) return false;
  // SOL compara a tensão de shunt (com sinal); com sign = -1 a sobrecorrente seria negativa,
  // então o limite só é programado para sign = +1 (caso do sensor 'bus' no system.yaml).
  if (p_.sign < 0) return false;
  if (!write_reg(kRegAlertLimit, shunt_alert_limit(current_a, p_.shunt_ohm))) return false;
  return write_reg(kRegMaskEnable, kMaskSol);  // ativo baixo, transparente (LEN = 0)
}

bool Ina226::read(uint32_t now_ms, InaReading* r) {
  r->ok = false;
  if (!present_) return false;
  uint16_t mask = 0, sh = 0, bus = 0;
  if (!read_reg(kRegMaskEnable, &mask) || !read_reg(kRegShuntVoltage, &sh) || !read_reg(kRegBusVoltage, &bus)) {
    note_error();
    return false;
  }
  // CVRF indica conversão nova desde a última leitura do registrador 0x06.
  if ((mask & kMaskCvrf) != 0 || last_conversion_ms_ == 0) last_conversion_ms_ = now_ms;
  if (now_ms - last_conversion_ms_ > semcfg::kInaStaleMs) {
    note_error();  // conversor parado (ex.: reset espúrio do INA226)
    return false;
  }
  consecutive_errors_ = 0;
  r->shunt_v = shunt_volts(sh);
  r->bus_v = bus_volts(bus);
  r->current_a = static_cast<double>(p_.sign) * r->shunt_v / p_.shunt_ohm;
  r->power_w = r->bus_v * r->current_a;
  r->ok = true;
  return true;
}

void Ina226::maintain(uint32_t now_ms) {
  if (healthy()) return;
  if (now_ms - last_retry_ms_ < semcfg::kInaRetryMs) return;
  last_retry_ms_ = now_ms;
  begin();
}

}  // namespace semhw
