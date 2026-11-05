"""Serviço do EMS preditivo ao vivo (MPC no backend, hardware ou gêmeo digital).

Fluxo (docs/05 §5/§7 e docs/06 §3.3–3.4):

1. assina ``<prefixo>/telemetry`` e converte cada pacote em :class:`EmsInputs`
   (:func:`telemetry_to_inputs`);
2. alimenta o :class:`ForecastProvider` (``smartenergy.forecast.factory.make_forecast``,
   import tardio) com a potência FV medida;
3. **somente se o dispositivo reportar** ``ems.mode == "predictive"``, a cada
   ``ems.predictive.replan_s`` (alinhado à grade), chama
   ``get_strategy("predictive", cfg).decide(...)``;
4. se o MPC produziu plano válido, assina e publica ``set_loads`` (TTL
   ``command_ttl_s``) em ``<prefixo>/cmd``; sempre publica a decisão
   ``sem.decision.v1`` (``origin="backend"``) em ``<prefixo>/ems/decision``.

Se o MPC falhar (fallback), NENHUM comando é enviado: o dispositivo aplica as
salvaguardas e volta às regras locais quando o TTL do último comando expira.
A origem (``source``) da decisão é a da telemetria recebida — dados do gêmeo
digital continuam ``simulation``.
"""

from __future__ import annotations

import json
import logging
import math
import queue
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from smartenergy.config import SystemConfig
from smartenergy.ems.base import Decision, EmsInputs, ForecastProvider, LoadState, Strategy
from smartenergy.ingest.mqtt_conn import MqttSettings, Topics, make_client
from smartenergy.security.hmac_cmd import build_command, envelope_to_json, sign_command
from smartenergy.security.schemas import schema_error
from smartenergy.timeutil import demand_at, tod_from_epoch_ms

log = logging.getLogger("smartenergy.live")

PublishFn = Callable[[str, str, int], None]


@dataclass(frozen=True)
class Published:
    """Mensagem publicada pelo serviço (registro para testes/diagnóstico)."""

    topic: str
    payload: str
    qos: int


def _f(x: Any, default: float = 0.0) -> float:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(float(x)):
        return default
    return float(x)


def _opt(x: Any) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(float(x)):
        return None
    return float(x)


def telemetry_to_inputs(
    payload: Mapping[str, Any],
    cfg: SystemConfig,
    prev_outputs: Mapping[str, LoadState] | None = None,
) -> EmsInputs:
    """Converte ``sem.telemetry.v1`` em :class:`EmsInputs` (somente medições/estimativas).

    * ``t_s`` = ``uptime_ms/1000`` (tempo monotônico do dispositivo, para permanência mínima);
    * ``ts_ms`` = ``ts_ms`` se ``time_synced``, senão 0 (o MPC então cai para regras);
    * ``tod_s`` = hora local de ``ts_ms`` (0 sem hora sincronizada);
    * medições com ``ok=false`` viram 0 (o SOC inválido é sinalizado por ``soc_valid``);
    * ``demand`` vem de ``loads[].demand`` (pedido reportado pelo dispositivo); se a carga
      não constar da telemetria, usa o perfil programado (``demand.schedule``);
    * ``outputs[k].last_switch_t_s`` é mantido a partir de ``prev_outputs``: muda para ``t_s``
      quando o estado on/off observado difere do anterior; sem histórico, ``-inf``.
    """
    synced = bool(payload.get("time_synced")) and int(payload.get("ts_ms") or 0) > 0
    ts_ms = int(payload["ts_ms"]) if synced else 0
    t_s = _f(payload.get("uptime_ms")) / 1000.0
    tod_s = tod_from_epoch_ms(ts_ms, cfg) if synced else 0.0
    meas = payload.get("meas") or {}

    def vip(point: str) -> tuple[float, float, float]:
        m = meas.get(point) or {}
        if m.get("ok") is False:
            return 0.0, 0.0, 0.0
        v, i = _f(m.get("v")), _f(m.get("i"))
        p = _opt(m.get("p"))
        return v, i, (p if p is not None else v * i)

    v_bat, i_bat, p_bat = vip("bat")
    _, _, p_pv = vip("pv")
    _, _, p_bus = vip("bus")
    soc_blk = payload.get("soc") or {}
    soc_val = _opt(soc_blk.get("value"))
    soc_valid = bool(soc_blk.get("valid")) and soc_val is not None
    t_bat_blk = meas.get("t_bat") or {}
    t_bat = _opt(t_bat_blk.get("t")) if t_bat_blk.get("ok") is not False else None

    by_id = {int(x["id"]): x for x in payload.get("loads") or [] if isinstance(x, Mapping) and "id" in x}
    prev = dict(prev_outputs or {})
    demand: dict[str, bool] = {}
    done: dict[str, float] = {}
    outputs: dict[str, LoadState] = {}
    for ld in cfg.loads:
        tl = by_id.get(ld.id) or {}
        if ld.deferrable is None:
            if "demand" in tl:
                demand[ld.key] = bool(tl["demand"])
            else:
                demand[ld.key] = bool(synced and demand_at(cfg, ld.key, tod_s))
        else:
            dfr = tl.get("deferrable") or {}
            done[ld.key] = _f(dfr.get("e_done_wh"))
        on = bool(tl.get("on", False))
        duty = _f(tl.get("duty")) if on else 0.0
        p = prev.get(ld.key)
        if p is None:
            last_sw = -math.inf
        elif bool(p.on) != on:
            last_sw = t_s
        else:
            last_sw = p.last_switch_t_s
        outputs[ld.key] = LoadState(id=ld.id, key=ld.key, on=on, duty=duty, last_switch_t_s=last_sw)

    return EmsInputs(
        t_s=t_s, ts_ms=ts_ms, tod_s=tod_s,
        soc=min(max(soc_val, 0.0), 1.0) if soc_val is not None else 0.0, soc_valid=soc_valid,
        v_bat=v_bat, i_bat=i_bat, p_bat=p_bat, p_pv=p_pv, p_bus=p_bus,
        demand=demand, deferrable_done_wh=done, outputs=outputs,
        soc_unc=_f(soc_blk.get("unc")), t_bat_c=t_bat,
    )


