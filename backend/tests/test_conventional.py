"""Testes da operação convencional (docs/06 §3.1)."""

from __future__ import annotations

import pytest

from smartenergy.config import load_config
from smartenergy.ems.base import EmsInputs, LoadState
from smartenergy.ems.conventional import ConventionalStrategy, conventional_schedule
from smartenergy.ems.registry import get_strategy

H = 3600


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def mk(cfg, tod, led=False, fan=False, done=0.0, soc=0.5):
    return EmsInputs(
        t_s=tod, ts_ms=0, tod_s=tod, soc=soc, soc_valid=True, v_bat=13.0, i_bat=0.0, p_bat=0.0, p_pv=0.0,
        p_bus=0.0, demand={"led": led, "fan": fan}, deferrable_done_wh={"res": done},
        outputs={ld.key: LoadState(ld.id, ld.key) for ld in cfg.loads},
    )


def test_registry_builds_conventional(cfg):
    assert isinstance(get_strategy("conventional", cfg), ConventionalStrategy)


@pytest.mark.parametrize("led, fan", [(False, False), (True, False), (False, True), (True, True)])
def test_non_deferrable_follow_demand_regardless_of_soc(cfg, led, fan):
    dec = ConventionalStrategy(cfg).decide(mk(cfg, 12 * H, led, fan, soc=0.05))
    assert dec.commands["led"].on is led and dec.commands["fan"].on is fan
    assert dec.commands["fan"].duty == (1.0 if fan else 0.0)
    assert dec.state == "NORMAL" and dec.mode == "conventional" and not dec.fallback


@pytest.mark.parametrize(
    "tod, done, on",
    [(17 * H + 3599, 0.0, False), (18 * H, 0.0, True), (21 * H + 3599, 39.9, True),
     (20 * H, 40.0, False), (22 * H, 0.0, False), (10 * H, 0.0, False)],
)
def test_deferrable_fixed_schedule_until_energy_done(cfg, tod, done, on):
    c = ConventionalStrategy(cfg).decide(mk(cfg, tod, done=done)).commands["res"]
    assert c.on is on and c.duty == (1.0 if on else 0.0) and c.id == 3


def test_schedule_falls_back_to_window(cfg):
    cfg2 = cfg.with_overrides({"demand.conventional.res_schedule": []})
    assert conventional_schedule(cfg2, cfg2.load_by_key("res")) == [["07:00", "22:00"]]
    assert ConventionalStrategy(cfg2).decide(mk(cfg2, 8 * H)).commands["res"].on


def test_all_loads_have_commands(cfg):
    dec = ConventionalStrategy(cfg).decide(mk(cfg, 19 * H, True, True))
    assert set(dec.commands) == {ld.key for ld in cfg.loads}
    assert "res: horario fixo" in dec.reason
