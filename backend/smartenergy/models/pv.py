"""Modelo do gerador fotovoltaico (potência disponível e ponto de operação).

Potência máxima disponível (docs/06 §6)::

    Tc = Ta + (NOCT − 20)/800 · G_poa
    P  = p_stc · (G_poa/1000) · (1 + γ·(Tc − 25)) · derate

O ponto de operação (V, I) é usado apenas para compor as medições simuladas
da entrada FV (``pv_v``, ``pv_i``). É uma **aproximação**: Voc varia com a
temperatura (coeficiente ``beta_voc_per_c``) e com o logaritmo da irradiância;
``Vmp = Voc·(vmp/voc)``. Quando o MPPT puxa menos que a potência disponível
(corte por bateria cheia), a tensão de operação se desloca linearmente de Vmp
para Voc. Os parâmetros elétricos são os do ``system.yaml`` (PENDENTES de
datasheet); o fator logarítmico ``VOC_LOG_COEF`` é uma hipótese de modelagem
(≈ n·Ns·kT/q / Voc para 36 células), não uma especificação do módulo.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import ArrayLike, NDArray

VOC_LOG_COEF = 0.056
"""Sensibilidade relativa de Voc a ln(G/1000) (hipótese de modelagem, adimensional)."""

MIN_IRRADIANCE_W_M2 = 1.0
"""Abaixo disto o módulo é considerado no escuro (V = I = P = 0)."""


@dataclass(frozen=True)
class PvParams:
    """Parâmetros do módulo (seção ``pv`` do system.yaml)."""

    p_stc_w: float
    vmp_v: float
    imp_a: float
    voc_v: float
    isc_a: float
    gamma_pmp_per_c: float
    beta_voc_per_c: float
    noct_c: float
    derate: float

    @classmethod
    def from_config(cls, cfg) -> "PvParams":
        pv = cfg.raw["pv"]
        return cls(
            p_stc_w=float(pv["p_stc_w"]),
            vmp_v=float(pv["vmp_v"]),
            imp_a=float(pv["imp_a"]),
            voc_v=float(pv["voc_v"]),
            isc_a=float(pv["isc_a"]),
            gamma_pmp_per_c=float(pv["gamma_pmp_per_c"]),
            beta_voc_per_c=float(pv["beta_voc_per_c"]),
            noct_c=float(pv["noct_c"]),
            derate=float(pv["derate"]),
        )


class PvModel:
    """Gerador FV: potência disponível no ponto de máxima potência e ponto de operação."""

    def __init__(self, params: PvParams) -> None:
        self.p = params

    @classmethod
    def from_config(cls, cfg) -> "PvModel":
        return cls(PvParams.from_config(cfg))

    def cell_temperature(self, g_poa: ArrayLike, t_amb_c: ArrayLike) -> NDArray[np.float64]:
        """Temperatura de célula pelo modelo NOCT: ``Ta + (NOCT − 20)/800·G`` (°C)."""
        g = np.asarray(g_poa, dtype=np.float64)
        return np.asarray(t_amb_c, dtype=np.float64) + (self.p.noct_c - 20.0) / 800.0 * g

    def power(self, g_poa: ArrayLike, t_cell_c: ArrayLike) -> NDArray[np.float64]:
        """Potência máxima disponível na saída do módulo (= entrada do MPPT), W, ≥ 0."""
        g = np.maximum(np.asarray(g_poa, dtype=np.float64), 0.0)
        tc = np.asarray(t_cell_c, dtype=np.float64)
        p = self.p.p_stc_w * (g / 1000.0) * (1.0 + self.p.gamma_pmp_per_c * (tc - 25.0)) * self.p.derate
        return np.maximum(p, 0.0)

    def voc(self, g_poa: float, t_cell_c: float) -> float:
        """Tensão de circuito aberto aproximada (V); 0 no escuro."""
        if g_poa < MIN_IRRADIANCE_W_M2:
            return 0.0
        v = self.p.voc_v * (1.0 + self.p.beta_voc_per_c * (t_cell_c - 25.0))
        v *= 1.0 + VOC_LOG_COEF * float(np.log(g_poa / 1000.0))
        return max(v, 0.0)

    def operating_point(self, g_poa: float, t_cell_c: float, p_avail_w: float, p_drawn_w: float) -> tuple[float, float]:
        """Ponto de operação (V, I) quando o MPPT puxa ``p_drawn_w`` de ``p_avail_w`` disponíveis.

        Aproximação: em MPP, ``V = Vmp(G, T)``; com corte, ``V`` sobe linearmente
        até ``Voc`` conforme a fração não aproveitada. ``I = P/V``.
        """
        voc = self.voc(g_poa, t_cell_c)
        if voc <= 0.0:
            return 0.0, 0.0
        vmp = voc * (self.p.vmp_v / self.p.voc_v)
        if p_avail_w <= 0.0 or p_drawn_w <= 0.0:
            return voc, 0.0
        frac_unused = min(max(1.0 - p_drawn_w / p_avail_w, 0.0), 1.0)
        v = vmp + (voc - vmp) * frac_unused
        return v, p_drawn_w / v
