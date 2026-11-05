"""Testes da geometria solar, céu claro, decomposição/transposição e gerador de clima."""

from __future__ import annotations

import numpy as np
import pytest

from smartenergy.config import load_config
from smartenergy.models import solar
from smartenergy.models.solar import SolarSite
from smartenergy.models.weather import (
    KC_PARAMS, WEATHER_COLUMNS, ambient_temperature, generate_weather, markov_day_regimes,
)
from smartenergy.timeutil import local_midnight_epoch_ms


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def _day_grid(cfg, date: str, step_s: int = 60) -> np.ndarray:
    t0 = local_midnight_epoch_ms(date, cfg)
    return t0 + np.arange(86400 // step_s, dtype=np.int64) * step_s * 1000


def test_december_solstice_sun_near_zenith_at_solar_noon(cfg):
    """Cornélio Procópio (−23,18°) ≈ Trópico de Capricórnio: Sol quase a pino no solstício de dezembro."""
    site = SolarSite.from_config(cfg)
    t = _day_grid(cfg, "2026-12-21")
    pos = site.position(t)
    i = int(np.argmin(pos.zenith_deg))
    assert pos.zenith_deg[i] < 1.0
    # meio-dia solar ≈ 12:21 local (longitude −50,65° no fuso de −45°, EoT ≈ +2 min)
    assert 12 * 60 + 10 <= i <= 12 * 60 + 35
    assert abs(pos.declination_deg[i] - (-23.44)) < 0.1


def test_june_solstice_sun_to_the_north(cfg):
    site = SolarSite.from_config(cfg)
    pos = site.position(_day_grid(cfg, "2026-06-21"))
    i = int(np.argmin(pos.zenith_deg))
    assert pos.zenith_deg[i] == pytest.approx(23.18 + 23.44, abs=0.5)
    az = pos.azimuth_deg[i]
    assert min(az, 360.0 - az) < 3.0  # voltado ao Norte (azimute 0)
    # manhã a Leste, tarde a Oeste
    assert 30 < pos.azimuth_deg[i - 180] < 90 + 30
    assert 270 - 30 < pos.azimuth_deg[i + 180] < 330


def test_haurwitz_and_extraterrestrial():
    assert solar.clearsky_haurwitz(np.array([1.0]))[0] == pytest.approx(1098.0 * np.exp(-0.057))
    assert solar.clearsky_haurwitz(np.array([0.0, -0.3]))[0] == 0.0
    g_jan = solar.extraterrestrial_normal(np.array([1767225600000]))[0]  # 2026-01-01 UTC
    g_jul = solar.extraterrestrial_normal(np.array([1782864000000]))[0]  # 2026-07-01 UTC
    assert g_jan > 1400 and g_jul < 1330


def test_erbs_piecewise():
    kd = solar.erbs_diffuse_fraction(np.array([0.1, 0.5, 0.9]))
    assert kd[0] == pytest.approx(1 - 0.09 * 0.1)
    k = 0.5
    assert kd[1] == pytest.approx(0.9511 - 0.1604 * k + 4.388 * k**2 - 16.638 * k**3 + 12.336 * k**4)
    assert kd[2] == pytest.approx(0.165)


def test_horizontal_plane_equals_ghi_and_tilted_gains_in_winter(cfg):
    t = _day_grid(cfg, "2026-06-21", 600)
    flat = SolarSite(-23.18, -50.65, tilt_deg=0.0, azimuth_deg=0.0)
    cs = flat.clearsky(t)
    up = cs.position.cos_zenith > 0.05
    np.testing.assert_allclose(cs.poa_clear[up], cs.ghi_clear[up], rtol=1e-6)
    tilted = SolarSite(-23.18, -50.65, tilt_deg=23.0, azimuth_deg=0.0).clearsky(t)
    # no inverno o plano inclinado para o Norte recebe mais energia que o horizontal
    assert tilted.poa_clear.sum() > 1.1 * cs.ghi_clear.sum()


def test_weather_columns_and_night(cfg):
    w = generate_weather(cfg, "ensolarado", seed=1, days=1)
    for c in WEATHER_COLUMNS:
        assert c in w.columns
    assert len(w) == 1440
    assert w.attrs["source"] == "simulation"
    night = (w["tod_s"] < 4 * 3600) | (w["tod_s"] > 21 * 3600)
    assert (w.loc[night, "p_pv_avail"] == 0).all()
    assert w["p_pv_avail"].max() < cfg.raw["pv"]["p_stc_w"]
    assert w["p_pv_avail"].max() > 0.6 * cfg.raw["pv"]["p_stc_w"]


def test_weather_deterministic_and_prefix_stable(cfg):
    a = generate_weather(cfg, "semana_variavel", seed=3, days=3)
    b = generate_weather(cfg, "semana_variavel", seed=3, days=3)
    c = generate_weather(cfg, "semana_variavel", seed=3, days=7)
    d = generate_weather(cfg, "semana_variavel", seed=4, days=3)
    np.testing.assert_array_equal(a["kc"].to_numpy(), b["kc"].to_numpy())
    np.testing.assert_array_equal(a["p_pv_avail"].to_numpy(), c["p_pv_avail"].to_numpy()[: len(a)])
    assert not np.array_equal(a["kc"].to_numpy(), d["kc"].to_numpy())


def test_regime_statistics(cfg):
    means = {}
    for regime in ("clear", "partly", "overcast"):
        w = generate_weather(cfg, regime, seed=2, days=5)
        day = w["ghi_clear"] > 100
        means[regime] = float(w.loc[day, "kc"].mean())
        stds = float(w.loc[day, "kc"].std())
        if regime == "clear":
            assert means[regime] == pytest.approx(0.95, abs=0.02) and stds < 0.05
        if regime == "overcast":
            assert means[regime] == pytest.approx(KC_PARAMS["overcast"].mean, abs=0.07)
    assert means["clear"] > means["partly"] > means["overcast"]
    # nuvens intermitentes: o regime parcial tem variabilidade de curto prazo bem maior
    wp = generate_weather(cfg, "partly", seed=2, days=2)
    wc = generate_weather(cfg, "clear", seed=2, days=2)
    assert np.abs(np.diff(wp["kc"])).mean() > 3 * np.abs(np.diff(wc["kc"])).mean()


def test_markov_regimes(cfg):
    seq = markov_day_regimes(5, 20)
    assert set(seq) <= {"clear", "partly", "overcast"}
    assert markov_day_regimes(5, 7) == seq[:7]
    w = generate_weather(cfg, "semana_variavel", seed=5)
    assert w.attrs["days"] == 7 and w.attrs["day_regimes"] == seq[:7]


def test_ambient_temperature_extremes():
    tod = np.arange(0, 86400, 60.0)
    t = ambient_temperature(tod, 19.0, 31.0, 6.0, 15.0)
    assert t.min() == pytest.approx(19.0, abs=1e-6)
    assert t.max() == pytest.approx(31.0, abs=1e-6)
    assert tod[np.argmin(t)] == 6 * 3600 and tod[np.argmax(t)] == 15 * 3600
