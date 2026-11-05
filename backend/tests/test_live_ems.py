"""Testes do serviço do EMS preditivo ao vivo (conversão de telemetria e publicação de comandos)."""

from __future__ import annotations

import json
import math
from typing import Any

import pytest

from smartenergy.config import load_config
from smartenergy.ems.base import Decision, EmsInputs, LoadCommand, LoadState
from smartenergy.live.ems_service import LiveEmsService, Published, telemetry_to_inputs
from smartenergy.security.hmac_cmd import verify_envelope
from smartenergy.security.schemas import schema_error
from smartenergy.storage.synthetic import synthetic_telemetry

T0 = 1763290800000          # 2025-11-16 11:00 UTC = 08:00 local (UTC-3)
SLOT_MS = 900 * 1000        # ems.predictive.replan_s
KEY = b"chave-de-teste-com-32-bytes-ok!!"
CFG = load_config()


class FakeStrategy:
    name = "predictive"

    def __init__(self, fallback: bool = False) -> None:
        self.fallback = fallback
        self.calls: list[tuple[EmsInputs, Any]] = []
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1

    def decide(self, inp: EmsInputs, forecast: Any = None) -> Decision:
        self.calls.append((inp, forecast))
        cmds = {"led": LoadCommand(1, "led", True, 1.0), "fan": LoadCommand(2, "fan", True, 0.6),
                "res": LoadCommand(3, "res", False, 0.0)}
        plan = None if self.fallback else {"step_s": 900, "t0_ms": inp.ts_ms - inp.ts_ms % SLOT_MS,
                                           "soc": [0.7, 0.71], "pv_fc_w": [20.0], "loads": {}, "status": "Optimal"}
        return Decision(mode="predictive", state="NORMAL", commands=cmds, reason="mpc: passo 0",
                        fallback=self.fallback, plan=plan)


