"""Carregamento e validação dos esquemas JSON normativos (``schemas/*.json``).

Os esquemas são a forma validável do contrato de interfaces (docs/05). Este
módulo os carrega uma única vez (cache) e oferece validação com mensagens de
erro curtas, adequadas para registro (log) de mensagens rejeitadas.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from smartenergy.config import REPO_ROOT

SCHEMA_DIR = REPO_ROOT / "schemas"

#: nome lógico -> arquivo em ``schemas/``
SCHEMA_FILES: dict[str, str] = {
    "telemetry": "telemetry.schema.json",
    "decision": "decision.schema.json",
    "event": "event.schema.json",
    "command": "command.schema.json",
    "envelope": "envelope.schema.json",
}


class SchemaValidationError(ValueError):
    """Documento JSON não obedece ao esquema normativo."""

    def __init__(self, schema: str, message: str):
        super().__init__(f"{schema}: {message}")
        self.schema = schema
        self.message = message


@lru_cache(maxsize=None)
def load_schema(name: str, schema_dir: str | None = None) -> dict[str, Any]:
    """Lê o esquema ``name`` (chave de :data:`SCHEMA_FILES` ou nome de arquivo)."""
    filename = SCHEMA_FILES.get(name, name)
    path = Path(schema_dir) if schema_dir else SCHEMA_DIR
    with open(path / filename, encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=None)
def get_validator(name: str) -> Draft202012Validator:
    """Validador (Draft 2020-12) em cache para o esquema ``name``."""
    schema = load_schema(name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def schema_error(name: str, doc: Any) -> str | None:
    """Retorna a mensagem do erro mais relevante, ou ``None`` se ``doc`` é válido."""
    err = best_match(get_validator(name).iter_errors(doc))
    if err is None:
        return None
    where = "/".join(str(p) for p in err.absolute_path) or "<raiz>"
    return f"{where}: {err.message}"


def validate(name: str, doc: Any) -> None:
    """Valida ``doc`` contra o esquema ``name``; levanta :class:`SchemaValidationError`."""
    msg = schema_error(name, doc)
    if msg is not None:
        raise SchemaValidationError(name, msg)


def is_valid(name: str, doc: Any) -> bool:
    return get_validator(name).is_valid(doc)
