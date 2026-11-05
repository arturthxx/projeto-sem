"""Gerador de clima sintético (irradiância e temperatura) — DADOS SIMULADOS.

Cadeia por passo ``dt_s``:

1. posição solar (NOAA) e GHI de céu claro (Haurwitz) — :mod:`smartenergy.models.solar`;
2. índice de céu claro ``kc`` estocástico por regime (abaixo);
3. ``GHI = kc · GHI_clear`` → Erbs → transposição isotrópica → ``POA``;
4. temperatura ambiente senoidal por partes entre ``t_amb_min_c`` (em
   ``t_min_hour``) e ``t_amb_max_c`` (em ``t_max_hour``), hora local;
5. temperatura de célula (NOCT) e potência FV disponível na entrada do MPPT.

Regimes de ``kc`` (AR(1) com coeficiente definido por minuto e convertido para
``dt_s``: ``φ_dt = φ_1min^(dt/60)``):

* ``clear``    — média 0,95, pouca variação;
* ``partly``   — fundo AR(1) ≈ 0,80 + eventos de sombreamento por nuvens
  (cadeia de dois estados livre/sombreado; atenuação sorteada por evento);
* ``overcast`` — média 0,25, variação lenta;
* ``markov``   — regime **diário** sorteado por cadeia de Markov entre os três acima.

Os parâmetros estatísticos (médias, desvios, durações de nuvem, matriz de
transição) são **sintéticos** — escolhidos para gerar cenários contrastantes,
NÃO calibrados com dados de Cornélio Procópio (PENDENTE: calibrar com séries
do INMET/estação local).

Determinismo: toda a aleatoriedade vem de ``numpy.random.Generator`` semeados
por ``(seed, regime, dia)``; o mesmo par (cenário, semente) gera o mesmo clima
para todas as estratégias, e gerar N+1 dias reproduz exatamente os N primeiros.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from math import ceil

import numpy as np
import pandas as pd

from smartenergy.models.pv import PvModel
from smartenergy.models.solar import SolarSite, extraterrestrial_normal
from smartenergy.timeutil import DAY_S, local_midnight_epoch_ms

REGIMES = ("clear", "partly", "overcast")
"""Regimes diários elementares (``markov`` sorteia entre eles)."""

WEATHER_COLUMNS = (
    "t", "tod_s", "ghi", "ghi_clear", "poa", "poa_clear", "kc", "t_amb", "t_cell", "p_pv_avail",
)


@dataclass(frozen=True)
class KcRegimeParams:
    """Parâmetros do índice de céu claro de um regime (sintéticos)."""

    mean: float
    std: float
    phi_1min: float
    lo: float
    hi: float
    # eventos de sombreamento (somente 'partly')
    p_enter_1min: float = 0.0     # prob./min de entrar em sombra
    p_exit_1min: float = 1.0      # prob./min de sair da sombra
    att_lo: float = 1.0           # atenuação multiplicativa sorteada por evento
    att_hi: float = 1.0


KC_PARAMS: dict[str, KcRegimeParams] = {
    "clear": KcRegimeParams(mean=0.95, std=0.015, phi_1min=0.99, lo=0.85, hi=1.02),
    "partly": KcRegimeParams(
        mean=0.80, std=0.06, phi_1min=0.98, lo=0.45, hi=1.05,
        p_enter_1min=1.0 / 12.0, p_exit_1min=1.0 / 6.0, att_lo=0.25, att_hi=0.60,
    ),
    "overcast": KcRegimeParams(mean=0.25, std=0.06, phi_1min=0.995, lo=0.08, hi=0.50),
}

MARKOV_TRANSITION = np.array(
    [
        [0.60, 0.30, 0.10],  # de clear
        [0.30, 0.45, 0.25],  # de partly
        [0.20, 0.40, 0.40],  # de overcast
    ]
)
"""Matriz de transição diária (linhas: regime de hoje; colunas: amanhã) — sintética."""


def stable_stream_id(name: str) -> int:
    """Identificador estável (entre execuções) de um fluxo aleatório nomeado."""
    return zlib.crc32(name.encode("utf-8"))


def make_rng(seed: int, stream: str, *extra: int) -> np.random.Generator:
    """Gerador determinístico para ``(seed, stream, *extra)`` — fluxos independentes."""
    return np.random.default_rng([int(seed) & 0xFFFFFFFF, stable_stream_id(stream), *[int(x) & 0xFFFFFFFF for x in extra]])


def resolve_regime(cfg, scenario: str) -> str:
    """Nome de cenário (``simulation.scenarios``) ou de regime → regime."""
    scenarios = cfg.raw["simulation"]["scenarios"]
    if scenario in scenarios:
        return str(scenarios[scenario]["regime"])
    if scenario in (*REGIMES, "markov"):
        return scenario
    raise ValueError(f"cenário desconhecido: {scenario!r}; opções: {list(scenarios)} ou regimes {[*REGIMES, 'markov']}")


def scenario_days(cfg, scenario: str) -> int:
    """Número de dias padrão do cenário (1 se for um regime sem cenário)."""
    scenarios = cfg.raw["simulation"]["scenarios"]
    if scenario in scenarios:
        return int(scenarios[scenario]["days"])
    return 1


def markov_day_regimes(seed: int, days: int) -> list[str]:
    """Sequência de regimes diários pela cadeia de Markov (primeiro dia: distribuição estacionária)."""
    rng = make_rng(seed, "weather/markov-chain")
    evals, evecs = np.linalg.eig(MARKOV_TRANSITION.T)
    stat = np.real(evecs[:, np.argmin(np.abs(evals - 1.0))])
    stat = stat / stat.sum()
    seq: list[str] = []
    state = int(rng.choice(3, p=stat))
    for d in range(days):
        if d > 0:
            state = int(rng.choice(3, p=MARKOV_TRANSITION[state]))
        seq.append(REGIMES[state])
    return seq


def _ar1(rng: np.random.Generator, n: int, mean: float, std: float, phi: float) -> np.ndarray:
    """Processo AR(1) estacionário com média/desvio dados."""
    x = np.empty(n)
    eps = rng.standard_normal(n)
    s_innov = std * np.sqrt(max(1.0 - phi * phi, 0.0))
    x[0] = mean + std * eps[0]
    for k in range(1, n):
        x[k] = mean + phi * (x[k - 1] - mean) + s_innov * eps[k]
    return x


def kc_day(regime: str, rng: np.random.Generator, n: int, dt_s: float) -> np.ndarray:
    """Índice de céu claro de um dia (``n`` passos de ``dt_s``) para um regime elementar."""
    p = KC_PARAMS[regime]
    scale = dt_s / 60.0
    phi = p.phi_1min**scale
    kc = _ar1(rng, n, p.mean, p.std, phi)
    if p.p_enter_1min > 0.0:
        p_in = 1.0 - (1.0 - p.p_enter_1min) ** scale
        p_out = 1.0 - (1.0 - p.p_exit_1min) ** scale
        u = rng.random(n)
        att_draw = rng.uniform(p.att_lo, p.att_hi, n)
        shaded = False
        att = 1.0
        factor = np.ones(n)
        for k in range(n):
            if shaded:
                if u[k] < p_out:
                    shaded = False
            elif u[k] < p_in:
                shaded = True
                att = att_draw[k]
            factor[k] = att if shaded else 1.0
        kc = kc * factor
        return np.clip(kc, p.att_lo * p.lo, p.hi)
    return np.clip(kc, p.lo, p.hi)


def ambient_temperature(tod_s: np.ndarray, t_min: float, t_max: float, h_min: float, h_max: float) -> np.ndarray:
    """Temperatura senoidal por partes: mínimo em ``h_min`` e máximo em ``h_max`` (hora local)."""
    h = (np.asarray(tod_s, dtype=np.float64) / 3600.0) % 24.0
    amp = t_max - t_min
    rise = h_max - h_min
    fall = 24.0 - rise
    x_rise = (h - h_min) % 24.0  # horas desde o mínimo
    rising = x_rise <= rise
    t_rise = t_min + amp * (1.0 - np.cos(np.pi * x_rise / rise)) / 2.0
    x_fall = (h - h_max) % 24.0
    t_fall = t_max - amp * (1.0 - np.cos(np.pi * x_fall / fall)) / 2.0
    return np.where(rising, t_rise, t_fall)


def ambient_temperature_cfg(cfg, tod_s: np.ndarray) -> np.ndarray:
    w = cfg.raw["simulation"]["weather"]
    return ambient_temperature(
        tod_s, float(w["t_amb_min_c"]), float(w["t_amb_max_c"]), float(w["t_min_hour"]), float(w["t_max_hour"])
    )


def generate_weather(
    cfg,
    scenario: str,
    seed: int = 1,
    start_date: str | None = None,
    days: int | None = None,
    dt_s: float | None = None,
) -> pd.DataFrame:
    """Gera a série de clima sintético (DADOS SIMULADOS).

    Args:
        cfg: ``SystemConfig``.
        scenario: nome de cenário de ``simulation.scenarios`` ou regime
            (``clear``/``partly``/``overcast``/``markov``).
        seed: semente (controla toda a aleatoriedade).
        start_date: dia inicial local ``AAAA-MM-DD`` (padrão ``simulation.start_date``).
        days: número de dias (padrão: o do cenário).
        dt_s: passo em segundos (padrão ``simulation.dt_s``); deve dividir 86400.

    Returns:
        DataFrame com colunas ``t`` (ms UTC), ``tod_s``, ``ghi``, ``ghi_clear``,
        ``poa``, ``poa_clear`` (W/m²), ``kc``, ``t_amb``, ``t_cell`` (°C),
        ``p_pv_avail`` (W na entrada do MPPT) e ``regime`` (regime do dia).
        ``df.attrs`` registra cenário, regime, semente e ``source="simulation"``.
    """
    sim = cfg.raw["simulation"]
    regime = resolve_regime(cfg, scenario)
    start_date = start_date or str(sim["start_date"])
    days = int(days if days is not None else scenario_days(cfg, scenario))
    dt = float(dt_s if dt_s is not None else sim["dt_s"])
    if days <= 0:
        raise ValueError("days deve ser > 0")
    steps_per_day = DAY_S / dt
    if abs(steps_per_day - round(steps_per_day)) > 1e-9:
        raise ValueError(f"dt_s={dt} deve dividir 86400")
    spd = int(round(steps_per_day))
    n = spd * days

    t0 = local_midnight_epoch_ms(start_date, cfg)
    k = np.arange(n, dtype=np.int64)
    t = t0 + (k * int(round(dt * 1000))).astype(np.int64)
    tod = (k % spd) * dt

    site = SolarSite.from_config(cfg)
    cs = site.clearsky(t)

    if regime == "markov":
        day_regimes = markov_day_regimes(seed, days)
    else:
        day_regimes = [regime] * days
    kc = np.empty(n)
    for d, reg in enumerate(day_regimes):
        rng = make_rng(seed, f"weather/kc/{reg}", d)
        kc[d * spd:(d + 1) * spd] = kc_day(reg, rng, spd, dt)

    ghi = kc * cs.ghi_clear
    g0n = extraterrestrial_normal(t)
    poa = site.poa_from_ghi(ghi, cs.position, g0n)

    t_amb = ambient_temperature_cfg(cfg, tod)
    pv = PvModel.from_config(cfg)
    t_cell = pv.cell_temperature(poa, t_amb)
    p_avail = pv.power(poa, t_cell)

    df = pd.DataFrame(
        {
            "t": t,
            "tod_s": tod,
            "ghi": ghi,
            "ghi_clear": cs.ghi_clear,
            "poa": poa,
            "poa_clear": cs.poa_clear,
            "kc": kc,
            "t_amb": t_amb,
            "t_cell": t_cell,
            "p_pv_avail": p_avail,
            "regime": np.repeat(np.array(day_regimes, dtype=object), spd),
        }
    )
    df.attrs.update(
        {"source": "simulation", "scenario": scenario, "regime": regime, "seed": int(seed),
         "start_date": start_date, "days": days, "dt_s": dt, "day_regimes": day_regimes}
    )
    return df


def extra_days_for_horizon(horizon_s: float) -> int:
    """Dias extras de clima necessários para cobrir um horizonte de previsão."""
    return int(ceil(max(horizon_s, 0.0) / DAY_S))
