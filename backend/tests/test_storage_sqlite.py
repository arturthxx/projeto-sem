"""Testes do armazenamento SQLite, do achatamento §8 e da reamostragem."""

from __future__ import annotations

import csv
import io
import sqlite3
from pathlib import Path

import pytest

from smartenergy.security.schemas import schema_error
from smartenergy.storage.columns import flatten_telemetry, series_columns
from smartenergy.storage.resample import resample_columns
from smartenergy.storage.sqlite_store import SqliteStore
from smartenergy.storage.synthetic import synthetic_telemetry

T0 = 1763290800000  # instante fixo (ms)


@pytest.fixture()
def store(tmp_path: Path) -> SqliteStore:
    s = SqliteStore(tmp_path / "sem.sqlite")
    yield s
    s.close()


def test_synthetic_telemetry_is_schema_valid() -> None:
    assert schema_error("telemetry", synthetic_telemetry()) is None
    assert schema_error("telemetry", synthetic_telemetry(time_synced=False, source="hardware")) is None


def test_flatten_telemetry_columns_and_rules() -> None:
    p = synthetic_telemetry(T0, 7, soc=0.61, pv_p=20.0)
    p["meas"]["bus"] = {"v": None, "i": None, "p": None, "ok": False}
    p["sim"] = {"scenario": "x", "strategy": "rules", "seed": 1, "p_pv_avail": 25.0, "soc_true": 0.6}
    row = flatten_telemetry(p)
    assert row["t"] == T0 and row["source"] == "simulation"
    for c in series_columns((1, 2, 3)):
        assert c in row, c
    assert row["pv_p"] == 20.0 and row["soc"] == 0.61 and row["soc_unc"] == 0.05
    assert row["bus_v"] is None and row["bus_p"] is None            # ok=false -> ausente
    assert row["pv_p_avail"] == 25.0 and row["curtail_p"] == pytest.approx(5.0)
    assert row["soc_true"] == 0.6
    assert row["irr_poa"] is None                                   # irr.ok=false
    assert row["l1_on"] == 1 and row["l3_on"] == 0 and row["l2_duty"] == 0.6 and row["l1_d"] == 1
    assert row["def_done_wh"] == 12.5
    assert (row["mode"], row["state"], row["safety"]) == ("rules", "NORMAL", "OK")
    hw = flatten_telemetry(synthetic_telemetry(T0, 1, source="hardware"))
    assert hw["source"] == "hardware" and hw["soc_true"] is None and hw["pv_p_avail"] is None


def test_wal_mode_and_insert_requires_source(store: SqliteStore, tmp_path: Path) -> None:
    con = sqlite3.connect(tmp_path / "sem.sqlite")
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    con.close()
    p = synthetic_telemetry(T0, 0)
    del p["source"]
    with pytest.raises(ValueError):
        store.insert_telemetry(p)
    assert store.latest() is None


def test_latest_and_history_resampling(store: SqliteStore) -> None:
    # 24 pacotes a cada 5 s = 2 janelas de 60 s; SOC cresce 0.001 por pacote
    for i in range(24):
        store.insert_telemetry(synthetic_telemetry(T0 + i * 5000, i, soc=0.5 + 0.001 * i, pv_p=10.0 + i))
    assert store.latest()["seq"] == 23
    h = store.history(minutes=10, step_s=60)
    assert h["source"] == "simulation" and h["step_s"] == 60
    assert h["t"] == [T0 - T0 % 60000 + k * 60000 for k in range(len(h["t"]))]
    assert set(series_columns((1, 2, 3))) <= set(h["series"])
    # média dos valores de cada janela
    tt = [T0 + i * 5000 for i in range(24)]
    for k, tb in enumerate(h["t"]):
        idx = [i for i, t in enumerate(tt) if tb <= t < tb + 60000]
        assert h["series"]["soc"][k] == pytest.approx(sum(0.5 + 0.001 * i for i in idx) / len(idx), abs=1e-4)
        assert h["series"]["pv_p"][k] == pytest.approx(sum(10.0 + i for i in idx) / len(idx), abs=1e-4)
    assert h["series"]["mode"][0] == "rules"
    assert h["series"]["soc_true"] == [None] * len(h["t"])  # hardware/sintético sem bloco sim
    assert h["time_synced_frac"] == 1.0


def test_history_gaps_are_null_and_step_never_below_native(store: SqliteStore) -> None:
    store.insert_telemetry(synthetic_telemetry(T0, 0, soc=0.5))
    store.insert_telemetry(synthetic_telemetry(T0 + 5000, 1, soc=0.5))
    store.insert_telemetry(synthetic_telemetry(T0 + 300_000, 2, soc=0.6))  # lacuna de ~5 min
    h = store.history(minutes=60, step_s=60)
    assert h["series"]["soc"][0] == pytest.approx(0.5)
    assert None in h["series"]["soc"] and h["series"]["soc"][-1] == pytest.approx(0.6)
    r = resample_columns([0, 300_000, 600_000], {"x": [1.0, 2.0, 3.0]}, step_s=60)
    assert r["step_s"] == 300 and r["series"]["x"] == [1.0, 2.0, 3.0]  # passo nativo preservado


def test_sources_are_never_mixed_by_default(store: SqliteStore) -> None:
    for i in range(5):
        store.insert_telemetry(synthetic_telemetry(T0 + i * 5000, i, source="simulation", soc=0.2))
    for i in range(5):
        store.insert_telemetry(synthetic_telemetry(T0 + i * 5000, i, source="hardware", soc=0.8))
    h = store.history(minutes=10, step_s=60)                    # padrão: origem da última telemetria
    assert h["source"] == "hardware" and all(v == pytest.approx(0.8) for v in h["series"]["soc"] if v is not None)
    hs = store.history(minutes=10, step_s=60, source="simulation")
    assert hs["source"] == "simulation" and hs["series"]["soc"][0] == pytest.approx(0.2)
    assert store.history(minutes=10, step_s=60, source="all")["source"] == "mixed"
    with pytest.raises(ValueError):
        store.history(source="medido")


