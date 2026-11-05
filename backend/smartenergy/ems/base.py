"""Tipos e protocolos comuns do EMS (normativo — ver docs/06-especificacao-algoritmos.md).

Estes tipos são o contrato entre o simulador (``smartenergy.simulation``), as
estratégias (``smartenergy.ems.*``), o serviço ao vivo (backend) e os vetores
golden usados nos testes do firmware.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

MODES = ("conventional", "rules", "predictive")
STATES = ("PROTECTION", "CRITICAL", "ECONOMY", "NORMAL")  # índice = nível (0..3)
SAFETY_CODES = ("OK", "BOOT", "LVD", "OVP", "OC", "OC_LOCKOUT", "OT", "SENSOR")


@dataclass
class LoadState:
    """Estado atual de uma saída de carga (após o supervisor de segurança)."""

    id: int
    key: str
    on: bool = False
    duty: float = 0.0
    last_switch_t_s: float = float("-inf")  # instante (tempo monotônico) da última transição on<->off


@dataclass
class EmsInputs:
    """Entradas de uma avaliação do EMS (medições/estimativas — nunca valores 'verdadeiros')."""

    t_s: float                      # tempo monotônico em segundos (permanência mínima, TTL)
    ts_ms: int                      # epoch UTC em ms (0 se a hora não estiver sincronizada)
    tod_s: float                    # segundos desde a meia-noite local
    soc: float                      # SOC estimado (0..1)
    soc_valid: bool
    v_bat: float                    # V
    i_bat: float                    # A (+ descarga)
    p_bat: float                    # W (+ descarga)
    p_pv: float                     # W na entrada FV do MPPT
    p_bus: float                    # W no barramento de cargas
    demand: dict[str, bool]         # pedido do perfil por chave de carga não deslocável (ex.: led, fan)
    deferrable_done_wh: dict[str, float]  # energia já entregue hoje por carga deslocável
    outputs: dict[str, LoadState]   # saídas atuais por chave
    soc_unc: float = 0.0
    t_bat_c: float | None = None


@dataclass
class LoadCommand:
    id: int
    key: str
    on: bool
    duty: float  # 0..1 (1 para cargas não dimerizáveis ligadas; 0 quando desligada)


@dataclass
class Decision:
    mode: str                               # conventional | rules | predictive
    state: str                              # NORMAL | ECONOMY | CRITICAL | PROTECTION
    commands: dict[str, LoadCommand]        # por chave de carga (todas as cargas)
    reason: str
    fallback: bool = False                  # preditivo caiu para regras
    plan: dict[str, Any] | None = None      # plano do MPC (ver docs/05 §5)
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ForecastProvider(Protocol):
    """Fornece previsões para o MPC. Potências FV na ENTRADA do MPPT (W, disponível)."""

    method: str

    def observe(self, ts_ms: int, p_pv_w: float, p_pv_avail_w: float | None = None) -> None:
        """Informa medição recente (para métodos de persistência)."""

    def pv_forecast(self, t0_ms: int, step_s: int, n: int) -> "list[float]":
        """Potência FV média disponível por passo [t0 + k·step, t0 + (k+1)·step), k = 0..n-1."""

    def demand_forecast(self, t0_ms: int, step_s: int, n: int) -> "dict[str, list[float]]":
        """Fração (0..1) de cada passo em que cada carga não deslocável é pedida."""


@runtime_checkable
class Strategy(Protocol):
    name: str

    def reset(self) -> None:
        """Limpa o estado interno (início de experimento)."""

    def decide(self, inp: EmsInputs, forecast: ForecastProvider | None = None) -> Decision:
        """Calcula os comandos de carga para o instante atual."""


def all_off_commands(cfg) -> dict[str, LoadCommand]:
    return {ld.key: LoadCommand(id=ld.id, key=ld.key, on=False, duty=0.0) for ld in cfg.loads}
