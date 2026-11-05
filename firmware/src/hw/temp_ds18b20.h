// =============================================================================
// src/hw/temp_ds18b20.h — temperaturas da bateria e do módulo FV (DS18B20 em
// barramento OneWire único), com conversão ASSÍNCRONA (não bloqueante):
// requestTemperatures() com setWaitForConversion(false) e leitura após o
// tempo de conversão da resolução escolhida.
//
// Atribuição dos sensores: índice 0 (menor endereço ROM na enumeração) = bateria,
// índice 1 = módulo FV. PENDENTE: fixar os endereços ROM reais após a montagem
// (registrar em docs/04-mapa-de-pinos.md) — a ordem de enumeração não é garantida
// entre lotes de sensores.
// =============================================================================
#pragma once

#include <DallasTemperature.h>
#include <OneWire.h>

#include <cstdint>

namespace semhw {

struct TempReading {
  bool ok = false;
  double t_c = 0.0;
};

class TempSensors {
 public:
  explicit TempSensors(uint8_t pin) : bus_(pin), dallas_(&bus_) {}

  /// Enumera os sensores e dispara a primeira conversão (não bloqueia).
  void begin(uint8_t resolution_bits);

  /// Máquina de estados: lê quando a conversão termina e dispara a próxima.
  void poll(uint32_t now_ms);

  TempReading bat() const { return value(0); }
  TempReading pv() const { return value(1); }
  uint8_t count() const { return count_; }

 private:
  TempReading value(int idx) const;
  uint32_t conversion_ms() const;

  OneWire bus_;
  DallasTemperature dallas_;
  DeviceAddress addr_[2] = {};
  uint8_t count_ = 0;
  uint8_t resolution_ = 11;
  bool converting_ = false;
  uint32_t t_request_ms_ = 0;
  uint32_t t_last_ok_ms_[2] = {};
  uint32_t now_ms_ = 0;
  double t_c_[2] = {};
  bool ever_ok_[2] = {};
};

}  // namespace semhw
