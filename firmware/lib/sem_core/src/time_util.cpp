// Utilidades de tempo (hora local, janelas e calendário) sem dependência de libc de tempo.
#include "sem_core/time_util.h"

#include <cmath>
#include <cstdio>

namespace sem {

bool parse_hhmm(const char* s, int32_t* out_s) {
  if (s == nullptr || out_s == nullptr) return false;
  int h = 0, m = 0;
  char tail = 0;
  // Aceita "H:MM" e "HH:MM"; rejeita lixo após os minutos.
  if (std::sscanf(s, "%d:%d%c", &h, &m, &tail) != 2) return false;
  if (h < 0 || h > 24 || m < 0 || m >= 60 || (h == 24 && m != 0)) return false;
  *out_s = h * 3600 + m * 60;
  return true;
}

void format_hhmm(int32_t tod_s, char* out, int out_len) {
  if (out == nullptr || out_len <= 0) return;
  int32_t t = tod_s % 86400;
  if (t < 0) t += 86400;
  std::snprintf(out, static_cast<size_t>(out_len), "%02d:%02d", static_cast<int>(t / 3600),
                static_cast<int>((t % 3600) / 60));
}

bool in_window(double tod_s, int32_t start_s, int32_t end_s) {
  if (start_s == end_s) return false;
  // Mesmo comportamento do operador % do Python para tod negativo.
  double t = std::fmod(tod_s, kDaySeconds);
  if (t < 0.0) t += kDaySeconds;
  const double a = static_cast<double>(start_s);
  const double b = static_cast<double>(end_s);
  if (a < b) return a <= t && t < b;
  return t >= a || t < b;
}

bool in_any_window(double tod_s, const Schedule& schedule) {
  for (int i = 0; i < schedule.count && i < kMaxWindows; ++i) {
    if (in_window(tod_s, schedule.windows[i].start_s, schedule.windows[i].end_s)) return true;
  }
  return false;
}

namespace {
// Divisão inteira com arredondamento para −∞ (válida para negativos).
int64_t floor_div(int64_t a, int64_t b) {
  int64_t q = a / b;
  if ((a % b != 0) && ((a < 0) != (b < 0))) --q;
  return q;
}
}  // namespace

double tod_from_epoch_ms(int64_t ts_ms, int32_t utc_offset_s) {
  const int64_t local_ms = ts_ms + static_cast<int64_t>(utc_offset_s) * 1000;
  const int64_t day_ms = 86400LL * 1000LL;
  int64_t r = local_ms % day_ms;
  if (r < 0) r += day_ms;
  return static_cast<double>(r) / 1000.0;
}

int32_t local_day_index(int64_t ts_ms, int32_t utc_offset_s) {
  const int64_t local_ms = ts_ms + static_cast<int64_t>(utc_offset_s) * 1000;
  return static_cast<int32_t>(floor_div(local_ms, 86400LL * 1000LL));
}

// Algoritmo de H. Hinnant ("chrono-compatible low-level date algorithms").
void civil_from_days(int32_t days, int32_t* year, uint32_t* month, uint32_t* day) {
  int64_t z = static_cast<int64_t>(days) + 719468;
  const int64_t era = (z >= 0 ? z : z - 146096) / 146097;
  const uint32_t doe = static_cast<uint32_t>(z - era * 146097);
  const uint32_t yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
  int64_t y = static_cast<int64_t>(yoe) + era * 400;
  const uint32_t doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
  const uint32_t mp = (5 * doy + 2) / 153;
  const uint32_t d = doy - (153 * mp + 2) / 5 + 1;
  const uint32_t m = mp < 10 ? mp + 3 : mp - 9;
  if (m <= 2) ++y;
  if (year) *year = static_cast<int32_t>(y);
  if (month) *month = m;
  if (day) *day = d;
}

int32_t days_from_civil(int32_t year, uint32_t month, uint32_t day) {
  const int64_t y = static_cast<int64_t>(year) - (month <= 2 ? 1 : 0);
  const int64_t era = (y >= 0 ? y : y - 399) / 400;
  const uint32_t yoe = static_cast<uint32_t>(y - era * 400);
  const uint32_t mp = month > 2 ? month - 3 : month + 9;
  const uint32_t doy = (153 * mp + 2) / 5 + day - 1;
  const uint32_t doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
  return static_cast<int32_t>(era * 146097 + static_cast<int64_t>(doe) - 719468);
}

void format_local_day(int64_t ts_ms, int32_t utc_offset_s, char* out, int out_len) {
  if (out == nullptr || out_len <= 0) return;
  int32_t y = 0;
  uint32_t m = 0, d = 0;
  civil_from_days(local_day_index(ts_ms, utc_offset_s), &y, &m, &d);
  std::snprintf(out, static_cast<size_t>(out_len), "%04d-%02u-%02u", static_cast<int>(y), m, d);
}

double seconds_until(double tod_s, int32_t deadline_s) {
  double r = static_cast<double>(deadline_s) - tod_s;
  if (r < 0.0) r += kDaySeconds;
  return r;
}

}  // namespace sem
