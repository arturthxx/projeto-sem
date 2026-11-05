// STUB (host): subconjunto do core Arduino-ESP32 2.0.x (3.x com STUB_ARDUINO_MAJOR=3).
#pragma once
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "Print.h"
#include "Stream.h"
#include "esp_arduino_version.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

typedef bool boolean;
typedef uint8_t byte;

#define LOW 0x0
#define HIGH 0x1
#define INPUT 0x01
#define OUTPUT 0x03
#define INPUT_PULLUP 0x05
#define RISING 0x01
#define FALLING 0x02
#define CHANGE 0x03
#define IRAM_ATTR
#define digitalPinToInterrupt(p) ((((uint8_t)(p)) < 40) ? (p) : -1)
#define pgm_read_byte_near(addr) (*(const unsigned char*)(addr))
#define pgm_read_byte(addr) (*(const unsigned char*)(addr))

unsigned long millis();
unsigned long micros();
void delay(uint32_t);
void yield(void);
void pinMode(uint8_t pin, uint8_t mode);
void digitalWrite(uint8_t pin, uint8_t val);
int digitalRead(uint8_t pin);
void attachInterrupt(uint8_t pin, void (*)(void), int mode);

#if ESP_ARDUINO_VERSION_MAJOR >= 3
bool ledcAttach(uint8_t pin, uint32_t freq, uint8_t resolution);
bool ledcWrite(uint8_t pin, uint32_t duty);
#else
uint32_t ledcSetup(uint8_t channel, uint32_t freq, uint8_t resolution_bits);
void ledcAttachPin(uint8_t pin, uint8_t channel);
void ledcWrite(uint8_t channel, uint32_t duty);
#endif

void configTzTime(const char* tz, const char* server1, const char* server2 = nullptr, const char* server3 = nullptr);
bool getLocalTime(struct tm* info, uint32_t ms = 5000);

#define SERIAL_8N1 0x800001c
class HardwareSerial : public Stream {
 public:
  void begin(unsigned long baud, uint32_t config = SERIAL_8N1, int8_t rxPin = -1, int8_t txPin = -1,
             bool invert = false, unsigned long timeout_ms = 20000UL, uint8_t rxfifo_full_thrhd = 112);
  int available() override;
  int read() override;
  int peek() override;
  size_t write(uint8_t) override;
  using Print::write;
};
extern HardwareSerial Serial;

class EspClass {
 public:
  void restart();
};
extern EspClass ESP;
