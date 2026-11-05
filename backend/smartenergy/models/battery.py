"""Modelo da bateria LiFePO4 4S com BMS (simulador).

Modelo de circuito equivalente mínimo (docs/06 §6)::

    V = OCV(SOC) − I·R           (I > 0 = descarga; I < 0 = carga)

* ``OCV(SOC)`` é interpolada linearmente na curva típica ``battery.ocv_curve``
  (repouso, 25 °C — curva de literatura, PENDENTE de medição).
* Corrente a partir da potência nos terminais: resolve ``V·I = P`` com
  ``V = OCV − I·R`` → ``R·I² − OCV·I + P = 0`` → ``I = (OCV − √(OCV² − 4RP)) / 2R``
  (vale para descarga, P > 0, e para carga, P < 0).
* Aceitação de carga CC-CV aproximada: limite de corrente
  ``I_lim = min(mppt.max_charge_current_a, battery.max_charge_current_a)``;
  acima de SOC 0,95 o limite decai linearmente até 0 em SOC 1,0 e, quando o
  carregador está limitando (fase CV), a tensão terminal fica regulada em
  ``mppt.absorption_v``. A diferença entre essa tensão e ``OCV + I·R`` é
  tratada como perda de polarização (não armazenada): o SOC é integrado por
  contagem de Coulomb com ``coulombic_efficiency``.
* BMS: abre (desconecta a bateria) se a descarga exigir ``V < bms_uvp_v``,
  corrente acima de ``max_discharge_current_a`` ou SOC abaixo de zero.
  Carga bloqueada abaixo de ``charge_min_temp_c``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import sqrt

import numpy as np

TAPER_START_SOC = 0.95
"""SOC a partir do qual a aceitação de corrente decai linearmente (fase CV aproximada)."""


@dataclass(frozen=True)
class BatteryParams:
    capacity_ah: float
    nominal_v: float
    coulombic_efficiency: float
    r_internal_ohm: float
    max_charge_current_a: float
    max_discharge_current_a: float
    bms_ovp_v: float
    bms_uvp_v: float
    charge_min_temp_c: float
    ocv_soc: tuple[float, ...]
    ocv_v: tuple[float, ...]
    charger_max_current_a: float     # mppt.max_charge_current_a
    absorption_v: float              # mppt.absorption_v
    taper_start_soc: float = TAPER_START_SOC

    @classmethod
    def from_config(cls, cfg) -> "BatteryParams":
        b, m = cfg.raw["battery"], cfg.raw["mppt"]
        return cls(
            capacity_ah=float(b["capacity_ah"]),
            nominal_v=float(b["nominal_v"]),
            coulombic_efficiency=float(b["coulombic_efficiency"]),
            r_internal_ohm=float(b["r_internal_ohm"]),
            max_charge_current_a=float(b["max_charge_current_a"]),
            max_discharge_current_a=float(b["max_discharge_current_a"]),
            bms_ovp_v=float(b["bms_ovp_v"]),
            bms_uvp_v=float(b["bms_uvp_v"]),
            charge_min_temp_c=float(b.get("charge_min_temp_c", 0.0)),
            ocv_soc=tuple(float(x) for x in b["ocv_curve"]["soc"]),
            ocv_v=tuple(float(x) for x in b["ocv_curve"]["v"]),
            charger_max_current_a=float(m["max_charge_current_a"]),
            absorption_v=float(m["absorption_v"]),
        )

    @property
    def energy_wh(self) -> float:
        """Energia nominal ``E_nom = nominal_v · capacity_ah`` (Wh)."""
        return self.nominal_v * self.capacity_ah

    @property
    def charge_current_limit_a(self) -> float:
        """Limite de corrente de carga em fase CC = mínimo entre carregador e bateria/BMS."""
        return min(self.charger_max_current_a, self.max_charge_current_a)


@dataclass
class BatteryOperatingPoint:
    """Ponto de operação da bateria em um passo."""

    i_a: float              # + descarga
    v_v: float              # tensão terminal
    p_w: float              # V·I (+ descarga)
    ocv_v: float
    regime: str             # idle | discharge | charge_cc | charge_cv | charge
    limited: bool = False   # carregador limitando (há corte de FV)


@dataclass
class BatteryModel:
    """Estado (SOC verdadeiro) e equações da bateria."""

    params: BatteryParams
    soc: float = 0.6
    _ocv_soc: np.ndarray = field(init=False, repr=False)
    _ocv_v: np.ndarray = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._ocv_soc = np.asarray(self.params.ocv_soc, dtype=np.float64)
        self._ocv_v = np.asarray(self.params.ocv_v, dtype=np.float64)
        if np.any(np.diff(self._ocv_soc) <= 0) or np.any(np.diff(self._ocv_v) < 0):
            raise ValueError("ocv_curve deve ser crescente em SOC e não decrescente em V")
        self.soc = float(min(max(self.soc, 0.0), 1.0))

    @classmethod
    def from_config(cls, cfg, soc: float) -> "BatteryModel":
        return cls(BatteryParams.from_config(cfg), soc=soc)

    # ------------------------------------------------------------------ OCV
    def ocv(self, soc: float | None = None) -> float:
        """Tensão de circuito aberto (V) por interpolação linear da curva."""
        s = self.soc if soc is None else soc
        return float(np.interp(s, self._ocv_soc, self._ocv_v))

    def soc_from_ocv(self, v: float) -> float:
        """Inversa da curva OCV (interpolação linear; patamar plano → baixa confiança)."""
        return float(np.interp(v, self._ocv_v, self._ocv_soc))

    # --------------------------------------------------------- aceitação
    def charge_acceptance_a(self, soc: float | None = None, t_bat_c: float | None = None) -> float:
        """Corrente máxima de carga aceita (A, ≥ 0) — CC até 0,95 e rampa linear até 0 em SOC 1."""
        s = self.soc if soc is None else soc
        if t_bat_c is not None and t_bat_c < self.params.charge_min_temp_c:
            return 0.0
        i_lim = self.params.charge_current_limit_a
        ts = self.params.taper_start_soc
        if s <= ts:
            return i_lim
        return max(0.0, i_lim * (1.0 - s) / (1.0 - ts))

    def charge_limit_a(self, dt_s: float, t_bat_c: float | None = None) -> float:
        """Corrente de carga máxima no passo: aceitação CC-CV e não ultrapassar SOC 1 em ``dt_s``."""
        i_acc = self.charge_acceptance_a(t_bat_c=t_bat_c)
        if dt_s > 0:
            cap_as = self.params.capacity_ah * 3600.0
            i_room = (1.0 - self.soc) * cap_as / (self.params.coulombic_efficiency * dt_s)
            i_acc = min(i_acc, i_room)
        return max(0.0, i_acc)

    # ------------------------------------------------------------ solução
    def current_for_power(self, p_w: float, ocv: float | None = None) -> float | None:
        """Resolve ``V·I = P`` com ``V = OCV − I·R``; retorna I (A, + descarga) ou None se impossível."""
        e = self.ocv() if ocv is None else ocv
        r = self.params.r_internal_ohm
        if r <= 0.0:
            return p_w / e
        disc = e * e - 4.0 * r * p_w
        if disc < 0.0:
            return None
        return (e - sqrt(disc)) / (2.0 * r)

    def solve_charge(self, p_surplus_w: float, dt_s: float, t_bat_c: float | None = None) -> BatteryOperatingPoint:
        """Ponto de operação quando há ``p_surplus_w`` ≥ 0 disponíveis para carga.

        Retorna a corrente de carga (negativa) limitada pela aceitação CC-CV e
        por não ultrapassar SOC = 1 dentro do passo ``dt_s``.
        """
        e = self.ocv()
        r = self.params.r_internal_ohm
        if p_surplus_w <= 0.0:
            return BatteryOperatingPoint(0.0, e, 0.0, e, "idle")
        i_acc = self.charge_limit_a(dt_s, t_bat_c)
        i_free = -(self.current_for_power(-p_surplus_w, e) or 0.0)  # magnitude sem limite
        if i_free <= i_acc:
            v = e + i_free * r
            return BatteryOperatingPoint(-i_free, v, -v * i_free, e, "charge")
        if self.soc < self.params.taper_start_soc:
            v = e + i_acc * r
            return BatteryOperatingPoint(-i_acc, v, -v * i_acc, e, "charge_cc", limited=True)
        v = self.params.absorption_v
        i = min(i_acc, p_surplus_w / v)
        return BatteryOperatingPoint(-i, v, -v * i, e, "charge_cv", limited=i >= i_acc)

    def solve_discharge(self, p_w: float, dt_s: float) -> BatteryOperatingPoint | None:
        """Ponto de operação para fornecer ``p_w`` > 0; None se o BMS abriria."""
        e = self.ocv()
        i = self.current_for_power(p_w, e)
        if i is None:
            return None
        v = e - i * self.params.r_internal_ohm
        if i > self.params.max_discharge_current_a + 1e-12 or v < self.params.bms_uvp_v:
            return None
        if dt_s > 0 and self.soc - i * dt_s / (self.params.capacity_ah * 3600.0) < 0.0:
            return None
        return BatteryOperatingPoint(i, v, v * i, e, "discharge")

    def idle_point(self) -> BatteryOperatingPoint:
        e = self.ocv()
        return BatteryOperatingPoint(0.0, e, 0.0, e, "idle")

    # --------------------------------------------------------- integração
    def integrate(self, i_a: float, dt_s: float) -> float:
        """Atualiza o SOC verdadeiro por contagem de Coulomb; retorna o novo SOC."""
        cap_as = self.params.capacity_ah * 3600.0
        if i_a >= 0.0:
            self.soc -= i_a * dt_s / cap_as
        else:
            self.soc += self.params.coulombic_efficiency * (-i_a) * dt_s / cap_as
        self.soc = float(min(max(self.soc, 0.0), 1.0))
        return self.soc
