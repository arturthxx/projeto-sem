# 05 — Contrato de interfaces (NORMATIVO)

> Este documento é **normativo**: firmware, backend, simulação, gêmeo digital e
> dashboard DEVEM obedecer exatamente aos formatos aqui definidos. Mudanças
> exigem incremento de versão do esquema (`*.v1` → `*.v2`).
>
> Parâmetros numéricos: [`config/system.yaml`](../config/system.yaml) (fonte única).
> Esquemas JSON validáveis: [`schemas/`](../schemas/).

## 1. Convenções gerais

| Item | Convenção |
|---|---|
| Tempo | `ts_ms` = epoch UTC em milissegundos. `0` quando `time_synced=false`. Exibição em hora local `America/Sao_Paulo` (UTC−3). |
| Unidades | V, A, W, Wh, °C (`degC`), SOC em fração (0–1), duty em fração (0–1). Todo pacote de telemetria carrega o dicionário `units`. |
| Corrente da bateria | **positiva = descarga**, negativa = carga (documento-base, seção 6.2). `p_bat = v_bat × i_bat` (positiva = bateria fornecendo energia). |
| Corrente FV e de cargas | sempre ≥ 0 no sentido fonte → consumo. |
| Origem dos dados | campo `source` ∈ {`hardware`, `simulation`}. **Toda** tela/arquivo que exibir dados deve mostrar a origem. Dados de simulação nunca são rotulados como medição. |
| Pontos de medição | `pv` (entrada FV do MPPT), `bat` (ramo da bateria), `bus` (barramento das cargas experimentais), `t_pv` (temperatura do módulo), `t_bat` (temperatura da bateria), `irr` (irradiância, opcional), `l1..l3` (por carga: medida se houver sensor dedicado, senão **estimada**). |
| Identificação | `device_id` (ex.: `sem-esp32-01`), `boot_id` (contador de inicializações persistido em NVS), `seq` (contador de mensagens desde o boot). |
| Prioridades | `high`, `medium`, `low`. |
| Modos do EMS | `conventional`, `rules`, `predictive`. |
| Estados do EMS por regras | `NORMAL`, `ECONOMY`, `CRITICAL`, `PROTECTION`. |
| Estados de segurança | `OK`, `BOOT`, `LVD`, `OVP`, `OC`, `OC_LOCKOUT`, `OT`, `SENSOR`. |

## 2. Tópicos MQTT

Prefixo configurável `comm.mqtt_prefix` (padrão `smartenergy`). QoS 0 para telemetria, QoS 1 para comandos/acks/eventos/decisões.

| Tópico | Direção | Retido | Conteúdo |
|---|---|---|---|
| `smartenergy/telemetry` | dispositivo → servidor | não | Snapshot completo `sem.telemetry.v1` (§3). **Fonte primária para armazenamento.** |
| `smartenergy/pv/voltage` · `/current` · `/power` | → | não | Valor escalar (§4) |
| `smartenergy/battery/voltage` · `/current` · `/soc` | → | não | Valor escalar |
| `smartenergy/load/{1,2,3}/power` · `/state` | → | não | Valor escalar (`state`: 0/1) |
| `smartenergy/ems/mode` | → | sim | `{"value":"rules", ...}` |
| `smartenergy/ems/decision` | → (firmware **e** backend) | não | `sem.decision.v1` (§5) — publicado a cada mudança de decisão |
| `smartenergy/system/status` | → | sim (LWT) | `{"status":"online"|"offline","device_id":...,"boot_id":...,"fw_version":...,"ts_ms":...}` |
| `smartenergy/system/event` | → | não | `sem.event.v1` (§6) |
| `smartenergy/cmd` | servidor → dispositivo | não | Envelope autenticado `sem.cmd.v1` (§7) |
| `smartenergy/cmd/ack` | → | não | `{"id":..., "accepted":bool, "reason":"...", "ts_ms":...}` |

