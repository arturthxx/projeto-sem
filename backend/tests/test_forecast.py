"""Testes dos provedores de previsão FV e de demanda."""

from __future__ import annotations

import numpy as np
import pytest

from smartenergy.config import load_config
from smartenergy.ems.base import ForecastProvider
from smartenergy.forecast.base import WeatherSeries
from smartenergy.forecast.clearsky_persistence import ClearSkyPersistenceForecast
from smartenergy.forecast.factory import make_forecast
from smartenergy.forecast.open_meteo import OpenMeteoForecast, build_request_url, to_open_meteo_azimuth
from smartenergy.models.weather import generate_weather
from smartenergy.timeutil import local_midnight_epoch_ms


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def weather(cfg):
    return generate_weather(cfg, "partly", seed=7, days=4)


def test_factory(cfg, weather):
    for m in ("perfect", "noisy", "clearsky_persistence", "open_meteo"):
        fc = make_forecast(m, cfg, weather=weather, seed=1)
        assert fc.method == m and isinstance(fc, ForecastProvider)
    assert make_forecast(None, cfg).method == cfg.raw["forecast"]["pv_method"]
    with pytest.raises(ValueError):
        make_forecast("perfect", cfg)  # sem clima simulado
    with pytest.raises(ValueError):
        make_forecast("bola_de_cristal", cfg, weather=weather)


