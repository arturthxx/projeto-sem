"""Testes da ingestão MQTT: lógica de validação e ponta a ponta com broker amqtt em processo."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import warnings
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from smartenergy.ingest.mqtt_conn import MqttSettings, MqttUnavailableError, Topics, make_client, publish_once
from smartenergy.ingest.mqtt_ingest import MessageHandler, MqttIngest
from smartenergy.security import cli as sign_cli
from smartenergy.security.hmac_cmd import verify_envelope
from smartenergy.storage.sqlite_store import SqliteStore
from smartenergy.storage.synthetic import synthetic_telemetry

T0 = 1763290800000
KEY = "chave-de-teste-com-32-bytes-ok!!"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class InProcessBroker:
    """Broker amqtt rodando em thread própria (porta livre aleatória)."""

    def __init__(self) -> None:
        self.port = _free_port()
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._broker: Any = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        from amqtt.broker import Broker

        asyncio.set_event_loop(self._loop)
        cfg = {
            "listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{self.port}"}},
            "plugins": {"amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True}},
        }
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self._broker = Broker(cfg, loop=self._loop)
            self._loop.run_until_complete(self._broker.start())
        self._ready.set()
        self._loop.run_forever()

    def __enter__(self) -> "InProcessBroker":
        self._thread.start()
        assert self._ready.wait(15), "broker amqtt não iniciou"
        return self

    def __exit__(self, *exc: Any) -> None:
        fut = asyncio.run_coroutine_threadsafe(self._broker.shutdown(), self._loop)
        try:
            fut.result(10)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(5)


@pytest.fixture(scope="module")
def broker() -> Iterator[InProcessBroker]:
    with InProcessBroker() as b:
        yield b


def _wait(cond: Callable[[], bool], timeout_s: float = 10.0) -> bool:
    t_end = time.monotonic() + timeout_s
    while time.monotonic() < t_end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


# --------------------------------------------------------------------------- lógica
def test_handler_validates_and_stores() -> None:
    store = SqliteStore(":memory:")
    seen: list[str] = []
    h = MessageHandler(store, Topics("smartenergy"), on_stored=lambda kind, doc: seen.append(kind))
    good = json.dumps(synthetic_telemetry(T0, 1, source="hardware"))
    assert h.handle("smartenergy/telemetry", good.encode()) is True
    assert h.handle("smartenergy/telemetry", b"{nao json") is False
    bad = synthetic_telemetry(T0, 2)
    bad["soc"]["value"] = 1.7
    assert h.handle("smartenergy/telemetry", json.dumps(bad)) is False
    assert h.handle("smartenergy/system/status", json.dumps({"status": "online", "device_id": "d"})) is True
    assert h.handle("smartenergy/system/status", json.dumps({"status": "talvez"})) is False
    assert h.handle("smartenergy/cmd/ack", json.dumps({"id": "c-1", "accepted": True, "reason": ""})) is True
    assert h.handle("smartenergy/cmd/ack", json.dumps({"id": "c-1"})) is False
    assert h.handle("outro/topico", b"{}") is False
    c = store.counts()
    assert (c["telemetry"], c["status"], c["acks"], c["rejected"]) == (1, 1, 1, 4)
    assert store.latest()["source"] == "hardware"          # origem preservada
    assert h.stats["rejected"] == 4 and h.stats["ignored"] == 1 and seen == ["telemetry", "status", "ack"]
    errs = [r["error"] for r in store.rejected()]
    assert any("JSON" in e for e in errs) and any("soc/value" in e for e in errs)


def test_handler_survives_storage_errors() -> None:
    class Broken:
        def insert_telemetry(self, *_a: Any, **_k: Any) -> None:
            raise RuntimeError("disco cheio")

    h = MessageHandler(Broken())  # type: ignore[arg-type]
    assert h.handle("smartenergy/telemetry", json.dumps(synthetic_telemetry())) is False
    assert h.stats["errors"] == 1


def test_publish_once_reports_unavailable_broker() -> None:
    with pytest.raises(MqttUnavailableError):
        publish_once(MqttSettings(host="127.0.0.1", port=_free_port()), "x", "y", timeout_s=2)


# --------------------------------------------------------------------------- ponta a ponta
def test_ingest_end_to_end_with_broker(broker: InProcessBroker, tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "e2e.sqlite")
    settings = MqttSettings(host="127.0.0.1", port=broker.port)
    ing = MqttIngest(store, settings, Topics("smartenergy"))
    ing.start()
    try:
        assert ing.wait_connected(10)
        time.sleep(0.3)  # SUBACK
        pub = make_client(settings, "test-pub")
        pub.connect("127.0.0.1", broker.port)
        pub.loop_start()
        msgs = [
            ("smartenergy/telemetry", json.dumps(synthetic_telemetry(T0, 1, source="simulation"))),
            ("smartenergy/telemetry", json.dumps(synthetic_telemetry(T0 + 5000, 2, source="simulation"))),
            ("smartenergy/telemetry", '{"schema":"sem.telemetry.v1"}'),                  # inválida
            ("smartenergy/telemetry", "lixo"),                                            # não-JSON
            ("smartenergy/ems/decision", json.dumps({
                "schema": "sem.decision.v1", "ts_ms": T0, "source": "simulation", "origin": "firmware",
                "mode": "rules", "state": "ECONOMY", "prev_state": "NORMAL", "safety": "OK",
                "reason": "soc 0.68 < 0.70", "actions": [{"id": 2, "on": True, "duty": 0.6}],
                "inputs": {"soc": 0.68}, "plan": None})),
            ("smartenergy/system/event", json.dumps({
                "schema": "sem.event.v1", "ts_ms": T0, "source": "simulation", "level": "info",
                "code": "BOOT", "msg": "boot"})),
            ("smartenergy/system/status", json.dumps({"status": "online", "device_id": "sem-esp32-01"})),
            ("smartenergy/cmd/ack", json.dumps({"id": "c-9", "accepted": True, "reason": "ok", "ts_ms": T0})),
        ]
        for topic, payload in msgs:
            pub.publish(topic, payload, qos=1).wait_for_publish(5)
        ok = _wait(lambda: ing.stats["received"] >= len(msgs))
        pub.disconnect()
        pub.loop_stop()
        assert ok, dict(ing.stats)
    finally:
        ing.stop()
    c = store.counts()
    assert c["telemetry"] == 2 and c["decisions"] == 1 and c["events"] == 1
    assert c["status"] == 1 and c["acks"] == 1 and c["rejected"] == 2
    assert store.latest()["seq"] == 2 and store.latest()["source"] == "simulation"
    assert store.history(minutes=5, step_s=5)["series"]["soc"][0] == pytest.approx(0.73)


def test_sign_cmd_publish_end_to_end(broker: InProcessBroker, monkeypatch: pytest.MonkeyPatch) -> None:
    got: list[bytes] = []
    sub = make_client(MqttSettings(host="127.0.0.1", port=broker.port), "test-sub")
    sub.on_message = lambda _c, _u, m: got.append(m.payload)
    sub.connect("127.0.0.1", broker.port)
    sub.subscribe("smartenergy/cmd", qos=1)
    sub.loop_start()
    try:
        time.sleep(0.3)
        monkeypatch.setenv("SEM_CMD_KEY", KEY)
        rc = sign_cli.main(["--type", "set_mode", "--args", '{"mode":"rules"}', "--publish",
                            "--host", "127.0.0.1", "--port", str(broker.port), "--prefix", "smartenergy"])
        assert rc == 0
        assert _wait(lambda: bool(got))
    finally:
        sub.disconnect()
        sub.loop_stop()
    cmd = verify_envelope(got[0], KEY.encode())
    assert cmd["type"] == "set_mode" and cmd["args"] == {"mode": "rules"}


def test_api_cmd_mode_publishes_to_broker(broker: InProcessBroker, tmp_path: Path) -> None:
    """POST /api/cmd/mode assina e publica de verdade via MQTT (sem publicador falso)."""
    from fastapi.testclient import TestClient

    from smartenergy.api.app import ApiSettings, create_app

    got: list[bytes] = []
    sub = make_client(MqttSettings(host="127.0.0.1", port=broker.port), "test-sub2")
    sub.on_message = lambda _c, _u, m: got.append(m.payload)
    sub.connect("127.0.0.1", broker.port)
    sub.subscribe("smartenergy/cmd", qos=1)
    sub.loop_start()
    try:
        time.sleep(0.3)
        app = create_app(
            ApiSettings(db_path=str(tmp_path / "api.sqlite"), results_dir=tmp_path / "none", dashboard_dir=None,
                        mqtt=MqttSettings(host="127.0.0.1", port=broker.port)),
            environ={"SEM_API_TOKEN": "tok-123", "SEM_CMD_KEY": KEY})
        with TestClient(app) as client:
            r = client.post("/api/cmd/mode", json={"mode": "predictive"},
                            headers={"Authorization": "Bearer tok-123"})
        assert r.status_code == 202, r.text
        assert _wait(lambda: bool(got))
    finally:
        sub.disconnect()
        sub.loop_stop()
    cmd = verify_envelope(got[0], KEY.encode())
    assert cmd["id"] == r.json()["id"] and cmd["args"] == {"mode": "predictive"}
