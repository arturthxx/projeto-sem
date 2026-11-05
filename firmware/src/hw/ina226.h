// =============================================================================
// src/hw/ina226.h — driver PRÓPRIO do INA226 via Wire (I2C).
//
//   * verificação de identidade (0xFE = 0x5449, 0xFF = 0x2260);
//   * configuração: média de 16 amostras, conversão de 1,1 ms, modo contínuo;
//   * calibração CAL = 0,00512/(current_LSB·R_shunt) (registradores de
//     corrente/potência ficam coerentes, embora a corrente seja calculada pela
//     tensão de shunt para não depender do arredondamento de CAL);
//   * tensão de shunt COM SINAL (medição bidirecional da bateria);
//   * detecção de falha: erro/timeout de I2C, identidade errada, ausência de
//     conversão nova (CVRF) por mais de kInaStaleMs;
//   * ALERT por sobrecorrente (função SOL, limite em tensão de shunt) para o
//     sensor 'bus' — pino coletor aberto, ativo em nível baixo, modo
//     transparente (segue o comparador; a filtragem temporal é do supervisor).
// =============================================================================
#pragma once

#include <Wire.h>

#include <cstdint>

#include "sem_core/params.h"

namespace semhw {

struct InaReading {
  bool ok = false;
  double bus_v = 0.0;    // V (IN- em relação ao GND do INA226)
  double shunt_v = 0.0;  // V, com sinal
  double current_a = 0.0;  // A, com sinal (aplicado 'sign' do system.yaml)
  double power_w = 0.0;    // W = bus_v · current_a
};

class Ina226 {
 public:
  Ina226(TwoWire& wire, const sem::Ina226Params& params) : wire_(wire), p_(params) {}

  /// Reinicia, confere a identidade e programa configuração e calibração.
  bool begin();

  /// Lê tensões de shunt e barramento. Em falha, r->ok = false.
  bool read(uint32_t now_ms, InaReading* r);

  /// Programa o ALERT por sobrecorrente (SOL) com limite em tensão de shunt.
  bool configure_overcurrent_alert(double current_a);

  bool present() const { return present_; }
  bool healthy() const { return present_ && consecutive_errors_ < kFailThreshold(); }
  uint32_t error_count() const { return error_count_; }
  uint16_t calibration() const { return cal_; }
  const char* name() const { return p_.name; }

  /// Tentativa periódica de reinicialização quando ausente/falho (chamar a cada amostra).
  void maintain(uint32_t now_ms);

 private:
  static int kFailThreshold();
  bool write_reg(uint8_t reg, uint16_t value);
  bool read_reg(uint8_t reg, uint16_t* value);
  void note_error();

  TwoWire& wire_;
  sem::Ina226Params p_;
  bool present_ = false;
  bool alert_configured_ = false;
  double alert_current_a_ = 0.0;
  uint16_t cal_ = 0;
  int consecutive_errors_ = 0;
  uint32_t error_count_ = 0;
  uint32_t last_conversion_ms_ = 0;
  uint32_t last_retry_ms_ = 0;
};

}  // namespace semhw
