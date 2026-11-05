#!/usr/bin/env bash
# =============================================================================
# host_compile_check.sh — VERIFICAÇÃO DE COMPILAÇÃO no PC de TODOS os .cpp de
# firmware/src/ (código dependente de Arduino/ESP-IDF), usando:
#   * stubs mínimos com as assinaturas reais (firmware/tools/host_stubs/);
#   * PubSubClient v2.8 e ArduinoJson v7.2.1 REAIS (clonados em build_host/);
#   * g++ -std=gnu++17 -Wall -Wextra -Werror, duas vezes: core Arduino-ESP32
#     2.x (IDF 4.4) e 3.x (IDF 5) — exercita os dois ramos de #if.
# Depois compila e executa tools/host_payload_check.cpp, que gera os payloads
# do contrato com src/comms/telemetry.cpp e os valida contra schemas/*.json
# (jsonschema do venv do backend, se disponível).
#
# IMPORTANTE: isto pega erros de sintaxe/tipo, mas NÃO substitui compilar com
# o toolchain real do ESP32 (`pio run -e esp32dev`): os stubs não reproduzem
# todos os cabeçalhos, macros e comportamentos do core.
# =============================================================================
set -euo pipefail

FW="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "$FW/.." && pwd)"
BH="$FW/build_host"
OUT="$BH/hostcheck"
CXX="${CXX:-g++}"
mkdir -p "$OUT"

clone() {  # clone <url> <tag> <dir>
  local url="$1" tag="$2" dir="$BH/$3"
  if [ ! -f "$dir/.sem_tag" ] || [ "$(cat "$dir/.sem_tag")" != "$tag" ]; then
    rm -rf "$dir"
    git -c advice.detachedHead=false clone --quiet --depth 1 -b "$tag" "$url" "$dir"
    echo "$tag" > "$dir/.sem_tag"
  fi
}
clone https://github.com/knolleary/pubsubclient v2.8 pubsubclient
clone https://github.com/bblanchon/ArduinoJson v7.2.1 arduinojson

INC=(-I"$FW/tools/host_stubs" -I"$FW/include" -I"$FW/src" -I"$FW/lib/sem_core/include"
     -I"$BH/arduinojson/src" -I"$BH/pubsubclient/src")
# Sem -DARDUINO: o ArduinoJson desliga o suporte a String/Stream/PROGMEM do Arduino
# (não usado pelo firmware), evitando reproduzir essas classes nos stubs.
DEFS=(-DESP32 -DSEM_ALLOW_EXAMPLE_SECRETS)
STRICT=(-std=gnu++17 -Wall -Wextra -Werror)

mapfile -t SRCS < <(find "$FW/src" -name '*.cpp' | sort)
mapfile -t CORE < <(find "$FW/lib/sem_core/src" -name '*.cpp' | sort)
echo "== ${#SRCS[@]} arquivos em src/ + ${#CORE[@]} em lib/sem_core/src"

for major in 2 3; do
  echo "== core Arduino-ESP32 ${major}.x (stubs)"
  for f in "${SRCS[@]}" "${CORE[@]}"; do
    rel="${f#"$FW"/}"
    "$CXX" "${STRICT[@]}" "${INC[@]}" "${DEFS[@]}" -DSTUB_ARDUINO_MAJOR="$major" -c "$f" \
      -o "$OUT/$(echo "$rel" | tr '/' '_').${major}.o"
    echo "   ok  $rel"
  done
  # Biblioteca de terceiros: compilada com os mesmos stubs (avisos dela não são nossos).
  "$CXX" -std=gnu++17 -w "${INC[@]}" "${DEFS[@]}" -DSTUB_ARDUINO_MAJOR="$major" \
    -c "$BH/pubsubclient/src/PubSubClient.cpp" -o "$OUT/PubSubClient.${major}.o"
  echo "   ok  PubSubClient.cpp (v2.8, real)"
done

echo "== payloads do contrato (tools/host_payload_check.cpp + src/comms/telemetry.cpp)"
"$CXX" "${STRICT[@]}" -I"$FW/include" -I"$FW/src" -I"$FW/lib/sem_core/include" -I"$BH/arduinojson/src" \
  "$FW/tools/host_payload_check.cpp" "$FW/src/comms/telemetry.cpp" "${CORE[@]}" -o "$OUT/host_payload_check"
PAY="$OUT/payloads"
rm -rf "$PAY" && mkdir -p "$PAY"
"$OUT/host_payload_check" "$PAY"

PY="$REPO/.venv/bin/python"
if [ -x "$PY" ] && "$PY" -c "import jsonschema" 2>/dev/null; then
  "$PY" -I - "$REPO/schemas" "$PAY" <<'PYEOF'
import json, sys, pathlib
import jsonschema
schemas, pay = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
def load(p): return json.loads(pathlib.Path(p).read_text(encoding="utf-8"))
checks = {
    "telemetry_full.json": "telemetry.schema.json",
    "telemetry_nulls.json": "telemetry.schema.json",
    "decision.json": "decision.schema.json",
    "decision_first.json": "decision.schema.json",
    "event.json": "event.schema.json",
}
for doc, sch in checks.items():
    jsonschema.validate(load(pay / doc), load(schemas / sch))
    print(f"   ok  {doc} valida em schemas/{sch}")
# Payloads sem schema formal: chaves do docs/05.
need = {
    "ack.json": {"id", "accepted", "reason", "ts_ms"},
    "ack_null_id.json": {"id", "accepted", "reason", "ts_ms"},
    "status_offline.json": {"status", "device_id", "boot_id", "fw_version", "ts_ms"},
    "scalar.json": {"value", "unit", "point", "ts_ms", "source", "device_id"},
    "mode.json": {"value", "unit", "point", "ts_ms", "source", "device_id"},
}
for doc, keys in need.items():
    d = load(pay / doc)
    assert set(d) == keys, (doc, sorted(d))
    print(f"   ok  {doc} tem as chaves do docs/05")
t = load(pay / "telemetry_full.json")
assert t["source"] == "hardware" and t["schema"] == "sem.telemetry.v1"
assert list(t) == ["schema", "device_id", "fw_version", "source", "boot_id", "seq", "ts_ms", "time_synced",
                   "uptime_ms", "window_ms", "n_samples", "units", "meas", "energy", "soc", "loads", "ems", "faults"]
print("   ok  ordem/campos da telemetria iguais ao exemplo do docs/05 §3")
PYEOF
else
  echo "   AVISO: .venv com jsonschema indisponível — validação por schema PULADA"
fi
echo "== host_compile_check: OK (lembrete: não substitui 'pio run -e esp32dev')"
