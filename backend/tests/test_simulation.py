"""Testes do motor de simulação, da telemetria simulada e do subcomando ``simulate``.

Os testes do motor usam estimador, supervisor e estratégia *fakes* definidos aqui
(independentes dos módulos de estimação/EMS); os testes de integração com as
estratégias reais são pulados se esses módulos não existirem.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import jsonschema
import numpy as np
import pytest

from smartenergy.config import REPO_ROOT, load_config
from smartenergy.ems.base import Decision, LoadCommand
from smartenergy.models.battery import BatteryModel
from smartenergy.models.mppt import MpptModel, mppt_output_from_input
from smartenergy.simulation import engine
from smartenergy.simulation.engine import run_simulation, solve_step
from smartenergy.simulation.telemetry import daily_energy_counters, row_to_telemetry, simulation_to_telemetry
from smartenergy.timeutil import in_window

VOCAB = [
    "t", "pv_v", "pv_i", "pv_p", "pv_p_avail", "curtail_p", "irr_poa", "t_amb", "t_pv", "t_bat",
    "bat_v", "bat_i", "bat_p", "bus_v", "bus_i", "bus_p", "aux_p", "soc", "soc_unc", "soc_true",
    "unserved_p", "def_done_wh", "mode", "state", "safety",
] + [f"l{n}_{s}" for n in (1, 2, 3) for s in ("p", "on", "duty", "d")]


def _schema(name: str) -> dict:
    with open(REPO_ROOT / "schemas" / f"{name}.schema.json", encoding="utf-8") as fh:
        return json.load(fh)


# ----------------------------------------------------------------------------- fakes
class FakeEstimator:
    """Contagem de Coulomb simples (sem sincronizações)."""

    def __init__(self, cfg, initial_soc, v_init):
        b = cfg.raw["battery"]
        self.soc, self.unc, self.valid = float(initial_soc), 0.02, True
        self.last_sync, self.since_sync_s = "persisted", 0.0
        self.cap, self.eta = float(b["capacity_ah"]), float(b["coulombic_efficiency"])

    def update(self, dt_s, i_a, v_v, sensor_ok=True):
        if i_a > 0:
            self.soc -= i_a * dt_s / (3600 * self.cap)
        else:
            self.soc += self.eta * (-i_a) * dt_s / (3600 * self.cap)
        self.soc = min(max(self.soc, 0.0), 1.0)
        self.since_sync_s += dt_s
        return []


@dataclass
class FakeStatus:
    allowed: bool
    code: str
    events: list = field(default_factory=list)


class FakeSafety:
    """LVD por tensão/SOC com religamento por V e SOC (subconjunto do docs/06 §2)."""

    def __init__(self, cfg):
        self.p = cfg.raw["safety"]
        self.lvd = False

    def reset(self):
        self.lvd = False

    def update(self, t_s, dt_s, v_bat, soc, soc_valid, v_bus=None, i_bus=None, t_bat=None):
        ev = []
        if not self.lvd and (v_bat < self.p["lvd_v"] or soc < self.p["soc_hard_min"]):
            self.lvd = True
            ev.append(("LVD_TRIP", "warn", f"v_bat={v_bat:.2f} soc={soc:.2f}"))
        elif self.lvd and v_bat >= self.p["reconnect_v"] and soc >= self.p["reconnect_min_soc"]:
            self.lvd = False
            ev.append(("LVD_CLEAR", "info", "religamento"))
        return FakeStatus(not self.lvd, "LVD" if self.lvd else "OK", ev)


class DemandStrategy:
    """Liga não deslocáveis pela demanda; deslocável na janela até cumprir a energia diária."""

    name = "conventional"

    def __init__(self, cfg):
        self.cfg = cfg

    def reset(self):
        pass

    def decide(self, inp, forecast=None):
        cmds = {}
        for ld in self.cfg.loads:
            if ld.deferrable is None:
                on = inp.demand.get(ld.key, False)
            else:
                dfr = ld.deferrable
                on = in_window(inp.tod_s, dfr.window_start, dfr.deadline) and \
                    inp.deferrable_done_wh[ld.key] < dfr.daily_energy_wh
            cmds[ld.key] = LoadCommand(ld.id, ld.key, on, 1.0 if on else 0.0)
        return Decision(mode="conventional", state="NORMAL", commands=cmds, reason="demanda programada")


FAKES = dict(estimator_factory=FakeEstimator, safety_factory=FakeSafety)


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def sunny(cfg):
    return run_simulation(cfg, "ensolarado", None, 1, strategy_obj=DemandStrategy(cfg), **FAKES)


@pytest.fixture(scope="module")
def cloudy(cfg):
    return run_simulation(cfg, "nublado", None, 1, strategy_obj=DemandStrategy(cfg), **FAKES)


# ----------------------------------------------------------------------------- física do passo
def _models(cfg, soc):
    return BatteryModel.from_config(cfg, soc), MpptModel.from_config(cfg), list(cfg.loads)


def _check_balance(ph):
    assert ph.mppt_out + ph.bat.p_w == pytest.approx(sum(ph.load_p) + ph.aux_p, abs=1e-9)


def test_step_discharge_at_night(cfg):
    bat, mppt, loads = _models(cfg, 0.6)
    ph = solve_step(bat, mppt, loads, [1.0, 1.0, 0.0], 0.0, 0.8, bat.ocv(), 60.0, 25.0, False)
    _check_balance(ph)
    assert ph.pv_in == 0.0 and ph.bat.i_a > 0
    assert ph.v_bus == pytest.approx(bat.ocv() - ph.bat.i_a * 0.04)
    # cargas avaliadas na tensão final (ponto fixo convergido)
    assert ph.load_p[0] == pytest.approx(5.0 * (ph.v_bus / 12.8) ** 1.5, rel=1e-6)


def test_step_free_charge_and_cc_limit(cfg):
    bat, mppt, loads = _models(cfg, 0.5)
    ph = solve_step(bat, mppt, loads, [0, 0, 0], 20.0, 0.8, bat.ocv(), 60.0, 25.0, False)
    _check_balance(ph)
    assert ph.pv_in == pytest.approx(20.0) and ph.bat.regime == "charge"
    ph = solve_step(bat, mppt, loads, [0, 0, 0], 60.0, 0.8, bat.ocv(), 60.0, 25.0, False)
    _check_balance(ph)
    assert ph.bat.i_a == pytest.approx(-3.0) and ph.pv_in < 60.0  # corte por limite de corrente


def test_step_cv_and_partial_regulation(cfg):
    bat, mppt, loads = _models(cfg, 1.0)
    ph = solve_step(bat, mppt, loads, [1, 1, 0], 40.0, 0.8, bat.ocv(), 60.0, 25.0, False)
    _check_balance(ph)
    assert ph.v_bus == pytest.approx(14.2) and ph.bat.i_a == pytest.approx(0.0)
    assert ph.pv_in < 40.0
    # FV mal cobre as cargas em OCV mas não em 14,2 V → regulação parcial, bateria parada
    p_load_ocv = sum(ld.p_nom_w * (bat.ocv() / 12.8) ** ld.voltage_exponent for ld in loads)
    p_av = (p_load_ocv + 0.8 + 1.0 + 0.3) / 0.95
    ph = solve_step(bat, mppt, loads, [1, 1, 1], p_av, 0.8, bat.ocv(), 60.0, 25.0, False)
    _check_balance(ph)
    assert bat.ocv() < ph.v_bus < 14.2 and ph.bat.i_a == 0.0
    assert ph.pv_in == pytest.approx(p_av)


def test_step_bms_trip_and_release(cfg):
    bat, mppt, loads = _models(cfg, 0.0005)
    ph = solve_step(bat, mppt, loads, [1, 1, 1], 0.0, 0.8, bat.ocv(), 60.0, 25.0, False)
    assert ph.bms_tripped and ph.bms_open and sum(ph.load_p) == 0.0 and ph.aux_p == 0.0
    assert ph.deficit_p > 15.0  # cargas avaliadas em ~10 V (OCV em SOC ~0)
    _check_balance(ph)
    ph = solve_step(bat, mppt, loads, [1, 1, 1], 20.0, 0.8, bat.ocv(), 60.0, 25.0, True)
    assert ph.bms_released and not ph.bms_open and ph.bat.i_a < 0 and ph.aux_p == 0.8
    _check_balance(ph)


# ----------------------------------------------------------------------------- motor
def test_columns_and_metadata(sunny):
    for c in VOCAB:
        assert c in sunny.df.columns, c
    assert len(sunny.df) == 3 * 1440
    assert sunny.meta["source"] == "simulation" and "SIMULADOS" in sunny.meta["warning"]
    assert sunny.df.attrs["source"] == "simulation"
    assert sunny.meta["seed"] == 1 and sunny.meta["days"] == 3 and sunny.meta["dt_s"] == 60.0


@pytest.mark.parametrize("res_name", ["sunny", "cloudy"])
def test_energy_balance_closes(cfg, res_name, request):
    res = request.getfixturevalue(res_name)
    df = res.df
    assert abs(res.metrics["balance_error_wh"]) < 1e-6
    out = mppt_output_from_input(df["pv_p"].to_numpy(), 0.95, 0.3)
    resid = out + df["bat_p"] - df["bus_p"] - df["aux_p"]
    assert np.abs(resid).max() < 1e-9
    np.testing.assert_allclose(df["bus_p"], df["l1_p"] + df["l2_p"] + df["l3_p"], atol=1e-12)
    assert (df["curtail_p"] >= -1e-12).all()
    np.testing.assert_allclose(df["curtail_p"], np.maximum(0, df["pv_p_avail"] - df["pv_p"]), atol=1e-12)


def test_true_soc_follows_coulomb_counting(cfg, cloudy):
    df = cloudy.df
    i = df["bat_i"].to_numpy()
    s = df["soc_true"].to_numpy()
    d = np.where(i >= 0, -i * 60 / 21600.0, 0.99 * (-i) * 60 / 21600.0)
    expected = np.clip(s[:-1] + d[:-1], 0, 1)
    np.testing.assert_allclose(s[1:], expected, atol=1e-12)
    assert df.attrs["soc_true_end"] == pytest.approx(min(max(s[-1] + d[-1], 0), 1))


def test_safety_blocks_outputs(cloudy):
    df = cloudy.df
    lvd = df["safety"] == "LVD"
    assert lvd.any(), "cenário nublado deveria acionar LVD com a estratégia de demanda"
    assert (df.loc[lvd, ["l1_on", "l2_on", "l3_on"]].to_numpy() == 0).all()
    assert cloudy.metrics["lvd_events"] >= 1
    codes = {e["code"] for e in cloudy.events}
    assert {"BOOT", "LVD_TRIP"} <= codes


def test_curtailment_and_absorption_when_full(cfg):
    res = run_simulation(cfg, "ensolarado", None, 1, days=1, initial_soc=1.0,
                         strategy_obj=DemandStrategy(cfg), **FAKES)
    df = res.df
    assert res.metrics["e_curtail_wh"] > 50.0
    assert df["bat_v"].max() <= 14.2 + 1e-9
    assert df["soc_true"].max() <= 1.0
    # rampa CC-CV: perto de SOC 1 a corrente de carga tende a zero (aproximação assintótica)
    near_full = df["soc_true"] >= 0.999
    assert (df.loc[near_full, "bat_i"] > -3.0 * 0.001 / 0.05 - 1e-9).all()
    np.testing.assert_allclose(df.loc[near_full & (df["bat_i"] < 0), "bat_v"], 14.2)


def test_deferrable_counter_resets_at_midnight(sunny):
    df = sunny.df
    midnight = df["tod_s"] == 0
    assert (df.loc[midnight, "def_done_wh"] == 0).all()
    day0 = df.iloc[:1440]
    inc = np.diff(day0["def_done_wh"].to_numpy())
    np.testing.assert_allclose(inc, day0["l3_p"].to_numpy()[:-1] / 60.0, atol=1e-12)
    assert day0["def_done_wh"].iloc[-1] + day0["l3_p"].iloc[-1] / 60 >= 40.0 - 1e-9


def test_determinism_and_shared_weather(cfg, sunny):
    again = run_simulation(cfg, "ensolarado", None, 1, strategy_obj=DemandStrategy(cfg), **FAKES)
    assert again.df.drop(columns=[]).equals(sunny.df)
    other = run_simulation(cfg, "parcialmente_nublado", None, 2, days=1, strategy_obj=DemandStrategy(cfg), **FAKES)
    base = run_simulation(cfg, "parcialmente_nublado", None, 1, days=1, strategy_obj=DemandStrategy(cfg), **FAKES)
    assert not np.array_equal(other.df["pv_p_avail"], base.df["pv_p_avail"])

    class AllOff(DemandStrategy):
        def decide(self, inp, forecast=None):
            cmds = {ld.key: LoadCommand(ld.id, ld.key, False, 0.0) for ld in self.cfg.loads}
            return Decision(mode="rules", state="NORMAL", commands=cmds, reason="tudo desligado")

    off = run_simulation(cfg, "parcialmente_nublado", None, 1, days=1, strategy_obj=AllOff(cfg), **FAKES)
    np.testing.assert_array_equal(off.df["pv_p_avail"], base.df["pv_p_avail"])  # mesmo clima
    assert off.metrics["e_served_wh"] == 0.0


def test_sensor_errors_reach_estimator_only(cfg, sunny):
    df = sunny.df
    gain = cfg.raw["simulation"]["sensor_error"]["voltage_gain"]
    np.testing.assert_allclose(df["bat_v_meas"], df["bat_v"] * (1 + gain), rtol=1e-12)
    resid = df["bat_i_meas"] - df["bat_i"] * 1.005 - 0.002
    assert 0.003 < resid.std() < 0.007  # ruído σ = 5 mA
    assert (df["soc"] != df["soc_true"]).any()
    assert sunny.metrics["soc_est_mae"] < 0.05


def test_decisions_and_events_schema(sunny, cloudy):
    dec_schema, ev_schema = _schema("decision"), _schema("event")
    for res in (sunny, cloudy):
        assert res.decisions and res.events
        for d in res.decisions:
            jsonschema.validate(d, dec_schema)
            assert d["source"] == "simulation" and d["origin"] == "firmware"
        for e in res.events:
            jsonschema.validate(e, ev_schema)
        keys = [(d["mode"], d["state"], json.dumps(d["actions"]), d["safety"]) for d in res.decisions]
        assert all(a != b for a, b in zip(keys, keys[1:])), "decisão registrada sem mudança"


def test_telemetry_valid(cfg, sunny):
    schema = _schema("telemetry")
    meta = {"scenario": "ensolarado", "strategy": "conventional", "seed": 1, "t0_ms": sunny.meta["t0_ms"]}
    pkts = simulation_to_telemetry(sunny.df, cfg, sim_meta=meta, every=97)
    assert len(pkts) > 40
    for p in pkts:
        jsonschema.validate(p, schema)
        assert p["source"] == "simulation" and p["sim"]["scenario"] == "ensolarado"
    row = sunny.df.iloc[18 * 60]
    p = row_to_telemetry(row, cfg, seq=5)
    jsonschema.validate(p, schema)
    assert "sim" not in p and p["seq"] == 5
    assert p["loads"][2]["deferrable"]["e_req_wh"] == 40.0
    assert p["meas"]["bat"]["v"] == pytest.approx(round(row["bat_v_meas"], 3))
    # contadores diários: zeram à meia-noite e acumulam pv_in
    cnt = daily_energy_counters(sunny.df, cfg)
    assert cnt["pv_in"].iloc[1440] == 0.0
    assert cnt["pv_in"].iloc[1439] == pytest.approx(sunny.df["pv_p"].iloc[:1439].sum() / 60)
    assert cnt["day"].iloc[0] == "2026-11-16"


def test_forecasts_recorded_for_any_strategy(cfg):
    res = run_simulation(cfg, "parcialmente_nublado", None, 1, days=1, forecast_method="perfect",
                         strategy_obj=DemandStrategy(cfg), **FAKES)
    fc = res.forecasts
    assert fc is not None and len(fc) == 96 * 96
    assert res.metrics["forecast_mae_w"] == pytest.approx(0.0, abs=1e-9)
    assert res.meta["forecast_method"] == "perfect"
    noisy = run_simulation(cfg, "parcialmente_nublado", None, 1, days=1, forecast_method="noisy",
                           strategy_obj=DemandStrategy(cfg), **FAKES)
    assert noisy.metrics["forecast_mae_w"] > 0.5
    np.testing.assert_array_equal(noisy.df["pv_p_avail"], res.df["pv_p_avail"])


def test_overrides_applied(cfg):
    res = run_simulation(cfg, "nublado", None, 1, days=1, overrides={"battery.capacity_ah": 12.0},
                         strategy_obj=DemandStrategy(cfg), **FAKES)
    assert res.meta["overrides"] == {"battery.capacity_ah": 12.0}
    assert res.metrics["equiv_cycles"] == pytest.approx(res.metrics["bat_throughput_wh"] / (2 * 12.8 * 12.0))


def test_performance_seven_days(cfg):
    t0 = time.perf_counter()
    res = run_simulation(cfg, "semana_variavel", None, 1, strategy_obj=DemandStrategy(cfg), **FAKES)
    assert len(res.df) == 7 * 1440
    assert time.perf_counter() - t0 < 5.0


def test_cli_writes_outputs(cfg, tmp_path, monkeypatch):
    from smartenergy.simulation import cli

    monkeypatch.setattr(engine, "_default_strategy_factory", lambda name, c: DemandStrategy(c))
    monkeypatch.setattr(engine, "_default_estimator_factory", FakeEstimator)
    monkeypatch.setattr(engine, "_default_safety_factory", FakeSafety)
    out = tmp_path / "sim"
    rc = cli.main(["--scenario", "nublado", "--strategy", "rules", "--seed", "2", "--days", "1",
                   "--set", "aux.p_w=1.0", "--out", str(out)])
    assert rc == 0
    lines = (out / "series.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("# source=simulation") and "SIMULADOS" in lines[0]
    assert lines[1].startswith("t[ms],") and "soc_true[1]" in lines[1] and "l3_p[W]" in lines[1]
    assert len(lines) == 2 + 1440
    met = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    assert met["source"] == "simulation" and met["meta"]["overrides"] == {"aux.p_w": 1.0}
    assert met["metrics"]["e_aux_wh"] == pytest.approx(24.0, abs=0.5)
    assert (out / "balance.png").stat().st_size > 10_000
    assert json.loads((out / "decisions.json").read_text(encoding="utf-8"))["items"]


# ----------------------------------------------------------------------------- integração (módulos reais)
@pytest.mark.parametrize("strategy", ["conventional", "rules"])
def test_integration_real_strategies(cfg, strategy):
    pytest.importorskip("smartenergy.estimation.soc")
    pytest.importorskip("smartenergy.ems.safety")
    pytest.importorskip(f"smartenergy.ems.{strategy}")
    t0 = time.perf_counter()
    res = run_simulation(cfg, "semana_variavel", strategy, 1)
    assert time.perf_counter() - t0 < 10.0
    assert abs(res.metrics["balance_error_wh"]) < 1e-6
    assert res.df["mode"].iloc[0] == strategy
    for d in res.decisions[:50]:
        jsonschema.validate(d, _schema("decision"))
    for e in res.events:
        jsonschema.validate(e, _schema("event"))
    m = res.metrics
    assert 0.0 <= m["service_rate_high"] <= 1.0 and m["e_served_wh"] > 0.0
    assert 0.0 <= m["e_unserved_wh"] <= m["e_demand_wh"]


def test_integration_rules_protect_high_priority(cfg):
    pytest.importorskip("smartenergy.ems.rules")
    pytest.importorskip("smartenergy.ems.conventional")
    conv = run_simulation(cfg, "nublado", "conventional", 1)
    rules = run_simulation(cfg, "nublado", "rules", 1)
    np.testing.assert_array_equal(conv.df["pv_p_avail"], rules.df["pv_p_avail"])
    assert rules.metrics["e_unserved_high_wh"] <= conv.metrics["e_unserved_high_wh"] + 1e-9


def test_integration_predictive_short_horizon(cfg):
    pytest.importorskip("smartenergy.ems.predictive")
    pytest.importorskip("pulp")
    res = run_simulation(cfg, "parcialmente_nublado", "predictive", 1, days=1, forecast_method="perfect",
                         overrides={"ems.predictive.horizon_steps": 16})
    assert abs(res.metrics["balance_error_wh"]) < 1e-6
    assert res.decisions and all(d["origin"] == "backend" for d in res.decisions)
    assert res.forecasts is not None and res.metrics["forecast_mae_w"] == pytest.approx(0.0, abs=1e-9)
    # salvaguarda §3.4: não deslocáveis nunca ligadas sem pedido
    df = res.df
    assert not ((df["l1_on"] == 1) & (df["l1_d"] == 0)).any()
    assert not ((df["l2_on"] == 1) & (df["l2_d"] == 0)).any()
