// =============================================================================
// sem_core/events.h — eventos gerados pela lógica pura (sem.event.v1, docs/05 §6).
//
// Os módulos (SOC, segurança, EMS, validador) apenas ANEXAM eventos a uma
// EventList de capacidade fixa; a camada de aplicação carimba o instante
// (ts_ms) e os encaminha à tarefa de comunicação. Sem alocação dinâmica.
// =============================================================================
#pragma once

#include <cstdarg>
#include <cstdint>

namespace sem {

enum class EventLevel : uint8_t { Info = 0, Warn, Error, Critical };

/// Códigos mínimos do contrato (docs/05 §6). A ordem não é normativa.
enum class EventCode : uint8_t {
  Boot = 0,
  TimeSync,
  WifiUp,
  WifiDown,
  MqttUp,
  MqttDown,
  SensorFail,
  SensorOk,
  LvdTrip,
  LvdClear,
  OvpTrip,
  OvpClear,
  OcTrip,
  OcLockout,
  OtTrip,
  OtClear,
  SocSyncFull,
  SocSyncEmpty,
  ModeChange,
  CmdRejected,
  PlanExpired,
  FaultsReset,
  OcRetry,  // extensão: nova tentativa após OC (mesmo código do espelho Python)
  Count_
};

const char* to_string(EventCode c);
const char* to_string(EventLevel l);

/// Um campo do objeto 'data' do evento (número ou texto).
struct EventDatum {
  char key[16] = {};
  bool is_string = false;
  double number = 0.0;
  char text[48] = {};
};

/// Evento pronto para serialização (ts_ms preenchido pela aplicação).
struct CoreEvent {
  EventCode code = EventCode::Boot;
  EventLevel level = EventLevel::Info;
  int64_t ts_ms = 0;
  char msg[112] = {};
  EventDatum data[2] = {};
  uint8_t n_data = 0;

  /// Acrescenta um campo numérico em 'data' (ignora se cheio).
  CoreEvent& with_number(const char* key, double value);
  /// Acrescenta um campo de texto em 'data' (ignora se cheio).
  CoreEvent& with_text(const char* key, const char* value);
};

/// Lista de eventos de capacidade fixa. Eventos excedentes são descartados e contados.
class EventList {
 public:
  static constexpr int kCapacity = 8;

  /// Anexa um evento com mensagem formatada (printf). Retorna o evento criado
  /// (ou um evento descartável, se a lista estiver cheia) para encadear with_*().
  CoreEvent& push(EventCode code, EventLevel level, const char* fmt, ...)
#if defined(__GNUC__)
      __attribute__((format(printf, 4, 5)))
#endif
      ;

  int size() const { return size_; }
  bool empty() const { return size_ == 0; }
  const CoreEvent& operator[](int i) const { return items_[i]; }
  uint32_t dropped() const { return dropped_; }
  void clear() {
    size_ = 0;
    dropped_ = 0;
  }
  /// Verdadeiro se algum evento com este código está na lista (útil nos testes).
  bool contains(EventCode code) const;

 private:
  CoreEvent items_[kCapacity];
  CoreEvent overflow_;  // destino de push() quando cheia
  int size_ = 0;
  uint32_t dropped_ = 0;
};

}  // namespace sem