def test_weather_series_interval_means_and_wrap():
    t = np.arange(0, 2 * 86_400_000, 60_000, dtype=np.int64)  # 2 dias, 1 min
    v = (t // 60_000 % 1440).astype(float)                    # minuto do dia
    s = WeatherSeries(t, v)
    assert s.interval_means(0, 900, 2).tolist() == [7.0, 22.0]          # média de 0..14 e 15..29
    # além do fim: repete o último dia
    late = s.interval_means(2 * 86_400_000 + 600_000, 60, 1)[0]
    assert late == 10.0


def test_perfect_equals_truth(cfg, weather):
    fc = make_forecast("perfect", cfg, weather=weather)
    t0 = int(weather["t"].iloc[0]) + 10 * 3600 * 1000
    out = fc.pv_forecast(t0, 900, 8)
    p = weather["p_pv_avail"].to_numpy()
    i0 = 10 * 60
    expected = [p[i0 + 15 * k: i0 + 15 * (k + 1)].mean() for k in range(8)]
    np.testing.assert_allclose(out, expected, rtol=1e-12)


def test_noisy_deterministic_and_statistics(cfg):
    w = generate_weather(cfg, "clear", seed=1, days=30)
    a = make_forecast("noisy", cfg, weather=w, seed=3)
    b = make_forecast("noisy", cfg, weather=w, seed=3)
    c = make_forecast("noisy", cfg, weather=w, seed=4)
    t0 = int(w["t"].iloc[0]) + 8 * 3600 * 1000
    assert a.pv_forecast(t0, 900, 40) == b.pv_forecast(t0, 900, 40)
    assert a.pv_forecast(t0, 900, 40) != c.pv_forecast(t0, 900, 40)
    # erro relativo ~ rel_rmse (0,20) e autocorrelação ~ phi (0,9) por passo de 15 min
    e = a.error.v
    assert 0.15 < e.std() < 0.25
    assert 0.8 < np.corrcoef(e[:-1], e[1:])[0, 1] < 0.97
    # erro definido sobre o tempo absoluto: emissões diferentes concordam no mesmo alvo
    f1 = a.pv_forecast(t0, 900, 8)[4]
    f2 = a.pv_forecast(t0 + 4 * 900_000, 900, 2)[0]
    assert f1 == pytest.approx(f2)
    assert min(a.pv_forecast(t0, 900, 96)) >= 0.0


def test_clearsky_persistence_blend(cfg):
    fc = ClearSkyPersistenceForecast(cfg)
    t_noon = local_midnight_epoch_ms("2026-11-16", cfg) + 12 * 3600 * 1000
    # sem observações → climatologia
    cs = fc.clear.interval_means(t_noon, 900, 96)
    np.testing.assert_allclose(fc.pv_forecast(t_noon, 900, 96), 0.7 * cs, rtol=1e-9)
    # observa 1 h de céu a 50 % do claro
    for k in range(60, 0, -1):
        ts = t_noon - k * 60_000
        fc.observe(ts, 0.5 * float(fc.clear.power(np.array([ts]))[0]))
    assert fc.observed_index(t_noon) == pytest.approx(0.5, abs=1e-9)
    out = np.asarray(fc.pv_forecast(t_noon, 900, 96))
    k0 = 0.7 + (0.5 - 0.7) * np.exp(-(0.125) / 3.0)
    assert out[0] == pytest.approx(cs[0] * k0, rel=1e-9)
    assert out[20] / cs[20] == pytest.approx(0.7 + (0.5 - 0.7) * np.exp(-(20.5 * 0.25) / 3.0), rel=1e-9)
    # à noite o céu claro é nulo → previsão nula
    night = int(np.argmin(cs[40:])) + 40
    assert cs[night] == 0.0 and out[night] == 0.0
    # potência disponível informada tem precedência sobre a medida (corte por bateria cheia)
    fc2 = ClearSkyPersistenceForecast(cfg)
    for k in range(60, 0, -1):
        ts = t_noon - k * 60_000
        p_cs = float(fc2.clear.power(np.array([ts]))[0])
        fc2.observe(ts, 0.2 * p_cs, p_pv_avail_w=0.9 * p_cs)
    assert fc2.observed_index(t_noon) == pytest.approx(0.9, abs=1e-9)


def test_clearsky_persistence_night_uses_climatology(cfg):
    fc = ClearSkyPersistenceForecast(cfg)
    t_night = local_midnight_epoch_ms("2026-11-16", cfg) + 2 * 3600 * 1000
    for k in range(60):
        fc.observe(t_night - k * 60_000, 0.0)
    assert fc.observed_index(t_night) is None


def test_demand_forecast_fractions(cfg, weather):
    fc = make_forecast("perfect", cfg, weather=weather)
    t0 = local_midnight_epoch_ms("2026-11-16", cfg) + 5 * 3600 * 1000  # 05:00 local
    d = fc.demand_forecast(t0, 3600, 3)
    assert d["led"] == [0.5, 1.0, 0.0]  # led 05:30–07:00
    assert "res" not in d               # deslocável não entra no perfil
    d15 = fc.demand_forecast(t0 + 7 * 3600 * 1000, 900, 4)  # 12:00
    assert d15["fan"] == [1.0, 1.0, 1.0, 1.0]


def test_open_meteo_conventions_and_parsing(cfg):
    assert to_open_meteo_azimuth(0.0) == -180.0
    assert to_open_meteo_azimuth(90.0) == -90.0
    assert to_open_meteo_azimuth(180.0) == 0.0
    assert to_open_meteo_azimuth(270.0) == 90.0
    url = build_request_url(cfg)
    assert "global_tilted_irradiance" in url and "tilt=23.0" in url and "azimuth=-180.0" in url
    # resposta de exemplo (sem rede): rótulo no FIM da hora
    t0 = local_midnight_epoch_ms("2026-11-16", cfg) + 12 * 3600 * 1000
    hours = [(t0 // 1000) + 3600 * k for k in range(0, 5)]
    payload = {"hourly": {"time": hours, "global_tilted_irradiance": [0, 1000, 500, None, 0],
                          "temperature_2m": [25, 25, 25, 25, 25]}}
    fc = OpenMeteoForecast(cfg)
    fc.load_payload(payload)
    out = fc.pv_forecast(t0, 3600, 2)  # [12h,13h) → rótulo 13h; [13h,14h) → rótulo 14h
    p1 = float(fc.pv.power(1000.0, fc.pv.cell_temperature(1000.0, 25.0)))
    p2 = float(fc.pv.power(500.0, fc.pv.cell_temperature(500.0, 25.0)))
    assert out == pytest.approx([p1, p2])
