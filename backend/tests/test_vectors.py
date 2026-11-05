"""Testes dos vetores golden (docs/06 §7)."""

from __future__ import annotations

import json

import pytest

from smartenergy.config import load_config
from smartenergy.experiments import vectors as vec


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def doc(cfg):
    return vec.build_vectors(cfg)


@pytest.fixture(scope="module")
def committed():
    return vec.DEFAULT_OUT.read_text(encoding="utf-8")


def test_committed_file_is_up_to_date(doc, committed):
    assert committed == vec.dumps(doc), "rode: python -m smartenergy gen-vectors"


def test_generation_is_deterministic(cfg, doc):
    assert vec.dumps(vec.build_vectors(cfg)) == vec.dumps(doc)


def test_committed_file_replays_without_divergence(cfg, committed):
    assert vec.verify_vectors(json.loads(committed), cfg) == []


def test_verify_detects_tampering(cfg, committed):
    bad = json.loads(committed)
    bad["rules"][0]["steps"][0]["expect"]["state"] = "PROTECTION"
    bad["soc"][0]["steps"][0]["expect"]["soc"] += 1e-6
    assert len(vec.verify_vectors(bad, cfg)) == 2


def test_format(doc):
    assert doc["schema"] == "sem.vectors.v1" and doc["source"] == "synthetic"
    p = doc["params"]
    assert set(p["rules"]) == {"normal_min_soc", "economy_min_soc", "critical_min_soc", "hysteresis_soc",
                               "economy_medium_duty", "min_dwell_s", "surplus_on_soc", "surplus_off_soc",
                               "economy_surplus_margin_w", "deadline_margin_s"}
    assert {"capacity_ah", "coulombic_efficiency", "ocv_soc", "ocv_v", "empty_soc_value"} <= set(p["soc"])
    assert p["safety"]["lvd_v"] == 11.6 and len(p["safety"]) == 15
    res = next(ld for ld in p["loads"] if ld["key"] == "res")
    assert res["deferrable"] == {"daily_energy_wh": 40.0, "window_start_s": 25200, "deadline_s": 79200}
    st = doc["rules"][0]["steps"][0]
    assert {"t_s", "tod_s", "soc", "soc_valid", "p_bat", "d", "e_done_wh", "expect"} <= set(st)
    assert set(st["expect"]["loads"]["fan"]) == {"on", "duty"}
    assert {"dt_s", "i_a", "v_v", "ok"} <= set(doc["soc"][0]["steps"][0])
    assert {"t_s", "dt_s", "v_bat", "soc", "soc_valid", "v_bus", "i_bus", "t_bat"} <= set(doc["safety"][0]["steps"][0])


def test_coverage_of_branches(doc):
    states = {s["expect"]["state"] for sc in doc["rules"] for s in sc["steps"]}
    assert states == {"NORMAL", "ECONOMY", "CRITICAL", "PROTECTION"}
    duties = {s["expect"]["loads"]["fan"]["duty"] for sc in doc["rules"] for s in sc["steps"]}
    assert 0.6 in duties
    codes = {s["expect"]["code"] for sc in doc["safety"] for s in sc["steps"]}
    assert codes == {"OK", "LVD", "OVP", "OC", "OC_LOCKOUT", "OT", "SENSOR"}  # BOOT: sem boot_hold
    syncs = {s["expect"]["last_sync"] for sc in doc["soc"] for s in sc["steps"]}
    assert {"persisted", "ocv_init", "full", "empty"} <= syncs
    assert any(not s["expect"]["valid"] for sc in doc["soc"] for s in sc["steps"])
    events = {e for sc in doc["soc"] for s in sc["steps"] for e in s["expect"]["events"]}
    assert events == {"SOC_SYNC_FULL", "SOC_SYNC_EMPTY"}
    assert any(s.get("reset_faults") for sc in doc["safety"] for s in sc["steps"])
    assert any(s["v_bat"] is None for sc in doc["safety"] for s in sc["steps"])


def test_hysteresis_vector_matches_hand_computed_oracle(doc):
    sc = next(s for s in doc["rules"] if s["name"] == "hysteresis_all_boundaries")
    got = "".join(s["expect"]["state"][0] for s in sc["steps"])
    assert got == "NNNEEEN" + "EEECCCECCCPPPCPCEENPPN"


def test_dwell_vector_matches_hand_computed_oracle(doc):
    sc = next(s for s in doc["rules"] if s["name"] == "res_latch_dwell_normal")
    got = "".join("1" if s["expect"]["loads"]["res"]["on"] else "0" for s in sc["steps"])
    assert got == "01111000111"


def test_cli_writes_and_checks(tmp_path, capsys):
    out = tmp_path / "v.json"
    assert vec.main(["--out", str(out)]) == 0
    assert json.loads(out.read_text())["schema"] == "sem.vectors.v1"
    assert vec.main(["--out", str(out), "--check"]) == 0
    out.write_text("{}")
    assert vec.main(["--out", str(out), "--check"]) == 1
    assert "DESATUALIZADO" in capsys.readouterr().out


def test_cli_entrypoint_registered():
    from smartenergy.cli import SUBCOMMANDS

    assert SUBCOMMANDS["gen-vectors"][0] == "smartenergy.experiments.vectors"
