# Firmware ESP32 — Smart Energy Manager

Firmware do nó de medição e controle da microrrede CC de bancada (painel FV 50 W →
MPPT comercial → bateria LiFePO4 12,8 V 6 Ah com BMS → 3 cargas CC 12 V chaveadas por
MOSFET). PlatformIO, framework Arduino (core Arduino-ESP32 2.0.x), C++17.

Contratos normativos seguidos: [`docs/05-contrato-de-interfaces.md`](../docs/05-contrato-de-interfaces.md)
(MQTT, payloads, comandos) e [`docs/06-especificacao-algoritmos.md`](../docs/06-especificacao-algoritmos.md)
(SOC, segurança, estratégias, energia). Parâmetros: [`config/system.yaml`](../config/system.yaml)
(fonte única), convertidos em `include/system_params.h` por `tools/gen_firmware_params.py`.

## Estrutura

```
firmware/
├── platformio.ini            env:esp32dev (espressif32 @ 6.9.0) e env:native (testes)
├── include/
│   ├── config.h              constantes de compilação (tarefas, filas, MQTT, timeouts)
│   ├── system_params.h       GERADO de config/system.yaml — não editar
│   └── secrets.h.example     modelo de credenciais (copiar para secrets.h)
├── lib/sem_core/             lógica PURA (sem Arduino), testável no PC, tudo em double
│   ├── soc_estimator         §1  contagem de Coulomb + sincronizações + persistência
│   ├── safety_supervisor     §2  BOOT, LVD, OVP, OC (retry/lockout), OT, SENSOR, prioridade
│   ├── rules_engine          §3.2 histerese por faixas, ações por estado, deslocável com latch
│   ├── strategies            §3.1 convencional, §3.4 guarda preditiva/TTL, perfil de demanda
│   ├── ems_controller        orquestra modos (convencional/regras/preditivo + fallback)
│   ├── energy                §4  integrador trapezoidal, potência estimada, LoadManager
│   ├── command_validator     docs/05 §7: HMAC (interface), schema, faixas, replay, skew, TTL
│   └── types/params/events/time_util   vocabulário do contrato, structs de parâmetros
├── src/                      camada dependente de Arduino/ESP-IDF
│   ├── main.cpp              saídas em LOW primeiro; cria as tarefas
│   ├── app/                  control_task, comms_task, filas/mutex, watchdog IDF4/IDF5
│   ├── hw/                   INA226 (driver próprio), DS18B20, DS3231, saídas/PWM, painel, stub UART MPPT
│   ├── comms/                Wi-Fi/SNTP/MQTT, montagem dos JSON do contrato
│   ├── crypto/               HMAC-SHA256 via mbedtls
│   └── storage/              NVS (Preferences): boot_id, SOC, contadores
├── test/                     testes nativos (Unity) + vetores golden
│   ├── test_*/               uma suíte por pasta (padrão PlatformIO)
│   ├── support/              utilidades header-only (HMAC de software, JSON mínimo, fixtures)
│   └── vectors/              sample_vectors.json (à mão) e rules_vectors.json (gerado pelo Python)
└── tools/
    ├── run_native_tests.sh   testes nativos com g++ (-Werror) sem PlatformIO
    ├── host_compile_check.sh verificação de compilação de src/ com stubs + validação de payloads
    ├── host_payload_check.cpp
    └── host_stubs/           cabeçalhos mínimos (Arduino, Wire, WiFi, FreeRTOS, mbedtls, ...)
```

## Arquitetura de tarefas

```
                 núcleo 1 (prioridade 10)                         núcleo 0 (prioridade 3)
 ┌──────────────────────────────────────────────┐     ┌──────────────────────────────────────┐
 │ control_task — período 100 ms (vTaskDelayUntil)│     │ comms_task — laço de 20 ms           │
 │  1. ALERT (GPIO 34) + INA226 pv/bat/bus (I2C) │     │  Wi-Fi c/ backoff, SNTP "<-03>3",    │
 │     DS18B20 assíncrono, botão de reset        │     │  MQTT (LWT retido), reconexão        │
 │  2. aplica comandos validados (fila)          │◄────┤  valida comandos (HMAC, schema,      │
 │  3. SOC (§1)                                  │ cmd │  replay, skew, TTL) -> ack + fila    │
 │  4. SEGURANÇA (§2) — toda amostra             │     │                                      │
 │  5. EMS (modo atual) — a cada 1 s             │────►│  publica eventos, decisões,          │
 │  6. saídas: on = on_EMS AND allowed           │ev/de│  telemetria (+ tópicos escalares),   │
 │  7. energia (§4), janela de telemetria (5 s)  │tel. │  status e modo (retidos)             │
 │  8. NVS (regra §1), RTC, LED, watchdog (5 s)  │     │                                      │
 └──────────────────────────────────────────────┘     └──────────────────────────────────────┘
```

