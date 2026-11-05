// =============================================================================
// test/support/fixtures.h — utilidades comuns aos testes nativos (header-only,
// incluído por caminho relativo para funcionar no PlatformIO e no script).
// Os parâmetros padrão vêm de include/system_params.h (gerado do system.yaml).
// =============================================================================
#pragma once

#include <cstring>

#include "sem_core/sem_core.h"
#include "system_params.h"

namespace semtest {

inline sem::SystemParams params() { return sem::make_system_params(); }

/// Entradas do EMS com valores neutros.
inline sem::EmsInputs make_inputs(double t_s, double tod_s, double soc, bool soc_valid = true) {
  sem::EmsInputs in{};
  in.t_s = t_s;
  in.tod_s = tod_s;
  in.soc = soc;
  in.soc_valid = soc_valid;
  in.v_bat = 13.0;
  return in;
}

/// Harness dos vetores golden: as saídas passam a ser as da decisão e, se 'on'
/// mudou, last_switch = t_s.
inline void adopt(sem::EmsInputs* in, const sem::Decision& d, double t_s) {
  for (int i = 0; i < d.n_loads; ++i) {
    sem::LoadOutput& o = in->outputs[i];
    if (o.on != d.commands[i].on) o.last_switch_t_s = t_s;
    o.on = d.commands[i].on;
    o.duty = d.commands[i].duty;
  }
}

inline bool contains(const char* haystack, const char* needle) { return std::strstr(haystack, needle) != nullptr; }

}  // namespace semtest
