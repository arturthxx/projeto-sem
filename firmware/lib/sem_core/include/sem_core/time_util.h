// =============================================================================
// sem_core/time_util.h — hora local, janelas [início, fim) e datas.
//
// Espelho de backend/smartenergy/timeutil.py: tod = segundos desde a
// meia-noite LOCAL; intervalo [a, b) com cruzamento de meia-noite quando
// b < a; a == b => vazio. Fuso fixo (UTC−3, sem horário de verão).
// =============================================================================
#pragma once

#include <cstdint>

#include "sem_core/params.h"

namespace sem {

/// "HH:MM" -> segundos desde a meia-noite. Retorna false se inválido.
bool parse_hhmm(const char* s, int32_t* out_s);

/// Segundos desde a meia-noite -> "HH:MM" (out com pelo menos 6 bytes).
void format_hhmm(int32_t tod_s, char* out, int out_len);

/// tod ∈ [start, end) com suporte a cruzar a meia-noite (tod reduzido módulo 86400).
bool in_window(double tod_s, int32_t start_s, int32_t end_s);

/// tod pertence a alguma janela da lista.
bool in_any_window(double tod_s, const Schedule& schedule);

/// Segundos desde a meia-noite local para um epoch UTC em ms.
double tod_from_epoch_ms(int64_t ts_ms, int32_t utc_offset_s);

/// Índice do dia LOCAL (dias desde 1970-01-01 local) para um epoch UTC em ms.
int32_t local_day_index(int64_t ts_ms, int32_t utc_offset_s);

/// Data civil (proléptica gregoriana) a partir do índice de dias desde 1970-01-01.
void civil_from_days(int32_t days, int32_t* year, uint32_t* month, uint32_t* day);

/// Índice de dias desde 1970-01-01 a partir da data civil.
int32_t days_from_civil(int32_t year, uint32_t month, uint32_t day);

/// Dia local "AAAA-MM-DD" (out com pelo menos 11 bytes).
void format_local_day(int64_t ts_ms, int32_t utc_offset_s, char* out, int out_len);

/// Tempo restante até 'deadline_s' a partir de 'tod_s', em [0, 86400).
double seconds_until(double tod_s, int32_t deadline_s);

}  // namespace sem
