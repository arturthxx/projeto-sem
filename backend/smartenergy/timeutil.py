"""Utilidades de tempo: horário local, janelas [início, fim) e demanda programada.

Convenção (docs/06): intervalo [início, fim) em hora local; se fim < início o
intervalo cruza a meia-noite. ``tod`` = segundos desde a meia-noite local.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from smartenergy.config import SystemConfig

DAY_S = 86400


def parse_hhmm(s: str) -> int:
    """'HH:MM' -> segundos desde a meia-noite."""
    hh, mm = s.strip().split(":")
    h, m = int(hh), int(mm)
    if not (0 <= h <= 24 and 0 <= m < 60) or (h == 24 and m != 0):
        raise ValueError(f"horário inválido: {s!r}")
    return h * 3600 + m * 60


def in_window(tod_s: float, start: str | int, end: str | int) -> bool:
    """True se tod ∈ [start, end) (com suporte a cruzar a meia-noite)."""
    a = parse_hhmm(start) if isinstance(start, str) else int(start)
    b = parse_hhmm(end) if isinstance(end, str) else int(end)
    t = tod_s % DAY_S
    if a == b:
        return False
    if a < b:
        return a <= t < b
    return t >= a or t < b


def in_any_window(tod_s: float, windows: Iterable[Sequence[str]]) -> bool:
    return any(in_window(tod_s, w[0], w[1]) for w in windows)


def demand_at(cfg: SystemConfig, load_key: str, tod_s: float) -> bool:
    """Pedido (demanda) da carga ``load_key`` no instante ``tod_s`` segundo o perfil programado.

    Cargas sem agenda em ``demand.schedule`` (ex.: deslocável) retornam False;
    a demanda da carga deslocável é tratada pela energia diária pendente.
    """
    schedule = cfg.raw["demand"]["schedule"]
    windows = schedule.get(load_key)
    if not windows:
        return False
    return in_any_window(tod_s, windows)


def local_tz(cfg: SystemConfig) -> timezone:
    """Fuso fixo UTC-3 (America/Sao_Paulo sem horário de verão desde 2019)."""
    return timezone(timedelta(hours=float(cfg.raw["site"]["utc_offset_h"])))


def tod_from_epoch_ms(ts_ms: int, cfg: SystemConfig) -> float:
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=local_tz(cfg))
    return dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1e6


def local_day_from_epoch_ms(ts_ms: int, cfg: SystemConfig) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=local_tz(cfg)).strftime("%Y-%m-%d")


def local_midnight_epoch_ms(date_str: str, cfg: SystemConfig) -> int:
    """'AAAA-MM-DD' (hora local) -> epoch ms UTC da meia-noite local."""
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=local_tz(cfg))
    return int(d.timestamp() * 1000)
