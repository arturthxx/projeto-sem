"""Pacotes ``sem.telemetry.v1`` SINTÉTICOS para testes e verificações rápidas.

Não são medições nem saída do simulador físico: são valores fixos/parametrizados
que obedecem ao esquema. Por padrão ``source="simulation"`` (honestidade
científica); testes podem pedir ``source="hardware"`` apenas para verificar que a
origem é preservada de ponta a ponta.
"""

from __future__ import annotations

import copy
from typing import Any

UNITS = {"v": "V", "i": "A", "p": "W", "e": "Wh", "t": "degC", "soc": "1", "duty": "1"}


def synthetic_telemetry(
    ts_ms: int = 1763290800000,
    seq: int = 0,
    *,
    source: str = "simulation",
    device_id: str = "sem-esp32-01",
    boot_id: int = 1,
    soc: float = 0.73,
    pv_p: float = 30.0,
    bus_p: float = 10.0,
    mode: str = "rules",
    state: str = "NORMAL",
    day: str | None = "2025-11-16",
    energy_scale: float = 1.0,
    loads_on: tuple[bool, bool, bool] = (True, True, False),
    demand: tuple[bool, bool, bool] = (True, True, False),
    res_done_wh: float = 12.5,
    time_synced: bool = True,
) -> dict[str, Any]:
    """Gera um pacote válido contra ``schemas/telemetry.schema.json``."""
    v_bat = 13.2
    p_bat = bus_p + 0.8 - pv_p * 0.95
    pv_v = 18.0
    loads = []
    keys = (("led", "high", 5.0), ("fan", "medium", 3.6), ("res", "low", 16.4))
    for idx, (key, prio, p) in enumerate(keys):
        on = bool(loads_on[idx])
        item: dict[str, Any] = {
            "id": idx + 1, "key": key, "prio": prio, "demand": bool(demand[idx]), "on": on,
            "duty": (0.6 if key == "fan" else 1.0) if on else 0.0, "p": p if on else 0.0,
            "p_src": "estimated", "fault": False,
        }
        if key == "res":
            item["deferrable"] = {"e_req_wh": 40.0, "e_done_wh": res_done_wh,
                                  "window_start": "07:00", "deadline": "22:00"}
        loads.append(item)
    pkt: dict[str, Any] = {
        "schema": "sem.telemetry.v1",
        "device_id": device_id,
        "fw_version": "0.1.0",
        "source": source,
        "boot_id": boot_id,
        "seq": seq,
        "ts_ms": int(ts_ms) if time_synced else 0,
        "time_synced": time_synced,
        "uptime_ms": 1000 * 5 * seq,
        "window_ms": 5000,
        "n_samples": 50,
        "units": dict(UNITS),
        "meas": {
            "pv": {"v": pv_v, "i": round(pv_p / pv_v, 4), "p": pv_p, "ok": True},
            "bat": {"v": v_bat, "i": round(p_bat / v_bat, 4), "p": round(p_bat, 3), "ok": True},
            "bus": {"v": v_bat - 0.01, "i": round(bus_p / (v_bat - 0.01), 4), "p": bus_p, "ok": True},
            "t_pv": {"t": 40.0, "ok": True},
            "t_bat": {"t": 28.0, "ok": True},
            "irr": {"g": None, "ok": False},
        },
        "energy": {
            "day": day if time_synced else None,
            "pv_in": 100.0 * energy_scale, "bat_chg": 50.0 * energy_scale, "bat_dis": 20.0 * energy_scale,
            "load": 70.0 * energy_scale, "unserved": 1.0 * energy_scale,
        },
        "soc": {"value": soc, "unc": 0.05, "valid": True, "last_sync": "full", "since_sync_s": 3600},
        "loads": loads,
        "ems": {"mode": mode, "state": state, "safety": "OK", "fallback": False, "cmd_age_s": None},
        "faults": [],
    }
    return copy.deepcopy(pkt)