Os tópicos escalares existem para compatibilidade com o documento-base e com
ferramentas genéricas (Grafana/Node-RED). O backend armazena a partir de
`smartenergy/telemetry`.

## 3. Telemetria `sem.telemetry.v1`

Publicada a cada `comm.telemetry_period_s` (5 s). Valores de `meas` são **médias
na janela** `window_ms` sobre `n_samples` amostras (amostragem a
`comm.sample_period_ms`).

```json
{
  "schema": "sem.telemetry.v1",
  "device_id": "sem-esp32-01",
  "fw_version": "0.1.0",
  "source": "hardware",
  "boot_id": 42,
  "seq": 1234,
  "ts_ms": 1763290800000,
  "time_synced": true,
  "uptime_ms": 3600000,
  "window_ms": 5000,
  "n_samples": 50,
  "units": {"v":"V","i":"A","p":"W","e":"Wh","t":"degC","soc":"1","duty":"1"},
  "meas": {
    "pv":    {"v": 18.21, "i": 2.71, "p": 49.35, "ok": true},
    "bat":   {"v": 13.31, "i": -1.20, "p": -15.97, "ok": true},
    "bus":   {"v": 13.30, "i": 1.48, "p": 19.68, "ok": true},
    "t_pv":  {"t": 45.2, "ok": true},
    "t_bat": {"t": 28.1, "ok": true},
    "irr":   {"g": null, "ok": false}
  },
  "energy": {
    "day": "2026-11-16",
    "pv_in": 120.5, "bat_chg": 60.1, "bat_dis": 20.3, "load": 80.2, "unserved": 3.2
  },
  "soc": {"value": 0.734, "unc": 0.05, "valid": true, "last_sync": "full", "since_sync_s": 3600},
  "loads": [
    {"id": 1, "key": "led", "prio": "high",   "demand": true,  "on": true,  "duty": 1.0, "p": 5.1, "p_src": "estimated", "fault": false},
    {"id": 2, "key": "fan", "prio": "medium", "demand": true,  "on": true,  "duty": 0.6, "p": 3.0, "p_src": "estimated", "fault": false},
    {"id": 3, "key": "res", "prio": "low",    "demand": false, "on": false, "duty": 0.0, "p": 0.0, "p_src": "estimated", "fault": false,
     "deferrable": {"e_req_wh": 40.0, "e_done_wh": 12.5, "window_start": "07:00", "deadline": "22:00"}}
  ],
  "ems": {"mode": "rules", "state": "NORMAL", "safety": "OK", "fallback": false, "cmd_age_s": null},
  "faults": []
}
```

Regras:
- `energy.*` em Wh acumulados no **dia local** (zeram à meia-noite local). Sem hora sincronizada, `day=null` e os contadores são desde o boot.
- `energy.unserved`: energia demandada e não fornecida no dia (cargas não deslocáveis; a carga deslocável é contabilizada no fechamento do prazo).
- `loads[].demand`: pedido do perfil programado (para a carga deslocável: `true` enquanto houver energia pendente dentro da janela).
- `loads[].p_src`: `measured` (INA226 dedicado) ou `estimated` (p_nom·(V/v_ref)^k·duty, calibrado no Experimento 2).
- `meas.*.ok=false` → valor ausente/inválido (campo numérico `null`).
- Pacotes do gêmeo digital/simulação usam `source:"simulation"` e PODEM incluir o bloco extra `"sim": {"scenario":..., "strategy":..., "seed":..., "p_pv_avail":..., "soc_true":...}`.

## 4. Valor escalar (tópicos individuais)

```json
{"value": 18.21, "unit": "V", "point": "pv", "ts_ms": 1763290800000, "source": "hardware", "device_id": "sem-esp32-01"}
```

## 5. Decisão `sem.decision.v1`

