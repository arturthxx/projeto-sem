"""Catálogo de experimentos para ``/api/experiments`` e ``/api/comparison``.

Fontes (em ordem):

1. diretório ``results/``: cada subdiretório com ``metrics.json`` (formato de
   ``python -m smartenergy simulate``: ``{source, meta:{scenario, strategy, seed, ...}, metrics}``)
   e, opcionalmente, ``series.csv`` (linha ``# source=...`` + cabeçalho ``coluna[unidade]``);
2. tabela ``experiments`` do SQLite (experimentos de bancada registrados);
3. modo demonstração: ``dashboard/data/demo.json`` (:class:`DemoExperiments`).

``/api/comparison`` usa o ``comparison.json`` mais recente em ``results/`` (gerado
pelo comparador) ou, na falta dele, agrega média/desvio-padrão por
cenário × estratégia a partir dos ``metrics.json`` encontrados.
"""

from __future__ import annotations

import json
import math
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd

from smartenergy.storage.resample import resample_columns, resample_frame
from smartenergy.storage.sqlite_store import SqliteStore, summarize_sources

COMPARISON_SERIES = ("soc_true", "pv_p", "pv_p_avail", "bus_p", "unserved_p", "curtail_p")
SERIES_MAX_POINTS = 2000
COMPARISON_STEP_S = 600

METRICS_DEF: dict[str, dict[str, str]] = {
    "service_rate": {"label": "Taxa de atendimento", "unit": "1", "better": "higher"},
    "service_rate_high": {"label": "Atendimento (alta prioridade)", "unit": "1", "better": "higher"},
    "service_rate_medium": {"label": "Atendimento (media prioridade)", "unit": "1", "better": "higher"},
    "service_rate_low": {"label": "Atendimento (deslocavel)", "unit": "1", "better": "higher"},
    "e_unserved_wh": {"label": "Energia nao atendida", "unit": "Wh", "better": "lower"},
    "e_unserved_high_wh": {"label": "Nao atendida (alta)", "unit": "Wh", "better": "lower"},
    "e_unserved_medium_wh": {"label": "Nao atendida (media)", "unit": "Wh", "better": "lower"},
    "e_unserved_low_wh": {"label": "Nao atendida (deslocavel)", "unit": "Wh", "better": "lower"},
    "e_served_wh": {"label": "Energia entregue as cargas", "unit": "Wh", "better": "higher"},
    "e_pv_wh": {"label": "Energia FV colhida", "unit": "Wh", "better": "higher"},
    "e_curtail_wh": {"label": "Energia FV nao aproveitada", "unit": "Wh", "better": "lower"},
    "curtail_frac": {"label": "Fracao de corte FV", "unit": "1", "better": "lower"},
    "bat_throughput_wh": {"label": "Throughput da bateria", "unit": "Wh", "better": "lower"},
    "equiv_cycles": {"label": "Ciclos equivalentes", "unit": "1", "better": "lower"},
    "soc_min": {"label": "SOC minimo", "unit": "1", "better": "higher"},
    "delta_soc": {"label": "Variacao de SOC (final - inicial)", "unit": "1", "better": "higher"},
    "n_switch": {"label": "Comutacoes", "unit": "1", "better": "lower"},
    "lvd_events": {"label": "Desconexoes por subtensao", "unit": "1", "better": "lower"},
    "time_in_lvd_h": {"label": "Tempo em LVD", "unit": "h", "better": "lower"},
}

_UNIT_RE = re.compile(r"\[[^\]]*\]$")


def read_series_csv(path: Path) -> pd.DataFrame:
    """Lê um CSV do vocabulário §8 (ignora linhas ``#``; remove ``[unidade]`` do cabeçalho)."""
    df = pd.read_csv(path, comment="#")
    df.columns = [_UNIT_RE.sub("", str(c)).strip() for c in df.columns]
    return df


