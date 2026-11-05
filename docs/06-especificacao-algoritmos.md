# 06 — Especificação dos algoritmos (NORMATIVO)

> O firmware (C++) e o backend/simulador (Python) implementam **os mesmos**
> algoritmos descritos aqui. A equivalência é verificada por vetores de teste
> *golden* gerados em Python e reproduzidos no teste nativo do firmware
> (`firmware/test/`), ver §7. Parâmetros: `config/system.yaml`.

Notação: `dt` = passo de avaliação em segundos; `tod` = segundos desde a
meia-noite local; intervalo `[início, fim)` em hora local, com `início ≤ tod < fim`
(se `fim < início`, o intervalo cruza a meia-noite). `I` = corrente da bateria
(**+ descarga**). `C` = `battery.capacity_ah`.

---

## 1. Estimador de SOC (contagem de Coulomb + sincronizações)

Estado: `soc`, `unc_base`, `ah_since_sync`, `last_sync` ∈ {`none`,`ocv_init`,`persisted`,`full`,`empty`,`manual`}, `t_full`, `t_empty`, `valid`.

**Inicialização** (nesta ordem):
1. Se houver valor persistido (NVS / estado anterior): `soc = persisted`, `unc_base = persisted_unc`, `last_sync = persisted`.
2. Senão: `soc = OCV⁻¹(V)` por interpolação linear da curva `battery.ocv_curve` (baixa confiança), `unc_base = unknown_initial_uncertainty`, `last_sync = ocv_init`.
3. `valid = true`, `ah_since_sync = 0`.

**Atualização** a cada amostra (`dt`, `I`, `V`, sensor válido):
```
I_eff = 0                       se |I| < current_deadband_a
        I                       caso contrário
se I_eff > 0:  soc -= I_eff·dt / (3600·C)                       # descarga
senão:         soc += η_c · (−I_eff)·dt / (3600·C)               # carga (η_c = coulombic_efficiency)
ah_since_sync += |I_eff|·dt/3600
soc = clamp(soc, 0, 1)

# sincronização de cheio
se V ≥ full_sync.voltage_v  e  |I| ≤ full_sync.tail_current_a:  t_full += dt  senão t_full = 0
se t_full ≥ full_sync.hold_s:  soc = 1; unc_base = sync_uncertainty; ah_since_sync = 0; last_sync = full
                               (t_full continua acumulando; ressincroniza enquanto a condição persistir; evento SOC_SYNC_FULL só na borda)

# sincronização de vazio (tensão compensada pela queda resistiva)
Vc = V + I·r_internal_ohm
se Vc ≤ empty_sync.voltage_v:  t_empty += dt  senão t_empty = 0
se t_empty ≥ empty_sync.hold_s:  soc = min(soc, empty_sync.soc_value); unc_base = sync_uncertainty; ah_since_sync = 0; last_sync = empty

unc = min(0.5, unc_base + drift_uncertainty_per_ah · ah_since_sync)
```
- Sensor da bateria inválido por mais de `safety.sensor_timeout_s` → integração suspensa e `valid = false` (volta a `true` quando o sensor retorna, com `unc_base += 0.05`).
- `sync_soc` (comando) → `soc = valor`, `unc_base = sync_uncertainty`, `last_sync = manual`.
- Persistência (firmware): grava `soc` e `unc` a cada `persist_every_s` **ou** quando `|soc − soc_gravado| ≥ persist_delta_soc` (limita desgaste da flash).

---

## 2. Supervisor de segurança (sempre ativo, todos os modos)

Executado a cada amostra (firmware: 100 ms; simulação: `dt`). Saída: `allowed`
(booleano global) e `safety` (código). A saída física de cada carga é
`on_final = on_EMS AND allowed`.

`V_bat` = tensão do sensor `bat`; se inválido, usa `bus.v` como substituto; se
ambos inválidos → `SENSOR` (tudo desligado).

