// Tarefa de comunicação: rede, comandos autenticados e publicação MQTT.
#include "app/comms_task.h"

#include <Arduino.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include <cstdio>
#include <cstring>

#include "app/app_context.h"
#include "comms/net.h"
#include "comms/secrets_include.h"
#include "comms/telemetry.h"
#include "config.h"
#include "crypto/hmac_mbedtls.h"
#include "sem_core/sem_core.h"

namespace semapp {

namespace {

class CommsLoop {
 public:
  explicit CommsLoop(const sem::SystemParams& p)
      : p_(p),
        validator_(p.comm, p.loads, &hmac_, reinterpret_cast<const uint8_t*>(CMD_HMAC_KEY), std::strlen(CMD_HMAC_KEY)) {
    meta_.device_id = p.project.device_id;
    meta_.fw_version = p.project.firmware_version;
    meta_.source = semcfg::kSource;
  }

  void setup();
  void step();

 private:
  void on_message(const char* topic, const uint8_t* payload, unsigned int len);
  void handle_transitions(uint32_t tr);
  void publish_event(const sem::CoreEvent& ev);
  void publish_telemetry(TelemetrySnapshot* t);
  void publish_mode(int64_t now_ms);
  void local_event(sem::EventCode code, sem::EventLevel level, const char* msg);

