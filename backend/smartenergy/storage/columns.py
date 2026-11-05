"""Vocabulário de séries temporais (docs/05 §8) e achatamento de ``sem.telemetry.v1``.

``flatten_telemetry`` converte um pacote de telemetria em um dicionário plano
com as colunas do §8 — o mesmo vocabulário dos CSVs de simulação, da rota
``/api/history`` e do ``demo.json``. Colunas que a telemetria não traz (ex.:
``soc_true``, exclusiva de simulação) ficam ``None``; nada é inventado.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

#: colunas fixas do §8 (sem ``t`` e sem as colunas por carga), na ordem do contrato
BASE_COLUMNS: tuple[str, ...] = (
    "pv_v", "pv_i", "pv_p", "pv_p_avail", "curtail_p", "irr_poa",
    "t_amb", "t_pv", "t_bat",
    "bat_v", "bat_i", "bat_p",
    "bus_v", "bus_i", "bus_p",
    "aux_p", "soc", "soc_unc", "soc_true",
)
TAIL_COLUMNS: tuple[str, ...] = ("unserved_p", "def_done_wh", "mode", "state", "safety")
TEXT_COLUMNS: frozenset[str] = frozenset({"mode", "state", "safety"})
LOAD_FIELDS: tuple[str, ...] = ("p", "on", "duty", "d")
DEFAULT_LOAD_IDS: tuple[int, ...] = (1, 2, 3)

UNITS: dict[str, str] = {
    "t": "ms",
    "pv_v": "V", "pv_i": "A", "pv_p": "W", "pv_p_avail": "W", "curtail_p": "W", "irr_poa": "W/m2",
    "t_amb": "degC", "t_pv": "degC", "t_bat": "degC",
    "bat_v": "V", "bat_i": "A", "bat_p": "W", "bus_v": "V", "bus_i": "A", "bus_p": "W",
    "aux_p": "W", "soc": "1", "soc_unc": "1", "soc_true": "1",
    "unserved_p": "W", "def_done_wh": "Wh", "mode": "text", "state": "text", "safety": "text",
}
LOAD_UNITS: dict[str, str] = {"p": "W", "on": "0/1", "duty": "1", "d": "0/1"}


def load_columns(load_ids: Iterable[int]) -> list[str]:
    """``l{n}_p, l{n}_on, l{n}_duty, l{n}_d`` para cada carga."""
    return [f"l{n}_{f}" for n in load_ids for f in LOAD_FIELDS]


def series_columns(load_ids: Iterable[int] = DEFAULT_LOAD_IDS) -> list[str]:
    """Todas as colunas do §8 (exceto ``t``) na ordem canônica."""
    return [*BASE_COLUMNS, *load_columns(load_ids), *TAIL_COLUMNS]


def unit_of(column: str) -> str:
    """Unidade de uma coluna do §8 (``""`` se desconhecida)."""
    if column in UNITS:
        return UNITS[column]
    if column.startswith("l") and "_" in column:
        return LOAD_UNITS.get(column.split("_", 1)[1], "")
    return ""


def load_ids_from_config(cfg: Any | None) -> tuple[int, ...]:
    """Ids das cargas configuradas (ou 1..3 sem configuração)."""
    if cfg is None or not getattr(cfg, "loads", None):
        return DEFAULT_LOAD_IDS
    return tuple(int(ld.id) for ld in cfg.loads)


def _num(x: Any) -> float | None:
    if isinstance(x, bool) or x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    return None


def _vip(meas: Mapping[str, Any], point: str) -> tuple[float | None, float | None, float | None]:
    m = meas.get(point) or {}
    if m.get("ok") is False:
        return None, None, None
    return _num(m.get("v")), _num(m.get("i")), _num(m.get("p"))


def _temp(meas: Mapping[str, Any], point: str) -> float | None:
    m = meas.get(point) or {}
    if m.get("ok") is False:
        return None
    return _num(m.get("t"))


def telemetry_time_ms(payload: Mapping[str, Any], rx_ms: int | None = None) -> int:
    """Instante usado no eixo do tempo: ``ts_ms`` se sincronizado, senão a hora de recepção."""
    ts = int(payload.get("ts_ms") or 0)
    if payload.get("time_synced") and ts > 0:
        return ts
    return int(rx_ms if rx_ms is not None else ts)


def flatten_telemetry(payload: Mapping[str, Any], rx_ms: int | None = None) -> dict[str, Any]:
    """Achata ``sem.telemetry.v1`` nas colunas do §8 (+ ``t`` e ``source``).

    * medições com ``ok=false`` viram ``None``;
    * ``pv_p_avail``/``soc_true``/``t_amb``/``aux_p``/``unserved_p`` só existem no
      bloco ``sim`` (simulação/gêmeo digital); em hardware ficam ``None``;
    * ``curtail_p = max(0, pv_p_avail − pv_p)`` quando ambos existem;
    * ``source`` é preservado exatamente como recebido.
    """
    meas = payload.get("meas") or {}
    sim = payload.get("sim") or {}
    soc = payload.get("soc") or {}
    ems = payload.get("ems") or {}
    pv_v, pv_i, pv_p = _vip(meas, "pv")
    bat_v, bat_i, bat_p = _vip(meas, "bat")
    bus_v, bus_i, bus_p = _vip(meas, "bus")
    irr = meas.get("irr") or {}
    pv_avail = _num(sim.get("p_pv_avail"))
    curtail = max(0.0, pv_avail - pv_p) if (pv_avail is not None and pv_p is not None) else _num(sim.get("curtail_p"))

    row: dict[str, Any] = {
        "t": telemetry_time_ms(payload, rx_ms),
        "source": payload.get("source"),
        "pv_v": pv_v, "pv_i": pv_i, "pv_p": pv_p,
        "pv_p_avail": pv_avail,
        "curtail_p": curtail,
        "irr_poa": _num(irr.get("g")) if irr.get("ok") else None,
        "t_amb": _num(sim.get("t_amb")),
        "t_pv": _temp(meas, "t_pv"),
        "t_bat": _temp(meas, "t_bat"),
        "bat_v": bat_v, "bat_i": bat_i, "bat_p": bat_p,
        "bus_v": bus_v, "bus_i": bus_i, "bus_p": bus_p,
        "aux_p": _num(sim.get("aux_p")),
        "soc": _num(soc.get("value")),
        "soc_unc": _num(soc.get("unc")),
        "soc_true": _num(sim.get("soc_true")),
    }
    def_done: float | None = None
    for ld in payload.get("loads") or []:
        n = int(ld["id"])
        row[f"l{n}_p"] = _num(ld.get("p"))
        row[f"l{n}_on"] = 1 if ld.get("on") else 0
        row[f"l{n}_duty"] = _num(ld.get("duty"))
        row[f"l{n}_d"] = 1 if ld.get("demand") else 0
        dfr = ld.get("deferrable")
        if isinstance(dfr, Mapping) and def_done is None:
            def_done = _num(dfr.get("e_done_wh"))
    row["unserved_p"] = _num(sim.get("unserved_p"))
    row["def_done_wh"] = def_done
    row["mode"] = ems.get("mode")
    row["state"] = ems.get("state")
    row["safety"] = ems.get("safety")
    return row
