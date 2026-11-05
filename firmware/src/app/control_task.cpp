// Tarefa de controle: amostragem, SOC, segurança, EMS, saídas, energia e telemetria.
#include "app/control_task.h"

#include <Arduino.h>
#include <Wire.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <sys/time.h>

#include <cstring>

#include "app/app_context.h"
#include "app/snapshots.h"
#include "app/wdt_compat.h"
#include "config.h"
#include "hw/ina226.h"
#include "hw/mppt_uart_stub.h"
#include "hw/outputs.h"
#include "hw/panel.h"
#include "hw/rtc_ds3231.h"
#include "hw/temp_ds18b20.h"
#include "sem_core/sem_core.h"
#include "storage/nvs_store.h"

namespace semapp {

namespace {

volatile uint32_t g_alert_edges = 0;  // diagnóstico: bordas de descida do ALERT do INA226 'bus'
void IRAM_ATTR on_ina_alert() { g_alert_edges = g_alert_edges + 1; }

/// Acumulador de médias da janela de telemetria.
struct Acc {
  double v = 0.0, i = 0.0, p = 0.0;
  uint32_t n = 0;
  void add(const semhw::InaReading& r) {
    if (!r.ok) return;
    v += r.bus_v;
    i += r.current_a;
    p += r.power_w;
    ++n;
  }
  MeasAvg mean() const {
    MeasAvg m{};
    if (n == 0) return m;
    m.ok = true;
    m.v = v / n;
    m.i = i / n;
    m.p = p / n;
    return m;
  }
};

void add_counters(sem::EnergyCounters* a, const sem::EnergyCounters& b) {
  a->pv_in_wh += b.pv_in_wh;
  a->bat_chg_wh += b.bat_chg_wh;
  a->bat_dis_wh += b.bat_dis_wh;
  a->load_wh += b.load_wh;
  a->unserved_wh += b.unserved_wh;
  for (int i = 0; i < sem::kMaxLoads; ++i) a->load_wh_by[i] += b.load_wh_by[i];
}

class ControlLoop {
 public:
  explicit ControlLoop(const sem::SystemParams& p)
      : p_(p),
        ina_pv_(Wire, p.hardware.ina_pv),
        ina_bat_(Wire, p.hardware.ina_bat),
        ina_bus_(Wire, p.hardware.ina_bus),
        temps_(static_cast<uint8_t>(p.hardware.onewire_pin)),
        rtc_(Wire, p.hardware.rtc_ds3231_address),
        soc_(p.soc),
        persist_(p.soc),
        safety_(p.safety, true),
        ems_(p),
        loads_(p.loads),
        demand_(p.loads, p.demand) {}

  void setup();
  void step();

 private:
  void read_sensors(uint32_t now_ms);
  void apply_commands(double t_s);
  void run_ems(double t_s, int64_t now_ms, bool has_time);
  void update_energy(double dt, int64_t now_ms, bool has_time);
  void publish_telemetry(double t_s, int64_t now_ms, bool has_time);
  void persist(double t_s, bool force);
  void flush_events(int64_t now_ms);
  void sensor_edge(bool ok, bool* last, const char* name);
  semhw::LedPattern led_pattern() const;

  const sem::SystemParams& p_;
  semhw::Ina226 ina_pv_, ina_bat_, ina_bus_;
  semhw::TempSensors temps_;
  semhw::LoadOutputs outputs_;
  semhw::Panel panel_;
  semhw::RtcDs3231 rtc_;
  semhw::MpptUartStub mppt_;
  semstore::NvsStore nvs_;
  sem::SocEstimator soc_;
  sem::SocPersistPolicy persist_;
  sem::SafetySupervisor safety_;
  sem::EmsController ems_;
  sem::LoadManager loads_;
  sem::EnergyIntegrator energy_;
  sem::DemandProfile demand_;
  sem::EventList events_;

