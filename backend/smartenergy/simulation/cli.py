"""Subcomando ``python -m smartenergy simulate`` — executa uma simulação e salva os resultados.

Exemplo::

    python -m smartenergy simulate --scenario ensolarado --strategy rules --seed 1 \\
        [--days N] [--forecast perfect] [--initial-soc 0.6] [--set battery.capacity_ah=12] \\
        [--out results/sim_ensolarado_rules_s1]

Saídas (diretório ``--out``; padrão ``results/sim_<cenário>_<estratégia>_s<semente>``):

* ``series.csv`` — linha de comentário ``# source=simulation ...`` e cabeçalho ``coluna[unidade]``;
* ``metrics.json`` — indicadores do docs/06 §5 + ``meta`` (com o aviso DADOS SIMULADOS);
* ``decisions.json`` / ``events.json`` — ``sem.decision.v1`` / ``sem.event.v1``;
* ``forecasts.csv`` — previsões emitidas (se houver previsão);
* ``balance.png`` — balanço energético e SOC (matplotlib, backend Agg).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from smartenergy.config import SystemConfig, load_config
from smartenergy.simulation.engine import SIM_WARNING, SimResult, run_simulation, series_units

TABLE_METRICS = (
    "e_pv_avail_wh", "e_pv_wh", "e_curtail_wh", "curtail_frac", "e_demand_wh", "e_served_wh",
    "e_unserved_wh", "e_unserved_high_wh", "e_unserved_medium_wh", "e_unserved_low_wh", "service_rate",
    "e_bat_chg_wh", "e_bat_dis_wh", "equiv_cycles", "soc_min", "delta_soc", "n_switch", "lvd_events",
    "time_in_lvd_h", "e_aux_wh", "balance_error_wh", "forecast_mae_w", "forecast_rmse_w", "forecast_mape",
)


def _parse_overrides(items: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for it in items or []:
        if "=" not in it:
            raise SystemExit(f"--set espera chave=valor, recebido {it!r}")
        k, v = it.split("=", 1)
        out[k.strip()] = yaml.safe_load(v)
    return out


def write_csv(df: pd.DataFrame, path: Path, cfg: SystemConfig, meta: dict[str, Any]) -> None:
    """CSV com comentário de origem e cabeçalho ``coluna[unidade]``."""
    units = series_units(cfg)
    header = [f"{c}[{units.get(c, '')}]" for c in df.columns]
    comment = (f"# source=simulation; {SIM_WARNING} scenario={meta.get('scenario')}; strategy={meta.get('strategy')}; "
               f"seed={meta.get('seed')}; dt_s={meta.get('dt_s')}; start_date={meta.get('start_date')}; "
               f"forecast={meta.get('forecast_method')}; version={meta.get('version')}")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(comment + "\n")
        df.to_csv(fh, index=False, header=header, float_format="%.6g", lineterminator="\n")


def plot_result(res: SimResult, path: Path) -> None:
    """Figura com balanço de potência (W) e SOC; título marcado como SIMULADO."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    df = res.df
    t = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert("America/Sao_Paulo").dt.tz_localize(None)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 6.5), sharex=True, gridspec_kw={"height_ratios": [3, 2]})
    ax1.plot(t, df["pv_p_avail"], color="#f2b134", lw=0.8, label="FV disponível")
    ax1.plot(t, df["pv_p"], color="#e07b00", lw=1.0, label="FV colhida (entrada MPPT)")
    ax1.plot(t, df["bus_p"] + df["aux_p"], color="#3465a4", lw=1.0, label="cargas + aux")
    ax1.plot(t, df["bat_p"], color="#4e9a06", lw=0.8, label="bateria (+ descarga)")
    if "unserved_p" in df:
        ax1.fill_between(t, 0, df["unserved_p"], color="#cc0000", alpha=0.3, lw=0, label="não atendida")
    ax1.axhline(0, color="k", lw=0.5)
    ax1.set_ylabel("Potência (W)")
    ax1.legend(loc="upper left", fontsize=8, ncol=3)
    ax1.grid(alpha=0.3)
    ax2.plot(t, df["soc_true"], color="k", lw=1.2, label="SOC verdadeiro")
    ax2.plot(t, df["soc"], color="#75507b", lw=0.9, ls="--", label="SOC estimado (EMS)")
    ax2.set_ylim(0, 1.02)
    ax2.set_ylabel("SOC (1)")
    ax2.legend(loc="lower left", fontsize=8)
    ax2.grid(alpha=0.3)
    m = res.meta
    fig.suptitle(f"DADOS SIMULADOS — cenário {m['scenario']}, estratégia {m['strategy']}, semente {m['seed']}",
                 fontsize=11)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def format_table(metrics: dict[str, Any]) -> str:
    lines = [f"{'indicador':<24} {'valor':>12}", "-" * 37]
    for k in TABLE_METRICS:
        if k in metrics and metrics[k] is not None:
            v = metrics[k]
            lines.append(f"{k:<24} {v:>12.4g}" if isinstance(v, float) else f"{k:<24} {v!s:>12}")
    return "\n".join(lines)