| Proteção | Disparo | Liberação |
|---|---|---|
| `BOOT` | ao iniciar | após `boot_settle_samples` amostras válidas |
| `LVD` | `V_bat < lvd_v` por ≥ `lvd_delay_s` **ou** (`soc.valid` e `soc < soc_hard_min`) | `V_bat ≥ reconnect_v` **e** (`!soc.valid` ou `soc ≥ reconnect_min_soc`) |
| `OVP` | `bus.v > bus_ovp_v` por ≥ `bus_ovp_delay_s` | `bus.v < bus_ovp_v − 0,3 V` por ≥ `bus_ovp_delay_s` |
| `OC` | `bus.i > bus_overcurrent_a` por ≥ `bus_overcurrent_delay_ms` | nova tentativa após `overcurrent_retry_s`; após `overcurrent_max_retries` disparos sem 10 min livres → `OC_LOCKOUT` (só `reset_faults`/botão) |
| `OT` | `t_bat > battery_overtemp_c` | `t_bat < battery_overtemp_release_c` |
| `SENSOR` | `bat` e `bus` inválidos > `sensor_timeout_s` | algum volta a ser válido |

Prioridade do código reportado quando há mais de uma ativa:
`OC_LOCKOUT > OC > OVP > OT > SENSOR > LVD > BOOT > OK`.
Qualquer proteção ativa ⇒ `allowed = false` (todas as cargas experimentais desligadas;
o ESP32 permanece alimentado). O simulador modela `BOOT` (desativado), `LVD` e `SOC`;
`OVP/OC/OT/SENSOR` são testados apenas no firmware (testes nativos).

---

## 3. Estratégias de gerenciamento

Entradas comuns (`EmsInputs`): `tod`, `soc`, `soc_valid`, `v_bat`, `i_bat`,
`p_bat` (+ descarga), `p_pv`, `p_bus`, demanda `d_k` por carga (do perfil
`demand.schedule`), estado da carga deslocável (`e_done_wh` hoje), saídas atuais
e instante da última comutação de cada carga.

Saída (`Decision`): para cada carga `{id, on, duty}`, `state`, `reason`.
`duty = 1` para cargas não dimerizáveis ligadas; `duty = 0` quando desligada.

### 3.1 Operação convencional (referência)
- `led`: `on = d_led`.
- `fan`: `on = d_fan`, `duty = 1`.
- `res` (deslocável): `on = (tod ∈ demand.conventional.res_schedule) AND (e_done_wh < daily_energy_wh)`.
- `state = "NORMAL"` (fixo, apenas informativo). Somente o supervisor de segurança atua (equivalente à saída LOAD com LVD de controladores comerciais).

### 3.2 Controle baseado em regras (histerese por faixas de SOC)
Limites inferiores: `T3 = normal_min_soc` (NORMAL), `T2 = economy_min_soc` (ECONOMY),
`T1 = critical_min_soc` (CRITICAL), `T0 = −∞` (PROTECTION). `H = hysteresis_soc`.
Nível: NORMAL=3, ECONOMY=2, CRITICAL=1, PROTECTION=0.

```
primeira avaliação:  nível = maior L tal que soc ≥ T_L         (sem histerese)
avaliações seguintes:
   se !soc_valid:  nível = min(nível, 1); motivo "soc_invalid"   (CRITICAL; LVD continua atuando pela tensão)
   senão:
      enquanto nível > 0 e soc < T_nível:          nível -= 1    (descida imediata)
      enquanto nível < 3 e soc ≥ T_(nível+1) + H:  nível += 1    (subida exige histerese)
```

Ações por estado (cargas não deslocáveis):

| Estado | high (`led`) | medium (`fan`) | low não deslocável |
|---|---|---|---|
| NORMAL | `on = d` | `on = d`, `duty = 1` | `on = d` |
| ECONOMY | `on = d` | `on = d`, `duty = economy_medium_duty` (≥ `min_duty`) | desligada |
| CRITICAL | `on = d` | desligada | desligada |
| PROTECTION | desligada | desligada | desligada |

Carga deslocável (`res`), com `latch` persistente entre avaliações:
```
restante = max(0, daily_energy_wh − e_done_wh)
na_janela = tod ∈ [window_start, deadline)
se restante == 0  ou  !na_janela  ou  estado ∈ {CRITICAL, PROTECTION}:
      quer = false; latch = false
senão se estado == NORMAL:
      se soc ≥ surplus_on_soc:  latch = true
      se soc <  surplus_off_soc: latch = false
      t_necessario = restante / p_nom_w · 3600
      t_restante   = deadline − tod
      forçar_prazo = t_restante ≤ t_necessario + deadline_margin_s
      quer = latch OR forçar_prazo
senão (ECONOMY):
      se carga desligada:  latch = (−p_bat ≥ p_nom_w + economy_surplus_margin_w)   # bateria recebendo excedente suficiente
      se carga ligada:     latch = (p_bat ≤ economy_surplus_margin_w)               # desliga se a bateria passar a descarregar
      quer = latch
```
Tempo mínimo de permanência: uma mudança `on↔off` **da carga deslocável**
motivada por `quer` só é aplicada se `agora − última_comutação ≥ min_dwell_s`;
desligamentos por `restante == 0`, saída da janela, estado CRITICAL/PROTECTION ou
segurança são imediatos. Cargas não deslocáveis seguem demanda+estado (a
histerese do estado evita oscilação).

