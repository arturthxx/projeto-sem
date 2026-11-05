"""Fontes de dados da API: armazenamento real (SQLite) ou pacote de demonstração.

Ambas expõem a mesma interface usada por :mod:`smartenergy.api.app`. A fonte de
demonstração (``dashboard/data/demo.json``) é SEMPRE rotulada
``source="simulation"``, qualquer que seja o conteúdo do arquivo.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

from smartenergy.config import SystemConfig
from smartenergy.storage.columns import series_columns
from smartenergy.storage.resample import resample_columns
from smartenergy.storage.sqlite_store import KINDS, SqliteStore, format_csv
from smartenergy.timeutil import demand_at, tod_from_epoch_ms

DEMO_SOURCE = "simulation"


def sse_message(event: str, data: Any, event_id: str | None = None) -> str:
    """Formata uma mensagem Server-Sent Events (uma linha ``data:`` com JSON compacto)."""
    body = json.dumps(data, separators=(",", ":"), ensure_ascii=False, default=float)
    head = f"id: {event_id}\n" if event_id else ""
    return f"{head}event: {event}\ndata: {body}\n\n"


def config_summary(cfg: SystemConfig) -> dict[str, Any]:
    """Bloco ``config`` de ``/api/meta`` (cargas, bateria, FV, EMS, segurança)."""
    s = cfg.summary()
    return {
        "loads": s["loads"], "battery": s["battery"], "battery_energy_wh": s["battery_energy_wh"],
        "pv": s["pv"], "mppt": s["mppt"], "ems": s["ems"], "safety": s["safety"],
        "aux": s["aux"], "demand": s["demand"], "site": s["site"],
        "comm": {k: cfg.raw.get("comm", {}).get(k) for k in ("mqtt_prefix", "telemetry_period_s")},
    }


def schedule_load_forecast(cfg: SystemConfig, t0_ms: int, step_s: int, n: int, sub_s: int = 60) -> list[float]:
    """Potência média prevista (W) das cargas NÃO deslocáveis + auxiliar, pelo perfil programado."""
    aux = float(cfg.raw.get("aux", {}).get("p_w", 0.0))
    out = []
    k_sub = max(1, int(step_s // sub_s))
    for k in range(n):
        acc = 0.0
        for j in range(k_sub):
            tod = tod_from_epoch_ms(int(t0_ms + (k * step_s + j * sub_s) * 1000), cfg)
            acc += sum(ld.p_nom_w for ld in cfg.loads if ld.deferrable is None and demand_at(cfg, ld.key, tod))
        out.append(round(acc / k_sub + aux, 3))
    return out


class StoreBackend:
    """Dados reais (ou do gêmeo digital) a partir do SQLite; origem preservada."""

    def __init__(self, store: SqliteStore, cfg: SystemConfig, poll_s: float = 1.0, keepalive_s: float = 15.0):
        self.store = store
        self.cfg = cfg
        self.poll_s = poll_s
        self.keepalive_s = keepalive_s
        self.demo = False

    def default_source(self) -> str | None:
        return self.store.resolve_source(None)[0]

    def meta(self) -> dict[str, Any]:
        ls = self.store.last_seen()
        period_ms = float(self.cfg.raw.get("comm", {}).get("telemetry_period_s", 5)) * 1000.0
        now = time.time() * 1000.0
        recent = ls["last_seen_ms"] is not None and now - ls["last_seen_ms"] <= 3 * period_ms
        offline_after = ls["status"] == "offline" and (ls["status_rx_ms"] or 0) >= (ls["last_seen_ms"] or 0)
        return {
            "source": ls["source"],
            "device_id": ls["device_id"] or self.cfg.raw["project"]["device_id"],
            "fw_version": ls["fw_version"],
            "connected": bool(recent and not offline_after),
            "last_seen_ms": ls["last_seen_ms"],
            "status": ls["status"],
            "demo": False,
            "config": config_summary(self.cfg),
        }

    def live(self) -> dict[str, Any] | None:
        return self.store.latest()

    def history(self, minutes: float, step_s: float, source: str | None) -> dict[str, Any]:
        return self.store.history(minutes=minutes, step_s=step_s, source=source)

    def energy_daily(self, days: int, source: str | None) -> dict[str, Any]:
        return self.store.energy_daily(days=days, source=source)

    def decisions(self, limit: int, source: str | None) -> dict[str, Any]:
        return self.store.decisions(limit=limit, source=source)

    def events(self, limit: int, source: str | None) -> dict[str, Any]:
        return self.store.events(limit=limit, source=source)

    def export_csv(self, minutes: float, source: str | None, step_s: float | None) -> str:
        return self.store.export_csv(minutes=minutes, source=source, step_s=step_s)

    def forecast(self, source: str | None) -> dict[str, Any]:
        """Previsão do último plano do MPC publicado pelo backend (origin=backend)."""
        dec = self.store.latest_backend_plan(source)
        if dec is None:
            return {"source": self.store.resolve_source(source)[0], "method": None, "t": [], "pv_fc_w": [],
                    "load_fc_w": [], "soc_plan": None,
                    "note": "sem plano do MPC: o modo preditivo nao esta ativo ou o servico live-ems nao publicou"}
        plan = dec["plan"]
        step = int(plan.get("step_s") or 900)
        t0 = int(plan.get("t0_ms") or dec.get("ts_ms") or 0)
        pv = list(plan.get("pv_fc_w") or [])
        n = len(pv)
        load_fc = plan.get("load_fc_w")
        if not isinstance(load_fc, list) or len(load_fc) != n:
            load_fc = schedule_load_forecast(self.cfg, t0, step, n)
        soc = plan.get("soc")
        return {
            "source": dec.get("source"),
            "method": plan.get("forecast_method") or "mpc_plan",
            "t": [t0 + k * step * 1000 for k in range(n)],
            "pv_fc_w": pv,
            "load_fc_w": load_fc,
            "soc_plan": list(soc[:n]) if isinstance(soc, list) else None,
            "plan_ts_ms": dec.get("ts_ms"),
            "status": plan.get("status"),
        }

    async def stream(self, limit: int | None = None) -> AsyncIterator[str]:
        """SSE: envia a última telemetria e depois cada novo registro (consulta periódica ao SQLite)."""
        sent = 0
        cursors = {k: await asyncio.to_thread(self.store.max_id, k) for k in KINDS}
        latest = await asyncio.to_thread(self.store.latest)
        if latest is not None:
            yield sse_message("telemetry", latest, f"telemetry-{cursors['telemetry']}")
            sent += 1
            if limit is not None and sent >= limit:
                return
        last_ping = time.monotonic()
        while True:
            for kind in KINDS:
                rows = await asyncio.to_thread(self.store.rows_after, kind, cursors[kind])
                for rid, doc in rows:
                    cursors[kind] = rid
                    yield sse_message(kind, doc, f"{kind}-{rid}")
                    sent += 1
                    if limit is not None and sent >= limit:
                        return
            if time.monotonic() - last_ping >= self.keepalive_s:
                last_ping = time.monotonic()
                yield ": keepalive\n\n"
            await asyncio.sleep(self.poll_s)


class DemoBackend:
    """Serve ``dashboard/data/demo.json`` (dados SIMULADOS, sempre ``source="simulation"``)."""

    def __init__(self, demo: Mapping[str, Any], cfg: SystemConfig, keepalive_s: float = 15.0):
        self.demo_doc = dict(demo)
        self.cfg = cfg
        self.keepalive_s = keepalive_s
        self.demo = True

    @classmethod
    def from_file(cls, path: Path, cfg: SystemConfig) -> "DemoBackend":
        if not path.is_file():
            raise FileNotFoundError(
                f"{path} não encontrado: gere com 'python -m smartenergy export-demo' ou rode sem --demo")
        return cls(json.loads(path.read_text(encoding="utf-8")), cfg)

    def default_source(self) -> str:
        return DEMO_SOURCE

    def meta(self) -> dict[str, Any]:
        m = dict(self.demo_doc.get("meta") or {})
        m.setdefault("device_id", self.cfg.raw["project"]["device_id"])
        m.setdefault("config", config_summary(self.cfg))
        m.update(source=DEMO_SOURCE, demo=True, connected=False,
                 disclaimer=self.demo_doc.get("disclaimer", "Dados SIMULADOS - nao sao medicoes de hardware."))
        return m

    def live(self) -> dict[str, Any] | None:
        live = self.demo_doc.get("live")
        if not isinstance(live, dict):
            return None
        return {**live, "source": DEMO_SOURCE}

    def history(self, minutes: float, step_s: float, source: str | None) -> dict[str, Any]:
        h = self.demo_doc.get("history") or {}
        t = h.get("t") or []
        series = h.get("series") or {}
        if not t:
            return {"source": DEMO_SOURCE, "t": [], "series": {c: [] for c in series_columns()}, "step_s": int(step_s)}
        r = resample_columns(t, series, step_s, minutes)
        return {"source": DEMO_SOURCE, "t": r["t"], "series": r["series"], "step_s": r["step_s"]}

    def energy_daily(self, days: int, source: str | None) -> dict[str, Any]:
        items = list((self.demo_doc.get("energy_daily") or {}).get("days") or [])
        return {"source": DEMO_SOURCE, "days": items[-int(days):]}

    def _items(self, key: str, limit: int) -> dict[str, Any]:
        items = [dict(x, source=DEMO_SOURCE) for x in (self.demo_doc.get(key) or []) if isinstance(x, dict)]
        items.sort(key=lambda x: int(x.get("ts_ms") or 0), reverse=True)
        return {"source": DEMO_SOURCE, "items": items[: int(limit)]}

    def decisions(self, limit: int, source: str | None) -> dict[str, Any]:
        return self._items("decisions", limit)

    def events(self, limit: int, source: str | None) -> dict[str, Any]:
        return self._items("events", limit)

    def forecast(self, source: str | None) -> dict[str, Any]:
        fc = dict(self.demo_doc.get("forecast") or {})
        fc.setdefault("method", None)
        for k in ("t", "pv_fc_w", "load_fc_w"):
            fc.setdefault(k, [])
        fc.setdefault("soc_plan", None)
        fc["source"] = DEMO_SOURCE
        return fc

    def export_csv(self, minutes: float, source: str | None, step_s: float | None) -> str:
        h = self.history(minutes, step_s or 60, None)
        cols = list(h["series"].keys())
        rows = ([h["t"][i], *[h["series"][c][i] for c in cols]] for i in range(len(h["t"])))
        return format_csv(cols, rows, DEMO_SOURCE, extra="demo.json")

    async def stream(self, limit: int | None = None) -> AsyncIterator[str]:
        """SSE no modo demonstração: envia o ``live`` do demo.json e depois só keepalive."""
        live = self.live()
        if live is not None:
            yield sse_message("telemetry", live, "telemetry-demo")
            if limit is not None and limit <= 1:
                return
        while True:
            await asyncio.sleep(self.keepalive_s)
            yield ": keepalive (demo: dados SIMULADOS estaticos)\n\n"
