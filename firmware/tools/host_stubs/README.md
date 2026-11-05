# host_stubs — cabeçalhos mínimos para verificação de compilação no PC

Usados SOMENTE por `firmware/tools/host_compile_check.sh` para compilar os `.cpp`
de `firmware/src/` com g++ no PC e pegar erros de sintaxe/tipo. As declarações
seguem as assinaturas reais do core Arduino-ESP32 2.0.x (e 3.x quando
`STUB_ARDUINO_MAJOR=3`), ESP-IDF 4.4/5.x, PubSubClient 2.8, OneWire 2.3 e
DallasTemperature 3.11 — mas NÃO há implementação: nada aqui é linkado nem
executado. Isto NÃO substitui compilar com o toolchain real
(`pio run -e esp32dev`).