class FakeForecast:
    method = "fake"

    def __init__(self) -> None:
        self.obs: list[tuple[int, float]] = []

    def observe(self, ts_ms: int, p_pv_w: float, p_pv_avail_w: float | None = None) -> None:
        self.obs.append((ts_ms, p_pv_w))

    def pv_forecast(self, t0_ms: int, step_s: int, n: int) -> list[float]:
        return [30.0 if 6 <= ((t0_ms // 1000 + k * step_s) // 3600 - 3) % 24 <= 17 else 0.0 for k in range(n)]

    def demand_forecast(self, t0_ms: int, step_s: int, n: int) -> dict[str, list[float]]:
        return {"led": [0.3] * n, "fan": [0.3] * n}


def _service(strategy: Any = None, **kw: Any) -> tuple[LiveEmsService, list[tuple[str, str, int]], FakeForecast]:
    sent: list[tuple[str, str, int]] = []
    fc = FakeForecast()
    svc = LiveEmsService(CFG, KEY, lambda t, p, q: sent.append((t, p, q)), strategy=strategy or FakeStrategy(),
                         forecast=fc, clock=lambda: (T0 + 1000) / 1000.0, **kw)
    return svc, sent, fc


# --------------------------------------------------------------------------- telemetry_to_inputs
def test_telemetry_to_inputs_mapping() -> None:
    p = synthetic_telemetry(T0, 10, soc=0.66, pv_p=25.0, bus_p=9.0, demand=(True, False, False))
    inp = telemetry_to_inputs(p, CFG)
    assert inp.ts_ms == T0 and inp.t_s == pytest.approx(50.0)
    assert inp.tod_s == pytest.approx(8 * 3600)
    assert inp.soc == 0.66 and inp.soc_valid is True and inp.soc_unc == 0.05
    assert inp.p_pv == 25.0 and inp.p_bus == 9.0 and inp.v_bat == 13.2
    assert inp.p_bat == pytest.approx(p["meas"]["bat"]["p"]) and inp.t_bat_c == 28.0
    assert inp.demand == {"led": True, "fan": False}
    assert inp.deferrable_done_wh == {"res": 12.5}
    assert inp.outputs["led"].on is True and inp.outputs["fan"].duty == 0.6 and inp.outputs["res"].on is False
    assert all(math.isinf(o.last_switch_t_s) for o in inp.outputs.values())


def test_telemetry_to_inputs_switch_tracking_and_invalid_values() -> None:
    prev = {"led": LoadState(1, "led", on=True, duty=1.0, last_switch_t_s=3.0),
            "fan": LoadState(2, "fan", on=False, duty=0.0, last_switch_t_s=4.0),
            "res": LoadState(3, "res", on=False, duty=0.0, last_switch_t_s=-math.inf)}
    p = synthetic_telemetry(T0, 20, loads_on=(True, True, False))
    inp = telemetry_to_inputs(p, CFG, prev)
    assert inp.outputs["led"].last_switch_t_s == 3.0          # sem mudança: mantém
    assert inp.outputs["fan"].last_switch_t_s == pytest.approx(100.0)  # ligou: agora (uptime)
    assert math.isinf(inp.outputs["res"].last_switch_t_s)
    q = synthetic_telemetry(T0, 1, time_synced=False)
    q["soc"]["value"] = None
    q["meas"]["bat"] = {"v": None, "i": None, "p": None, "ok": False}
    del q["loads"][0]["demand"]
    inp2 = telemetry_to_inputs(q, CFG)
    assert inp2.ts_ms == 0 and inp2.tod_s == 0.0 and inp2.soc_valid is False and inp2.soc == 0.0
    assert inp2.v_bat == 0.0 and inp2.p_bat == 0.0
    assert inp2.demand["led"] is False  # sem hora e sem campo demand: não inventa pedido


# --------------------------------------------------------------------------- serviço
def test_no_commands_unless_device_in_predictive_mode() -> None:
    strat = FakeStrategy()
    svc, sent, fc = _service(strat)
    for i in range(3):
        assert svc.handle_telemetry(synthetic_telemetry(T0 + i * 5000, i, mode="rules")) == []
    assert sent == [] and strat.calls == [] and len(fc.obs) == 3  # previsão é alimentada mesmo assim
    assert svc.stats["skipped_mode"] == 3


def test_predictive_publishes_signed_set_loads_and_decision_once_per_slot() -> None:
    strat = FakeStrategy()
    svc, sent, _fc = _service(strat)
    out = svc.handle_telemetry(synthetic_telemetry(T0, 0, mode="predictive", source="simulation"))
    assert strat.resets == 1 and len(strat.calls) == 1
    assert [p.topic for p in out] == ["smartenergy/cmd", "smartenergy/ems/decision"]
    assert all(isinstance(p, Published) and p.qos == 1 for p in out) and len(sent) == 2
    cmd = verify_envelope(out[0].payload, KEY)
    assert cmd["type"] == "set_loads" and cmd["ttl_s"] == 1200 and cmd["ts_ms"] == T0 + 1000
    assert cmd["args"]["loads"] == [{"id": 1, "on": True, "duty": 1.0}, {"id": 2, "on": True, "duty": 0.6},
                                    {"id": 3, "on": False, "duty": 0.0}]
    dec = json.loads(out[1].payload)
    assert schema_error("decision", dec) is None
    assert dec["origin"] == "backend" and dec["source"] == "simulation" and dec["mode"] == "predictive"
    assert dec["plan"]["forecast_method"] == CFG.raw["forecast"]["pv_method"] and dec["prev_state"] is None
    # mesma janela de replanejamento: nada novo
    assert svc.handle_telemetry(synthetic_telemetry(T0 + 5000, 1, mode="predictive")) == []
    # próxima janela: novo plano e novo comando
    nxt = T0 - T0 % SLOT_MS + SLOT_MS
    out2 = svc.handle_telemetry(synthetic_telemetry(nxt, 2, mode="predictive", source="hardware"))
    assert len(out2) == 2 and len(strat.calls) == 2
    dec2 = json.loads(out2[1].payload)
    assert dec2["source"] == "hardware" and dec2["prev_state"] == "NORMAL"  # origem preservada


def test_fallback_sends_no_command() -> None:
    svc, sent, _ = _service(FakeStrategy(fallback=True))
    out = svc.handle_telemetry(synthetic_telemetry(T0, 0, mode="predictive"))
    assert [p.topic for p in out] == ["smartenergy/ems/decision"] and len(sent) == 1
    dec = json.loads(out[0].payload)
    assert dec["actions"] == [] and dec["reason"].startswith("sem comando enviado") and dec["plan"] is None
    assert svc.stats["fallback"] == 1 and svc.stats["commands"] == 0


def test_invalid_unsynced_other_device_and_reentry() -> None:
    strat = FakeStrategy()
    svc, sent, _ = _service(strat, device_id="sem-esp32-01")
    bad = synthetic_telemetry(T0, 0, mode="predictive")
    bad["soc"]["value"] = 2.0
    assert svc.handle_telemetry(bad) == [] and svc.stats["invalid"] == 1
    assert svc.handle_telemetry(synthetic_telemetry(T0, 0, mode="predictive", device_id="outro")) == []
    assert svc.handle_telemetry(synthetic_telemetry(T0, 0, mode="predictive", time_synced=False)) == []
    assert svc.stats["skipped_no_time"] == 1 and sent == []
    assert len(svc.handle_telemetry(synthetic_telemetry(T0, 1, mode="predictive"))) == 2
    svc.handle_telemetry(synthetic_telemetry(T0 + 5000, 2, mode="rules"))
    # volta ao preditivo na mesma janela: reinicia a estratégia e replaneja imediatamente
    assert len(svc.handle_telemetry(synthetic_telemetry(T0 + 10000, 3, mode="predictive"))) == 2
    assert strat.resets == 2


def test_with_real_predictive_strategy() -> None:
    """Integração com ems.predictive (MILP/HiGHS) usando previsão falsa determinística."""
    from smartenergy.ems.registry import get_strategy

    strat = get_strategy("predictive", CFG, time_limit_s=10)
    svc, _sent, _ = _service(strat)
    out = svc.handle_telemetry(synthetic_telemetry(T0, 0, mode="predictive", soc=0.7))
    dec = json.loads(out[-1].payload)
    assert schema_error("decision", dec) is None and dec["origin"] == "backend"
    if dec["plan"] is None:  # MPC sem solução: nenhum comando, decisão explica
        pytest.fail(f"MPC caiu para regras: {dec['reason']}")
    cmd = verify_envelope(out[0].payload, KEY)
    assert cmd["type"] == "set_loads" and len(cmd["args"]["loads"]) == 3
    assert dec["plan"]["status"] and len(dec["plan"]["pv_fc_w"]) == CFG.raw["ems"]["predictive"]["horizon_steps"]


def test_set_loads_args_normalized_to_firmware_rules() -> None:
    from smartenergy.live.ems_service import set_loads_args

    dec = Decision(mode="predictive", state="NORMAL", reason="x", commands={
        "res": LoadCommand(3, "res", False, 0.7), "led": LoadCommand(1, "led", True, 0.3),
        "fan": LoadCommand(2, "fan", True, 0.1)})
    args = set_loads_args(dec, CFG)
    assert args["loads"] == [{"id": 1, "on": True, "duty": 1.0}, {"id": 2, "on": True, "duty": 0.4},
                             {"id": 3, "on": False, "duty": 0.0}]
    assert schema_error("command", {"schema": "sem.cmd.v1", "id": "c", "ts_ms": 1, "ttl_s": 10,
                                    "type": "set_loads", "args": args}) is None
