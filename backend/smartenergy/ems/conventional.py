"""Operação convencional de referência (docs/06 §3.1).

Equivale a um controlador comercial com saída LOAD + LVD: as cargas não
deslocáveis seguem o perfil de demanda; a deslocável liga no horário fixo
``demand.conventional.<key>_schedule`` até cumprir a energia diária. Somente o
supervisor de segurança atua (fora desta classe). ``state`` é fixo em
``"NORMAL"`` (apenas informativo).
"""

from __future__ import annotations

from typing import Any

from smartenergy.config import LoadConfig, SystemConfig
from smartenergy.ems.base import Decision, EmsInputs, ForecastProvider, LoadCommand
from smartenergy.timeutil import in_any_window


def conventional_schedule(cfg: SystemConfig, ld: LoadConfig) -> list[list[str]]:
    """Agenda convencional da carga deslocável ``ld`` (``<key>_schedule``).

    Na ausência de agenda específica, usa a própria janela da deslocável.
    """
    conv = cfg.raw.get("demand", {}).get("conventional", {}) or {}
    sched = conv.get(f"{ld.key}_schedule")
    if sched:
        return [list(w) for w in sched]
    assert ld.deferrable is not None
    return [[ld.deferrable.window_start, ld.deferrable.deadline]]


class ConventionalStrategy:
    """Estratégia convencional (sem gerenciamento)."""

    name = "conventional"

    def __init__(self, cfg: SystemConfig, **_: Any) -> None:
        self.cfg = cfg
        self._schedules = {
            ld.key: conventional_schedule(cfg, ld) for ld in cfg.loads if ld.deferrable is not None
        }

    def reset(self) -> None:
        """Sem estado interno."""

    def decide(self, inp: EmsInputs, forecast: ForecastProvider | None = None) -> Decision:
        commands: dict[str, LoadCommand] = {}
        notes: list[str] = []
        for ld in self.cfg.loads:
            if ld.deferrable is not None:
                done = float(inp.deferrable_done_wh.get(ld.key, 0.0))
                in_sched = in_any_window(inp.tod_s, self._schedules[ld.key])
                pending = done < ld.deferrable.daily_energy_wh
                on = in_sched and pending
                if on:
                    notes.append(f"{ld.key}: horario fixo")
                elif in_sched and not pending:
                    notes.append(f"{ld.key}: energia diaria concluida")
            else:
                on = bool(inp.demand.get(ld.key, False))
            commands[ld.key] = LoadCommand(id=ld.id, key=ld.key, on=on, duty=1.0 if on else 0.0)
        reason = "convencional: cargas seguem o perfil de demanda"
        if notes:
            reason += "; " + "; ".join(notes)
        return Decision(mode=self.name, state="NORMAL", commands=commands, reason=reason)