  const sem::SystemParams& p_;
  semcrypto::MbedtlsHmacSha256 hmac_;
  sem::CommandValidator validator_;
  semcomms::NetManager net_;
  PayloadMeta meta_{};
  char buf_[semcfg::kJsonOutMax] = {};
  char topic_cmd_[64] = {};
  uint32_t seq_ = 0;
  bool mode_published_ = false;
  sem::EmsMode published_mode_ = sem::EmsMode::Rules;
  sem::EventList pending_;  // eventos de rede gerados enquanto o MQTT estava fora
  TelemetrySnapshot tel_{};
  DecisionSnapshot dec_{};
};

void CommsLoop::setup() {
  std::snprintf(topic_cmd_, sizeof(topic_cmd_), "%s/cmd", p_.comm.mqtt_prefix);
  const SharedStatus st = g_app.status();
  build_status_json(false, meta_, st.boot_id, 0, buf_, sizeof(buf_));  // LWT (retido)
  semcomms::NetManager::Config c;
  c.ssid = WIFI_SSID;
  c.wifi_pass = WIFI_PASS;
  c.mqtt_host = MQTT_HOST;
  c.mqtt_port = MQTT_PORT;
  c.mqtt_user = MQTT_USER;
  c.mqtt_pass = MQTT_PASS;
  c.client_id = p_.project.device_id;
  c.prefix = p_.comm.mqtt_prefix;
  c.posix_tz = p_.project.posix_tz;
  net_.begin(c, buf_, [this](const char* t, const uint8_t* pl, unsigned int n) { on_message(t, pl, n); });
}

void CommsLoop::local_event(sem::EventCode code, sem::EventLevel level, const char* msg) {
  pending_.push(code, level, "%s", msg).ts_ms = net_.now_epoch_ms();
}

void CommsLoop::on_message(const char* topic, const uint8_t* payload, unsigned int len) {
  if (std::strcmp(topic, topic_cmd_) != 0) return;
  const int64_t now_ms = net_.now_epoch_ms();
  sem::ValidationContext ctx;
  ctx.now_ms = now_ms;
  ctx.time_synced = now_ms > 0;
  ctx.mode = g_app.status().mode;
  sem::ValidationResult r = validator_.validate(reinterpret_cast<const char*>(payload), len, ctx);
  bool accepted = r.accepted;
  const char* reason = r.detail;
  if (accepted) {
    CommandMessage m;
    m.cmd = r.cmd;
    m.rx_ms = now_ms;
    m.time_synced = ctx.time_synced;
    if (!g_app.post_command(m)) {
      accepted = false;
      reason = "fila de comandos cheia";
    } else if (r.cmd.type == sem::CommandType::SetMode) {
      g_app.set_mode(r.cmd.mode);  // visão imediata para um set_loads logo em seguida
    }
  }
  build_ack_json(r.has_id ? r.cmd.id : nullptr, accepted, accepted ? "ok" : reason, now_ms, buf_, sizeof(buf_));
  net_.publish("cmd/ack", buf_);
  if (!accepted) {
    sem::EventList one;
    one.push(sem::EventCode::CmdRejected, sem::EventLevel::Warn, "Comando rejeitado (%s): %s",
             sem::to_string(r.reason), reason)
        .with_text("reason", sem::to_string(r.reason))
        .with_text("id", r.has_id ? r.cmd.id : "");
    sem::CoreEvent ev = one[0];
    ev.ts_ms = now_ms;
    publish_event(ev);
  }
}

void CommsLoop::publish_event(const sem::CoreEvent& ev) {
  if (build_event_json(ev, meta_, buf_, sizeof(buf_)) > 0) net_.publish("system/event", buf_);
  Serial.printf("[evento] %s %s: %s\n", sem::to_string(ev.level), sem::to_string(ev.code), ev.msg);
}

void CommsLoop::publish_mode(int64_t now_ms) {
  const sem::EmsMode m = g_app.status().mode;
  if (mode_published_ && m == published_mode_) return;
  if (build_scalar_text_json(sem::to_string(m), "ems", now_ms, meta_, buf_, sizeof(buf_)) > 0 &&
      net_.publish("ems/mode", buf_, true)) {
    mode_published_ = true;
    published_mode_ = m;
  }
}

void CommsLoop::publish_telemetry(TelemetrySnapshot* t) {
  t->seq = seq_++;
  if (build_telemetry_json(*t, meta_, buf_, sizeof(buf_)) > 0) {
    net_.publish("telemetry", buf_);
  } else {
    Serial.println("[telemetria] buffer insuficiente");
  }
  // Tópicos escalares de compatibilidade (docs/05 §4).
  const int64_t ts = t->time_synced ? t->ts_ms : 0;
  struct Scalar {
    const char* topic;
    const MeasAvg& m;
    double value;
    int dec;
    const char* unit;
    const char* point;
  };
  const Scalar scalars[] = {
      {"pv/voltage", t->pv, t->pv.v, 3, "V", "pv"},       {"pv/current", t->pv, t->pv.i, 3, "A", "pv"},
      {"pv/power", t->pv, t->pv.p, 2, "W", "pv"},         {"battery/voltage", t->bat, t->bat.v, 3, "V", "bat"},
      {"battery/current", t->bat, t->bat.i, 3, "A", "bat"},
  };
  for (const Scalar& s : scalars) {
    if (s.m.ok && build_scalar_json(s.value, s.dec, s.unit, s.point, ts, meta_, buf_, sizeof(buf_)) > 0) {
      net_.publish(s.topic, buf_);
    }
  }
  if (t->soc_has_value && build_scalar_json(t->soc, 4, "1", "bat", ts, meta_, buf_, sizeof(buf_)) > 0) {
    net_.publish("battery/soc", buf_);
  }
  for (int i = 0; i < t->n_loads && i < sem::kMaxLoads; ++i) {
    const LoadSnap& l = t->loads[i];
    char topic[32], point[8];
    std::snprintf(point, sizeof(point), "l%d", l.id);
    std::snprintf(topic, sizeof(topic), "load/%d/power", l.id);
    if (build_scalar_json(l.p, 2, "W", point, ts, meta_, buf_, sizeof(buf_)) > 0) net_.publish(topic, buf_);
    std::snprintf(topic, sizeof(topic), "load/%d/state", l.id);
    if (build_scalar_json(l.on ? 1.0 : 0.0, 0, "1", point, ts, meta_, buf_, sizeof(buf_)) > 0) net_.publish(topic, buf_);
  }
}

void CommsLoop::handle_transitions(uint32_t tr) {
  using N = semcomms::NetManager;
  if (tr & N::kWifiUp) local_event(sem::EventCode::WifiUp, sem::EventLevel::Info, "Wi-Fi conectado");
  if (tr & N::kWifiDown) local_event(sem::EventCode::WifiDown, sem::EventLevel::Warn, "Wi-Fi desconectado");
  if (tr & N::kMqttDown) {
    g_app.set_mqtt_up(false);
    local_event(sem::EventCode::MqttDown, sem::EventLevel::Warn, "MQTT desconectado");
  }
  if (tr & N::kTimeSync) {
    g_app.set_time_synced(true);
    local_event(sem::EventCode::TimeSync, sem::EventLevel::Info, "Relogio sincronizado");
  }
  if (tr & N::kMqttUp) {
    g_app.set_mqtt_up(true);
    const SharedStatus st = g_app.status();
    const int64_t now_ms = net_.now_epoch_ms();
    if (build_status_json(true, meta_, st.boot_id, now_ms, buf_, sizeof(buf_)) > 0) {
      net_.publish("system/status", buf_, true);
    }
    mode_published_ = false;  // republica o modo retido
    local_event(sem::EventCode::MqttUp, sem::EventLevel::Info, "MQTT conectado");
  }
}

void CommsLoop::step() {
  net_.loop(millis());
  const uint32_t tr = net_.take_transitions();
  if (tr != 0) handle_transitions(tr);

  if (!net_.mqtt_up()) {
    // Sem broker: descarta telemetria antiga (a fila é sobrescrita pelo controle);
    // eventos ficam na fila (o controle descarta os excedentes sem bloquear).
    xQueueReceive(g_app.q_telemetry, &tel_, 0);
    return;
  }
  const int64_t now_ms = net_.now_epoch_ms();
  publish_mode(now_ms);
  for (int k = 0; k < pending_.size(); ++k) publish_event(pending_[k]);
  pending_.clear();
  sem::CoreEvent ev;
  for (int k = 0; k < 8 && xQueueReceive(g_app.q_events, &ev, 0) == pdTRUE; ++k) publish_event(ev);
  for (int k = 0; k < 2 && xQueueReceive(g_app.q_decisions, &dec_, 0) == pdTRUE; ++k) {
    if (build_decision_json(dec_, meta_, buf_, sizeof(buf_)) > 0) net_.publish("ems/decision", buf_);
  }
  if (xQueueReceive(g_app.q_telemetry, &tel_, 0) == pdTRUE) publish_telemetry(&tel_);
}

void comms_task_fn(void* arg) {
  CommsLoop* loop = static_cast<CommsLoop*>(arg);
  loop->setup();
  for (;;) {
    loop->step();
    vTaskDelay(pdMS_TO_TICKS(semcfg::kCommsLoopMs));
  }
}

}  // namespace

bool start_comms_task(const sem::SystemParams& params) {
  static CommsLoop loop(params);
  return xTaskCreatePinnedToCore(comms_task_fn, "sem_comms", semcfg::kCommsTaskStack, &loop,
                                 semcfg::kCommsTaskPriority, nullptr, semcfg::kCommsTaskCore) == pdPASS;
}

}  // namespace semapp