* A troca de dados entre tarefas é feita **somente** por filas FreeRTOS (timeout zero do lado
  do controle; telemetria em fila de 1 posição com `xQueueOverwrite`) e por um estado
  compartilhado protegido por mutex (`app/app_context.h`). **A perda de Wi-Fi/MQTT nunca
  bloqueia o controle nem a segurança**: eventos excedentes são descartados e a telemetria
  antiga é sobrescrita.
* Watchdog de tarefa (TWDT) na `control_task` (5 s, pânico ⇒ reinício), com API compatível
  com ESP-IDF 4.x (core 2.x) e 5.x (core 3.x) — `app/wdt_compat.cpp`.
* Boot: (1) todos os pinos de carga em `LOW` **antes de qualquer outra coisa** (o pull-down
  externo cobre o intervalo até ali); (2) parâmetros, filas; (3) `control_task`; (4) `comms_task`.
  O supervisor começa em `BOOT` e só libera cargas após `boot_settle_samples` amostras válidas.

## Mapa de pinos (de `config/system.yaml` → `include/system_params.h`)

| Função | GPIO | Observação |
|---|---|---|
| I2C SDA / SCL | 21 / 22 | INA226 pv 0x40, bat 0x41, bus 0x44; RTC DS3231 0x68 (opcional) |
| OneWire | 4 | DS18B20: índice 0 = bateria, 1 = módulo FV (**PENDENTE** fixar endereços ROM) |
| Cargas led / fan / res | 25 / 26 / 27 | HIGH = ligada; pull-down externo; `fan` em PWM LEDC 100 Hz |
| ALERT do INA226 `bus` | 34 | entrada, pull-up externo 10 k, ativo baixo |
| Botão de reset de falhas | 35 | entrada, pull-up externo 10 k; manter 2 s pressionado |
| LED de status | 2 | ver padrões em `src/hw/panel.h` |
| UART2 RX / TX | 16 / 17 | reservada p/ VE.Direct ou RS-485 — **stub**, não implementado |

Corrente das cargas: não há sensor por carga; a potência é **estimada**
`p = p_nom·(V/v_ref)^k·duty` e publicada com `p_src = "estimated"`.

## Compilar e gravar (PlatformIO)

```bash
pip install platformio                     # ou extensão PlatformIO do VS Code
python tools/gen_firmware_params.py        # (na raiz do repo) após mudar config/system.yaml
cp firmware/include/secrets.h.example firmware/include/secrets.h   # e edite
cd firmware
pio run -e esp32dev                        # compila
pio run -e esp32dev -t upload              # grava via USB
pio device monitor -e esp32dev             # 115200 baud
```

`python tools/gen_firmware_params.py --check` falha se `system_params.h` estiver desatualizado
em relação ao YAML (útil em CI).

### Credenciais (`include/secrets.h`, não versionado)

`WIFI_SSID`, `WIFI_PASS`, `MQTT_HOST`, `MQTT_PORT`, `MQTT_USER`, `MQTT_PASS`, `CMD_HMAC_KEY`.
`CMD_HMAC_KEY` deve ser igual à variável `SEM_CMD_KEY` do backend. Sem `secrets.h` a compilação
para com uma mensagem explicando o que fazer.

## MQTT (prefixo `smartenergy`)

Publica: `telemetry` (5 s, `sem.telemetry.v1`, `source:"hardware"`), `pv/{voltage,current,power}`,
`battery/{voltage,current,soc}`, `load/{1,2,3}/{power,state}`, `ems/decision` (a cada mudança),
`ems/mode` (retido), `system/event`, `system/status` (retido; LWT `offline`), `cmd/ack`.
Assina: `cmd` (envelope `{"payload","hmac"}`; `set_mode`, `set_loads` só em `predictive`,
`sync_soc`, `reset_faults`). Comandos nunca contornam a segurança: saída = comando AND permitido.

## Testes e verificações

| Comando | O que faz |
|---|---|
| `firmware/tools/run_native_tests.sh` | Clona Unity v2.6.0 e ArduinoJson v7.2.1 em `build_host/` (gitignored) e compila `lib/sem_core` + `test/test_*` com `g++ -std=c++17 -Wall -Wextra -Werror`; executa todas as suítes. |
| `pio test -e native` | Mesmas suítes via PlatformIO (se instalado). |
| `firmware/tools/host_compile_check.sh` | Compila **todos** os `.cpp` de `src/` com stubs (`tools/host_stubs/`) + PubSubClient v2.8 e ArduinoJson v7.2.1 reais, duas vezes (core 2.x/IDF 4 e core 3.x/IDF 5), com `-Werror`; gera os payloads com `src/comms/telemetry.cpp` e valida contra `schemas/*.json`. |
| `python -m smartenergy gen-vectors` | (backend) regenera `test/vectors/rules_vectors.json`. |

