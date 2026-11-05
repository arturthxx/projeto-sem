"""Testes dos comandos autenticados sem.cmd.v1 (HMAC-SHA256) e da CLI sign-cmd."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from smartenergy.security import cli as sign_cli
from smartenergy.security.hmac_cmd import (
    CommandError,
    CommandKeyError,
    SignatureError,
    build_command,
    compute_hmac,
    envelope_to_json,
    generate_vectors,
    load_key,
    sign_command,
    verify_envelope,
)
from smartenergy.security.schemas import schema_error

KEY = b"chave-de-teste-com-32-bytes-ok!!"
VECTORS = Path(__file__).parent / "data" / "hmac_vectors.json"


def test_build_command_valid_and_schema() -> None:
    cmd = build_command("set_mode", {"mode": "rules"}, 60, id="c-1", ts_ms=1000)
    assert cmd == {"schema": "sem.cmd.v1", "id": "c-1", "ts_ms": 1000, "ttl_s": 60, "type": "set_mode",
                   "args": {"mode": "rules"}}
    assert schema_error("command", cmd) is None
    auto = build_command("reset_faults", None, 10)
    assert auto["id"].startswith("c-") and len(auto["id"]) <= 40 and auto["ts_ms"] > 0


@pytest.mark.parametrize(
    "type_,args,ttl",
    [
        ("set_loads", {"loads": [{"id": 2, "on": True, "duty": 1.5}]}, 60),  # duty fora da faixa
        ("set_mode", {"mode": "turbo"}, 60),                                 # modo desconhecido
        ("set_mode", {"mode": "rules"}, 0),                                  # ttl < 1
        ("set_mode", {"mode": "rules"}, 3601),                               # ttl > 3600
        ("explode", {}, 60),                                                 # tipo desconhecido
        ("reset_faults", {"x": 1}, 60),                                      # args devem ser vazios
    ],
)
def test_build_command_rejects_invalid(type_: str, args: dict, ttl: int) -> None:
    with pytest.raises(CommandError):
        build_command(type_, args, ttl)


def test_sign_and_verify_roundtrip() -> None:
    cmd = build_command("set_loads", {"loads": [{"id": 1, "on": True, "duty": 1.0}]}, 1200, id="c-2", ts_ms=5)
    env = sign_command(cmd, KEY)
    assert set(env) == {"payload", "hmac"}
    assert " " not in env["payload"]  # JSON compacto
    expected = hmac.new(KEY, env["payload"].encode("utf-8"), hashlib.sha256).hexdigest()
    assert env["hmac"] == expected and env["hmac"] == compute_hmac(env["payload"], KEY)
    assert schema_error("envelope", env) is None
    assert verify_envelope(env, KEY) == cmd
    assert verify_envelope(envelope_to_json(env), KEY) == cmd           # string JSON
    assert verify_envelope(envelope_to_json(env).encode(), KEY) == cmd  # bytes MQTT


def test_verify_detects_tampering() -> None:
    env = sign_command(build_command("set_mode", {"mode": "rules"}, 60, id="c-3", ts_ms=5), KEY)
    with pytest.raises(SignatureError):
        verify_envelope({"payload": env["payload"].replace("rules", "predictive"), "hmac": env["hmac"]}, KEY)
    with pytest.raises(SignatureError):
        verify_envelope(env, b"outra-chave-qualquer-de-32-bytes")
    flipped = env["hmac"][:-1] + ("0" if env["hmac"][-1] != "0" else "1")
    with pytest.raises(SignatureError):
        verify_envelope({"payload": env["payload"], "hmac": flipped}, KEY)
    with pytest.raises(CommandError):  # hex maiúsculo viola o esquema do envelope
        verify_envelope({"payload": env["payload"], "hmac": env["hmac"].upper()}, KEY)
    with pytest.raises(CommandError):
        verify_envelope("{nao-json", KEY)


def test_verify_clock_skew() -> None:
    env = sign_command(build_command("set_mode", {"mode": "rules"}, 60, ts_ms=1_000_000), KEY)
    assert verify_envelope(env, KEY, now_ms_=1_000_000 + 119_000, max_skew_s=120)["ts_ms"] == 1_000_000
    with pytest.raises(CommandError, match="desvio"):
        verify_envelope(env, KEY, now_ms_=1_000_000 + 121_000, max_skew_s=120)


def test_load_key_formats() -> None:
    assert load_key(environ={"SEM_CMD_KEY": "uma-frase-secreta-longa"}) == b"uma-frase-secreta-longa"
    assert load_key(environ={"SEM_CMD_KEY": "hex:" + "ab" * 16}) == bytes([0xAB] * 16)
    with pytest.raises(CommandKeyError):
        load_key(environ={})
    with pytest.raises(CommandKeyError):
        load_key(environ={"SEM_CMD_KEY": "curta"})
    with pytest.raises(CommandKeyError):
        load_key(environ={"SEM_CMD_KEY": "hex:zz"})


def test_cross_vectors_file_is_current_and_consistent() -> None:
    """O arquivo usado pelo firmware deve ser idêntico ao gerado e coerente com a verificação."""
    doc = json.loads(VECTORS.read_text(encoding="utf-8"))
    assert doc == generate_vectors()
    key = bytes.fromhex(doc["key_hex"])
    assert key == doc["key_utf8"].encode("utf-8")
    kinds = {"hmac": SignatureError, "envelope": CommandError, "schema": CommandError}
    assert sum(v["valid"] for v in doc["vectors"]) >= 5
    for v in doc["vectors"]:
        env = {"payload": v["payload"], "hmac": v["hmac"]}
        if v["valid"]:
            assert verify_envelope(env, key)["schema"] == "sem.cmd.v1", v["name"]
        else:
            with pytest.raises(kinds[v["expect"]]):
                verify_envelope(env, key)
            if v["expect"] == "hmac":
                with pytest.raises(SignatureError):
                    verify_envelope(env, key)


def test_cli_sign_cmd_prints_verifiable_envelope(monkeypatch: pytest.MonkeyPatch,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("SEM_CMD_KEY", KEY.decode())
    rc = sign_cli.main(["--type", "set_mode", "--args", '{"mode":"predictive"}', "--ttl", "30", "--id", "c-cli"])
    assert rc == 0
    env = json.loads(capsys.readouterr().out.strip())
    cmd = verify_envelope(env, KEY)
    assert cmd["args"] == {"mode": "predictive"} and cmd["ttl_s"] == 30 and cmd["id"] == "c-cli"


def test_cli_sign_cmd_errors(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("SEM_CMD_KEY", raising=False)
    assert sign_cli.main(["--type", "set_mode", "--args", '{"mode":"rules"}']) == 2
    monkeypatch.setenv("SEM_CMD_KEY", KEY.decode())
    assert sign_cli.main(["--type", "set_mode", "--args", "{ruim"]) == 2
    assert sign_cli.main(["--type", "set_mode", "--args", '{"mode":"x"}']) == 2
    # broker inexistente -> código 3 e mensagem clara
    assert sign_cli.main(["--type", "reset_faults", "--publish", "--host", "127.0.0.1", "--port", "1"]) == 3
    assert "inacessível" in capsys.readouterr().err