def set_loads_args(dec: Decision) -> dict[str, Any]:
    """Argumentos ``set_loads`` (todas as cargas, ordenadas por id) a partir da decisão."""
    loads = []
    for c in sorted(dec.commands.values(), key=lambda c: c.id):
        on = bool(c.on)
        loads.append({"id": int(c.id), "on": on, "duty": round(min(max(float(c.duty), 0.0), 1.0), 4) if on else 0.0})
    return {"loads": loads}


def decision_payload(
    dec: Decision,
    inp: EmsInputs,
    *,
    source: str,
    safety: str,
    prev_state: str | None,
    command_sent: bool,
    forecast_method: str | None = None,
) -> dict[str, Any]:
    """Monta ``sem.decision.v1`` com ``origin="backend"`` (docs/05 §5)."""
    actions = set_loads_args(dec)["loads"] if command_sent else []
    reason = dec.reason if command_sent else f"sem comando enviado (dispositivo segue regras locais): {dec.reason}"
    plan = dict(dec.plan) if isinstance(dec.plan, Mapping) else None
    if plan is not None and forecast_method:
        plan.setdefault("forecast_method", forecast_method)
    return {
        "schema": "sem.decision.v1",
        "ts_ms": int(inp.ts_ms),
        "source": source,
        "origin": "backend",
        "mode": dec.mode,
        "state": dec.state,
        "prev_state": prev_state,
        "safety": safety,
        "reason": reason[:500],
        "actions": actions,
        "inputs": {"soc": round(inp.soc, 4), "soc_valid": inp.soc_valid, "v_bat": round(inp.v_bat, 3),
                   "p_pv": round(inp.p_pv, 3), "p_bat": round(inp.p_bat, 3), "p_bus": round(inp.p_bus, 3)},
        "plan": plan,
    }


