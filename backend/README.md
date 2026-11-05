# Smart Energy Manager — backend Python

Backend do protótipo de microrrede CC de bancada (IC UTFPR-CP): simulação, EMS
(convencional, regras e preditivo/MPC), ingestão MQTT, armazenamento, API HTTP
com dashboard e serviço do EMS preditivo ao vivo.

**Honestidade dos dados:** todo dado tem `source` ∈ {`hardware`, `simulation`},
preservado da origem até a API/CSV. Simulação, gêmeo digital e modo
demonstração são sempre `simulation`. As consultas agregadas não misturam
origens sem pedido explícito (`?source=all`).

## Instalação

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e 'backend[dev]'          # a partir da raiz do repositório
pip install -e 'backend[influx]'       # opcional: InfluxDB
cd backend && pytest -q                # testes
```

Requisitos principais: numpy, pandas, pulp + highspy (solver HiGHS do MPC),
fastapi + uvicorn, paho-mqtt, jsonschema. Testes: pytest, httpx, amqtt (broker
MQTT em processo).

## Subcomandos (`python -m smartenergy <subcomando>`)

| Subcomando | Função |
|---|---|
| `simulate`, `compare`, `export-demo`, `gen-vectors` | simulação, comparação, `demo.json`, vetores golden |
| `serve` | API HTTP + dashboard: `serve --host 0.0.0.0 --port 8000 [--db ARQ] [--demo] [--ingest]` |
| `ingest` | assina o MQTT, valida e grava no SQLite: `ingest [--db ARQ] [--influx]` |
| `twin` | gêmeo digital: publica telemetria SIMULADA via MQTT |
| `sign-cmd` | assina (e publica) comando: `sign-cmd --type set_mode --args '{"mode":"rules"}' [--publish --host H --port P]` |
| `live-ems` *(a registrar no `cli.py`)* | EMS preditivo ao vivo: `smartenergy.live.cli:main(argv)` |

Exemplos:

```sh
python -m smartenergy serve --demo                         # dashboard com dados SIMULADOS (demo.json)
python -m smartenergy serve --host 0.0.0.0 --ingest        # API + ingestão no mesmo processo
python -m smartenergy sign-cmd --type reset_faults --publish
python -m smartenergy sign-cmd --write-vectors tests/data/hmac_vectors.json
python -c "from smartenergy.live.cli import main; main(['--dry-run'])"
```

## Variáveis de ambiente

| Variável | Uso |
|---|---|
| `SEM_CONFIG` | caminho do `system.yaml` (padrão `config/system.yaml`) |
| `SEM_DB_PATH` | arquivo SQLite (padrão `backend/data/sem.sqlite`, modo WAL; não versionado) |
| `SEM_CMD_KEY` | chave HMAC dos comandos (texto ≥ 16 bytes ou `hex:<hex>`); a mesma de `firmware/include/secrets.h` |
| `SEM_API_TOKEN` | token Bearer exigido por `POST /api/cmd/mode` (sem ele, a rota responde 503) |
| `SEM_MQTT_HOST`, `SEM_MQTT_PORT` | broker (padrão `localhost:1883`) |
| `SEM_MQTT_USER`, `SEM_MQTT_PASSWORD` | credenciais do broker (ACL: usuário `sem-backend`, ver `infra/`) |
| `SEM_MQTT_TLS`, `SEM_MQTT_CA` | TLS opcional (`1`) e arquivo da CA |
| `SEM_INFLUX_URL`, `SEM_INFLUX_TOKEN`, `SEM_INFLUX_ORG`, `SEM_INFLUX_BUCKET` | InfluxDB opcional (`ingest --influx`) |

Nunca versione chaves/senhas: use um `.env` local (ignorado pelo git).

## Arquitetura dos serviços

```
ESP32 / gêmeo ──MQTT──► broker (Mosquitto, ACL) ──► ingest ──► SQLite (WAL) ◄── API FastAPI ──► dashboard
     ▲                                  │                                         │ SSE /api/stream
     │ smartenergy/cmd (HMAC)           └──► live-ems (MPC) ── set_loads + decisão┘ POST /api/cmd/mode
