// Wi-Fi + SNTP + MQTT com reconexão não bloqueante (máquina de estados).
#include "comms/net.h"

#include <Arduino.h>
#include <sys/time.h>
#include <time.h>

#include <cstdio>
#include <cstring>

#include "config.h"

namespace semcomms {

void NetManager::topic(const char* suffix, char* out, size_t cap) const {
  std::snprintf(out, cap, "%s/%s", cfg_.prefix, suffix);
}

void NetManager::begin(const Config& cfg, const char* lwt_payload, Handler on_message) {
  cfg_ = cfg;
  handler_ = std::move(on_message);
  std::snprintf(lwt_, sizeof(lwt_), "%s", lwt_payload != nullptr ? lwt_payload : "{\"status\":\"offline\"}");
  wifi_backoff_ms_ = semcfg::kWifiBackoffMinMs;
  mqtt_backoff_ms_ = semcfg::kMqttBackoffMinMs;

  WiFi.setHostname(cfg_.client_id);  // antes de mode()/begin() para valer no core 2.x
  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(false);  // a reconexão (com backoff) é feita aqui

  mqtt_.setServer(cfg_.mqtt_host, cfg_.mqtt_port);
  mqtt_.setBufferSize(semcfg::kMqttBufferSize);  // telemetria completa em um pacote
  mqtt_.setKeepAlive(semcfg::kMqttKeepAliveS);
  mqtt_.setSocketTimeout(semcfg::kMqttSocketTimeoutS);
  mqtt_.setCallback([this](char* t, uint8_t* p, unsigned int n) {
    if (handler_) handler_(t, p, n);
  });
}

bool NetManager::connect_mqtt() {
  char lwt_topic[64];
  topic("system/status", lwt_topic, sizeof(lwt_topic));
  const bool has_user = cfg_.mqtt_user != nullptr && cfg_.mqtt_user[0] != '\0';
  const bool ok = mqtt_.connect(cfg_.client_id, has_user ? cfg_.mqtt_user : nullptr, has_user ? cfg_.mqtt_pass : nullptr,
                                lwt_topic, 1, true, lwt_, true);
  if (!ok) return false;
  char cmd_topic[64];
  topic("cmd", cmd_topic, sizeof(cmd_topic));
  mqtt_.subscribe(cmd_topic, 1);
  return true;
}

void NetManager::loop(uint32_t now_ms) {
  // --- Wi-Fi
  const bool w = WiFi.status() == WL_CONNECTED;
  if (w != wifi_up_) {
    wifi_up_ = w;
    transitions_ |= w ? kWifiUp : kWifiDown;
    if (w) {
      wifi_backoff_ms_ = semcfg::kWifiBackoffMinMs;
      if (!sntp_started_) {
        configTzTime(cfg_.posix_tz, semcfg::kNtpServer1, semcfg::kNtpServer2);
        sntp_started_ = true;
      }
    }
  }
  if (!w) {
    if (mqtt_up_) {
      mqtt_up_ = false;
      transitions_ |= kMqttDown;
    }
    if (!wifi_tried_ || now_ms - t_wifi_try_ms_ >= wifi_backoff_ms_) {
      wifi_tried_ = true;
      t_wifi_try_ms_ = now_ms;
      WiFi.disconnect();
      WiFi.begin(cfg_.ssid, cfg_.wifi_pass);  // assíncrono
      wifi_backoff_ms_ = wifi_backoff_ms_ * 2 > semcfg::kWifiBackoffMaxMs ? semcfg::kWifiBackoffMaxMs : wifi_backoff_ms_ * 2;
    }
  } else if (!mqtt_.connected()) {
    // --- MQTT
    if (mqtt_up_) {
      mqtt_up_ = false;
      transitions_ |= kMqttDown;
    }
    if (now_ms - t_mqtt_try_ms_ >= mqtt_backoff_ms_) {
      t_mqtt_try_ms_ = now_ms;
      if (connect_mqtt()) {
        mqtt_up_ = true;
        transitions_ |= kMqttUp;
        mqtt_backoff_ms_ = semcfg::kMqttBackoffMinMs;
      } else {
        mqtt_backoff_ms_ = mqtt_backoff_ms_ * 2 > semcfg::kMqttBackoffMaxMs ? semcfg::kMqttBackoffMaxMs : mqtt_backoff_ms_ * 2;
      }
    }
  } else {
    mqtt_.loop();  // entrega mensagens recebidas ao handler (comandos)
  }

  // --- Hora (SNTP ou RTC ajustaram o relógio do sistema)
  if (!time_synced_ && static_cast<int64_t>(time(nullptr)) >= semcfg::kMinValidEpochS) {
    time_synced_ = true;
    transitions_ |= kTimeSync;
  }
}

bool NetManager::publish(const char* suffix, const char* payload, bool retained) {
  if (!mqtt_up_ || payload == nullptr) return false;
  char t[80];
  topic(suffix, t, sizeof(t));
  return mqtt_.publish(t, reinterpret_cast<const uint8_t*>(payload), static_cast<unsigned int>(std::strlen(payload)),
                       retained);
}

int64_t NetManager::now_epoch_ms() const {
  struct timeval tv;
  gettimeofday(&tv, nullptr);
  if (static_cast<int64_t>(tv.tv_sec) < semcfg::kMinValidEpochS) return 0;
  return static_cast<int64_t>(tv.tv_sec) * 1000 + static_cast<int64_t>(tv.tv_usec) / 1000;
}

int NetManager::rssi() const { return wifi_up_ ? static_cast<int>(WiFi.RSSI()) : 0; }

}  // namespace semcomms
