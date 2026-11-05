"""Testes da API FastAPI (TestClient) com store temporário populado no próprio teste."""

from __future__ import annotations

import json
import threading
import time
import warnings
from pathlib import Path
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from smartenergy.api.app import ApiSettings, create_app
from smartenergy.config import load_config
from smartenergy.ingest.mqtt_conn import MqttUnavailableError
from smartenergy.security.hmac_cmd import verify_envelope
from smartenergy.security.schemas import schema_error
from smartenergy.storage.sqlite_store import SqliteStore
from smartenergy.storage.synthetic import synthetic_telemetry

T0 = 1763290800000
TOKEN = "token-de-teste"
KEY = "chave-de-teste-com-32-bytes-ok!!"
ENV = {"SEM_API_TOKEN": TOKEN, "SEM_CMD_KEY": KEY}


def _decision(i: int, origin: str = "firmware", plan: dict | None = None) -> dict[str, Any]:
    return {"schema": "sem.decision.v1", "ts_ms": T0 + i * 1000, "source": "simulation", "origin": origin,
            "mode": "predictive" if plan else "rules", "state": "NORMAL", "prev_state": None, "safety": "OK",
            "reason": f"d{i}", "actions": [{"id": 1, "on": True, "duty": 1.0}], "inputs": {"soc": 0.7}, "plan": plan}


def _write_experiment(root: Path, name: str, scenario: str, strategy: str, seed: int, rate: float) -> None:
    d = root / name
    d.mkdir(parents=True)
    meta = {"source": "simulation", "scenario": scenario, "strategy": strategy, "seed": seed,
            "generated_at": f"2026-11-0{seed}T00:00:00Z"}
    (d / "metrics.json").write_text(json.dumps({"source": "simulation", "meta": meta,
                                                "metrics": {"service_rate": rate, "e_unserved_wh": 10 * seed}}))
    lines = ["# source=simulation; DADOS SIMULADOS", "t[ms],soc_true[1],pv_p[W],pv_p_avail[W],bus_p[W],mode[text]"]
    for k in range(120):  # 2 h a 60 s
        lines.append(f"{T0 + k * 60000},{0.5 + k * 0.001:.4f},{rate * 10:.3f},{rate * 12:.3f},5.0,rules")
    (d / "series.csv").write_text("\n".join(lines) + "\n")


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, Any]:
    cfg = load_config()
    store = SqliteStore(tmp_path / "api.sqlite", cfg=cfg)
    for i in range(36):  # 3 min a 5 s, telemetria sintética válida (source=simulation)
        p = synthetic_telemetry(T0 + i * 5000, i, soc=0.6 + 0.001 * i)
        assert schema_error("telemetry", p) is None
        store.insert_telemetry(p)
    for i in range(5):
        store.insert_decision(_decision(i))
    store.insert_event({"schema": "sem.event.v1", "ts_ms": T0, "source": "simulation", "level": "info",
                        "code": "BOOT", "msg": "boot"})
    results = tmp_path / "results"
    _write_experiment(results, "sim_ensolarado_rules_s1", "ensolarado", "rules", 1, 0.90)
    _write_experiment(results, "sim_ensolarado_rules_s2", "ensolarado", "rules", 2, 0.80)
    _write_experiment(results, "cmp/sim_ensolarado_predictive_s1", "ensolarado", "predictive", 1, 0.95)
    dash = tmp_path / "dashboard"
    dash.mkdir()
    (dash / "index.html").write_text("<html>SEM dashboard</html>")
    published: list[tuple[str, str]] = []
    app = create_app(ApiSettings(results_dir=results, dashboard_dir=dash, stream_poll_s=0.05),
                     cfg=cfg, store=store, cmd_publisher=lambda t, p: published.append((t, p)), environ=ENV)
    return {"client": TestClient(app), "store": store, "published": published, "results": results, "cfg": cfg}


def test_meta_and_live(env: dict[str, Any]) -> None:
    c = env["client"]
    m = c.get("/api/meta").json()
    assert m["source"] == "simulation" and m["device_id"] == "sem-esp32-01" and m["demo"] is False
    assert isinstance(m["connected"], bool) and m["last_seen_ms"] > 0
    for k in ("loads", "battery", "pv", "ems", "safety"):
        assert k in m["config"]
    assert "ocv_curve" not in m["config"]["battery"]
    live = c.get("/api/live").json()
    assert live["seq"] == 35 and live["source"] == "simulation"


