// =============================================================================
// include/config.h — constantes de COMPILAÇÃO do firmware (não são parâmetros
// do sistema: estes vêm de config/system.yaml via include/system_params.h).
// =============================================================================
#pragma once

#include <cstddef>
#include <cstdint>

#include "system_params.h"

namespace semcfg {

// --- Identificação --------------------------------------------------------------
constexpr const char* kFwVersion = sem::sysp::kFirmwareVersion;  // de project.firmware_version
constexpr const char* kSource = "hardware";                       // docs/05 §1: dados MEDIDOS
constexpr const char* kFwBuildDate = __DATE__ " " __TIME__;

// --- Tarefas FreeRTOS (docs: firmware/README.md, "Arquitetura de tarefas") ---------
constexpr uint32_t kControlTaskStack = 8192;   // bytes
constexpr unsigned kControlTaskPriority = 10;  // alta (loopTask = 1; Wi-Fi/lwIP rodam no núcleo 0)
constexpr int kControlTaskCore = 1;
constexpr uint32_t kCommsTaskStack = 10240;
constexpr unsigned kCommsTaskPriority = 3;
constexpr int kCommsTaskCore = 0;
constexpr uint32_t kCommsLoopMs = 20;
constexpr uint32_t kTaskWdtTimeoutS = 5;  // watchdog da tarefa de controle

// --- Filas entre tarefas ------------------------------------------------------------
constexpr unsigned kEventQueueLen = 16;
constexpr unsigned kCommandQueueLen = 4;
constexpr unsigned kDecisionQueueLen = 4;

// --- MQTT ------------------------------------------------------------------------------
constexpr uint16_t kMqttBufferSize = 2560;  // telemetria sem.telemetry.v1 (~1,6 KB) + cabeçalho
constexpr size_t kJsonOutMax = 2304;        // buffer de serialização
constexpr uint16_t kMqttKeepAliveS = 30;
constexpr uint16_t kMqttSocketTimeoutS = 5;
constexpr uint32_t kWifiBackoffMinMs = 2000;
constexpr uint32_t kWifiBackoffMaxMs = 60000;
constexpr uint32_t kMqttBackoffMinMs = 2000;
constexpr uint32_t kMqttBackoffMaxMs = 30000;

// --- Hora -------------------------------------------------------------------------------
constexpr const char* kNtpServer1 = "a.st1.ntp.br";
constexpr const char* kNtpServer2 = "pool.ntp.org";
constexpr int64_t kMinValidEpochS = 1704067200;  // 2024-01-01: relógio abaixo disso = não sincronizado

// --- Sensores ------------------------------------------------------------------------
constexpr uint32_t kInaStaleMs = 500;      // sem conversão nova por mais que isso = falha
constexpr int kInaFailThreshold = 3;       // leituras I2C seguidas com erro => sensor inválido
constexpr uint32_t kInaRetryMs = 5000;     // nova tentativa de inicialização
constexpr uint16_t kI2cTimeoutMs = 20;
constexpr uint8_t kTempResolutionBits = 11;  // DS18B20: 375 ms de conversão, 0,125 °C
constexpr uint32_t kTempPeriodMs = 1000;
constexpr uint32_t kTempStaleMs = 5000;

// --- Saídas / painel ------------------------------------------------------------------
constexpr uint8_t kLedcResolutionBits = 12;
constexpr uint32_t kButtonLongPressMs = 2000;  // botão de reset de falhas
constexpr uint32_t kButtonDebounceMs = 30;

}  // namespace semcfg