Suítes nativas: `test_soc` (descarga/carga/eficiência/zona morta/sincronizações/incerteza/sensor
inválido/persistência), `test_safety` (cada proteção, atrasos, religamento, retry, lockout, reset,
prioridade), `test_rules` (histerese nas duas direções, ações por estado, deslocável: latch,
excedente, economia, prazo, janela, permanência), `test_strategies` (perfil com cruzamento de
meia-noite, convencional, guarda preditiva/TTL, troca de modo), `test_validator` (HMAC com
verificador de software validado pelo RFC 4231, schema, faixas, `min_duty`, replay, skew, TTL),
`test_energy` (trapézio, separação carga/descarga, virada do dia, potência estimada, LoadManager,
calendário), `test_vectors` (vetores golden).

**Vetores golden** (`sem.vectors.v1`): o harness lê `params` (regras, cargas, SOC, segurança) e
reproduz as sequências de `rules`, `soc` e `safety`, comparando estados/códigos/bools exatamente,
números com tolerância 1e-9 (duty 1e-6) e, quando presente, a sequência de códigos de `events`.
`sample_vectors.json` (pequeno, escrito à mão) valida o próprio harness; se `rules_vectors.json`
não existir, o teste é ignorado com aviso. O parser JSON dos vetores é próprio
(`test/support/mini_json.h`, `strtod`): o ArduinoJson 7.2 armazena decimais curtos como `float`
(0.3 → 0.30000001), o que quebraria a tolerância de 1e-9.

## Decisões de implementação (onde o texto normativo é omisso)

Todas iguais ao espelho Python (verificadas pelos vetores golden):

* Temporizadores acumulam `dt` e comparam literalmente (sem epsilon): com `dt = 0,1 s`,
  50 amostras somam 4,999… s, então um atraso de 5 s dispara na 51ª amostra.
* SOC: amostra inválida zera `t_full/t_empty`; no retorno do sensor `unc_base = min(0,5, unc_base + 0,05)`;
  `since_sync_s` só avança em amostras válidas.
* Regras: SOC inválido já na 1ª avaliação ⇒ `CRITICAL`; `t_restante` calculado módulo 24 h.
* Segurança: OVP/OC com medida ausente zeram o temporizador; OC: trips ≥ `overcurrent_max_retries`
  ⇒ `OC_LOCKOUT`; contagem zera após 10 min acumulados com saída habilitada e sem disparo;
  evento extra `OC_RETRY`; `FAULTS_RESET` sai na amostra seguinte ao reset. O pino ALERT do
  INA226 conta como sobrecorrente (redundância de hardware, só no firmware).
* Preditivo: carga ausente do último `set_loads` ⇒ desligada; SOC inválido tratado como baixo.
* Comandos: o `id` entra no cache anti-replay assim que passa HMAC + schema (mesmo se rejeitado
  depois por skew/TTL/modo); `duty`/`soc` arredondados a 1e-6 (precisão `float` do ArduinoJson).
* Sem hora válida (sem SNTP e sem RTC): o perfil de demanda não é avaliado ⇒ nenhuma carga é
  pedida (falha `NO_TIME` na telemetria); contadores de energia "desde o boot" (`day = null`).

## Limitações — o que foi e o que NÃO foi verificado

Verificado neste ambiente (PC, sem hardware):

* lógica pura (`lib/sem_core`): suítes nativas e vetores golden gerados pelo backend Python;
* compilação de todos os `.cpp` de `src/` contra **stubs** (core 2.x e 3.x) e PubSubClient/ArduinoJson reais;
* formato dos payloads (telemetria, decisão, evento) contra `schemas/` e tamanho da telemetria
  (~1,4 KB no pior caso) frente ao buffer MQTT (2,5 KB);
* conversões do INA226 (configuração 0x4527, CAL, limite de ALERT) por `static_assert`.

**NÃO** verificado (exige o toolchain e/ou a bancada):

* compilação com o toolchain real (`pio run -e esp32dev`): o registry do PlatformIO estava
  inacessível; os stubs não substituem os cabeçalhos reais;
* comportamento em hardware: I2C/INA226, DS18B20, DS3231, LEDC, NVS, Wi-Fi/MQTT/SNTP,
  temporização das tarefas, watchdog, consumo de pilha (`uxTaskGetStackHighWaterMark`);
* calibração dos sensores e das potências nominais das cargas (valores **PENDENTE** no YAML).

Outras limitações conhecidas:

* PubSubClient publica só com QoS 0 (o contrato pede QoS 1 para acks/eventos/decisões); a
  assinatura de `cmd` usa QoS 1. Alternativa futura: `esp-mqtt` do ESP-IDF.
* MQTT sem TLS (rede local da bancada); a autenticidade dos comandos vem do HMAC.
* Interface serial com o MPPT (VE.Direct/RS-485) é apenas um stub documentado.
* Sem OTA.
