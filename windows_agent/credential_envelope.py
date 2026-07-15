from __future__ import annotations

import base64
import json
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

"""Decrypts the AES-256-GCM envelope produced server-side by
tradejournal-drp/supabase/functions/_shared/cloudSyncCrypto.ts's encryptCredentials(), delivered
inside a provisioning job's payload.credential_envelope (see request-mt5-connection/index.ts).

WebCrypto's `crypto.subtle.encrypt({name:"AES-GCM"}, ...)` appends the 16-byte auth tag to the end
of the returned ciphertext -- this is the same "ciphertext || tag" layout `cryptography`'s AESGCM
expects, so no reformatting is needed between the two runtimes. Verified by an actual
cross-runtime round trip in tests/windows/test_credential_envelope.py (a real envelope produced
by Node's WebCrypto, decrypted here), not assumed from documentation alone.
"""

SUPPORTED_ALG = "aes-256-gcm-v1"
KEY_BYTE_LENGTH = 32


def decrypt_credential_envelope(envelope: dict[str, Any], key_base64: str) -> dict[str, Any]:
    if envelope.get("alg") != SUPPORTED_ALG:
        raise ValueError(f"unsupported envelope alg: {envelope.get('alg')!r}")
    key = base64.b64decode(key_base64)
    if len(key) != KEY_BYTE_LENGTH:
        raise ValueError(f"encryption key must decode to {KEY_BYTE_LENGTH} bytes, got {len(key)}")
    iv = base64.b64decode(envelope["iv"])
    ciphertext = base64.b64decode(envelope["ciphertext"])
    plaintext = AESGCM(key).decrypt(iv, ciphertext, None)
    return json.loads(plaintext.decode("utf-8"))