  semhw::InaReading pv_{}, bat_{}, bus_{};
  semhw::TempReading t_bat_{}, t_pv_{};
  bool alert_ = false;
  bool ok_pv_ = true, ok_bat_ = true, ok_bus_ = true, ok_tbat_ = true;  // para eventos de borda
  sem::SafetyStatus safety_status_{};
  sem::Decision decision_{};
  bool has_decision_ = false;
  bool demand_now_[sem::kMaxLoads] = {};
  double t_prev_s_ = -1.0;
  double t_last_ems_s_ = -1e9;
  double t_window_start_s_ = 0.0;
  double t_last_telemetry_s_ = 0.0;
  Acc acc_pv_, acc_bat_, acc_bus_;
  double acc_load_p_[sem::kMaxLoads] = {};
  uint32_t n_samples_ = 0;
  uint32_t boot_id_ = 0;
  uint32_t faults_ = 0;
  bool force_persist_ = false;
  semstore::PersistedState persisted_{};
  bool energy_restore_pending_ = false;
  bool rtc_written_ = false;
  sem::EmsMode last_mode_ = sem::EmsMode::Rules;
  sem::SafetyCode last_code_ = sem::SafetyCode::Boot;
  TelemetrySnapshot tel_{};
  DecisionSnapshot dec_snap_{};
};

void ControlLoop::setup() {
  // Saídas já estão em LOW (main.cpp). Configura PWM/digitais mantendo tudo desligado.
  outputs_.begin(p_.loads);
  panel_.begin(p_.hardware.status_led_pin, p_.hardware.button_pin);
  pinMode(static_cast<uint8_t>(p_.hardware.ina_alert_pin), INPUT);  // GPIO 34: só entrada, pull-up externo
  attachInterrupt(digitalPinToInterrupt(p_.hardware.ina_alert_pin), on_ina_alert, FALLING);
  mppt_.begin(p_.hardware.uart_mppt_rx, p_.hardware.uart_mppt_tx);  // stub: UART2 reservada

  Wire.begin(p_.hardware.i2c_sda, p_.hardware.i2c_scl, p_.hardware.i2c_hz);
  Wire.setTimeOut(semcfg::kI2cTimeoutMs);
  ina_pv_.begin();
  ina_bat_.begin();
  ina_bus_.begin();
  ina_bus_.configure_overcurrent_alert(p_.safety.bus_overcurrent_a);
  temps_.begin(semcfg::kTempResolutionBits);

  // Hora pelo RTC (opcional) enquanto o SNTP não sincroniza.
  int64_t rtc_epoch = 0;
  if (rtc_.begin() && rtc_.read_epoch(&rtc_epoch) && rtc_epoch >= semcfg::kMinValidEpochS &&
      epoch_ms_or_zero() == 0) {
    struct timeval tv;
    tv.tv_sec = static_cast<time_t>(rtc_epoch);
    tv.tv_usec = 0;
    settimeofday(&tv, nullptr);
  }

  // Persistência: boot_id, SOC e contadores.
  if (nvs_.begin()) {
    boot_id_ = nvs_.increment_boot_id();
    nvs_.load(&persisted_);
    if (persisted_.has_soc) soc_.init_persisted(persisted_.soc, persisted_.unc);
    if (persisted_.has_energy) {
      energy_restore_pending_ = true;
      for (int i = 0; i < p_.loads.count; ++i) loads_.set_switch_count(i, persisted_.n_switch[i]);
    }
  } else {
    faults_ |= kFaultNvs;
  }
  g_app.set_boot_id(boot_id_);
  g_app.set_mode(ems_.mode());
  last_mode_ = ems_.mode();

  decision_.n_loads = p_.loads.count;  // até a primeira avaliação: tudo desligado
  for (int i = 0; i < p_.loads.count; ++i) dec_snap_.ids[i] = p_.loads.items[i].id;
  events_.push(sem::EventCode::Boot, sem::EventLevel::Info, "Inicializacao: boot_id=%u fw=%s",
               static_cast<unsigned>(boot_id_), semcfg::kFwVersion)
      .with_number("boot_id", boot_id_);
  t_window_start_s_ = monotonic_s();
  t_last_telemetry_s_ = t_window_start_s_;
}

void ControlLoop::sensor_edge(bool ok, bool* last, const char* name) {
  if (ok == *last) return;
  *last = ok;
  if (ok) {
    events_.push(sem::EventCode::SensorOk, sem::EventLevel::Info, "Sensor %s restabelecido", name).with_text("sensor", name);
  } else {
    events_.push(sem::EventCode::SensorFail, sem::EventLevel::Error, "Falha no sensor %s", name).with_text("sensor", name);
  }
}

void ControlLoop::read_sensors(uint32_t now_ms) {
  alert_ = digitalRead(static_cast<uint8_t>(p_.hardware.ina_alert_pin)) == LOW;  // antes do I2C
  ina_pv_.maintain(now_ms);
  ina_bat_.maintain(now_ms);
  ina_bus_.maintain(now_ms);
  ina_pv_.read(now_ms, &pv_);
  ina_bat_.read(now_ms, &bat_);
  ina_bus_.read(now_ms, &bus_);
  temps_.poll(now_ms);
  t_bat_ = temps_.bat();
  t_pv_ = temps_.pv();
  sensor_edge(pv_.ok, &ok_pv_, "pv");
  sensor_edge(bat_.ok, &ok_bat_, "bat");
  sensor_edge(bus_.ok, &ok_bus_, "bus");
  sensor_edge(t_bat_.ok, &ok_tbat_, "t_bat");
  faults_ &= ~(kFaultInaPv | kFaultInaBat | kFaultInaBus | kFaultTempBat | kFaultTempPv);
  if (!pv_.ok) faults_ |= kFaultInaPv;
  if (!bat_.ok) faults_ |= kFaultInaBat;
  if (!bus_.ok) faults_ |= kFaultInaBus;
  if (!t_bat_.ok) faults_ |= kFaultTempBat;
  if (!t_pv_.ok) faults_ |= kFaultTempPv;
}

void ControlLoop::apply_commands(double t_s) {
  CommandMessage msg;
  while (xQueueReceive(g_app.q_commands, &msg, 0) == pdTRUE) {
    const sem::ParsedCommand& c = msg.cmd;
    switch (c.type) {
      case sem::CommandType::SetMode:
        ems_.set_mode(c.mode, &events_);
        break;
      case sem::CommandType::SetLoads: {
        if (ems_.mode() != sem::EmsMode::Predictive) break;  // validador já exige; dupla checagem
        sem::LoadPlan plan{};
        for (int k = 0; k < c.n_loads; ++k) {
          const sem::LoadCmdArg& a = c.loads[k];
          if (a.index < 0 || a.index >= sem::kMaxLoads) continue;
          plan.present[a.index] = true;
          plan.cmd[a.index].on = a.on;
          plan.cmd[a.index].duty = a.duty;
        }
        const double expires = sem::plan_expiry_t_s(c.ts_ms, c.ttl_s, msg.time_synced, msg.rx_ms, t_s);
        const double issued =
            (msg.time_synced && c.ts_ms > 0) ? t_s - static_cast<double>(msg.rx_ms - c.ts_ms) / 1000.0 : t_s;
        ems_.accept_plan(plan, issued, expires);
        break;
      }
      case sem::CommandType::SyncSoc:
        soc_.sync_manual(c.soc);
        force_persist_ = true;
        break;
      case sem::CommandType::ResetFaults:
        safety_.reset_faults();  // FAULTS_RESET sai na próxima amostra
        break;
    }
  }
}

void ControlLoop::run_ems(double t_s, int64_t now_ms, bool has_time) {
  sem::EmsInputs in{};
  in.t_s = t_s;
  in.ts_ms = now_ms;
  in.tod_s = has_time ? sem::tod_from_epoch_ms(now_ms, p_.project.utc_offset_s) : 0.0;
  in.soc = soc_.soc();
  in.soc_valid = soc_.initialized() && soc_.valid();
  in.soc_unc = soc_.unc();
  in.v_bat = bat_.ok ? bat_.bus_v : 0.0;
  in.i_bat = bat_.ok ? bat_.current_a : 0.0;
  in.p_bat = bat_.ok ? bat_.power_w : 0.0;
  in.p_pv = pv_.ok ? pv_.power_w : 0.0;
  in.p_bus = bus_.ok ? bus_.power_w : 0.0;
  // Sem hora válida o perfil programado não pode ser avaliado: nenhuma demanda
  // (decisão conservadora, sinalizada em 'faults' como NO_TIME).
  if (has_time) {
    demand_.fill(in.tod_s, demand_now_);
  } else {
    for (bool& d : demand_now_) d = false;
  }
  for (int i = 0; i < p_.loads.count; ++i) {
    in.demand[i] = demand_now_[i];
    in.e_done_wh[i] = energy_.today().load_wh_by[i];
    in.outputs[i] = loads_.output(i);
  }
  if (!has_time) {
    // Fora de qualquer janela da carga deslocável: tod fictício não é usado.
    for (int i = 0; i < p_.loads.count; ++i) {
      if (p_.loads.items[i].deferrable.enabled) in.e_done_wh[i] = p_.loads.items[i].deferrable.daily_energy_wh;
    }
  }
  const sem::Decision d = ems_.evaluate(in, &events_);
  const bool changed = !has_decision_ || sem::decision_differs(d, decision_);
  decision_ = d;
  has_decision_ = true;
  if (changed) {
    dec_snap_.ts_ms = now_ms;
    dec_snap_.decision = d;
    dec_snap_.safety = safety_status_.code;
    dec_snap_.soc = in.soc;
    dec_snap_.soc_valid = in.soc_valid;
    dec_snap_.v_bat = in.v_bat;
    dec_snap_.p_pv = in.p_pv;
    dec_snap_.p_bat = in.p_bat;
    dec_snap_.p_bus = in.p_bus;
    g_app.post_decision(dec_snap_);
  }
}

void ControlLoop::update_energy(double dt, int64_t now_ms, bool has_time) {
  if (has_time) {
    const int32_t day = sem::local_day_index(now_ms, p_.project.utc_offset_s);
    if (energy_restore_pending_) {
      energy_restore_pending_ = false;
      if (persisted_.has_energy && persisted_.day_index == day) {
        sem::EnergyCounters merged = persisted_.energy;
        add_counters(&merged, energy_.today());  // + o que foi integrado antes da hora válida
        energy_.restore(day, merged);
      }
    }
    if (energy_.roll_day(day)) force_persist_ = true;  // meia-noite local
  }
  const double v_ref = bus_.ok ? bus_.bus_v : (bat_.ok ? bat_.bus_v : 0.0);
  sem::PowerSample s{};
  s.p_pv = pv_.ok ? pv_.power_w : 0.0;
  s.p_bat = bat_.ok ? bat_.power_w : 0.0;
  s.p_bus = bus_.ok ? bus_.power_w : 0.0;
  for (int i = 0; i < p_.loads.count; ++i) {
    const sem::LoadOutput& o = loads_.output(i);
    s.p_load[i] = o.on ? sem::estimate_load_power_w(p_.loads.items[i], v_ref, o.duty) : 0.0;
    acc_load_p_[i] += s.p_load[i];
  }
  s.p_unserved = sem::unserved_power_w(p_.loads, demand_now_, loads_.outputs(), v_ref);
  energy_.add(dt, s);
}

void ControlLoop::persist(double t_s, bool force) {
  if (!nvs_.ok() || !soc_.initialized() || !soc_.valid()) return;
  if (!force && !persist_.should_persist(t_s, soc_.soc())) return;
  bool ok = nvs_.save_soc(soc_.soc(), soc_.unc());
  if (energy_.has_day()) {
    uint32_t n_sw[sem::kMaxLoads] = {};
    for (int i = 0; i < p_.loads.count; ++i) n_sw[i] = loads_.switch_count(i);
    ok = nvs_.save_energy(energy_.day_index(), energy_.today(), n_sw, p_.loads.count) && ok;
  }
  if (ok) {
    persist_.mark_persisted(t_s, soc_.soc());
    faults_ &= ~kFaultNvs;
  } else {
    faults_ |= kFaultNvs;
  }
}

void ControlLoop::publish_telemetry(double t_s, int64_t now_ms, bool has_time) {
  TelemetrySnapshot& s = tel_;
  s = TelemetrySnapshot{};
  s.boot_id = boot_id_;
  s.ts_ms = now_ms;
  s.time_synced = has_time;
  s.uptime_ms = static_cast<int64_t>(t_s * 1000.0);
  s.window_ms = static_cast<uint32_t>((t_s - t_window_start_s_) * 1000.0);
  s.n_samples = n_samples_;
  s.pv = acc_pv_.mean();
  s.bat = acc_bat_.mean();
  s.bus = acc_bus_.mean();
  s.t_pv.ok = t_pv_.ok;
  s.t_pv.t = t_pv_.t_c;
  s.t_bat.ok = t_bat_.ok;
  s.t_bat.t = t_bat_.t_c;
  const sem::EnergyCounters& e = energy_.today();
  s.has_day = has_time && energy_.has_day();
  if (s.has_day) sem::format_local_day(now_ms, p_.project.utc_offset_s, s.day, sizeof(s.day));
  s.e_pv_in_wh = e.pv_in_wh;
  s.e_bat_chg_wh = e.bat_chg_wh;
  s.e_bat_dis_wh = e.bat_dis_wh;
  s.e_load_wh = e.load_wh;
  s.e_unserved_wh = e.unserved_wh;
  s.soc_has_value = soc_.initialized();
  s.soc = soc_.soc();
  s.soc_unc = soc_.unc();
  s.soc_valid = soc_.initialized() && soc_.valid();
  s.last_sync = soc_.last_sync();
  s.since_sync_s = soc_.since_sync_s();
  s.n_loads = p_.loads.count;
  const double tod = has_time ? sem::tod_from_epoch_ms(now_ms, p_.project.utc_offset_s) : 0.0;
  for (int i = 0; i < p_.loads.count && i < sem::kMaxLoads; ++i) {
    const sem::LoadParams& L = p_.loads.items[i];
    LoadSnap& l = s.loads[i];
    l.id = L.id;
    sem::copy_cstr(l.key, L.key);
    l.prio = L.priority;
    l.on = loads_.output(i).on;
    l.duty = loads_.output(i).duty;
    l.p = n_samples_ > 0 ? acc_load_p_[i] / n_samples_ : 0.0;
    l.fault = false;  // sem sensor dedicado por carga
    if (L.deferrable.enabled) {
      l.deferrable = true;
      l.e_req_wh = L.deferrable.daily_energy_wh;
      l.e_done_wh = e.load_wh_by[i];
      l.window_start_s = L.deferrable.window_start_s;
      l.deadline_s = L.deferrable.deadline_s;
      l.demand = has_time && demand_.deferrable_pending(i, tod, e.load_wh_by[i]);
    } else {
      l.demand = demand_now_[i];
    }
  }
  s.mode = decision_.mode;
  s.state = decision_.state;
  s.safety = safety_status_.code;
  s.fallback = decision_.fallback;
  const double age = ems_.cmd_age_s(t_s);
  s.has_cmd_age = age >= 0.0;
  s.cmd_age_s = age;
  s.faults = faults_;
  g_app.post_telemetry(s);

  acc_pv_ = Acc{};
  acc_bat_ = Acc{};
  acc_bus_ = Acc{};
  for (double& a : acc_load_p_) a = 0.0;
  n_samples_ = 0;
  t_window_start_s_ = t_s;
}

void ControlLoop::flush_events(int64_t now_ms) {
  for (int k = 0; k < events_.size(); ++k) {
    sem::CoreEvent ev = events_[k];
    ev.ts_ms = now_ms;
    g_app.post_event(ev);  // descarta se a fila estiver cheia (comunicação parada)
  }
  events_.clear();
}

semhw::LedPattern ControlLoop::led_pattern() const {
  switch (safety_status_.code) {
    case sem::SafetyCode::Boot:
      return semhw::LedPattern::Boot;
    case sem::SafetyCode::OcLockout:
      return semhw::LedPattern::Lockout;
    case sem::SafetyCode::Ok:
      return g_app.status().mqtt_up ? semhw::LedPattern::Ok : semhw::LedPattern::NoComms;
    default:
      return semhw::LedPattern::Fault;
  }
}

void ControlLoop::step() {
  const double t_s = monotonic_s();
  const uint32_t now_ms32 = millis();
  const double dt = t_prev_s_ < 0.0 ? p_.comm.sample_period_ms / 1000.0 : t_s - t_prev_s_;
  t_prev_s_ = t_s;
  const int64_t now_ms = epoch_ms_or_zero();
  const bool has_time = now_ms > 0;
  if (has_time) {
    faults_ &= ~kFaultNoTime;
  } else {
    faults_ |= kFaultNoTime;
  }

  // 1) Sensores e botão.
  read_sensors(now_ms32);
  if (panel_.poll_reset_button(now_ms32)) safety_.reset_faults();

  // 2) Comandos validados vindos da comms_task.
  apply_commands(t_s);

  // 3) Estimador de SOC (inicializa pela OCV na primeira tensão válida, se não houver valor persistido).
  if (!soc_.initialized() && bat_.ok) soc_.init_from_voltage(bat_.bus_v);
  if (soc_.initialized()) soc_.update(dt, bat_.current_a, bat_.bus_v, bat_.ok, &events_);
  const bool soc_valid = soc_.initialized() && soc_.valid();
  if (soc_valid) {
    faults_ &= ~kFaultSocInvalid;
  } else {
    faults_ |= kFaultSocInvalid;
  }

  // 4) Supervisor de segurança — TODA amostra, todos os modos.
  sem::SafetyInputs si{};
  si.t_s = t_s;
  si.dt_s = dt;
  si.v_bat_ok = bat_.ok;
  si.v_bat = bat_.bus_v;
  si.soc = soc_.soc();
  si.soc_valid = soc_valid;
  si.v_bus_ok = bus_.ok;
  si.v_bus = bus_.bus_v;
  si.i_bus_ok = bus_.ok;
  si.i_bus = bus_.current_a;
  si.t_bat_ok = t_bat_.ok;
  si.t_bat = t_bat_.t_c;
  si.oc_alert = alert_;
  safety_status_ = safety_.update(si, &events_);

  // 5) EMS a cada ems_period_ms.
  if (t_s - t_last_ems_s_ >= p_.comm.ems_period_ms / 1000.0 - 1e-3) {
    t_last_ems_s_ = t_s;
    run_ems(t_s, now_ms, has_time);
  }

  // 6) Saídas: on_final = on_EMS AND allowed (aplicado a cada amostra).
  const uint32_t changed = loads_.apply(decision_, safety_status_.allowed, t_s);
  for (int i = 0; i < p_.loads.count; ++i) {
    if (changed & (1u << i)) outputs_.write(i, loads_.output(i).on, loads_.output(i).duty);
  }

  // 7) Energia, janela de telemetria.
  update_energy(dt, now_ms, has_time);
  acc_pv_.add(pv_);
  acc_bat_.add(bat_);
  acc_bus_.add(bus_);
  ++n_samples_;
  if (t_s - t_last_telemetry_s_ >= p_.comm.telemetry_period_s - 1e-3) {
    t_last_telemetry_s_ = t_s;
    publish_telemetry(t_s, now_ms, has_time);
  }

  // 8) Persistência (regra do §1) e RTC.
  persist(t_s, force_persist_);
  force_persist_ = false;
  if (has_time && rtc_.present() && !rtc_written_ && g_app.status().time_synced) {
    rtc_written_ = rtc_.write_epoch(now_ms / 1000);  // SNTP -> RTC (uma vez por boot)
  }

  // 9) Estado compartilhado, eventos e LED.
  if (ems_.mode() != last_mode_) {
    last_mode_ = ems_.mode();
    g_app.set_mode(last_mode_);
  }
  if (safety_status_.code != last_code_) {
    last_code_ = safety_status_.code;
    g_app.set_safety(last_code_);
  }
  flush_events(now_ms);
  panel_.update_led(now_ms32, led_pattern());
}

void control_task_fn(void* arg) {
  ControlLoop* loop = static_cast<ControlLoop*>(arg);
  wdt_subscribe_current_task(semcfg::kTaskWdtTimeoutS);
  TickType_t last_wake = xTaskGetTickCount();
  const TickType_t period = pdMS_TO_TICKS(sem::sysp::kSamplePeriodMs);
  for (;;) {
    vTaskDelayUntil(&last_wake, period);
    wdt_feed();
    loop->step();
  }
}

}  // namespace

bool start_control_task(const sem::SystemParams& params) {
  static ControlLoop loop(params);  // armazenamento estático (objeto grande, fora da pilha)
  loop.setup();
  return xTaskCreatePinnedToCore(control_task_fn, "sem_control", semcfg::kControlTaskStack, &loop,
                                 semcfg::kControlTaskPriority, nullptr, semcfg::kControlTaskCore) == pdPASS;
}

}  // namespace semapp
