// =============================================================================
// Smart Energy Manager — firmware do ESP32 (PlatformIO, framework Arduino).
//
// Ordem de inicialização (segurança primeiro):
//   1. TODAS as saídas de carga em LOW (antes de qualquer outra coisa);
//   2. parâmetros (gerados de config/system.yaml), filas e mutex;
//   3. tarefa de CONTROLE no núcleo 1 (sensores, SOC, segurança, EMS, saídas);
//   4. tarefa de COMUNICAÇÃO no núcleo 0 (Wi-Fi, SNTP, MQTT, comandos).
// O loop() do Arduino fica ocioso.
// =============================================================================
#include <Arduino.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include "app/app_context.h"
#include "app/comms_task.h"
#include "app/control_task.h"
#include "config.h"
#include "hw/outputs.h"
#include "system_params.h"

namespace {
sem::SystemParams g_params;  // duração estática: as tarefas guardam referências
}

void setup() {
  // 1) Cargas desligadas ANTES de tudo (o pull-down externo cobre o intervalo até aqui).
  semhw::LoadOutputs::force_all_low(sem::sysp::kLoadGpio, sem::sysp::kNumLoads);

  Serial.begin(115200);
  Serial.printf("\n[sem] Smart Energy Manager fw %s (%s) device=%s\n", semcfg::kFwVersion, semcfg::kFwBuildDate,
                sem::sysp::kDeviceId);

  // 2) Parâmetros e recursos compartilhados.
  g_params = sem::make_system_params();
  if (!semapp::g_app.begin()) {
    Serial.println("[sem] ERRO: sem memoria para filas; reiniciando em 5 s (cargas desligadas)");
    delay(5000);
    ESP.restart();
  }

  // 3) e 4) Tarefas.
  if (!semapp::start_control_task(g_params)) {
    Serial.println("[sem] ERRO: falha ao criar a tarefa de controle; reiniciando");
    delay(1000);
    ESP.restart();
  }
  if (!semapp::start_comms_task(g_params)) {
    Serial.println("[sem] AVISO: tarefa de comunicacao nao criada; controle local segue ativo");
  }
}

void loop() {
  vTaskDelay(pdMS_TO_TICKS(1000));  // todo o trabalho está nas tarefas
}
