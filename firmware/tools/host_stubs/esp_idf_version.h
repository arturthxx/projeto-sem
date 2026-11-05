// STUB (host): core 2.x => ESP-IDF 4.4; core 3.x => ESP-IDF 5.1.
#pragma once
#include "esp_arduino_version.h"
#if ESP_ARDUINO_VERSION_MAJOR >= 3
#define ESP_IDF_VERSION_MAJOR 5
#define ESP_IDF_VERSION_MINOR 1
#else
#define ESP_IDF_VERSION_MAJOR 4
#define ESP_IDF_VERSION_MINOR 4
#endif
#define ESP_IDF_VERSION_PATCH 0
