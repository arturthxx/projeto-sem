// =============================================================================
// tools/host_payload_check.cpp — programa de PC (não vai para o ESP32) que:
//   1. verifica em tempo de compilação as conversões puras do INA226;
//   2. monta os payloads do contrato com src/comms/telemetry.cpp a partir de
//      instantâneos de EXEMPLO (valores fictícios, apenas para validar o
//      FORMATO) e grava os JSON em <dir>, para validação contra schemas/
//      por tools/host_compile_check.sh (jsonschema no venv do backend).
// Uso: host_payload_check <dir_saida>
// =============================================================================
#include <cstdio>
#include <cstring>
#include <string>

#include "app/snapshots.h"
#include "comms/telemetry.h"
#include "config.h"
#include "hw/ina226_regs.h"
#include "system_params.h"

namespace ina = semhw::ina226;

// --- 1. Verificações estáticas do INA226 (datasheet SBOS547) -----------------------
static_assert(ina::kDefaultConfig == 0x4527, "config: AVG=16, VBUSCT=VSHCT=1,1 ms, contínuo");
static_assert(ina::calibration_value(ina::current_lsb_for(5.0), 0.010) == 3355, "CAL pv/bus (5 A, 10 mOhm)");
static_assert(ina::calibration_value(ina::current_lsb_for(8.0), 0.010) == 2097, "CAL bat (8 A, 10 mOhm)");
static_assert(ina::shunt_alert_limit(4.0, 0.010) == 16000, "limite SOL de 4 A em 10 mOhm = 40 mV / 2,5 uV");
static_assert(ina::shunt_volts(0xFFFF) == -2.5e-6, "tensão de shunt com sinal");
static_assert(ina::shunt_volts(0x7FFF) > 0.0819 && ina::shunt_volts(0x7FFF) < 0.0820, "fundo de escala +81,9 mV");
static_assert(ina::bus_volts(10240) > 12.7999 && ina::bus_volts(10240) < 12.8001, "1,25 mV/LSB");

namespace {

bool write_file(const std::string& path, const char* text, size_t n) {
  FILE* f = std::fopen(path.c_str(), "wb");
  if (f == nullptr) return false;
  const bool ok = std::fwrite(text, 1, n, f) == n;
  std::fclose(f);
  return ok;
}

}  // namespace

