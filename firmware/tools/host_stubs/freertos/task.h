// STUB (host): tarefas FreeRTOS (assinaturas do ESP-IDF).
#pragma once
#include <stdint.h>
#include "freertos/FreeRTOS.h"
#ifdef __cplusplus
extern "C" {
#endif
typedef void* TaskHandle_t;
typedef void (*TaskFunction_t)(void*);
BaseType_t xTaskCreatePinnedToCore(TaskFunction_t pvTaskCode, const char* const pcName, const uint32_t usStackDepth,
                                   void* const pvParameters, UBaseType_t uxPriority, TaskHandle_t* const pvCreatedTask,
                                   const BaseType_t xCoreID);
void vTaskDelay(const TickType_t xTicksToDelay);
void vTaskDelayUntil(TickType_t* const pxPreviousWakeTime, const TickType_t xTimeIncrement);
TickType_t xTaskGetTickCount(void);
void vTaskDelete(TaskHandle_t xTaskToDelete);
#ifdef __cplusplus
}
#endif