class LiveEmsService:
    """Lógica do serviço (independente do transporte; ``publish(topic, payload, qos)`` injetável)."""

    def __init__(
        self,
        cfg: SystemConfig,
        key: bytes,
        publish: PublishFn,
        *,
        strategy: Strategy | None = None,
        forecast: ForecastProvider | None = None,
        forecast_method: str | None = None,
        device_id: str | None = None,
        topics: Topics | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        mp = cfg.raw["ems"]["predictive"]
        self.cfg = cfg
        self.key = key
        self.publish = publish
        self.replan_s = int(mp["replan_s"])
        self.ttl_s = int(mp["command_ttl_s"])
        self.forecast_method = forecast_method or str(cfg.raw["forecast"]["pv_method"])
        self.device_id = device_id
        self.topics = topics or Topics.from_config(cfg)
        self.clock = clock
        self._strategy = strategy
        self._forecast = forecast
        self._forecast_error: str | None = None
        self._outputs: dict[str, LoadState] = {}
        self._boot: tuple[Any, Any] | None = None
        self._active = False
        self._last_slot: int | None = None
        self._prev_state: str | None = None
        self.stats: Counter[str] = Counter()

    # dependências de outros pacotes (import tardio) ---------------------------
    @property
    def strategy(self) -> Strategy:
        if self._strategy is None:
            from smartenergy.ems.registry import get_strategy

            self._strategy = get_strategy("predictive", self.cfg, forecast_method=self.forecast_method)
        return self._strategy

    @property
    def forecast(self) -> ForecastProvider | None:
        if self._forecast is None and self._forecast_error is None:
            try:
                from smartenergy.forecast.factory import make_forecast

                self._forecast = make_forecast(self.forecast_method, self.cfg)
            except Exception as exc:  # módulo ausente, método só de simulação, etc.
                self._forecast_error = str(exc)
                log.error("previsão %s indisponível: %s (o MPC cairá para regras)", self.forecast_method, exc)
        return self._forecast

    # processamento -------------------------------------------------------------
    def handle_telemetry(self, payload: Mapping[str, Any]) -> list[Published]:
        """Processa um pacote de telemetria; devolve o que foi publicado."""
        self.stats["telemetry"] += 1
        err = schema_error("telemetry", payload)
        if err is not None:
            self.stats["invalid"] += 1
            log.warning("telemetria inválida ignorada: %s", err)
            return []
        if self.device_id and payload.get("device_id") != self.device_id:
            self.stats["other_device"] += 1
            return []
        boot = (payload.get("device_id"), payload.get("boot_id"))
        if boot != self._boot:  # reinício do dispositivo: uptime zerou
            self._boot = boot
            self._outputs = {}
            self._last_slot = None
        inp = telemetry_to_inputs(payload, self.cfg, self._outputs)
        self._outputs = inp.outputs

        fc = self.forecast
        if fc is not None and inp.ts_ms > 0:
            try:
                fc.observe(inp.ts_ms, inp.p_pv)
            except Exception as exc:  # pragma: no cover - provedor de outro pacote
                log.warning("forecast.observe falhou: %s", exc)

        if (payload.get("ems") or {}).get("mode") != "predictive":
            if self._active:
                log.info("dispositivo saiu do modo preditivo; comandos suspensos")
            self._active = False
            self.stats["skipped_mode"] += 1
            return []
        if not self._active:
            log.info("dispositivo em modo preditivo; MPC ativo (replan %d s, TTL %d s)", self.replan_s, self.ttl_s)
            self._active = True
            self._last_slot = None
            self.strategy.reset()
        if inp.ts_ms <= 0:
            self.stats["skipped_no_time"] += 1
            return []
        slot = inp.ts_ms // (self.replan_s * 1000)
        if slot == self._last_slot:
            return []
        self._last_slot = slot
        return self._decide_and_publish(payload, inp, fc)

    def _decide_and_publish(self, payload: Mapping[str, Any], inp: EmsInputs,
                            fc: ForecastProvider | None) -> list[Published]:
        try:
            dec = self.strategy.decide(inp, fc)
        except Exception as exc:
            self.stats["errors"] += 1
            log.exception("falha na decisão do MPC: %s", exc)
            return []
        out: list[Published] = []
        send = not dec.fallback
        if send:
            cmd = build_command("set_loads", set_loads_args(dec), self.ttl_s, ts_ms=int(self.clock() * 1000))
            out.append(Published(self.topics.cmd, envelope_to_json(sign_command(cmd, self.key)), 1))
            self.stats["commands"] += 1
        else:
            self.stats["fallback"] += 1
            log.warning("MPC em fallback; nenhum comando enviado: %s", dec.reason)
        doc = decision_payload(dec, inp, source=str(payload["source"]), safety=str(payload["ems"]["safety"]),
                               prev_state=self._prev_state, command_sent=send,
                               forecast_method=self.forecast_method)
        self._prev_state = dec.state
        err = schema_error("decision", doc)
        if err is None:
            out.append(Published(self.topics.decision, json.dumps(doc, separators=(",", ":"), default=float), 1))
        else:  # pragma: no cover - indica erro de programação
            self.stats["errors"] += 1
            log.error("decisão fora do esquema (não publicada): %s", err)
        for p in out:
            self.publish(p.topic, p.payload, p.qos)
        return out


class LiveEmsRunner:
    """Transporte MQTT do :class:`LiveEmsService` (worker separado do laço de rede)."""

    def __init__(self, service_factory: Callable[[PublishFn], LiveEmsService], settings: MqttSettings,
                 topics: Topics, dry_run: bool = False) -> None:
        self.settings = settings
        self.topics = topics
        self.dry_run = dry_run
        self._client = make_client(settings, "live-ems")
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=200)
        self.service = service_factory(self._publish)
        self._stop = threading.Event()

    def _publish(self, topic: str, payload: str, qos: int) -> None:
        if self.dry_run:
            log.info("[dry-run] %s %s", topic, payload[:300])
            return
        self._client.publish(topic, payload, qos=qos)

    def _on_connect(self, client: Any, _u: Any, _f: Any, reason_code: Any, _p: Any = None) -> None:
        if getattr(reason_code, "is_failure", False):
            log.error("conexão MQTT recusada: %s", reason_code)
            return
        client.subscribe(self.topics.telemetry, qos=0)
        log.info("live-ems conectado; assinando %s", self.topics.telemetry)

    def _on_message(self, _c: Any, _u: Any, msg: Any) -> None:
        try:
            doc = json.loads(msg.payload)
        except ValueError:
            log.warning("telemetria não-JSON ignorada")
            return
        try:
            self._queue.put_nowait(doc)
        except queue.Full:
            log.warning("fila cheia; telemetria descartada")

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                doc = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.service.handle_telemetry(doc)
            except Exception:  # nunca derruba o serviço
                log.exception("erro ao processar telemetria")

    def run_forever(self) -> None:
        threading.Thread(target=self._worker, daemon=True, name="live-ems-worker").start()
        self._client.connect_async(self.settings.host, self.settings.port, keepalive=self.settings.keepalive_s)
        try:
            self._client.loop_forever(retry_first_connection=True)
        finally:
            self._stop.set()
