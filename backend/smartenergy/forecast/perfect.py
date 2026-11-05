"""Previsão perfeita: potência FV disponível verdadeira do clima simulado.

Serve como limite superior de desempenho do MPC. Existe **apenas em simulação**
(no hardware o futuro não é conhecido).
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from smartenergy.config import SystemConfig
from smartenergy.forecast.base import BaseForecast, WeatherSeries


class PerfectForecast(BaseForecast):
    """Média exata de ``p_pv_avail`` em cada passo (entrada do MPPT)."""

    method = "perfect"

    def __init__(self, cfg: SystemConfig, weather: pd.DataFrame) -> None:
        super().__init__(cfg)
        self.series = WeatherSeries(weather["t"].to_numpy(), weather["p_pv_avail"].to_numpy())

    def pv_forecast(self, t0_ms: int, step_s: int, n: int) -> list[float]:
        return [float(x) for x in self.series.interval_means(t0_ms, step_s, n)]

    def describe(self) -> dict[str, Any]:
        return {"method": self.method, "simulation_only": True}
