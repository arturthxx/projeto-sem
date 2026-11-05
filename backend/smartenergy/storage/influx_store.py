"""Armazenamento OPCIONAL em InfluxDB 2.x (séries temporais para Grafana).

Requer ``pip install -e .[influx]`` (pacote ``influxdb-client``), importado de
forma tardia: o restante do backend funciona sem ele. **Não testado neste
ambiente** (sem servidor InfluxDB disponível); o caminho padrão e testado é o
:class:`~smartenergy.storage.sqlite_store.SqliteStore`.

Variáveis de ambiente (ver ``infra/.env.example``): ``SEM_INFLUX_URL``,
``SEM_INFLUX_TOKEN``, ``SEM_INFLUX_ORG``, ``SEM_INFLUX_BUCKET``.

Modelo de dados (line protocol):

* ``telemetry``: tags ``device_id``, ``source``; campos = colunas numéricas do §8
  (docs/05) + ``mode``/``state``/``safety`` como campos texto; tempo = ``t`` (ms);
* ``decision``: tags ``source``, ``origin``, ``mode``; campos ``state``, ``reason``;
* ``event``: tags ``source``, ``level``, ``code``; campo ``msg``.
"""

from __future__ import annotations

import os
from typing import Any, Mapping

from smartenergy.storage.columns import TEXT_COLUMNS, flatten_telemetry


class InfluxUnavailableError(RuntimeError):
    """Pacote ``influxdb-client`` ausente ou configuração incompleta."""


class InfluxStore:
    """Escrita de telemetria/decisões/eventos no InfluxDB 2.x (somente escrita)."""

    def __init__(self, url: str, token: str, org: str, bucket: str) -> None:
        try:
            from influxdb_client import InfluxDBClient  # import tardio (dependência opcional)
            from influxdb_client.client.write_api import SYNCHRONOUS
        except ImportError as exc:
            raise InfluxUnavailableError(
                "influxdb-client não instalado: pip install -e '.[influx]'") from exc
        self.bucket = bucket
        self.org = org
        self._client = InfluxDBClient(url=url, token=token, org=org)
        self._write = self._client.write_api(write_options=SYNCHRONOUS)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "InfluxStore":
        env = os.environ if environ is None else environ
        missing = [k for k in ("SEM_INFLUX_URL", "SEM_INFLUX_TOKEN", "SEM_INFLUX_ORG", "SEM_INFLUX_BUCKET")
                   if not env.get(k)]
        if missing:
            raise InfluxUnavailableError(f"variáveis ausentes: {', '.join(missing)}")
        return cls(env["SEM_INFLUX_URL"], env["SEM_INFLUX_TOKEN"], env["SEM_INFLUX_ORG"], env["SEM_INFLUX_BUCKET"])

    def _point(self, measurement: str, tags: Mapping[str, Any], fields: Mapping[str, Any], t_ms: int) -> Any:
        from influxdb_client import Point, WritePrecision

        p = Point(measurement).time(int(t_ms), WritePrecision.MS)
        for k, v in tags.items():
            if v is not None:
                p = p.tag(k, str(v))
        for k, v in fields.items():
            if v is not None:
                p = p.field(k, v)
        return p

    def insert_telemetry(self, payload: Mapping[str, Any], rx_ms: int | None = None) -> None:
        flat = flatten_telemetry(payload, rx_ms)
        t = int(flat.pop("t"))
        tags = {"device_id": payload.get("device_id"), "source": flat.pop("source")}
        fields = {k: (v if k in TEXT_COLUMNS else float(v)) for k, v in flat.items() if v is not None}
        self._write.write(bucket=self.bucket, org=self.org, record=self._point("telemetry", tags, fields, t))

    def insert_decision(self, payload: Mapping[str, Any], rx_ms: int | None = None) -> None:
        tags = {"source": payload.get("source"), "origin": payload.get("origin"), "mode": payload.get("mode")}
        fields = {"state": payload.get("state"), "reason": payload.get("reason")}
        self._write.write(bucket=self.bucket, org=self.org,
                          record=self._point("decision", tags, fields, int(payload.get("ts_ms") or rx_ms or 0)))

    def insert_event(self, payload: Mapping[str, Any], rx_ms: int | None = None) -> None:
        tags = {"source": payload.get("source"), "level": payload.get("level"), "code": payload.get("code")}
        fields = {"msg": payload.get("msg")}
        self._write.write(bucket=self.bucket, org=self.org,
                          record=self._point("event", tags, fields, int(payload.get("ts_ms") or rx_ms or 0)))

    def close(self) -> None:
        self._client.close()
