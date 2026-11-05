"""Conexão MQTT comum aos serviços do backend (ingestão, API, EMS ao vivo, sign-cmd).

Configuração por variáveis de ambiente (nenhum segredo no repositório):

=====================  =====================================================
``SEM_MQTT_HOST``      endereço do broker (padrão ``localhost``)
``SEM_MQTT_PORT``      porta (padrão ``1883``; ``8883`` com TLS)
``SEM_MQTT_USER``      usuário do broker (ACL do Mosquitto: ``sem-backend``)
``SEM_MQTT_PASSWORD``  senha do usuário
``SEM_MQTT_TLS``       ``1`` para habilitar TLS
``SEM_MQTT_CA``        arquivo da CA (opcional, com TLS)
=====================  =====================================================

O prefixo dos tópicos vem de ``comm.mqtt_prefix`` (system.yaml).
"""

from __future__ import annotations

import os
import threading
import uuid
from dataclasses import dataclass, replace
from typing import Any, Mapping

from smartenergy.config import SystemConfig

DEFAULT_PREFIX = "smartenergy"


class MqttUnavailableError(RuntimeError):
    """Broker MQTT inacessível ou publicação não confirmada."""


@dataclass(frozen=True)
class MqttSettings:
    """Parâmetros de conexão ao broker."""

    host: str = "localhost"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    tls: bool = False
    ca_certs: str | None = None
    keepalive_s: int = 30
    client_id_prefix: str = "sem-backend"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **overrides: Any) -> "MqttSettings":
        """Lê ``SEM_MQTT_*``; argumentos não-``None`` em ``overrides`` têm precedência."""
        env = os.environ if environ is None else environ
        tls = env.get("SEM_MQTT_TLS", "0").strip().lower() in {"1", "true", "yes", "sim"}
        s = cls(
            host=env.get("SEM_MQTT_HOST", "localhost"),
            port=int(env.get("SEM_MQTT_PORT", "8883" if tls else "1883")),
            username=env.get("SEM_MQTT_USER") or None,
            password=env.get("SEM_MQTT_PASSWORD") or None,
            tls=tls,
            ca_certs=env.get("SEM_MQTT_CA") or None,
        )
        return replace(s, **{k: v for k, v in overrides.items() if v is not None})

    def client_id(self, role: str) -> str:
        return f"{self.client_id_prefix}-{role}-{uuid.uuid4().hex[:6]}"


@dataclass(frozen=True)
class Topics:
    """Tópicos MQTT do contrato (docs/05 §2)."""

    prefix: str = DEFAULT_PREFIX

    @property
    def telemetry(self) -> str:
        return f"{self.prefix}/telemetry"

    @property
    def decision(self) -> str:
        return f"{self.prefix}/ems/decision"

    @property
    def mode(self) -> str:
        return f"{self.prefix}/ems/mode"

    @property
    def event(self) -> str:
        return f"{self.prefix}/system/event"

    @property
    def status(self) -> str:
        return f"{self.prefix}/system/status"

    @property
    def cmd(self) -> str:
        return f"{self.prefix}/cmd"

    @property
    def ack(self) -> str:
        return f"{self.prefix}/cmd/ack"

    @classmethod
    def from_config(cls, cfg: SystemConfig | None) -> "Topics":
        if cfg is None:
            return cls()
        return cls(str(cfg.raw.get("comm", {}).get("mqtt_prefix", DEFAULT_PREFIX)))


def make_client(settings: MqttSettings, role: str) -> Any:
    """Cria um cliente paho-mqtt (API v2) com credenciais, TLS e reconexão exponencial."""
    import paho.mqtt.client as mqtt  # import tardio: só quem usa MQTT precisa do paho

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=settings.client_id(role))
    if settings.username:
        client.username_pw_set(settings.username, settings.password)
    if settings.tls:
        client.tls_set(ca_certs=settings.ca_certs)
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    return client


def publish_once(
    settings: MqttSettings,
    topic: str,
    payload: str,
    qos: int = 1,
    retain: bool = False,
    timeout_s: float = 5.0,
) -> None:
    """Conecta, publica uma mensagem (aguarda confirmação QoS) e desconecta.

    Raises:
        MqttUnavailableError: broker inacessível, conexão recusada ou publicação não confirmada.
    """
    client = make_client(settings, "pub")
    connected = threading.Event()
    result: dict[str, Any] = {}

    def on_connect(_c: Any, _u: Any, _f: Any, reason_code: Any, _p: Any = None) -> None:
        result["rc"] = reason_code
        connected.set()

    client.on_connect = on_connect
    try:
        client.connect(settings.host, settings.port, keepalive=settings.keepalive_s)
    except (OSError, ValueError) as exc:
        raise MqttUnavailableError(f"broker MQTT {settings.host}:{settings.port} inacessível: {exc}") from exc
    client.loop_start()
    try:
        if not connected.wait(timeout_s):
            raise MqttUnavailableError("tempo esgotado aguardando CONNACK do broker")
        rc = result.get("rc")
        if rc is not None and getattr(rc, "is_failure", False):
            raise MqttUnavailableError(f"conexão recusada pelo broker: {rc}")
        info = client.publish(topic, payload, qos=qos, retain=retain)
        try:
            info.wait_for_publish(timeout=timeout_s)
        except (RuntimeError, ValueError) as exc:
            raise MqttUnavailableError(f"publicação falhou: {exc}") from exc
        if not info.is_published():
            raise MqttUnavailableError("publicação não confirmada pelo broker")
    finally:
        client.disconnect()
        client.loop_stop()
