// =============================================================================
// src/crypto/hmac_mbedtls.h — HMAC-SHA256 com a mbedtls do ESP-IDF
// (aceleração SHA por hardware do ESP32), implementando sem::HmacSha256.
// A comparação em tempo constante fica no núcleo (sem::constant_time_equal).
// =============================================================================
#pragma once

#include "sem_core/command_validator.h"

namespace semcrypto {

class MbedtlsHmacSha256 : public sem::HmacSha256 {
 public:
  bool compute(const uint8_t* key, size_t key_len, const uint8_t* msg, size_t msg_len,
               uint8_t out[sem::kHmacLen]) const override;
};

}  // namespace semcrypto
