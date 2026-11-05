// STUB (host): WiFi do core Arduino-ESP32 2.0.x.
#pragma once
#include <stdint.h>
#include "Client.h"
#include "IPAddress.h"
typedef enum { WIFI_MODE_NULL = 0, WIFI_MODE_STA, WIFI_MODE_AP, WIFI_MODE_APSTA } wifi_mode_t;
#define WIFI_OFF WIFI_MODE_NULL
#define WIFI_STA WIFI_MODE_STA
typedef enum {
  WL_NO_SHIELD = 255,
  WL_IDLE_STATUS = 0,
  WL_NO_SSID_AVAIL = 1,
  WL_SCAN_COMPLETED = 2,
  WL_CONNECTED = 3,
  WL_CONNECT_FAILED = 4,
  WL_CONNECTION_LOST = 5,
  WL_DISCONNECTED = 6
} wl_status_t;
class WiFiClass {
 public:
  bool mode(wifi_mode_t);
  wl_status_t begin(const char* ssid, const char* passphrase = nullptr, int32_t channel = 0,
                    const uint8_t* bssid = nullptr, bool connect = true);
  bool disconnect(bool wifioff = false, bool eraseap = false);
  wl_status_t status();
  bool setAutoReconnect(bool autoReconnect);
  bool setHostname(const char* hostname);
  IPAddress localIP();
  int8_t RSSI();
};
extern WiFiClass WiFi;
class WiFiClient : public Client {
 public:
  WiFiClient();
  int connect(IPAddress ip, uint16_t port) override;
  int connect(const char* host, uint16_t port) override;
  size_t write(uint8_t) override;
  size_t write(const uint8_t* buf, size_t size) override;
  int available() override;
  int read() override;
  int read(uint8_t* buf, size_t size) override;
  int peek() override;
  void flush() override;
  void stop() override;
  uint8_t connected() override;
  operator bool() override;
};
