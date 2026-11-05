"""Testes do MPC (docs/06 §3.3) e das salvaguardas (§3.4) com previsões sintéticas determinísticas."""

from __future__ import annotations

import math
import sys
import time
import types

import pytest

from smartenergy.config import load_config
from smartenergy.ems import predictive as pred
from smartenergy.ems.base import EmsInputs, LoadCommand, LoadState
from smartenergy.ems.predictive import (
    MpcSolution,
    PredictiveStrategy,
    RulesParams,
    apply_safeguards,
    solve_mpc,
)
from smartenergy.ems.registry import get_strategy
from smartenergy.timeutil import demand_at, local_midnight_epoch_ms, tod_from_epoch_ms

H = 3600
STEP = 900


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def mid(cfg):
    return local_midnight_epoch_ms("2026-11-16", cfg)


class SynthFC:
    """Previsão sintética: FV em meia-senoide (06h–19h) e demanda do perfil programado."""

    method = "synthetic"

    def __init__(self, cfg, peak_w=50.0, fail=False):
        self.cfg, self.peak_w, self.fail, self.n_obs = cfg, peak_w, fail, 0

    def observe(self, ts_ms, p_pv_w, p_pv_avail_w=None):
        self.n_obs += 1

    def pv_forecast(self, t0_ms, step_s, n):
        if self.fail:
            raise RuntimeError("falha sintetica")
        tod0 = tod_from_epoch_ms(t0_ms, self.cfg)
        out = []
        for k in range(n):
            h = ((tod0 + (k + 0.5) * step_s) % 86400) / H
            out.append(self.peak_w * math.sin(math.pi * (h - 6) / 13) if 6 <= h <= 19 else 0.0)
        return out

    def demand_forecast(self, t0_ms, step_s, n):
        tod0 = tod_from_epoch_ms(t0_ms, self.cfg)
        return {key: [1.0 if demand_at(self.cfg, key, tod0 + (k + 0.5) * step_s) else 0.0 for k in range(n)]
                for key in ("led", "fan")}


def mk(cfg, mid, tod, soc, done=0.0, valid=True, outs=None, ts_ms=None):
    return EmsInputs(
        t_s=tod, ts_ms=mid + int(tod * 1000) if ts_ms is None else ts_ms, tod_s=tod, soc=soc, soc_valid=valid,
        v_bat=13.1, i_bat=0.0, p_bat=0.0, p_pv=0.0, p_bus=0.0,
        demand={k: demand_at(cfg, k, tod) for k in ("led", "fan")}, deferrable_done_wh={"res": done},
        outputs=outs or {ld.key: LoadState(ld.id, ld.key) for ld in cfg.loads},
    )


