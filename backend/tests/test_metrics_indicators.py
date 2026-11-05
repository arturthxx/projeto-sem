"""Testes dos indicadores (docs/06 §5) com séries construídas à mão (passo de 1 h)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from smartenergy.config import load_config
from smartenergy.metrics.indicators import compute_metrics, forecast_metrics
from smartenergy.timeutil import local_midnight_epoch_ms


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def _hand_series(cfg) -> pd.DataFrame:
    """Um dia local, dt = 3600 s (h = 1), barramento em 12,8 V (P_k(V) = p_nom)."""
    n = 24
    t0 = local_midnight_epoch_ms("2026-11-16", cfg)
    z = np.zeros(n)
    df = pd.DataFrame({"t": t0 + np.arange(n) * 3_600_000, "bus_v": np.full(n, 12.8)})
    # FV: 20 W disponíveis das 8h às 15h; 10 W colhidos às 12h e 13h
    avail = z.copy(); avail[8:16] = 20.0
    pv = avail.copy(); pv[12:14] = 10.0
    df["pv_p_avail"], df["pv_p"] = avail, pv
    # led (alta): pedido 18–22h (5 h), atendido 18–21h
    df["l1_d"] = z.copy(); df.loc[18:22, "l1_d"] = 1
    df["l1_p"] = z.copy(); df.loc[18:21, "l1_p"] = 5.0
    df["l1_on"] = (df["l1_p"] > 0).astype(int)
    # ventilador (média): pedido 12–16h; duty 0,6 às 12h e 13h
    df["l2_d"] = z.copy(); df.loc[12:16, "l2_d"] = 1
    df["l2_p"] = z.copy(); df.loc[12:13, "l2_p"] = 3.6; df.loc[14:16, "l2_p"] = 6.0
    df["l2_on"] = (df["l2_p"] > 0).astype(int)
    # resistiva deslocável (baixa): 10h e 11h (no prazo) + 22h (após o prazo de 22:00)
    df["l3_p"] = z.copy(); df.loc[[10, 11, 22], "l3_p"] = 16.4
    df["l3_on"] = (df["l3_p"] > 0).astype(int)
    df["l3_d"] = 0
    df["bus_p"] = df["l1_p"] + df["l2_p"] + df["l3_p"]
    df["aux_p"] = 0.8
    bat = z.copy(); bat[0:6] = 2.0; bat[9:11] = -10.0
    df["bat_p"] = bat
    soc = np.full(n, 0.6); soc[5] = 0.4; soc[-1] = 0.5
    df["soc_true"] = soc
    df["soc"] = soc + 0.01
    df["safety"] = "OK"; df.loc[[3, 4, 7], "safety"] = "LVD"
    return df


def test_compute_metrics_by_hand(cfg):
    m = compute_metrics(_hand_series(cfg), cfg, 3600.0)
    assert m["e_pv_avail_wh"] == pytest.approx(160.0)
    assert m["e_pv_wh"] == pytest.approx(140.0)
    assert m["e_curtail_wh"] == pytest.approx(20.0)
    assert m["curtail_frac"] == pytest.approx(0.125)
    assert m["e_demand_high_wh"] == pytest.approx(25.0)
    assert m["e_unserved_high_wh"] == pytest.approx(5.0)
    assert m["e_demand_medium_wh"] == pytest.approx(30.0)
    assert m["e_unserved_medium_wh"] == pytest.approx(4.8)
    assert m["e_demand_low_wh"] == pytest.approx(40.0)
    assert m["e_unserved_low_wh"] == pytest.approx(40.0 - 32.8)
    assert m["e_demand_wh"] == pytest.approx(95.0)
    assert m["e_unserved_wh"] == pytest.approx(17.0)
    assert m["service_rate"] == pytest.approx(1 - 17.0 / 95.0)
    assert m["service_rate_high"] == pytest.approx(0.8)
    assert m["service_rate_medium"] == pytest.approx(1 - 4.8 / 30.0)
    assert m["service_rate_low"] == pytest.approx(32.8 / 40.0)
    assert m["e_served_wh"] == pytest.approx(20.0 + 25.2 + 49.2)
    assert m["e_bat_chg_wh"] == pytest.approx(20.0)
    assert m["e_bat_dis_wh"] == pytest.approx(12.0)
    assert m["bat_throughput_wh"] == pytest.approx(32.0)
    assert m["equiv_cycles"] == pytest.approx(32.0 / (2 * 76.8))
    assert m["soc_min"] == pytest.approx(0.4)
    assert m["max_dod"] == pytest.approx(0.6)
    assert m["delta_soc"] == pytest.approx(-0.1)
    assert m["soc_est_mae"] == pytest.approx(0.01)
    assert m["n_switch_led"] == 2 and m["n_switch_fan"] == 2 and m["n_switch_res"] == 4
    assert m["n_switch"] == 8
    assert m["lvd_events"] == 2
    assert m["time_in_lvd_h"] == pytest.approx(3.0)
    assert m["e_aux_wh"] == pytest.approx(19.2)
    # Σ(η·pv − sc) + Σbat − Σbus − Σaux = (6·18,7 + 2·9,2) − 8 − 94,4 − 19,2
    assert m["balance_error_wh"] == pytest.approx(130.6 - 8.0 - 94.4 - 19.2)
    assert m["deferrable_days"] == 1


def test_voltage_dependence_and_attrs(cfg):
    df = _hand_series(cfg)
    df["bus_v"] = 14.08  # 1,1 × 12,8
    df.attrs["soc_true_end"] = 0.7
    m = compute_metrics(df, cfg, 3600.0)
    assert m["e_demand_high_wh"] == pytest.approx(25.0 * 1.1**1.5)
    assert m["delta_soc"] == pytest.approx(0.1)


def test_forecast_metrics_by_hand():
    out = forecast_metrics([0, 10, 20, 1, 0], [0, 12, 15, 2, 1], p_stc=50.0)
    assert out["n"] == 4
    assert out["mae_w"] == pytest.approx(2.25)
    assert out["rmse_w"] == pytest.approx(math.sqrt(7.75))
    assert out["bias_w"] == pytest.approx(-0.25)
    assert out["n_mape"] == 2
    assert out["mape"] == pytest.approx((0.2 + 0.25) / 2)
    empty = forecast_metrics([0, 0], [0, 0], p_stc=50.0)
    assert empty["mae_w"] is None and empty["mape"] is None
