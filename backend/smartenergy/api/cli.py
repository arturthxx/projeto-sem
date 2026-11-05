"""Subcomando ``python -m smartenergy serve`` — API HTTP + dashboard estático.

Exemplos::

    python -m smartenergy serve                         # 127.0.0.1:8000, SQLite padrão
    python -m smartenergy serve --host 0.0.0.0 --port 8000 --db backend/data/sem.sqlite --ingest
    python -m smartenergy serve --demo                  # dashboard/data/demo.json (dados SIMULADOS)

``--host 0.0.0.0`` expõe a API na rede local (necessário para abrir o dashboard
de outro computador/celular). Comandos (``POST /api/cmd/mode``) exigem
``SEM_API_TOKEN`` e ``SEM_CMD_KEY``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from smartenergy.api.app import ApiSettings, create_app
from smartenergy.ingest.mqtt_conn import MqttSettings


def _parser() -> argparse.ArgumentParser:
    d = ApiSettings.__dataclass_fields__
    p = argparse.ArgumentParser(prog="smartenergy serve", description="API HTTP (FastAPI) + dashboard.")
    p.add_argument("--host", default="127.0.0.1", help="endereço (padrão 127.0.0.1; 0.0.0.0 = rede local)")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--db", default=None, help="arquivo SQLite (padrão $SEM_DB_PATH ou backend/data/sem.sqlite)")
    p.add_argument("--config", default=None, help="system.yaml (padrão $SEM_CONFIG ou config/system.yaml)")
    p.add_argument("--demo", action="store_true", help="serve dashboard/data/demo.json (dados SIMULADOS)")
    p.add_argument("--demo-file", default=str(d["demo_path"].default), help="caminho do demo.json")
    p.add_argument("--results", default=str(d["results_dir"].default), help="diretório de resultados de experimentos")
    p.add_argument("--dashboard", default=str(d["dashboard_dir"].default), help="diretório estático do dashboard")
    p.add_argument("--ingest", action="store_true", help="roda também a ingestão MQTT neste processo")
    p.add_argument("--mqtt-host", default=None, help="broker MQTT (padrão $SEM_MQTT_HOST ou localhost)")
    p.add_argument("--mqtt-port", type=int, default=None, help="porta MQTT (padrão $SEM_MQTT_PORT ou 1883)")
    p.add_argument("--log-level", default="info")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        import uvicorn
    except ImportError:  # pragma: no cover
        print("erro: uvicorn não instalado (pip install -e .)", file=sys.stderr)
        return 2
    settings = ApiSettings(
        db_path=args.db, demo=args.demo, demo_path=Path(args.demo_file), results_dir=Path(args.results),
        dashboard_dir=Path(args.dashboard), config_path=args.config, ingest=args.ingest,
        mqtt=MqttSettings.from_env(host=args.mqtt_host, port=args.mqtt_port),
    )
    try:
        app = create_app(settings)
    except FileNotFoundError as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 2
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
