// =============================================================================
// src/storage/nvs_store.h — persistência em NVS (Preferences, namespace "sem").
//
// Chaves: boot_id (contador de inicializações), soc/unc (estimador §1),
// energy (contadores do dia + comutações, blob versionado).
// Regra de persistência (docs/06 §1, limita o desgaste da flash): o SOC é
// gravado a cada persist_every_s OU quando |soc − soc_gravado| ≥
// persist_delta_soc (sem::SocPersistPolicy); os contadores de energia vão
// junto e também na virada do dia. Sem hora válida, os contadores de energia
// não são restaurados (não se sabe se o dia mudou).
// =============================================================================
#pragma once

#include <cstdint>

#include "sem_core/energy.h"

namespace semstore {

struct PersistedState {
  bool has_soc = false;
  double soc = 0.0;
  double unc = 0.0;
  bool has_energy = false;
  int32_t day_index = 0;
  sem::EnergyCounters energy{};
  uint32_t n_switch[sem::kMaxLoads] = {};
};

class NvsStore {
 public:
  bool begin();
  bool ok() const { return ok_; }

  /// Incrementa e grava o contador de boots; retorna o novo valor (0 em falha).
  uint32_t increment_boot_id();

  bool load(PersistedState* out);
  bool save_soc(double soc, double unc);
  bool save_energy(int32_t day_index, const sem::EnergyCounters& e, const uint32_t* n_switch, int n);
  bool clear_soc();

 private:
  bool ok_ = false;
};

}  // namespace semstore
