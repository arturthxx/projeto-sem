// =============================================================================
// sem_core/sem_core.h — cabeçalho único da lógica pura do firmware.
//
// Biblioteca sem dependência de Arduino/ESP-IDF (apenas C++17 + ArduinoJson,
// que é portátil), compilada tanto no ESP32 quanto no PC (testes nativos).
// Toda a lógica usa double e recebe parâmetros por structs (sem globais).
// =============================================================================
#pragma once

#include "sem_core/command_validator.h"  // docs/05 §7
#include "sem_core/ems_controller.h"     // modos do EMS
#include "sem_core/ems_types.h"          // EmsInputs / Decision
#include "sem_core/energy.h"             // docs/06 §4 + LoadManager
#include "sem_core/events.h"             // sem.event.v1
#include "sem_core/params.h"             // structs de parâmetros
#include "sem_core/rules_engine.h"       // docs/06 §3.2
#include "sem_core/safety_supervisor.h"  // docs/06 §2
#include "sem_core/soc_estimator.h"      // docs/06 §1
#include "sem_core/strategies.h"         // docs/06 §3.1, §3.4 + perfil de demanda
#include "sem_core/time_util.h"          // janelas e calendário
#include "sem_core/types.h"              // vocabulário do contrato