`reason` deve citar a regra aplicada (ex.: `"soc 0.68 < 0.70 -> ECONOMY"`,
`"res: excedente soc>=0.90"`, `"res: prazo"`).

### 3.3 Gerenciamento preditivo (MPC com horizonte deslizante)
A cada `replan_s` (15 min), alinhado à grade de `step_s`:

**Dados**: `N = horizon_steps`, `Δ = step_s/3600` h. Previsões por passo `k`:
`P̂pv[k]` (W disponíveis na saída do MPPT = previsão FV × `mppt.efficiency`),
`d_led[k]`, `d_fan[k]` ∈ [0,1] (fração do passo com pedido), `P_aux`.
`E0 = soc·E_nom` com `E_nom = battery.nominal_v · capacity_ah` (Wh).

**Variáveis**: `u1[k]∈{0,1}` (led), `z2[k]∈{0,1}` e `u2[k]∈[0,1]` (fan, semicontínua:
`min_duty·z2 ≤ u2 ≤ z2`), `u3[k]∈{0,1}` (res), `pc[k], pd[k] ≥ 0` (carga/descarga, W),
`pcu[k] ≥ 0` (corte FV), `e[k]` (Wh, k=0..N), `s_j[k] ≥ 0` (comutações),
`sh ≥ 0` (déficit da deslocável), `v[k] ≥ 0` (violação de SOC mínimo).

**Restrições**:
```
u1[k] ≤ ceil(d_led[k]);   u2[k] ≤ d_fan[k] (z2[k] ≤ ceil(d_fan[k]))
u3[k] = 0 fora de [window_start, deadline)
P̂pv[k] − pcu[k] + pd[k] = pc[k] + P1·u1[k] + P2·u2[k] + P3·u3[k] + P_aux        (balanço)
0 ≤ pcu[k] ≤ P̂pv[k]
pc[k] ≤ V_nom·mppt.max_charge_current_a ;   pd[k] ≤ V_nom·battery.max_discharge_current_a
e[k+1] = e[k] + (η_c·pc[k] − pd[k]/η_d)·Δ        (η_d = 0,98; η_c = coulombic_efficiency)
e[0] = E0 ;   e[k] ≤ soc_max·E_nom ;   e[k] ≥ soc_min·E_nom − v[k]
s_j[k] ≥ u_j[k] − u_j[k−1],  s_j[k] ≥ u_j[k−1] − u_j[k]     (j ∈ {1,z2,3}; u_j[−1] = estado atual)
sh ≥ restante_res − Σ_{k na janela de hoje} P3·u3[k]·Δ
```
**Objetivo** (generalização da função J da seção 9.4 do documento-base com pesos por prioridade):
```
min  Σ_k [ α_high·P1·(d_led[k] − u1[k])·Δ + α_med·P2·(d_fan[k] − u2[k])·Δ
          + β·pcu[k]·Δ + γ·(pc[k] + pd[k])·Δ + δ·Σ_j s_j[k] + w_soc·v[k] ]
     + α_low·sh − λ·e[N]
```
(`α_high, α_med, α_low, β, γ, δ, λ, w_soc` = `ems.predictive.weights`.)
Se a janela da deslocável de hoje já terminou, o déficit de amanhã entra com o
requisito completo e a janela de amanhã (dentro do horizonte).

**Aplicação**: aplica-se apenas o primeiro passo (`u1[0], u2[0], u3[0]`). Solver
HiGHS via PuLP, limite `time_limit_s`, `mip_gap`. Se o solver falhar ou não
houver solução → *fallback* para regras (registrar `status`).

### 3.4 Salvaguardas no modo preditivo (firmware e simulação)
Mesmo com comando válido do MPC:
- cargas não deslocáveis: `on = cmd.on AND d_k` (nunca liga sem pedido);
- `soc < critical_min_soc` → medium e low desligadas; `soc < soc_hard_min` → LVD (segurança);
- comando expirado (TTL) ou ausente → regras (`fallback = true`).

