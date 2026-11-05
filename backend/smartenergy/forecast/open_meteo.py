"""Previsão FV a partir da API pública Open-Meteo (irradiância no plano inclinado).

**NÃO TESTADO contra o serviço real** neste ambiente (sem acesso à internet);
apenas a conversão de azimute e a interpretação de uma resposta de exemplo são
cobertas por testes. Usar no backend ao vivo com conectividade.

Requisição: ``GET https://api.open-meteo.com/v1/forecast`` com
``hourly=global_tilted_irradiance,temperature_2m``, ``tilt`` e ``azimuth`` do
módulo, ``timezone=UTC`` e ``timeformat=unixtime``. Convenção de azimute da
Open-Meteo: 0° = Sul, −90° = Leste, +90° = Oeste, ±180° = Norte; o
``pv.azimuth_deg`` do projeto usa 0° = Norte, sentido horário (ver
:func:`to_open_meteo_azimuth`).

As variáveis de radiação horárias da Open-Meteo são médias da **hora anterior**
ao rótulo; um instante ``t`` usa, portanto, o valor rotulado com o fim da sua
hora. Potência: ``PvModel.power(GTI, Tc(GTI, T2m))`` (entrada do MPPT).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import numpy as np

from smartenergy.config import SystemConfig
from smartenergy.forecast.base import BaseForecast, ForecastUnavailableError
from smartenergy.models.pv import PvModel

API_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_REFRESH_S = 3600.0
HOUR_MS = 3_600_000


def to_open_meteo_azimuth(azimuth_north_cw_deg: float) -> float:
    """Converte azimute (0 = Norte, horário) para a convenção Open-Meteo (0 = Sul, Oeste +), em [−180, 180)."""
    a = (float(azimuth_north_cw_deg) - 180.0) % 360.0
    return a - 360.0 if a >= 180.0 else a


def build_request_url(cfg: SystemConfig, forecast_days: int = 3, past_days: int = 1) -> str:
    """URL da requisição para o local e a orientação do módulo do ``system.yaml``."""
    site, pv = cfg.raw["site"], cfg.raw["pv"]
    params = {
        "latitude": f"{float(site['latitude_deg']):.4f}",
        "longitude": f"{float(site['longitude_deg']):.4f}",
        "hourly": "global_tilted_irradiance,temperature_2m",
        "tilt": f"{float(pv['tilt_deg']):.1f}",
        "azimuth": f"{to_open_meteo_azimuth(float(pv['azimuth_deg'])):.1f}",
        "timezone": "UTC",
        "timeformat": "unixtime",
        "forecast_days": str(int(forecast_days)),
        "past_days": str(int(past_days)),
    }
    return f"{API_URL}?{urllib.parse.urlencode(params)}"


def parse_response(payload: dict[str, Any]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Extrai ``(t_ms, gti_w_m2, t2m_c)`` de uma resposta JSON da Open-Meteo.

    Raises:
        ForecastUnavailableError: resposta sem as séries esperadas.
    """
    try:
        hourly = payload["hourly"]
        t = np.asarray(hourly["time"], dtype=np.int64) * 1000
        gti = np.asarray([np.nan if x is None else x for x in hourly["global_tilted_irradiance"]], dtype=np.float64)
        t2m = np.asarray([np.nan if x is None else x for x in hourly["temperature_2m"]], dtype=np.float64)
    except (KeyError, TypeError, ValueError) as exc:
        raise ForecastUnavailableError(f"resposta da Open-Meteo sem séries horárias esperadas: {exc}") from exc
    if t.size == 0 or not (t.size == gti.size == t2m.size):
        raise ForecastUnavailableError("resposta da Open-Meteo vazia ou com séries de tamanhos diferentes")
    return t, np.nan_to_num(gti, nan=0.0).clip(min=0.0), np.nan_to_num(t2m, nan=25.0)


class OpenMeteoForecast(BaseForecast):
    """Previsão FV pela API Open-Meteo (requer internet; resposta em cache por ``refresh_s``)."""

    method = "open_meteo"

    def __init__(self, cfg: SystemConfig, timeout_s: float = DEFAULT_TIMEOUT_S,
                 refresh_s: float = DEFAULT_REFRESH_S) -> None:
        super().__init__(cfg)
        self.timeout_s = float(timeout_s)
        self.refresh_s = float(refresh_s)
        self.pv = PvModel.from_config(cfg)
        self._cache: tuple[np.ndarray, np.ndarray] | None = None  # (t_ms rótulo, potência W)
        self._fetched_at = 0.0

    def fetch(self) -> dict[str, Any]:
        """Baixa a previsão (HTTP); erros viram :class:`ForecastUnavailableError`."""
        url = build_request_url(self.cfg)
        req = urllib.request.Request(url, headers={"User-Agent": "smartenergy/0.1 (IC UTFPR-CP)"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310 - URL fixa https
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise ForecastUnavailableError(f"falha ao consultar Open-Meteo ({API_URL}): {exc}") from exc

    def load_payload(self, payload: dict[str, Any]) -> None:
        """Converte uma resposta em potência FV horária e guarda em cache."""
        t, gti, t2m = parse_response(payload)
        p = self.pv.power(gti, self.pv.cell_temperature(gti, t2m))
        self._cache = (t, np.asarray(p, dtype=np.float64))
        self._fetched_at = time.time()

    def _ensure(self) -> tuple[np.ndarray, np.ndarray]:
        if self._cache is None or time.time() - self._fetched_at > self.refresh_s:
            self.load_payload(self.fetch())
        assert self._cache is not None
        return self._cache

    def pv_forecast(self, t0_ms: int, step_s: int, n: int) -> list[float]:
        t_lbl, p = self._ensure()
        sub = max(1, int(np.ceil(float(step_s) / 300.0)))
        offs = (np.arange(sub) + 0.5) * float(step_s) * 1000.0 / sub
        ts = (float(t0_ms) + np.arange(n)[:, None] * float(step_s) * 1000.0 + offs[None, :]).ravel()
        # valor horário rotulado no FIM da hora (média da hora anterior)
        label = np.ceil(ts / HOUR_MS) * HOUR_MS
        idx = np.minimum(np.searchsorted(t_lbl, label), t_lbl.size - 1)
        if not np.all(t_lbl[idx] == label):
            raise ForecastUnavailableError("horizonte solicitado fora da cobertura da previsão Open-Meteo")
        return [float(x) for x in p[idx].reshape(n, sub).mean(axis=1)]

    def describe(self) -> dict[str, Any]:
        return {"method": self.method, "url": API_URL, "tested": False}
