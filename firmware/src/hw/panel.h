// =============================================================================
// src/hw/panel.h — painel local: botão de reset de falhas (GPIO 35, entrada
// sem pull-up interno — pull-up externo de 10 k; ativo em nível baixo) e LED
// de status (GPIO 2).
//
// Padrões do LED:
//   BOOT      piscar rápido (10 Hz)
//   OK        pulso curto a cada 1 s
//   NO_COMMS  dois pulsos curtos a cada 2 s (controle local segue normal)
//   FAULT     piscar 4 Hz (proteção ativa: LVD/OVP/OC/OT/SENSOR)
//   LOCKOUT   aceso fixo (OC_LOCKOUT: exige botão ou comando reset_faults)
// =============================================================================
#pragma once

#include <cstdint>

namespace semhw {

enum class LedPattern : uint8_t { Boot, Ok, NoComms, Fault, Lockout };

class Panel {
 public:
  void begin(int led_pin, int button_pin);

  /// Retorna true UMA vez quando o botão é mantido pressionado por kButtonLongPressMs.
  bool poll_reset_button(uint32_t now_ms);

  void update_led(uint32_t now_ms, LedPattern pattern);

 private:
  int led_pin_ = -1;
  int button_pin_ = -1;
  bool raw_last_ = false;
  bool stable_ = false;
  uint32_t t_change_ms_ = 0;
  uint32_t t_press_ms_ = 0;
  bool fired_ = false;
  bool led_on_ = false;
};

}  // namespace semhw
