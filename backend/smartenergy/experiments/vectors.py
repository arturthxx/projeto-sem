"""Geração dos vetores golden de equivalência firmware ↔ Python (docs/06 §7).

Uso::

    python -m smartenergy gen-vectors [--out firmware/test/vectors/rules_vectors.json]

O arquivo ``sem.vectors.v1`` contém os parâmetros usados e, para cada módulo
(regras, SOC, segurança), cenários com as entradas passo a passo e as saídas
esperadas calculadas pelas implementações Python (``ems.rules``,
``estimation.soc``, ``ems.safety``). O teste nativo do firmware reproduz as
sequências e exige igualdade (duty/SOC com tolerância).

Harness (idêntico nos dois lados):

* regras: saídas iniciais desligadas com ``last_switch = −inf``; após cada passo
  as saídas passam a ser as esperadas e, se ``on`` mudou, ``last_switch = t_s``;
  as regras ignoram a segurança; cada cenário começa com estado novo
  (primeira avaliação sem histerese);
* SOC: ``init`` = ``{"soc", "unc"}`` (valor persistido) ou ``{"v_init"}`` (OCV⁻¹);
  em cada passo ``update(dt_s, i_a, v_v, ok)``; ``i_a``/``v_v`` são ``null``
  quando ``ok = false``;
* segurança: supervisor SEM ``boot_hold``; ``v_bat``/``v_bus``/``t_bat``/``i_bus``
  podem ser ``null`` (sensor inválido); um passo com ``"reset_faults": true``
  chama ``reset_faults()`` ANTES de ``update``.

Os ``dt`` dos cenários de segurança são frações diádicas (0,125/0,25/0,5/1/5/100 s)
para que a soma dos temporizadores seja exata em ponto flutuante e em ms
inteiros; os limiares de SOC testados evitam igualdades que dependam de
arredondamento (ex.: ``T + H``), exceto ``soc == T`` (literal idêntico).

Os vetores são SINTÉTICOS (não são medições).
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Callable

from smartenergy.config import REPO_ROOT, SystemConfig, load_config
from smartenergy.ems.base import EmsInputs, LoadState
from smartenergy.ems.rules import RuleBasedStrategy, RulesParams, deferrable_window_s
from smartenergy.ems.safety import SafetySupervisor
from smartenergy.estimation.soc import SocEstimator, SocParams

SCHEMA = "sem.vectors.v1"
DEFAULT_OUT = REPO_ROOT / "firmware" / "test" / "vectors" / "rules_vectors.json"

Step = dict[str, Any]


# =============================================================================
# Parâmetros
# =============================================================================
def build_params(cfg: SystemConfig) -> dict[str, Any]:
    rp = RulesParams.from_config(cfg)
    sp = SocParams.from_config(cfg)
    loads = []
    for ld in cfg.loads:
        dfr = None
        if ld.deferrable is not None:
            ws, dl = deferrable_window_s(ld)
            dfr = {"daily_energy_wh": ld.deferrable.daily_energy_wh, "window_start_s": ws, "deadline_s": dl}
        loads.append({
            "id": ld.id, "key": ld.key, "priority": ld.priority, "p_nom_w": ld.p_nom_w,
            "dimmable": ld.dimmable, "min_duty": ld.min_duty, "deferrable": dfr,
        })
    soc = {
        "capacity_ah": sp.capacity_ah,
        "coulombic_efficiency": sp.coulombic_efficiency,
        "r_internal_ohm": sp.r_internal_ohm,
        "current_deadband_a": sp.current_deadband_a,
        "full_voltage_v": sp.full_voltage_v,
        "full_tail_current_a": sp.full_tail_current_a,
        "full_hold_s": sp.full_hold_s,
        "empty_voltage_v": sp.empty_voltage_v,
        "empty_hold_s": sp.empty_hold_s,
        "empty_soc_value": sp.empty_soc_value,
        "unknown_initial_uncertainty": sp.unknown_initial_uncertainty,
        "sync_uncertainty": sp.sync_uncertainty,
        "drift_uncertainty_per_ah": sp.drift_uncertainty_per_ah,
        "ocv_soc": list(sp.ocv_soc),
        "ocv_v": list(sp.ocv_v),
    }
    return {
        "rules": {
            "normal_min_soc": rp.normal_min_soc,
            "economy_min_soc": rp.economy_min_soc,
            "critical_min_soc": rp.critical_min_soc,
            "hysteresis_soc": rp.hysteresis_soc,
            "economy_medium_duty": rp.economy_medium_duty,
            "min_dwell_s": rp.min_dwell_s,
            "surplus_on_soc": rp.surplus_on_soc,
            "surplus_off_soc": rp.surplus_off_soc,
            "economy_surplus_margin_w": rp.economy_surplus_margin_w,
            "deadline_margin_s": rp.deadline_margin_s,
        },
        "loads": loads,
        "soc": soc,
        "safety": dict(cfg.raw["safety"]),
    }


# =============================================================================
# Harness (o mesmo descrito no cabeçalho; usado na geração e na verificação)
# =============================================================================
def run_rules(cfg: SystemConfig, steps: list[Step]) -> list[dict[str, Any]]:
    """Executa um cenário de regras e devolve as saídas esperadas por passo."""
    strat = RuleBasedStrategy(cfg)
    outputs = {ld.key: LoadState(id=ld.id, key=ld.key) for ld in cfg.loads}
    expects = []
    for st in steps:
        inp = EmsInputs(
            t_s=float(st["t_s"]), ts_ms=0, tod_s=float(st["tod_s"]), soc=float(st["soc"]),
            soc_valid=bool(st["soc_valid"]), v_bat=0.0, i_bat=0.0, p_bat=float(st["p_bat"]),
            p_pv=0.0, p_bus=0.0, demand={k: bool(v) for k, v in st["d"].items()},
            deferrable_done_wh={k: float(v) for k, v in st["e_done_wh"].items()},
            outputs={k: LoadState(v.id, v.key, v.on, v.duty, v.last_switch_t_s) for k, v in outputs.items()},
        )
        dec = strat.decide(inp)
        loads = {}
        for ld in cfg.loads:
            c = dec.commands[ld.key]
            loads[ld.key] = {"on": bool(c.on), "duty": float(c.duty)}
            o = outputs[ld.key]
            if bool(c.on) != o.on:
                o.last_switch_t_s = float(st["t_s"])
            o.on, o.duty = bool(c.on), float(c.duty)
        expects.append({"state": dec.state, "loads": loads})
    return expects


def run_soc(cfg: SystemConfig, init: dict[str, Any], steps: list[Step]) -> list[dict[str, Any]]:
    if "v_init" in init:
        est = SocEstimator(cfg, v_init=float(init["v_init"]))
    else:
        est = SocEstimator(cfg, initial_soc=float(init["soc"]), initial_unc=float(init["unc"]))
    expects = []
    for st in steps:
        ev = est.update(float(st["dt_s"]), st["i_a"], st["v_v"], bool(st["ok"]))
        expects.append({
            "soc": est.soc, "unc": est.unc, "last_sync": est.last_sync, "valid": est.valid,
            "events": [e[0] for e in ev],
        })
    return expects


def run_safety(cfg: SystemConfig, steps: list[Step]) -> list[dict[str, Any]]:
    sup = SafetySupervisor(cfg, boot_hold=False)
    expects = []
    for st in steps:
        if st.get("reset_faults"):
            sup.reset_faults()
        res = sup.update(
            float(st["t_s"]), float(st["dt_s"]), st["v_bat"], float(st["soc"]), bool(st["soc_valid"]),
            v_bus=st["v_bus"], i_bus=st["i_bus"], t_bat=st["t_bat"],
        )
        expects.append({"allowed": res.allowed, "code": res.code, "events": [e[0] for e in res.events]})
    return expects


# =============================================================================
# Cenários de regras
# =============================================================================
H = 3600


def _rs(t: float, tod: float, soc: float, valid: bool = True, p_bat: float = 0.0,
        led: bool = True, fan: bool = True, res_done: float = 0.0) -> Step:
    return {"t_s": t, "tod_s": tod, "soc": soc, "soc_valid": valid, "p_bat": p_bat,
            "d": {"led": led, "fan": fan}, "e_done_wh": {"res": res_done}}


def rules_scenarios() -> list[tuple[str, list[Step]]]:
    sc: list[tuple[str, list[Step]]] = []
    eve = 19 * H + 1800  # 19:30: led e fan pedidos; res concluída nos cenários de histerese

    # Primeira avaliação (sem histerese), inclusive nas fronteiras exatas T.
    for soc in (0.80, 0.70, 0.6999, 0.40, 0.3999, 0.20, 0.1999, 0.0):
        sc.append((f"init_soc_{soc:.4f}", [_rs(0, eve, soc, res_done=40.0)]))
    sc.append(("init_soc_invalid", [_rs(0, eve, 0.95, valid=False, res_done=40.0),
                                    _rs(60, eve, 0.95, valid=True, res_done=40.0)]))

    # Histerese: descida imediata e subida com H em cada fronteira.
    seq = [0.80, 0.71, 0.70, 0.6999, 0.72, 0.7499, 0.7501, 0.69, 0.45, 0.40, 0.3999, 0.44, 0.4499,
           0.4501, 0.39, 0.25, 0.20, 0.1999, 0.24, 0.2499, 0.2501, 0.10, 0.30, 0.50, 0.7499, 0.80,
           0.15, 0.05, 0.96]
    sc.append(("hysteresis_all_boundaries", [_rs(60 * i, eve, s, res_done=40.0) for i, s in enumerate(seq)]))

    # SOC inválido -> CRITICAL (nível = min(nível, 1)); PROTECTION permanece.
    inv = [(0.80, True), (0.80, False), (0.10, False), (0.80, True), (0.50, True), (0.15, True),
           (0.90, False), (0.90, True), (0.30, True)]
    sc.append(("soc_invalid", [_rs(60 * i, eve, s, valid=v, res_done=40.0) for i, (s, v) in enumerate(inv)]))

    # Demanda ligando/desligando em cada estado.
    dem = [(0.80, True, False), (0.80, False, True), (0.80, False, False), (0.60, True, True),
           (0.60, True, False), (0.60, False, True), (0.30, True, True), (0.30, False, True),
           (0.10, True, True), (0.10, False, False)]
    sc.append(("demand_toggle", [_rs(60 * i, eve, s, led=a, fan=b, res_done=40.0)
                                 for i, (s, a, b) in enumerate(dem)]))

    # ECONOMY com duty reduzido do ventilador.
    sc.append(("economy_duty", [_rs(0, 12 * H, 0.55, led=False, fan=True, res_done=40.0),
                                _rs(60, 12 * H + 60, 0.55, led=True, fan=True, res_done=40.0),
                                _rs(120, 12 * H + 120, 0.74, led=True, fan=True, res_done=40.0),
                                _rs(180, 12 * H + 180, 0.76, led=True, fan=True, res_done=40.0)]))

    # Deslocável: latch por excedente em NORMAL + permanência mínima bloqueando/liberando.
    t0 = 10 * H
    latch = [(0, 0.85), (60, 0.90), (120, 0.85), (180, 0.7999), (240, 0.79), (360, 0.79),
             (420, 0.95), (480, 0.95), (660, 0.95), (720, 0.82), (780, 0.81)]
    sc.append(("res_latch_dwell_normal", [_rs(t, t0 + t, s, led=False, fan=False, res_done=10.0)
                                          for t, s in latch]))

    # Deslocável: prazo forçado (apenas NORMAL), liberação ao entregar energia, energia concluída.
    b = 68000
    dl = [(0, 68000, 0.75, 0.0), (700, 68700, 0.75, 0.0), (760, 68760, 0.75, 5.0),
          (2000, 70000, 0.75, 20.0), (7000, 75000, 0.75, 20.0), (7060, 75060, 0.75, 40.0),
          (7120, 75120, 0.75, 40.0)]
    sc.append(("res_deadline_and_done", [_rs(t, tod, s, led=False, fan=False, res_done=e) for t, tod, s, e in dl]))
    del b

    # Deslocável: fim da janela desliga imediatamente (mesmo dentro da permanência mínima).
    end = [(0, 79000, 0.75, 38.0), (60, 79060, 0.75, 38.0), (140, 79140, 0.75, 38.0),
           (200, 79200, 0.75, 38.0), (260, 79260, 0.95, 38.0)]
    sc.append(("res_window_end", [_rs(t, tod, s, led=True, fan=True, res_done=e) for t, tod, s, e in end]))

    # Deslocável: início da janela (06:59 fora, 07:00 dentro).
    beg = [(0, 7 * H - 60, 0.95, 0.0), (60, 7 * H, 0.95, 0.0), (120, 7 * H + 60, 0.95, 0.0)]
    sc.append(("res_window_start", [_rs(t, tod, s, led=False, fan=False, res_done=e) for t, tod, s, e in beg]))

    # Deslocável em ECONOMY: excedente pela potência da bateria (p_bat < 0 = carga).
    eco = [(0, 0.60, 0.0), (60, 0.60, -10.0), (120, 0.60, -18.5), (180, 0.60, 1.5), (240, 0.60, 2.5),
           (420, 0.60, 2.5), (480, 0.60, -25.0), (600, 0.60, -25.0), (780, 0.60, -25.0),
           (840, 0.60, -16.0)]
    sc.append(("res_economy_surplus", [_rs(t, 11 * H + t, s, p_bat=p, led=False, fan=True, res_done=0.0)
                                       for t, s, p in eco]))
    # ECONOMY não força prazo.
    sc.append(("res_economy_no_deadline", [_rs(0, 21 * H, 0.60, p_bat=0.0, res_done=0.0),
                                           _rs(60, 21 * H + 60, 0.60, p_bat=-5.0, res_done=0.0)]))

    # Deslocável ligada por latch em NORMAL, depois ECONOMY (latch vira regra de p_bat) e CRITICAL.
    tr = [(0, 0.92, -30.0), (60, 0.69, -5.0), (120, 0.66, 1.0), (180, 0.65, 3.0), (480, 0.65, 3.0),
          (540, 0.70, -30.0), (600, 0.92, -30.0), (900, 0.92, -30.0), (960, 0.39, -30.0)]
    sc.append(("res_state_transitions", [_rs(t, 12 * H + t, s, p_bat=p, led=False, fan=True, res_done=0.0)
                                         for t, s, p in tr]))
    return sc


# =============================================================================
# Cenários de SOC
# =============================================================================
def _ss(dt: float, i: float | None, v: float | None, ok: bool = True) -> Step:
    return {"dt_s": dt, "i_a": i, "v_v": v, "ok": ok}


def soc_scenarios() -> list[tuple[str, dict[str, Any], list[Step]]]:
    sc: list[tuple[str, dict[str, Any], list[Step]]] = []
    sc.append(("discharge_persisted", {"soc": 0.5, "unc": 0.05},
               [_ss(1.0, 2.0, 13.0)] * 5 + [_ss(60.0, 2.0, 12.95)] * 5 + [_ss(600.0, 3.0, 12.9)] * 3))
    sc.append(("charge_coulombic_eff", {"soc": 0.4, "unc": 0.05},
               [_ss(1.0, -2.0, 13.3)] * 3 + [_ss(300.0, -2.5, 13.4)] * 4))
    sc.append(("deadband", {"soc": 0.5, "unc": 0.05},
               [_ss(10.0, 0.004, 13.1), _ss(10.0, -0.0049, 13.1), _ss(10.0, 0.0, 13.1),
                _ss(10.0, 0.005, 13.1), _ss(10.0, -0.005, 13.1), _ss(10.0, 0.0051, 13.1)]))
    full = ([_ss(10.0, -1.0, 14.1)] * 3 + [_ss(10.0, -0.25, 14.1)] * 13 + [_ss(10.0, -0.2, 14.0)] * 2
            + [_ss(10.0, -0.1, 13.5)] + [_ss(10.0, -0.25, 14.2)] * 6 + [_ss(10.0, -0.31, 14.2)]
            + [_ss(10.0, 0.25, 14.05)] * 12)
    sc.append(("full_sync", {"soc": 0.9, "unc": 0.1}, full))
    empty = ([_ss(5.0, 1.0, 11.9)] * 2 + [_ss(5.0, 1.0, 11.70)] * 7 + [_ss(5.0, 1.0, 11.78)]
             + [_ss(5.0, 2.0, 11.70)] * 6 + [_ss(5.0, -0.5, 11.70)] * 2)
    sc.append(("empty_sync", {"soc": 0.3, "unc": 0.1}, empty))
    sc.append(("empty_sync_keeps_lower_soc", {"soc": 0.03, "unc": 0.1}, [_ss(10.0, 0.5, 11.5)] * 4))
    for v in (13.1, 12.4, 9.5, 14.0, 13.025, 11.6, 13.62):
        sc.append((f"ocv_init_{v:.3f}", {"v_init": v}, [_ss(1.0, 0.0, v), _ss(60.0, 1.0, v)]))
    sens = ([_ss(1.0, 1.0, 13.0)] * 2 + [_ss(1.0, None, None, ok=False)] * 2 + [_ss(1.0, 1.0, 13.0)]
            + [_ss(1.0, None, None, ok=False)] * 5 + [_ss(1.0, 1.0, 13.0)] * 2
            + [_ss(0.5, None, None, ok=False)] * 8 + [_ss(1.0, -1.0, 13.2)])
    sc.append(("sensor_invalid", {"soc": 0.5, "unc": 0.05}, sens))
    sc.append(("clamp_empty_and_full", {"soc": 0.001, "unc": 0.05},
               [_ss(60.0, 2.0, 12.0)] * 2 + [_ss(3600.0, -6.0, 13.6)] * 2))
    sc.append(("uncertainty_cap", {"soc": 0.9, "unc": 0.45},
               [_ss(1800.0, 4.0, 13.0)] * 3 + [_ss(1800.0, -3.0, 13.3)] * 2))
    return sc


# =============================================================================
# Cenários de segurança
# =============================================================================
_NOM = {"v_bat": 13.0, "soc": 0.5, "soc_valid": True, "v_bus": 13.0, "i_bus": 1.0, "t_bat": 25.0}


def _seq(segments: list[tuple[int, float, dict[str, Any]]]) -> list[Step]:
    """Monta passos a partir de segmentos ``(n, dt, overrides)`` com ``t_s`` acumulado."""
    steps: list[Step] = []
    t = 0.0
    for n, dt, ov in segments:
        for _ in range(n):
            t += dt
            st: Step = {"t_s": t, "dt_s": dt, **_NOM, **ov}
            steps.append(st)
    return steps


def safety_scenarios() -> list[tuple[str, list[Step]]]:
    sc: list[tuple[str, list[Step]]] = []
    sc.append(("lvd_voltage_delay_and_reconnect", _seq([
        (2, 0.5, {}),
        (6, 0.5, {"v_bat": 11.5}),            # 3 s abaixo do limiar
        (1, 0.5, {"v_bat": 11.7}),            # interrompe -> zera
        (10, 0.5, {"v_bat": 11.5}),           # 5 s -> LVD no 10º passo
        (2, 0.5, {"v_bat": 12.5}),            # abaixo do religamento
        (2, 0.5, {"v_bat": 12.95, "soc": 0.2}),  # tensão ok, SOC < reconnect_min_soc
        (2, 0.5, {"v_bat": 12.95, "soc": 0.3}),  # religa
    ])))
    sc.append(("lvd_soc_hard_min", _seq([
        (1, 0.25, {}),
        (2, 0.25, {"soc": 0.09}),             # LVD imediato por SOC
        (2, 0.25, {"soc": 0.20}),
        (2, 0.25, {"soc": 0.30, "v_bat": 12.8}),  # SOC ok mas tensão < reconnect_v
        (2, 0.25, {"soc": 0.30}),             # religa
        (2, 0.25, {"soc": 0.05, "soc_valid": False}),  # SOC inválido não dispara
        (12, 0.5, {"soc": 0.05, "soc_valid": False, "v_bat": 11.0}),  # mas a tensão dispara
        (2, 0.5, {"soc": 0.05, "soc_valid": False, "v_bat": 13.0}),  # religa: !soc_valid
    ])))
    sc.append(("lvd_bus_substitute", _seq([
        (2, 0.5, {"v_bat": None}),
        (11, 0.5, {"v_bat": None, "v_bus": 11.5}),
        (2, 0.5, {"v_bat": None, "v_bus": 13.0}),
    ])))
    sc.append(("ovp_trip_release", _seq([
        (2, 0.25, {}),
        (3, 0.25, {"v_bus": 15.2}),
        (1, 0.25, {"v_bus": 14.9}),           # interrompe
        (5, 0.25, {"v_bus": 15.2}),           # 1 s -> OVP
        (4, 0.25, {"v_bus": 14.8}),           # acima de bus_ovp_v - 0,3: mantém
        (3, 0.25, {"v_bus": 14.5}),
        (1, 0.25, {"v_bus": 15.1}),           # interrompe a liberação
        (5, 0.25, {"v_bus": 14.5}),           # libera após 1 s
        (2, 0.25, {"v_bus": None}),           # barramento inválido não dispara
    ])))
    oc_hi = {"i_bus": 5.0}
    oc_off = {"i_bus": 0.0}
    sc.append(("oc_retry_and_lockout", _seq([
        (2, 0.125, {}),
        (1, 0.125, oc_hi), (1, 0.125, {}),    # pulso curto: não dispara
        (2, 0.125, oc_hi),                    # 0,25 s >= 0,2 s -> OC (1)
        (6, 5.0, oc_off),                     # 30 s -> nova tentativa
        (2, 0.125, oc_hi),                    # OC (2)
        (6, 5.0, oc_off),
        (2, 0.125, oc_hi),                    # 3º disparo sem 10 min livres -> OC_LOCKOUT
        (10, 100.0, oc_off),                  # bloqueio persiste
    ])))
    sc.append(("oc_free_window_resets_count", _seq([
        (2, 0.125, oc_hi), (6, 5.0, oc_off),  # disparo 1 e nova tentativa
        (2, 0.125, oc_hi), (6, 5.0, oc_off),  # disparo 2 e nova tentativa
        (7, 100.0, {}),                       # > 10 min livres -> contagem zerada
        (2, 0.125, oc_hi), (6, 5.0, oc_off),  # disparo (1) e nova tentativa
        (2, 0.125, oc_hi), (6, 5.0, oc_off),  # disparo (2)
        (2, 0.125, oc_hi),                    # disparo (3) -> OC_LOCKOUT
        (1, 1.0, oc_off),
    ])))
    lock = _seq([
        (2, 0.125, oc_hi), (6, 5.0, oc_off), (2, 0.125, oc_hi), (6, 5.0, oc_off), (2, 0.125, oc_hi),
        (2, 1.0, oc_off),
        (1, 1.0, oc_off),                     # reset_faults
        (2, 1.0, {}),
        (2, 0.125, oc_hi),                    # após reset: OC simples (contagem zerada)
    ])
    lock[-5]["reset_faults"] = True
    sc.append(("oc_lockout_reset_faults", lock))
    sc.append(("ot_trip_release", _seq([
        (1, 1.0, {}),
        (1, 1.0, {"t_bat": 55.0}),            # não é > 55
        (2, 1.0, {"t_bat": 56.0}),
        (2, 1.0, {"t_bat": 50.0}),
        (2, 1.0, {"t_bat": None}),            # sensor de temperatura inválido: mantém
        (1, 1.0, {"t_bat": 45.0}),            # não é < 45
        (2, 1.0, {"t_bat": 44.5}),
    ])))
    sc.append(("sensor_both_invalid", _seq([
        (1, 1.0, {}),
        (5, 1.0, {"v_bat": None, "v_bus": None}),   # > 3 s -> SENSOR
        (1, 1.0, {"v_bat": None, "v_bus": 13.0}),   # um volta -> libera
        (2, 0.5, {"v_bat": None, "v_bus": None}),
        (1, 0.5, {}),
    ])))
    sc.append(("priority", _seq([
        (1, 0.5, {}),
        (10, 0.5, {"v_bat": 11.0}),                                   # LVD
        (2, 0.5, {"v_bat": 11.0, "t_bat": 60.0}),                     # OT > LVD
        (4, 0.25, {"v_bat": 11.0, "t_bat": 60.0, "v_bus": 15.5}),     # OVP > OT
        (2, 0.125, {"v_bat": 11.0, "t_bat": 60.0, "v_bus": 15.5, "i_bus": 6.0}),  # OC > OVP
        (2, 1.0, {"v_bat": 11.0, "t_bat": 60.0, "v_bus": 14.0, "i_bus": 0.0}),    # OVP liberando
        (2, 1.0, {"v_bat": 11.0, "t_bat": 40.0, "v_bus": 14.0, "i_bus": 0.0}),
        (6, 5.0, {"v_bat": 11.0, "v_bus": 14.0, "i_bus": 0.0}),       # OC retry -> resta LVD
        (5, 1.0, {"v_bat": None, "v_bus": None}),                     # SENSOR > LVD
        (2, 1.0, {"v_bat": 13.0, "v_bus": 13.0}),                     # libera tudo
    ])))
    return sc


# =============================================================================
# Montagem / verificação
# =============================================================================
def build_vectors(cfg: SystemConfig) -> dict[str, Any]:
    """Gera o documento ``sem.vectors.v1`` completo."""
    rules = []
    for name, steps in rules_scenarios():
        exp = run_rules(cfg, steps)
        rules.append({"name": name, "steps": [{**s, "expect": e} for s, e in zip(steps, exp)]})
    soc = []
    for name, init, steps in soc_scenarios():
        exp = run_soc(cfg, init, steps)
        soc.append({"name": name, "init": init, "steps": [{**s, "expect": e} for s, e in zip(steps, exp)]})
    safety = []
    for name, steps in safety_scenarios():
        exp = run_safety(cfg, steps)
        safety.append({"name": name, "steps": [{**s, "expect": e} for s, e in zip(steps, exp)]})
    return {
        "schema": SCHEMA,
        "generator": "python -m smartenergy gen-vectors (smartenergy.experiments.vectors)",
        "source": "synthetic",
        "notes": [
            "Vetores sinteticos gerados pelas implementacoes Python (docs/06 §7); nao sao medicoes.",
            "Harness: ver docstring de smartenergy/experiments/vectors.py.",
            "Tolerancias sugeridas: duty 1e-6; soc/unc 1e-9 (double).",
            "'events' nas saidas esperadas e informativo (codigos docs/05 §6).",
        ],
        "params": build_params(cfg),
        "rules": rules,
        "soc": soc,
        "safety": safety,
    }


def verify_vectors(doc: dict[str, Any], cfg: SystemConfig, tol: float = 1e-9) -> list[str]:
    """Reexecuta os vetores com o harness e devolve a lista de divergências."""
    errors: list[str] = []

    def cmp(where: str, got: Any, exp: Any) -> None:
        if isinstance(exp, float) and isinstance(got, (int, float)):
            if not math.isclose(got, exp, rel_tol=0.0, abs_tol=tol):
                errors.append(f"{where}: {got!r} != {exp!r}")
        elif got != exp:
            errors.append(f"{where}: {got!r} != {exp!r}")

    def check(kind: str, scenarios: list[dict[str, Any]], runner: Callable[[dict[str, Any]], list[dict]]) -> None:
        for sc in scenarios:
            exp = [s["expect"] for s in sc["steps"]]
            got = runner(sc)
            for i, (g, e) in enumerate(zip(got, exp)):
                for k, ev in e.items():
                    if k == "loads":
                        for lk, lv in ev.items():
                            cmp(f"{kind}/{sc['name']}[{i}].{lk}.on", g[k][lk]["on"], lv["on"])
                            cmp(f"{kind}/{sc['name']}[{i}].{lk}.duty", g[k][lk]["duty"], lv["duty"])
                    else:
                        cmp(f"{kind}/{sc['name']}[{i}].{k}", g[k], ev)

    def strip(steps: list[dict[str, Any]]) -> list[Step]:
        return [{k: v for k, v in s.items() if k != "expect"} for s in steps]

    check("rules", doc["rules"], lambda sc: run_rules(cfg, strip(sc["steps"])))
    check("soc", doc["soc"], lambda sc: run_soc(cfg, sc["init"], strip(sc["steps"])))
    check("safety", doc["safety"], lambda sc: run_safety(cfg, strip(sc["steps"])))
    return errors


def dumps(doc: dict[str, Any]) -> str:
    """JSON determinístico: um passo por linha (diffs legíveis)."""
    lines = ["{"]
    head = {k: doc[k] for k in ("schema", "generator", "source", "notes", "params")}
    for k, v in head.items():
        lines.append(f"{json.dumps(k)}: {json.dumps(v, ensure_ascii=False)},")
    for i, mod in enumerate(("rules", "soc", "safety")):
        lines.append(f"{json.dumps(mod)}: [")
        for j, sc in enumerate(doc[mod]):
            meta = {k: v for k, v in sc.items() if k != "steps"}
            meta_s = json.dumps(meta, ensure_ascii=False)[1:-1]
            lines.append("{" + meta_s + ', "steps": [')
            steps = [json.dumps(s, ensure_ascii=False) for s in sc["steps"]]
            lines.append(",\n".join(steps))
            lines.append("]}" + ("," if j < len(doc[mod]) - 1 else ""))
        lines.append("]" + ("," if i < 2 else ""))
    lines.append("}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m smartenergy gen-vectors",
                                 description="Gera vetores golden sem.vectors.v1 (regras, SOC, segurança).")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="arquivo de saída (JSON)")
    ap.add_argument("--config", default=None, help="config/system.yaml alternativo")
    ap.add_argument("--check", action="store_true", help="apenas verifica se o arquivo existente está atualizado")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    doc = build_vectors(cfg)
    text = dumps(doc)
    out = Path(args.out)
    if args.check:
        ok = out.exists() and out.read_text(encoding="utf-8") == text
        print(f"{out}: {'atualizado' if ok else 'DESATUALIZADO (rode gen-vectors)'}")
        return 0 if ok else 1
    errors = verify_vectors(json.loads(text), cfg)
    if errors:  # pragma: no cover - indicaria não determinismo
        print("\n".join(errors[:20]))
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    n_r = sum(len(s["steps"]) for s in doc["rules"])
    n_s = sum(len(s["steps"]) for s in doc["soc"])
    n_f = sum(len(s["steps"]) for s in doc["safety"])
    print(f"{out}: {len(doc['rules'])} cenários de regras ({n_r} passos), "
          f"{len(doc['soc'])} de SOC ({n_s}), {len(doc['safety'])} de segurança ({n_f})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
