"""Previsão por persistência do índice de céu claro (funciona no hardware, sem internet).

Modelo::

    P̂(k) = P_cs(k) · [ k_clim + (k_obs − k_clim) · exp(−lead_k / τ) ]

* ``P_cs(k)`` — potência FV de **céu claro do modelo** (Haurwitz + Erbs +
  transposição isotrópica + modelo FV/NOCT com a temperatura ambiente
  sintética de ``simulation.weather``), média do passo ``k``;
* ``k_obs`` — índice observado = Σ P_medida / Σ P_cs na última janela
  ``forecast.clearsky_persistence.window_s`` (só quando o céu claro da janela
  for significativo: média ≥ 5 % de ``p_stc_w``; senão usa ``k_clim``);
* ``k_clim`` — ``climatology_k``; ``τ`` — ``blend_tau_h``; ``lead_k`` = tempo
  entre o instante da previsão e o meio do passo ``k``.

Limitação conhecida: a potência medida na entrada do MPPT é **cortada** quando a
bateria está cheia; nesse caso ``k_obs`` subestima o céu real. Se a potência
disponível for conhecida (``observe(..., p_pv_avail_w=...)``, ex.: estimada por
piranômetro), ela é usada no lugar da medida.
"""

from __future__ import annotations

from collections import deque
from math import ceil
from typing import Any

import numpy as np

from smartenergy.config import SystemConfig
from smartenergy.forecast.base import BaseForecast
from smartenergy.models.pv import PvModel
from smartenergy.models.solar import SolarSite
from smartenergy.models.weather import ambient_temperature_cfg

MIN_CLEAR_FRACTION = 0.05
"""Fração mínima de ``p_stc_w`` (média de céu claro na janela) para estimar ``k_obs``."""

K_OBS_MAX = 1.2
"""Limite superior do índice observado (realce por bordas de nuvem)."""

SUBSAMPLE_S = 300.0
"""Espaçamento máximo das subamostras usadas na média de céu claro de cada passo."""


class ClearSkyPv:
    """Potência FV de céu claro do modelo na entrada do MPPT (W)."""

    def __init__(self, cfg: SystemConfig) -> None:
        self.cfg = cfg
        self.site = SolarSite.from_config(cfg)
        self.pv = PvModel.from_config(cfg)
        self.utc_offset_s = float(cfg.raw["site"]["utc_offset_h"]) * 3600.0

    def power(self, ts_ms: np.ndarray) -> np.ndarray:
        ts = np.atleast_1d(np.asarray(ts_ms, dtype=np.float64))
        cs = self.site.clearsky(ts)
        tod = np.mod(ts / 1000.0 + self.utc_offset_s, 86400.0)
        t_amb = ambient_temperature_cfg(self.cfg, tod)
        return self.pv.power(cs.poa_clear, self.pv.cell_temperature(cs.poa_clear, t_amb))

    def interval_means(self, t0_ms: int, step_s: float, n: int) -> np.ndarray:
        n_sub = max(1, int(ceil(float(step_s) / SUBSAMPLE_S - 1e-9)))
        offs = (np.arange(n_sub) + 0.5) * float(step_s) * 1000.0 / n_sub
        starts = float(t0_ms) + np.arange(n) * float(step_s) * 1000.0
        return self.power((starts[:, None] + offs[None, :]).ravel()).reshape(n, n_sub).mean(axis=1)


class ClearSkyPersistenceForecast(BaseForecast):
    """Céu claro × índice persistido, relaxando para a climatologia."""

    method = "clearsky_persistence"

    def __init__(
        self,
        cfg: SystemConfig,
        window_s: float | None = None,
        blend_tau_h: float | None = None,
        climatology_k: float | None = None,
    ) -> None:
        super().__init__(cfg)
        p = cfg.raw["forecast"]["clearsky_persistence"]
        self.window_s = float(p["window_s"] if window_s is None else window_s)
        self.blend_tau_h = float(p["blend_tau_h"] if blend_tau_h is None else blend_tau_h)
        self.climatology_k = float(p["climatology_k"] if climatology_k is None else climatology_k)
        self.clear = ClearSkyPv(cfg)
        self.p_stc = float(cfg.raw["pv"]["p_stc_w"])
        self._obs: deque[tuple[int, float]] = deque()

    def observe(self, ts_ms: int, p_pv_w: float, p_pv_avail_w: float | None = None) -> None:
        """Registra a potência observada (usa a disponível, se informada) e descarta o que saiu da janela."""
        p = p_pv_avail_w if p_pv_avail_w is not None else p_pv_w
        if p is None or not np.isfinite(p):
            return
        self._obs.append((int(ts_ms), max(float(p), 0.0)))
        limit = int(ts_ms) - int(self.window_s * 1000)
        while self._obs and self._obs[0][0] <= limit:
            self._obs.popleft()

    def observed_index(self, now_ms: int | None = None) -> float | None:
        """Índice de céu claro observado na janela; ``None`` se não houver sol suficiente."""
        if not self._obs:
            return None
        if now_ms is not None:
            limit = int(now_ms) - int(self.window_s * 1000)
            obs = [o for o in self._obs if o[0] > limit]
        else:
            obs = list(self._obs)
        if not obs:
            return None
        ts = np.array([o[0] for o in obs], dtype=np.float64)
        pm = np.array([o[1] for o in obs], dtype=np.float64)
        pcs = self.clear.power(ts)
        if pcs.mean() < MIN_CLEAR_FRACTION * self.p_stc:
            return None
        return float(np.clip(pm.sum() / pcs.sum(), 0.0, K_OBS_MAX))

    def pv_forecast(self, t0_ms: int, step_s: int, n: int) -> list[float]:
        p_cs = self.clear.interval_means(t0_ms, step_s, n)
        k_obs = self.observed_index(t0_ms)
        k_clim = self.climatology_k
        if k_obs is None:
            k = np.full(n, k_clim)
        else:
            lead_h = (np.arange(n) + 0.5) * float(step_s) / 3600.0
            tau = max(self.blend_tau_h, 1e-9)
            k = k_clim + (k_obs - k_clim) * np.exp(-lead_h / tau)
        return [float(x) for x in np.maximum(p_cs * k, 0.0)]

    def describe(self) -> dict[str, Any]:
        return {"method": self.method, "window_s": self.window_s, "blend_tau_h": self.blend_tau_h,
                "climatology_k": self.climatology_k}

