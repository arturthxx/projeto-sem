// STUB (host): TwoWire do core Arduino-ESP32 2.0.x (conjunto de sobrecargas reais de requestFrom).
#pragma once
#include <stddef.h>
#include <stdint.h>
#include "Stream.h"
class TwoWire : public Stream {
 public:
  bool begin(int sda = -1, int scl = -1, uint32_t frequency = 0);
  bool setClock(uint32_t frequency);
  void setTimeOut(uint16_t timeOutMillis);
  void beginTransmission(uint16_t address);
  void beginTransmission(uint8_t address);
  void beginTransmission(int address);
  uint8_t endTransmission(bool sendStop);
  uint8_t endTransmission(void);
  size_t requestFrom(uint16_t address, size_t size, bool sendStop);
  uint8_t requestFrom(uint16_t address, uint8_t size, bool sendStop);
  uint8_t requestFrom(uint16_t address, uint8_t size, uint8_t sendStop);
  size_t requestFrom(uint8_t address, size_t len, bool stopBit);
  uint8_t requestFrom(uint16_t address, uint8_t size);
  uint8_t requestFrom(uint8_t address, uint8_t size, uint8_t sendStop);
  uint8_t requestFrom(uint8_t address, uint8_t size);
  uint8_t requestFrom(int address, int size, int sendStop);
  uint8_t requestFrom(int address, int size);
  size_t write(uint8_t) override;
  size_t write(const uint8_t*, size_t) override;
  int available(void) override;
  int read(void) override;
  int peek(void) override;
};
extern TwoWire Wire;