---

## 4. Integração de energia

Trapezoidal por amostra: `E += (P_k + P_{k−1})/2 · dt / 3600` (Wh), separando
`bat_chg` (P<0) e `bat_dis` (P>0) pelo sinal de cada amostra. Contadores do dia
zeram à meia-noite local.

---

## 5. Indicadores de desempenho (simulação e experimentos)

Sobre a série com passo `dt` (h = dt/3600):

| Indicador | Definição |
|---|---|
| `e_pv_wh` | Σ pv_p·h (energia FV colhida na entrada do MPPT) |
| `e_pv_avail_wh` | Σ pv_p_avail·h (simulação: exata; hardware: estimada) |
| `e_curtail_wh` | Σ max(0, pv_p_avail − pv_p)·h — energia FV disponível não aproveitada (limitação por bateria cheia/corrente máx.) |
| `curtail_frac` | e_curtail_wh / e_pv_avail_wh |
| `e_demand_wh` | Σ_k Σ_t d_k·P_k(V)·h (não deslocáveis) + Σ_dias daily_energy_wh (deslocável) |
| `e_served_wh` | energia efetivamente entregue às cargas experimentais |
| `e_unserved_wh` | Σ max(0, d_k·P_k(V) − p_k)·h (não deslocáveis) + Σ_dias max(0, req − entregue no prazo) |
| `e_unserved_{high,medium,low}_wh` | por classe de prioridade |
| `service_rate` (e por classe) | 1 − e_unserved / e_demand |
| `e_bat_chg_wh`, `e_bat_dis_wh` | Σ max(0,−bat_p)·h, Σ max(0,bat_p)·h |
| `bat_throughput_wh`, `equiv_cycles` | chg + dis; throughput / (2·E_nom) |
| `soc_min`, `max_dod` | mínimo de soc_true; 1 − soc_min |
| `delta_soc` | soc_true final − inicial (diferenças de SOC final devem ser consideradas na comparação) |
| `n_switch` (e por carga) | número de transições on↔off |
| `lvd_events`, `time_in_lvd_h` | disparos e tempo em LVD |
| `e_aux_wh` | Σ aux_p·h |
| `balance_error_wh` | fechamento do balanço energético do simulador (deve ser ≈ 0) |
| Previsão | `mae_w`, `rmse_w` (passos diurnos), `mape` apenas onde real ≥ 5 % de `p_stc_w` |

Comparações entre estratégias usam **os mesmos** perfis de geração e demanda
(mesma semente → mesmos números aleatórios) e o mesmo SOC inicial.

---

## 6. Modelos do simulador (resumo; detalhes em `docs/07-simulacao.md`)
- Posição solar (NOAA/Spencer), céu claro de Haurwitz, decomposição de Erbs, transposição isotrópica para o plano (inclinação/azimute de `pv`).
- Índice de céu claro estocástico por regime (`clear`, `partly`, `overcast`, `markov` diário) com AR(1) por minuto; semente controla tudo.
- FV: `P = p_stc · (G_poa/1000) · (1 + γ·(Tc − 25)) · derate`, `Tc = Ta + (NOCT − 20)/800 · G_poa`.
- MPPT: `P_out = efficiency · P_in − self_consumption` (quando há FV), limitado pela aceitação de carga da bateria + cargas → excedente vira `curtail`.
- Bateria: OCV(SOC) + `r_internal`; carga CC-CV aproximada: acima de SOC 0,95 a aceitação de corrente decai linearmente até 0 em SOC 1, com tensão terminal regulada em `mppt.absorption_v`; BMS corta abaixo de `bms_uvp_v`.
- Cargas: `P = p_nom · (V/v_ref)^k · duty`.
- Medições simuladas com erros de `simulation.sensor_error`; o EMS usa o SOC **estimado** (§1); métricas usam o **verdadeiro**.

---

## 7. Vetores *golden* de equivalência firmware ↔ Python
Arquivo `firmware/test/vectors/rules_vectors.json` (gerado por
`python -m smartenergy gen-vectors`): sequência de entradas (`tod`, `soc`,
`soc_valid`, `p_bat`, `d_led`, `d_fan`, `e_done_wh`, `t_s`) e saídas esperadas
(`state`, `on/duty` por carga) para os módulos de regras, SOC e segurança. O
teste nativo C++ reproduz a sequência e exige igualdade (duty com tolerância 1e‑6).
