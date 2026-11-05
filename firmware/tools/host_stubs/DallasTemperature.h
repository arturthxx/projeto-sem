// STUB (host): DallasTemperature 3.11.x (subconjunto usado).
#pragma once
#include <stdint.h>
#include "OneWire.h"
#define DEVICE_DISCONNECTED_C -127
typedef uint8_t DeviceAddress[8];
class DallasTemperature {
 public:
  struct request_t {
    bool result;
    unsigned long timestamp;
    operator bool() { return result; }
  };
  DallasTemperature();
  explicit DallasTemperature(OneWire*);
  void begin(void);
  uint8_t getDeviceCount(void);
  bool getAddress(uint8_t*, uint8_t);
  void setResolution(uint8_t);
  bool setResolution(const uint8_t*, uint8_t, bool skipGlobalBitResolutionCalculation = false);
  void setWaitForConversion(bool);
  request_t requestTemperatures(void);
  bool isConversionComplete(void);
  float getTempC(const uint8_t*);
};
