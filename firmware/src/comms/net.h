// =============================================================================
// src/comms/net.h — Wi-Fi (reconexão com backoff exponencial), SNTP
// (TZ POSIX "<-03>3") e MQTT (PubSubClient) com LWT retido em
// <prefixo>/system/status, buffer ampliado para a telemetria e reconexão NÃO
// bloqueante (tentativas espaçadas; cada tentativa limitada pelo timeout do
// socket). Roda SOMENTE na comms_task (núcleo 0): falhas de rede nunca
// atrasam o controle nem a segurança (núcleo 1).
//
// Limitação: PubSubClient publica apenas com QoS 0 (assina com QoS 1). O
// contrato pede QoS 1 para acks/eventos/decisões — ver firmware/README.md.
// =============================================================================
#pragma once

#include <PubSubClient.h>
#include <WiFi.h>

#include <cstdint>
#include <functional>

namespace semcomms {

class NetManager {
 public:
  struct Config {
    const char* ssid = "";
    const char* wifi_pass = "";
    const char* mqtt_host = "";
    uint16_t mqtt_port = 1883;
    const char* mqtt_user = "";
    const char* mqtt_pass = "";
    const char* client_id = "";
    const char* prefix = "smartenergy";
    const char* posix_tz = "<-03>3";
  };
  using Handler = std::function<void(const char* topic, const uint8_t* payload, unsigned int len)>;

  enum Transition : uint32_t { kWifiUp = 1, kWifiDown = 2, kMqttUp = 4, kMqttDown = 8, kTimeSync = 16 };

  NetManager() : mqtt_(wifi_client_) {}

  /// 'lwt_payload' é copiado (JSON de status "offline", retido).
  void begin(const Config& cfg, const char* lwt_payload, Handler on_message);

  /// Passo da máquina de estados (chamar a cada ~20 ms na comms_task).
  void loop(uint32_t now_ms);

  bool wifi_up() const { return wifi_up_; }
  bool mqtt_up() const { return mqtt_up_; }
  bool time_synced() const { return time_synced_; }

  /// Publica em <prefixo>/<suffix>. Retorna false se desconectado ou payload grande demais.
  bool publish(const char* suffix, const char* payload, bool retained = false);

  /// Retorna e zera as transições ocorridas desde a última chamada (bits Transition).
  uint32_t take_transitions() {
    const uint32_t t = transitions_;
    transitions_ = 0;
    return t;
  }

  /// Epoch UTC atual em ms, ou 0 se o relógio não for válido.
  int64_t now_epoch_ms() const;

  int rssi() const;

 private:
  bool connect_mqtt();
  void topic(const char* suffix, char* out, size_t cap) const;

  Config cfg_{};
  WiFiClient wifi_client_;
  PubSubClient mqtt_;
  Handler handler_;
  char lwt_[192] = {};
  bool wifi_up_ = false;
  bool mqtt_up_ = false;
  bool time_synced_ = false;
  bool sntp_started_ = false;
  bool wifi_tried_ = false;
  uint32_t wifi_backoff_ms_ = 0;
  uint32_t mqtt_backoff_ms_ = 0;
  uint32_t t_wifi_try_ms_ = 0;
  uint32_t t_mqtt_try_ms_ = 0;
  uint32_t transitions_ = 0;
};

}  // namespace semcomms
