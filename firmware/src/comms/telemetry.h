// =============================================================================
// src/comms/telemetry.h — montagem dos payloads JSON do contrato (docs/05):
// sem.telemetry.v1 (§3), valor escalar (§4), sem.decision.v1 (§5),
// sem.event.v1 (§6), ack de comando (§7) e status/LWT (§2).
//
// Depende apenas de ArduinoJson + sem_core (sem Arduino.h): também é compilado
// no PC por tools/host_compile_check.sh, que valida a saída contra schemas/.
// Todas as funções retornam o número de bytes escritos em 'out' (sem o '\0')
// ou 0 se o buffer for pequeno demais.
// =============================================================================
#pragma once

#include <cstddef>
#include <cstdint>

#include "app/snapshots.h"

namespace semapp {

size_t build_telemetry_json(const TelemetrySnapshot& s, const PayloadMeta& meta, char* out, size_t cap);

size_t build_decision_json(const DecisionSnapshot& s, const PayloadMeta& meta, char* out, size_t cap);

size_t build_event_json(const sem::CoreEvent& ev, const PayloadMeta& meta, char* out, size_t cap);

/// Ack em smartenergy/cmd/ack. id == nullptr => "id": null.
size_t build_ack_json(const char* id, bool accepted, const char* reason, int64_t ts_ms, char* out, size_t cap);

/// Status (retido; também usado como LWT com online = false).
size_t build_status_json(bool online, const PayloadMeta& meta, uint32_t boot_id, int64_t ts_ms, char* out,
                         size_t cap);

/// Valor escalar numérico (tópicos de compatibilidade, docs/05 §4).
size_t build_scalar_json(double value, int decimals, const char* unit, const char* point, int64_t ts_ms,
                         const PayloadMeta& meta, char* out, size_t cap);

/// Valor escalar textual (ex.: smartenergy/ems/mode).
size_t build_scalar_text_json(const char* value, const char* point, int64_t ts_ms, const PayloadMeta& meta, char* out,
                              size_t cap);

/// Nome textual de cada bit de falha (lista 'faults').
const char* fault_name(uint32_t bit);

/// Arredonda para 'decimals' casas (payload compacto e legível).
double round_to(double x, int decimals);

}  // namespace semapp
