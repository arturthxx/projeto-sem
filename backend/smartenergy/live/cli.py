"""Subcomando do EMS preditivo ao vivo (a ser registrado em ``smartenergy/cli.py``, ex.: ``live-ems``).

Exemplo::

    export SEM_CMD_KEY=... SEM_MQTT_HOST=localhost SEM_MQTT_USER=sem-backend SEM_MQTT_PASSWORD=...
    python -c "from smartenergy.live.cli import main; main([])"   # até o registro no cli.py
    python -m smartenergy live-ems --forecast clearsky_persistence [--dry-run]

O serviço só envia ``set_loads`` quando o dispositivo reporta ``ems.mode ==
"predictive"`` (mude o modo com ``POST /api/cmd/mode`` ou ``sign-cmd``).
"""

from __future__ import annotations

import argparse
import logging
import secrets
import sys

from smartenergy.config import load_config
from smartenergy.ingest.mqtt_conn import MqttSettings, Topics
from smartenergy.live.ems_service import LiveEmsRunner, LiveEmsService, PublishFn
from smartenergy.security.hmac_cmd import CommandError, load_key


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="smartenergy live-ems", description="EMS preditivo (MPC) ao vivo via MQTT.")
    p.add_argument("--config", default=None, help="system.yaml (padrão $SEM_CONFIG ou config/system.yaml)")
    p.add_argument("--host", default=None, help="broker MQTT (padrão $SEM_MQTT_HOST ou localhost)")
    p.add_argument("--port", type=int, default=None, help="porta (padrão $SEM_MQTT_PORT ou 1883)")
    p.add_argument("--forecast", default=None, help="método de previsão FV (padrão forecast.pv_method)")
    p.add_argument("--device-id", default=None, help="atende apenas este device_id (padrão: qualquer)")
    p.add_argument("--dry-run", action="store_true", help="não publica: apenas registra no log o que enviaria")
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    try:
        key = load_key()
    except CommandError as exc:
        if not args.dry_run:
            print(f"erro: {exc}", file=sys.stderr)
            return 2
        key = secrets.token_bytes(32)  # dry-run: chave efêmera, nada é publicado
    if args.forecast in ("perfect", "noisy"):
        print("erro: previsões 'perfect'/'noisy' existem apenas em simulação", file=sys.stderr)
        return 2
    topics = Topics.from_config(cfg)

    def factory(publish: PublishFn) -> LiveEmsService:
        return LiveEmsService(cfg, key, publish, forecast_method=args.forecast, device_id=args.device_id,
                              topics=topics)

    runner = LiveEmsRunner(factory, MqttSettings.from_env(host=args.host, port=args.port), topics, args.dry_run)
    try:
        runner.run_forever()
    except KeyboardInterrupt:
        pass
    logging.getLogger("smartenergy.live").info("encerrando; estatísticas: %s", dict(runner.service.stats))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
