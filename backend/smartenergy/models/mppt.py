"""Modelo do controlador de carga MPPT comercial (o EMS não controla o rastreamento).

``P_out = efficiency · P_in − self_consumption`` quando há FV suficiente para
o controlador operar; a saída é limitada pela demanda (cargas + auxiliar +
aceitação de carga da bateria) e o excedente vira corte (``curtail``).

Hipóteses (docs/06 §6):

* O autoconsumo é suprido pelo próprio FV; sem FV suficiente
  (``η·P_disp ≤ autoconsumo``) o controlador fica inativo e não puxa potência
  (nem da bateria — consumo noturno desprezado).
* O limite ``max_charge_current_a`` é aplicado à corrente da **bateria**
  (o modelo Victron real limita a corrente total de saída; diferença pequena
  nesta bancada, documentada como aproximação).
* Rendimento constante (``mppt.efficiency`` é PENDENTE: a curva real depende da potência).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MpptParams:
    efficiency: float
    self_consumption_w: float
    max_charge_current_a: float
    absorption_v: float
    float_v: float

    @classmethod
    def from_config(cls, cfg) -> "MpptParams":
        m = cfg.raw["mppt"]
        return cls(
            efficiency=float(m["efficiency"]),
            self_consumption_w=float(m["self_consumption_w"]),
            max_charge_current_a=float(m["max_charge_current_a"]),
            absorption_v=float(m["absorption_v"]),
            float_v=float(m["float_v"]),
        )


class MpptModel:
    """Conversão entrada FV → saída CC do controlador."""

    def __init__(self, params: MpptParams) -> None:
        self.p = params

    @classmethod
    def from_config(cls, cfg) -> "MpptModel":
        return cls(MpptParams.from_config(cfg))

    def is_active(self, p_in_avail_w: float) -> bool:
        """True se o FV disponível basta para o controlador operar com saída positiva."""
        return self.p.efficiency * p_in_avail_w > self.p.self_consumption_w

    def max_output(self, p_in_avail_w: float) -> float:
        """Potência máxima de saída (W) com o FV disponível ``p_in_avail_w``."""
        if not self.is_active(p_in_avail_w):
            return 0.0
        return self.p.efficiency * p_in_avail_w - self.p.self_consumption_w

    def input_for_output(self, p_out_w: float, p_in_avail_w: float) -> float:
        """Potência puxada na entrada FV para entregar ``p_out_w`` na saída (W)."""
        if not self.is_active(p_in_avail_w):
            return 0.0
        p_in = (max(p_out_w, 0.0) + self.p.self_consumption_w) / self.p.efficiency
        return min(p_in, p_in_avail_w)

    def output_from_input(self, p_in_w: float) -> float:
        """Inverso de :meth:`input_for_output` (usado no fechamento do balanço)."""
        if p_in_w <= 0.0:
            return 0.0
        return max(self.p.efficiency * p_in_w - self.p.self_consumption_w, 0.0)


def mppt_output_from_input(p_in_w, efficiency: float, self_consumption_w: float):
    """Versão vetorizada (numpy/pandas) de :meth:`MpptModel.output_from_input`."""
    import numpy as np

    p = np.asarray(p_in_w, dtype=np.float64)
    return np.where(p > 0.0, np.maximum(efficiency * p - self_consumption_w, 0.0), 0.0)
