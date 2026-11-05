// =============================================================================
// src/app/wdt_compat.h — watchdog de tarefa (TWDT) compatível com ESP-IDF 4.x
// (core Arduino-ESP32 2.x) e 5.x (core 3.x), cuja API de configuração mudou.
// =============================================================================
#pragma once

#include <cstdint>

namespace semapp {

/// (Re)configura o TWDT com 'timeout_s' e pânico (reinício) em caso de estouro,
/// e inscreve a tarefa ATUAL. Retorna false se falhar.
bool wdt_subscribe_current_task(uint32_t timeout_s);

/// Alimenta o watchdog da tarefa atual.
void wdt_feed();

}  // namespace semapp
