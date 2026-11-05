"""Controle baseado em regras com histerese por faixas de SOC (docs/06 §3.2).

Espelho EXATO do EMS por regras do firmware (verificado por vetores golden).

Níveis: NORMAL=3, ECONOMY=2, CRITICAL=1, PROTECTION=0, com limites inferiores
``T3 = normal_min_soc``, ``T2 = economy_min_soc``, ``T1 = critical_min_soc`` e
``T0 = −∞``. A primeira avaliação não usa histerese; depois a descida é
imediata e a subida exige ``soc ≥ T_(nível+1) + H``.

As ações são definidas por PRIORIDADE (genéricas a partir de ``cfg.loads``):

=========== ========= ===================================== ===================
Estado      high      medium                                low não deslocável
=========== ========= ===================================== ===================
NORMAL      on = d    on = d, duty = 1                      on = d
ECONOMY     on = d    on = d, duty = max(eco_duty, min_duty) desligada
CRITICAL    on = d    desligada                             desligada
PROTECTION  desligada desligada                             desligada
=========== ========= ===================================== ===================

Cargas deslocáveis (``deferrable``) usam ``latch`` persistente, economia por
excedente (``p_bat``), prazo (apenas em NORMAL) e tempo mínimo de permanência
(apenas para mudanças discricionárias).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from smartenergy.config import LoadConfig, SystemConfig
from smartenergy.ems.base import STATES, Decision, EmsInputs, ForecastProvider, LoadCommand
from smartenergy.timeutil import DAY_S, in_window, parse_hhmm

LEVEL_NORMAL, LEVEL_ECONOMY, LEVEL_CRITICAL, LEVEL_PROTECTION = 3, 2, 1, 0


@dataclass(frozen=True)
class RulesParams:
    """Parâmetros de ``ems.rules`` (nomes iguais aos do firmware/vetores)."""

    normal_min_soc: float
    economy_min_soc: float
    critical_min_soc: float
    hysteresis_soc: float
    economy_medium_duty: float
    min_dwell_s: float
    surplus_on_soc: float
    surplus_off_soc: float
    economy_surplus_margin_w: float
    deadline_margin_s: float

    @classmethod
    def from_config(cls, cfg: SystemConfig) -> "RulesParams":
        r = cfg.raw["ems"]["rules"]
        d = r["deferrable"]
        return cls(
            normal_min_soc=float(r["normal_min_soc"]),
            economy_min_soc=float(r["economy_min_soc"]),
            critical_min_soc=float(r["critical_min_soc"]),
            hysteresis_soc=float(r["hysteresis_soc"]),
            economy_medium_duty=float(r["economy_medium_duty"]),
            min_dwell_s=float(r["min_dwell_s"]),
            surplus_on_soc=float(d["surplus_on_soc"]),
            surplus_off_soc=float(d["surplus_off_soc"]),
            economy_surplus_margin_w=float(d["economy_surplus_margin_w"]),
            deadline_margin_s=float(d["deadline_margin_s"]),
        )

    def threshold(self, level: int) -> float:
        """Limite inferior ``T_level`` (``T0 = −∞``)."""
        return (-math.inf, self.critical_min_soc, self.economy_min_soc, self.normal_min_soc)[level]


def deferrable_window_s(ld: LoadConfig) -> tuple[int, int]:
    """Janela ``[window_start, deadline)`` da carga deslocável, em s desde a meia-noite."""
    assert ld.deferrable is not None
    return parse_hhmm(ld.deferrable.window_start), parse_hhmm(ld.deferrable.deadline)


def seconds_until(tod_s: float, target_s: float) -> float:
    """Segundos de ``tod`` até o próximo ``target`` (hora local, cruzando a meia-noite)."""
    t = tod_s % DAY_S
    return target_s - t if target_s > t else target_s + DAY_S - t


def initial_level(soc: float, p: RulesParams) -> int:
    """Primeira avaliação: maior nível L tal que ``soc ≥ T_L`` (sem histerese)."""
    for level in (LEVEL_NORMAL, LEVEL_ECONOMY, LEVEL_CRITICAL):
        if soc >= p.threshold(level):
            return level
    return LEVEL_PROTECTION


def next_level(prev: int | None, soc: float, soc_valid: bool, p: RulesParams) -> tuple[int, str]:
    """Atualiza o nível (função pura). Retorna ``(nível, motivo)``."""
    if prev is None:
        if not soc_valid:
            return LEVEL_CRITICAL, "soc_invalid -> CRITICAL"
        lvl = initial_level(soc, p)
        return lvl, f"inicial: soc {soc:.3f} -> {STATES[lvl]}"
    if not soc_valid:
        lvl = min(prev, LEVEL_CRITICAL)
        return lvl, f"soc_invalid -> {STATES[lvl]}"
    lvl = prev
    why = ""
    while lvl > 0 and soc < p.threshold(lvl):
        why = f"soc {soc:.3f} < {p.threshold(lvl):.2f} -> {STATES[lvl - 1]}"
        lvl -= 1
    while lvl < 3 and soc >= p.threshold(lvl + 1) + p.hysteresis_soc:
        why = f"soc {soc:.3f} >= {p.threshold(lvl + 1):.2f}+{p.hysteresis_soc:.2f} -> {STATES[lvl + 1]}"
        lvl += 1
    if lvl == prev:
        why = f"{STATES[lvl]}: soc {soc:.3f}"
    return lvl, why


def non_deferrable_command(ld: LoadConfig, level: int, demand: bool, p: RulesParams) -> LoadCommand:
    """Ação por estado e prioridade para uma carga não deslocável."""
    if ld.priority == "high":
        on = demand and level >= LEVEL_CRITICAL
        duty = 1.0
    elif ld.priority == "medium":
        on = demand and level >= LEVEL_ECONOMY
        if level == LEVEL_ECONOMY and ld.dimmable:
            duty = max(p.economy_medium_duty, ld.min_duty)
        else:
            duty = 1.0
    else:  # low
        on = demand and level == LEVEL_NORMAL
        duty = 1.0
    return LoadCommand(id=ld.id, key=ld.key, on=on, duty=duty if on else 0.0)


class RuleBasedStrategy:
    """EMS por regras (histerese de SOC + carga deslocável com latch)."""

    name = "rules"

    def __init__(self, cfg: SystemConfig, **_: Any) -> None:
        self.cfg = cfg
        self.p = RulesParams.from_config(cfg)
        self._windows = {ld.key: deferrable_window_s(ld) for ld in cfg.loads if ld.deferrable is not None}
        self.level: int | None = None
        self.latch: dict[str, bool] = {}
        self.reset()

    def reset(self) -> None:
        self.level = None
        self.latch = {k: False for k in self._windows}

    @property
    def state(self) -> str | None:
        return None if self.level is None else STATES[self.level]

    # ------------------------------------------------------------------
    def _deferrable(self, ld: LoadConfig, inp: EmsInputs, level: int) -> tuple[LoadCommand, str]:
        p = self.p
        assert ld.deferrable is not None
        key = ld.key
        st = inp.outputs.get(key)
        cur_on = bool(st.on) if st is not None else False
        last_sw = float(st.last_switch_t_s) if st is not None else -math.inf
        done = float(inp.deferrable_done_wh.get(key, 0.0))
        remaining = max(0.0, ld.deferrable.daily_energy_wh - done)
        ws, dl = self._windows[key]
        inside = in_window(inp.tod_s, ws, dl)

        if remaining <= 0.0 or not inside or level <= LEVEL_CRITICAL:
            # Desligamentos imediatos (sem tempo mínimo de permanência).
            self.latch[key] = False
            if remaining <= 0.0:
                why = f"{key}: energia diaria concluida"
            elif not inside:
                why = f"{key}: fora da janela"
            else:
                why = f"{key}: desligada em {STATES[level]}"
            return LoadCommand(id=ld.id, key=key, on=False, duty=0.0), why

        if level == LEVEL_NORMAL:
            if inp.soc >= p.surplus_on_soc:
                self.latch[key] = True
            if inp.soc < p.surplus_off_soc:
                self.latch[key] = False
            t_need = remaining / ld.p_nom_w * 3600.0
            t_left = seconds_until(inp.tod_s, dl)
            force = t_left <= t_need + p.deadline_margin_s
            want = self.latch[key] or force
            if force:
                why = f"{key}: prazo ({t_left:.0f} s <= {t_need:.0f}+{p.deadline_margin_s:.0f} s)"
            elif self.latch[key]:
                why = f"{key}: excedente soc>={p.surplus_on_soc:.2f}"
            else:
                why = f"{key}: aguardando excedente (soc {inp.soc:.3f})"
        else:  # ECONOMY
            if not cur_on:
                self.latch[key] = (-inp.p_bat) >= ld.p_nom_w + p.economy_surplus_margin_w
            else:
                self.latch[key] = inp.p_bat <= p.economy_surplus_margin_w
            want = self.latch[key]
            if want:
                why = f"{key}: excedente p_bat={inp.p_bat:.1f} W (ECONOMY)"
            else:
                why = f"{key}: sem excedente p_bat={inp.p_bat:.1f} W (ECONOMY)"

        on = want
        if want != cur_on:
            elapsed = inp.t_s - last_sw
            if not elapsed >= p.min_dwell_s:
                on = cur_on
                why += f"; {key}: permanencia minima ({elapsed:.0f}/{p.min_dwell_s:.0f} s)"
        return LoadCommand(id=ld.id, key=key, on=on, duty=1.0 if on else 0.0), why

    def decide(self, inp: EmsInputs, forecast: ForecastProvider | None = None) -> Decision:
        prev_state = self.state
        level, why_level = next_level(self.level, inp.soc, inp.soc_valid, self.p)
        self.level = level
        commands: dict[str, LoadCommand] = {}
        reasons = [why_level]
        for ld in self.cfg.loads:
            if ld.deferrable is not None:
                cmd, why = self._deferrable(ld, inp, level)
                reasons.append(why)
            else:
                cmd = non_deferrable_command(ld, level, bool(inp.demand.get(ld.key, False)), self.p)
            commands[ld.key] = cmd
        return Decision(
            mode=self.name,
            state=STATES[level],
            commands=commands,
            reason="; ".join(reasons),
            extra={"level": level, "prev_state": prev_state, "latch": dict(self.latch)},
        )
