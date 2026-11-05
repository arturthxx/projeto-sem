"""Testes do EMS por regras (docs/06 §3.2)."""

from __future__ import annotations

import math

import pytest

from smartenergy.config import load_config
from smartenergy.ems.base import EmsInputs, LoadState
from smartenergy.ems.registry import get_strategy
from smartenergy.ems.rules import RuleBasedStrategy, RulesParams, next_level, seconds_until

H = 3600
EVE = 19 * H + 1800  # led e fan pedidos


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def rp(cfg):
    return RulesParams.from_config(cfg)


class Harness:
    """Harness dos vetores golden: saídas = comandos; last_switch atualizado nas transições."""

    def __init__(self, cfg, **kw):
        self.cfg = cfg
        self.s = RuleBasedStrategy(cfg, **kw)
        self.out = {ld.key: LoadState(ld.id, ld.key) for ld in cfg.loads}

    def step(self, t, tod, soc, valid=True, p_bat=0.0, led=True, fan=True, done=0.0):
        inp = EmsInputs(
            t_s=t, ts_ms=0, tod_s=tod, soc=soc, soc_valid=valid, v_bat=13.0, i_bat=0.0, p_bat=p_bat,
            p_pv=0.0, p_bus=0.0, demand={"led": led, "fan": fan}, deferrable_done_wh={"res": done},
            outputs={k: LoadState(v.id, v.key, v.on, v.duty, v.last_switch_t_s) for k, v in self.out.items()},
        )
        dec = self.s.decide(inp)
        for k, c in dec.commands.items():
            if c.on != self.out[k].on:
                self.out[k].last_switch_t_s = t
            self.out[k].on, self.out[k].duty = c.on, c.duty
        return dec

    def bits(self, dec):
        return "".join("1" if dec.commands[ld.key].on else "0" for ld in self.cfg.loads)


def test_registry_builds_rules(cfg):
    assert isinstance(get_strategy("rules", cfg), RuleBasedStrategy)


# ----------------------------------------------------------------- níveis
@pytest.mark.parametrize("soc, level", [(1.0, 3), (0.70, 3), (0.6999, 2), (0.40, 2), (0.3999, 1),
                                        (0.20, 1), (0.1999, 0), (0.0, 0)])
def test_first_evaluation_without_hysteresis(rp, soc, level):
    assert next_level(None, soc, True, rp)[0] == level


def test_first_evaluation_invalid_soc_is_critical(rp):
    assert next_level(None, 0.95, False, rp) == (1, "soc_invalid -> CRITICAL")


@pytest.mark.parametrize(
    "prev, soc, level",
    [(3, 0.6999, 2), (3, 0.15, 0), (2, 0.7499, 2), (2, 0.7501, 3), (1, 0.4499, 1), (1, 0.4501, 2),
     (0, 0.2499, 0), (0, 0.2501, 1), (0, 0.96, 3), (1, 0.80, 3), (2, 0.40, 2), (3, 0.70, 3)],
)
def test_hysteresis_descends_immediately_and_ascends_with_margin(rp, prev, soc, level):
    assert next_level(prev, soc, True, rp)[0] == level


@pytest.mark.parametrize("prev, level", [(3, 1), (2, 1), (1, 1), (0, 0)])
def test_invalid_soc_caps_at_critical(rp, prev, level):
    assert next_level(prev, 0.99, False, rp)[0] == level


def test_reason_cites_rule(rp):
    assert next_level(3, 0.68, True, rp)[1] == "soc 0.680 < 0.70 -> ECONOMY"
    assert "-> NORMAL" in next_level(2, 0.76, True, rp)[1]


def test_reset_restarts_first_evaluation(cfg):
    h = Harness(cfg)
    h.step(0, EVE, 0.72)
    h.step(60, EVE, 0.69)
    assert h.s.state == "ECONOMY"
    h.s.reset()
    assert h.s.state is None
    assert h.step(120, EVE, 0.72).state == "NORMAL"


# ---------------------------------------------------- ações por estado
@pytest.mark.parametrize(
    "soc, state, bits, fan_duty",
    [(0.80, "NORMAL", "110", 1.0), (0.55, "ECONOMY", "110", 0.6), (0.30, "CRITICAL", "100", 0.0),
     (0.10, "PROTECTION", "000", 0.0)],
)
def test_actions_per_state(cfg, soc, state, bits, fan_duty):
    h = Harness(cfg)
    dec = h.step(0, EVE, soc, done=40.0)
    assert dec.state == state and h.bits(dec) == bits
    assert dec.commands["fan"].duty == pytest.approx(fan_duty)
    assert dec.commands["led"].duty == (1.0 if bits[0] == "1" else 0.0)


def test_no_demand_no_load(cfg):
    h = Harness(cfg)
    dec = h.step(0, EVE, 0.9, led=False, fan=False, done=40.0)
    assert h.bits(dec) == "000"


def test_economy_duty_respects_min_duty(cfg):
    cfg2 = cfg.with_overrides({"ems.rules.economy_medium_duty": 0.3})
    dec = Harness(cfg2).step(0, EVE, 0.5, done=40.0)
    assert dec.commands["fan"].duty == pytest.approx(cfg.load_by_key("fan").min_duty)


def test_non_dimmable_medium_keeps_full_duty_in_economy(cfg):
    cfg2 = cfg.with_overrides({"loads.fan.dimmable": False})
    dec = Harness(cfg2).step(0, EVE, 0.5, done=40.0)
    assert dec.commands["fan"].on and dec.commands["fan"].duty == 1.0