```json
{
  "schema": "sem.decision.v1",
  "ts_ms": 1763290800000,
  "source": "hardware",
  "origin": "firmware",
  "mode": "rules",
  "state": "ECONOMY",
  "prev_state": "NORMAL",
  "safety": "OK",
  "reason": "soc 0.68 < normal_min_soc 0.70",
  "actions": [{"id": 2, "on": true, "duty": 0.6}],
  "inputs": {"soc": 0.68, "soc_valid": true, "v_bat": 13.12, "p_pv": 0.0, "p_bat": 18.2, "p_bus": 17.4},
  "plan": null
}
```
- `origin` ∈ {`firmware`, `backend`}. Decisões do MPC (backend) incluem `plan`: `{"step_s":900,"t0_ms":...,"soc":[...],"loads":{"1":[...],"2":[...],"3":[...]},"pv_fc_w":[...],"objective":...,"solve_ms":...,"status":"Optimal"}`.

## 6. Evento `sem.event.v1`

```json
{"schema":"sem.event.v1","ts_ms":1763290800000,"source":"hardware","level":"warn","code":"LVD_TRIP","msg":"Desconexao por subtensao: v_bat=11.55 V","data":{"v_bat":11.55}}
```
`level` ∈ {`info`,`warn`,`error`,`critical`}. Códigos mínimos: `BOOT`, `TIME_SYNC`, `WIFI_UP`, `WIFI_DOWN`, `MQTT_UP`, `MQTT_DOWN`, `SENSOR_FAIL`, `SENSOR_OK`, `LVD_TRIP`, `LVD_CLEAR`, `OVP_TRIP`, `OVP_CLEAR`, `OC_TRIP`, `OC_LOCKOUT`, `OT_TRIP`, `OT_CLEAR`, `SOC_SYNC_FULL`, `SOC_SYNC_EMPTY`, `MODE_CHANGE`, `CMD_REJECTED`, `PLAN_EXPIRED`, `FAULTS_RESET`.

## 7. Comandos `sem.cmd.v1` (autenticados)

Envelope publicado em `smartenergy/cmd`:
```json
{"payload": "<string JSON do comando>", "hmac": "<hex minúsculo de HMAC-SHA256(chave, bytes UTF-8 de payload)>"}
```
O HMAC é calculado sobre os **bytes exatos** da string `payload` (evita
canonicalização de JSON). A chave compartilhada fica em
`firmware/include/secrets.h` (não versionado) e na variável de ambiente
`SEM_CMD_KEY` do backend. A autenticação do broker (usuário/senha + ACL do
Mosquitto) é uma camada adicional, não substituta.

Conteúdo de `payload`:
```json
{"schema":"sem.cmd.v1","id":"c-000123","ts_ms":1763290800000,"ttl_s":1200,"type":"set_loads",
 "args":{"loads":[{"id":1,"on":true,"duty":1.0},{"id":2,"on":true,"duty":0.6},{"id":3,"on":false,"duty":0.0}]}}
```

| `type` | `args` | Observações |
|---|---|---|
| `set_mode` | `{"mode":"conventional"|"rules"|"predictive"}` | |
| `set_loads` | `{"loads":[{"id":int,"on":bool,"duty":0..1}]}` | Aceito **apenas** no modo `predictive`. Válido até `ts_ms + ttl_s` (ou `ttl_s` a partir do recebimento se a hora não estiver sincronizada). Ao expirar → evento `PLAN_EXPIRED` e *fallback* para `rules` (`ems.fallback=true`). |
| `sync_soc` | `{"soc":0..1}` | Calibração manual (ex.: após carga completa verificada). |
| `reset_faults` | `{}` | Libera `OC_LOCKOUT`. |

