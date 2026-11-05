"""Previsão ruidosa: verdadeira × (1 + erro relativo AR(1)) — SOMENTE simulação.

O erro relativo ``e`` é um processo AR(1) estacionário com desvio
``forecast.noisy.rel_rmse`` e coeficiente ``forecast.noisy.ar1_phi`` por passo de
``ems.predictive.step_s``, definido sobre o **tempo absoluto** (instante-alvo):
previsões emitidas em instantes diferentes concordam sobre o erro de um mesmo
intervalo, como ocorre com um produto de previsão numérica com viés
persistente. O processo é gerado uma única vez a partir de uma semente própria
(fluxo ``forecast/noisy``), independente do clima e dos sensores, de modo que o
resultado não depende da ordem das consultas.

A previsão é limitada a ``≥ 0``.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from smartenergy.config import SystemConfig
from smartenergy.forecast.base import BaseForecast, WeatherSeries
from smartenergy.models.weather import make_rng


def ar1_series(rng: np.random.Generator, n: int, std: float, phi: float) -> np.ndarray:
    """AR(1) estacionário de média zero, desvio ``std`` e coeficiente ``phi``."""
    eps = rng.standard_normal(n)
    x = np.empty(n)
    s_innov = std * np.sqrt(max(1.0 - phi * phi, 0.0))
    x[0] = std * eps[0]
    for k in range(1, n):
        x[k] = phi * x[k - 1] + s_innov * eps[k]
    return x


class NoisyForecast(BaseForecast):
    """Previsão perfeita corrompida por erro multiplicativo AR(1) determinístico."""

    method = "noisy"

    def __init__(
        self,
        cfg: SystemConfig,
        weather: pd.DataFrame,
        seed: int = 1,
        rel_rmse: float | None = None,
        phi: float | None = None,
        error_step_s: float | None = None,
    ) -> None:
        super().__init__(cfg)
        p = cfg.raw["forecast"]["noisy"]
        self.rel_rmse = float(p["rel_rmse"] if rel_rmse is None else rel_rmse)
        self.phi = float(p["ar1_phi"] if phi is None else phi)
        self.error_step_s = float(error_step_s or cfg.raw["ems"]["predictive"]["step_s"])
        self.seed = int(seed)
        self.truth = WeatherSeries(weather["t"].to_numpy(), weather["p_pv_avail"].to_numpy())
        n_err = int(np.ceil((self.truth.t_end - self.truth.t_start) / (self.error_step_s * 1000.0))) + 1
        rng = make_rng(seed, "forecast/noisy")
        err_t = self.truth.t_start + np.arange(n_err, dtype=np.int64) * int(self.error_step_s * 1000)
        self.error = WeatherSeries(err_t, ar1_series(rng, n_err, self.rel_rmse, self.phi))

    def pv_forecast(self, t0_ms: int, step_s: int, n: int) -> list[float]:
        true_means = self.truth.interval_means(t0_ms, step_s, n)
        mids = float(t0_ms) + (np.arange(n) + 0.5) * float(step_s) * 1000.0
        e = self.error.value_at(mids)
        return [float(x) for x in np.maximum(true_means * (1.0 + e), 0.0)]

    def describe(self) -> dict[str, Any]:
        return {"method": self.method, "simulation_only": True, "rel_rmse": self.rel_rmse,
                "ar1_phi": self.phi, "seed": self.seed}
