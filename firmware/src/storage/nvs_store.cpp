// Persistência em NVS com Preferences.
#include "storage/nvs_store.h"

#include <Preferences.h>

#include <cmath>
#include <cstring>

namespace semstore {

namespace {
Preferences g_prefs;
constexpr const char* kNamespace = "sem";
constexpr uint32_t kEnergyBlobVersion = 1;

struct EnergyBlob {
  uint32_t version;
  int32_t day_index;
  sem::EnergyCounters energy;
  uint32_t n_switch[sem::kMaxLoads];
};
}  // namespace

bool NvsStore::begin() {
  ok_ = g_prefs.begin(kNamespace, false);
  return ok_;
}

uint32_t NvsStore::increment_boot_id() {
  if (!ok_) return 0;
  const uint32_t id = g_prefs.getUInt("boot_id", 0) + 1;
  g_prefs.putUInt("boot_id", id);
  return id;
}

bool NvsStore::load(PersistedState* out) {
  if (!ok_ || out == nullptr) return false;
  *out = PersistedState{};
  if (g_prefs.isKey("soc") && g_prefs.isKey("unc")) {
    const double soc = g_prefs.getDouble("soc", NAN);
    const double unc = g_prefs.getDouble("unc", NAN);
    if (std::isfinite(soc) && std::isfinite(unc) && soc >= 0.0 && soc <= 1.0 && unc >= 0.0 && unc <= 1.0) {
      out->has_soc = true;
      out->soc = soc;
      out->unc = unc;
    }
  }
  if (g_prefs.isKey("energy") && g_prefs.getBytesLength("energy") == sizeof(EnergyBlob)) {
    EnergyBlob b{};
    if (g_prefs.getBytes("energy", &b, sizeof(b)) == sizeof(b) && b.version == kEnergyBlobVersion) {
      out->has_energy = true;
      out->day_index = b.day_index;
      out->energy = b.energy;
      std::memcpy(out->n_switch, b.n_switch, sizeof(out->n_switch));
    }
  }
  return true;
}

bool NvsStore::save_soc(double soc, double unc) {
  if (!ok_) return false;
  return g_prefs.putDouble("soc", soc) == sizeof(double) && g_prefs.putDouble("unc", unc) == sizeof(double);
}

bool NvsStore::save_energy(int32_t day_index, const sem::EnergyCounters& e, const uint32_t* n_switch, int n) {
  if (!ok_) return false;
  EnergyBlob b{};
  b.version = kEnergyBlobVersion;
  b.day_index = day_index;
  b.energy = e;
  for (int i = 0; i < n && i < sem::kMaxLoads; ++i) b.n_switch[i] = n_switch[i];
  return g_prefs.putBytes("energy", &b, sizeof(b)) == sizeof(b);
}

bool NvsStore::clear_soc() {
  if (!ok_) return false;
  g_prefs.remove("soc");
  g_prefs.remove("unc");
  return true;
}

}  // namespace semstore
