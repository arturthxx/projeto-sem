// =============================================================================
// src/app/app_context.h — recursos compartilhados entre as tarefas.
//
//   control_task (núcleo 1) --q_events/q_decisions/q_telemetry--> comms_task (núcleo 0)
//   comms_task              --q_commands----------------------> control_task
//   status (mutex): modo, segurança, boot_id, hora válida, comunicação ativa.
//
// Toda operação de fila usa timeout ZERO no lado do controle (nunca bloqueia):
// se a comunicação estiver parada, eventos excedentes são descartados e a
// telemetria é sobrescrita (xQueueOverwrite, fila de 1 posição).
// =============================================================================
#pragma once

#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/semphr.h>

#include <cstdint>

#include "app/snapshots.h"
#include "sem_core/sem_core.h"

namespace semapp {

struct SharedStatus {
  sem::EmsMode mode = sem::EmsMode::Rules;
  sem::SafetyCode safety = sem::SafetyCode::Boot;
  uint32_t boot_id = 0;
  bool time_synced = false;
  bool mqtt_up = false;
};

class AppContext {
 public:
  /// Cria filas e mutex. Retorna false se faltar memória.
  bool begin();

  SharedStatus status() const;
  void set_mode(sem::EmsMode m);
  void set_safety(sem::SafetyCode c);
  void set_boot_id(uint32_t id);
  void set_time_synced(bool v);
  void set_mqtt_up(bool v);

  /// Enfileira sem bloquear (descarta se cheia). Retorna false se descartou.
  bool post_event(const sem::CoreEvent& ev);
  bool post_decision(const DecisionSnapshot& d);
  void post_telemetry(const TelemetrySnapshot& t);
  bool post_command(const CommandMessage& c);
  unsigned command_slots() const;

  QueueHandle_t q_events = nullptr;
  QueueHandle_t q_decisions = nullptr;
  QueueHandle_t q_telemetry = nullptr;
  QueueHandle_t q_commands = nullptr;

 private:
  mutable SemaphoreHandle_t mutex_ = nullptr;
  SharedStatus status_{};
};

/// Instância única (criada em main.cpp).
extern AppContext g_app;

/// Tempo monotônico em segundos desde o boot (esp_timer, 64 bits).
double monotonic_s();

/// Epoch UTC em ms se o relógio for válido; 0 caso contrário.
int64_t epoch_ms_or_zero();

}  // namespace semapp
