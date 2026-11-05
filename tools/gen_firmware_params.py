#!/usr/bin/env python3
"""Gera ``firmware/include/system_params.h`` a partir de ``config/system.yaml``.

O arquivo YAML é a FONTE ÚNICA de parâmetros do projeto. Este gerador converte
os blocos relevantes ao firmware (cargas, limiares de segurança, estimador de
SOC, regras do EMS, perfil de demanda, pinos, endereços dos INA226, shunts e
períodos) em:

* constantes ``constexpr`` de compilação (namespace ``sem::sysp``) — pinos,
  endereços, períodos e versão;
* a função ``sem::make_system_params()``, que preenche a struct
  ``sem::SystemParams`` (``firmware/lib/sem_core/include/sem_core/params.h``).

Uso::

    python tools/gen_firmware_params.py            # (re)gera o cabeçalho
    python tools/gen_firmware_params.py --check    # falha se estiver desatualizado

O cabeçalho gerado é versionado e começa com "GERADO AUTOMATICAMENTE — não editar".
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[1]
DEFAULT_YAML = REPO / "config" / "system.yaml"
DEFAULT_OUT = REPO / "firmware" / "include" / "system_params.h"

# Limites estáticos de sem_core/types.h (validados aqui para falhar cedo).
K_MAX_LOADS = 8
K_MAX_WINDOWS = 4
K_MAX_OCV_POINTS = 16
K_KEY_LEN = 16

PRIORITY = {"high": "sem::Priority::High", "medium": "sem::Priority::Medium", "low": "sem::Priority::Low"}
MODE = {"conventional": "sem::EmsMode::Conventional", "rules": "sem::EmsMode::Rules",
        "predictive": "sem::EmsMode::Predictive"}


def parse_hhmm(s: str) -> int:
    """'HH:MM' -> segundos desde a meia-noite (mesma regra de smartenergy.timeutil)."""
    hh, mm = str(s).strip().split(":")
    h, m = int(hh), int(mm)
    if not (0 <= h <= 24 and 0 <= m < 60) or (h == 24 and m != 0):
        raise ValueError(f"horário inválido: {s!r}")
    return h * 3600 + m * 60


def num(x: Any) -> str:
    """Literal double C++ com representação exata do float do Python (repr)."""
    v = float(x)
    r = repr(v)
    if r in ("inf", "-inf", "nan"):
        raise ValueError(f"valor não finito: {x!r}")
    if "e" not in r and "." not in r:
        r += ".0"
    return r


def cstr(s: Any) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def posix_tz(utc_offset_h: float) -> str:
    """UTC-3 -> "<-03>3" (POSIX: sinal invertido; sem horário de verão)."""
    off = int(round(utc_offset_h))
    sign = "-" if off < 0 else "+"
    return f"<{sign}{abs(off):02d}>{-off}"


def gen(cfg: dict[str, Any], yaml_bytes: bytes) -> str:
    sha = hashlib.sha256(yaml_bytes).hexdigest()[:16]
    loads = cfg["loads"]
    if len(loads) > K_MAX_LOADS:
        raise ValueError(f"mais de {K_MAX_LOADS} cargas")
    hw = cfg["hardware"]
    bat = cfg["battery"]
    se = cfg["soc_estimator"]
    sa = cfg["safety"]
    ru = cfg["ems"]["rules"]
    pr = cfg["ems"]["predictive"]
    cm = cfg["comm"]
    pj = cfg["project"]
    site = cfg["site"]
    ocv_soc = bat["ocv_curve"]["soc"]
    ocv_v = bat["ocv_curve"]["v"]
    if len(ocv_soc) != len(ocv_v) or len(ocv_v) > K_MAX_OCV_POINTS:
        raise ValueError("curva OCV inválida")
    if any(b <= a for a, b in zip(ocv_v, ocv_v[1:])):
        raise ValueError("curva OCV deve ter V estritamente crescente")
    key_index = {ld["key"]: i for i, ld in enumerate(loads)}

    L: list[str] = []
    w = L.append
    w("// " + "=" * 77)
    w("// GERADO AUTOMATICAMENTE — não editar.")
    w("// Fonte: config/system.yaml (sha256 " + sha + "...)")
    w("// Gerador: tools/gen_firmware_params.py  (rode-o após alterar o YAML)")
    w("// Valores marcados PENDENTE no YAML são referências a confirmar em datasheet/medição.")
    w("// " + "=" * 77)
    w("#pragma once")
    w("")
    w('#include "sem_core/params.h"')
    w("")
    w("namespace sem {")
    w("namespace sysp {")
    w("")
    w("// --- Identificação ---")
    w(f"constexpr const char* kDeviceId = {cstr(pj['device_id'])};")
    w(f"constexpr const char* kFirmwareVersion = {cstr(pj['firmware_version'])};")
    w(f"constexpr const char* kMqttPrefix = {cstr(cm['mqtt_prefix'])};")
    w(f"constexpr const char* kPosixTz = {cstr(posix_tz(site['utc_offset_h']))};")
    w(f"constexpr int32_t kUtcOffsetS = {int(round(float(site['utc_offset_h']) * 3600))};")
    w("")
    w("// --- Períodos ---")
    w(f"constexpr uint32_t kSamplePeriodMs = {int(cm['sample_period_ms'])};")
    w(f"constexpr uint32_t kEmsPeriodMs = {int(cm['ems_period_ms'])};")
    w(f"constexpr uint32_t kTelemetryPeriodMs = {int(round(float(cm['telemetry_period_s']) * 1000))};")
    w(f"constexpr uint32_t kPersistEveryS = {int(se['persist_every_s'])};")
    w("")
    w("// --- Pinos (docs/04-mapa-de-pinos.md) ---")
    w(f"constexpr int kPinI2cSda = {int(hw['i2c_sda'])};")
    w(f"constexpr int kPinI2cScl = {int(hw['i2c_scl'])};")
    w(f"constexpr uint32_t kI2cHz = {int(hw['i2c_hz'])};")
    w(f"constexpr int kPinOneWire = {int(hw['onewire_pin'])};")
    w(f"constexpr int kPinStatusLed = {int(hw['status_led_pin'])};")
    w(f"constexpr int kPinInaAlert = {int(hw['ina_alert_pin'])};  // entrada; pull-up externo")
    w(f"constexpr int kPinButton = {int(hw['button_pin'])};       // entrada; pull-up externo")
    w(f"constexpr int kPinUartMpptRx = {int(hw['uart_mppt_rx'])};")
    w(f"constexpr int kPinUartMpptTx = {int(hw['uart_mppt_tx'])};")
    w(f"constexpr int kNumLoads = {len(loads)};")
    w("constexpr int kLoadGpio[kNumLoads] = {" + ", ".join(str(int(ld["gpio"])) for ld in loads) + "};")
    w("")
    w("// --- Endereços I2C ---")
    for name in ("pv", "bat", "bus"):
        w(f"constexpr uint8_t kIna226Addr{name.capitalize()} = 0x{int(hw['ina226'][name]['address']):02X};")
    w(f"constexpr uint8_t kRtcDs3231Addr = 0x{int(hw['rtc_ds3231_address']):02X};")
    w("")
    w("}  // namespace sysp")
    w("")
    w("/// Parâmetros completos do sistema (espelho de config/system.yaml).")
    w("inline SystemParams make_system_params() {")
    w("  SystemParams p{};")
    w("  // project / site")
    w(f"  copy_cstr(p.project.device_id, {cstr(pj['device_id'])});")
    w(f"  copy_cstr(p.project.firmware_version, {cstr(pj['firmware_version'])});")
    w(f"  p.project.utc_offset_s = {int(round(float(site['utc_offset_h']) * 3600))};")
    w(f"  copy_cstr(p.project.posix_tz, {cstr(posix_tz(site['utc_offset_h']))});")
    w("  // battery")
    for k in ("nominal_v", "capacity_ah", "max_charge_current_a", "max_discharge_current_a", "bms_ovp_v",
              "bms_uvp_v", "charge_min_temp_c"):
        w(f"  p.battery.{k} = {num(bat[k])};")
    w("  // soc_estimator (+ battery, safety.sensor_timeout_s)")
    w(f"  p.soc.capacity_ah = {num(bat['capacity_ah'])};")
    w(f"  p.soc.coulombic_efficiency = {num(bat['coulombic_efficiency'])};")
    w(f"  p.soc.r_internal_ohm = {num(bat['r_internal_ohm'])};")
    w(f"  p.soc.current_deadband_a = {num(se['current_deadband_a'])};")
    w(f"  p.soc.full_voltage_v = {num(se['full_sync']['voltage_v'])};")
    w(f"  p.soc.full_tail_current_a = {num(se['full_sync']['tail_current_a'])};")
    w(f"  p.soc.full_hold_s = {num(se['full_sync']['hold_s'])};")
    w(f"  p.soc.empty_voltage_v = {num(se['empty_sync']['voltage_v'])};")
    w(f"  p.soc.empty_hold_s = {num(se['empty_sync']['hold_s'])};")
    w(f"  p.soc.empty_soc_value = {num(se['empty_sync']['soc_value'])};")
    for k in ("unknown_initial_uncertainty", "sync_uncertainty", "drift_uncertainty_per_ah", "persist_every_s",
              "persist_delta_soc"):
        w(f"  p.soc.{k} = {num(se[k])};")
    w(f"  p.soc.sensor_timeout_s = {num(sa['sensor_timeout_s'])};")
    for i, (s_, v_) in enumerate(zip(ocv_soc, ocv_v)):
        w(f"  p.soc.ocv_soc[{i}] = {num(s_)};  p.soc.ocv_v[{i}] = {num(v_)};")
    w(f"  p.soc.ocv_count = {len(ocv_v)};")
    w("  // safety")
    for k in ("lvd_v", "lvd_delay_s", "soc_hard_min", "reconnect_v", "reconnect_min_soc", "bus_ovp_v",
              "bus_ovp_delay_s", "bus_overcurrent_a", "bus_overcurrent_delay_ms", "overcurrent_retry_s",
              "battery_overtemp_c", "battery_overtemp_release_c", "sensor_timeout_s"):
        w(f"  p.safety.{k} = {num(sa[k])};")
    w(f"  p.safety.overcurrent_max_retries = {int(sa['overcurrent_max_retries'])};")
    w(f"  p.safety.boot_settle_samples = {int(sa['boot_settle_samples'])};")
    w("  // ems.rules")
    for k in ("normal_min_soc", "economy_min_soc", "critical_min_soc", "hysteresis_soc", "economy_medium_duty",
              "min_dwell_s"):
        w(f"  p.rules.{k} = {num(ru[k])};")
    for k in ("surplus_on_soc", "surplus_off_soc", "economy_surplus_margin_w", "deadline_margin_s"):
        w(f"  p.rules.{k} = {num(ru['deferrable'][k])};")
    w("  // ems.predictive (parte usada no firmware)")
    for k in ("step_s", "replan_s", "command_ttl_s", "soc_min", "soc_max"):
        w(f"  p.predictive.{k} = {num(pr[k])};")
    w(f"  p.predictive.horizon_steps = {int(pr['horizon_steps'])};")
    w(f"  p.default_mode = {MODE[cfg['ems']['default_mode']]};")
    w(f"  p.aux_p_w = {num(cfg['aux']['p_w'])};")
    w("  // loads")
    w(f"  p.loads.count = {len(loads)};")
    for i, ld in enumerate(loads):
        if len(ld["key"]) >= K_KEY_LEN:
            raise ValueError(f"chave longa demais: {ld['key']}")
        pfx = f"  p.loads.items[{i}]"
        w(f"  // [{i}] {ld['key']}: {ld['name']}")
        w(f"{pfx}.id = {int(ld['id'])};")
        w(f"  copy_cstr(p.loads.items[{i}].key, {cstr(ld['key'])});")
        w(f"{pfx}.priority = {PRIORITY[ld['priority']]};")
        w(f"{pfx}.p_nom_w = {num(ld['p_nom_w'])};")
        w(f"{pfx}.v_ref_v = {num(ld['v_ref_v'])};")
        w(f"{pfx}.voltage_exponent = {num(ld['voltage_exponent'])};")
        w(f"{pfx}.dimmable = {'true' if ld.get('dimmable') else 'false'};")
        w(f"{pfx}.min_duty = {num(ld.get('min_duty') or 0.0)};")
        w(f"{pfx}.pwm_freq_hz = {num(ld.get('pwm_freq_hz') or 0.0)};")
        w(f"{pfx}.i_max_a = {num(ld['i_max_a'])};")
        w(f"{pfx}.fuse_a = {num(ld['fuse_a'])};")
        w(f"{pfx}.gpio = {int(ld['gpio'])};")
        de = ld.get("deferrable")
        if de:
            w(f"{pfx}.deferrable.enabled = true;")
            w(f"{pfx}.deferrable.daily_energy_wh = {num(de['daily_energy_wh'])};")
            w(f"{pfx}.deferrable.window_start_s = {parse_hhmm(de['window_start'])};  // {de['window_start']}")
            w(f"{pfx}.deferrable.deadline_s = {parse_hhmm(de['deadline'])};  // {de['deadline']}")
    w("  // demand.schedule (janelas [início, fim) em hora local)")
    for key, windows in (cfg["demand"].get("schedule") or {}).items():
        if key not in key_index:
            raise ValueError(f"demand.schedule: carga desconhecida {key!r}")
        if len(windows) > K_MAX_WINDOWS:
            raise ValueError(f"demand.schedule.{key}: mais de {K_MAX_WINDOWS} janelas")
        i = key_index[key]
        for j, (a, b) in enumerate(windows):
            w(f"  p.demand.schedule[{i}].windows[{j}] = {{{parse_hhmm(a)}, {parse_hhmm(b)}}};  // {key} {a}-{b}")
        w(f"  p.demand.schedule[{i}].count = {len(windows)};")
    w("  // demand.conventional.<key>_schedule (operação convencional da carga deslocável)")
    for name, windows in (cfg["demand"].get("conventional") or {}).items():
        if not name.endswith("_schedule"):
            continue
        key = name[: -len("_schedule")]
        if key not in key_index:
            raise ValueError(f"demand.conventional: carga desconhecida {key!r}")
        i = key_index[key]
        for j, (a, b) in enumerate(windows):
            w(f"  p.conventional.deferrable_schedule[{i}].windows[{j}] = {{{parse_hhmm(a)}, {parse_hhmm(b)}}};"
              f"  // {key} {a}-{b}")
        w(f"  p.conventional.deferrable_schedule[{i}].count = {len(windows)};")
    w("  // comm")
    w(f"  copy_cstr(p.comm.mqtt_prefix, {cstr(cm['mqtt_prefix'])});")
    w(f"  p.comm.telemetry_period_s = {num(cm['telemetry_period_s'])};")
    w(f"  p.comm.sample_period_ms = {int(cm['sample_period_ms'])};")
    w(f"  p.comm.ems_period_ms = {int(cm['ems_period_ms'])};")
    w(f"  p.comm.command_hmac = {'true' if cm['command_hmac'] else 'false'};")
    w(f"  p.comm.command_max_skew_s = {num(cm['command_max_skew_s'])};")
    w(f"  p.comm.command_id_cache = {int(cm['command_id_cache'])};")
    w("  // hardware")
    for k in ("i2c_sda", "i2c_scl", "onewire_pin", "status_led_pin", "ina_alert_pin", "button_pin", "uart_mppt_rx",
              "uart_mppt_tx"):
        w(f"  p.hardware.{k} = {int(hw[k])};")
    w(f"  p.hardware.i2c_hz = {int(hw['i2c_hz'])};")
    w(f"  p.hardware.rtc_ds3231_address = 0x{int(hw['rtc_ds3231_address']):02X};")
    for name in ("pv", "bat", "bus"):
        s = hw["ina226"][name]
        pfx = f"  p.hardware.ina_{name}"
        w(f"  copy_cstr(p.hardware.ina_{name}.name, {cstr(name)});")
        w(f"{pfx}.address = 0x{int(s['address']):02X};")
        w(f"{pfx}.shunt_ohm = {num(s['shunt_ohm'])};")
        w(f"{pfx}.max_current_a = {num(s['max_current_a'])};")
        w(f"{pfx}.sign = {int(s['sign'])};")
    w("  return p;")
    w("}")
    w("")
    w("}  // namespace sem")
    w("")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", type=Path, default=DEFAULT_YAML)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--check", action="store_true", help="não escreve; retorna 1 se o arquivo estiver desatualizado")
    args = ap.parse_args(argv)

    raw = args.config.read_bytes()
    cfg = yaml.safe_load(raw)
    text = gen(cfg, raw)
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        if current != text:
            print(f"DESATUALIZADO: {args.out} (rode tools/gen_firmware_params.py)", file=sys.stderr)
            return 1
        print(f"OK: {args.out} corresponde a {args.config}")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"gerado: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