Validação no dispositivo (todas obrigatórias; falha → ack `accepted:false` + evento `CMD_REJECTED`):
1. HMAC correto (comparação em tempo constante).
2. `schema == "sem.cmd.v1"`, `type` conhecido, tipos/faixas de `args` válidos (ids existentes, `0 ≤ duty ≤ 1`, `duty` ≥ `min_duty` quando `on` e a carga for dimerizável; carga não dimerizável só aceita duty 0 ou 1).
3. `id` não presente no cache dos últimos `comm.command_id_cache` ids (anti-replay).
4. Se `time_synced`: `|ts_ms − agora| ≤ command_max_skew_s`.
5. `1 ≤ ttl_s ≤ 3600`.

**Comandos nunca contornam o supervisor de segurança**: a saída final de cada
carga é `comando_EMS AND permitido_pela_segurança`. Em `predictive`, além disso,
`on` de cargas não deslocáveis é combinado com a demanda (`on = cmd.on AND demanda`)
e as salvaguardas do §3.4 de [06-especificacao-algoritmos.md](06-especificacao-algoritmos.md) valem.

## 8. Vocabulário de séries temporais (colunas)

Usado em CSVs de simulação/experimentos, na API `/api/history` e no `demo.json`.

| Coluna | Unidade | Descrição |
|---|---|---|
| `t` | ms (epoch UTC) | instante |
| `pv_v`, `pv_i`, `pv_p` | V, A, W | entrada FV do MPPT (medida) |
| `pv_p_avail` | W | potência FV disponível (simulação: exata; hardware: estimada, requer irradiância) |
| `curtail_p` | W | `max(0, pv_p_avail − pv_p)` |
| `irr_poa` | W/m² | irradiância no plano do módulo |
| `t_amb`, `t_pv`, `t_bat` | °C | temperaturas |
| `bat_v`, `bat_i`, `bat_p` | V, A, W | ramo da bateria (+ = descarga) |
| `bus_v`, `bus_i`, `bus_p` | V, A, W | barramento de cargas |
| `aux_p` | W | consumo auxiliar (ESP32 etc.) |
| `soc` | 1 | SOC **estimado** (o que o EMS usa) |
| `soc_unc` | 1 | incerteza do SOC estimado |
| `soc_true` | 1 | SOC verdadeiro (somente simulação) |
| `l{n}_p` | W | potência da carga n |
| `l{n}_on`, `l{n}_duty` | 0/1, 1 | estado e duty da carga n |
| `l{n}_d` | 0/1 | demanda (pedido) da carga n |
| `unserved_p` | W | potência demandada não atendida (não deslocáveis) |
| `def_done_wh` | Wh | energia já entregue à carga deslocável no dia |
| `mode`, `state`, `safety` | texto | modo/estado do EMS e da segurança |

## 9. API HTTP do backend (FastAPI)

Base `/api`. Todas as respostas JSON incluem `"source"`.

| Método e rota | Resposta |
|---|---|
| `GET /api/meta` | `{source, device_id, fw_version?, connected, last_seen_ms, config:{loads:[...], battery:{...}, pv:{...}, ems:{...}, safety:{...}}}` |
| `GET /api/live` | último `sem.telemetry.v1` (ou `null`) |
| `GET /api/stream` | **SSE**: eventos `telemetry`, `decision`, `event` (dados = JSON dos esquemas acima) |
| `GET /api/history?minutes=1440&step_s=60` | `{source, t:[...], series:{<coluna>:[...]}}` colunar (§8), reamostrado por média |
| `GET /api/energy/daily?days=14` | `{source, days:[{day, pv_in, bat_chg, bat_dis, load, unserved}]}` |
| `GET /api/decisions?limit=100` | `{source, items:[sem.decision.v1...]}` |
| `GET /api/events?limit=100` | `{source, items:[sem.event.v1...]}` |
| `GET /api/forecast` | `{source, method, t:[...], pv_fc_w:[...], load_fc_w:[...], soc_plan:[...]|null}` |
| `GET /api/experiments` | `{items:[{id, scenario, strategy, seed, source, created}]}` |
| `GET /api/experiments/{id}` | `{id, ..., metrics:{...}, series:{t:[...], ...}}` |
| `GET /api/comparison` | igual a `demo.comparison` (§10) |
| `GET /api/export.csv?minutes=1440` | CSV com cabeçalho `coluna[unidade]` e linha de comentário `# source=...` |
| `POST /api/cmd/mode` | corpo `{"mode":...}`; exige `Authorization: Bearer $SEM_API_TOKEN`; publica `set_mode` assinado |

