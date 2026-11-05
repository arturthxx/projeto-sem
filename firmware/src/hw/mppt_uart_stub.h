// =============================================================================
// src/hw/mppt_uart_stub.h — STUB documentado da interface serial com o
// controlador de carga MPPT (UART2: RX = GPIO 16, TX = GPIO 17).
//
// NÃO IMPLEMENTADO (fora do escopo do protótipo atual). Opções previstas:
//   * Victron SmartSolar (modelo de referência): VE.Direct, protocolo TEXTO,
//     19200 baud 8N1, 3,3 V TTL (o conector JST-PH fornece 5 V em alguns
//     modelos: usar divisor/conversor de nível no TX do controlador). Blocos
//     de pares "rótulo<TAB>valor" a cada ~1 s com "Checksum" (soma de bytes = 0 mod 256).
//     Campos úteis: V (mV, bateria), I (mA), VPV (mV), PPV (W), CS (estado de
//     carga: 0 off, 3 bulk, 4 absorption, 5 float), ERR, H20/H22 (energia do dia).
//   * EPEver Tracer-AN (alternativa): RS-485 Modbus RTU 115200 8N1, exige
//     transceptor (ex.: MAX3485 a 3,3 V); registradores de entrada 0x3100…
//     (PV V/I/P, bateria V/I) — conferir o documento "Tracer-AN Modbus protocol".
//
// Quando implementado, os dados do MPPT serão uma fonte ADICIONAL (estado de
// carga, Voc/Vmp), sem substituir os INA226 nem o supervisor de segurança.
// =============================================================================
#pragma once

#include <cstdint>

namespace semhw {

struct MpptStatus {
  bool ok = false;  // sempre false no stub
  double v_bat = 0.0;
  double i_bat = 0.0;
  double v_pv = 0.0;
  double p_pv = 0.0;
  int charge_state = -1;
};

class MpptUartStub {
 public:
  /// Apenas registra os pinos reservados; NÃO inicializa a UART2 (pinos livres).
  void begin(int rx_pin, int tx_pin) {
    rx_pin_ = rx_pin;
    tx_pin_ = tx_pin;
  }
  /// Stub: nunca há dados.
  bool poll(MpptStatus* out) {
    if (out != nullptr) *out = MpptStatus{};
    return false;
  }
  int rx_pin() const { return rx_pin_; }
  int tx_pin() const { return tx_pin_; }

 private:
  int rx_pin_ = -1;
  int tx_pin_ = -1;
};

}  // namespace semhw
