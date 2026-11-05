# Infraestrutura (broker MQTT e TSDB opcional)

> **Status: NÃO EXECUTADO.** O ambiente de desenvolvimento não tem Docker; os
> arquivos foram validados apenas sintaticamente (`yaml.safe_load` no compose,
> `sh -n` no script). Teste em uma máquina com Docker antes de usar na bancada.

| Arquivo | Função |
|---|---|
| `docker-compose.yml` | `mosquitto` (sempre), `influxdb` e `grafana` (perfil `tsdb`, opcionais) |
| `mosquitto/mosquitto.conf` | broker sem acesso anônimo, com `password_file` e `acl_file` |
| `mosquitto/acl` | permissões por usuário (menor privilégio) |
| `mosquitto/make-passwd.sh` | gera `mosquitto/passwd` (hashes) com o `mosquitto_passwd` da imagem |
| `.env.example` | variáveis/senhas de exemplo — copie para `.env` (não versionado) |

## Subir o broker

```sh
cd infra
cp .env.example .env              # troque TODAS as senhas
./mosquitto/make-passwd.sh        # cria mosquitto/passwd (não versionado)
docker compose up -d              # só o Mosquitto (porta 1883)
docker compose --profile tsdb up -d   # + InfluxDB 2 (8086) e Grafana (3000)
```

## Usuários e permissões (ACL)

| Usuário | Publica | Assina |
|---|---|---|
| `sem-device` (ESP32) | `telemetry`, `pv/#`, `battery/#`, `load/#`, `ems/mode`, `ems/decision`, `system/status`, `system/event`, `cmd/ack` | `cmd` |
| `sem-twin` (gêmeo digital, dados SIMULADOS) | igual ao dispositivo | `cmd` |
| `sem-backend` (ingest, API, live-ems) | `cmd`, `ems/decision` (MPC) | tudo o que o dispositivo publica |
| `sem-monitor` (Grafana/Node-RED/healthcheck) | — | `smartenergy/#`, `$SYS/#` |

Todos os tópicos têm o prefixo `smartenergy/` (`comm.mqtt_prefix`). A
autenticação do broker é uma camada **adicional**: comandos continuam exigindo
HMAC-SHA256 válido (docs/05 §7), verificado pelo firmware.

## Backend com o broker

```sh
export SEM_MQTT_HOST=localhost SEM_MQTT_PORT=1883
export SEM_MQTT_USER=sem-backend SEM_MQTT_PASSWORD='<senha do .env>'
python -m smartenergy ingest            # MQTT -> SQLite
python -m smartenergy serve --ingest    # ou: API + ingestão no mesmo processo
```

## InfluxDB/Grafana (opcional)

O caminho padrão e testado de armazenamento é o SQLite. Para gravar também no
InfluxDB: `pip install -e 'backend[influx]'`, exporte `SEM_INFLUX_URL=http://localhost:8086`,
`SEM_INFLUX_TOKEN`, `SEM_INFLUX_ORG`, `SEM_INFLUX_BUCKET` (mesmos valores do `.env`)
e rode `python -m smartenergy ingest --influx`. O datasource do Grafana deve ser
configurado manualmente (InfluxDB, linguagem Flux, token acima). Esse caminho
**não foi testado** (sem servidor InfluxDB no ambiente de desenvolvimento).

## TLS

Para acesso fora da rede do laboratório, acrescente um `listener 8883` com
`cafile`/`certfile`/`keyfile` no `mosquitto.conf` e use `SEM_MQTT_TLS=1`
(+ `SEM_MQTT_CA`) no backend. Não exponha a porta 1883 à internet.
