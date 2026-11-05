// STUB (host): Print (Arduino API).
#pragma once
#include <stddef.h>
#include <stdint.h>
class Print {
 public:
  virtual ~Print() {}
  virtual size_t write(uint8_t) = 0;
  virtual size_t write(const uint8_t* buffer, size_t size);
  size_t printf(const char* format, ...) __attribute__((format(printf, 2, 3)));
  size_t print(const char*);
  size_t println(const char*);
  size_t println(void);
};
