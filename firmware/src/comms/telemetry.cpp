// Montagem dos payloads JSON do contrato (docs/05) com ArduinoJson 7.
#include "comms/telemetry.h"

#include <ArduinoJson.h>

#include <cmath>

namespace semapp {

namespace {

size_t finish(const JsonDocument& doc, char* out, size_t cap) {
  if (out == nullptr || cap == 0) return 0;
  const size_t need = measureJson(doc);
  if (need + 1 > cap) {
    out[0] = '\0';
    return 0;
  }
  return serializeJson(doc, out, cap);
}

void put_vip(JsonObject o, const MeasAvg& m) {
  if (m.ok) {
    o["v"] = round_to(m.v, 3);
    o["i"] = round_to(m.i, 3);
    o["p"] = round_to(m.p, 2);
  } else {
    o["v"] = nullptr;
    o["i"] = nullptr;
    o["p"] = nullptr;
  }
  o["ok"] = m.ok;
}

void put_temp(JsonObject o, const TempMeas& t) {
  if (t.ok) {
    o["t"] = round_to(t.t, 2);
  } else {
    o["t"] = nullptr;
  }
  o["ok"] = t.ok;
}

double nonneg(double x, int decimals) {
  const double r = round_to(x, decimals);
  return r > 0.0 ? r : 0.0;
}

}  // namespace

double round_to(double x, int decimals) {
  if (!std::isfinite(x)) return 0.0;
  const double f = std::pow(10.0, decimals);
  return std::round(x * f) / f;
}

const char* fault_name(uint32_t bit) {
  switch (bit) {
    case kFaultInaPv:
      return "INA226_PV";
    case kFaultInaBat:
      return "INA226_BAT";
    case kFaultInaBus:
      return "INA226_BUS";
    case kFaultTempBat:
      return "DS18B20_BAT";
    case kFaultTempPv:
      return "DS18B20_PV";
    case kFaultNoTime:
      return "NO_TIME";
    case kFaultSocInvalid:
      return "SOC_INVALID";
    case kFaultNvs:
      return "NVS";
    default:
      return "UNKNOWN";
  }
}

size_t build_telemetry_json(const TelemetrySnapshot& s, const PayloadMeta& meta, char* out, size_t cap) {
  JsonDocument d;
  d["schema"] = "sem.telemetry.v1";
  d["device_id"] = meta.device_id;
  d["fw_version"] = meta.fw_version;
  d["source"] = meta.source;
  d["boot_id"] = s.boot_id;
  d["seq"] = s.seq;
  d["ts_ms"] = s.time_synced ? s.ts_ms : static_cast<int64_t>(0);
  d["time_synced"] = s.time_synced;
  d["uptime_ms"] = s.uptime_ms;
  d["window_ms"] = s.window_ms;
  d["n_samples"] = s.n_samples;

  JsonObject u = d["units"].to<JsonObject>();
  u["v"] = "V";
  u["i"] = "A";
  u["p"] = "W";
  u["e"] = "Wh";
  u["t"] = "degC";
  u["soc"] = "1";
  u["duty"] = "1";

  JsonObject meas = d["meas"].to<JsonObject>();
  put_vip(meas["pv"].to<JsonObject>(), s.pv);
  put_vip(meas["bat"].to<JsonObject>(), s.bat);
  put_vip(meas["bus"].to<JsonObject>(), s.bus);
  put_temp(meas["t_pv"].to<JsonObject>(), s.t_pv);
  put_temp(meas["t_bat"].to<JsonObject>(), s.t_bat);
  JsonObject irr = meas["irr"].to<JsonObject>();  // sem piranômetro no protótipo
  irr["g"] = nullptr;
  irr["ok"] = false;

  JsonObject e = d["energy"].to<JsonObject>();
  if (s.has_day) {
    e["day"] = s.day;
  } else {
    e["day"] = nullptr;  // sem hora: contadores desde o boot
  }
  e["pv_in"] = nonneg(s.e_pv_in_wh, 3);
  e["bat_chg"] = nonneg(s.e_bat_chg_wh, 3);
  e["bat_dis"] = nonneg(s.e_bat_dis_wh, 3);
  e["load"] = nonneg(s.e_load_wh, 3);
  e["unserved"] = nonneg(s.e_unserved_wh, 3);

  JsonObject soc = d["soc"].to<JsonObject>();
  if (s.soc_has_value) {
    soc["value"] = round_to(s.soc, 4);
    soc["unc"] = round_to(s.soc_unc, 4);
  } else {
    soc["value"] = nullptr;
    soc["unc"] = nullptr;
  }
  soc["valid"] = s.soc_valid;
  soc["last_sync"] = sem::to_string(s.last_sync);
  if (s.soc_has_value) {
    soc["since_sync_s"] = nonneg(s.since_sync_s, 1);
  } else {
    soc["since_sync_s"] = nullptr;
  }

  JsonArray loads = d["loads"].to<JsonArray>();
  for (int k = 0; k < s.n_loads && k < sem::kMaxLoads; ++k) {
    const LoadSnap& l = s.loads[k];
    JsonObject o = loads.add<JsonObject>();
    o["id"] = l.id;
    o["key"] = l.key;
    o["prio"] = sem::to_string(l.prio);
    o["demand"] = l.demand;
    o["on"] = l.on;
    o["duty"] = round_to(l.duty, 4);
    o["p"] = nonneg(l.p, 2);
    o["p_src"] = "estimated";  // sem INA226 por carga no protótipo
    o["fault"] = l.fault;
    if (l.deferrable) {
      JsonObject df = o["deferrable"].to<JsonObject>();
      char ws[8], dl[8];
      sem::format_hhmm(l.window_start_s, ws, sizeof(ws));
      sem::format_hhmm(l.deadline_s, dl, sizeof(dl));
      df["e_req_wh"] = nonneg(l.e_req_wh, 3);
      df["e_done_wh"] = nonneg(l.e_done_wh, 3);
      df["window_start"] = ws;
      df["deadline"] = dl;
    }
  }

  JsonObject ems = d["ems"].to<JsonObject>();
  ems["mode"] = sem::to_string(s.mode);
  ems["state"] = sem::to_string(s.state);
  ems["safety"] = sem::to_string(s.safety);
  ems["fallback"] = s.fallback;
  if (s.has_cmd_age) {
    ems["cmd_age_s"] = nonneg(s.cmd_age_s, 1);
  } else {
    ems["cmd_age_s"] = nullptr;
  }

  JsonArray faults = d["faults"].to<JsonArray>();
  for (uint32_t bit = 1; bit != 0 && bit <= kFaultNvs; bit <<= 1) {
    if (s.faults & bit) faults.add(fault_name(bit));
  }
  return finish(d, out, cap);
}

size_t build_decision_json(const DecisionSnapshot& s, const PayloadMeta& meta, char* out, size_t cap) {
  const sem::Decision& dec = s.decision;
  JsonDocument d;
  d["schema"] = "sem.decision.v1";
  d["ts_ms"] = s.ts_ms;
  d["source"] = meta.source;
  d["origin"] = "firmware";
  d["mode"] = sem::to_string(dec.mode);
  d["state"] = sem::to_string(dec.state);
  if (dec.has_prev_state) {
    d["prev_state"] = sem::to_string(dec.prev_state);
  } else {
    d["prev_state"] = nullptr;
  }
  d["safety"] = sem::to_string(s.safety);
  d["reason"] = dec.reason;
  JsonArray actions = d["actions"].to<JsonArray>();
  for (int k = 0; k < dec.n_loads && k < sem::kMaxLoads; ++k) {
    JsonObject a = actions.add<JsonObject>();
    a["id"] = s.ids[k];
    a["on"] = dec.commands[k].on;
    a["duty"] = round_to(dec.commands[k].duty, 4);
  }
  JsonObject in = d["inputs"].to<JsonObject>();
  in["soc"] = round_to(s.soc, 4);
  in["soc_valid"] = s.soc_valid;
  in["v_bat"] = round_to(s.v_bat, 3);
  in["p_pv"] = round_to(s.p_pv, 2);
  in["p_bat"] = round_to(s.p_bat, 2);
  in["p_bus"] = round_to(s.p_bus, 2);
  in["fallback"] = dec.fallback;
  d["plan"] = nullptr;  // planos só nas decisões do backend (MPC)
  return finish(d, out, cap);
}

size_t build_event_json(const sem::CoreEvent& ev, const PayloadMeta& meta, char* out, size_t cap) {
  JsonDocument d;
  d["schema"] = "sem.event.v1";
  d["ts_ms"] = ev.ts_ms > 0 ? ev.ts_ms : static_cast<int64_t>(0);
  d["source"] = meta.source;
  d["level"] = sem::to_string(ev.level);
  d["code"] = sem::to_string(ev.code);
  d["msg"] = ev.msg;
  JsonObject data = d["data"].to<JsonObject>();
  for (int k = 0; k < ev.n_data && k < 2; ++k) {
    const sem::EventDatum& x = ev.data[k];
    if (x.is_string) {
      data[x.key] = x.text;
    } else {
      data[x.key] = round_to(x.number, 4);
    }
  }
  return finish(d, out, cap);
}

size_t build_ack_json(const char* id, bool accepted, const char* reason, int64_t ts_ms, char* out, size_t cap) {
  JsonDocument d;
  if (id != nullptr) {
    d["id"] = id;
  } else {
    d["id"] = nullptr;
  }
  d["accepted"] = accepted;
  d["reason"] = reason != nullptr ? reason : "";
  d["ts_ms"] = ts_ms;
  return finish(d, out, cap);
}

size_t build_status_json(bool online, const PayloadMeta& meta, uint32_t boot_id, int64_t ts_ms, char* out,
                         size_t cap) {
  JsonDocument d;
  d["status"] = online ? "online" : "offline";
  d["device_id"] = meta.device_id;
  d["boot_id"] = boot_id;
  d["fw_version"] = meta.fw_version;
  d["ts_ms"] = ts_ms;
  return finish(d, out, cap);
}

size_t build_scalar_json(double value, int decimals, const char* unit, const char* point, int64_t ts_ms,
                         const PayloadMeta& meta, char* out, size_t cap) {
  JsonDocument d;
  d["value"] = round_to(value, decimals);
  d["unit"] = unit;
  d["point"] = point;
  d["ts_ms"] = ts_ms;
  d["source"] = meta.source;
  d["device_id"] = meta.device_id;
  return finish(d, out, cap);
}

size_t build_scalar_text_json(const char* value, const char* point, int64_t ts_ms, const PayloadMeta& meta, char* out,
                              size_t cap) {
  JsonDocument d;
  d["value"] = value;
  d["unit"] = "";
  d["point"] = point;
  d["ts_ms"] = ts_ms;
  d["source"] = meta.source;
  d["device_id"] = meta.device_id;
  return finish(d, out, cap);
}

}  // namespace semapp