def problem(cfg, mid, tod, soc, done=0.0, peak=50.0, **kw):
    st = PredictiveStrategy(cfg, **kw)
    inp = mk(cfg, mid, tod, soc, done)
    t0 = (inp.ts_ms // (STEP * 1000)) * STEP * 1000
    return st.build_problem(inp, SynthFC(cfg, peak), t0)[0]


def energy(sol, key, p_w):
    return sum(sol.u[key]) * p_w * STEP / H


def switches(seq, initial=0.0):
    prev, n = initial, 0
    for x in seq:
        n += (x > 0.5) != (prev > 0.5)
        prev = x
    return n


def test_registry_and_solver_available(cfg):
    assert isinstance(get_strategy("predictive", cfg), PredictiveStrategy)
    assert pred.make_solver(1.0, 0.01).available()


# ------------------------------------------------- (a) excedente ao meio-dia
def test_surplus_midday_schedules_deferrable_and_reduces_curtailment(cfg, mid):
    pb = problem(cfg, mid, 9 * H, 0.98, peak=60.0)
    sol = solve_mpc(pb)
    assert sol.ok and sol.status == "Optimal"
    res_steps = [k for k, x in enumerate(sol.u["res"]) if x > 0.5]
    p_res = cfg.load_by_key("res").p_nom_w
    assert energy(sol, "res", p_res) >= 40.0 and sol.deficit_wh["res"] == pytest.approx(0.0, abs=1e-6)
    # todos os passos da deslocável ocorrem com FV disponível acima da sua potência
    assert all(pb.pv_avail_w[k] > p_res for k in res_steps)
    # sem a deslocável no horizonte, o corte FV seria maior
    pb0 = problem(cfg, mid, 9 * H, 0.98, peak=60.0, done=40.0)
    sol0 = solve_mpc(pb0)
    cut = sum(sol.curtail_w) * STEP / H
    cut0 = sum(sol0.curtail_w) * STEP / H
    assert cut0 - cut >= 30.0


# ------------------------------------------------- (b) escassez à noite
def test_night_scarcity_cuts_medium_before_high(cfg, mid):
    pb = problem(cfg, mid, 18 * H, 0.72, done=40.0)
    sol = solve_mpc(pb)
    assert sol.ok
    led = next(l for l in pb.loads if l.key == "led")
    fan = next(l for l in pb.loads if l.key == "fan")
    night = range(0, 20)  # 18:00–23:00
    led_srv = sum(sol.u["led"][k] for k in night) / sum(led.demand[k] for k in night)
    fan_srv = sum(sol.u["fan"][k] for k in night) / sum(fan.demand[k] for k in night)
    assert led_srv >= 0.95
    assert fan_srv < 0.5 and fan_srv < led_srv
    assert min(sol.soc) >= pb.soc_min - 1e-6     # SOC mínimo preservado quando possível


# ------------------------------------------------- (c) restrições
def test_constraints_balance_limits_window_and_demand(cfg, mid):
    pb = problem(cfg, mid, 6 * H, 0.25, peak=200.0)  # FV muito alta: carga limitada
    sol = solve_mpc(pb)
    assert sol.ok
    pc_max = cfg.battery.nominal_v * cfg.mppt.max_charge_current_a
    assert max(sol.pc_w) <= pc_max + 1e-6 and max(sol.pc_w) == pytest.approx(pc_max, rel=1e-6)
    assert max(sol.pd_w) <= cfg.battery.nominal_v * cfg.battery.max_discharge_current_a + 1e-6
    tod0 = 6 * H
    for k in range(pb.n):
        load = sum(l.p_w * sol.u[l.key][k] for l in pb.loads)
        bal = pb.pv_avail_w[k] - sol.curtail_w[k] + sol.pd_w[k] - sol.pc_w[k] - load - pb.p_aux_w
        assert abs(bal) < 1e-5
        assert -1e-9 <= sol.curtail_w[k] <= pb.pv_avail_w[k] + 1e-6
        tod = (tod0 + k * STEP) % 86400
        if not (7 * H <= tod < 22 * H):
            assert sol.u["res"][k] == 0.0
        for l in pb.loads:
            if l.demand is not None:
                assert sol.u[l.key][k] <= math.ceil(l.demand[k]) + 1e-9
                if l.dimmable and sol.u[l.key][k] > 0:
                    assert sol.u[l.key][k] >= l.min_duty - 1e-6
    assert max(sol.soc) <= pb.soc_max + 1e-9


def test_soc_min_is_soft(cfg, mid):
    pb = problem(cfg, mid, 18 * H, 0.12, done=40.0)  # abaixo de soc_min: ainda viável
    sol = solve_mpc(pb)
    assert sol.ok and min(sol.soc) < pb.soc_min
    assert sum(sol.u["fan"][:20]) == 0.0               # à noite, nada de média prioridade com déficit


def test_discharge_limit_blocks_deferrable_at_night(cfg, mid):
    cfg2 = cfg.with_overrides({"battery.max_discharge_current_a": 1.0})
    pb = problem(cfg2, mid, 19 * H, 1.0, done=0.0)
    sol = solve_mpc(pb)
    assert sol.ok and max(sol.pd_w) <= 12.8 + 1e-6
    tonight = [k for k in range(pb.n) if pb.loads[2].allowed[k]]
    assert tonight and all(sol.u["res"][k] == 0.0 for k in tonight)
    assert sol.deficit_wh["res"] == pytest.approx(40.0)


def test_deferrable_requirement_today_and_tomorrow(cfg, mid):
    pb = problem(cfg, mid, 10 * H, 0.5, done=15.0)
    res = next(l for l in pb.loads if l.key == "res")
    assert res.requirement_wh == pytest.approx(25.0) and sum(res.allowed) == 48   # 10:00–22:00
    pb = problem(cfg, mid, 23 * H, 0.5, done=40.0)
    res = next(l for l in pb.loads if l.key == "res")
    assert res.requirement_wh == pytest.approx(40.0) and sum(res.allowed) == 60   # amanhã 07–22
    assert res.allowed[0] is False and res.allowed[(31 - 23) * 4] is True


# ------------------------------------------------- (d) pesos
def test_weights_change_behaviour_coherently(cfg, mid):
    p_res = cfg.load_by_key("res").p_nom_w
    base = solve_mpc(problem(cfg, mid, 20 * H + 1800, 1.0, done=30.0))
    assert energy(base, "res", p_res) >= 10.0 - 1e-6            # prazo hoje: usa a bateria
    no_low = solve_mpc(problem(cfg, mid, 20 * H + 1800, 1.0, done=30.0, weights={"unserved_low": 0.0}))
    assert energy(no_low, "res", p_res) <= energy(base, "res", p_res)
    # sem valor para a deslocável nem custo de corte FV (que premia "abrir espaço" na bateria): não liga
    no_val = solve_mpc(problem(cfg, mid, 20 * H + 1800, 1.0, done=30.0,
                               weights={"unserved_low": 0.0, "curtailment": 0.0}))
    assert energy(no_val, "res", p_res) == 0.0
    many = solve_mpc(problem(cfg, mid, 9 * H, 0.6))
    few = solve_mpc(problem(cfg, mid, 9 * H, 0.6, weights={"switching": 50.0}))
    n_many = sum(switches(many.u[k]) for k in ("led", "fan", "res"))
    n_few = sum(switches(few.u[k]) for k in ("led", "fan", "res"))
    assert n_few <= n_many
    cheap_led = solve_mpc(problem(cfg, mid, 18 * H, 0.35, done=40.0, weights={"unserved_high": 0.5}))
    dear_led = solve_mpc(problem(cfg, mid, 18 * H, 0.35, done=40.0))
    assert sum(cheap_led.u["led"]) < sum(dear_led.u["led"])


# ------------------------------------------------- estratégia
def test_decide_plan_format_and_first_step(cfg, mid):
    st = PredictiveStrategy(cfg)
    fc = SynthFC(cfg)
    inp = mk(cfg, mid, 12 * H + 120, 0.95)
    dec = st.decide(inp, fc)
    assert dec.mode == "predictive" and not dec.fallback and dec.plan is not None
    plan = dec.plan
    assert {"step_s", "t0_ms", "soc", "loads", "pv_fc_w", "objective", "solve_ms", "status"} <= set(plan)
    assert plan["step_s"] == STEP and plan["t0_ms"] == mid + 12 * H * 1000
    assert len(plan["soc"]) == 97 and len(plan["pv_fc_w"]) == 96
    assert set(plan["loads"]) == {"1", "2", "3"} and all(len(v) == 96 for v in plan["loads"].values())
    assert plan["status"] == "Optimal" and plan["soc"][0] == pytest.approx(0.95)
    for ld in cfg.loads:
        x = plan["loads"][str(ld.id)][0]
        assert dec.commands[ld.key].on == (x > 0 and (ld.deferrable is not None or inp.demand[ld.key]))
    assert dec.state == "NORMAL"
    assert fc.n_obs == 0  # previsão externa: observe() é responsabilidade do chamador


def test_replans_only_at_grid_and_holds_commands(cfg, mid):
    st = PredictiveStrategy(cfg)
    fc = SynthFC(cfg)
    d1 = st.decide(mk(cfg, mid, 12 * H, 0.9), fc)
    d2 = st.decide(mk(cfg, mid, 12 * H + 300, 0.9), fc)
    assert st.n_solves == 1 and d2.plan is None and d2.extra["plan_step"] == 0
    assert {k: c.on for k, c in d1.commands.items()} == {k: c.on for k, c in d2.commands.items()}
    d3 = st.decide(mk(cfg, mid, 12 * H + 900, 0.9), fc)
    assert st.n_solves == 2 and d3.plan is not None and d3.plan["t0_ms"] == mid + (12 * H + 900) * 1000


def test_safeguards(cfg):
    rp = RulesParams.from_config(cfg)
    all_on = {ld.key: LoadCommand(ld.id, ld.key, True, 0.2) for ld in cfg.loads}

    def inp(tod, soc, led, fan, done=0.0):
        return EmsInputs(t_s=0, ts_ms=1, tod_s=tod, soc=soc, soc_valid=True, v_bat=13, i_bat=0, p_bat=0,
                         p_pv=0, p_bus=0, demand={"led": led, "fan": fan}, deferrable_done_wh={"res": done},
                         outputs={})

    out, notes = apply_safeguards(cfg, rp, all_on, inp(12 * H, 0.5, False, True))
    assert not out["led"].on and out["fan"].on and out["fan"].duty == pytest.approx(0.4)
    assert out["res"].on and out["res"].duty == 1.0 and out["led"].duty == 0.0
    out, _ = apply_safeguards(cfg, rp, all_on, inp(12 * H, 0.19, True, True))
    assert out["led"].on and not out["fan"].on and not out["res"].on
    out, _ = apply_safeguards(cfg, rp, all_on, inp(23 * H, 0.9, True, True))
    assert not out["res"].on                                   # fora da janela
    out, _ = apply_safeguards(cfg, rp, all_on, inp(12 * H, 0.9, True, True, done=40.0))
    assert not out["res"].on                                   # energia concluída


def test_fallbacks(cfg, mid, monkeypatch):
    st = PredictiveStrategy(cfg)
    fc = SynthFC(cfg)
    d = st.decide(mk(cfg, mid, 12 * H, 0.9, ts_ms=0), fc)
    assert d.fallback and d.extra["status"] == "no_time" and "fallback regras" in d.reason
    d = st.decide(mk(cfg, mid, 12 * H, 0.9, valid=False), fc)
    assert d.fallback and d.state == "CRITICAL" and d.extra["status"] == "soc_invalid"
    d = PredictiveStrategy(cfg).decide(mk(cfg, mid, 12 * H, 0.9), SynthFC(cfg, fail=True))
    assert d.fallback and d.extra["status"].startswith("error")
    monkeypatch.setattr(pred, "solve_mpc", lambda *a, **k: MpcSolution(ok=False, status="Infeasible"))
    st = PredictiveStrategy(cfg)
    d = st.decide(mk(cfg, mid, 12 * H, 0.9), fc)
    assert d.fallback and d.extra["status"] == "Infeasible" and st.last_status == "Infeasible"
    d = st.decide(mk(cfg, mid, 12 * H + 60, 0.9), fc)            # mesmo slot: não re-resolve
    assert d.fallback and st.n_solves == 1
    assert d.commands["led"].on is False and d.commands["fan"].on is True  # regras (NORMAL)


def test_missing_forecast_factory_falls_back_with_message(cfg, mid, monkeypatch):
    monkeypatch.setitem(sys.modules, "smartenergy.forecast.factory", None)  # força ImportError
    d = PredictiveStrategy(cfg).decide(mk(cfg, mid, 12 * H, 0.9))
    assert d.fallback and d.extra["status"] == "no_forecast"
    assert "previsao indisponivel" in d.reason


def test_default_forecast_built_by_factory_and_observed(cfg, mid, monkeypatch):
    fc = SynthFC(cfg)
    calls = []
    fake = types.ModuleType("smartenergy.forecast.factory")
    fake.make_forecast = lambda method, c: calls.append(method) or fc
    monkeypatch.setitem(sys.modules, "smartenergy.forecast.factory", fake)
    st = PredictiveStrategy(cfg)
    d = st.decide(mk(cfg, mid, 12 * H, 0.9))
    assert not d.fallback and calls == [cfg.forecast.pv_method] and fc.n_obs == 1


def test_solve_time_96_steps(cfg, mid):
    times = []
    for tod, soc in ((12 * H, 0.95), (20 * H, 0.45)):
        pb = problem(cfg, mid, tod, soc)
        t = time.perf_counter()
        sol = solve_mpc(pb, 10.0, 0.01)
        times.append(time.perf_counter() - t)
        assert sol.ok
    print(f"\nMPC 96 passos: build+solve = {[round(x * 1000) for x in times]} ms")
    assert max(times) < 5.0
