"""Armazenamento padrão em SQLite (arquivo ``backend/data/sem.sqlite``, modo WAL).

Tabelas:

* ``telemetry``  — um registro por ``sem.telemetry.v1``: JSON bruto + colunas
  achatadas do vocabulário §8 (docs/05) + contadores de energia do dia;
* ``decisions``  — ``sem.decision.v1`` (firmware e backend);
* ``events``     — ``sem.event.v1``;
* ``status``     — ``<prefixo>/system/status`` (online/offline, LWT);
* ``acks``       — ``<prefixo>/cmd/ack``;
* ``rejected``   — mensagens recebidas e rejeitadas (JSON/esquema inválido), para auditoria;
* ``experiments``— catálogo de experimentos (id, cenário, estratégia, semente, métricas, séries).

**Origem dos dados**: toda linha guarda ``source`` (``hardware``/``simulation``)
exatamente como recebido. Consultas agregadas (``history``, ``energy_daily``,
``export_csv``, ``decisions``, ``events``) NUNCA misturam origens sem pedido
explícito: por padrão filtram pela origem da telemetria mais recente;
``source="all"`` desativa o filtro (e o rótulo vira ``"mixed"`` se houver mais
de uma origem).

O eixo do tempo (``t_ms``) é ``ts_ms`` do dispositivo quando ``time_synced``; sem
hora sincronizada usa-se a hora de recepção no servidor (``rx_ms``) e a coluna
``time_synced=0`` registra isso.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd

from smartenergy.config import REPO_ROOT
from smartenergy.storage.columns import (
    TEXT_COLUMNS,
    flatten_telemetry,
    load_ids_from_config,
    series_columns,
    unit_of,
)
from smartenergy.storage.resample import resample_frame

DEFAULT_DB_PATH = REPO_ROOT / "backend" / "data" / "sem.sqlite"
SOURCES = ("hardware", "simulation")
ALL_SOURCES = "all"
ENERGY_FIELDS = ("pv_in", "bat_chg", "bat_dis", "load", "unserved")
_LOAD_COL_RE = re.compile(r"^l\d+_(p|on|duty|d)$")
KINDS = ("telemetry", "decision", "event")
_KIND_TABLE = {"telemetry": "telemetry", "decision": "decisions", "event": "events"}


def default_db_path() -> Path:
    """``$SEM_DB_PATH`` ou ``backend/data/sem.sqlite``."""
    env = os.environ.get("SEM_DB_PATH")
    return Path(env) if env else DEFAULT_DB_PATH


def _now_ms() -> int:
    return int(time.time() * 1000)


def summarize_sources(sources: Iterable[str | None]) -> str | None:
    """Rótulo de origem de um conjunto de registros: única, ``"mixed"`` ou ``None``."""
    s = {x for x in sources if x}
    if not s:
        return None
    return next(iter(s)) if len(s) == 1 else "mixed"


class SqliteStore:
    """Persistência de telemetria, decisões, eventos e experimentos em SQLite.

    Uma conexão por instância, protegida por trava (uso seguro entre threads do
    servidor HTTP e do cliente MQTT). Processos distintos (ingestão e API)
    podem abrir o mesmo arquivo: o modo WAL permite leitura concorrente.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        cfg: Any | None = None,
        load_ids: Iterable[int] | None = None,
    ) -> None:
        p = str(path) if path is not None else str(default_db_path())
        if p != ":memory:":
            Path(p).parent.mkdir(parents=True, exist_ok=True)
        self.path = p
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(p, check_same_thread=False, timeout=30.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        if p != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        self.load_ids = tuple(load_ids) if load_ids is not None else load_ids_from_config(cfg)
        self._create_schema()

    # ------------------------------------------------------------------ esquema
    def _create_schema(self) -> None:
        cols = []
        for c in series_columns(self.load_ids):
            cols.append(f'"{c}" {"TEXT" if c in TEXT_COLUMNS else "REAL"}')
        energy = ", ".join(f"e_{f} REAL" for f in ENERGY_FIELDS)
        ddl = f"""
        CREATE TABLE IF NOT EXISTS telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            t_ms INTEGER NOT NULL, ts_ms INTEGER, rx_ms INTEGER NOT NULL,
            device_id TEXT, source TEXT NOT NULL, seq INTEGER, boot_id INTEGER,
            time_synced INTEGER, soc_valid INTEGER, fallback INTEGER,
            energy_day TEXT, {energy},
            {", ".join(cols)},
            json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_tel_src_t ON telemetry(source, t_ms);
        CREATE INDEX IF NOT EXISTS ix_tel_src_day ON telemetry(source, energy_day);
        CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_ms INTEGER, rx_ms INTEGER NOT NULL, source TEXT NOT NULL, origin TEXT,
            mode TEXT, state TEXT, json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_dec_src ON decisions(source, id);
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_ms INTEGER, rx_ms INTEGER NOT NULL, source TEXT NOT NULL, level TEXT, code TEXT,
            json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_evt_src ON events(source, id);
        CREATE TABLE IF NOT EXISTS status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rx_ms INTEGER NOT NULL, device_id TEXT, status TEXT, json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS acks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rx_ms INTEGER NOT NULL, cmd_id TEXT, accepted INTEGER, reason TEXT, json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rejected (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rx_ms INTEGER NOT NULL, topic TEXT, error TEXT, raw TEXT
        );
        CREATE TABLE IF NOT EXISTS experiments (
            id TEXT PRIMARY KEY, scenario TEXT, strategy TEXT, seed INTEGER,
            source TEXT NOT NULL, created TEXT, metrics_json TEXT, meta_json TEXT, series_path TEXT
        );
        """
        with self._lock:
            self._conn.executescript(ddl)
            self._tele_cols = self._table_columns("telemetry")
            # cargas configuradas depois da criação do arquivo
            for c in series_columns(self.load_ids):
                self._ensure_column(c)

    def _table_columns(self, table: str) -> set[str]:
        return {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}

    def _ensure_column(self, col: str) -> None:
        if col in self._tele_cols:
            return
        kind = "TEXT" if col in TEXT_COLUMNS else "REAL"
        self._conn.execute(f'ALTER TABLE telemetry ADD COLUMN "{col}" {kind}')
        self._tele_cols.add(col)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "SqliteStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ escrita
    def insert_telemetry(self, payload: Mapping[str, Any], rx_ms: int | None = None) -> int:
        """Grava um ``sem.telemetry.v1`` (já validado). Retorna o id da linha."""
        source = payload.get("source")
        if source not in SOURCES:
            raise ValueError(f"telemetria sem 'source' válido: {source!r}")
        rx = int(rx_ms if rx_ms is not None else _now_ms())
        flat = flatten_telemetry(payload, rx)
        energy = payload.get("energy") or {}
        soc = payload.get("soc") or {}
        ems = payload.get("ems") or {}
        row: dict[str, Any] = {
            "t_ms": int(flat.pop("t")),
            "ts_ms": int(payload.get("ts_ms") or 0),
            "rx_ms": rx,
            "device_id": payload.get("device_id"),
            "source": flat.pop("source"),
            "seq": payload.get("seq"),
            "boot_id": payload.get("boot_id"),
            "time_synced": 1 if payload.get("time_synced") else 0,
            "soc_valid": 1 if soc.get("valid") else 0,
            "fallback": 1 if ems.get("fallback") else 0,
            "energy_day": energy.get("day"),
            "json": json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        }
        for f in ENERGY_FIELDS:
            v = energy.get(f)
            row[f"e_{f}"] = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        with self._lock:
            for k, v in flat.items():
                if k not in self._tele_cols:
                    if not _LOAD_COL_RE.match(k):
                        continue
                    self._ensure_column(k)
                row[k] = v
            return self._insert("telemetry", row)

    def insert_decision(self, payload: Mapping[str, Any], rx_ms: int | None = None) -> int:
        """Grava um ``sem.decision.v1`` (origem firmware ou backend)."""
        source = payload.get("source")
        if source not in SOURCES:
            raise ValueError(f"decisão sem 'source' válido: {source!r}")
        row = {
            "ts_ms": int(payload.get("ts_ms") or 0), "rx_ms": int(rx_ms if rx_ms is not None else _now_ms()),
            "source": source, "origin": payload.get("origin"), "mode": payload.get("mode"),
            "state": payload.get("state"),
            "json": json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=float),
        }
        with self._lock:
            return self._insert("decisions", row)

    def insert_event(self, payload: Mapping[str, Any], rx_ms: int | None = None) -> int:
        """Grava um ``sem.event.v1``."""
        source = payload.get("source")
        if source not in SOURCES:
            raise ValueError(f"evento sem 'source' válido: {source!r}")
        row = {
            "ts_ms": int(payload.get("ts_ms") or 0), "rx_ms": int(rx_ms if rx_ms is not None else _now_ms()),
            "source": source, "level": payload.get("level"), "code": payload.get("code"),
            "json": json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        }
        with self._lock:
            return self._insert("events", row)

    def insert_status(self, payload: Mapping[str, Any], rx_ms: int | None = None) -> int:
        row = {
            "rx_ms": int(rx_ms if rx_ms is not None else _now_ms()), "device_id": payload.get("device_id"),
            "status": payload.get("status"), "json": json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        }
        with self._lock:
            return self._insert("status", row)

    def insert_ack(self, payload: Mapping[str, Any], rx_ms: int | None = None) -> int:
        row = {
            "rx_ms": int(rx_ms if rx_ms is not None else _now_ms()), "cmd_id": payload.get("id"),
            "accepted": 1 if payload.get("accepted") else 0, "reason": payload.get("reason"),
            "json": json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        }
        with self._lock:
            return self._insert("acks", row)

    def insert_rejected(self, topic: str, error: str, raw: bytes | str, rx_ms: int | None = None) -> int:
        """Registra uma mensagem rejeitada (texto truncado em 4 kB)."""
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        row = {"rx_ms": int(rx_ms if rx_ms is not None else _now_ms()), "topic": topic,
               "error": error[:500], "raw": text[:4096]}
        with self._lock:
            return self._insert("rejected", row)

    def upsert_experiment(
        self,
        exp_id: str,
        scenario: str | None,
        strategy: str | None,
        seed: int | None,
        metrics: Mapping[str, Any] | None,
        *,
        source: str = "simulation",
        created: str | None = None,
        series_path: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        """Registra (ou substitui) um experimento no catálogo."""
        if source not in SOURCES:
            raise ValueError(f"experimento sem 'source' válido: {source!r}")
        row = {
            "id": exp_id, "scenario": scenario, "strategy": strategy,
            "seed": int(seed) if seed is not None else None, "source": source,
            "created": created or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "metrics_json": json.dumps(dict(metrics or {}), default=float),
            "meta_json": json.dumps(dict(meta or {}), default=str), "series_path": series_path,
        }
        cols = ", ".join(row)
        qs = ", ".join("?" for _ in row)
        with self._lock:
            self._conn.execute(f"INSERT OR REPLACE INTO experiments ({cols}) VALUES ({qs})", tuple(row.values()))

    def _insert(self, table: str, row: Mapping[str, Any]) -> int:
        cols = ", ".join(f'"{c}"' for c in row)
        qs = ", ".join("?" for _ in row)
        cur = self._conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", tuple(row.values()))
        return int(cur.lastrowid or 0)

    # ------------------------------------------------------------------ leitura
    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def resolve_source(self, source: str | None) -> tuple[str | None, bool]:
        """Converte o parâmetro ``source`` em ``(filtro, explícito)``.

        ``None`` → origem da telemetria mais recente (ou sem filtro se vazio);
        ``"all"`` → sem filtro; ``hardware``/``simulation`` → filtro explícito.
        """
        if source == ALL_SOURCES:
            return None, True
        if source:
            if source not in SOURCES:
                raise ValueError(f"source inválido: {source!r} (use hardware, simulation ou all)")
            return source, True
        rows = self._query("SELECT source FROM telemetry ORDER BY id DESC LIMIT 1")
        return (rows[0]["source"] if rows else None), False

    def latest(self, source: str | None = None) -> dict[str, Any] | None:
        """Último ``sem.telemetry.v1`` recebido (ordem de chegada), ou ``None``."""
        if source and source != ALL_SOURCES:
            rows = self._query("SELECT json FROM telemetry WHERE source=? ORDER BY id DESC LIMIT 1", (source,))
        else:
            rows = self._query("SELECT json FROM telemetry ORDER BY id DESC LIMIT 1")
        return json.loads(rows[0]["json"]) if rows else None

    def last_seen(self) -> dict[str, Any]:
        """Resumo do dispositivo: última telemetria e último status (para ``/api/meta``)."""
        out: dict[str, Any] = {"device_id": None, "source": None, "fw_version": None, "last_seen_ms": None,
                               "last_t_ms": None, "status": None, "status_rx_ms": None}
        rows = self._query("SELECT rx_ms, t_ms, device_id, source, json FROM telemetry ORDER BY id DESC LIMIT 1")
        if rows:
            r = rows[0]
            out.update(device_id=r["device_id"], source=r["source"], last_seen_ms=r["rx_ms"], last_t_ms=r["t_ms"],
                       fw_version=json.loads(r["json"]).get("fw_version"))
        st = self._query("SELECT rx_ms, device_id, status FROM status ORDER BY id DESC LIMIT 1")
        if st:
            out.update(status=st[0]["status"], status_rx_ms=st[0]["rx_ms"])
            out["device_id"] = out["device_id"] or st[0]["device_id"]
        return out

    def history(
        self,
        minutes: float = 1440,
        step_s: float = 60,
        source: str | None = None,
        end_ms: int | None = None,
    ) -> dict[str, Any]:
        """Séries do §8 nos últimos ``minutes`` (até o último registro), médias por ``step_s``.

        Retorna ``{source, t, series, step_s, time_synced_frac}``; ``t`` = início de cada janela (ms).
        """
        filt, _ = self.resolve_source(source)
        cols = series_columns(self.load_ids)
        where, params = ("WHERE source=?", [filt]) if filt else ("", [])
        if end_ms is None:
            r = self._query(f"SELECT MAX(t_ms) AS m FROM telemetry {where}", params)
            end_ms = r[0]["m"] if r and r[0]["m"] is not None else None
        if end_ms is None:
            return {"source": filt, "t": [], "series": {c: [] for c in cols}, "step_s": int(step_s)}
        start = int(end_ms - float(minutes) * 60_000)
        cond = "t_ms BETWEEN ? AND ?" + (" AND source=?" if filt else "")
        sel = ", ".join(f'"{c}"' for c in cols)
        rows = self._query(
            f"SELECT t_ms AS t, source AS _src, time_synced AS _ts, {sel} FROM telemetry WHERE {cond} ORDER BY t_ms, id",
            [start, int(end_ms), *([filt] if filt else [])],
        )
        df = pd.DataFrame([tuple(r) for r in rows], columns=["t", "_src", "_ts", *cols])
        label = filt or summarize_sources(df["_src"].tolist())
        synced = float(df["_ts"].mean()) if len(df) else None
        out = resample_frame(df.drop(columns=["_src", "_ts"]), step_s)
        return {"source": label, "t": out["t"], "series": out["series"], "step_s": out["step_s"],
                "time_synced_frac": synced}

    def energy_daily(self, days: int = 14, source: str | None = None) -> dict[str, Any]:
        """Energia por dia local: máximo dos contadores acumulados de cada dia.

        Os contadores do firmware zeram à meia-noite local; o máximo do dia é o
        total do dia (se o dispositivo reiniciar e zerar os contadores no meio do
        dia, o total fica SUBESTIMADO — nunca duplicado).
        """
        filt, _ = self.resolve_source(source)
        where = "WHERE energy_day IS NOT NULL" + (" AND source=?" if filt else "")
        aggs = ", ".join(f"MAX(e_{f}) AS {f}" for f in ENERGY_FIELDS)
        rows = self._query(
            f"SELECT energy_day AS day, source, {aggs}, COUNT(*) AS n FROM telemetry {where} "
            f"GROUP BY energy_day, source ORDER BY energy_day DESC LIMIT ?",
            [*([filt] if filt else []), int(days) * (1 if filt else len(SOURCES))],
        )
        items = []
        for r in reversed(rows):
            d = {"day": r["day"], "source": r["source"], "n": r["n"]}
            for f in ENERGY_FIELDS:
                d[f] = None if r[f] is None else round(float(r[f]), 3)
            items.append(d)
        label = filt or summarize_sources(d["source"] for d in items)
        return {"source": label, "days": items}

    def _items(self, table: str, limit: int, source: str | None) -> dict[str, Any]:
        filt, _ = self.resolve_source(source)
        if filt:
            rows = self._query(f"SELECT json, source FROM {table} WHERE source=? ORDER BY id DESC LIMIT ?",
                               (filt, int(limit)))
        else:
            rows = self._query(f"SELECT json, source FROM {table} ORDER BY id DESC LIMIT ?", (int(limit),))
        return {"source": filt or summarize_sources(r["source"] for r in rows),
                "items": [json.loads(r["json"]) for r in rows]}

    def decisions(self, limit: int = 100, source: str | None = None) -> dict[str, Any]:
        """``{source, items}`` com as decisões mais recentes primeiro."""
        return self._items("decisions", limit, source)

    def events(self, limit: int = 100, source: str | None = None) -> dict[str, Any]:
        """``{source, items}`` com os eventos mais recentes primeiro."""
        return self._items("events", limit, source)

    def latest_backend_plan(self, source: str | None = None) -> dict[str, Any] | None:
        """Última decisão do backend (MPC) que contém ``plan`` (para ``/api/forecast``)."""
        filt, _ = self.resolve_source(source)
        sql = "SELECT json FROM decisions WHERE origin='backend'" + (" AND source=?" if filt else "")
        sql += " ORDER BY id DESC LIMIT 50"
        for r in self._query(sql, [filt] if filt else []):
            d = json.loads(r["json"])
            if isinstance(d.get("plan"), dict):
                return d
        return None

    def acks(self, limit: int = 100) -> list[dict[str, Any]]:
        return [json.loads(r["json"]) for r in self._query("SELECT json FROM acks ORDER BY id DESC LIMIT ?", (limit,))]

    def rejected(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._query("SELECT rx_ms, topic, error, raw FROM rejected ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def counts(self) -> dict[str, int]:
        out = {}
        for t in ("telemetry", "decisions", "events", "status", "acks", "rejected", "experiments"):
            out[t] = int(self._query(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"])
        return out

    # ------------------------------------------------------------------ fluxo (SSE)
    def max_id(self, kind: str) -> int:
        table = _KIND_TABLE[kind]
        r = self._query(f"SELECT MAX(id) AS m FROM {table}")
        return int(r[0]["m"] or 0)

    def rows_after(self, kind: str, after_id: int, limit: int = 100) -> list[tuple[int, dict[str, Any]]]:
        """Registros de ``kind`` (telemetry/decision/event) com id > ``after_id``, em ordem."""
        table = _KIND_TABLE[kind]
        rows = self._query(f"SELECT id, json FROM {table} WHERE id > ? ORDER BY id LIMIT ?", (int(after_id), limit))
        return [(int(r["id"]), json.loads(r["json"])) for r in rows]

    # ------------------------------------------------------------------ experimentos
    def list_experiments(self) -> list[dict[str, Any]]:
        rows = self._query("SELECT id, scenario, strategy, seed, source, created, series_path FROM experiments "
                           "ORDER BY created DESC, id")
        return [dict(r) for r in rows]

    def get_experiment(self, exp_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM experiments WHERE id=?", (exp_id,))
        if not rows:
            return None
        r = dict(rows[0])
        r["metrics"] = json.loads(r.pop("metrics_json") or "{}")
        r["meta"] = json.loads(r.pop("meta_json") or "{}")
        return r

    # ------------------------------------------------------------------ exportação
    def export_csv(
        self,
        minutes: float = 1440,
        source: str | None = None,
        step_s: float | None = None,
    ) -> str:
        """CSV das colunas §8 (cabeçalho ``coluna[unidade]``; 1ª linha ``# source=...``).

        Sem ``step_s`` exporta os registros brutos da janela; com ``step_s``, a série reamostrada.
        """
        filt, _ = self.resolve_source(source)
        cols = series_columns(self.load_ids)
        if step_s:
            h = self.history(minutes=minutes, step_s=step_s, source=source)
            t, series, label = h["t"], h["series"], h["source"]
            rows_out = [[t[i], *[series[c][i] for c in cols]] for i in range(len(t))]
        else:
            where, params = ("WHERE source=?", [filt]) if filt else ("", [])
            r = self._query(f"SELECT MAX(t_ms) AS m FROM telemetry {where}", params)
            end = r[0]["m"] if r and r[0]["m"] is not None else None
            rows_out = []
            srcs: list[str] = []
            if end is not None:
                cond = "t_ms BETWEEN ? AND ?" + (" AND source=?" if filt else "")
                sel = ", ".join(f'"{c}"' for c in cols)
                rows = self._query(f"SELECT t_ms, source, {sel} FROM telemetry WHERE {cond} ORDER BY t_ms, id",
                                   [int(end - float(minutes) * 60_000), int(end), *([filt] if filt else [])])
                for row in rows:
                    srcs.append(row["source"])
                    rows_out.append([row["t_ms"], *[row[c] for c in cols]])
            label = filt if filt else summarize_sources(srcs)
        return format_csv(cols, rows_out, label, extra=f"device_rows={len(rows_out)}")


def format_csv(cols: list[str], rows: Iterable[Iterable[Any]], source: str | None, extra: str = "") -> str:
    """Formata CSV com linha ``# source=...`` e cabeçalho ``coluna[unidade]`` (docs/05 §8/§9)."""
    buf = io.StringIO()
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    note = "DADOS SIMULADOS - nao sao medicoes de hardware; " if source in ("simulation", "mixed") else ""
    buf.write(f"# source={source or 'none'}; {note}generated_at={gen}" + (f"; {extra}" if extra else "") + "\n")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([f"t[{unit_of('t')}]", *[f"{c}[{unit_of(c)}]" for c in cols]])
    for r in rows:
        w.writerow(["" if v is None else (f"{v:.6g}" if isinstance(v, float) else v) for v in r])
    return buf.getvalue()
