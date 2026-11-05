"""API HTTP do backend (FastAPI) — rotas do docs/05 §9 + dashboard estático.

* Base ``/api``; todas as respostas JSON incluem ``source`` (``/api/live`` devolve o
  próprio ``sem.telemetry.v1``, que já o contém, ou ``null``).
* Modo demonstração (``demo=True``): serve ``dashboard/data/demo.json`` sempre com
  ``source="simulation"``; comandos ficam desabilitados.
* ``/api/stream``: Server-Sent Events (``telemetry``, ``decision``, ``event``).
* ``POST /api/cmd/mode``: exige ``Authorization: Bearer $SEM_API_TOKEN``; assina
  ``set_mode`` com ``$SEM_CMD_KEY`` e publica em ``<prefixo>/cmd``; 503 claro se o
  broker MQTT estiver indisponível.
* CORS restrito a ``localhost``/``127.0.0.1``; o dashboard servido pela própria
  API (mesma origem) não depende de CORS.
"""

from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Literal, Mapping

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from smartenergy import __version__
from smartenergy.api.backends import DemoBackend, StoreBackend
from smartenergy.api.experiments import DemoExperiments, ExperimentCatalog
from smartenergy.config import REPO_ROOT, SystemConfig, load_config
from smartenergy.ingest.mqtt_conn import MqttSettings, MqttUnavailableError, Topics, publish_once
from smartenergy.security.hmac_cmd import CommandError, build_command, envelope_to_json, load_key, sign_command
from smartenergy.storage.sqlite_store import SqliteStore

log = logging.getLogger("smartenergy.api")

CORS_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"
SOURCE_PATTERN = r"^(hardware|simulation|all)$"
MAX_MINUTES = 60 * 24 * 31

Publisher = Callable[[str, str], None]


@dataclass
class ApiSettings:
    """Parâmetros do servidor (a CLI preenche a partir dos argumentos/variáveis de ambiente)."""

    db_path: str | None = None
    demo: bool = False
    demo_path: Path = REPO_ROOT / "dashboard" / "data" / "demo.json"
    results_dir: Path = REPO_ROOT / "results"
    dashboard_dir: Path | None = REPO_ROOT / "dashboard"
    config_path: str | None = None
    api_token: str | None = None          # padrão: $SEM_API_TOKEN
    cmd_ttl_s: int = 60
    stream_poll_s: float = 1.0
    keepalive_s: float = 15.0
    ingest: bool = False                  # roda a ingestão MQTT no mesmo processo
    mqtt: MqttSettings = field(default_factory=MqttSettings.from_env)


class ModeBody(BaseModel):
    mode: Literal["conventional", "rules", "predictive"]


def _bearer_ok(authorization: str | None, token: str) -> bool:
    if not authorization or not authorization.lower().startswith("bearer "):
        return False
    given = authorization[7:].strip().encode("utf-8")
    return hmac.compare_digest(given, token.encode("utf-8"))