def save_result(res: SimResult, out: Path, cfg: SystemConfig, plot: bool = True) -> None:
    """Grava CSV, JSONs, previsões e figura em ``out``."""
    out.mkdir(parents=True, exist_ok=True)
    write_csv(res.df, out / "series.csv", cfg, res.meta)
    with open(out / "metrics.json", "w", encoding="utf-8") as fh:
        json.dump({"source": "simulation", "meta": res.meta, "metrics": res.metrics}, fh, ensure_ascii=False,
                  indent=2, default=float)
    with open(out / "decisions.json", "w", encoding="utf-8") as fh:
        json.dump({"source": "simulation", "items": res.decisions}, fh, ensure_ascii=False, default=float)
    with open(out / "events.json", "w", encoding="utf-8") as fh:
        json.dump({"source": "simulation", "items": res.events}, fh, ensure_ascii=False, default=float)
    if res.forecasts is not None:
        res.forecasts.to_csv(out / "forecasts.csv", index=False, float_format="%.6g")
    if plot:
        plot_result(res, out / "balance.png")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m smartenergy simulate",
                                description="Executa uma simulação (DADOS SIMULADOS).")
    p.add_argument("--scenario", default="ensolarado", help="cenário de simulation.scenarios ou regime")
    p.add_argument("--strategy", default="rules", help="conventional | rules | predictive")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--days", type=int, default=None)
    p.add_argument("--forecast", default=None, help="perfect | noisy | clearsky_persistence | open_meteo")
    p.add_argument("--initial-soc", type=float, default=None)
    p.add_argument("--start-date", default=None)
    p.add_argument("--set", action="append", dest="overrides", metavar="CHAVE=VALOR",
                   help="substitui parâmetro do system.yaml (repetível), ex.: battery.capacity_ah=12")
    p.add_argument("--config", default=None, help="caminho alternativo do system.yaml")
    p.add_argument("--out", default=None, help="diretório de saída")
    p.add_argument("--no-plot", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    overrides = _parse_overrides(args.overrides)
    res = run_simulation(cfg, args.scenario, args.strategy, args.seed, days=args.days,
                         forecast_method=args.forecast, initial_soc=args.initial_soc,
                         overrides=overrides or None, start_date=args.start_date)
    out = Path(args.out or f"results/sim_{args.scenario}_{args.strategy}_s{args.seed}")
    save_result(res, out, cfg.with_overrides(overrides or None), plot=not args.no_plot)
    m = res.meta
    print(f"[{SIM_WARNING}] cenário={m['scenario']} estratégia={m['strategy']} semente={m['seed']} "
          f"dias={m['days']} previsão={m['forecast_method']} ({m['wall_time_s']} s)")
    print(format_table(res.metrics))
    print(f"resultados em {out}/")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
