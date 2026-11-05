"""Base comum e fábrica dos provedores de previsão (``ForecastProvider`` de ``ems/base.py``).

Todos os provedores retornam a potência FV **disponível na ENTRADA do MPPT**
(W, média de cada passo ``[t0 + k·step, t0 + (k+1)·step)``); a conversão para a
saída do MPPT (× ``mppt.efficiency``) é responsabilidade do consumidor (MPC).
A previsão de demanda é sempre delegada ao perfil programado
(:class:`~smartenergy.forecast.demand.ScheduleDemandForecast`).

Métodos (``forecast.pv_method``):

* ``perfect``              — potência disponível verdadeira do clima simulado (SÓ simulação);
* ``noisy``                — verdadeira × (1 + erro AR(1)) (SÓ simulação);
* ``clearsky_persistence`` — céu claro do modelo × índice observado, decaindo para a climatologia;
* ``open_meteo``           — irradiância no plano prevista pela API Open-Meteo (requer internet).
"""

from __future__ import annotations

from math import ceil
from typing import Any

import numpy as np
import pandas as pd
from numpy.typing import NDArray

from smartenergy.config import SystemConfig
from smartenergy.forecast.demand import ScheduleDemandForecast, make_demand_forecast

FORECAST_METHODS = ("perfect", "noisy", "clearsky_persistence", "open_meteo")
"""Métodos de previsão FV disponíveis."""

SIMULATION_ONLY_METHODS = ("perfect", "noisy")
"""Métodos que dependem do clima simulado (não existem no hardware)."""

DAY_MS = 86_400_000


class ForecastUnavailableError(RuntimeError):
    """A previsão não pôde ser obtida (ex.: falha de rede na Open-Meteo)."""


class BaseForecast:
    """Implementação parcial de ``ForecastProvider``: demanda programada e ``observe`` vazio."""

    method = "base"

    def __init__(self, cfg: SystemConfig, demand: ScheduleDemandForecast | None = None) -> None:
        self.cfg = cfg
        self.demand = demand or make_demand_forecast(cfg)

    def observe(self, ts_ms: int, p_pv_w: float, p_pv_avail_w: float | None = None) -> None:
        """Medição recente (ignorada pelos métodos que não usam persistência)."""

    def pv_forecast(self, t0_ms: int, step_s: int, n: int) -> list[float]:  # pragma: no cover - abstrato
        raise NotImplementedError

    def demand_forecast(self, t0_ms: int, step_s: int, n: int) -> dict[str, list[float]]:
        """Fração (0..1) de cada passo em que cada carga não deslocável é pedida."""
        return self.demand.demand_forecast(t0_ms, step_s, n)

    def describe(self) -> dict[str, Any]:
        """Metadados do provedor (para ``meta`` e para a API ``/api/forecast``)."""
        return {"method": self.method}


class WeatherSeries:
    """Série uniforme (grade ``dt``) com consulta de médias por intervalo.

    Cada amostra ``i`` representa o intervalo ``[t_i, t_i + dt)``. Consultas além
    do fim da série são rebatidas em dias inteiros para dentro dela (repetição
    periódica do último dia disponível); antes do início, idem para frente.
    """

    def __init__(self, t_ms: NDArray[np.int64] | pd.Series, values: NDArray[np.float64] | pd.Series) -> None:
        self.t = np.asarray(t_ms, dtype=np.int64)
        self.v = np.asarray(values, dtype=np.float64)
        if self.t.size < 2:
            raise ValueError("série de clima precisa de pelo menos 2 amostras")
        self.dt_ms = int(self.t[1] - self.t[0])
        self.t_start = int(self.t[0])
        self.t_end = int(self.t[-1]) + self.dt_ms  # exclusivo

    def _index(self, ts_ms: NDArray[np.float64]) -> NDArray[np.int64]:
        ts = np.asarray(ts_ms, dtype=np.float64).copy()
        span = self.t_end - self.t_start
        if span >= DAY_MS:
            late = ts >= self.t_end
            if np.any(late):
                ts[late] -= np.ceil((ts[late] - self.t_end + 1) / DAY_MS) * DAY_MS
            early = ts < self.t_start
            if np.any(early):
                ts[early] += np.ceil((self.t_start - ts[early]) / DAY_MS) * DAY_MS
        idx = np.floor((ts - self.t_start) / self.dt_ms).astype(np.int64)
        return np.clip(idx, 0, self.v.size - 1)

    def value_at(self, ts_ms: NDArray[np.float64]) -> NDArray[np.float64]:
        """Valor da amostra que contém cada instante."""
        return self.v[self._index(ts_ms)]

    def interval_means(self, t0_ms: int, step_s: float, n: int) -> NDArray[np.float64]:
        """Média em ``[t0 + k·step, t0 + (k+1)·step)``, k = 0..n−1 (subamostragem na grade ``dt``)."""
        if n <= 0:
            return np.zeros(0)
        step_ms = float(step_s) * 1000.0
        n_sub = max(1, int(ceil(step_ms / self.dt_ms - 1e-9)))
        offs = (np.arange(n_sub) + 0.5) * step_ms / n_sub
        starts = float(t0_ms) + np.arange(n) * step_ms
        samples = self.value_at((starts[:, None] + offs[None, :]).ravel()).reshape(n, n_sub)
        return samples.mean(axis=1)


def make_forecast(
    method: str | None,
    cfg: SystemConfig,
    weather: pd.DataFrame | None = None,
    seed: int = 1,
) -> BaseForecast:
    """Fábrica de provedores de previsão FV (+ demanda programada).

    Args:
        method: ``perfect`` | ``noisy`` | ``clearsky_persistence`` | ``open_meteo``
            (``None`` → ``forecast.pv_method`` do system.yaml).
        cfg: configuração do sistema.
        weather: clima simulado (``models.weather.generate_weather``); obrigatório
            para ``perfect`` e ``noisy``.
        seed: semente do erro do método ``noisy`` (fluxo próprio, independente do clima).

    Raises:
        ValueError: método desconhecido ou clima ausente para métodos de simulação.
    """
    m = method or str(cfg.raw["forecast"]["pv_method"])
    if m in SIMULATION_ONLY_METHODS and weather is None:
        raise ValueError(f"previsão {m!r} só existe em simulação e requer o clima simulado (weather)")
    if m == "perfect":
        from smartenergy.forecast.perfect import PerfectForecast

        return PerfectForecast(cfg, weather)
    if m == "noisy":
        from smartenergy.forecast.noisy import NoisyForecast

        return NoisyForecast(cfg, weather, seed=seed)
    if m == "clearsky_persistence":
        from smartenergy.forecast.clearsky_persistence import ClearSkyPersistenceForecast

        return ClearSkyPersistenceForecast(cfg)
    if m == "open_meteo":
        from smartenergy.forecast.open_meteo import OpenMeteoForecast

        return OpenMeteoForecast(cfg)
    raise ValueError(f"método de previsão desconhecido: {m!r}; opções: {list(FORECAST_METHODS)}")
