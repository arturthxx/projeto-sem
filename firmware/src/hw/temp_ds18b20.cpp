// DS18B20 com conversão assíncrona (OneWire + DallasTemperature).
#include "hw/temp_ds18b20.h"

#include "config.h"

namespace semhw {

void TempSensors::begin(uint8_t resolution_bits) {
  resolution_ = resolution_bits;
  dallas_.begin();
  const uint8_t n = dallas_.getDeviceCount();
  count_ = 0;
  for (uint8_t i = 0; i < n && count_ < 2; ++i) {
    if (dallas_.getAddress(addr_[count_], i)) ++count_;
  }
  dallas_.setResolution(resolution_);
  dallas_.setWaitForConversion(false);  // NÃO bloquear a tarefa de controle
  converting_ = false;
}

uint32_t TempSensors::conversion_ms() const {
  // 750 ms em 12 bits, metade a cada bit a menos (datasheet DS18B20) + margem.
  const uint8_t bits = resolution_ < 9 ? 9 : (resolution_ > 12 ? 12 : resolution_);
  return (750u >> (12 - bits)) + 10u;
}

void TempSensors::poll(uint32_t now_ms) {
  now_ms_ = now_ms;
  if (count_ == 0) return;
  if (!converting_) {
    if (now_ms - t_request_ms_ >= semcfg::kTempPeriodMs || t_request_ms_ == 0) {
      dallas_.requestTemperatures();
      t_request_ms_ = now_ms;
      converting_ = true;
    }
    return;
  }
  if (now_ms - t_request_ms_ < conversion_ms()) return;
  converting_ = false;
  for (uint8_t i = 0; i < count_; ++i) {
    const float t = dallas_.getTempC(addr_[i]);
    // -127 = desconectado; 85 °C é o valor de power-on (conversão não ocorreu).
    if (t > DEVICE_DISCONNECTED_C + 1.0f && t != 85.0f && t > -55.0f && t < 125.0f) {
      t_c_[i] = static_cast<double>(t);
      t_last_ok_ms_[i] = now_ms;
      ever_ok_[i] = true;
    }
  }
}

TempReading TempSensors::value(int idx) const {
  TempReading r{};
  if (idx < 0 || idx >= count_ || !ever_ok_[idx]) return r;
  if (now_ms_ - t_last_ok_ms_[idx] > semcfg::kTempStaleMs) return r;  // leitura velha = inválida
  r.ok = true;
  r.t_c = t_c_[idx];
  return r;
}

}  // namespace semhw
