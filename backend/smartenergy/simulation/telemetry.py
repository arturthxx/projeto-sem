"""Conversão de linhas da série simulada em pacotes ``sem.telemetry.v1`` (``source="simulation"``).

Usado pelo gêmeo digital (``twin``), pelo ``export-demo`` e pela API para
publicar dados simulados **no mesmo formato do ESP32** — sempre rotulados como
simulação. Medições (``meas``) usam as leituras simuladas dos sensores
(``*_meas``) quando presentes; senão, os valores físicos da série. A potência
das cargas é ``p_src="estimated"`` (como no firmware sem INA226 dedicado).
O bloco opcional ``sim`` (docs/05 §3) carrega cenário, estratégia, semente,
``p_pv_avail`` e ``soc_true``.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

import numpy as np
import pandas as pd

from smartenergy.config import SystemConfig
from smartenergy.metrics.indicators import local_day_and_tod

UNITS = {"v": "V", "i": "A", "p": "W", "e": "Wh", "t": "degC", "soc": "1", "duty": "1"}
DEFAULT_LAST_SYNC = "persisted"


def _num(row: Any, key: str, nd: int = 3) -> float | None:
    """Valor numérico arredondado da linha (``None`` se ausente/NaN)."""
    try:
        v = row.get(key) if hasattr(row, "get") else row[key]
    except (KeyError, IndexError):
        return None
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return round(f, nd)


def _first(row: Any, *keys: str, nd: int = 3) -> float | None:
    for k in keys:
        v = _num(row, k, nd)
        if v is not None:
            return v
    return None


def _vip(row: Any, point: str) -> dict[str, Any]:
    v = _first(row, f"{point}_v_meas", f"{point}_v")
    i = _first(row, f"{point}_i_meas", f"{point}_i")
    if v is None or i is None:
        return {"v": v, "i": i, "p": None, "ok": False}
    return {"v": v, "i": i, "p": round(v * i, 3), "ok": True}


def _temp(row: Any, key: str) -> dict[str, Any]:
    t = _num(row, key, 2)
    return {"t": t, "ok": t is not None}


def _fmt_day(t_ms: int, cfg: SystemConfig) -> str:
    day, _ = local_day_and_tod([t_ms], float(cfg.raw["site"]["utc_offset_h"]))
    return pd.Timestamp(int(day[0]) * 86400, unit="s").strftime("%Y-%m-%d")


def row_to_telemetry(
    row: Mapping[str, Any] | pd.Series,
    cfg: SystemConfig,
    seq: int,
    boot_id: int = 1,
    energy_day: Mapping[str, Any] | None = None,
    sim_meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Converte uma linha da série (vocabulário docs/05 §8) em ``sem.telemetry.v1``.

    Args:
        row: linha (``pd.Series`` ou dicionário) com ``t``, medições, ``soc``, ``l{n}_*`` etc.
        cfg: configuração (cargas, identificação do dispositivo).
        seq: contador de mensagens desde o "boot".
        boot_id: contador de inicializações.
        energy_day: contadores do dia ``{day, pv_in, bat_chg, bat_dis, load, unserved}``
            (ver :func:`daily_energy_counters`); ausente → zeros no dia local de ``t``.
        sim_meta: ``{scenario, strategy, seed, t0_ms?}`` — inclui o bloco ``sim``.

    Returns:
        Dicionário válido contra ``schemas/telemetry.schema.json`` com ``source="simulation"``.
    """
    t_ms = int(_num(row, "t", 0) or 0)
    window_ms = int(round(float((sim_meta or {}).get("dt_s", cfg.raw["simulation"]["dt_s"])) * 1000))
    t0 = (sim_meta or {}).get("t0_ms")
    uptime = max(0, t_ms - int(t0)) if t0 is not None else max(0, int(seq) * window_ms)

    soc_v = _num(row, "soc", 4)
    soc_unc = _num(row, "soc_unc", 4)
    valid_raw = row.get("soc_valid") if hasattr(row, "get") else None
    soc_valid = bool(valid_raw) if valid_raw is not None and not (isinstance(valid_raw, float) and math.isnan(valid_raw)) else soc_v is not None
    last_sync = row.get("soc_last_sync") if hasattr(row, "get") else None
    if not isinstance(last_sync, str) or last_sync not in ("none", "ocv_init", "persisted", "full", "empty", "manual"):
        last_sync = DEFAULT_LAST_SYNC
    since = _num(row, "soc_since_sync_s", 0)

    loads = []
    for ld in cfg.loads:
        on = bool(_num(row, f"l{ld.id}_on", 0) or 0)
        duty = _num(row, f"l{ld.id}_duty", 4) or 0.0
        p = _num(row, f"l{ld.id}_p", 3)
        item: dict[str, Any] = {
            "id": ld.id, "key": ld.key, "prio": ld.priority,
            "demand": bool(_num(row, f"l{ld.id}_d", 0) or 0),
            "on": on, "duty": min(max(duty, 0.0), 1.0) if on else 0.0,
            "p": max(p, 0.0) if p is not None else None, "p_src": "estimated", "fault": False,
        }
        if ld.deferrable is not None:
            item["deferrable"] = {
                "e_req_wh": float(ld.deferrable.daily_energy_wh),
                "e_done_wh": max(_num(row, "def_done_wh", 3) or 0.0, 0.0),
                "window_start": ld.deferrable.window_start,
                "deadline": ld.deferrable.deadline,
            }
        loads.append(item)

    if energy_day is None:
        energy = {"day": _fmt_day(t_ms, cfg), "pv_in": 0.0, "bat_chg": 0.0, "bat_dis": 0.0, "load": 0.0, "unserved": 0.0}
    else:
        energy = {"day": energy_day.get("day")}
        for k in ("pv_in", "bat_chg", "bat_dis", "load", "unserved"):
            energy[k] = round(max(float(energy_day.get(k, 0.0) or 0.0), 0.0), 3)

    safety = str(row.get("safety", "OK") if hasattr(row, "get") else "OK")
    faults = ["BMS_OPEN"] if (_num(row, "bms_open", 0) or 0) else []
    pkt: dict[str, Any] = {
        "schema": "sem.telemetry.v1",
        "device_id": str(cfg.raw["project"]["device_id"]),
        "fw_version": str(cfg.raw["project"].get("firmware_version", "0.0.0")),
        "source": "simulation",
        "boot_id": int(boot_id),
        "seq": int(seq),
        "ts_ms": t_ms,
        "time_synced": True,
        "uptime_ms": int(uptime),
        "window_ms": window_ms,
        "n_samples": 1,
        "units": dict(UNITS),
        "meas": {
            "pv": _vip(row, "pv"),
            "bat": _vip(row, "bat"),
            "bus": _vip(row, "bus"),
            "t_pv": _temp(row, "t_pv"),
            "t_bat": _temp(row, "t_bat"),
            "irr": {"g": _num(row, "irr_poa", 1), "ok": _num(row, "irr_poa", 1) is not None},
        },
        "energy": energy,
        "soc": {
            "value": min(max(soc_v, 0.0), 1.0) if soc_v is not None else None,
            "unc": min(max(soc_unc, 0.0), 1.0) if soc_unc is not None else None,
            "valid": soc_valid,
            "last_sync": last_sync,
            "since_sync_s": max(since, 0.0) if since is not None else None,
        },
        "loads": loads,
        "ems": {
            "mode": str(row.get("mode", "rules") if hasattr(row, "get") else "rules"),
            "state": str(row.get("state", "NORMAL") if hasattr(row, "get") else "NORMAL"),
            "safety": safety,
            "fallback": bool(_num(row, "fallback", 0) or 0),
            "cmd_age_s": None,
        },
        "faults": faults,
    }
    if sim_meta is not None:
        pkt["sim"] = {
            "scenario": sim_meta.get("scenario"),
            "strategy": sim_meta.get("strategy"),
            "seed": sim_meta.get("seed"),
            "p_pv_avail": _num(row, "pv_p_avail", 3),
            "soc_true": _num(row, "soc_true", 4),
        }
    return pkt


