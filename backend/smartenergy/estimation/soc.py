"""Estimador de SOC por contagem de Coulomb com sincronizações (docs/06 §1).

Espelho EXATO do estimador do firmware: a equivalência é verificada pelos
vetores golden gerados em ``smartenergy.experiments.vectors``. Qualquer
mudança aqui deve ser replicada no firmware (e os vetores, regenerados).

Convenções: corrente ``I`` positiva = DESCARGA; SOC em fração (0..1);
``dt`` em segundos; tensões em V.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from smartenergy.config import SystemConfig

#: Teto da incerteza reportada (docs/06 §1: ``unc = min(0.5, ...)``).
UNC_MAX = 0.5
#: Acréscimo de incerteza quando o sensor volta após ter sido declarado inválido.
SENSOR_RETURN_UNC_PENALTY = 0.05

SYNC_SOURCES = ("none", "ocv_init", "persisted", "full", "empty", "manual")

Event = tuple[str, str, str]  # (code, level, msg)


def ocv_inverse(v: float, ocv_soc: Sequence[float], ocv_v: Sequence[float]) -> float:
    """SOC = OCV⁻¹(V) por interpolação linear da curva (tensões crescentes).

    Fora da faixa tabelada o resultado é saturado no primeiro/último ponto.
    Implementação deliberadamente simples (mesma do firmware).
    """
    n = len(ocv_v)
    if n == 0 or n != len(ocv_soc):
        raise ValueError("curva OCV inválida")
    if v <= ocv_v[0]:
        return float(ocv_soc[0])
    if v >= ocv_v[n - 1]:
        return float(ocv_soc[n - 1])
    for i in range(1, n):
        if v < ocv_v[i]:
            v0, v1 = ocv_v[i - 1], ocv_v[i]
            s0, s1 = ocv_soc[i - 1], ocv_soc[i]
            return float(s0 + (s1 - s0) * (v - v0) / (v1 - v0))
    return float(ocv_soc[n - 1])  # pragma: no cover - inalcançável


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


@dataclass(frozen=True)
class SocParams:
    """Parâmetros do estimador extraídos de ``config/system.yaml``."""

    capacity_ah: float
    coulombic_efficiency: float
    r_internal_ohm: float
    current_deadband_a: float
    full_voltage_v: float
    full_tail_current_a: float
    full_hold_s: float
    empty_voltage_v: float
    empty_hold_s: float
    empty_soc_value: float
    unknown_initial_uncertainty: float
    sync_uncertainty: float
    drift_uncertainty_per_ah: float
    sensor_timeout_s: float
    ocv_soc: tuple[float, ...]
    ocv_v: tuple[float, ...]

    @classmethod
    def from_config(cls, cfg: SystemConfig) -> "SocParams":
        r = cfg.raw
        b, se, sf = r["battery"], r["soc_estimator"], r["safety"]
        return cls(
            capacity_ah=float(b["capacity_ah"]),
            coulombic_efficiency=float(b["coulombic_efficiency"]),
            r_internal_ohm=float(b["r_internal_ohm"]),
            current_deadband_a=float(se["current_deadband_a"]),
            full_voltage_v=float(se["full_sync"]["voltage_v"]),
            full_tail_current_a=float(se["full_sync"]["tail_current_a"]),
            full_hold_s=float(se["full_sync"]["hold_s"]),
            empty_voltage_v=float(se["empty_sync"]["voltage_v"]),
            empty_hold_s=float(se["empty_sync"]["hold_s"]),
            empty_soc_value=float(se["empty_sync"]["soc_value"]),
            unknown_initial_uncertainty=float(se["unknown_initial_uncertainty"]),
            sync_uncertainty=float(se["sync_uncertainty"]),
            drift_uncertainty_per_ah=float(se["drift_uncertainty_per_ah"]),
            sensor_timeout_s=float(sf["sensor_timeout_s"]),
            ocv_soc=tuple(float(x) for x in b["ocv_curve"]["soc"]),
            ocv_v=tuple(float(x) for x in b["ocv_curve"]["v"]),
        )


class SocEstimator:
    """Contagem de Coulomb com η_c, zona morta, sincronizações de cheio/vazio e incerteza.

    Inicialização (docs/06 §1, nesta ordem):

    1. ``initial_soc`` informado (valor persistido) → ``last_sync = "persisted"``;
       ``unc_base = initial_unc`` (se ``None``, usa ``sync_uncertainty``).
    2. Senão ``v_init`` informado → ``soc = OCV⁻¹(v_init)``, ``unc_base =
       unknown_initial_uncertainty``, ``last_sync = "ocv_init"``.
    3. Sem nenhum dos dois (uso excepcional) → ``soc = 0.5``, ``unc_base = 0.5``,
       ``last_sync = "none"``.

    Atributos públicos: ``soc``, ``unc``, ``valid``, ``last_sync``,
    ``since_sync_s``, ``ah_since_sync``.
    """

    def __init__(
        self,
        cfg: SystemConfig,
        initial_soc: float | None = None,
        initial_unc: float | None = None,
        v_init: float | None = None,
    ) -> None:
        self.p = SocParams.from_config(cfg)
        self.soc: float = 0.5
        self.unc_base: float = UNC_MAX
        self.last_sync: str = "none"
        self.ah_since_sync: float = 0.0
        self.since_sync_s: float = 0.0
        self.t_full: float = 0.0
        self.t_empty: float = 0.0
        self.t_invalid: float = 0.0
        self.valid: bool = True
        self._full_active = False   # condição de sincronização de cheio ativa (para evento de borda)
        self._empty_active = False
        if initial_soc is not None:
            self.soc = _clamp(float(initial_soc), 0.0, 1.0)
            self.unc_base = float(initial_unc) if initial_unc is not None else self.p.sync_uncertainty
            self.last_sync = "persisted"
        elif v_init is not None:
            self.soc = ocv_inverse(float(v_init), self.p.ocv_soc, self.p.ocv_v)
            self.unc_base = self.p.unknown_initial_uncertainty
            self.last_sync = "ocv_init"

    # ------------------------------------------------------------------
    @property
    def unc(self) -> float:
        """Incerteza do SOC: ``min(0.5, unc_base + drift·ah_since_sync)``."""
        return min(UNC_MAX, self.unc_base + self.p.drift_uncertainty_per_ah * self.ah_since_sync)

    def sync(self, soc: float) -> None:
        """Comando ``sync_soc``: SOC informado manualmente (``last_sync = "manual"``).

        Também zera ``ah_since_sync``/``since_sync_s`` (o desvio acumulado deixa
        de valer após a sincronização, como nas sincronizações automáticas).
        """
        self.soc = _clamp(float(soc), 0.0, 1.0)
        self.unc_base = self.p.sync_uncertainty
        self.last_sync = "manual"
        self.ah_since_sync = 0.0
        self.since_sync_s = 0.0

    def snapshot(self) -> dict[str, float | str | bool]:
        """Estado resumido (telemetria/persistência)."""
        return {
            "soc": self.soc,
            "unc": self.unc,
            "valid": self.valid,
            "last_sync": self.last_sync,
            "ah_since_sync": self.ah_since_sync,
            "since_sync_s": self.since_sync_s,
        }

    # ------------------------------------------------------------------
    def update(
        self, dt_s: float, i_a: float | None, v_v: float | None, sensor_ok: bool = True
    ) -> list[Event]:
        """Processa uma amostra. Retorna eventos gerados (somente nas bordas)."""
        p = self.p
        events: list[Event] = []
        dt = float(dt_s)

        if not sensor_ok or i_a is None or v_v is None:
            # Integração suspensa; temporizadores de sincronização reiniciados.
            self.t_invalid += dt
            self.t_full = 0.0
            self.t_empty = 0.0
            self._full_active = False
            self._empty_active = False
            if self.valid and self.t_invalid > p.sensor_timeout_s:
                self.valid = False
            return events

        if not self.valid:
            # Sensor voltou após ter sido declarado inválido.
            self.valid = True
            self.unc_base = min(UNC_MAX, self.unc_base + SENSOR_RETURN_UNC_PENALTY)
        self.t_invalid = 0.0

        i = float(i_a)
        v = float(v_v)
        i_eff = 0.0 if abs(i) < p.current_deadband_a else i
        if i_eff > 0.0:
            self.soc -= i_eff * dt / (3600.0 * p.capacity_ah)
        else:
            self.soc += p.coulombic_efficiency * (-i_eff) * dt / (3600.0 * p.capacity_ah)
        self.ah_since_sync += abs(i_eff) * dt / 3600.0
        self.since_sync_s += dt
        self.soc = _clamp(self.soc, 0.0, 1.0)

        # --- sincronização de cheio -------------------------------------
        if v >= p.full_voltage_v and abs(i) <= p.full_tail_current_a:
            self.t_full += dt
        else:
            self.t_full = 0.0
        if self.t_full >= p.full_hold_s:
            self.soc = 1.0
            self.unc_base = p.sync_uncertainty
            self.ah_since_sync = 0.0
            self.since_sync_s = 0.0
            self.last_sync = "full"
            if not self._full_active:
                events.append(("SOC_SYNC_FULL", "info", f"SOC sincronizado em 100 % (V={v:.2f} V, I={i:.3f} A)"))
            self._full_active = True
        else:
            self._full_active = False

        # --- sincronização de vazio (tensão compensada) ------------------
        vc = v + i * p.r_internal_ohm
        if vc <= p.empty_voltage_v:
            self.t_empty += dt
        else:
            self.t_empty = 0.0
        if self.t_empty >= p.empty_hold_s:
            self.soc = min(self.soc, p.empty_soc_value)
            self.unc_base = p.sync_uncertainty
            self.ah_since_sync = 0.0
            self.since_sync_s = 0.0
            self.last_sync = "empty"
            if not self._empty_active:
                events.append(
                    ("SOC_SYNC_EMPTY", "warn", f"SOC limitado a {p.empty_soc_value:.2f} (Vc={vc:.2f} V)")
                )
            self._empty_active = True
        else:
            self._empty_active = False
        return events
