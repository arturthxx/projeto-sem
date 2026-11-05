// RTC DS3231 mínimo (registradores 0x00–0x06 em BCD, status 0x0F).
#include "hw/rtc_ds3231.h"

#include "sem_core/time_util.h"

namespace semhw {

namespace {
uint8_t bcd2bin(uint8_t v) { return static_cast<uint8_t>((v >> 4) * 10 + (v & 0x0F)); }
uint8_t bin2bcd(uint8_t v) { return static_cast<uint8_t>(((v / 10) << 4) | (v % 10)); }
constexpr uint8_t kRegSeconds = 0x00;
constexpr uint8_t kRegStatus = 0x0F;
constexpr uint8_t kStatusOsf = 0x80;  // oscilador parou: hora inválida
}  // namespace

bool RtcDs3231::read_regs(uint8_t reg, uint8_t* buf, uint8_t n) {
  wire_.beginTransmission(addr_);
  wire_.write(reg);
  if (wire_.endTransmission(false) != 0) return false;
  if (wire_.requestFrom(addr_, n) != n) return false;
  for (uint8_t i = 0; i < n; ++i) {
    const int b = wire_.read();
    if (b < 0) return false;
    buf[i] = static_cast<uint8_t>(b);
  }
  return true;
}

bool RtcDs3231::write_regs(uint8_t reg, const uint8_t* buf, uint8_t n) {
  wire_.beginTransmission(addr_);
  wire_.write(reg);
  wire_.write(buf, n);
  return wire_.endTransmission() == 0;
}

bool RtcDs3231::begin() {
  uint8_t st = 0;
  present_ = read_regs(kRegStatus, &st, 1);
  return present_;
}

bool RtcDs3231::read_epoch(int64_t* epoch_s) {
  if (!present_ || epoch_s == nullptr) return false;
  uint8_t st = 0;
  if (!read_regs(kRegStatus, &st, 1) || (st & kStatusOsf) != 0) return false;
  uint8_t r[7];
  if (!read_regs(kRegSeconds, r, 7)) return false;
  const int sec = bcd2bin(r[0] & 0x7F);
  const int min = bcd2bin(r[1] & 0x7F);
  const int hour = bcd2bin(r[2] & 0x3F);  // modo 24 h (gravado por write_epoch)
  const uint32_t day = bcd2bin(r[4] & 0x3F);
  const uint32_t month = bcd2bin(r[5] & 0x1F);
  const int32_t year = 2000 + bcd2bin(r[6]) + ((r[5] & 0x80) ? 100 : 0);
  if (month < 1 || month > 12 || day < 1 || day > 31 || hour > 23 || min > 59 || sec > 59) return false;
  const int64_t days = sem::days_from_civil(year, month, day);
  *epoch_s = days * 86400 + hour * 3600 + min * 60 + sec;
  return true;
}

bool RtcDs3231::write_epoch(int64_t epoch_s) {
  if (!present_ || epoch_s < 0) return false;
  const int32_t days = static_cast<int32_t>(epoch_s / 86400);
  const int32_t sod = static_cast<int32_t>(epoch_s % 86400);
  int32_t y = 0;
  uint32_t m = 0, d = 0;
  sem::civil_from_days(days, &y, &m, &d);
  if (y < 2000 || y > 2199) return false;
  const int32_t wday = ((days % 7) + 4) % 7 + 1;  // 1970-01-01 = quinta (5); DS3231 aceita 1..7
  uint8_t r[7];
  r[0] = bin2bcd(static_cast<uint8_t>(sod % 60));
  r[1] = bin2bcd(static_cast<uint8_t>((sod / 60) % 60));
  r[2] = bin2bcd(static_cast<uint8_t>(sod / 3600));  // bit 6 = 0 => 24 h
  r[3] = static_cast<uint8_t>(wday);
  r[4] = bin2bcd(static_cast<uint8_t>(d));
  r[5] = static_cast<uint8_t>(bin2bcd(static_cast<uint8_t>(m)) | (y >= 2100 ? 0x80 : 0x00));
  r[6] = bin2bcd(static_cast<uint8_t>(y % 100));
  if (!write_regs(kRegSeconds, r, 7)) return false;
  uint8_t st = 0;
  if (!read_regs(kRegStatus, &st, 1)) return false;
  st = static_cast<uint8_t>(st & ~kStatusOsf);
  return write_regs(kRegStatus, &st, 1);
}

}  // namespace semhw
