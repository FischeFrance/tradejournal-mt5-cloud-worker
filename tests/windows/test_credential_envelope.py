from __future__ import annotations

import pytest

from windows_agent.credential_envelope import decrypt_credential_envelope

# Real cross-runtime fixture: this exact envelope was produced by Node's WebCrypto using the
# SAME algorithm as tradejournal-drp/supabase/functions/_shared/cloudSyncCrypto.ts's
# encryptCredentials({investor_password: "demo-secret-123"}, KEY) -- not a value invented on the
# Python side. This is fabricated test data (no real credential), generated once to prove actual
# interoperability between the two runtimes, then hardcoded here as a fixed regression vector.
REAL_KEY = "A3pTi6Omvm2o+F4Di+qR3+qCQ3Xr1enpL8qvs89tX4Y="
REAL_ENVELOPE = {
    "alg": "aes-256-gcm-v1",
    "iv": "UrKXmgk/O3bCZSK2",
    "ciphertext": "cm75mNRjhFD6xMjNAEQkqxGyn9uGH8X1chkM2n/3mwNBA3vtw+6cHTIMNHnNzx+olXWtqaLdiQ==",
}


def test_decrypts_a_real_webcrypto_produced_envelope():
    result = decrypt_credential_envelope(REAL_ENVELOPE, REAL_KEY)
    assert result == {"investor_password": "demo-secret-123"}


def test_rejects_unsupported_alg():
    with pytest.raises(ValueError, match="unsupported envelope alg"):
        decrypt_credential_envelope({**REAL_ENVELOPE, "alg": "aes-128-cbc"}, REAL_KEY)


def test_rejects_wrong_key_length():
    with pytest.raises(ValueError, match="32 bytes"):
        decrypt_credential_envelope(REAL_ENVELOPE, "dG9vc2hvcnQ=")


def test_rejects_tampered_ciphertext():
    tampered = {**REAL_ENVELOPE, "ciphertext": REAL_ENVELOPE["ciphertext"][:-4] + "AAAA"}
    with pytest.raises(Exception):
        decrypt_credential_envelope(tampered, REAL_KEY)


def test_rejects_wrong_key():
    wrong_key = "B" * 43 + "="
    with pytest.raises(Exception):
        decrypt_credential_envelope(REAL_ENVELOPE, wrong_key)
