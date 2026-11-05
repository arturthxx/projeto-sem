"""Fábrica de provedores de previsão: ``make_forecast(method, cfg, weather=None, seed=1)``.

Ponto de entrada estável usado pelas estratégias (ex.: ``ems.predictive``) e pelo
simulador; a implementação vive em :mod:`smartenergy.forecast.base`.
"""

from __future__ import annotations

from smartenergy.forecast.base import (
    FORECAST_METHODS,
    SIMULATION_ONLY_METHODS,
    BaseForecast,
    ForecastUnavailableError,
    make_forecast,
)
from smartenergy.forecast.demand import ScheduleDemandForecast, make_demand_forecast

__all__ = [
    "FORECAST_METHODS",
    "SIMULATION_ONLY_METHODS",
    "BaseForecast",
    "ForecastUnavailableError",
    "ScheduleDemandForecast",
    "make_demand_forecast",
    "make_forecast",
]
