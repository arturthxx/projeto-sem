// =============================================================================
// sem_core/safety_supervisor.h — supervisor de segurança local (docs/06 §2).
//
// Executado a CADA amostra (firmware: 100 ms), em TODOS os modos do EMS.
// Saída: 'allowed' (global) e o código de maior prioridade:
//   OC_LOCKOUT > OC > OVP > OT > SENSOR > LVD > BOOT > OK.
// A saída física de cada carga é on_final = on_EMS AND allowed (LoadManager).
// Não substitui BMS, fusíveis nem as proteções do controlador de carga.
//
// Detalhes de implementação (texto normativo omisso) — idênticos ao espelho
// Python backend/smartenergy/ems/safety.py (verificado por vetores golden):
//   * Temporizadores acumulam dt (double) enquanto a condição vale e zeram
//     quando ela deixa de valer; comparação literal com o limiar (sem epsilon).
//     Com dt = 0,1 s, 50 amostras somam 4,999…, logo um atraso de 5 s dispara
//     na 51ª amostra (idêntico em Python).
//   * V_bat efetiva = v_bat se válido; senão v_bus. Sem nenhuma das duas, o
//     temporizador de LVD fica congelado (o critério de SOC continua valendo).
//   * OVP/OC: medida ausente (v_bus/i_bus inválidos) conta como condição falsa
//     (temporizador zera). OT: t_bat ausente mantém o estado.
//   * SENSOR: bat e bus inválidos por MAIS de sensor_timeout_s (comparação '>').
//   * BOOT: amostra válida = V_bat efetiva disponível.
//   * OC: a cada disparo trips += 1 (eventos OC_TRIP; e OC_LOCKOUT se
//     trips >= overcurrent_max_retries — só reset_faults/botão libera); senão
//     nova tentativa (evento OC_RETRY) após overcurrent_retry_s. 'trips' zera
//     após overcurrent_free_reset_s (10 min) acumulados com a saída habilitada
//     e sem disparo. O pino ALERT do INA226 (oc_alert) conta como
//     sobrecorrente (redundância de hardware; no Python é sempre falso).
//   * reset_faults(): o evento FAULTS_RESET é emitido na próxima update().
// =============================================================================
#pragma once

#include "sem_core/events.h"
#include "sem_core/params.h"
#include "sem_core/types.h"

namespace sem {

/// Entradas de uma amostra do supervisor. Campos *_ok = false => sensor inválido (None no Python).
struct SafetyInputs {
  double t_s = 0.0;
  double dt_s = 0.0;
  bool v_bat_ok = false;
  double v_bat = 0.0;
  double soc = 0.0;
  bool soc_valid = false;
  bool v_bus_ok = false;
  double v_bus = 0.0;
  bool i_bus_ok = false;
  double i_bus = 0.0;
  bool t_bat_ok = false;
  double t_bat = 0.0;
  bool oc_alert = false;  // pino ALERT do INA226 'bus' ativo (sobrecorrente por hardware)
};

struct SafetyStatus {
  bool allowed = false;
  SafetyCode code = SafetyCode::Boot;
};

class SafetySupervisor {
 public:
  /// boot_hold = true no firmware (BOOT até boot_settle_samples amostras válidas);
  /// false na simulação e nos vetores golden.
  explicit SafetySupervisor(const SafetyParams& params, bool boot_hold = true);

  /// Volta ao estado inicial (inclusive BOOT, se boot_hold).
  void reset();

  /// Avalia uma amostra. Eventos de borda (LVD_TRIP, OVP_CLEAR, ...) são anexados a 'events'.
  SafetyStatus update(const SafetyInputs& in, EventList* events = nullptr);

  /// Comando reset_faults / botão: libera OC e OC_LOCKOUT e zera a contagem de
  /// disparos. O evento FAULTS_RESET sai na próxima update().
  void reset_faults();

  SafetyStatus status() const { return status_; }
  bool active(SafetyCode c) const;
  int oc_trips() const { return oc_trips_; }

 private:
  SafetyCode highest() const;

  SafetyParams p_;
  bool boot_hold_;
  SafetyStatus status_{};

  bool boot_ = false;
  int boot_count_ = 0;
  bool lvd_ = false;
  double t_lvd_ = 0.0;
  bool ovp_ = false;
  double t_ovp_ = 0.0;
  double t_ovp_clear_ = 0.0;
  bool oc_ = false;
  bool oc_lockout_ = false;
  int oc_trips_ = 0;
  double t_oc_ = 0.0;
  double t_oc_wait_ = 0.0;
  double t_oc_free_ = 0.0;
  bool pending_reset_event_ = false;
  bool ot_ = false;
  bool sensor_ = false;
  double t_sensor_ = 0.0;
};

}  // namespace sem