def _iso_mtime(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _finite(x: Any) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return float(x) if math.isfinite(float(x)) else None


def _frame_to_series(df: pd.DataFrame, step_s: float | None, columns: Iterable[str] | None,
                     max_points: int = SERIES_MAX_POINTS) -> dict[str, Any]:
    if "t" not in df.columns:
        return {"t": []}
    if columns:
        keep = ["t", *[c for c in columns if c in df.columns and c != "t"]]
        df = df[keep]
    # resample_frame nunca desce abaixo do passo nativo e limita o número de pontos
    out = resample_frame(df, float(step_s) if step_s else 1.0, max_points=max_points)
    return {"t": out["t"], **out["series"], "step_s": out["step_s"]}


class ExperimentCatalog:
    """Experimentos do diretório ``results/`` (+ tabela ``experiments`` do SQLite)."""

    def __init__(self, results_dir: Path | str, store: SqliteStore | None = None, max_depth: int = 3) -> None:
        self.results_dir = Path(results_dir)
        self.store = store
        self.max_depth = max_depth
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    # ------------------------------------------------------------------ varredura
    def _metric_files(self, name: str = "metrics.json") -> list[Path]:
        if not self.results_dir.is_dir():
            return []
        out = []
        for p in self.results_dir.rglob(name):
            rel = p.relative_to(self.results_dir)
            if len(rel.parts) - 1 <= self.max_depth and not any(x.startswith(".") for x in rel.parts):
                out.append(p)
        return out

    def _load_record(self, mfile: Path) -> dict[str, Any] | None:
        key = str(mfile)
        mtime = mfile.stat().st_mtime
        hit = self._cache.get(key)
        if hit and hit[0] == mtime:
            return hit[1]
        try:
            doc = json.loads(mfile.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(doc, dict):
            return None
        meta = doc.get("meta") if isinstance(doc.get("meta"), dict) else {}
        metrics = doc.get("metrics") if isinstance(doc.get("metrics"), dict) else {}
        rel = mfile.parent.relative_to(self.results_dir).as_posix()
        series_csv = mfile.parent / "series.csv"
        rec = {
            "id": rel,
            "scenario": meta.get("scenario", doc.get("scenario")),
            "strategy": meta.get("strategy", doc.get("strategy")),
            "seed": meta.get("seed", doc.get("seed")),
            "source": doc.get("source") or meta.get("source") or "unknown",
            "created": meta.get("generated_at") or _iso_mtime(mfile),
            "meta": meta,
            "metrics": metrics,
            "_series_path": str(series_csv) if series_csv.is_file() else None,
        }
        self._cache[key] = (mtime, rec)
        return rec

    def records(self) -> list[dict[str, Any]]:
        recs: dict[str, dict[str, Any]] = {}
        if self.store is not None:
            for r in self.store.list_experiments():
                full = self.store.get_experiment(r["id"]) or r
                recs[r["id"]] = {**full, "_series_path": full.get("series_path")}
        for mf in self._metric_files():
            rec = self._load_record(mf)
            if rec is not None:
                recs[rec["id"]] = rec
        return sorted(recs.values(), key=lambda r: (str(r.get("created") or ""), r["id"]), reverse=True)

    # ------------------------------------------------------------------ API
    def list(self) -> dict[str, Any]:
        items = [{k: r.get(k) for k in ("id", "scenario", "strategy", "seed", "source", "created")}
                 for r in self.records()]
        return {"source": summarize_sources(i["source"] for i in items), "items": items}

    def get(self, exp_id: str, step_s: float | None = None, columns: Iterable[str] | None = None) -> dict[str, Any] | None:
        rec = next((r for r in self.records() if r["id"] == exp_id), None)
        if rec is None:
            return None
        out = {k: v for k, v in rec.items() if not k.startswith("_") and k != "series_path"}
        series: dict[str, Any] = {"t": []}
        sp = rec.get("_series_path")
        if sp and Path(sp).is_file():
            series = _frame_to_series(read_series_csv(Path(sp)), step_s, columns)
        out["series"] = series
        return out

    def comparison(self) -> dict[str, Any]:
        files = self._metric_files("comparison.json")
        if files:
            newest = max(files, key=lambda p: p.stat().st_mtime)
            try:
                doc = json.loads(newest.read_text(encoding="utf-8"))
                comp = doc.get("comparison", doc) if isinstance(doc, dict) else None
                if isinstance(comp, dict):
                    comp = dict(comp)
                    comp.setdefault("source", doc.get("source", "simulation"))
                    comp.setdefault("origin_file", newest.relative_to(self.results_dir).as_posix())
                    return comp
            except (OSError, ValueError):
                pass
        return aggregate_comparison(self.records(), self._series_for)

    def _series_for(self, rec: Mapping[str, Any]) -> dict[str, Any] | None:
        sp = rec.get("_series_path")
        if not sp or not Path(sp).is_file():
            return None
        return _frame_to_series(read_series_csv(Path(sp)), COMPARISON_STEP_S, COMPARISON_SERIES)


def aggregate_comparison(records: list[dict[str, Any]], series_loader: Any = None) -> dict[str, Any]:
    """Agrega ``metrics`` por cenário × estratégia (média e desvio-padrão amostral)."""
    recs = [r for r in records if r.get("scenario") and r.get("strategy")]
    scenarios = sorted({str(r["scenario"]) for r in recs})
    strategies = [s for s in ("conventional", "rules", "predictive") if any(r["strategy"] == s for r in recs)]
    strategies += sorted({str(r["strategy"]) for r in recs} - set(strategies))
    seeds = sorted({int(r["seed"]) for r in recs if isinstance(r.get("seed"), int)})
    present = [m for m in METRICS_DEF if any(_finite((r.get("metrics") or {}).get(m)) is not None for r in recs)]
    table = []
    for sc in scenarios:
        for st in strategies:
            group = [r for r in recs if r["scenario"] == sc and r["strategy"] == st]
            if not group:
                continue
            mean: dict[str, float | None] = {}
            std: dict[str, float | None] = {}
            for m in present:
                vals = [v for v in (_finite((r.get("metrics") or {}).get(m)) for r in group) if v is not None]
                mean[m] = round(statistics.fmean(vals), 6) if vals else None
                std[m] = round(statistics.stdev(vals), 6) if len(vals) > 1 else (0.0 if vals else None)
            table.append({"scenario": sc, "strategy": st, "n": len(group), "mean": mean, "std": std})
    series: dict[str, Any] = {}
    if series_loader is not None:
        for sc in scenarios:
            entry: dict[str, Any] = {}
            for st in strategies:
                group = sorted((r for r in recs if r["scenario"] == sc and r["strategy"] == st),
                               key=lambda r: (r.get("seed") is None, r.get("seed") or 0))
                s = series_loader(group[0]) if group else None
                if not s:
                    continue
                entry.setdefault("t", s["t"])
                entry[st] = {c: s.get(c, []) for c in COMPARISON_SERIES}
            if entry:
                series[sc] = entry
    return {
        "source": summarize_sources(r.get("source") for r in recs),
        "scenarios": scenarios,
        "strategies": strategies,
        "seeds": seeds,
        "metrics_def": {m: METRICS_DEF[m] for m in present},
        "table": table,
        "series": series,
        "origin_file": None,
    }


class DemoExperiments:
    """Experimentos derivados de ``demo.json`` (sempre ``source="simulation"``)."""

    def __init__(self, demo: Mapping[str, Any]) -> None:
        self.comp = dict(demo.get("comparison") or {})
        self.created = demo.get("generated_at")

    def comparison(self) -> dict[str, Any]:
        return {**self.comp, "source": "simulation"}

    def _pairs(self) -> list[tuple[str, str]]:
        pairs = [(str(r.get("scenario")), str(r.get("strategy"))) for r in self.comp.get("table", [])]
        for sc, entry in (self.comp.get("series") or {}).items():
            for st in entry:
                if st != "t" and (sc, st) not in pairs:
                    pairs.append((sc, st))
        return pairs

    def list(self) -> dict[str, Any]:
        items = [{"id": f"demo/{sc}/{st}", "scenario": sc, "strategy": st, "seed": None,
                  "source": "simulation", "created": self.created} for sc, st in self._pairs()]
        return {"source": "simulation", "items": items}

    def get(self, exp_id: str, step_s: float | None = None, columns: Iterable[str] | None = None) -> dict[str, Any] | None:
        parts = exp_id.split("/")
        if len(parts) != 3 or parts[0] != "demo" or (parts[1], parts[2]) not in self._pairs():
            return None
        sc, st = parts[1], parts[2]
        row = next((r for r in self.comp.get("table", []) if r.get("scenario") == sc and r.get("strategy") == st), {})
        entry = (self.comp.get("series") or {}).get(sc, {})
        cols = entry.get(st, {}) if isinstance(entry.get(st), dict) else {}
        if columns:
            cols = {k: v for k, v in cols.items() if k in set(columns)}
        t = entry.get("t", [])
        series: dict[str, Any] = {"t": t, **cols}
        if step_s and t:
            r = resample_columns(t, cols, step_s)
            series = {"t": r["t"], **r["series"], "step_s": r["step_s"]}
        return {"id": exp_id, "scenario": sc, "strategy": st, "seed": None, "source": "simulation",
                "created": self.created, "n": row.get("n"), "metrics": row.get("mean", {}),
                "metrics_std": row.get("std", {}), "series": series,
                "note": "media das sementes do pacote de demonstracao (dados SIMULADOS)"}
