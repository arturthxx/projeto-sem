#!/usr/bin/env sh
# Gera infra/mosquitto/passwd (NAO versionado) com os usuarios da ACL.
# As senhas vem do ambiente (ou de infra/.env); o arquivo guarda apenas hashes.
# Requer Docker (usa o mosquitto_passwd da propria imagem eclipse-mosquitto:2).
# NAO EXECUTADO no ambiente de desenvolvimento (sem Docker).
set -eu
cd "$(dirname "$0")"
if [ -f ../.env ]; then . ../.env; fi
: "${MQTT_DEVICE_PASSWORD:?defina MQTT_DEVICE_PASSWORD}"
: "${MQTT_BACKEND_PASSWORD:?defina MQTT_BACKEND_PASSWORD}"
: "${MQTT_TWIN_PASSWORD:?defina MQTT_TWIN_PASSWORD}"
: "${MQTT_HEALTH_PASSWORD:?defina MQTT_HEALTH_PASSWORD}"
: > passwd
add_user() { docker run --rm -v "$PWD:/m" eclipse-mosquitto:2 mosquitto_passwd -b /m/passwd "$1" "$2"; }
add_user sem-device  "$MQTT_DEVICE_PASSWORD"
add_user sem-backend "$MQTT_BACKEND_PASSWORD"
add_user sem-twin    "$MQTT_TWIN_PASSWORD"
add_user sem-monitor "$MQTT_HEALTH_PASSWORD"
chmod 600 passwd
echo "mosquitto/passwd gerado (4 usuarios)."
