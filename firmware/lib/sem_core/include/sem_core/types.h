// =============================================================================
// sem_core/types.h — tipos e enumerações comuns da lógica pura do firmware.
//
// Vocabulário normativo (docs/05 §1 e docs/06): modos do EMS, estados por
// regras, códigos de segurança e tipos de sincronização do SOC. As funções
// to_string()/parse_*() usam EXATAMENTE as grafias do contrato.
//
// Nada aqui depende de Arduino/ESP-IDF: compila no PC (testes nativos).
// =============================================================================
#pragma once

#include <cstddef>
#include <cstdint>
#include <limits>

namespace sem {

// ---------------------------------------------------------------------------
// Limites estáticos (sem alocação dinâmica na lógica de controle)
// ---------------------------------------------------------------------------
constexpr int kMaxLoads = 8;        // = maxItems de set_loads (schemas/command.schema.json)
constexpr int kMaxWindows = 4;      // janelas por carga no perfil de demanda
constexpr int kMaxOcvPoints = 16;   // pontos da curva OCV x SOC
constexpr int kKeyLen = 16;         // chave curta da carga ("led", "fan", "res")
constexpr int kReasonLen = 192;     // texto de 'reason' das decisões
constexpr double kDaySeconds = 86400.0;
constexpr double kNegInf = -std::numeric_limits<double>::infinity();

/// Prioridade de uma carga (docs/05 §1).
enum class Priority : uint8_t { High = 0, Medium = 1, Low = 2 };

/// Modo do EMS (docs/05 §1).
enum class EmsMode : uint8_t { Conventional = 0, Rules = 1, Predictive = 2 };

/// Estado do EMS por regras. O valor numérico é o NÍVEL de docs/06 §3.2
/// (PROTECTION=0, CRITICAL=1, ECONOMY=2, NORMAL=3).
enum class EmsState : uint8_t { Protection = 0, Critical = 1, Economy = 2, Normal = 3 };

/// Código do supervisor de segurança (docs/06 §2).
enum class SafetyCode : uint8_t { Ok = 0, Boot, Lvd, Ovp, Oc, OcLockout, Ot, Sensor };

/// Origem da última sincronização do estimador de SOC (docs/06 §1).
enum class SyncKind : uint8_t { None = 0, OcvInit, Persisted, Full, Empty, Manual };

const char* to_string(Priority p);
const char* to_string(EmsMode m);
const char* to_string(EmsState s);
const char* to_string(SafetyCode c);
const char* to_string(SyncKind k);

/// Conversões texto -> enum. Retornam false se o texto não pertence ao vocabulário.
bool parse_priority(const char* s, Priority* out);
bool parse_mode(const char* s, EmsMode* out);
bool parse_state(const char* s, EmsState* out);
bool parse_safety_code(const char* s, SafetyCode* out);
bool parse_sync_kind(const char* s, SyncKind* out);

/// Saída atual de uma carga (APÓS o supervisor de segurança).
struct LoadOutput {
  bool on = false;
  double duty = 0.0;
  double last_switch_t_s = kNegInf;  // instante (monotônico) da última transição on<->off
};

/// Comando para uma carga (decisão do EMS ou plano do MPC).
struct LoadCommand {
  bool on = false;
  double duty = 0.0;
};

/// Cópia limitada de string C (sempre termina em '\0'). Utilizável em constexpr.
template <size_t N>
constexpr void copy_cstr(char (&dst)[N], const char* src) {
  size_t i = 0;
  if (src != nullptr) {
    for (; i + 1 < N && src[i] != '\0'; ++i) dst[i] = src[i];
  }
  for (; i < N; ++i) dst[i] = '\0';
}

/// Limita x ao intervalo [lo, hi].
constexpr double clamp(double x, double lo, double hi) { return x < lo ? lo : (x > hi ? hi : x); }

}  // namespace sem
