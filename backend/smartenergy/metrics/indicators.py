"""Indicadores de desempenho (docs/06 §5) — simulação e experimentos.

Entrada: série temporal no vocabulário do docs/05 §8, passo uniforme ``dt_s``,
cada linha descrevendo o intervalo ``[t, t + dt)`` (potências médias no passo).
Energias por soma retangular ``Σ P·h`` com ``h = dt/3600`` (exata para o
simulador, cujas potências são constantes no passo).

Convenções adotadas onde o docs/06 deixa margem:

* Demanda de uma carga não deslocável: ``d_k·P_k(V)`` com ``P_k(V) =
  p_nom·(V/v_ref)^k`` (duty 1) na tensão do barramento ``bus_v``; redução de
  duty (ex.: ventilador em ECONOMY) conta como energia não atendida.
* Carga deslocável: requisito ``daily_energy_wh`` por **dia local** cujo prazo
  esteja coberto pela série; "entregue no prazo" = energia entregue no dia
  local antes de ``deadline``.
* ``n_switch``: transições on↔off entre linhas consecutivas (o estado anterior
  à primeira linha não é conhecido).
* ``lvd_events``: bordas de entrada em ``safety == "LVD"`` (a linha inicial
  conta se já estiver em LVD).
* ``soc_min``/``max_dod``/``delta_soc`` usam ``soc_true`` (simulação) ou, na
  falta dele (hardware), o SOC estimado ``soc``. ``delta_soc`` usa
  ``df.attrs["soc_true_end"]`` (SOC ao fim do último passo) quando presente.
* ``balance_error_wh = Σ (P_mppt_out + bat_p − bus_p − aux_p)·h`` com
  ``P_mppt_out = η·pv_p − autoconsumo`` (quando ``pv_p > 0``): fechamento do
  balanço no barramento CC (≈ 0 no simulador; no hardware reflete erros de medição
  e do modelo do MPPT).
* Razões com denominador nulo valem 0 (frações) ou 1 (taxas de atendimento).
"""

from __future__ import annotations

from typing import Any, Iterable

import numpy as np
import pandas as pd

from smartenergy.config import PRIORITIES, LoadConfig, SystemConfig
from smartenergy.models.mppt import mppt_output_from_input
from smartenergy.timeutil import DAY_S, parse_hhmm

MAPE_MIN_FRACTION_OF_STC = 0.05
"""MAPE só onde a potência real ≥ 5 % de ``p_stc_w`` (docs/06 §5)."""


def _col(df: pd.DataFrame, name: str, default: float = 0.0) -> np.ndarray:
    if name in df.columns:
        return np.nan_to_num(df[name].to_numpy(dtype=np.float64), nan=default)
    return np.full(len(df), default, dtype=np.float64)


def local_day_and_tod(t_ms: Iterable[int], utc_offset_h: float) -> tuple[np.ndarray, np.ndarray]:
    """(índice do dia local, segundos desde a meia-noite local) a partir de epoch ms UTC."""
    local_s = np.asarray(t_ms, dtype=np.float64) / 1000.0 + float(utc_offset_h) * 3600.0
    day = np.floor(local_s / DAY_S).astype(np.int64)
    return day, local_s - day * DAY_S


def full_power(load: LoadConfig, v: np.ndarray) -> np.ndarray:
    """``P_k(V) = p_nom·(V/v_ref)^k`` com duty 1 (vetorizado; 0 para V ≤ 0)."""
    vv = np.maximum(np.asarray(v, dtype=np.float64), 0.0)
    return load.p_nom_w * (vv / load.v_ref_v) ** load.voltage_exponent


