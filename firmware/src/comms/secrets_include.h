// =============================================================================
// src/comms/secrets_include.h — inclui as credenciais (include/secrets.h, NÃO
// versionado). Sem o arquivo a compilação falha com instrução clara.
// SEM_ALLOW_EXAMPLE_SECRETS (somente verificação de compilação no PC,
// tools/host_compile_check.sh) usa o modelo secrets.h.example.
// =============================================================================
#pragma once

#if __has_include("secrets.h")
#include "secrets.h"
#elif defined(SEM_ALLOW_EXAMPLE_SECRETS)
#include "secrets.h.example"
#else
#error "Crie firmware/include/secrets.h a partir de firmware/include/secrets.h.example"
#endif

#if !defined(WIFI_SSID) || !defined(WIFI_PASS) || !defined(MQTT_HOST) || !defined(MQTT_PORT) || \
    !defined(MQTT_USER) || !defined(MQTT_PASS) || !defined(CMD_HMAC_KEY)
#error "secrets.h incompleto: defina WIFI_SSID, WIFI_PASS, MQTT_HOST/PORT/USER/PASS e CMD_HMAC_KEY"
#endif
