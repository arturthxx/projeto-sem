// Eventos sem.event.v1: códigos textuais e lista de capacidade fixa.
#include "sem_core/events.h"

#include <cstdio>
#include <cstring>

#include "sem_core/types.h"

namespace sem {

namespace {
constexpr const char* kEventNames[] = {
    "BOOT",       "TIME_SYNC", "WIFI_UP",   "WIFI_DOWN",     "MQTT_UP",        "MQTT_DOWN",
    "SENSOR_FAIL", "SENSOR_OK", "LVD_TRIP",  "LVD_CLEAR",     "OVP_TRIP",       "OVP_CLEAR",
    "OC_TRIP",    "OC_LOCKOUT", "OT_TRIP",   "OT_CLEAR",      "SOC_SYNC_FULL",  "SOC_SYNC_EMPTY",
    "MODE_CHANGE", "CMD_REJECTED", "PLAN_EXPIRED", "FAULTS_RESET",
};
static_assert(sizeof(kEventNames) / sizeof(kEventNames[0]) == static_cast<size_t>(EventCode::Count_),
              "tabela de nomes de eventos desatualizada");

constexpr const char* kLevelNames[] = {"info", "warn", "error", "critical"};
}  // namespace

const char* to_string(EventCode c) {
  const auto i = static_cast<size_t>(c);
  return i < static_cast<size_t>(EventCode::Count_) ? kEventNames[i] : "UNKNOWN";
}

const char* to_string(EventLevel l) {
  const auto i = static_cast<size_t>(l);
  return i < 4 ? kLevelNames[i] : "info";
}

CoreEvent& CoreEvent::with_number(const char* key, double value) {
  if (n_data < 2) {
    EventDatum& d = data[n_data++];
    copy_cstr(d.key, key);
    d.is_string = false;
    d.number = value;
  }
  return *this;
}

CoreEvent& CoreEvent::with_text(const char* key, const char* value) {
  if (n_data < 2) {
    EventDatum& d = data[n_data++];
    copy_cstr(d.key, key);
    d.is_string = true;
    copy_cstr(d.text, value);
  }
  return *this;
}

CoreEvent& EventList::push(EventCode code, EventLevel level, const char* fmt, ...) {
  CoreEvent* ev = &overflow_;
  if (size_ < kCapacity) {
    ev = &items_[size_++];
  } else {
    ++dropped_;
  }
  *ev = CoreEvent{};
  ev->code = code;
  ev->level = level;
  va_list args;
  va_start(args, fmt);
  std::vsnprintf(ev->msg, sizeof(ev->msg), fmt, args);
  va_end(args);
  return *ev;
}

bool EventList::contains(EventCode code) const {
  for (int i = 0; i < size_; ++i)
    if (items_[i].code == code) return true;
  return false;
}

}  // namespace sem