def _deferrable_days(
    load: LoadConfig, p: np.ndarray, day: np.ndarray, tod: np.ndarray, h: float, dt_s: float
) -> tuple[float, float, int]:
    """(demanda, não atendida, nº de dias avaliados) da carga deslocável."""
    assert load.deferrable is not None
    req = float(load.deferrable.daily_energy_wh)
    deadline = parse_hhmm(load.deferrable.deadline)
    demand = unserved = 0.0
    n_days = 0
    for d in np.unique(day):
        sel = day == d
        tods = tod[sel]
        # o prazo precisa estar coberto pela série (último passo antes do prazo presente)
        if tods.min() > 0.5 * dt_s or tods.max() < deadline - dt_s - 1e-6:
            continue
        before = sel.copy()
        before[sel] = tods < deadline
        delivered = float(p[before].sum() * h)
        demand += req
        unserved += max(0.0, req - delivered)
        n_days += 1
    return demand, unserved, n_days


def compute_metrics(df: pd.DataFrame, cfg: SystemConfig, dt_s: float | None = None) -> dict[str, Any]:
    """Calcula todos os indicadores do docs/06 §5 (nomes exatamente como lá).

    Args:
        df: série no vocabulário do docs/05 §8 (passo uniforme).
        cfg: configuração (cargas, bateria, MPPT, fuso).
        dt_s: passo em segundos (padrão: ``df.attrs["dt_s"]`` ou diferença de ``t``).

    Returns:
        Dicionário plano: energias em Wh, frações em 0..1, contagens inteiras.
        Indicadores por classe usam os sufixos ``_high``/``_medium``/``_low``;
        por carga, ``n_switch_<key>``.
    """
    if dt_s is None:
        dt_s = float(df.attrs.get("dt_s") or (float(df["t"].iloc[1] - df["t"].iloc[0]) / 1000.0))
    dt_s = float(dt_s)
    h = dt_s / 3600.0
    n = len(df)
    m: dict[str, Any] = {"n_steps": int(n), "dt_s": dt_s, "duration_h": n * h}

    pv_p = _col(df, "pv_p")
    pv_avail = _col(df, "pv_p_avail") if "pv_p_avail" in df.columns else pv_p.copy()
    m["e_pv_wh"] = float(pv_p.sum() * h)
    m["e_pv_avail_wh"] = float(pv_avail.sum() * h)
    m["e_curtail_wh"] = float(np.maximum(0.0, pv_avail - pv_p).sum() * h)
    m["curtail_frac"] = m["e_curtail_wh"] / m["e_pv_avail_wh"] if m["e_pv_avail_wh"] > 0 else 0.0

    # --- demanda / atendimento por prioridade ---------------------------------
    bus_v = _col(df, "bus_v")
    day, tod = local_day_and_tod(df["t"].to_numpy(), float(cfg.raw["site"]["utc_offset_h"]))
    dem = {p: 0.0 for p in PRIORITIES}
    uns = {p: 0.0 for p in PRIORITIES}
    served = 0.0
    n_def_days = 0
    for ld in cfg.loads:
        p_k = np.maximum(_col(df, f"l{ld.id}_p"), 0.0)
        served += float(p_k.sum() * h)
        if ld.deferrable is not None:
            d_wh, u_wh, nd = _deferrable_days(ld, p_k, day, tod, h, dt_s)
            n_def_days = max(n_def_days, nd)
        else:
            d = np.clip(_col(df, f"l{ld.id}_d"), 0.0, 1.0)
            want = d * full_power(ld, bus_v)
            d_wh = float(want.sum() * h)
            u_wh = float(np.maximum(0.0, want - p_k).sum() * h)
        dem[ld.priority] += d_wh
        uns[ld.priority] += u_wh
    m["e_demand_wh"] = float(sum(dem.values()))
    m["e_served_wh"] = served
    m["e_unserved_wh"] = float(sum(uns.values()))
    for p in PRIORITIES:
        m[f"e_demand_{p}_wh"] = dem[p]
        m[f"e_unserved_{p}_wh"] = uns[p]
    m["service_rate"] = 1.0 - m["e_unserved_wh"] / m["e_demand_wh"] if m["e_demand_wh"] > 0 else 1.0
    for p in PRIORITIES:
        m[f"service_rate_{p}"] = 1.0 - uns[p] / dem[p] if dem[p] > 0 else 1.0
    m["deferrable_days"] = int(n_def_days)

    # --- bateria ---------------------------------------------------------------
    bat_p = _col(df, "bat_p")
    m["e_bat_chg_wh"] = float(np.maximum(0.0, -bat_p).sum() * h)
    m["e_bat_dis_wh"] = float(np.maximum(0.0, bat_p).sum() * h)
    m["bat_throughput_wh"] = m["e_bat_chg_wh"] + m["e_bat_dis_wh"]
    e_nom = cfg.battery_energy_wh
    m["equiv_cycles"] = m["bat_throughput_wh"] / (2.0 * e_nom) if e_nom > 0 else 0.0

    soc_col = "soc_true" if "soc_true" in df.columns else "soc"
    soc = df[soc_col].to_numpy(dtype=np.float64) if soc_col in df.columns and n else np.zeros(0)
    if soc.size:
        soc_end = float(df.attrs.get("soc_true_end", soc[-1])) if soc_col == "soc_true" else float(soc[-1])
        m["soc_initial"] = float(soc[0])
        m["soc_final"] = soc_end
        m["soc_min"] = float(min(np.nanmin(soc), soc_end))
        m["max_dod"] = 1.0 - m["soc_min"]
        m["delta_soc"] = soc_end - float(soc[0])
    else:
        m.update({"soc_initial": None, "soc_final": None, "soc_min": None, "max_dod": None, "delta_soc": None})
    m["soc_source"] = soc_col
    if "soc_true" in df.columns and "soc" in df.columns and n:
        err = df["soc"].to_numpy(dtype=np.float64) - df["soc_true"].to_numpy(dtype=np.float64)
        m["soc_est_mae"] = float(np.nanmean(np.abs(err)))
        m["soc_est_max_abs_err"] = float(np.nanmax(np.abs(err)))

    # --- comutações e LVD ------------------------------------------------------
    n_sw_total = 0
    for ld in cfg.loads:
        on = _col(df, f"l{ld.id}_on") > 0.5
        n_sw = int(np.count_nonzero(on[1:] != on[:-1])) if on.size > 1 else 0
        m[f"n_switch_{ld.key}"] = n_sw
        n_sw_total += n_sw
    m["n_switch"] = n_sw_total
    if "safety" in df.columns and n:
        lvd = (df["safety"].astype(str).to_numpy() == "LVD")
        prev = np.concatenate(([False], lvd[:-1]))
        m["lvd_events"] = int(np.count_nonzero(lvd & ~prev))
        m["time_in_lvd_h"] = float(np.count_nonzero(lvd) * h)
    else:
        m["lvd_events"] = 0
        m["time_in_lvd_h"] = 0.0

    # --- auxiliar e fechamento do balanço -------------------------------------
    aux_p = _col(df, "aux_p")
    m["e_aux_wh"] = float(aux_p.sum() * h)
    mp = cfg.raw["mppt"]
    p_out = mppt_output_from_input(pv_p, float(mp["efficiency"]), float(mp["self_consumption_w"]))
    bus_p = _col(df, "bus_p")
    m["e_load_wh"] = float(bus_p.sum() * h)
    m["balance_error_wh"] = float(((p_out + bat_p - bus_p - aux_p) * h).sum())
    return m


