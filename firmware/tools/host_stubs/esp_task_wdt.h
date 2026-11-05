// STUB (host): watchdog de tarefas — API do IDF 4.4 ou 5.x conforme esp_idf_version.h.
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "esp_idf_version.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#ifdef __cplusplus
extern "C" {
#endif
#if ESP_IDF_VERSION_MAJOR >= 5
typedef struct {
  uint32_t timeout_ms;
  uint32_t idle_core_mask;
  bool trigger_panic;
} esp_task_wdt_config_t;
esp_err_t esp_task_wdt_init(const esp_task_wdt_config_t* config);
esp_err_t esp_task_wdt_reconfigure(const esp_task_wdt_config_t* config);
#else
esp_err_t esp_task_wdt_init(uint32_t timeout, bool panic);
#endif
esp_err_t esp_task_wdt_add(TaskHandle_t task_handle);
esp_err_t esp_task_wdt_reset(void);
esp_err_t esp_task_wdt_delete(TaskHandle_t task_handle);
#ifdef __cplusplus
}
#endif