def daily_energy_counters(df: pd.DataFrame, cfg: SystemConfig, dt_s: float | None = None) -> pd.DataFrame:
    """Contadores de energia do dia local (Wh) acumulados ANTES de cada linha.

    Colunas: ``day`` (AAAA-MM-DD local), ``pv_in``, ``bat_chg``, ``bat_dis``, ``load``
    (cargas experimentais, ``bus_p``) e ``unserved`` (não deslocáveis). Soma
    retangular (potências constantes no passo do simulador); zera à meia-noite local.
    """
    if dt_s is None:
        dt_s = float(df.attrs.get("dt_s") or (float(df["t"].iloc[1] - df["t"].iloc[0]) / 1000.0))
    h = float(dt_s) / 3600.0
    day_idx, _ = local_day_and_tod(df["t"].to_numpy(), float(cfg.raw["site"]["utc_offset_h"]))

    def col(c: str) -> np.ndarray:
        return np.nan_to_num(df[c].to_numpy(dtype=np.float64)) if c in df.columns else np.zeros(len(df))

    bat_p = col("bat_p")
    series = {
        "pv_in": col("pv_p"), "bat_chg": np.maximum(-bat_p, 0.0), "bat_dis": np.maximum(bat_p, 0.0),
        "load": col("bus_p"), "unserved": col("unserved_p"),
    }
    out = pd.DataFrame(index=df.index)
    out["day"] = pd.to_datetime(day_idx * 86400, unit="s").strftime("%Y-%m-%d")
    for name, p in series.items():
        e = pd.Series(np.maximum(p, 0.0) * h, index=df.index)
        out[name] = (e.groupby(day_idx).cumsum() - e).clip(lower=0.0).to_numpy()
    return out


def simulation_to_telemetry(
    df: pd.DataFrame,
    cfg: SystemConfig,
    sim_meta: Mapping[str, Any] | None = None,
    every: int = 1,
    boot_id: int = 1,
) -> list[dict[str, Any]]:
    """Converte a série inteira (a cada ``every`` linhas) em pacotes ``sem.telemetry.v1``."""
    counters = daily_energy_counters(df, cfg)
    out = []
    for seq, idx in enumerate(range(0, len(df), max(1, int(every)))):
        row = df.iloc[idx]
        e = counters.iloc[idx]
        out.append(row_to_telemetry(row, cfg, seq, boot_id=boot_id, energy_day=e.to_dict(), sim_meta=sim_meta))
    return out
