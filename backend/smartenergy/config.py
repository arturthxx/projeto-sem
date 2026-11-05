"""Carregamento da configuração do sistema (config/system.yaml).

O arquivo YAML é a fonte única de verdade dos parâmetros. Este módulo o expõe
como objetos com acesso por atributo (``cfg.battery.capacity_ah``), mantendo o
dicionário original em ``cfg.raw`` e oferecendo ``with_overrides`` para
análises de sensibilidade (ex.: ``{"battery.capacity_ah": 12.0}``).
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "system.yaml"

PRIORITIES = ("high", "medium", "low")


class Section:
    """Visão somente-leitura de um dicionário com acesso por atributo."""

    __slots__ = ("_data",)

    def __init__(self, data: Mapping[str, Any]):
        object.__setattr__(self, "_data", dict(data))

    def __getattr__(self, name: str) -> Any:
        try:
            return _wrap(self._data[name])
        except KeyError as exc:  # pragma: no cover - mensagem amigável
            raise AttributeError(f"parâmetro de configuração ausente: {name!r}") from exc

    def __getitem__(self, name: str) -> Any:
        return _wrap(self._data[name])

    def __contains__(self, name: str) -> bool:
        return name in self._data

    def get(self, name: str, default: Any = None) -> Any:
        return _wrap(self._data.get(name, default))

    def keys(self) -> Iterable[str]:
        return self._data.keys()

    def items(self):
        return ((k, _wrap(v)) for k, v in self._data.items())

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def __setattr__(self, name: str, value: Any) -> None:  # pragma: no cover
        raise AttributeError("configuração é somente-leitura; use with_overrides()")

    def __repr__(self) -> str:  # pragma: no cover
        return f"Section({self._data!r})"


def _wrap(value: Any) -> Any:
    if isinstance(value, Mapping):
        return Section(value)
    if isinstance(value, list):
        return [_wrap(v) for v in value]
    return value


@dataclass(frozen=True)
class DeferrableConfig:
    daily_energy_wh: float
    window_start: str
    deadline: str


@dataclass(frozen=True)
class LoadConfig:
    id: int
    key: str
    name: str
    priority: str
    p_nom_w: float
    v_ref_v: float
    voltage_exponent: float
    dimmable: bool
    i_max_a: float
    gpio: int
    fuse_a: float
    min_duty: float = 0.0
    pwm_freq_hz: float | None = None
    deferrable: DeferrableConfig | None = None

    def power_at(self, v: float, duty: float = 1.0) -> float:
        """Potência estimada P = p_nom·(V/v_ref)^k·duty (W)."""
        if duty <= 0.0 or v <= 0.0:
            return 0.0
        return self.p_nom_w * (v / self.v_ref_v) ** self.voltage_exponent * duty


@dataclass(frozen=True)
class SystemConfig:
    raw: dict[str, Any]
    path: Path | None = None
    loads: tuple[LoadConfig, ...] = field(default_factory=tuple)

    # --- acesso por seção -------------------------------------------------
    def __getattr__(self, name: str) -> Any:
        raw = object.__getattribute__(self, "raw")
        if name in raw:
            return _wrap(raw[name])
        raise AttributeError(f"seção de configuração ausente: {name!r}")

    # --- utilidades -------------------------------------------------------
    @property
    def battery_energy_wh(self) -> float:
        b = self.raw["battery"]
        return float(b["nominal_v"]) * float(b["capacity_ah"])

    def load_by_key(self, key: str) -> LoadConfig:
        for ld in self.loads:
            if ld.key == key:
                return ld
        raise KeyError(key)

    def load_by_id(self, load_id: int) -> LoadConfig:
        for ld in self.loads:
            if ld.id == load_id:
                return ld
        raise KeyError(load_id)

    def with_overrides(self, overrides: Mapping[str, Any] | None) -> "SystemConfig":
        """Retorna uma cópia com chaves pontilhadas substituídas.

        Ex.: ``cfg.with_overrides({"battery.capacity_ah": 12.0})``.
        Para cargas use ``loads.<key>.<campo>`` (ex.: ``loads.res.p_nom_w``).
        """
        if not overrides:
            return self
        data = copy.deepcopy(self.raw)
        for dotted, value in overrides.items():
            parts = dotted.split(".")
            if parts[0] == "loads" and len(parts) >= 3:
                target = next(ld for ld in data["loads"] if ld["key"] == parts[1])
                node = target
                for p in parts[2:-1]:
                    node = node.setdefault(p, {})
                node[parts[-1]] = value
                continue
            node = data
            for p in parts[:-1]:
                node = node.setdefault(p, {})
            node[parts[-1]] = value
        return _build(data, self.path)

    def summary(self) -> dict[str, Any]:
        """Resumo para /api/meta e demo.json."""
        r = self.raw
        return {
            "project": r["project"],
            "site": r["site"],
            "pv": r["pv"],
            "mppt": r["mppt"],
            "battery": {k: v for k, v in r["battery"].items() if k != "ocv_curve"},
            "battery_energy_wh": self.battery_energy_wh,
            "loads": r["loads"],
            "aux": r["aux"],
            "demand": r["demand"],
            "ems": r["ems"],
            "safety": r["safety"],
        }


def _build(data: dict[str, Any], path: Path | None) -> SystemConfig:
    loads = []
    for ld in data.get("loads", []):
        dfr = ld.get("deferrable")
        loads.append(
            LoadConfig(
                id=int(ld["id"]),
                key=str(ld["key"]),
                name=str(ld["name"]),
                priority=str(ld["priority"]),
                p_nom_w=float(ld["p_nom_w"]),
                v_ref_v=float(ld.get("v_ref_v", 12.8)),
                voltage_exponent=float(ld.get("voltage_exponent", 1.0)),
                dimmable=bool(ld.get("dimmable", False)),
                i_max_a=float(ld.get("i_max_a", 0.0)),
                gpio=int(ld.get("gpio", -1)),
                fuse_a=float(ld.get("fuse_a", 0.0)),
                min_duty=float(ld.get("min_duty", 0.0)),
                pwm_freq_hz=ld.get("pwm_freq_hz"),
                deferrable=DeferrableConfig(**dfr) if dfr else None,
            )
        )
        if loads[-1].priority not in PRIORITIES:
            raise ValueError(f"prioridade inválida para carga {ld['key']}: {ld['priority']}")
    return SystemConfig(raw=data, path=path, loads=tuple(loads))


def load_config(path: str | os.PathLike[str] | None = None) -> SystemConfig:
    """Lê ``config/system.yaml`` (ou ``$SEM_CONFIG`` / caminho informado)."""
    p = Path(path or os.environ.get("SEM_CONFIG") or DEFAULT_CONFIG_PATH)
    with open(p, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if int(data.get("schema_version", 0)) != 1:
        raise ValueError("schema_version de system.yaml não suportado")
    return _build(data, p)