int main(int argc, char** argv) {
  if (argc < 2) {
    std::fprintf(stderr, "uso: %s <dir_saida>\n", argv[0]);
    return 2;
  }
  const std::string dir = argv[1];
  const sem::SystemParams p = sem::make_system_params();
  semapp::PayloadMeta meta;
  meta.device_id = p.project.device_id;
  meta.fw_version = p.project.firmware_version;
  meta.source = semcfg::kSource;
  static char buf[semcfg::kJsonOutMax];
  int failures = 0;
  auto emit = [&](const char* name, size_t n) {
    if (n == 0) {
      std::fprintf(stderr, "ERRO: %s não coube em %zu bytes\n", name, sizeof(buf));
      ++failures;
      return;
    }
    std::printf("%-22s %5zu bytes\n", name, n);
    if (!write_file(dir + "/" + name, buf, n)) ++failures;
  };

  // --- Telemetria "pior caso" (todos os campos presentes, valores longos) ---------
  semapp::TelemetrySnapshot t{};
  t.boot_id = 4294967295u;
  t.seq = 4294967295u;
  t.ts_ms = 1794798000123LL;
  t.time_synced = true;
  t.uptime_ms = 8640000000LL;
  t.window_ms = 5000;
  t.n_samples = 50;
  t.pv = {true, 18.2134, 2.7123, 49.3512};
  t.bat = {true, 13.3101, -1.2003, -15.9762};
  t.bus = {true, 13.3012, 1.4801, 19.6834};
  t.t_pv = {true, 45.21};
  t.t_bat = {true, 28.13};
  t.has_day = true;
  sem::format_local_day(t.ts_ms, p.project.utc_offset_s, t.day, sizeof(t.day));
  t.e_pv_in_wh = 1234.5678;
  t.e_bat_chg_wh = 600.123;
  t.e_bat_dis_wh = 200.321;
  t.e_load_wh = 800.222;
  t.e_unserved_wh = 32.111;
  t.soc_has_value = true;
  t.soc = 0.73412;
  t.soc_unc = 0.05213;
  t.soc_valid = true;
  t.last_sync = sem::SyncKind::Full;
  t.since_sync_s = 36000.5;
  t.n_loads = p.loads.count;
  for (int i = 0; i < p.loads.count; ++i) {
    const sem::LoadParams& L = p.loads.items[i];
    semapp::LoadSnap& l = t.loads[i];
    l.id = L.id;
    sem::copy_cstr(l.key, L.key);
    l.prio = L.priority;
    l.demand = true;
    l.on = true;
    l.duty = L.dimmable ? 0.6 : 1.0;
    l.p = L.p_nom_w;
    if (L.deferrable.enabled) {
      l.deferrable = true;
      l.e_req_wh = L.deferrable.daily_energy_wh;
      l.e_done_wh = 12.5;
      l.window_start_s = L.deferrable.window_start_s;
      l.deadline_s = L.deferrable.deadline_s;
    }
  }
  t.mode = sem::EmsMode::Predictive;
  t.state = sem::EmsState::Economy;
  t.safety = sem::SafetyCode::OcLockout;
  t.fallback = true;
  t.has_cmd_age = true;
  t.cmd_age_s = 1199.9;
  t.faults = 0xFFu;  // todas as falhas conhecidas
  const size_t n_tel = semapp::build_telemetry_json(t, meta, buf, sizeof(buf));
  emit("telemetry_full.json", n_tel);
  if (n_tel + 128 > semcfg::kMqttBufferSize) {
    std::fprintf(stderr, "ERRO: telemetria (%zu B) + cabeçalho MQTT excede o buffer (%u B)\n", n_tel,
                 static_cast<unsigned>(semcfg::kMqttBufferSize));
    ++failures;
  }

  // --- Telemetria sem hora e com sensores inválidos (campos null) -----------------
  semapp::TelemetrySnapshot t0{};
  t0.n_loads = p.loads.count;
  for (int i = 0; i < p.loads.count; ++i) {
    t0.loads[i].id = p.loads.items[i].id;
    sem::copy_cstr(t0.loads[i].key, p.loads.items[i].key);
    t0.loads[i].prio = p.loads.items[i].priority;
  }
  t0.safety = sem::SafetyCode::Boot;
  t0.faults = semapp::kFaultNoTime | semapp::kFaultInaPv;
  emit("telemetry_nulls.json", semapp::build_telemetry_json(t0, meta, buf, sizeof(buf)));

  // --- Decisão ---------------------------------------------------------------------
  semapp::DecisionSnapshot d{};
  d.ts_ms = 1794798000123LL;
  d.decision.mode = sem::EmsMode::Rules;
  d.decision.state = sem::EmsState::Economy;
  d.decision.has_prev_state = true;
  d.decision.prev_state = sem::EmsState::Normal;
  d.decision.n_loads = p.loads.count;
  for (int i = 0; i < p.loads.count; ++i) {
    d.ids[i] = p.loads.items[i].id;
    d.decision.commands[i] = {i != 2, i == 1 ? 0.6 : (i != 2 ? 1.0 : 0.0)};
  }
  sem::append_reason(d.decision.reason, sizeof(d.decision.reason), "soc 0.68 < 0.70 -> ECONOMY");
  d.safety = sem::SafetyCode::Ok;
  d.soc = 0.68;
  d.soc_valid = true;
  d.v_bat = 13.12;
  d.p_bat = 18.2;
  d.p_bus = 17.4;
  emit("decision.json", semapp::build_decision_json(d, meta, buf, sizeof(buf)));
  d.decision.has_prev_state = false;
  emit("decision_first.json", semapp::build_decision_json(d, meta, buf, sizeof(buf)));

  // --- Evento, ack, status, escalares -----------------------------------------------
  sem::EventList ev;
  ev.push(sem::EventCode::LvdTrip, sem::EventLevel::Warn, "Desconexao por subtensao: v_bat=%.2f V", 11.55)
      .with_number("v_bat", 11.55)
      .with_text("sensor", "bat");
  sem::CoreEvent e = ev[0];
  e.ts_ms = 1794798000123LL;
  emit("event.json", semapp::build_event_json(e, meta, buf, sizeof(buf)));
  emit("ack.json", semapp::build_ack_json("c-000123", false, "hmac invalido", 1794798000123LL, buf, sizeof(buf)));
  emit("ack_null_id.json", semapp::build_ack_json(nullptr, false, "envelope nao e JSON", 0, buf, sizeof(buf)));
  emit("status_offline.json", semapp::build_status_json(false, meta, 42, 0, buf, sizeof(buf)));
  emit("scalar.json", semapp::build_scalar_json(18.2134, 3, "V", "pv", 1794798000123LL, meta, buf, sizeof(buf)));
  emit("mode.json", semapp::build_scalar_text_json("rules", "ems", 1794798000123LL, meta, buf, sizeof(buf)));

  // Buffer pequeno deve falhar de forma segura (retorna 0).
  char tiny[16];
  if (semapp::build_telemetry_json(t, meta, tiny, sizeof(tiny)) != 0 || tiny[0] != '\0') {
    std::fprintf(stderr, "ERRO: buffer pequeno não foi detectado\n");
    ++failures;
  }
  return failures == 0 ? 0 : 1;
}