def forecast_metrics(
    actual: Iterable[float],
    predicted: Iterable[float],
    p_stc: float,
    daytime: Iterable[bool] | None = None,
) -> dict[str, Any]:
    """Erros de previsão FV (docs/06 §5).

    Args:
        actual, predicted: potências (W) alinhadas.
        p_stc: potência nominal do módulo (W) — limiar do MAPE.
        daytime: máscara de passos diurnos; padrão ``actual > 0 ou predicted > 0``.

    Returns:
        ``mae_w`` e ``rmse_w`` (passos diurnos), ``bias_w`` (previsto − real),
        ``mape`` (FRAÇÃO, ×100 para %) só onde ``actual ≥ 5 %·p_stc``, e as
        contagens ``n`` e ``n_mape``. Sem amostras → valores ``None``.
    """
    a = np.asarray(list(actual), dtype=np.float64)
    f = np.asarray(list(predicted), dtype=np.float64)
    if a.shape != f.shape:
        raise ValueError("actual e predicted devem ter o mesmo tamanho")
    mask = (a > 0.0) | (f > 0.0) if daytime is None else np.asarray(list(daytime), dtype=bool)
    mask &= np.isfinite(a) & np.isfinite(f)
    err = f[mask] - a[mask]
    out: dict[str, Any] = {"n": int(err.size)}
    if err.size:
        out["mae_w"] = float(np.mean(np.abs(err)))
        out["rmse_w"] = float(np.sqrt(np.mean(err**2)))
        out["bias_w"] = float(np.mean(err))
    else:
        out.update({"mae_w": None, "rmse_w": None, "bias_w": None})
    sel = mask & (a >= MAPE_MIN_FRACTION_OF_STC * float(p_stc))
    out["n_mape"] = int(np.count_nonzero(sel))
    out["mape"] = float(np.mean(np.abs(f[sel] - a[sel]) / a[sel])) if out["n_mape"] else None
    return out