```

| Pacote | Conteúdo |
|---|---|
| `security/` | `hmac_cmd.py` (`build_command`, `sign_command`, `verify_envelope`, vetores de teste cruzado), `schemas.py` (validação com `schemas/*.json`), `cli.py` (`sign-cmd`) |
| `storage/` | `sqlite_store.py` (telemetria, decisões, eventos, status, acks, rejeitadas, experimentos), `columns.py` (`flatten_telemetry`, vocabulário §8), `resample.py` (médias por janela), `influx_store.py` (opcional, **não testado**), `synthetic.py` (pacotes sintéticos para testes) |
| `ingest/` | `mqtt_conn.py` (configuração/tópicos/publicação), `mqtt_ingest.py` (validação por esquema, rejeição registrada, reconexão automática), `cli.py` |
| `api/` | `app.py` (rotas do docs/05 §9, SSE, CORS restrito a localhost, dashboard estático em `/`), `backends.py` (SQLite ou `demo.json`), `experiments.py` (catálogo `results/` e comparação), `cli.py` |
| `live/` | `ems_service.py` (`telemetry_to_inputs`, `LiveEmsService`, transporte MQTT), `cli.py` |

### API (docs/05 §9)

`GET /api/meta`, `/api/live`, `/api/stream` (SSE: `telemetry`, `decision`,
`event`), `/api/history?minutes=&step_s=`, `/api/energy/daily?days=`,
`/api/decisions?limit=`, `/api/events?limit=`, `/api/forecast`,
`/api/experiments`, `/api/experiments/{id}`, `/api/comparison`,
`/api/export.csv?minutes=`; `POST /api/cmd/mode` (Bearer). Parâmetro opcional
`source=hardware|simulation|all` nas consultas agregadas (padrão: origem da
telemetria mais recente). Detalhes:

* `history`: janela termina no último registro; `t` = início de cada janela;
  janelas vazias = `null`; nunca reamostra abaixo do passo nativo dos dados.
  Sem hora sincronizada no dispositivo, usa-se a hora de recepção
  (`time_synced_frac` informa a fração sincronizada).
* `energy/daily`: máximo diário dos contadores do firmware (se o dispositivo
  reiniciar e zerar contadores no meio do dia, o total fica subestimado).
* `forecast`: último plano do MPC publicado pelo backend (`origin=backend`);
  `load_fc_w` pelo perfil programado quando o plano não o traz.
* `experiments`/`comparison`: subdiretórios de `results/` com `metrics.json`
  (+ `series.csv`); `comparison.json` mais recente tem precedência; senão
  agrega média/desvio por cenário × estratégia. Em `--demo`, vem do `demo.json`.
* `cmd/mode`: 401 sem token válido; 503 se `SEM_API_TOKEN`/`SEM_CMD_KEY`
  ausentes ou broker indisponível; 409 em modo demonstração; 202 quando
  publicado (o resultado chega em `cmd/ack`).

### EMS preditivo ao vivo (`live/`)

Assina a telemetria, mantém um provedor de previsão (`forecast.factory.make_forecast`)
alimentado com a potência FV medida e, **somente quando o dispositivo reporta
`ems.mode == "predictive"`**, a cada `replan_s` resolve o MPC e publica
`set_loads` assinado (TTL `command_ttl_s`) + `sem.decision.v1` com
`origin="backend"`. Se o MPC cair para regras, nenhum comando é enviado (o
firmware volta às regras locais quando o TTL expira).

### Comandos e vetores de teste

Envelope `{"payload": "<JSON compacto>", "hmac": "<hex>"}`, HMAC-SHA256 sobre os
bytes UTF-8 exatos de `payload`. `tests/data/hmac_vectors.json` traz vetores
(chave PÚBLICA de teste) válidos e inválidos para o firmware validar a mesma
implementação; o teste `test_security_hmac.py` garante que o arquivo está em dia.

## Testes dos serviços

```sh
cd backend
pytest -q tests/test_security_hmac.py tests/test_storage_sqlite.py \
          tests/test_ingest_mqtt.py tests/test_api.py tests/test_live_ems.py
```

`test_ingest_mqtt.py` sobe um broker amqtt em processo (porta livre aleatória)
e testa ingestão ponta a ponta, `sign-cmd --publish` e `POST /api/cmd/mode`
publicando de verdade. Docker/Mosquitto/InfluxDB **não** são exercitados pelos
testes (ver `infra/README.md`).
