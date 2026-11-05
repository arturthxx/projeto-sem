// STUB (host): Preferences (NVS) do core Arduino-ESP32 2.0.x.
#pragma once
#include <math.h>
#include <stddef.h>
#include <stdint.h>
class Preferences {
 public:
  bool begin(const char* name, bool readOnly = false, const char* partition_label = nullptr);
  void end();
  bool clear();
  bool remove(const char* key);
  size_t putUInt(const char* key, uint32_t value);
  size_t putInt(const char* key, int32_t value);
  size_t putDouble(const char* key, double value);
  size_t putBytes(const char* key, const void* value, size_t len);
  bool isKey(const char* key);
  uint32_t getUInt(const char* key, uint32_t defaultValue = 0);
  int32_t getInt(const char* key, int32_t defaultValue = 0);
  double getDouble(const char* key, double defaultValue = NAN);
  size_t getBytesLength(const char* key);
  size_t getBytes(const char* key, void* buf, size_t maxLen);
};
