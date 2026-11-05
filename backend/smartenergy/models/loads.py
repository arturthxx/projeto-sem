"""Modelo das cargas CC experimentais: ``P = p_nom · (V/v_ref)^k · duty``.

Aproximação de potência dependente da tensão (k = 2 para resistor puro; ~1,5
para LED com driver simples e ventilador), calibrável no Experimento 2. Os
valores de ``p_nom_w`` são PENDENTES (system.yaml).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike, NDArray

from smartenergy.config import LoadConfig


def load_power(load: LoadConfig, v: float, duty: float = 1.0) -> float:
    """Potência da carga (W) na tensão ``v`` com ciclo de trabalho ``duty``."""
    if duty <= 0.0 or v <= 0.0:
        return 0.0
    return load.p_nom_w * (v / load.v_ref_v) ** load.voltage_exponent * min(duty, 1.0)


def load_power_array(load: LoadConfig, v: ArrayLike, duty: ArrayLike) -> NDArray[np.float64]:
    """Versão vetorizada de :func:`load_power`."""
    vv = np.maximum(np.asarray(v, dtype=np.float64), 0.0)
    dd = np.clip(np.asarray(duty, dtype=np.float64), 0.0, 1.0)
    return load.p_nom_w * (vv / load.v_ref_v) ** load.voltage_exponent * dd


def load_current(load: LoadConfig, v: float, duty: float = 1.0) -> float:
    """Corrente média (A) = P/V."""
    if v <= 0.0:
        return 0.0
    return load_power(load, v, duty) / v


@dataclass(frozen=True)
class LoadOutput:
    """Saída física aplicada a uma carga (após o supervisor de segurança)."""

    on: bool
    duty: float

    @property
    def effective_duty(self) -> float:
        return min(max(self.duty, 0.0), 1.0) if self.on else 0.0