def create_app(
    settings: ApiSettings | None = None,
    *,
    cfg: SystemConfig | None = None,
    store: SqliteStore | None = None,
    cmd_publisher: Publisher | None = None,
    environ: Mapping[str, str] | None = None,
) -> FastAPI:
    """Cria a aplicação FastAPI.

    Args:
        settings: parâmetros do servidor.
        cfg: configuração do sistema (padrão: ``load_config(settings.config_path)``).
        store: armazenamento já aberto (testes); padrão: ``SqliteStore(settings.db_path)``.
        cmd_publisher: ``f(topic, payload)`` para publicar comandos (testes); padrão MQTT.
        environ: ambiente para ``SEM_API_TOKEN``/``SEM_CMD_KEY`` (padrão ``os.environ``).
    """
    st = settings or ApiSettings()
    env = os.environ if environ is None else environ
    cfg = cfg or load_config(st.config_path)
    topics = Topics.from_config(cfg)
    api_token = st.api_token if st.api_token is not None else env.get("SEM_API_TOKEN") or None

    backend: StoreBackend | DemoBackend
    catalog: ExperimentCatalog | DemoExperiments
    if st.demo:
        backend = DemoBackend.from_file(Path(st.demo_path), cfg)
        backend.keepalive_s = st.keepalive_s
        catalog = DemoExperiments(backend.demo_doc)
    else:
        store = store or SqliteStore(st.db_path, cfg=cfg)
        backend = StoreBackend(store, cfg, poll_s=st.stream_poll_s, keepalive_s=st.keepalive_s)
        catalog = ExperimentCatalog(st.results_dir, store)

    def publish(topic: str, payload: str) -> None:
        if cmd_publisher is not None:
            cmd_publisher(topic, payload)
        else:
            publish_once(st.mqtt, topic, payload, qos=1)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        ingest = None
        if st.ingest and isinstance(backend, StoreBackend):
            from smartenergy.ingest.mqtt_ingest import MqttIngest

            ingest = MqttIngest(backend.store, st.mqtt, topics)
            ingest.start()
            log.info("ingestão MQTT no processo da API: %s:%s", st.mqtt.host, st.mqtt.port)
        try:
            yield
        finally:
            if ingest is not None:
                ingest.stop()

    app = FastAPI(title="Smart Energy Manager API", version=__version__, lifespan=lifespan)
    app.state.backend = backend
    app.state.catalog = catalog
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=CORS_ORIGIN_REGEX,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

    # ------------------------------------------------------------------ leitura
    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        return backend.meta()

    @app.get("/api/live")
    def live() -> JSONResponse:
        return JSONResponse(backend.live())

    @app.get("/api/history")
    def history(
        minutes: float = Query(1440, gt=0, le=MAX_MINUTES),
        step_s: float = Query(60, ge=1, le=86400),
        source: str | None = Query(None, pattern=SOURCE_PATTERN),
    ) -> dict[str, Any]:
        return backend.history(minutes, step_s, source)

    @app.get("/api/energy/daily")
    def energy_daily(
        days: int = Query(14, ge=1, le=366),
        source: str | None = Query(None, pattern=SOURCE_PATTERN),
    ) -> dict[str, Any]:
        return backend.energy_daily(days, source)

    @app.get("/api/decisions")
    def decisions(
        limit: int = Query(100, ge=1, le=1000),
        source: str | None = Query(None, pattern=SOURCE_PATTERN),
    ) -> dict[str, Any]:
        return backend.decisions(limit, source)

    @app.get("/api/events")
    def events(
        limit: int = Query(100, ge=1, le=1000),
        source: str | None = Query(None, pattern=SOURCE_PATTERN),
    ) -> dict[str, Any]:
        return backend.events(limit, source)

    @app.get("/api/forecast")
    def forecast(source: str | None = Query(None, pattern=SOURCE_PATTERN)) -> dict[str, Any]:
        return backend.forecast(source)

    @app.get("/api/experiments")
    def experiments() -> dict[str, Any]:
        return catalog.list()

    @app.get("/api/experiments/{exp_id:path}")
    def experiment(
        exp_id: str,
        step_s: float | None = Query(None, ge=1, le=86400),
        columns: str | None = Query(None, description="colunas separadas por vírgula"),
    ) -> dict[str, Any]:
        cols = [c.strip() for c in columns.split(",") if c.strip()] if columns else None
        rec = catalog.get(exp_id, step_s=step_s, columns=cols)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"experimento não encontrado: {exp_id}")
        return rec

    @app.get("/api/comparison")
    def comparison() -> dict[str, Any]:
        return catalog.comparison()

    @app.get("/api/export.csv")
    def export_csv(
        minutes: float = Query(1440, gt=0, le=MAX_MINUTES),
        step_s: float | None = Query(None, ge=1, le=86400),
        source: str | None = Query(None, pattern=SOURCE_PATTERN),
    ) -> PlainTextResponse:
        text = backend.export_csv(minutes, source, step_s)
        src = text.split(";", 1)[0].removeprefix("# source=").strip()
        return PlainTextResponse(
            text, media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="sem_{src}_{int(minutes)}min.csv"',
                     "X-SEM-Source": src})

    @app.get("/api/stream")
    async def stream(limit: int | None = Query(None, ge=1, le=100000)) -> StreamingResponse:
        return StreamingResponse(
            backend.stream(limit), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------ comandos
    @app.post("/api/cmd/mode", status_code=202)
    def cmd_mode(body: ModeBody, authorization: str | None = Header(None)) -> dict[str, Any]:
        if not api_token:
            raise HTTPException(status_code=503, detail="comandos desabilitados: SEM_API_TOKEN não configurado")
        if not _bearer_ok(authorization, api_token):
            raise HTTPException(status_code=401, detail="token inválido ou ausente",
                                headers={"WWW-Authenticate": "Bearer"})
        if backend.demo:
            raise HTTPException(status_code=409, detail="modo demonstração (dados SIMULADOS): comandos desabilitados")
        try:
            key = load_key("SEM_CMD_KEY", env)
            cmd = build_command("set_mode", {"mode": body.mode}, st.cmd_ttl_s)
            text = envelope_to_json(sign_command(cmd, key))
        except CommandError as exc:
            raise HTTPException(status_code=503, detail=f"não foi possível assinar o comando: {exc}") from exc
        try:
            publish(topics.cmd, text)
        except MqttUnavailableError as exc:
            raise HTTPException(status_code=503, detail=f"broker MQTT indisponível: {exc}") from exc
        return {"source": backend.default_source(), "published": True, "topic": topics.cmd, "id": cmd["id"],
                "type": "set_mode", "mode": body.mode, "ttl_s": cmd["ttl_s"],
                "note": f"o resultado chega em {topics.ack} e no evento MODE_CHANGE/CMD_REJECTED"}

    @app.exception_handler(ValueError)
    async def value_error(_req: Any, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    # ------------------------------------------------------------------ dashboard
    dash = Path(st.dashboard_dir) if st.dashboard_dir else None
    if dash is not None and dash.is_dir():
        app.mount("/", StaticFiles(directory=str(dash), html=True), name="dashboard")
    else:
        @app.get("/")
        def root() -> dict[str, Any]:
            return {"source": backend.default_source(), "msg": "dashboard não encontrado; API em /api/meta",
                    "demo": backend.demo}

    return app
