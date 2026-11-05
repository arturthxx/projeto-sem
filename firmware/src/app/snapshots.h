// =============================================================================
// src/app/snapshots.h — estruturas trocadas entre as tarefas (por fila).
//
// Puras (sem FreeRTOS/Arduino): a tarefa de controle produz instantâneos;
// a de comunicação apenas os serializa (comms/telemetry) e publica.
// =============================================================================
#pragma once

#include <cstdint>

#include "sem_core/sem_core.h"

namespace semapp {

/// Média de uma grandeza V/I/P na janela de telemetria.
struct MeasAvg {
  bool ok = false;  // false => valores null no JSON
  double v = 0.0;
  double i = 0.0;
  double p = 0.0;
};

struct TempMeas {
  bool ok = false;
  double t = 0.0;
};

struct LoadSnap {
  int id = 0;
  char key[sem::kKeyLen] = {};
  sem::Priority prio = sem::Priority::Low;
  bool demand = false;
  bool on = false;
  double duty = 0.0;
  double p = 0.0;  // W estimados (p_src = "estimated")
  bool fault = false;
  bool deferrable = false;
  double e_req_wh = 0.0;
  double e_done_wh = 0.0;
  int32_t window_start_s = 0;
  int32_t deadline_s = 0;
};

/// Bits de falhas ativas (lista 'faults' da telemetria).
enum FaultBit : uint32_t {
  kFaultInaPv = 1u << 0,
  kFaultInaBat = 1u << 1,
  kFaultInaBus = 1u << 2,
  kFaultTempBat = 1u << 3,
  kFaultTempPv = 1u << 4,
  kFaultNoTime = 1u << 5,     // sem hora válida: perfil de demanda inativo
  kFaultSocInvalid = 1u << 6,
  kFaultNvs = 1u << 7,
};

/// Instantâneo de telemetria (sem.telemetry.v1). 'seq' é atribuído pela comms_task.
struct TelemetrySnapshot {
  uint32_t boot_id = 0;
  uint32_t seq = 0;
  int64_t ts_ms = 0;
  bool time_synced = false;
  int64_t uptime_ms = 0;
  uint32_t window_ms = 0;
  uint32_t n_samples = 0;
  MeasAvg pv{}, bat{}, bus{};
  TempMeas t_pv{}, t_bat{};
  bool has_day = false;
  char day[11] = {};  // "AAAA-MM-DD"
  double e_pv_in_wh = 0.0;
  double e_bat_chg_wh = 0.0;
  double e_bat_dis_wh = 0.0;
  double e_load_wh = 0.0;
  double e_unserved_wh = 0.0;
  bool soc_has_value = false;
  double soc = 0.0;
  double soc_unc = 0.0;
  bool soc_valid = false;
  sem::SyncKind last_sync = sem::SyncKind::None;
  double since_sync_s = 0.0;
  int n_loads = 0;
  LoadSnap loads[sem::kMaxLoads] = {};
  sem::EmsMode mode = sem::EmsMode::Rules;
  sem::EmsState state = sem::EmsState::Normal;
  sem::SafetyCode safety = sem::SafetyCode::Boot;
  bool fallback = false;
  bool has_cmd_age = false;
  double cmd_age_s = 0.0;
  uint32_t faults = 0;  // FaultBit
};

/// Decisão publicada a cada mudança (sem.decision.v1).
struct DecisionSnapshot {
  int64_t ts_ms = 0;
  sem::Decision decision{};
  sem::SafetyCode safety = sem::SafetyCode::Boot;
  int ids[sem::kMaxLoads] = {};  // id do contrato de cada índice de carga
  double soc = 0.0;
  bool soc_valid = false;
  double v_bat = 0.0;
  double p_pv = 0.0;
  double p_bat = 0.0;
  double p_bus = 0.0;
};

/// Comando validado (comms -> controle).
struct CommandMessage {
  sem::ParsedCommand cmd{};
  int64_t rx_ms = 0;  // epoch ms no recebimento (0 sem hora)
  bool time_synced = false;
};

/// Metadados fixos dos pacotes.
struct PayloadMeta {
  const char* device_id = "";
  const char* fw_version = "";
  const char* source = "hardware";
};

}  // namespace semapp
