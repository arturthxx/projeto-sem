"""Ingestão MQTT → armazenamento (docs/05 §2).

Assina ``<prefixo>/telemetry``, ``/ems/decision``, ``/system/event``,
``/system/status`` e ``/cmd/ack``; valida cada mensagem contra os esquemas
normativos (``schemas/``) e grava no store. Mensagens inválidas são
**rejeitadas e registradas** (log + tabela ``rejected``) sem derrubar o
processo. A reconexão ao broker é automática (paho-mqtt, atraso exponencial de
1 a 30 s) e as assinaturas são refeitas a cada conexão.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from typing import Any, Callable, Protocol

from smartenergy.ingest.mqtt_conn import MqttSettings, Topics, make_client
from smartenergy.security.schemas import schema_error

log = logging.getLogger("smartenergy.ingest")

#: tipo lógico -> sufixo do tópico
KINDS: dict[str, str] = {
    "telemetry": "telemetry",
    "decision": "ems/decision",
    "event": "system/event",
    "status": "system/status",
    "ack": "cmd/ack",
}
QOS: dict[str, int] = {"telemetry": 0, "decision": 1, "event": 1, "status": 1, "ack": 1}

StoredCallback = Callable[[str, dict[str, Any]], None]


class Store(Protocol):
    """Interface mínima de armazenamento usada pela ingestão."""

    def insert_telemetry(self, payload: dict[str, Any], rx_ms: int | None = None) -> Any: ...
    def insert_decision(self, payload: dict[str, Any], rx_ms: int | None = None) -> Any: ...
    def insert_event(self, payload: dict[str, Any], rx_ms: int | None = None) -> Any: ...
    def insert_status(self, payload: dict[str, Any], rx_ms: int | None = None) -> Any: ...
    def insert_ack(self, payload: dict[str, Any], rx_ms: int | None = None) -> Any: ...
    def insert_rejected(self, topic: str, error: str, raw: bytes | str, rx_ms: int | None = None) -> Any: ...


def _status_error(doc: Any) -> str | None:
    if not isinstance(doc, dict):
        return "status deve ser objeto JSON"
    if doc.get("status") not in ("online", "offline"):
        return "status: deve ser 'online' ou 'offline'"
    return None


def _ack_error(doc: Any) -> str | None:
    if not isinstance(doc, dict):
        return "ack deve ser objeto JSON"
    if not isinstance(doc.get("id"), str) or not doc["id"]:
        return "id: string obrigatória"
    if not isinstance(doc.get("accepted"), bool):
        return "accepted: booleano obrigatório"
    return None


def validation_error(kind: str, doc: Any) -> str | None:
    """Erro de validação (ou ``None``) de uma mensagem do tipo ``kind``."""
    if kind in ("telemetry", "decision", "event"):
        return schema_error(kind, doc)
    if kind == "status":
        return _status_error(doc)
    if kind == "ack":
        return _ack_error(doc)
    return f"tipo desconhecido: {kind}"


class MessageHandler:
    """Lógica de ingestão independente do transporte (testável sem broker)."""

    def __init__(self, stores: Store | list[Store], topics: Topics | None = None,
                 on_stored: StoredCallback | None = None) -> None:
        self.stores: list[Store] = stores if isinstance(stores, list) else [stores]
        self.topics = topics or Topics()
        self.on_stored = on_stored
        self.stats: Counter[str] = Counter()
        self._by_topic = {f"{self.topics.prefix}/{suffix}": kind for kind, suffix in KINDS.items()}

    @property
    def subscriptions(self) -> list[tuple[str, int]]:
        return [(f"{self.topics.prefix}/{suffix}", QOS[kind]) for kind, suffix in KINDS.items()]

    def kind_of(self, topic: str) -> str | None:
        return self._by_topic.get(topic)

    def handle(self, topic: str, raw: bytes | str, rx_ms: int | None = None) -> bool:
        """Valida e grava uma mensagem. Retorna ``True`` se armazenada. Nunca levanta."""
        rx = int(rx_ms if rx_ms is not None else time.time() * 1000)
        kind = self.kind_of(topic)
        self.stats["received"] += 1
        if kind is None:
            self.stats["ignored"] += 1
            return False
        try:
            doc = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            return self._reject(topic, kind, f"JSON inválido: {exc}", raw, rx)
        err = validation_error(kind, doc)
        if err is not None:
            return self._reject(topic, kind, err, raw, rx)
        try:
            for st in self.stores:
                getattr(st, f"insert_{kind}")(doc, rx_ms=rx)
        except Exception as exc:  # erro de armazenamento não derruba a ingestão
            log.exception("falha ao gravar %s", kind)
            self.stats[f"error_{kind}"] += 1
            self.stats["errors"] += 1
            self._record_rejected(topic, f"falha de armazenamento: {exc}", raw, rx)
            return False
        self.stats[f"stored_{kind}"] += 1
        self.stats["stored"] += 1
        if self.on_stored is not None:
            try:
                self.on_stored(kind, doc)
            except Exception:  # pragma: no cover - callback do usuário
                log.exception("callback on_stored falhou")
        return True

    def _reject(self, topic: str, kind: str, err: str, raw: bytes | str, rx: int) -> bool:
        self.stats[f"rejected_{kind}"] += 1
        self.stats["rejected"] += 1
        log.warning("mensagem rejeitada em %s: %s", topic, err)
        self._record_rejected(topic, err, raw, rx)
        return False

    def _record_rejected(self, topic: str, err: str, raw: bytes | str, rx: int) -> None:
        for st in self.stores:
            rec = getattr(st, "insert_rejected", None)
            if rec is None:
                continue
            try:
                rec(topic, err, raw, rx_ms=rx)
            except Exception:  # pragma: no cover
                log.exception("falha ao registrar mensagem rejeitada")


class MqttIngest:
    """Cliente MQTT de ingestão (thread de rede do paho, reconexão automática)."""

    def __init__(
        self,
        stores: Store | list[Store],
        settings: MqttSettings | None = None,
        topics: Topics | None = None,
        on_stored: StoredCallback | None = None,
    ) -> None:
        self.settings = settings or MqttSettings.from_env()
        self.handler = MessageHandler(stores, topics, on_stored)
        self.connected = threading.Event()
        self._client = make_client(self.settings, "ingest")
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    @property
    def stats(self) -> Counter[str]:
        return self.handler.stats

    # callbacks paho (API v2) ------------------------------------------------
    def _on_connect(self, client: Any, _u: Any, _f: Any, reason_code: Any, _p: Any = None) -> None:
        if getattr(reason_code, "is_failure", False):
            log.error("conexão MQTT recusada: %s", reason_code)
            return
        client.subscribe(self.handler.subscriptions)
        self.handler.stats["connects"] += 1
        self.connected.set()
        log.info("MQTT conectado a %s:%s; assinaturas: %s", self.settings.host, self.settings.port,
                 ", ".join(t for t, _ in self.handler.subscriptions))

    def _on_disconnect(self, _c: Any, _u: Any, _f: Any, reason_code: Any, _p: Any = None) -> None:
        self.connected.clear()
        log.warning("MQTT desconectado (%s); reconexão automática", reason_code)

    def _on_message(self, _c: Any, _u: Any, msg: Any) -> None:
        try:
            self.handler.handle(msg.topic, msg.payload)
        except Exception:  # pragma: no cover - handle() já não levanta
            log.exception("erro inesperado ao processar %s", getattr(msg, "topic", "?"))

    # ciclo de vida -------------------------------------------------------------
    def start(self) -> None:
        """Conecta em segundo plano (thread do paho); tenta de novo se o broker cair."""
        self._client.connect_async(self.settings.host, self.settings.port, keepalive=self.settings.keepalive_s)
        self._client.loop_start()

    def wait_connected(self, timeout_s: float = 10.0) -> bool:
        return self.connected.wait(timeout_s)

    def stop(self) -> None:
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()

    def run_forever(self) -> None:
        """Bloqueia até interrupção; reconecta inclusive se o broker não estiver no ar ao iniciar."""
        self._client.connect_async(self.settings.host, self.settings.port, keepalive=self.settings.keepalive_s)
        self._client.loop_forever(retry_first_connection=True)
