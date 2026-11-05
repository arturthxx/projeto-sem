// =============================================================================
// sem_core/ems_types.h — entradas e saídas comuns das estratégias do EMS
// (docs/06 §3; espelho de backend/smartenergy/ems/base.py: EmsInputs/Decision).
//
// Todos os vetores por carga são indexados pelo ÍNDICE da carga em
// LoadSetParams (0..count-1), não pelo id do contrato.
// =============================================================================
#pragma once

#include <cstddef>
#include <cstdint>

#include "sem_core/params.h"
#include "sem_core/types.h"

namespace sem {

/// Entradas de uma avaliação do EMS (medições/estimativas — nunca valores "verdadeiros").
struct EmsInputs {
  double t_s = 0.0;     // tempo monotônico (s): permanência mínima e TTL
  int64_t ts_ms = 0;    // epoch UTC em ms (0 se a hora não estiver sincronizada)
  double tod_s = 0.0;   // segundos desde a meia-noite local
  double soc = 0.0;     // SOC estimado (0..1)
  bool soc_valid = false;
  double soc_unc = 0.0;
  double v_bat = 0.0;   // V
  double i_bat = 0.0;   // A (+ descarga)
  double p_bat = 0.0;   // W (+ descarga)
  double p_pv = 0.0;    // W na entrada FV do MPPT
  double p_bus = 0.0;   // W no barramento de cargas
  bool demand[kMaxLoads] = {};          // pedido do perfil (cargas não deslocáveis)
  double e_done_wh[kMaxLoads] = {};     // energia entregue hoje (cargas deslocáveis)
  LoadOutput outputs[kMaxLoads] = {};   // saídas atuais (APÓS a segurança)
};

/// Decisão do EMS para todas as cargas.
struct Decision {
  EmsMode mode = EmsMode::Rules;
  EmsState state = EmsState::Normal;
  EmsState prev_state = EmsState::Normal;
  bool has_prev_state = false;  // false => prev_state = null no JSON
  LoadCommand commands[kMaxLoads] = {};
  int n_loads = 0;
  bool fallback = false;  // modo preditivo operando pelas regras (plano ausente/expirado)
  char reason[kReasonLen] = {};
};

/// Verdadeiro se as decisões diferem em modo, estado, fallback ou em algum
/// comando (on exato; duty com tolerância). Usado para publicar sem.decision.v1
/// apenas "a cada mudança de decisão" (docs/05 §2).
bool decision_differs(const Decision& a, const Decision& b, double duty_tol = 1e-6);

/// Acrescenta texto formatado ao final de 'buf' (separado por "; " se não vazio).
void append_reason(char* buf, size_t len, const char* fmt, ...)
#if defined(__GNUC__)
    __attribute__((format(printf, 3, 4)))
#endif
    ;

}  // namespace sem
