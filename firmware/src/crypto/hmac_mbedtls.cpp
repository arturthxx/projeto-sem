// HMAC-SHA256 via mbedtls (md.h, função de passo único mbedtls_md_hmac).
#include "crypto/hmac_mbedtls.h"

#include <mbedtls/md.h>

namespace semcrypto {

bool MbedtlsHmacSha256::compute(const uint8_t* key, size_t key_len, const uint8_t* msg, size_t msg_len,
                                uint8_t out[sem::kHmacLen]) const {
  const mbedtls_md_info_t* info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  if (info == nullptr || key == nullptr || out == nullptr) return false;
  if (msg == nullptr && msg_len != 0) return false;
  return mbedtls_md_hmac(info, key, key_len, msg, msg_len, out) == 0;
}

}  // namespace semcrypto
