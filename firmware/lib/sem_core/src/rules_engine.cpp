// Controle por regras com histerese por faixas de SOC (docs/06 §3.2).
#include "sem_core/rules_engine.h"

#include <cstdio>

#include "sem_core/time_util.h"

namespace sem {

RulesEngine::RulesEngine(const RulesParams& rules, const LoadSetParams& loads) : r_(rules), loads_(loads) { reset(); }

void RulesEngine::reset() {
  level_ = -1;
  for (bool& l : latch_) l = false;
}

double RulesEngine::threshold(int level) const {
  switch (level) {
    case 3:
      return r_.normal_min_soc;
    case 2:
      return r_.economy_min_soc;
    case 1:
      return r_.critical_min_soc;
    default:
      return kNegInf;
  }
}

EmsState RulesEngine::update_state(const EmsInputs& in, char* reason, size_t len) {
  const double soc = in.soc;
  if (level_ < 0 && !in.soc_valid) {
    level_ = 1;  // primeira avaliação com SOC inválido: CRITICAL (igual ao espelho Python)
    append_reason(reason, len, "soc_invalid -> CRITICAL");
    return EmsState::Critical;
  }
  if (level_ < 0) {
    // Primeira avaliação: maior L tal que soc >= T_L (sem histerese).
    int level = 0;
    for (int l = 3; l >= 1; --l) {
      if (soc >= threshold(l)) {
        level = l;
        break;
      }
    }
    level_ = level;
    append_reason(reason, len, "inicial: soc %.2f -> %s", soc, to_string(static_cast<EmsState>(level_)));
    return static_cast<EmsState>(level_);
  }

  const int before = level_;
  if (!in.soc_valid) {
    if (level_ > 1) level_ = 1;
    append_reason(reason, len, "soc_invalid -> %s", to_string(static_cast<EmsState>(level_)));
    return static_cast<EmsState>(level_);
  }
  while (level_ > 0 && soc < threshold(level_)) --level_;  // descida imediata
  while (level_ < 3 && soc >= threshold(level_ + 1) + r_.hysteresis_soc) ++level_;  // subida com histerese

  const EmsState st = static_cast<EmsState>(level_);
  if (level_ < before) {
    append_reason(reason, len, "soc %.2f < %.2f -> %s", soc, threshold(level_ + 1), to_string(st));
  } else if (level_ > before) {
    append_reason(reason, len, "soc %.2f >= %.2f+%.2f -> %s", soc, threshold(level_), r_.hysteresis_soc, to_string(st));
  } else {
    append_reason(reason, len, "%s (soc %.2f)", to_string(st), soc);
  }
  return st;
}

LoadCommand RulesEngine::decide_fixed(int idx, EmsState st, const EmsInputs& in) const {
  const LoadParams& L = loads_.items[idx];
  const bool d = in.demand[idx];
  LoadCommand c{};
  switch (L.priority) {
    case Priority::High:
      c.on = d && st != EmsState::Protection;
      c.duty = 1.0;
      break;
    case Priority::Medium:
      c.on = d && (st == EmsState::Normal || st == EmsState::Economy);
      if (st == EmsState::Economy && L.dimmable) {
        c.duty = r_.economy_medium_duty > L.min_duty ? r_.economy_medium_duty : L.min_duty;
      } else {
        c.duty = 1.0;
      }
      break;
    case Priority::Low:
      c.on = d && st == EmsState::Normal;
      c.duty = 1.0;
      break;
  }
  if (!c.on) c.duty = 0.0;
  return c;
}

LoadCommand RulesEngine::decide_deferrable(int idx, EmsState st, const EmsInputs& in, char* reason, size_t len) {
  const LoadParams& L = loads_.items[idx];
  const DeferrableParams& D = L.deferrable;
  const double rem = D.daily_energy_wh - in.e_done_wh[idx] > 0.0 ? D.daily_energy_wh - in.e_done_wh[idx] : 0.0;
  const bool in_win = in_window(in.tod_s, D.window_start_s, D.deadline_s);
  const LoadOutput& out = in.outputs[idx];
  const bool cur_on = out.on;

  bool want = false;
  bool forced_off = false;
  bool& latch = latch_[idx];

  if (rem <= 0.0 || !in_win || st == EmsState::Critical || st == EmsState::Protection) {
    want = false;
    latch = false;
    forced_off = true;
    if (rem <= 0.0) {
      append_reason(reason, len, "%s: energia diaria concluida", L.key);
    } else if (!in_win) {
      append_reason(reason, len, "%s: fora da janela", L.key);
    } else {
      append_reason(reason, len, "%s: estado %s", L.key, to_string(st));
    }
  } else if (st == EmsState::Normal) {
    if (in.soc >= r_.surplus_on_soc) latch = true;
    if (in.soc < r_.surplus_off_soc) latch = false;
    const double t_need = rem / L.p_nom_w * 3600.0;
    const double t_left = seconds_until(in.tod_s, D.deadline_s);
    const bool force = t_left <= t_need + r_.deadline_margin_s;
    want = latch || force;
    if (latch) {
      append_reason(reason, len, "%s: excedente soc>=%.2f", L.key, r_.surplus_on_soc);
    } else if (force) {
      append_reason(reason, len, "%s: prazo (faltam %.0f Wh, %.0f s)", L.key, rem, t_left);
    }
  } else {  // ECONOMY: só com excedente FV suficiente entrando na bateria
    if (!cur_on) {
      latch = (-in.p_bat >= L.p_nom_w + r_.economy_surplus_margin_w);
    } else {
      latch = (in.p_bat <= r_.economy_surplus_margin_w);
    }
    want = latch;
    if (latch) append_reason(reason, len, "%s: excedente FV em ECONOMY (p_bat %.1f W)", L.key, in.p_bat);
  }

  bool on = want;
  if (forced_off) {
    on = false;
  } else if (want != cur_on) {
    if (in.t_s - out.last_switch_t_s < r_.min_dwell_s) {
      on = cur_on;
      append_reason(reason, len, "%s: permanencia minima (%.0f s)", L.key, r_.min_dwell_s);
    }
  }
  LoadCommand c{};
  c.on = on;
  c.duty = on ? 1.0 : 0.0;
  return c;
}

Decision RulesEngine::decide(const EmsInputs& in) {
  Decision d{};
  d.mode = EmsMode::Rules;
  d.n_loads = loads_.count;
  if (level_ >= 0) {
    d.has_prev_state = true;
    d.prev_state = static_cast<EmsState>(level_);
  }
  d.state = update_state(in, d.reason, sizeof(d.reason));
  for (int i = 0; i < loads_.count && i < kMaxLoads; ++i) {
    if (loads_.items[i].deferrable.enabled) {
      d.commands[i] = decide_deferrable(i, d.state, in, d.reason, sizeof(d.reason));
    } else {
      d.commands[i] = decide_fixed(i, d.state, in);
    }
  }
  return d;
}

}  // namespace sem
