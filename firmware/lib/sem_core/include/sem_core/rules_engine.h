// =============================================================================
// sem_core/rules_engine.h — controle baseado em regras (docs/06 §3.2, EXATO).
// Espelho de backend/smartenergy/ems/rules.py (verificado por vetores golden).
//
//   * Faixas de SOC com histerese: descida imediata, subida exige T + H.
//   * Ações por estado para cargas não deslocáveis (por prioridade).
//   * Carga deslocável com 'latch' persistente (excedente / prazo / economia).
//   * Permanência mínima (min_dwell_s) SOMENTE para a carga deslocável e
//     somente para mudanças motivadas por 'quer'; desligamentos por energia
//     concluída, fora da janela ou estado CRITICAL/PROTECTION são imediatos.
//
// Decisões de implementação (texto normativo omisso):
//   * A primeira avaliação classifica pelo SOC sem histerese; se o SOC for
//     inválido já na primeira avaliação, o estado é CRITICAL (qualquer SOC).
//     A partir da segunda, soc inválido => min(nível, CRITICAL).
//   * t_restante = seconds_until(tod, deadline) (módulo 24 h), idêntico a
//     deadline − tod para janelas que não cruzam a meia-noite.
//   * Carga com 'deferrable.enabled' usa a lógica deslocável qualquer que seja
//     a prioridade; demais cargas seguem a tabela por prioridade.
// =============================================================================
#pragma once

#include "sem_core/ems_types.h"
#include "sem_core/params.h"

namespace sem {

class RulesEngine {
 public:
  RulesEngine(const RulesParams& rules, const LoadSetParams& loads);

  /// Esquece o nível (próxima avaliação = "primeira") e zera os latches.
  void reset();

  /// Avalia as regras. Não altera as saídas: o chamador aplica a decisão.
  Decision decide(const EmsInputs& in);

  bool has_state() const { return level_ >= 0; }
  EmsState state() const { return static_cast<EmsState>(level_ < 0 ? 0 : level_); }
  bool latch(int idx) const { return idx >= 0 && idx < kMaxLoads ? latch_[idx] : false; }

  /// Limite inferior do nível L (T0 = −inf, T1 = critical, T2 = economy, T3 = normal).
  double threshold(int level) const;

 private:
  EmsState update_state(const EmsInputs& in, char* reason, size_t len);
  LoadCommand decide_deferrable(int idx, EmsState st, const EmsInputs& in, char* reason, size_t len);
  LoadCommand decide_fixed(int idx, EmsState st, const EmsInputs& in) const;

  const RulesParams& r_;
  const LoadSetParams& loads_;
  int level_ = -1;  // -1 = nenhuma avaliação ainda
  bool latch_[kMaxLoads] = {};
};

}  // namespace sem
