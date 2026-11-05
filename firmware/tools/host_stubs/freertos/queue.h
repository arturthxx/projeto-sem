// STUB (host): filas FreeRTOS. No ESP-IDF várias são macros sobre xQueueGenericSend.
#pragma once
#include "freertos/FreeRTOS.h"
#ifdef __cplusplus
extern "C" {
#endif
typedef struct QueueDefinition* QueueHandle_t;
QueueHandle_t xQueueCreate(UBaseType_t uxQueueLength, UBaseType_t uxItemSize);
BaseType_t xQueueSend(QueueHandle_t xQueue, const void* pvItemToQueue, TickType_t xTicksToWait);
BaseType_t xQueueOverwrite(QueueHandle_t xQueue, const void* pvItemToQueue);
BaseType_t xQueueReceive(QueueHandle_t xQueue, void* const pvBuffer, TickType_t xTicksToWait);
UBaseType_t uxQueueSpacesAvailable(const QueueHandle_t xQueue);
#ifdef __cplusplus
}
#endif