def test_unsynced_time_uses_receive_time(store: SqliteStore) -> None:
    store.insert_telemetry(synthetic_telemetry(T0, 0, time_synced=False), rx_ms=T0 + 123)
    h = store.history(minutes=5, step_s=1)
    assert h["t"] == [T0 + 123 - (T0 + 123) % 1000] and h["time_synced_frac"] == 0.0


def test_energy_daily_takes_daily_maximum(store: SqliteStore) -> None:
    store.insert_telemetry(synthetic_telemetry(T0, 0, day="2025-11-16", energy_scale=0.5))
    store.insert_telemetry(synthetic_telemetry(T0 + 5000, 1, day="2025-11-16", energy_scale=1.0))
    store.insert_telemetry(synthetic_telemetry(T0 + 86_400_000, 2, day="2025-11-17", energy_scale=0.2))
    store.insert_telemetry(synthetic_telemetry(T0, 3, time_synced=False))  # day=null: ignorado
    e = store.energy_daily(days=14)
    assert e["source"] == "simulation"
    assert [d["day"] for d in e["days"]] == ["2025-11-16", "2025-11-17"]
    assert e["days"][0]["pv_in"] == 100.0 and e["days"][0]["unserved"] == 1.0
    assert e["days"][1]["load"] == pytest.approx(14.0)
    assert len(store.energy_daily(days=1)["days"]) == 1


def test_decisions_events_status_acks(store: SqliteStore) -> None:
    for i in range(3):
        store.insert_decision({"schema": "sem.decision.v1", "ts_ms": T0 + i, "source": "simulation",
                               "origin": "firmware", "mode": "rules", "state": "NORMAL", "safety": "OK",
                               "reason": f"r{i}", "actions": [], "inputs": {}, "plan": None})
    store.insert_event({"schema": "sem.event.v1", "ts_ms": T0, "source": "hardware", "level": "warn",
                        "code": "LVD_TRIP", "msg": "x"})
    d = store.decisions(limit=2)
    assert d["source"] == "simulation" and [x["reason"] for x in d["items"]] == ["r2", "r1"]
    ev = store.events(limit=10)
    assert ev["source"] == "hardware" and ev["items"][0]["code"] == "LVD_TRIP"
    store.insert_telemetry(synthetic_telemetry(T0, 0, source="simulation"))
    assert store.events(limit=10)["items"] == []          # padrão filtra pela origem da telemetria
    assert len(store.events(limit=10, source="all")["items"]) == 1
    store.insert_status({"status": "online", "device_id": "sem-esp32-01"})
    store.insert_ack({"id": "c-1", "accepted": False, "reason": "replay"})
    assert store.last_seen()["status"] == "online"
    assert store.acks()[0]["reason"] == "replay"
    with pytest.raises(ValueError):
        store.insert_event({"schema": "sem.event.v1", "ts_ms": 0, "level": "info", "code": "BOOT", "msg": ""})


def test_export_csv_header_and_source(store: SqliteStore) -> None:
    for i in range(3):
        store.insert_telemetry(synthetic_telemetry(T0 + i * 5000, i, source="hardware", soc=0.7))
    text = store.export_csv(minutes=10)
    lines = text.splitlines()
    assert lines[0].startswith("# source=hardware;") and "SIMULADOS" not in lines[0]
    rows = list(csv.reader(io.StringIO("\n".join(lines[1:]))))
    assert rows[0][0] == "t[ms]" and "soc[1]" in rows[0] and "pv_p[W]" in rows[0] and "mode[text]" in rows[0]
    assert len(rows) == 1 + 3
    i_soc = rows[0].index("soc[1]")
    assert float(rows[1][i_soc]) == pytest.approx(0.7)
    sim = SqliteStore(":memory:")
    sim.insert_telemetry(synthetic_telemetry(T0, 0))
    first = sim.export_csv(minutes=10, step_s=60).splitlines()[0]
    assert first.startswith("# source=simulation;") and "SIMULADOS" in first


def test_extra_load_column_is_added(store: SqliteStore) -> None:
    p = synthetic_telemetry(T0, 0)
    p["loads"].append({"id": 4, "key": "x", "prio": "low", "demand": True, "on": True, "duty": 1.0, "p": 2.0,
                       "p_src": "measured", "fault": False})
    store.insert_telemetry(p)
    con = store._conn  # noqa: SLF001 - verificação do esquema
    assert con.execute('SELECT "l4_p" FROM telemetry').fetchone()[0] == 2.0


def test_stream_cursor_and_experiments(store: SqliteStore) -> None:
    assert store.max_id("telemetry") == 0
    store.insert_telemetry(synthetic_telemetry(T0, 0))
    store.insert_telemetry(synthetic_telemetry(T0 + 5000, 1))
    rows = store.rows_after("telemetry", 1)
    assert [r[1]["seq"] for r in rows] == [1] and store.max_id("telemetry") == 2
    store.upsert_experiment("bancada-1", "ensolarado", "rules", 1, {"service_rate": 0.9}, source="hardware")
    exp = store.get_experiment("bancada-1")
    assert exp["source"] == "hardware" and exp["metrics"] == {"service_rate": 0.9}
    assert store.list_experiments()[0]["id"] == "bancada-1"
    assert store.counts()["telemetry"] == 2
