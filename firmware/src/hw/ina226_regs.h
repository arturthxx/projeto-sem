// =============================================================================
// src/hw/ina226_regs.h — mapa de registradores e conversões PURAS do INA226
// (TI INA226, datasheet SBOS547). Sem Arduino: verificável no PC
// (static_assert em tools/host_payload_check.cpp).
// =============================================================================
#pragma once

#include <cstdint>

namespace semhw {
namespace ina226 {

// --- Registradores ---------------------------------------------------------------
constexpr uint8_t kRegConfig = 0x00;
constexpr uint8_t kRegShuntVoltage = 0x01;  // com sinal, LSB 2,5 µV
constexpr uint8_t kRegBusVoltage = 0x02;    // sem sinal, LSB 1,25 mV
constexpr uint8_t kRegPower = 0x03;
constexpr uint8_t kRegCurrent = 0x04;
constexpr uint8_t kRegCalibration = 0x05;
constexpr uint8_t kRegMaskEnable = 0x06;
constexpr uint8_t kRegAlertLimit = 0x07;
constexpr uint8_t kRegManufacturerId = 0xFE;
constexpr uint8_t kRegDieId = 0xFF;

constexpr uint16_t kManufacturerId = 0x5449;  // "TI"
constexpr uint16_t kDieId = 0x2260;

constexpr double kShuntLsbV = 2.5e-6;
constexpr double kBusLsbV = 1.25e-3;

// --- Configuração (registrador 0x00) -----------------------------------------------
constexpr uint16_t kConfigReset = 0x8000;
constexpr uint16_t kConfigReserved = 0x4000;  // bit 14 (lido como 1)
enum class Avg : uint16_t { N1 = 0, N4 = 1, N16 = 2, N64 = 3, N128 = 4, N256 = 5, N512 = 6, N1024 = 7 };
enum class ConvTime : uint16_t { Us140 = 0, Us204 = 1, Us332 = 2, Us588 = 3, Us1100 = 4, Us2116 = 5, Us4156 = 6, Us8244 = 7 };
constexpr uint16_t kModeShuntBusContinuous = 0x7;

constexpr uint16_t config_word(Avg avg, ConvTime vbus_ct, ConvTime vsh_ct, uint16_t mode = kModeShuntBusContinuous) {
  return static_cast<uint16_t>(kConfigReserved | (static_cast<uint16_t>(avg) << 9) |
                               (static_cast<uint16_t>(vbus_ct) << 6) | (static_cast<uint16_t>(vsh_ct) << 3) |
                               (mode & 0x7));
}
/// Configuração usada: média de 16 amostras, 1,1 ms de conversão (V_bus e V_shunt), contínuo.
/// Tempo por atualização: 16 × (1,1 + 1,1) ms ≈ 35 ms (< período de amostragem de 100 ms).
constexpr uint16_t kDefaultConfig = config_word(Avg::N16, ConvTime::Us1100, ConvTime::Us1100);

// --- Máscara/habilitação (registrador 0x06) -------------------------------------------
constexpr uint16_t kMaskSol = 0x8000;   // alerta: tensão de shunt acima do limite (sobrecorrente)
constexpr uint16_t kMaskSul = 0x4000;   // alerta: tensão de shunt abaixo do limite
constexpr uint16_t kMaskBol = 0x2000;   // alerta: tensão de barramento acima
constexpr uint16_t kMaskBul = 0x1000;   // alerta: tensão de barramento abaixo
constexpr uint16_t kMaskPol = 0x0800;   // alerta: potência acima
constexpr uint16_t kMaskCnvr = 0x0400;  // ALERT na conversão pronta
constexpr uint16_t kMaskAff = 0x0010;   // flag de função de alerta
constexpr uint16_t kMaskCvrf = 0x0008;  // conversão pronta (limpa ao ler o registrador)
constexpr uint16_t kMaskOvf = 0x0004;   // estouro aritmético
constexpr uint16_t kMaskApol = 0x0002;  // polaridade do ALERT (0 = ativo baixo, coletor aberto)
constexpr uint16_t kMaskLen = 0x0001;   // latch do ALERT

// --- Calibração -------------------------------------------------------------------------
/// current_LSB mínimo para a corrente máxima esperada (registrador de 15 bits).
constexpr double current_lsb_for(double max_current_a) { return max_current_a / 32768.0; }

/// CAL = trunc(0,00512 / (current_LSB · R_shunt)), limitado a 0x7FFF (datasheet, eq. 1).
constexpr uint16_t calibration_value(double current_lsb_a, double r_shunt_ohm) {
  const double cal = 0.00512 / (current_lsb_a * r_shunt_ohm);
  return cal >= 32767.0 ? static_cast<uint16_t>(0x7FFF) : (cal <= 0.0 ? static_cast<uint16_t>(0) : static_cast<uint16_t>(cal));
}

/// Limite do registrador 0x07 para o alerta SOL: mesma escala da tensão de shunt
/// (2,5 µV/LSB), arredondado ao inteiro mais próximo.
constexpr uint16_t shunt_alert_limit(double current_a, double r_shunt_ohm) {
  const double counts = current_a * r_shunt_ohm / kShuntLsbV + 0.5;
  return counts >= 32767.0 ? static_cast<uint16_t>(0x7FFF) : (counts <= 0.0 ? static_cast<uint16_t>(0) : static_cast<uint16_t>(counts));
}

/// Registrador de shunt (complemento de dois) -> volts.
constexpr double shunt_volts(uint16_t raw) { return static_cast<double>(static_cast<int16_t>(raw)) * kShuntLsbV; }

/// Registrador de barramento -> volts.
constexpr double bus_volts(uint16_t raw) { return static_cast<double>(raw & 0x7FFF) * kBusLsbV; }

}  // namespace ina226
}  // namespace semhw