def test_live_null_when_empty(tmp_path: Path) -> None:
    app = create_app(ApiSettings(db_path=str(tmp_path / "e.sqlite"), results_dir=tmp_path, dashboard_dir=None),
                     environ={})
    c = TestClient(app)
    r = c.get("/api/live")
    assert r.status_code == 200 and r.json() is None
    h = c.get("/api/history").json()
    assert h["t"] == [] and h["source"] is None
    assert c.get("/").json()["msg"].startswith("dashboard")


def test_history_energy_decisions_events(env: dict[str, Any]) -> None:
    c = env["client"]
    h = c.get("/api/history", params={"minutes": 60, "step_s": 60}).json()
    assert h["source"] == "simulation" and len(h["t"]) in (3, 4)
    for col in ("pv_v", "pv_p", "bat_p", "bus_p", "soc", "soc_unc", "l1_p", "l2_duty", "l3_on", "mode", "safety"):
        assert len(h["series"][col]) == len(h["t"]), col
    assert c.get("/api/history", params={"step_s": 0}).status_code == 422
    assert c.get("/api/history", params={"source": "medido"}).status_code == 422
    assert c.get("/api/history", params={"source": "hardware"}).json()["t"] == []
    e = c.get("/api/energy/daily", params={"days": 7}).json()
    assert e["source"] == "simulation" and e["days"][0]["pv_in"] == 100.0
    d = c.get("/api/decisions", params={"limit": 3}).json()
    assert d["source"] == "simulation" and [x["reason"] for x in d["items"]] == ["d4", "d3", "d2"]
    ev = c.get("/api/events").json()
    assert ev["items"][0]["code"] == "BOOT" and ev["source"] == "simulation"


def test_forecast_from_backend_plan(env: dict[str, Any]) -> None:
    c = env["client"]
    empty = c.get("/api/forecast").json()
    assert empty["t"] == [] and empty["soc_plan"] is None and "source" in empty
    plan = {"step_s": 900, "t0_ms": T0, "soc": [0.6, 0.62, 0.64, 0.66, 0.68], "pv_fc_w": [10.0, 20.0, 30.0, 25.0],
            "loads": {"1": [1, 1, 0, 0]}, "objective": 1.0, "solve_ms": 5.0, "status": "Optimal",
            "forecast_method": "clearsky_persistence"}
    env["store"].insert_decision(_decision(9, origin="backend", plan=plan))
    f = c.get("/api/forecast").json()
    assert f["source"] == "simulation" and f["method"] == "clearsky_persistence"
    assert f["t"] == [T0 + k * 900_000 for k in range(4)]
    assert f["pv_fc_w"] == [10.0, 20.0, 30.0, 25.0] and f["soc_plan"] == [0.6, 0.62, 0.64, 0.66]
    assert len(f["load_fc_w"]) == 4 and all(x >= 0.8 for x in f["load_fc_w"])  # >= auxiliar


def test_experiments_and_comparison(env: dict[str, Any]) -> None:
    c = env["client"]
    items = c.get("/api/experiments").json()["items"]
    ids = {i["id"] for i in items}
    assert ids == {"sim_ensolarado_rules_s1", "sim_ensolarado_rules_s2", "cmp/sim_ensolarado_predictive_s1"}
    assert all(i["source"] == "simulation" for i in items)
    one = c.get("/api/experiments/cmp/sim_ensolarado_predictive_s1").json()
    assert one["metrics"]["service_rate"] == 0.95 and one["strategy"] == "predictive"
    assert len(one["series"]["t"]) == 120 and len(one["series"]["soc_true"]) == 120
    sub = c.get("/api/experiments/sim_ensolarado_rules_s1", params={"step_s": 600, "columns": "soc_true"}).json()
    assert len(sub["series"]["t"]) == 12 and set(sub["series"]) == {"t", "soc_true", "step_s"}
    assert c.get("/api/experiments/nao-existe").status_code == 404
    assert c.get("/api/experiments/../../etc/passwd").status_code == 404
    comp = c.get("/api/comparison").json()
    assert comp["source"] == "simulation" and comp["scenarios"] == ["ensolarado"]
    assert comp["strategies"] == ["rules", "predictive"] and comp["seeds"] == [1, 2]
    rules = next(r for r in comp["table"] if r["strategy"] == "rules")
    assert rules["n"] == 2 and rules["mean"]["service_rate"] == pytest.approx(0.85)
    assert rules["std"]["service_rate"] == pytest.approx(0.0707107, abs=1e-6)
    assert comp["metrics_def"]["service_rate"]["better"] == "higher"
    s = comp["series"]["ensolarado"]
    assert len(s["t"]) == len(s["rules"]["soc_true"]) == len(s["predictive"]["pv_p"]) > 0
    # comparison.json do comparador tem precedência sobre a agregação
    (env["results"] / "cmp" / "comparison.json").write_text(json.dumps(
        {"source": "simulation", "comparison": {"scenarios": ["x"], "strategies": [], "seeds": [], "table": [],
                                                "metrics_def": {}, "series": {}}}))
    assert c.get("/api/comparison").json()["scenarios"] == ["x"]


