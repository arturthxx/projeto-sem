"""Previsão de demanda pelo perfil programado (``demand.schedule``) e utilidades de janela.

A demanda das cargas não deslocáveis é o pedido do perfil programado em hora
local, intervalos ``[início, fim)`` (docs/06, notação). A previsão é a
**fração de cada passo** em que a carga é pedida, calculada exatamente por
sobreposição de intervalos (não por amostragem).
"""

from __future__ import annotations

from math import floor

import numpy as np
from numpy.typing import ArrayLike, NDArray

from smartenergy.timeutil import DAY_S, parse_hhmm


def _windows_s(windows) -> list[tuple[int, int]]:
    return [(parse_hhmm(w[0]), parse_hhmm(w[1])) for w in windows or []]


def window_mask(tod_s: ArrayLike, windows) -> NDArray[np.bool_]:
    """Versão vetorizada de ``timeutil.in_any_window`` (``tod`` em s locais)."""
    t = np.mod(np.asarray(tod_s, dtype=np.float64), DAY_S)
    out = np.zeros(t.shape, dtype=bool)
    for a, b in _windows_s(windows):
        if a == b:
            continue
        if a < b:
            out |= (t >= a) & (t < b)
        else:
            out |= (t >= a) | (t < b)
    return out


def demand_mask(cfg, load_key: str, tod_s: ArrayLike) -> NDArray[np.bool_]:
    """Pedido programado da carga ``load_key`` em cada ``tod_s`` (vetorizado; False sem agenda)."""
    windows = cfg.raw["demand"]["schedule"].get(load_key)
    return window_mask(tod_s, windows)


def window_overlap_fraction(tod0_s: float, step_s: float, n: int, windows) -> NDArray[np.float64]:
    """Fração de cada passo ``[tod0 + k·step, tod0 + (k+1)·step)`` coberta pelas janelas.

    ``tod0_s`` é o instante inicial em segundos locais desde a meia-noite do
    dia de referência (pode passar de 86400 — os dias seguintes são tratados).
    """
    if n <= 0:
        return np.zeros(0)
    starts = tod0_s + np.arange(n, dtype=np.float64) * step_s
    ends = starts + step_s
    d0 = int(floor(starts[0] / DAY_S)) - 1
    d1 = int(floor(ends[-1] / DAY_S)) + 1
    intervals: list[tuple[float, float]] = []
    for a, b in _windows_s(windows):
        if a == b:
            continue
        for d in range(d0, d1 + 1):
            base = d * DAY_S
            if a < b:
                intervals.append((base + a, base + b))
            else:
                intervals.append((base + a, base + DAY_S + b))
    if not intervals:
        return np.zeros(n)
    iv = np.asarray(intervals, dtype=np.float64)
    lo = np.maximum(starts[:, None], iv[None, :, 0])
    hi = np.minimum(ends[:, None], iv[None, :, 1])
    covered = np.clip(hi - lo, 0.0, None).sum(axis=1)
    return np.clip(covered / step_s, 0.0, 1.0)


class ScheduleDemandForecast:
    """Demanda prevista = perfil programado (``forecast.demand_method = schedule``)."""

    method = "schedule"

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.utc_offset_s = float(cfg.raw["site"]["utc_offset_h"]) * 3600.0
        self.keys = [ld.key for ld in cfg.loads if ld.deferrable is None]
        self.schedule = cfg.raw["demand"]["schedule"]

    def _tod0(self, t0_ms: int) -> float:
        local_s = t0_ms / 1000.0 + self.utc_offset_s
        return local_s - floor(local_s / DAY_S) * DAY_S

    def demand_forecast(self, t0_ms: int, step_s: int, n: int) -> dict[str, list[float]]:
        """Fração (0..1) de cada passo em que cada carga não deslocável é pedida."""
        tod0 = self._tod0(t0_ms)
        return {
            key: [float(x) for x in window_overlap_fraction(tod0, float(step_s), int(n), self.schedule.get(key))]
            for key in self.keys
        }

    def expected_load_w(self, t0_ms: int, step_s: int, n: int, include_aux: bool = True) -> list[float]:
        """Potência média esperada das cargas não deslocáveis (W, tensão de referência) por passo."""
        fr = self.demand_forecast(t0_ms, step_s, n)
        total = np.zeros(int(n))
        for ld in self.cfg.loads:
            if ld.key in fr:
                total += ld.p_nom_w * np.asarray(fr[ld.key])
        if include_aux:
            total += float(self.cfg.raw["aux"]["p_w"])
        return [float(x) for x in total]


def make_demand_forecast(cfg, method: str | None = None) -> ScheduleDemandForecast:
    """Fábrica da previsão de demanda (``forecast.demand_method``)."""
    m = method or str(cfg.raw["forecast"].get("demand_method", "schedule"))
    if m == "schedule":
        return ScheduleDemandForecast(cfg)
    if m == "historical_mean":
        raise NotImplementedError(
            "demand_method='historical_mean' ainda não implementado (requer histórico medido); use 'schedule'"
        )
    raise ValueError(f"método de previsão de demanda desconhecido: {m!r}")
