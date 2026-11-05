// =============================================================================
// src/app/comms_task.h — tarefa de COMUNICAÇÃO (núcleo 0, prioridade baixa):
// Wi-Fi, SNTP, MQTT; validação de comandos (HMAC/schema/replay/skew/TTL) com
// ack; publicação de telemetria, tópicos escalares, decisões, eventos e
// status. A perda de comunicação NUNCA bloqueia controle nem segurança: a
// troca com a control_task é feita só por filas com timeout zero e pelo
// estado compartilhado protegido por mutex (app/app_context.h).
// =============================================================================
#pragma once

#include "sem_core/params.h"

namespace semapp {

bool start_comms_task(const sem::SystemParams& params);

}  // namespace semapp