def test_export_csv(env: dict[str, Any]) -> None:
    r = env["client"].get("/api/export.csv", params={"minutes": 60})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.text.splitlines()
    assert lines[0].startswith("# source=simulation;") and "SIMULADOS" in lines[0]
    assert lines[1].startswith("t[ms],pv_v[V],pv_i[A],pv_p[W]") and len(lines) == 2 + 36
    assert r.headers["x-sem-source"] == "simulation"


def test_stream_sse(env: dict[str, Any]) -> None:
    c = env["client"]
    with c.stream("GET", "/api/stream", params={"limit": 1}) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    assert "event: telemetry" in body and '"seq":35' in body

    def later() -> None:
        time.sleep(0.3)
        env["store"].insert_event({"schema": "sem.event.v1", "ts_ms": T0, "source": "simulation",
                                   "level": "warn", "code": "LVD_TRIP", "msg": "lvd"})

    th = threading.Thread(target=later)
    th.start()
    with c.stream("GET", "/api/stream", params={"limit": 2}) as r:
        body = "".join(r.iter_text())
    th.join()
    events = [b for b in body.split("\n\n") if b.strip()]
    assert len(events) == 2 and "event: event" in events[1] and "LVD_TRIP" in events[1]


def test_cmd_mode_auth_and_publish(env: dict[str, Any]) -> None:
    c, pub = env["client"], env["published"]
    assert c.post("/api/cmd/mode", json={"mode": "rules"}).status_code == 401
    assert c.post("/api/cmd/mode", json={"mode": "rules"}, headers={"Authorization": "Bearer x"}).status_code == 401
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert c.post("/api/cmd/mode", json={"mode": "turbo"}, headers=auth).status_code == 422
    r = c.post("/api/cmd/mode", json={"mode": "predictive"}, headers=auth)
    assert r.status_code == 202 and r.json()["published"] is True and "source" in r.json()
    topic, payload = pub[-1]
    assert topic == "smartenergy/cmd"
    cmd = verify_envelope(payload, KEY.encode())
    assert cmd["type"] == "set_mode" and cmd["args"] == {"mode": "predictive"} and cmd["id"] == r.json()["id"]


def test_cmd_mode_unavailable_paths(tmp_path: Path) -> None:
    cfg = load_config()
    store = SqliteStore(":memory:", cfg=cfg)

    def broken(_t: str, _p: str) -> None:
        raise MqttUnavailableError("conexão recusada")

    settings = ApiSettings(results_dir=tmp_path, dashboard_dir=None)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    c = TestClient(create_app(settings, cfg=cfg, store=store, cmd_publisher=broken, environ=ENV))
    r = c.post("/api/cmd/mode", json={"mode": "rules"}, headers=auth)
    assert r.status_code == 503 and "MQTT" in r.json()["detail"]
    c2 = TestClient(create_app(settings, cfg=cfg, store=store, cmd_publisher=broken, environ={}))
    assert c2.post("/api/cmd/mode", json={"mode": "rules"}, headers=auth).status_code == 503  # sem token
    c3 = TestClient(create_app(settings, cfg=cfg, store=store, cmd_publisher=broken,
                               environ={"SEM_API_TOKEN": TOKEN}))
    r3 = c3.post("/api/cmd/mode", json={"mode": "rules"}, headers=auth)
    assert r3.status_code == 503 and "SEM_CMD_KEY" in r3.json()["detail"]


