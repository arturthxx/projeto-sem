// Filas, mutex e estado compartilhado entre control_task e comms_task.
#include "app/app_context.h"

#include <esp_timer.h>
#include <sys/time.h>

#include "config.h"

namespace semapp {

AppContext g_app;

namespace {
/// Guarda de mutex (RAII). Timeout curto: o controle nunca espera muito.
class Lock {
 public:
  explicit Lock(SemaphoreHandle_t m) : m_(m), ok_(m != nullptr && xSemaphoreTake(m, pdMS_TO_TICKS(5)) == pdTRUE) {}
  ~Lock() {
    if (ok_) xSemaphoreGive(m_);
  }
  bool ok() const { return ok_; }

 private:
  SemaphoreHandle_t m_;
  bool ok_;
};
}  // namespace

bool AppContext::begin() {
  mutex_ = xSemaphoreCreateMutex();
  q_events = xQueueCreate(semcfg::kEventQueueLen, sizeof(sem::CoreEvent));
  q_decisions = xQueueCreate(semcfg::kDecisionQueueLen, sizeof(DecisionSnapshot));
  q_telemetry = xQueueCreate(1, sizeof(TelemetrySnapshot));
  q_commands = xQueueCreate(semcfg::kCommandQueueLen, sizeof(CommandMessage));
  return mutex_ != nullptr && q_events != nullptr && q_decisions != nullptr && q_telemetry != nullptr &&
         q_commands != nullptr;
}

SharedStatus AppContext::status() const {
  Lock l(mutex_);
  return status_;  // em caso de timeout, cópia possivelmente desatualizada (somente leitura informativa)
}

void AppContext::set_mode(sem::EmsMode m) {
  Lock l(mutex_);
  if (l.ok()) status_.mode = m;
}
void AppContext::set_safety(sem::SafetyCode c) {
  Lock l(mutex_);
  if (l.ok()) status_.safety = c;
}
void AppContext::set_boot_id(uint32_t id) {
  Lock l(mutex_);
  if (l.ok()) status_.boot_id = id;
}
void AppContext::set_time_synced(bool v) {
  Lock l(mutex_);
  if (l.ok()) status_.time_synced = v;
}
void AppContext::set_mqtt_up(bool v) {
  Lock l(mutex_);
  if (l.ok()) status_.mqtt_up = v;
}

bool AppContext::post_event(const sem::CoreEvent& ev) { return xQueueSend(q_events, &ev, 0) == pdTRUE; }
bool AppContext::post_decision(const DecisionSnapshot& d) { return xQueueSend(q_decisions, &d, 0) == pdTRUE; }
void AppContext::post_telemetry(const TelemetrySnapshot& t) { xQueueOverwrite(q_telemetry, &t); }
bool AppContext::post_command(const CommandMessage& c) { return xQueueSend(q_commands, &c, 0) == pdTRUE; }
unsigned AppContext::command_slots() const { return static_cast<unsigned>(uxQueueSpacesAvailable(q_commands)); }

double monotonic_s() { return static_cast<double>(esp_timer_get_time()) / 1e6; }

int64_t epoch_ms_or_zero() {
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  if (static_cast<int64_t>(tv.tv_sec) < semcfg::kMinValidEpochS) return 0;
  return static_cast<int64_t>(tv.tv_sec) * 1000 + static_cast<int64_t>(tv.tv_usec) / 1000;
}

}  // namespace semapp
