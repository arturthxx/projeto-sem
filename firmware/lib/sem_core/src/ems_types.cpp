// Utilidades das decisões do EMS (comparação e montagem de 'reason').
#include "sem_core/ems_types.h"

#include <cmath>
#include <cstdarg>
#include <cstdio>
#include <cstring>

namespace sem {

bool decision_differs(const Decision& a, const Decision& b, double duty_tol) {
  if (a.mode != b.mode || a.state != b.state || a.fallback != b.fallback || a.n_loads != b.n_loads) return true;
  for (int i = 0; i < a.n_loads && i < kMaxLoads; ++i) {
    if (a.commands[i].on != b.commands[i].on) return true;
    if (std::fabs(a.commands[i].duty - b.commands[i].duty) > duty_tol) return true;
  }
  return false;
}

void append_reason(char* buf, size_t len, const char* fmt, ...) {
  if (buf == nullptr || len == 0) return;
  size_t used = std::strlen(buf);
  if (used + 1 >= len) return;
  if (used > 0) {
    const int n = std::snprintf(buf + used, len - used, "; ");
    if (n < 0) return;
    used += static_cast<size_t>(n);
    if (used + 1 >= len) return;
  }
  va_list args;
  va_start(args, fmt);
  std::vsnprintf(buf + used, len - used, fmt, args);
  va_end(args);
}

}  // namespace sem
