// =============================================================================
// src/app/control_task.h — tarefa de CONTROLE (FreeRTOS, prioridade alta,
// núcleo 1). A cada 100 ms: leitura dos sensores, estimador de SOC,
// supervisor de segurança (TODA amostra) e aplicação das saídas
// (on_final = on_EMS AND allowed); a cada 1 s: EMS (modo atual);
// integração de energia, janela de telemetria, persistência e eventos.
// Watchdog de tarefa: se o laço travar por mais de kTaskWdtTimeoutS, o ESP32
// reinicia (e as saídas voltam a LOW pelo pull-down externo).
// Não depende da comunicação: nada aqui espera por Wi-Fi/MQTT.
// =============================================================================
#pragma once

#include "sem_core/params.h"

namespace semapp {

/// Inicializa o hardware de controle e cria a tarefa (núcleo 1). 'params' deve
/// ter duração estática.
bool start_control_task(const sem::SystemParams& params);

}  // namespace semapp
