// Conversões entre enumerações e o vocabulário textual do contrato (docs/05 §1).
#include "sem_core/types.h"

#include <cstring>

namespace sem {

namespace {

template <typename E, size_t N>
bool parse_from_table(const char* s, const char* const (&names)[N], E* out) {
  if (s == nullptr || out == nullptr) return false;
  for (size_t i = 0; i < N; ++i) {
    if (std::strcmp(s, names[i]) == 0) {
      *out = static_cast<E>(i);
      return true;
    }
  }
  return false;
}

// As tabelas seguem a ORDEM numérica das enumerações em types.h.
constexpr const char* kPriorityNames[] = {"high", "medium", "low"};
constexpr const char* kModeNames[] = {"conventional", "rules", "predictive"};
constexpr const char* kStateNames[] = {"PROTECTION", "CRITICAL", "ECONOMY", "NORMAL"};
constexpr const char* kSafetyNames[] = {"OK", "BOOT", "LVD", "OVP", "OC", "OC_LOCKOUT", "OT", "SENSOR"};
constexpr const char* kSyncNames[] = {"none", "ocv_init", "persisted", "full", "empty", "manual"};

template <size_t N>
const char* name_at(const char* const (&names)[N], size_t idx) {
  return idx < N ? names[idx] : "?";
}

}  // namespace

const char* to_string(Priority p) { return name_at(kPriorityNames, static_cast<size_t>(p)); }
const char* to_string(EmsMode m) { return name_at(kModeNames, static_cast<size_t>(m)); }
const char* to_string(EmsState s) { return name_at(kStateNames, static_cast<size_t>(s)); }
const char* to_string(SafetyCode c) { return name_at(kSafetyNames, static_cast<size_t>(c)); }
const char* to_string(SyncKind k) { return name_at(kSyncNames, static_cast<size_t>(k)); }

bool parse_priority(const char* s, Priority* out) { return parse_from_table(s, kPriorityNames, out); }
bool parse_mode(const char* s, EmsMode* out) { return parse_from_table(s, kModeNames, out); }
bool parse_state(const char* s, EmsState* out) { return parse_from_table(s, kStateNames, out); }
bool parse_safety_code(const char* s, SafetyCode* out) { return parse_from_table(s, kSafetyNames, out); }
bool parse_sync_kind(const char* s, SyncKind* out) { return parse_from_table(s, kSyncNames, out); }

}  // namespace sem