## 10. Pacote estático de demonstração `sem.demo.v1`

Arquivo `dashboard/data/demo.json`, gerado por `python -m smartenergy export-demo`.
Permite abrir o dashboard **sem backend** (modo estático) — sempre rotulado como SIMULADO.

```json
{
  "schema": "sem.demo.v1",
  "generated_at": "2026-11-05T12:00:00Z",
  "source": "simulation",
  "disclaimer": "Dados SIMULADOS — nao sao medicoes de hardware.",
  "meta": { "...": "igual a /api/meta" },
  "live": { "...": "sem.telemetry.v1 com source=simulation" },
  "history": {"t": [], "series": {}},
  "energy_daily": {"days": []},
  "decisions": [],
  "events": [],
  "forecast": {"method": "...", "t": [], "pv_fc_w": [], "load_fc_w": [], "soc_plan": null},
  "comparison": {
    "scenarios": ["ensolarado", "parcialmente_nublado", "nublado", "semana_variavel"],
    "strategies": ["conventional", "rules", "predictive"],
    "seeds": [1, 2, 3],
    "metrics_def": {"<metrica>": {"label": "...", "unit": "...", "better": "lower|higher"}},
    "table": [{"scenario": "...", "strategy": "...", "n": 3, "mean": {"<metrica>": 0.0}, "std": {"<metrica>": 0.0}}],
    "series": {"<scenario>": {"t": [], "<strategy>": {"soc_true": [], "pv_p": [], "pv_p_avail": [], "bus_p": [], "unserved_p": [], "curtail_p": []}}}
  }
}
```

## 11. Interfaces Python (módulos do backend)

```
smartenergy/config.py        load_config(path=None) -> SystemConfig          (dataclasses; lê config/system.yaml)
smartenergy/timeutil.py      parse_hhmm, in_window, demand_at, local tz helpers
smartenergy/models/          solar.py (geometria, céu claro, transposição), pv.py, mppt.py, battery.py, loads.py, weather.py
smartenergy/estimation/soc.py   SocEstimator  (espelho exato do firmware)
smartenergy/ems/base.py      EmsInputs, LoadCommand, Decision, Strategy (protocolo)
smartenergy/ems/safety.py    SafetySupervisor (espelho do firmware: LVD/SOC mínimo/religamento)
smartenergy/ems/conventional.py, rules.py (espelho exato do firmware), predictive.py (MPC/MILP)
smartenergy/forecast/        perfect, noisy, clearsky_persistence, open_meteo, demand
smartenergy/simulation/engine.py   run_simulation(cfg, scenario, strategy, seed, **overrides) -> SimResult(df, metrics, decisions, events, meta)
smartenergy/simulation/telemetry.py row_to_telemetry(row, cfg, seq, ...) -> dict sem.telemetry.v1
smartenergy/metrics/indicators.py  compute_metrics(df, cfg, dt_s) -> dict
smartenergy/experiments/     runner (comparação), export_demo
smartenergy/security/hmac_cmd.py   sign_command(payload: dict, key: bytes) -> envelope; verify_envelope(...)
smartenergy/storage/         sqlite_store (padrão), influx_store (opcional)
smartenergy/ingest/          mqtt_ingest (paho-mqtt)
smartenergy/api/             app (FastAPI), SSE
smartenergy/twin/            dispositivo virtual: publica telemetria simulada via MQTT no MESMO formato do ESP32
smartenergy/cli.py           python -m smartenergy <simulate|compare|export-demo|serve|ingest|twin|sign-cmd>
```