METRICS_DEF: dict[str, dict[str, str]] = {
    "e_pv_wh": {"label": "Energia FV colhida", "unit": "Wh", "better": "higher"},
    "e_pv_avail_wh": {"label": "Energia FV disponível", "unit": "Wh", "better": "higher"},
    "e_curtail_wh": {"label": "Energia FV não aproveitada", "unit": "Wh", "better": "lower"},
    "curtail_frac": {"label": "Fração de corte FV", "unit": "1", "better": "lower"},
    "e_demand_wh": {"label": "Energia demandada", "unit": "Wh", "better": "higher"},
    "e_served_wh": {"label": "Energia entregue às cargas", "unit": "Wh", "better": "higher"},
    "e_unserved_wh": {"label": "Energia não atendida", "unit": "Wh", "better": "lower"},
    "e_unserved_high_wh": {"label": "Não atendida (alta prioridade)", "unit": "Wh", "better": "lower"},
    "e_unserved_medium_wh": {"label": "Não atendida (média prioridade)", "unit": "Wh", "better": "lower"},
    "e_unserved_low_wh": {"label": "Não atendida (baixa prioridade)", "unit": "Wh", "better": "lower"},
    "service_rate": {"label": "Taxa de atendimento", "unit": "1", "better": "higher"},
    "service_rate_high": {"label": "Atendimento (alta)", "unit": "1", "better": "higher"},
    "service_rate_medium": {"label": "Atendimento (média)", "unit": "1", "better": "higher"},
    "service_rate_low": {"label": "Atendimento (baixa)", "unit": "1", "better": "higher"},
    "e_bat_chg_wh": {"label": "Energia de carga da bateria", "unit": "Wh", "better": "lower"},
    "e_bat_dis_wh": {"label": "Energia de descarga da bateria", "unit": "Wh", "better": "lower"},
    "bat_throughput_wh": {"label": "Throughput da bateria", "unit": "Wh", "better": "lower"},
    "equiv_cycles": {"label": "Ciclos equivalentes", "unit": "1", "better": "lower"},
    "soc_min": {"label": "SOC mínimo", "unit": "1", "better": "higher"},
    "max_dod": {"label": "Profundidade máx. de descarga", "unit": "1", "better": "lower"},
    "delta_soc": {"label": "Variação de SOC (final − inicial)", "unit": "1", "better": "higher"},
    "n_switch": {"label": "Comutações", "unit": "1", "better": "lower"},
    "lvd_events": {"label": "Desconexões por subtensão", "unit": "1", "better": "lower"},
    "time_in_lvd_h": {"label": "Tempo em LVD", "unit": "h", "better": "lower"},
    "e_aux_wh": {"label": "Consumo auxiliar", "unit": "Wh", "better": "lower"},
    "balance_error_wh": {"label": "Erro de fechamento do balanço", "unit": "Wh", "better": "lower"},
}
"""Rótulos/unidades/sentido de melhoria (para ``demo.comparison.metrics_def``)."""
