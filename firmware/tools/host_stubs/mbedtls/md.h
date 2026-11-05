// STUB (host): subconjunto de mbedtls/md.h (mbedtls 2.28 / 3.x).
#pragma once
#include <stddef.h>
#ifdef __cplusplus
extern "C" {
#endif
typedef enum { MBEDTLS_MD_NONE = 0, MBEDTLS_MD_SHA256 = 6 } mbedtls_md_type_t;
typedef struct mbedtls_md_info_t mbedtls_md_info_t;
const mbedtls_md_info_t* mbedtls_md_info_from_type(mbedtls_md_type_t md_type);
int mbedtls_md_hmac(const mbedtls_md_info_t* md_info, const unsigned char* key, size_t keylen,
                    const unsigned char* input, size_t ilen, unsigned char* output);
#ifdef __cplusplus
}
#endif
