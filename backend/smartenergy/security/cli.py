"""Subcomando ``python -m smartenergy sign-cmd`` — assina (e opcionalmente publica) um comando.

Exemplos::

    export SEM_CMD_KEY='uma-frase-longa-e-aleatoria'
    python -m smartenergy sign-cmd --type set_mode --args '{"mode":"rules"}'
    python -m smartenergy sign-cmd --type reset_faults --publish --host 192.168.0.10
    python -m smartenergy sign-cmd --write-vectors backend/tests/data/hmac_vectors.json

Sem ``--publish`` o envelope é apenas impresso (stdout) — útil para testar o
firmware com ``mosquitto_pub``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from smartenergy.ingest.mqtt_conn import MqttSettings, MqttUnavailableError, Topics, publish_once
from smartenergy.security.hmac_cmd import (
    CMD_TYPES,
    KEY_ENV_VAR,
    CommandError,
    build_command,
    envelope_to_json,
    generate_vectors,
    load_key,
    sign_command,
)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="smartenergy sign-cmd", description="Assina um comando sem.cmd.v1 (HMAC-SHA256).")
    p.add_argument("--type", choices=CMD_TYPES, help="tipo do comando")
    p.add_argument("--args", default="{}", help="argumentos JSON (ex.: '{\"mode\":\"rules\"}')")
    p.add_argument("--ttl", type=int, default=60, help="validade em segundos (1..3600; padrão 60)")
    p.add_argument("--id", default=None, help="id do comando (gerado se omitido)")
    p.add_argument("--ts-ms", type=int, default=None, help="instante de emissão em epoch ms (padrão: agora)")
    p.add_argument("--key-env", default=KEY_ENV_VAR, help=f"variável de ambiente com a chave (padrão {KEY_ENV_VAR})")
    p.add_argument("--publish", action="store_true", help="publica o envelope em <prefixo>/cmd (QoS 1)")
    p.add_argument("--host", default=None, help="broker MQTT (padrão $SEM_MQTT_HOST ou localhost)")
    p.add_argument("--port", type=int, default=None, help="porta do broker (padrão $SEM_MQTT_PORT ou 1883)")
    p.add_argument("--prefix", default=None, help="prefixo dos tópicos (padrão comm.mqtt_prefix)")
    p.add_argument("--write-vectors", metavar="ARQUIVO", default=None,
                   help="grava os vetores de teste cruzado HMAC (chave pública de teste) e sai")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.write_vectors:
        out = Path(args.write_vectors)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(generate_vectors(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"vetores gravados em {out}")
        return 0

    if not args.type:
        print("erro: --type é obrigatório", file=sys.stderr)
        return 2
    try:
        cmd_args = json.loads(args.args)
    except ValueError as exc:
        print(f"erro: --args não é JSON válido: {exc}", file=sys.stderr)
        return 2
    try:
        key = load_key(args.key_env)
        cmd = build_command(args.type, cmd_args, args.ttl, id=args.id, ts_ms=args.ts_ms)
        env = sign_command(cmd, key)
    except CommandError as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 2

    text = envelope_to_json(env)
    print(text)
    if not args.publish:
        return 0

    prefix = args.prefix
    if prefix is None:
        try:
            from smartenergy.config import load_config

            prefix = Topics.from_config(load_config()).prefix
        except (OSError, ValueError):
            prefix = Topics().prefix
    topic = Topics(prefix).cmd
    settings = MqttSettings.from_env(host=args.host, port=args.port)
    try:
        publish_once(settings, topic, text, qos=1)
    except MqttUnavailableError as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 3
    print(f"publicado em {topic} ({settings.host}:{settings.port}); aguarde o ack em {topic}/ack", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