def test_cors_restricted_to_localhost(env: dict[str, Any]) -> None:
    c = env["client"]
    ok = c.get("/api/meta", headers={"Origin": "http://localhost:5173"})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    bad = c.get("/api/meta", headers={"Origin": "http://evil.example.com"})
    assert "access-control-allow-origin" not in bad.headers


def test_dashboard_static_served(env: dict[str, Any]) -> None:
    r = env["client"].get("/")
    assert r.status_code == 200 and "SEM dashboard" in r.text


# --------------------------------------------------------------------------- modo demonstração
def _demo_doc() -> dict[str, Any]:
    live = synthetic_telemetry(T0, 1)
    live["source"] = "hardware"  # mesmo que o arquivo esteja errado, a API força "simulation"
    t = [T0 + k * 300_000 for k in range(12)]
    return {
        "schema": "sem.demo.v1", "generated_at": "2026-11-05T12:00:00Z", "source": "simulation",
        "disclaimer": "Dados SIMULADOS - nao sao medicoes de hardware.",
        "meta": {"device_id": "sem-esp32-01", "connected": True},
        "live": live,
        "history": {"t": t, "series": {"soc": [0.5 + 0.01 * k for k in range(12)], "mode": ["rules"] * 12}},
        "energy_daily": {"days": [{"day": "2026-11-16", "pv_in": 1.0, "bat_chg": 0, "bat_dis": 0, "load": 0,
                                   "unserved": 0}]},
        "decisions": [_decision(1), _decision(2)],
        "events": [],
        "forecast": {"method": "perfect", "t": t[:2], "pv_fc_w": [1.0, 2.0], "load_fc_w": [3.0, 3.0],
                     "soc_plan": None},
        "comparison": {
            "scenarios": ["ensolarado"], "strategies": ["rules", "predictive"], "seeds": [1, 2, 3],
            "metrics_def": {"service_rate": {"label": "x", "unit": "1", "better": "higher"}},
            "table": [{"scenario": "ensolarado", "strategy": "rules", "n": 3, "mean": {"service_rate": 0.9},
                       "std": {"service_rate": 0.01}},
                      {"scenario": "ensolarado", "strategy": "predictive", "n": 3, "mean": {"service_rate": 0.95},
                       "std": {"service_rate": 0.01}}],
            "series": {"ensolarado": {"t": t, "rules": {"soc_true": [0.5] * 12}, "predictive": {"soc_true": [0.6] * 12}}},
        },
    }


def test_demo_mode_is_always_simulation(tmp_path: Path) -> None:
    demo = tmp_path / "demo.json"
    demo.write_text(json.dumps(_demo_doc()))
    app = create_app(ApiSettings(demo=True, demo_path=demo, dashboard_dir=None), environ=ENV)
    c = TestClient(app)
    m = c.get("/api/meta").json()
    assert m["source"] == "simulation" and m["demo"] is True and m["connected"] is False and "config" in m
    assert c.get("/api/live").json()["source"] == "simulation"
    h = c.get("/api/history", params={"minutes": 30, "step_s": 60}).json()
    assert h["source"] == "simulation" and h["step_s"] == 300 and len(h["t"]) == 7  # passo nativo preservado
    assert h["series"]["soc"][-1] == pytest.approx(0.61)
    for route in ("/api/energy/daily", "/api/decisions", "/api/events", "/api/forecast", "/api/comparison",
                  "/api/experiments"):
        assert c.get(route).json()["source"] == "simulation", route
    items = c.get("/api/experiments").json()["items"]
    assert {i["id"] for i in items} == {"demo/ensolarado/rules", "demo/ensolarado/predictive"}
    exp = c.get("/api/experiments/demo/ensolarado/predictive").json()
    assert exp["metrics"]["service_rate"] == 0.95 and exp["series"]["soc_true"] == [0.6] * 12
    assert c.get("/api/experiments/demo/x/y").status_code == 404
    csv_text = c.get("/api/export.csv").text
    assert csv_text.startswith("# source=simulation;") and "SIMULADOS" in csv_text.splitlines()[0]
    with c.stream("GET", "/api/stream", params={"limit": 1}) as r:
        assert '"source":"simulation"' in "".join(r.iter_text())
    r = c.post("/api/cmd/mode", json={"mode": "rules"}, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 409


def test_demo_mode_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="export-demo"):
        create_app(ApiSettings(demo=True, demo_path=tmp_path / "nao.json", dashboard_dir=None), environ={})
