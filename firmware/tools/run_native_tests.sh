#!/usr/bin/env bash
# =============================================================================
# run_native_tests.sh — compila e executa no PC os testes nativos (Unity) da
# lógica pura do firmware (lib/sem_core), SEM o toolchain do ESP32.
#
#   * clona Unity v2.6.0 e ArduinoJson v7.2.1 em firmware/build_host/ (gitignored);
#   * compila lib/sem_core + cada test/test_*/ com g++ -std=c++17 -Wall -Wextra -Werror;
#   * executa cada suíte; o código de saída é != 0 se alguma falhar.
#
# Equivalente (no PC com PlatformIO instalado):  pio test -e native
# Uso: firmware/tools/run_native_tests.sh [filtro]   (ex.: test_rules)
# =============================================================================
set -euo pipefail

FW="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BH="$FW/build_host"
OUT="$BH/native"
CXX="${CXX:-g++}"
CC="${CC:-gcc}"
FILTER="${1:-}"

mkdir -p "$BH" "$OUT/obj"

clone() {  # clone <url> <tag> <dir>
  local url="$1" tag="$2" dir="$BH/$3"
  if [ ! -f "$dir/.sem_tag" ] || [ "$(cat "$dir/.sem_tag")" != "$tag" ]; then
    rm -rf "$dir"
    git clone --quiet --depth 1 -b "$tag" "$url" "$dir"
    echo "$tag" > "$dir/.sem_tag"
  fi
}
clone https://github.com/ThrowTheSwitch/Unity v2.6.0 unity
clone https://github.com/bblanchon/ArduinoJson v7.2.1 arduinojson

CXXFLAGS=(-std=c++17 -Wall -Wextra -Werror -O1 -g)
INC=(-I"$FW/lib/sem_core/include" -I"$FW/include" -I"$BH/arduinojson/src" -I"$BH/unity/src")
DEFS=(-DUNITY_INCLUDE_DOUBLE "-DSEM_VECTORS_DIR=\"$FW/test/vectors\"")

echo "== compilando lib/sem_core"
CORE_OBJS=()
for src in "$FW"/lib/sem_core/src/*.cpp; do
  obj="$OUT/obj/core_$(basename "$src" .cpp).o"
  "$CXX" "${CXXFLAGS[@]}" "${INC[@]}" -c "$src" -o "$obj"
  CORE_OBJS+=("$obj")
done
"$CC" -std=c11 -O1 -DUNITY_INCLUDE_DOUBLE -c "$BH/unity/src/unity.c" -o "$OUT/obj/unity.o"

fail=0
total_tests=0
total_fail=0
total_ign=0
for dir in "$FW"/test/test_*/; do
  name="$(basename "$dir")"
  if [ -n "$FILTER" ] && [[ "$name" != *"$FILTER"* ]]; then continue; fi
  echo "== $name"
  "$CXX" "${CXXFLAGS[@]}" "${INC[@]}" "${DEFS[@]}" "$dir"*.cpp "${CORE_OBJS[@]}" "$OUT/obj/unity.o" -o "$OUT/$name"
  set +e
  log="$("$OUT/$name" 2>&1)"
  rc=$?
  set -e
  echo "$log" | grep -E "FAIL|IGNORE|\[vetores\]|Tests|^OK" || true
  summary="$(echo "$log" | grep -E '^[0-9]+ Tests [0-9]+ Failures [0-9]+ Ignored' | tail -1)"
  if [ -n "$summary" ]; then
    read -r t _ f _ i _ <<< "$summary"
    total_tests=$((total_tests + t)); total_fail=$((total_fail + f)); total_ign=$((total_ign + i))
  fi
  [ $rc -ne 0 ] && fail=1
done

echo "== TOTAL: $total_tests testes, $total_fail falhas, $total_ign ignorados"
exit $fail
