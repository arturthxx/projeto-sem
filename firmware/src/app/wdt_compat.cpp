// Watchdog de tarefa com compatibilidade IDF 4 / IDF 5.
#include "app/wdt_compat.h"

#include <esp_idf_version.h>
#include <esp_task_wdt.h>

namespace semapp {

bool wdt_subscribe_current_task(uint32_t timeout_s) {
#if ESP_IDF_VERSION_MAJOR >= 5
  esp_task_wdt_config_t cfg = {};
  cfg.timeout_ms = timeout_s * 1000u;
  cfg.idle_core_mask = 0;  // não vigia as tarefas ociosas (Wi-Fi pode ocupar o núcleo 0)
  cfg.trigger_panic = true;
  esp_err_t err = esp_task_wdt_init(&cfg);
  if (err == ESP_ERR_INVALID_STATE) err = esp_task_wdt_reconfigure(&cfg);  // já inicializado pelo core
  if (err != ESP_OK) return false;
#else
  // IDF 4.x: se o TWDT já estiver ativo, init() apenas atualiza timeout/pânico.
  if (esp_task_wdt_init(timeout_s, true) != ESP_OK) return false;
#endif
  return esp_task_wdt_add(nullptr) == ESP_OK;
}

void wdt_feed() { esp_task_wdt_reset(); }

}  // namespace semapp
