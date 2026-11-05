// =============================================================================
// src/hw/rtc_ds3231.h — RTC DS3231 (OPCIONAL, mínimo): mantém a hora em UTC
// sem rede. No boot, se o RTC tiver hora válida e o SNTP ainda não
// sincronizou, o relógio do sistema é ajustado por ele; após cada
// sincronização SNTP o RTC é regravado. Sem RTC instalado, tudo segue normal
// (begin() retorna false).
// =============================================================================
#pragma once

#include <Wire.h>

#include <cstdint>

namespace semhw {

class RtcDs3231 {
 public:
  RtcDs3231(TwoWire& wire, uint8_t address) : wire_(wire), addr_(address) {}

  /// Detecta o RTC no barramento.
  bool begin();
  bool present() const { return present_; }

  /// Lê a hora (UTC) em segundos desde 1970. Falha se o oscilador parou (OSF).
  bool read_epoch(int64_t* epoch_s);

  /// Grava a hora (UTC) e limpa o OSF.
  bool write_epoch(int64_t epoch_s);

 private:
  bool read_regs(uint8_t reg, uint8_t* buf, uint8_t n);
  bool write_regs(uint8_t reg, const uint8_t* buf, uint8_t n);

  TwoWire& wire_;
  uint8_t addr_;
  bool present_ = false;
};

}  // namespace semhw
