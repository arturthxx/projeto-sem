// Saídas de carga: digitais e PWM LEDC (compatível com core 2.x e 3.x).
#include "hw/outputs.h"

#include <Arduino.h>
#include <esp_arduino_version.h>

#include "config.h"

namespace semhw {

namespace {
uint32_t duty_counts(double duty) {
  const uint32_t full = (1u << semcfg::kLedcResolutionBits) - 1u;
  if (duty <= 0.0) return 0;
  if (duty >= 1.0) return full;
  return static_cast<uint32_t>(duty * static_cast<double>(full) + 0.5);
}
}  // namespace

void LoadOutputs::force_all_low(const int* gpios, int n) {
  for (int i = 0; i < n; ++i) {
    if (gpios[i] < 0) continue;
    digitalWrite(static_cast<uint8_t>(gpios[i]), LOW);  // registra o nível antes de virar saída
    pinMode(static_cast<uint8_t>(gpios[i]), OUTPUT);
    digitalWrite(static_cast<uint8_t>(gpios[i]), LOW);
  }
}

void LoadOutputs::begin(const sem::LoadSetParams& loads) {
  loads_ = &loads;
  uint8_t next_channel = 0;
  for (int i = 0; i < loads.count && i < sem::kMaxLoads; ++i) {
    const sem::LoadParams& L = loads.items[i];
    channel_[i] = -1;
    if (L.gpio < 0) continue;
    const uint8_t pin = static_cast<uint8_t>(L.gpio);
    if (L.dimmable && L.pwm_freq_hz > 0.0) {
      const uint32_t freq = static_cast<uint32_t>(L.pwm_freq_hz);
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
      ledcAttach(pin, freq, semcfg::kLedcResolutionBits);
      ledcWrite(pin, 0);
      channel_[i] = static_cast<int8_t>(next_channel++);  // apenas marca "PWM"
#else
      ledcSetup(next_channel, freq, semcfg::kLedcResolutionBits);
      ledcAttachPin(pin, next_channel);
      ledcWrite(next_channel, 0);
      channel_[i] = static_cast<int8_t>(next_channel++);
#endif
    } else {
      pinMode(pin, OUTPUT);
      digitalWrite(pin, LOW);
    }
  }
}

void LoadOutputs::write(int idx, bool on, double duty) {
  if (loads_ == nullptr || idx < 0 || idx >= loads_->count || idx >= sem::kMaxLoads) return;
  const int gpio = loads_->items[idx].gpio;
  if (gpio < 0) return;
  const uint8_t pin = static_cast<uint8_t>(gpio);
  if (channel_[idx] >= 0) {
    const uint32_t counts = on ? duty_counts(duty) : 0u;
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
    ledcWrite(pin, counts);
#else
    ledcWrite(static_cast<uint8_t>(channel_[idx]), counts);
#endif
  } else {
    digitalWrite(pin, on ? HIGH : LOW);
  }
}

void LoadOutputs::all_off() {
  if (loads_ == nullptr) return;
  for (int i = 0; i < loads_->count && i < sem::kMaxLoads; ++i) write(i, false, 0.0);
}

}  // namespace semhw
