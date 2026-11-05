"""Ponto de entrada de linha de comando: ``python -m smartenergy <subcomando> [...]``.

Cada subcomando vive no seu próprio módulo e expõe ``main(argv: list[str]) -> int``.
A importação é preguiçosa para que dependências opcionais (ex.: paho-mqtt) só
sejam exigidas pelo subcomando que as usa.
"""

from __future__ import annotations

import importlib
import sys

SUBCOMMANDS: dict[str, tuple[str, str]] = {
    "simulate": ("smartenergy.simulation.cli", "Executa uma simulação (cenário × estratégia × semente)."),
    "compare": ("smartenergy.experiments.cli", "Compara estratégias em vários cenários/sementes e gera relatório."),
    "export-demo": ("smartenergy.experiments.export_demo", "Gera dashboard/data/demo.json (dados SIMULADOS)."),
    "gen-vectors": ("smartenergy.experiments.vectors", "Gera vetores golden de equivalência firmware<->Python."),
    "serve": ("smartenergy.api.cli", "Servidor HTTP (API + dashboard)."),
    "ingest": ("smartenergy.ingest.cli", "Assina o MQTT e armazena telemetria/decisões/eventos."),
    "twin": ("smartenergy.twin.cli", "Dispositivo virtual: publica telemetria SIMULADA via MQTT."),
    "sign-cmd": ("smartenergy.security.cli", "Assina e (opcionalmente) publica um comando sem.cmd.v1."),
    "live-ems": ("smartenergy.live.cli", "EMS preditivo ao vivo (MPC): publica set_loads assinados via MQTT."),
}


def _usage() -> str:
    lines = ["uso: python -m smartenergy <subcomando> [opções]", "", "subcomandos:"]
    for name, (_, desc) in SUBCOMMANDS.items():
        lines.append(f"  {name:<12} {desc}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(_usage())
        return 0
    name, rest = argv[0], argv[1:]
    if name not in SUBCOMMANDS:
        print(f"subcomando desconhecido: {name}\n\n{_usage()}", file=sys.stderr)
        return 2
    module = importlib.import_module(SUBCOMMANDS[name][0])
    return int(module.main(rest) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
