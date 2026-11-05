"""Subcomando ``python -m smartenergy ingest`` — assina o MQTT e armazena no SQLite.

Exemplo::

    export SEM_MQTT_HOST=localhost SEM_MQTT_USER=sem-backend SEM_MQTT_PASSWORD=...
    python -m smartenergy ingest --db backend/data/sem.sqlite [--influx]

Mensagens inválidas são rejeitadas, registradas (log + tabela ``rejected``) e o
processo continua. Encerre com Ctrl+C.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading

from smartenergy.config import load_config
from smartenergy.ingest.mqtt_conn import MqttSettings, Topics
from smartenergy.ingest.mqtt_ingest import MqttIngest
from smartenergy.storage.sqlite_store import SqliteStore, default_db_path


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="smartenergy ingest", description="Ingestão MQTT -> SQLite (WAL).")
    p.add_argument("--db", default=None, help=f"arquivo SQLite (padrão $SEM_DB_PATH ou {default_db_path()})")
    p.add_argument("--config", default=None, help="system.yaml (padrão $SEM_CONFIG ou config/system.yaml)")
    p.add_argument("--host", default=None, help="broker MQTT (padrão $SEM_MQTT_HOST ou localhost)")
    p.add_argument("--port", type=int, default=None, help="porta (padrão $SEM_MQTT_PORT ou 1883)")
    p.add_argument("--influx", action="store_true", help="grava também no InfluxDB ($SEM_INFLUX_*; opcional)")
    p.add_argument("--stats-every", type=float, default=60.0, help="intervalo (s) do resumo no log")
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("smartenergy.ingest")
    cfg = load_config(args.config)
    stores: list = [SqliteStore(args.db, cfg=cfg)]
    if args.influx:
        from smartenergy.storage.influx_store import InfluxStore, InfluxUnavailableError

        try:
            stores.append(InfluxStore.from_env())
        except InfluxUnavailableError as exc:
            print(f"erro: {exc}", file=sys.stderr)
            return 2
    settings = MqttSettings.from_env(host=args.host, port=args.port)
    ing = MqttIngest(stores, settings, Topics.from_config(cfg))
    stop = threading.Event()

    def report() -> None:
        while not stop.wait(args.stats_every):
            log.info("estatísticas: %s", dict(ing.stats))

    threading.Thread(target=report, daemon=True).start()
    log.info("ingestão: broker %s:%s -> %s", settings.host, settings.port, stores[0].path)
    try:
        ing.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        log.info("encerrando; estatísticas: %s", dict(ing.stats))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
