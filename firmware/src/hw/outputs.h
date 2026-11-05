// =============================================================================
// src/hw/outputs.h — saídas físicas das cargas (MOSFET low-side; HIGH = carga
// ligada; pull-down externo no gate garante "desligado" durante o boot).
//
//   * force_all_low(): PRIMEIRA ação do setup() — todos os pinos de carga
//     como saída em nível baixo, antes de qualquer outra inicialização;
//   * cargas dimerizáveis: PWM LEDC em pwm_freq_hz (system.yaml), resolução
//     kLedcResolutionBits; demais: saída digital;
//   * compatível com o core Arduino-ESP32 2.x (ledcSetup/ledcAttachPin/
//     ledcWrite(canal)) e 3.x (ledcAttach/ledcWrite(pino)).
// =============================================================================
#pragma once

#include <cstdint>

#include "sem_core/params.h"

namespace semhw {

class LoadOutputs {
 public:
  /// Coloca TODOS os pinos de carga em LOW (chamar antes de qualquer outra coisa).
  static void force_all_low(const int* gpios, int n);

  /// Configura PWM para as cargas dimerizáveis. Todas começam desligadas.
  void begin(const sem::LoadSetParams& loads);

  /// Escreve o estado físico da carga 'idx' (duty já normalizado pelo LoadManager).
  void write(int idx, bool on, double duty);

  /// Desliga tudo (falha grave / reinício).
  void all_off();

 private:
  const sem::LoadSetParams* loads_ = nullptr;
  int8_t channel_[sem::kMaxLoads] = {};  // canal LEDC (core 2.x) ou -1 se digital
};

}  // namespace semhw
