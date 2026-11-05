"""Fábrica de estratégias por nome (importação preguiçosa)."""

from __future__ import annotations

import importlib
from typing import Any

from smartenergy.config import SystemConfig
from smartenergy.ems.base import Strategy

_STRATEGIES: dict[str, tuple[str, str]] = {
    "conventional": ("smartenergy.ems.conventional", "ConventionalStrategy"),
    "rules": ("smartenergy.ems.rules", "RuleBasedStrategy"),
    "predictive": ("smartenergy.ems.predictive", "PredictiveStrategy"),
}


def available_strategies() -> list[str]:
    return list(_STRATEGIES)


def get_strategy(name: str, cfg: SystemConfig, **kwargs: Any) -> Strategy:
    """Instancia a estratégia ``name`` com a configuração ``cfg``.

    kwargs extras são repassados ao construtor (ex.: ``forecast_method`` no preditivo).
    """
    try:
        module_name, cls_name = _STRATEGIES[name]
    except KeyError as exc:
        raise ValueError(f"estratégia desconhecida: {name!r}; opções: {available_strategies()}") from exc
    cls = getattr(importlib.import_module(module_name), cls_name)
    return cls(cfg, **kwargs)
