"""Reamostragem de séries colunares (docs/05 §8) por média em janelas de ``step_s``.

Usada pelo armazenamento SQLite (``/api/history``), pelo modo demonstração e
pelas séries de experimentos. Colunas numéricas: média dos valores válidos da
janela; colunas de texto (``mode``, ``state``, ``safety``): último valor da
janela. Janelas sem dados viram ``None`` (lacunas explícitas, não interpoladas).
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from smartenergy.storage.columns import TEXT_COLUMNS

MAX_POINTS = 5000
"""Máximo de pontos por série; ``step_s`` é aumentado automaticamente acima disso."""


def effective_step_s(span_ms: float, step_s: float, max_points: int = MAX_POINTS) -> int:
    """Passo efetivo (s) que mantém ``span/step ≤ max_points`` (múltiplo inteiro do pedido)."""
    step = max(1, int(round(step_s)))
    if span_ms > 0 and span_ms / (step * 1000.0) > max_points:
        step = step * int(math.ceil(span_ms / (step * 1000.0) / max_points))
    return step


def _is_text(name: str, s: pd.Series) -> bool:
    if name in TEXT_COLUMNS:
        return True
    if s.dtype == object:
        non_null = s.dropna()
        return bool(len(non_null)) and not pd.to_numeric(non_null, errors="coerce").notna().all()
    return False


def _to_json_list(s: pd.Series, decimals: int) -> list[Any]:
    if s.dtype == object:
        return [None if (v is None or (isinstance(v, float) and math.isnan(v))) else v for v in s.tolist()]
    arr = s.to_numpy(dtype=float)
    out: list[Any] = []
    for v in np.round(arr, decimals).tolist():
        out.append(None if math.isnan(v) else v)
    return out


def resample_frame(
    df: pd.DataFrame,
    step_s: float,
    t_col: str = "t",
    fill_gaps: bool = True,
    max_points: int = MAX_POINTS,
    decimals: int = 4,
) -> dict[str, Any]:
    """Reamostra ``df`` (coluna de tempo ``t_col`` em ms) e devolve ``{t, series, step_s}``.

    Os rótulos de ``t`` são o INÍCIO de cada janela ``[t, t + step)`` alinhada à época.
    """
    cols = [c for c in df.columns if c != t_col]
    if df.empty:
        return {"t": [], "series": {c: [] for c in cols}, "step_s": int(step_s)}
    t = pd.to_numeric(df[t_col], errors="coerce")
    df = df.loc[t.notna()].copy()
    t = t.loc[t.notna()].astype("int64")
    span = float(t.max() - t.min())
    # nunca reamostrar abaixo do passo nativo dos dados (evita lacunas artificiais)
    diffs = np.diff(np.unique(t.to_numpy()))
    native_s = int(math.ceil(float(np.median(diffs)) / 1000.0)) if len(diffs) else 1
    step = effective_step_s(span, max(float(step_s), float(native_s)), max_points)
    step_ms = step * 1000
    bins = (t // step_ms) * step_ms
    df["_bin"] = bins.to_numpy()

    text_cols = [c for c in cols if _is_text(c, df[c])]
    num_cols = [c for c in cols if c not in text_cols]
    for c in num_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    grouped = df.groupby("_bin", sort=True)
    num = grouped[num_cols].mean() if num_cols else pd.DataFrame()
    txt = grouped[text_cols].last() if text_cols else pd.DataFrame()  # last() ignora nulos
    index = sorted(df["_bin"].unique().tolist())
    if fill_gaps and index:
        index = list(range(int(index[0]), int(index[-1]) + step_ms, step_ms))
    series: dict[str, list[Any]] = {}
    for c in cols:
        if c in text_cols:
            s = txt[c].astype(object).reindex(index)
        else:
            s = num[c].reindex(index).astype(float)
        series[c] = _to_json_list(s, decimals)
    return {"t": [int(x) for x in index], "series": series, "step_s": step}


def resample_columns(
    t: Sequence[Any],
    series: Mapping[str, Sequence[Any]],
    step_s: float,
    minutes: float | None = None,
    max_points: int = MAX_POINTS,
) -> dict[str, Any]:
    """Versão colunar: recorta os últimos ``minutes`` (relativos ao último ``t``) e reamostra."""
    data = {k: list(v) for k, v in series.items() if len(v) == len(t)}
    df = pd.DataFrame(data)
    df.insert(0, "t", list(t))
    if minutes is not None and len(df):
        tt = pd.to_numeric(df["t"], errors="coerce")
        df = df.loc[tt >= tt.max() - float(minutes) * 60_000.0]
    return resample_frame(df, step_s, max_points=max_points)