def test_actions_are_generic_by_priority(cfg):
    # fan como prioridade baixa (não deslocável): só em NORMAL; led como medium: corta em CRITICAL
    cfg2 = cfg.with_overrides({"loads.fan.priority": "low", "loads.led.priority": "medium"})
    assert Harness(cfg2).bits(Harness(cfg2).step(0, EVE, 0.8, done=40.0)) == "110"
    h = Harness(cfg2)
    dec = h.step(0, EVE, 0.5, done=40.0)
    assert h.bits(dec) == "100" and dec.commands["led"].duty == 1.0   # led não dimerizável
    h = Harness(cfg2)
    assert h.bits(h.step(0, EVE, 0.3, done=40.0)) == "000"


# ------------------------------------------------------------ deslocável
def test_seconds_until_crosses_midnight():
    assert seconds_until(70000, 79200) == 9200
    assert seconds_until(80000, 3600) == 86400 - 80000 + 3600


def test_deferrable_surplus_latch_with_dwell(cfg):
    h = Harness(cfg)
    t0 = 10 * H
    seq = [(0, 0.85, "0"), (60, 0.90, "1"), (120, 0.85, "1"), (180, 0.7999, "1"), (240, 0.79, "1"),
           (360, 0.79, "0"), (420, 0.95, "0"), (480, 0.95, "0"), (660, 0.95, "1"), (720, 0.81, "1")]
    for t, soc, exp in seq:
        dec = h.step(t, t0 + t, soc, led=False, fan=False, done=10.0)
        assert h.bits(dec)[2] == exp, (t, soc, dec.reason)
    assert "excedente soc>=0.90" in dec.reason


def test_dwell_reason_is_reported(cfg):
    h = Harness(cfg)
    h.step(0, 10 * H, 0.95, led=False, fan=False)
    dec = h.step(60, 10 * H + 60, 0.75, led=False, fan=False)
    assert dec.commands["res"].on and "permanencia minima" in dec.reason


def test_deferrable_deadline_forced_only_in_normal(cfg):
    p = cfg.load_by_key("res")
    t_need = 40.0 / p.p_nom_w * 3600.0
    tod_force = 79200 - t_need - 1800 + 1
    h = Harness(cfg)
    assert not h.step(0, tod_force - 120, 0.75, led=False, fan=False).commands["res"].on
    dec = h.step(120, tod_force, 0.75, led=False, fan=False)
    assert dec.commands["res"].on and "res: prazo" in dec.reason
    h = Harness(cfg)
    assert not h.step(0, tod_force, 0.60, led=False, fan=False).commands["res"].on   # ECONOMY


def test_deferrable_immediate_offs_ignore_dwell(cfg):
    # energia concluída
    h = Harness(cfg)
    assert h.step(0, 10 * H, 0.95, led=False, fan=False, done=30.0).commands["res"].on
    dec = h.step(10, 10 * H + 10, 0.95, led=False, fan=False, done=40.0)
    assert not dec.commands["res"].on and "energia diaria concluida" in dec.reason
    # fim da janela
    h = Harness(cfg)
    assert h.step(0, 79100, 0.95, led=False, fan=False).commands["res"].on
    dec = h.step(100, 79200, 0.95, led=False, fan=False)
    assert not dec.commands["res"].on and "fora da janela" in dec.reason
    # estado CRITICAL
    h = Harness(cfg)
    assert h.step(0, 10 * H, 0.95, led=False, fan=False).commands["res"].on
    dec = h.step(10, 10 * H + 10, 0.35, led=False, fan=False)
    assert not dec.commands["res"].on and dec.state == "CRITICAL"
    assert h.s.latch["res"] is False


def test_deferrable_outside_window_clears_latch(cfg):
    h = Harness(cfg)
    dec = h.step(0, 7 * H - 60, 0.95, led=False, fan=False)
    assert not dec.commands["res"].on and not h.s.latch["res"]
    assert h.step(60, 7 * H, 0.95, led=False, fan=False).commands["res"].on


def test_deferrable_economy_surplus_by_battery_power(cfg):
    h = Harness(cfg)
    seq = [(0, 0.0, "0"), (60, -18.3, "0"), (120, -18.5, "1"), (180, 1.9, "1"), (240, 2.1, "1"),
           (420, 2.1, "0"), (480, -25.0, "0"), (780, -25.0, "1"), (840, -16.0, "1")]
    for t, p_bat, exp in seq:
        dec = h.step(t, 11 * H + t, 0.60, p_bat=p_bat, led=False, fan=True)
        assert dec.state == "ECONOMY"
        assert h.bits(dec)[2] == exp, (t, p_bat, dec.reason)
        assert dec.commands["fan"].duty == pytest.approx(0.6)
    assert "ECONOMY" in dec.reason


def test_decision_extra_and_all_commands(cfg):
    h = Harness(cfg)
    dec = h.step(0, EVE, 0.8)
    assert dec.mode == "rules" and set(dec.commands) == {"led", "fan", "res"}
    assert dec.extra["level"] == 3 and dec.extra["prev_state"] is None and "latch" in dec.extra
    dec = h.step(60, EVE, 0.5)
    assert dec.extra["prev_state"] == "NORMAL"


def test_missing_outputs_are_treated_as_off(cfg):
    s = RuleBasedStrategy(cfg)
    inp = EmsInputs(t_s=0, ts_ms=0, tod_s=10 * H, soc=0.95, soc_valid=True, v_bat=13.0, i_bat=0.0,
                    p_bat=0.0, p_pv=0.0, p_bus=0.0, demand={}, deferrable_done_wh={}, outputs={})
    dec = s.decide(inp)
    assert dec.commands["res"].on and not dec.commands["led"].on
    assert math.isinf(RulesParams.from_config(cfg).threshold(0))
