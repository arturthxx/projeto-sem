"""Motor de simulação da microrrede CC — produz SEMPRE dados ``source="simulation"``.

Laço a passo fixo ``dt_s`` (padrão ``simulation.dt_s`` = 60 s). Cada linha ``k`` da
série descreve o intervalo ``[t_k, t_k + dt)``: potências são médias no passo;
estados (``soc``, ``soc_true``, ``soc_unc``, ``def_done_wh``) são os valores no
instante ``t_k`` em que o EMS decide; saídas/estados do EMS valem durante o passo.

Em cada passo:

1. **Medições simuladas** do passo anterior (bateria, FV, barramento) com os
   erros de ``simulation.sensor_error`` — ganho, *offset* e ruído gaussiano de
   corrente (fluxo aleatório próprio ``sensor/noise``, determinístico pela semente).
2. **Estimador de SOC** (``estimation.soc.SocEstimator``) alimentado só pelas
   medições (já integrado até ``t_k``); o EMS usa o SOC **estimado**.
3. **Supervisor de segurança** (``ems.safety.SafetySupervisor``, sem ``boot_hold``).
4. **Estratégia** (``ems.registry.get_strategy``) decide com ``EmsInputs``
   (medidas/estimadas) e o provedor de previsão (se houver). No modo
   ``predictive`` aplicam-se as salvaguardas locais do docs/06 §3.4
   (``on = cmd.on AND d_k`` para não deslocáveis; ``soc < critical_min_soc`` →
   média e baixa desligadas), como faria o firmware.
5. **Saídas finais** = comando AND ``allowed``; atualização de ``last_switch``.
6. **Física do passo**: potência das cargas ``P = p_nom·(V/v_ref)^k·duty`` na tensão
   do barramento (iteração de ponto fixo com a solução da bateria) + auxiliar;
   o MPPT entrega ``min(η·P_disp − autoconsumo, cargas + aux + aceitação de
   carga da bateria)``; a bateria cobre o déficit (limites de corrente/BMS). Se
   não conseguir, o BMS abre: cargas experimentais e auxiliar ficam sem energia
   (déficit registrado em ``deficit_p``) até haver corrente de carga.
   ``pv_p`` = potência puxada na entrada do MPPT; ``curtail_p = p_pv_avail − pv_p``.
7. Contabilização da carga deslocável (``def_done_wh`` por dia local, zera à
   meia-noite) e integração do SOC verdadeiro (contagem de Coulomb do modelo).

Colunas: todo o vocabulário do docs/05 §8 (valores **físicos verdadeiros** do
simulador — as métricas usam o verdadeiro, docs/06 §6) mais ``soc_true`` e
extras: ``tod_s``, ``soc_valid``, ``soc_last_sync``, ``soc_since_sync_s``,
``fallback``, ``bms_open``, ``deficit_p`` e as leituras simuladas dos sensores
``{pv,bat,bus}_{v,i}_meas`` (o que o ESP32 veria; usadas na telemetria).

Decisões ``sem.decision.v1`` (``source="simulation"``; ``origin="backend"`` no
preditivo, ``"firmware"`` nas demais) são registradas quando modo, estado, ações
comandadas, código de segurança ou ``fallback`` mudam, ou quando o MPC emite um
novo plano. Eventos ``sem.event.v1``: ``BOOT`` (início), eventos do estimador e
do supervisor, ``MODE_CHANGE``, ``BMS_UVP_TRIP``/``BMS_RELEASE`` (específicos do simulador).

Hipóteses de modelagem: barramento de cargas = terminais da bateria (queda em
cabos desprezada); ``t_bat = t_amb`` (sem modelo térmico); clima e demanda
idênticos para todas as estratégias com a mesma semente.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import numpy as np
import pandas as pd

import smartenergy
from smartenergy.config import SystemConfig
from smartenergy.ems.base import Decision, EmsInputs, ForecastProvider, LoadState, Strategy
from smartenergy.forecast.base import make_forecast, WeatherSeries
from smartenergy.forecast.demand import demand_mask
from smartenergy.metrics.indicators import compute_metrics, forecast_metrics
from smartenergy.models.battery import BatteryModel, BatteryOperatingPoint
from smartenergy.models.loads import load_power
from smartenergy.models.mppt import MpptModel
from smartenergy.models.pv import PvModel
from smartenergy.models.weather import (
    extra_days_for_horizon, generate_weather, make_rng, resolve_regime, scenario_days,
)
from smartenergy.timeutil import DAY_S, parse_hhmm

SIM_WARNING = "DADOS SIMULADOS — nao sao medicoes de hardware."

PREDICTIVE_MODES = ("predictive",)

SERIES_UNITS: dict[str, str] = {
    "t": "ms", "tod_s": "s",
    "pv_v": "V", "pv_i": "A", "pv_p": "W", "pv_p_avail": "W", "curtail_p": "W", "irr_poa": "W/m2",
    "t_amb": "degC", "t_pv": "degC", "t_bat": "degC",
    "bat_v": "V", "bat_i": "A", "bat_p": "W", "bus_v": "V", "bus_i": "A", "bus_p": "W", "aux_p": "W",
    "soc": "1", "soc_unc": "1", "soc_true": "1", "soc_valid": "0/1", "soc_last_sync": "text",
    "soc_since_sync_s": "s", "unserved_p": "W", "def_done_wh": "Wh", "deficit_p": "W",
    "mode": "text", "state": "text", "safety": "text", "fallback": "0/1", "bms_open": "0/1",
    "pv_v_meas": "V", "pv_i_meas": "A", "bat_v_meas": "V", "bat_i_meas": "A", "bus_v_meas": "V", "bus_i_meas": "A",
}
"""Unidades das colunas (cabeçalho ``coluna[unidade]`` do CSV); cargas: ver :func:`series_units`."""


def series_units(cfg: SystemConfig) -> dict[str, str]:
    """Unidades de todas as colunas, incluindo ``l{n}_*`` das cargas configuradas."""
    u = dict(SERIES_UNITS)
    for ld in cfg.loads:
        u.update({f"l{ld.id}_p": "W", f"l{ld.id}_on": "0/1", f"l{ld.id}_duty": "1", f"l{ld.id}_d": "0/1"})
    return u


@dataclass
class SimResult:
    """Resultado de uma simulação (todos os dados são SIMULADOS)."""

    df: pd.DataFrame
    metrics: dict[str, Any]
    decisions: list[dict[str, Any]]
    events: list[dict[str, Any]]
    forecasts: pd.DataFrame | None
    meta: dict[str, Any]


# ----------------------------------------------------------------------------- sensores
@dataclass
class SensorErrorModel:
    """Erros sistemáticos + ruído das medições simuladas (``simulation.sensor_error``)."""

    current_gain: float
    current_offset_a: float
    voltage_gain: float
    noise_std_current_a: float

    @classmethod
    def from_config(cls, cfg: SystemConfig) -> "SensorErrorModel":
        s = cfg.raw["simulation"].get("sensor_error", {}) or {}
        return cls(
            current_gain=float(s.get("current_gain", 0.0)),
            current_offset_a=float(s.get("current_offset_a", 0.0)),
            voltage_gain=float(s.get("voltage_gain", 0.0)),
            noise_std_current_a=float(s.get("noise_std_current_a", 0.0)),
        )

    def current(self, i_a: float, z: float, nonneg: bool = False) -> float:
        """Corrente medida = ``I·(1+ganho) + offset + σ·z`` (``z`` ~ N(0,1))."""
        v = i_a * (1.0 + self.current_gain) + self.current_offset_a + self.noise_std_current_a * z
        return max(v, 0.0) if nonneg else v

    def voltage(self, v: float) -> float:
        return v * (1.0 + self.voltage_gain)


# ----------------------------------------------------------------------------- física
@dataclass
class StepPhysics:
    """Resultado físico de um passo (valores verdadeiros, médios no passo)."""

    v_bus: float
    bat: BatteryOperatingPoint
    load_p: list[float]
    aux_p: float
    mppt_out: float
    pv_in: float
    deficit_p: float
    bms_open: bool
    bms_tripped: bool = False
    bms_released: bool = False


def _bisect_voltage(f: Callable[[float], float], lo: float, hi: float, iters: int = 40) -> float:
    """Raiz de ``f`` decrescente em ``[lo, hi]`` (``f(lo) ≥ 0 ≥ f(hi)``) por bisseção."""
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        if f(mid) >= 0.0:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def solve_step(
    battery: BatteryModel,
    mppt: MpptModel,
    loads: list[Any],
    duties: list[float],
    p_avail: float,
    aux_w: float,
    v_guess: float,
    dt_s: float,
    t_bat_c: float | None,
    bms_open: bool,
    iterations: int = 4,
) -> StepPhysics:
    """Resolve o balanço do barramento CC em um passo (não altera o SOC).

    Regimes (``V`` = tensão do barramento = terminais da bateria):

    * **livre** — o MPPT entrega o máximo ``η·P_disp − autoconsumo`` e a bateria
      absorve/fornece a diferença com ``V = OCV − I·R`` (ponto fixo em ``V``, pois as
      cargas dependem de ``V``; contração forte, converge em 2–3 iterações);
    * **CC limitado** — corrente de carga acima do limite (``min`` MPPT/bateria e
      SOC ≤ 1 no passo): ``I = −I_lim``, ``V = OCV + I_lim·R``, excedente cortado;
    * **CV** — SOC > 0,95 e carregador limitando: ``V = mppt.absorption_v``,
      ``I = −min(I_lim, (P_mppt,máx − P_cargas(V_abs) − aux)/V_abs)``; se nem as
      cargas em ``V_abs`` couberem no FV, o carregador regula parcialmente: ``I = 0``
      e ``V`` entre OCV e ``V_abs`` com ``P_cargas(V) + aux = P_mppt,máx`` (bisseção);
    * **descarga** — validada pelo BMS (corrente máx., ``bms_uvp_v``, SOC ≥ 0); se
      inviável, o BMS abre: cargas experimentais sem energia e o auxiliar só é
      alimentado se houver FV (excedente recarrega a bateria e fecha o BMS).

    Em todos os casos ``mppt_out + bat_p = Σ load_p + aux_p`` (``bat_p = V·I``, + descarga).
    """
    mppt_max = mppt.max_output(p_avail)
    n_ld = len(loads)

    def loads_at(v: float) -> list[float]:
        return [load_power(ld, v, d) for ld, d in zip(loads, duties)]

    def done(v: float, op: BatteryOperatingPoint, load_p: list[float], aux_p: float) -> StepPhysics:
        mppt_out = sum(load_p) + aux_p - op.p_w
        pv_in = mppt.input_for_output(mppt_out, p_avail) if mppt_out > 0.0 else 0.0
        return StepPhysics(v, op, load_p, aux_p, mppt_out if pv_in > 0.0 else 0.0, pv_in, 0.0, False)

    if not bms_open:
        e = battery.ocv()
        r = battery.params.r_internal_ohm
        v = v_guess
        feasible = True
        for _ in range(max(1, iterations)):
            p_b = sum(loads_at(v)) + aux_w - mppt_max
            i = battery.current_for_power(p_b, e)
            if i is None:
                feasible = False
                break
            v_new = e - i * r
            if abs(v_new - v) < 1e-9:
                v = v_new
                break
            v = v_new
        if feasible:
            load_p = loads_at(v)
            p_b = sum(load_p) + aux_w - mppt_max
            if p_b >= 0.0:
                op = battery.solve_discharge(p_b, dt_s) if p_b > 0.0 else battery.idle_point()
                if op is not None:
                    return done(op.v_v, op, load_p, aux_w)
            else:
                op = battery.solve_charge(-p_b, dt_s, t_bat_c)
                if op.regime in ("charge", "idle"):
                    return done(op.v_v, op, load_p, aux_w)
                i_lim = battery.charge_limit_a(dt_s, t_bat_c)
                if op.regime == "charge_cc":
                    v_cc = e + i_lim * r
                    load_cc = loads_at(v_cc)
                    if sum(load_cc) + aux_w + v_cc * i_lim <= mppt_max + 1e-9:
                        return done(v_cc, BatteryOperatingPoint(-i_lim, v_cc, -v_cc * i_lim, e, "charge_cc", True),
                                    load_cc, aux_w)
                    return done(op.v_v, BatteryOperatingPoint(op.i_a, v, v * op.i_a, e, "charge"), load_p, aux_w)
                # charge_cv
                v_abs = battery.params.absorption_v
                load_abs = loads_at(v_abs)
                surplus = mppt_max - sum(load_abs) - aux_w
                if surplus >= 0.0:
                    i_cv = min(i_lim, surplus / v_abs)
                    return done(v_abs, BatteryOperatingPoint(-i_cv, v_abs, -v_abs * i_cv, e, "charge_cv",
                                                             i_cv >= i_lim), load_abs, aux_w)
                v_lo = min(e, v_abs)
                v_star = _bisect_voltage(lambda vv: mppt_max - sum(loads_at(vv)) - aux_w, v_lo, v_abs)
                return done(v_star, BatteryOperatingPoint(0.0, v_star, 0.0, e, "charge_cv_partial", True),
                            loads_at(v_star), aux_w)

    # --- BMS aberto (ou abrindo agora): cargas experimentais sem energia --------
    tripped = not bms_open
    v_ref = v_guess if v_guess > 0 else battery.ocv()
    demanded = sum(loads_at(v_ref)) + aux_w
    released = False
    if mppt_max >= aux_w and mppt_max > 0.0:
        op = battery.solve_charge(mppt_max - aux_w, dt_s, t_bat_c)
        released = op.i_a < 0.0
        aux_p = aux_w
    else:
        op = battery.idle_point()
        aux_p = 0.0
    mppt_out = aux_p - op.p_w
    pv_in = mppt.input_for_output(mppt_out, p_avail) if mppt_out > 0.0 else 0.0
    deficit = max(0.0, demanded - aux_p)
    return StepPhysics(op.v_v, op, [0.0] * n_ld, aux_p, mppt_out if pv_in > 0.0 else 0.0, pv_in, deficit,
                       not released, bms_tripped=tripped, bms_released=released and not tripped)


# ----------------------------------------------------------------------------- fábricas padrão
def _default_estimator_factory(cfg: SystemConfig, initial_soc: float, v_init: float) -> Any:
    try:
        from smartenergy.estimation.soc import SocEstimator
    except ImportError as exc:  # pragma: no cover - depende de outro módulo
        raise ImportError("smartenergy.estimation.soc.SocEstimator ainda não disponível") from exc
    return SocEstimator(cfg, initial_soc=initial_soc, v_init=v_init)


def _default_safety_factory(cfg: SystemConfig) -> Any:
    try:
        from smartenergy.ems.safety import SafetySupervisor
    except ImportError as exc:  # pragma: no cover - depende de outro módulo
        raise ImportError("smartenergy.ems.safety.SafetySupervisor ainda não disponível") from exc
    return SafetySupervisor(cfg, boot_hold=False)


def _default_strategy_factory(name: str, cfg: SystemConfig) -> Strategy:
    from smartenergy.ems.registry import get_strategy

    return get_strategy(name, cfg)


def _event(ts_ms: int, code: str, level: str, msg: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    ev: dict[str, Any] = {"schema": "sem.event.v1", "ts_ms": int(ts_ms), "source": "simulation",
                          "level": level, "code": code, "msg": msg}
    if data:
        ev["data"] = data
    return ev


def _r(x: float, nd: int = 4) -> float:
    return float(round(float(x), nd))


# ----------------------------------------------------------------------------- simulação
def run_simulation(
    cfg: SystemConfig,
    scenario: str,
    strategy: str | None,
    seed: int = 1,
    *,
    days: int | None = None,
    forecast_method: str | None = None,
    initial_soc: float | None = None,
    overrides: dict[str, Any] | None = None,
    strategy_obj: Strategy | None = None,
    start_date: str | None = None,
    dt_s: float | None = None,
    estimator_factory: Callable[[SystemConfig, float, float], Any] | None = None,
    safety_factory: Callable[[SystemConfig], Any] | None = None,
    record_forecasts: bool = True,
) -> SimResult:
    """Executa uma simulação (cenário × estratégia × semente) — DADOS SIMULADOS.

    Args:
        cfg: configuração do sistema.
        scenario: cenário de ``simulation.scenarios`` (ou regime ``clear``/``partly``/``overcast``/``markov``).
        strategy: ``conventional`` | ``rules`` | ``predictive`` (ou ``None`` com ``strategy_obj``).
        seed: semente (clima, ruído de sensores e erro de previsão em fluxos independentes).
        days: dias simulados (padrão: do cenário).
        forecast_method: método de previsão FV; padrão ``forecast.pv_method`` para o
            preditivo e nenhum para as demais (se informado, as previsões são registradas
            e avaliadas também para elas).
        initial_soc: SOC verdadeiro inicial (padrão ``simulation.initial_soc``); o estimado
            começa em ``initial_soc + simulation.initial_soc_estimate_error``.
        overrides: substituições de parâmetros (``cfg.with_overrides``), ex. ``{"battery.capacity_ah": 12}``.
        strategy_obj: instância de estratégia já construída (testes/estudos).
        start_date: dia inicial local ``AAAA-MM-DD`` (padrão ``simulation.start_date``).
        dt_s: passo (padrão ``simulation.dt_s``).
        estimator_factory: ``(cfg, soc_inicial, v_init) -> estimador`` (padrão ``SocEstimator``).
        safety_factory: ``(cfg) -> supervisor`` (padrão ``SafetySupervisor(cfg, boot_hold=False)``).
        record_forecasts: registra as previsões emitidas a cada ``replan_s``.

    Returns:
        :class:`SimResult` com ``df`` (série), ``metrics`` (docs/06 §5), ``decisions``
        (``sem.decision.v1`` só quando mudam), ``events`` (``sem.event.v1``),
        ``forecasts`` (DataFrame ou ``None``) e ``meta``.
    """
    wall0 = time.perf_counter()
    cfg = cfg.with_overrides(overrides)
    raw = cfg.raw
    sim = raw["simulation"]
    dt = float(dt_s if dt_s is not None else sim["dt_s"])
    n_days = int(days if days is not None else scenario_days(cfg, scenario))
    start = str(start_date or sim["start_date"])
    soc0 = float(initial_soc if initial_soc is not None else sim["initial_soc"])
    soc0 = min(max(soc0, 0.0), 1.0)
    strat_name = strategy or getattr(strategy_obj, "name", "custom")
    strat = strategy_obj if strategy_obj is not None else _default_strategy_factory(strat_name, cfg)
    strat.reset()
    is_predictive = strat_name in PREDICTIVE_MODES

    pred = raw["ems"]["predictive"]
    fc_step_s = int(pred["step_s"])
    fc_n = int(pred["horizon_steps"])
    replan_s = float(pred["replan_s"])
    fc_method = forecast_method or (str(raw["forecast"]["pv_method"]) if is_predictive else None)
    extra = extra_days_for_horizon(fc_step_s * fc_n) if fc_method else 0

    weather = generate_weather(cfg, scenario, seed, start, n_days + extra, dt)
    spd = int(round(DAY_S / dt))
    n = spd * n_days
    forecast: ForecastProvider | None = make_forecast(fc_method, cfg, weather=weather, seed=seed) if fc_method else None

    # --- arrays de entrada (listas Python: indexação escalar rápida) ---------------
    w = weather.iloc[:n]
    t_arr = w["t"].to_numpy(dtype=np.int64)
    tod_arr = w["tod_s"].to_numpy(dtype=np.float64)
    p_avail_l = w["p_pv_avail"].tolist()
    poa_l = w["poa"].tolist()
    t_amb_l = w["t_amb"].tolist()
    t_cell_l = w["t_cell"].tolist()
    t_l = t_arr.tolist()
    tod_l = tod_arr.tolist()
    utc_off_s = float(raw["site"]["utc_offset_h"]) * 3600.0
    day_l = np.floor((t_arr / 1000.0 + utc_off_s) / DAY_S).astype(np.int64).tolist()

    loads = list(cfg.loads)
    nondef = [ld for ld in loads if ld.deferrable is None]
    deferr = [ld for ld in loads if ld.deferrable is not None]
    dmask = {ld.key: demand_mask(cfg, ld.key, tod_arr).tolist() for ld in nondef}
    def_win = {ld.key: (parse_hhmm(ld.deferrable.window_start), parse_hhmm(ld.deferrable.deadline),
                        float(ld.deferrable.daily_energy_wh)) for ld in deferr}
    aux_w = float(raw["aux"]["p_w"])
    critical_soc = float(raw["ems"]["rules"]["critical_min_soc"])

    battery = BatteryModel.from_config(cfg, soc0)
    mppt = MpptModel.from_config(cfg)
    pvm = PvModel.from_config(cfg)
    sens = SensorErrorModel.from_config(cfg)
    noise = make_rng(seed, "sensor/noise").standard_normal((n + 1, 3)).tolist()

    # --- medições iniciais (bateria em repouso, sem FV) ----------------------------
    v_true = battery.ocv()
    z0 = noise[n]
    m_bat_v = sens.voltage(v_true)
    m_bat_i = sens.current(0.0, z0[0])
    m_pv_v, m_pv_i = 0.0, sens.current(0.0, z0[1], nonneg=True)
    m_bus_v, m_bus_i = sens.voltage(v_true), sens.current(0.0, z0[2], nonneg=True)
    t_bat = t_amb_l[0] if n else 25.0

    soc_est0 = min(max(soc0 + float(sim.get("initial_soc_estimate_error", 0.0) or 0.0), 0.0), 1.0)
    estimator = (estimator_factory or _default_estimator_factory)(cfg, soc_est0, m_bat_v)
    safety = (safety_factory or _default_safety_factory)(cfg)
    if hasattr(safety, "reset"):
        safety.reset()

    outputs = {ld.key: LoadState(id=ld.id, key=ld.key) for ld in loads}
    e_done = {ld.key: 0.0 for ld in deferr}
    cur_day = day_l[0] if n else 0
    bms_open = False

    cols: dict[str, list[Any]] = {c: [] for c in (
        "t", "tod_s", "pv_v", "pv_i", "pv_p", "pv_p_avail", "curtail_p", "irr_poa", "t_amb", "t_pv", "t_bat",
        "bat_v", "bat_i", "bat_p", "bus_v", "bus_i", "bus_p", "aux_p", "soc", "soc_unc", "soc_true",
        "soc_valid", "soc_last_sync", "soc_since_sync_s",
    )}
    for ld in loads:
        for suf in ("p", "on", "duty", "d"):
            cols[f"l{ld.id}_{suf}"] = []
    for c in ("unserved_p", "def_done_wh", "deficit_p", "mode", "state", "safety", "fallback", "bms_open",
              "pv_v_meas", "pv_i_meas", "bat_v_meas", "bat_i_meas", "bus_v_meas", "bus_i_meas"):
        cols[c] = []

    t0_ms = int(t_l[0]) if n else 0
    events: list[dict[str, Any]] = [
        _event(t0_ms, "BOOT", "info", f"Inicio da simulacao ({SIM_WARNING})",
               {"scenario": scenario, "strategy": strat_name, "seed": int(seed)}),
    ]
    decisions: list[dict[str, Any]] = []
    fc_rows: list[tuple[int, int, int, float]] = []
    last_key: Any = None
    last_state: str | None = None
    last_mode: str | None = None
    last_plan_id: int | None = None
    origin = "backend" if is_predictive else "firmware"
    pv_p_meas = 0.0

    for k in range(n):
        t_ms = int(t_l[k])
        t_s = k * dt
        tod = tod_l[k]
        if day_l[k] != cur_day:
            cur_day = day_l[k]
            for key in e_done:
                e_done[key] = 0.0

        # ---- estado estimado (integrado até t_k) e segurança --------------------
        soc_est = float(estimator.soc)
        soc_unc = float(getattr(estimator, "unc", 0.0))
        soc_valid = bool(getattr(estimator, "valid", True))
        st = safety.update(t_s, dt, m_bat_v, soc_est, soc_valid, v_bus=m_bus_v, i_bus=m_bus_i, t_bat=t_bat)
        for code, level, msg in getattr(st, "events", []) or []:
            events.append(_event(t_ms, code, level, msg, {"v_bat": _r(m_bat_v, 3), "soc": _r(soc_est)}))
        allowed = bool(st.allowed)

        # ---- EMS ----------------------------------------------------------------
        if forecast is not None:
            forecast.observe(t_ms, pv_p_meas)
        demand = {ld.key: bool(dmask[ld.key][k]) for ld in nondef}
        p_bat_m = m_bat_v * m_bat_i
        inp = EmsInputs(
            t_s=t_s, ts_ms=t_ms, tod_s=tod, soc=soc_est, soc_valid=soc_valid, v_bat=m_bat_v, i_bat=m_bat_i,
            p_bat=p_bat_m, p_pv=pv_p_meas, p_bus=m_bus_v * m_bus_i, demand=demand,
            deferrable_done_wh=dict(e_done), outputs=dict(outputs), soc_unc=soc_unc, t_bat_c=t_bat,
        )
        dec: Decision = strat.decide(inp, forecast)
        if record_forecasts and forecast is not None and t_s % replan_s == 0 and tod % fc_step_s == 0:
            fc = forecast.pv_forecast(t_ms, fc_step_s, fc_n)
            fc_rows.extend((t_ms, t_ms + j * fc_step_s * 1000, j, float(v)) for j, v in enumerate(fc))

        # ---- salvaguardas do modo preditivo (docs/06 §3.4) e saídas finais -------
        duties: list[float] = []
        for ld in loads:
            cmd = dec.commands.get(ld.key)
            on_cmd = bool(cmd.on) if cmd is not None else False
            duty_cmd = float(cmd.duty) if cmd is not None else 0.0
            if is_predictive:
                if ld.deferrable is None and not demand[ld.key]:
                    on_cmd = False
                if soc_est < critical_soc and ld.priority in ("medium", "low"):
                    on_cmd = False
            on = on_cmd and allowed
            duty = (min(max(duty_cmd, 0.0), 1.0) if ld.dimmable else 1.0) if on else 0.0
            if on and duty <= 0.0:
                duty = 1.0
            prev = outputs[ld.key]
            last_sw = t_s if on != prev.on else prev.last_switch_t_s
            outputs[ld.key] = LoadState(id=ld.id, key=ld.key, on=on, duty=duty, last_switch_t_s=last_sw)
            duties.append(duty)

        # ---- registro de decisões (só quando mudam) -------------------------------
        acts = tuple((c.id, bool(c.on), round(float(c.duty), 4))
                     for c in sorted(dec.commands.values(), key=lambda c: c.id))
        plan_id = id(dec.plan) if dec.plan is not None else None
        key_now = (dec.mode, dec.state, acts, st.code, bool(dec.fallback))
        if key_now != last_key or (plan_id is not None and plan_id != last_plan_id):
            if last_mode is not None and dec.mode != last_mode:
                events.append(_event(t_ms, "MODE_CHANGE", "info", f"modo {last_mode} -> {dec.mode}"))
            decisions.append({
                "schema": "sem.decision.v1", "ts_ms": t_ms, "source": "simulation", "origin": origin,
                "mode": dec.mode, "state": dec.state, "prev_state": last_state, "safety": st.code,
                "reason": str(dec.reason), "fallback": bool(dec.fallback),
                "actions": [{"id": a[0], "on": a[1], "duty": a[2]} for a in acts],
                "inputs": {"soc": _r(soc_est), "soc_valid": soc_valid, "v_bat": _r(m_bat_v, 3),
                           "p_pv": _r(pv_p_meas, 2), "p_bat": _r(p_bat_m, 2), "p_bus": _r(m_bus_v * m_bus_i, 2)},
                "plan": dec.plan,
            })
            last_key, last_state, last_mode, last_plan_id = key_now, dec.state, dec.mode, plan_id

        # ---- física do passo ----------------------------------------------------
        p_av = p_avail_l[k]
        t_bat = t_amb_l[k]
        soc_true = battery.soc
        ph = solve_step(battery, mppt, loads, duties, p_av, aux_w, v_true, dt, t_bat, bms_open)
        if ph.bms_tripped:
            events.append(_event(t_ms, "BMS_UVP_TRIP", "critical",
                                 "BMS abriu: bateria nao suporta a demanda (subtensao/corrente/SOC)",
                                 {"soc_true": _r(soc_true), "deficit_w": _r(ph.deficit_p, 2)}))
        if ph.bms_released:
            events.append(_event(t_ms, "BMS_RELEASE", "info", "BMS fechou: corrente de carga presente"))
        bms_open = ph.bms_open
        battery.integrate(ph.bat.i_a, dt)
        v_true = ph.v_bus
        pv_v, pv_i = pvm.operating_point(poa_l[k], t_cell_l[k], p_av, ph.pv_in)
        bus_p = sum(ph.load_p)
        bus_i = bus_p / v_true if v_true > 0 else 0.0

        # ---- linha da série (estado em t_k, potências médias do passo) ------------
        unserved = 0.0
        for idx, ld in enumerate(loads):
            p_ld = ph.load_p[idx]
            o = outputs[ld.key]
            if ld.deferrable is None:
                d = demand[ld.key]
                if d:
                    unserved += max(0.0, load_power(ld, v_true, 1.0) - p_ld)
            else:
                ws, dl, req = def_win[ld.key]
                inwin = (ws <= tod < dl) if ws < dl else (tod >= ws or tod < dl)
                d = inwin and e_done[ld.key] < req
            cols[f"l{ld.id}_p"].append(p_ld)
            cols[f"l{ld.id}_on"].append(int(o.on))
            cols[f"l{ld.id}_duty"].append(o.duty)
            cols[f"l{ld.id}_d"].append(int(d))
        def_key = deferr[0].key if deferr else None
        row = cols
        row["t"].append(t_ms); row["tod_s"].append(tod)
        row["pv_v"].append(pv_v); row["pv_i"].append(pv_i); row["pv_p"].append(ph.pv_in)
        row["pv_p_avail"].append(p_av); row["curtail_p"].append(max(0.0, p_av - ph.pv_in))
        row["irr_poa"].append(poa_l[k]); row["t_amb"].append(t_amb_l[k]); row["t_pv"].append(t_cell_l[k])
        row["t_bat"].append(t_bat)
        row["bat_v"].append(v_true); row["bat_i"].append(ph.bat.i_a); row["bat_p"].append(ph.bat.p_w)
        row["bus_v"].append(v_true); row["bus_i"].append(bus_i); row["bus_p"].append(bus_p)
        row["aux_p"].append(ph.aux_p)
        row["soc"].append(soc_est); row["soc_unc"].append(soc_unc); row["soc_true"].append(soc_true)
        row["soc_valid"].append(int(soc_valid))
        row["soc_last_sync"].append(str(getattr(estimator, "last_sync", "none")))
        row["soc_since_sync_s"].append(float(getattr(estimator, "since_sync_s", 0.0) or 0.0))
        row["unserved_p"].append(unserved)
        row["def_done_wh"].append(e_done[def_key] if def_key else 0.0)
        row["deficit_p"].append(ph.deficit_p)
        row["mode"].append(dec.mode); row["state"].append(dec.state); row["safety"].append(st.code)
        row["fallback"].append(int(bool(dec.fallback))); row["bms_open"].append(int(bms_open))

        # ---- energia da deslocável --------------------------------------------------
        for idx, ld in enumerate(loads):
            if ld.deferrable is not None:
                e_done[ld.key] += ph.load_p[idx] * dt / 3600.0

        # ---- medições do passo (usadas na próxima decisão) e estimador ---------------
        z = noise[k]
        m_bat_v = sens.voltage(v_true)
        m_bat_i = sens.current(ph.bat.i_a, z[0])
        m_pv_v = sens.voltage(pv_v)
        m_pv_i = sens.current(pv_i, z[1], nonneg=True) if pv_v > 0 else 0.0
        m_bus_v = sens.voltage(v_true)
        m_bus_i = sens.current(bus_i, z[2], nonneg=True)
        pv_p_meas = m_pv_v * m_pv_i
        row["pv_v_meas"].append(m_pv_v); row["pv_i_meas"].append(m_pv_i)
        row["bat_v_meas"].append(m_bat_v); row["bat_i_meas"].append(m_bat_i)
        row["bus_v_meas"].append(m_bus_v); row["bus_i_meas"].append(m_bus_i)
        for code, level, msg in estimator.update(dt, m_bat_i, m_bat_v, True) or []:
            events.append(_event(t_ms + int(dt * 1000), code, level, msg, {"soc": _r(estimator.soc)}))

    df = pd.DataFrame(cols)
    df.attrs.update({"source": "simulation", "dt_s": dt, "soc_true_end": battery.soc,
                     "scenario": scenario, "strategy": strat_name, "seed": int(seed)})
    metrics = compute_metrics(df, cfg, dt)

    forecasts_df: pd.DataFrame | None = None
    if fc_rows:
        forecasts_df = pd.DataFrame(fc_rows, columns=["t_issue", "t", "lead_steps", "pv_fc_w"])
        truth = WeatherSeries(weather["t"].to_numpy(), weather["p_pv_avail"].to_numpy())
        forecasts_df["pv_actual_w"] = _bulk_actual(truth, forecasts_df["t"].to_numpy(), fc_step_s)
        fm = forecast_metrics(forecasts_df["pv_actual_w"], forecasts_df["pv_fc_w"], float(raw["pv"]["p_stc_w"]))
        metrics.update({f"forecast_{k}": v for k, v in fm.items()})
    if fc_method:
        metrics["forecast_method"] = fc_method

    meta = {
        "source": "simulation",
        "warning": SIM_WARNING,
        "scenario": scenario,
        "regime": resolve_regime(cfg, scenario),
        "day_regimes": list(weather.attrs.get("day_regimes", []))[:n_days],
        "strategy": strat_name,
        "seed": int(seed),
        "dt_s": dt,
        "start_date": start,
        "t0_ms": t0_ms,
        "days": n_days,
        "initial_soc": soc0,
        "initial_soc_estimate": soc_est0,
        "forecast_method": fc_method,
        "forecast": forecast.describe() if forecast is not None and hasattr(forecast, "describe") else None,
        "version": smartenergy.__version__,
        "overrides": dict(overrides or {}),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "wall_time_s": round(time.perf_counter() - wall0, 3),
    }
    return SimResult(df=df, metrics=metrics, decisions=decisions, events=events, forecasts=forecasts_df, meta=meta)


def _bulk_actual(truth: WeatherSeries, t_targets: np.ndarray, step_s: int) -> np.ndarray:
    """Média verdadeira de ``p_pv_avail`` em ``[t, t + step)`` para muitos ``t`` (vetorizado)."""
    step_ms = float(step_s) * 1000.0
    n_sub = max(1, int(np.ceil(step_ms / truth.dt_ms - 1e-9)))
    offs = (np.arange(n_sub) + 0.5) * step_ms / n_sub
    ts = (np.asarray(t_targets, dtype=np.float64)[:, None] + offs[None, :]).ravel()
    return truth.value_at(ts).reshape(-1, n_sub).mean(axis=1)


__all__ = ["SimResult", "run_simulation", "solve_step", "series_units", "SensorErrorModel", "SIM_WARNING",
           "SERIES_UNITS"]
