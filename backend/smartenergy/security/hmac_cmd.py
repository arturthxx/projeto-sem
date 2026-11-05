"""Comandos autenticados ``sem.cmd.v1`` (docs/05 §7).

Formato do envelope publicado em ``<prefixo>/cmd``::

    {"payload": "<string JSON compacta do comando>", "hmac": "<hex minúsculo>"}

O HMAC-SHA256 é calculado sobre os **bytes UTF-8 exatos** da string
``payload`` — nenhuma canonicalização de JSON é necessária no firmware: ele
verifica o HMAC sobre a string recebida e só então a interpreta.

A chave compartilhada vem de ``$SEM_CMD_KEY`` (backend) e de
``firmware/include/secrets.h`` (dispositivo, não versionado). Formatos aceitos
para a variável de ambiente:

* texto puro (usado como bytes UTF-8), ex.: ``SEM_CMD_KEY='frase-longa-e-aleatoria...'``;
* ``hex:<hex>`` para chaves binárias, ex.: ``SEM_CMD_KEY=hex:00112233...``.

A chave deve ter pelo menos :data:`MIN_KEY_BYTES` bytes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any, Mapping

from smartenergy.security.schemas import schema_error

CMD_SCHEMA = "sem.cmd.v1"
CMD_TYPES = ("set_mode", "set_loads", "sync_soc", "reset_faults")
MIN_KEY_BYTES = 16
MAX_PAYLOAD_LEN = 1024  # limite do envelope.schema.json (bytes no buffer MQTT do firmware)
KEY_ENV_VAR = "SEM_CMD_KEY"


class CommandError(ValueError):
    """Comando ou envelope malformado (não obedece aos esquemas)."""


class SignatureError(CommandError):
    """HMAC inválido: o envelope não foi assinado com a chave compartilhada."""


class CommandKeyError(CommandError):
    """Chave HMAC ausente ou fraca."""


# ---------------------------------------------------------------------------
# Construção
# ---------------------------------------------------------------------------
def now_ms() -> int:
    """Epoch UTC atual em milissegundos."""
    return int(time.time() * 1000)


def new_command_id(prefix: str = "c") -> str:
    """Identificador único curto (≤ 40 caracteres) para anti-replay no dispositivo."""
    return f"{prefix}-{now_ms():x}-{secrets.token_hex(3)}"


def build_command(
    type: str,  # noqa: A002 - nome do campo do contrato
    args: Mapping[str, Any] | None,
    ttl_s: int,
    id: str | None = None,  # noqa: A002 - nome do campo do contrato
    ts_ms: int | None = None,
) -> dict[str, Any]:
    """Monta e valida o conteúdo ``payload`` de um comando ``sem.cmd.v1``.

    Args:
        type: ``set_mode`` | ``set_loads`` | ``sync_soc`` | ``reset_faults``.
        args: argumentos do tipo (ver docs/05 §7).
        ttl_s: validade em segundos (1..3600).
        id: identificador único (gerado se omitido).
        ts_ms: instante de emissão em epoch ms (agora, se omitido).

    Raises:
        CommandError: se o comando não obedecer a ``schemas/command.schema.json``.
    """
    payload: dict[str, Any] = {
        "schema": CMD_SCHEMA,
        "id": id if id is not None else new_command_id(),
        "ts_ms": int(now_ms() if ts_ms is None else ts_ms),
        "ttl_s": int(ttl_s),
        "type": type,
        "args": dict(args or {}),
    }
    err = schema_error("command", payload)
    if err is not None:
        raise CommandError(f"comando inválido: {err}")
    return payload


def payload_to_string(payload: Mapping[str, Any]) -> str:
    """Serialização JSON compacta (sem espaços, ASCII) usada como ``payload`` do envelope."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def compute_hmac(payload_str: str, key: bytes) -> str:
    """HMAC-SHA256 (hex minúsculo) sobre os bytes UTF-8 exatos de ``payload_str``."""
    return hmac.new(key, payload_str.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_payload_string(payload_str: str, key: bytes) -> dict[str, str]:
    """Assina uma string de payload já serializada (usado nos vetores de teste)."""
    _check_key(key)
    env = {"payload": payload_str, "hmac": compute_hmac(payload_str, key)}
    err = schema_error("envelope", env)
    if err is not None:
        raise CommandError(f"envelope inválido: {err}")
    return env


def sign_command(payload: Mapping[str, Any], key: bytes) -> dict[str, str]:
    """Valida o comando e devolve o envelope ``{"payload": str, "hmac": hex}``.

    Raises:
        CommandError: comando inválido ou payload maior que :data:`MAX_PAYLOAD_LEN`.
        CommandKeyError: chave ausente/curta.
    """
    err = schema_error("command", dict(payload))
    if err is not None:
        raise CommandError(f"comando inválido: {err}")
    s = payload_to_string(payload)
    if len(s.encode("utf-8")) > MAX_PAYLOAD_LEN:
        raise CommandError(f"payload com {len(s)} bytes excede {MAX_PAYLOAD_LEN}")
    return sign_payload_string(s, key)


def envelope_to_json(envelope: Mapping[str, str]) -> str:
    """Serialização compacta do envelope para publicação MQTT."""
    return json.dumps({"payload": envelope["payload"], "hmac": envelope["hmac"]}, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Verificação
# ---------------------------------------------------------------------------
def verify_envelope(
    envelope: Mapping[str, Any] | str | bytes,
    key: bytes,
    *,
    now_ms_: int | None = None,
    max_skew_s: float | None = None,
) -> dict[str, Any]:
    """Verifica o HMAC (tempo constante) e devolve o comando decodificado.

    Ordem idêntica à do firmware: esquema do envelope → HMAC → esquema do comando
    → (opcional) desvio de relógio ``|ts_ms − agora| ≤ max_skew_s``.

    Raises:
        CommandError: envelope/comando malformado ou fora da janela de tempo.
        SignatureError: HMAC não confere.
    """
    if isinstance(envelope, (str, bytes, bytearray)):
        try:
            envelope = json.loads(envelope)
        except (ValueError, UnicodeDecodeError) as exc:
            raise CommandError(f"envelope não é JSON: {exc}") from exc
    err = schema_error("envelope", envelope)
    if err is not None:
        raise CommandError(f"envelope inválido: {err}")
    _check_key(key)
    payload_str = str(envelope["payload"])
    expected = compute_hmac(payload_str, key)
    if not hmac.compare_digest(expected.encode("ascii"), str(envelope["hmac"]).encode("ascii")):
        raise SignatureError("HMAC inválido")
    try:
        payload = json.loads(payload_str)
    except ValueError as exc:
        raise CommandError(f"payload não é JSON: {exc}") from exc
    err = schema_error("command", payload)
    if err is not None:
        raise CommandError(f"comando inválido: {err}")
    if max_skew_s is not None:
        ref = now_ms() if now_ms_ is None else int(now_ms_)
        skew_s = abs(int(payload["ts_ms"]) - ref) / 1000.0
        if skew_s > float(max_skew_s):
            raise CommandError(f"desvio de relógio {skew_s:.1f} s > {max_skew_s} s")
    return payload


# ---------------------------------------------------------------------------
# Chave
# ---------------------------------------------------------------------------
def parse_key(value: str) -> bytes:
    """Converte o valor de ``$SEM_CMD_KEY`` em bytes (texto puro ou ``hex:...``)."""
    v = value.strip()
    if v.lower().startswith("hex:"):
        try:
            return bytes.fromhex(v[4:])
        except ValueError as exc:
            raise CommandKeyError(f"{KEY_ENV_VAR}: hex inválido") from exc
    return v.encode("utf-8")


def load_key(env_var: str = KEY_ENV_VAR, environ: Mapping[str, str] | None = None) -> bytes:
    """Lê a chave HMAC da variável de ambiente ``env_var``.

    Raises:
        CommandKeyError: variável ausente/vazia ou chave com menos de :data:`MIN_KEY_BYTES` bytes.
    """
    env = os.environ if environ is None else environ
    raw = env.get(env_var, "")
    if not raw.strip():
        raise CommandKeyError(f"variável de ambiente {env_var} não definida (chave HMAC dos comandos)")
    key = parse_key(raw)
    _check_key(key)
    return key


def _check_key(key: bytes) -> None:
    if not isinstance(key, (bytes, bytearray)) or len(key) < MIN_KEY_BYTES:
        raise CommandKeyError(f"chave HMAC deve ter ao menos {MIN_KEY_BYTES} bytes")


# ---------------------------------------------------------------------------
# Vetores de teste cruzado (firmware <-> Python)
# ---------------------------------------------------------------------------
VECTOR_KEY = "sem-test-key-NAO-USAR-EM-PRODUCAO"
"""Chave fixa e pública dos vetores de teste — nunca usar em produção."""

_VECTOR_TS_MS = 1763290800000  # 2025-11-16T11:00:00Z (instante fixo e arbitrário)


def generate_vectors(key_text: str = VECTOR_KEY) -> dict[str, Any]:
    """Gera vetores determinísticos (chave, payload, hmac) para validar o firmware.

    ``valid`` indica o resultado esperado da verificação COMPLETA (HMAC + esquema).
    """
    key = key_text.encode("utf-8")
    cmds = [
        ("set_mode_rules", build_command("set_mode", {"mode": "rules"}, 60, id="v-0001", ts_ms=_VECTOR_TS_MS)),
        ("set_mode_predictive",
         build_command("set_mode", {"mode": "predictive"}, 60, id="v-0002", ts_ms=_VECTOR_TS_MS)),
        ("set_loads", build_command(
            "set_loads",
            {"loads": [{"id": 1, "on": True, "duty": 1.0}, {"id": 2, "on": True, "duty": 0.6},
                       {"id": 3, "on": False, "duty": 0.0}]},
            1200, id="v-0003", ts_ms=_VECTOR_TS_MS)),
        ("sync_soc", build_command("sync_soc", {"soc": 1.0}, 60, id="v-0004", ts_ms=_VECTOR_TS_MS)),
        ("reset_faults", build_command("reset_faults", {}, 60, id="v-0005", ts_ms=_VECTOR_TS_MS)),
    ]
    vectors: list[dict[str, Any]] = []
    for name, cmd in cmds:
        env = sign_command(cmd, key)
        vectors.append({"name": name, "payload": env["payload"], "hmac": env["hmac"], "valid": True})

    base = vectors[0]
    flipped = base["hmac"][:-1] + ("0" if base["hmac"][-1] != "0" else "1")
    spaced = base["payload"].replace(",", ", ", 1)  # mesmo JSON semanticamente, bytes diferentes
    bad_schema_payload = payload_to_string(
        {"schema": CMD_SCHEMA, "id": "v-0006", "ts_ms": _VECTOR_TS_MS, "ttl_s": 60,
         "type": "set_loads", "args": {"loads": [{"id": 2, "on": True, "duty": 1.5}]}})
    negatives = [
        {"name": "hmac_alterado", "payload": base["payload"], "hmac": flipped, "valid": False,
         "expect": "hmac"},
        {"name": "payload_com_espaco", "payload": spaced, "hmac": base["hmac"], "valid": False,
         "expect": "hmac"},
        {"name": "hmac_maiusculo", "payload": base["payload"], "hmac": base["hmac"].upper(), "valid": False,
         "expect": "envelope"},
        {"name": "duty_fora_da_faixa", "payload": bad_schema_payload,
         "hmac": compute_hmac(bad_schema_payload, key), "valid": False, "expect": "schema"},
    ]
    return {
        "description": ("Vetores de teste cruzado HMAC-SHA256 dos comandos sem.cmd.v1 (docs/05 §7). "
                        "HMAC sobre os bytes UTF-8 exatos de 'payload'. Chave PUBLICA de teste."),
        "algorithm": "HMAC-SHA256",
        "key_utf8": key_text,
        "key_hex": key.hex(),
        "vectors": vectors + negatives,
    }
