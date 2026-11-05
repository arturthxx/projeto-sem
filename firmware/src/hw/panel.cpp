// Botão de reset de falhas (com debounce e pressionamento longo) e LED de status.
#include "hw/panel.h"

#include <Arduino.h>

#include "config.h"

namespace semhw {

void Panel::begin(int led_pin, int button_pin) {
  led_pin_ = led_pin;
  button_pin_ = button_pin;
  if (led_pin_ >= 0) {
    pinMode(static_cast<uint8_t>(led_pin_), OUTPUT);
    digitalWrite(static_cast<uint8_t>(led_pin_), LOW);
  }
  if (button_pin_ >= 0) pinMode(static_cast<uint8_t>(button_pin_), INPUT);  // GPIO 34–39: sem pull interno
}

bool Panel::poll_reset_button(uint32_t now_ms) {
  if (button_pin_ < 0) return false;
  const bool pressed = digitalRead(static_cast<uint8_t>(button_pin_)) == LOW;
  if (pressed != raw_last_) {
    raw_last_ = pressed;
    t_change_ms_ = now_ms;
  }
  if (now_ms - t_change_ms_ >= semcfg::kButtonDebounceMs && stable_ != raw_last_) {
    stable_ = raw_last_;
    if (stable_) {
      t_press_ms_ = now_ms;
      fired_ = false;
    }
  }
  if (stable_ && !fired_ && now_ms - t_press_ms_ >= semcfg::kButtonLongPressMs) {
    fired_ = true;
    return true;
  }
  return false;
}

void Panel::update_led(uint32_t now_ms, LedPattern pattern) {
  if (led_pin_ < 0) return;
  bool on = false;
  switch (pattern) {
    case LedPattern::Boot:
      on = (now_ms / 50) % 2 == 0;
      break;
    case LedPattern::Ok:
      on = (now_ms % 1000) < 80;
      break;
    case LedPattern::NoComms: {
      const uint32_t t = now_ms % 2000;
      on = t < 80 || (t >= 240 && t < 320);
      break;
    }
    case LedPattern::Fault:
      on = (now_ms / 125) % 2 == 0;
      break;
    case LedPattern::Lockout:
      on = true;
      break;
  }
  if (on != led_on_) {
    led_on_ = on;
    digitalWrite(static_cast<uint8_t>(led_pin_), on ? HIGH : LOW);
  }
}

}  // namespace semhw
