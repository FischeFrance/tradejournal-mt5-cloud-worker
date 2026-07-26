from __future__ import annotations

import json
import pickle
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.credential_provider import (
    Credentials,
    CredentialLeakError,
    FakeCredentialProvider,
    SecretString,
    json_dumps_safe,
)


SECRET_VALUE = "correct-horse-battery-staple"


class SecretStringTests(unittest.TestCase):
    def test_repr_never_contains_raw_value(self):
        self.assertNotIn(SECRET_VALUE, repr(SecretString(SECRET_VALUE)))

    def test_str_never_contains_raw_value(self):
        self.assertNotIn(SECRET_VALUE, str(SecretString(SECRET_VALUE)))

    def test_fstring_format_never_contains_raw_value(self):
        rendered = f"{SecretString(SECRET_VALUE)}"
        self.assertNotIn(SECRET_VALUE, rendered)

    def test_reveal_returns_raw_value(self):
        self.assertEqual(SecretString(SECRET_VALUE).reveal(), SECRET_VALUE)

    def test_equality_is_value_based_and_constant_time_path(self):
        self.assertEqual(SecretString(SECRET_VALUE), SecretString(SECRET_VALUE))
        self.assertNotEqual(SecretString(SECRET_VALUE), SecretString("other"))

    def test_pickle_is_blocked(self):
        with self.assertRaises(CredentialLeakError):
            pickle.dumps(SecretString(SECRET_VALUE))

    def test_plain_json_dumps_does_not_silently_serialize(self):
        with self.assertRaises(TypeError):
            json.dumps({"password": SecretString(SECRET_VALUE)})


class JsonDumpsSafeTests(unittest.TestCase):
    def test_raises_on_top_level_secret(self):
        with self.assertRaises(CredentialLeakError):
            json_dumps_safe(SecretString(SECRET_VALUE))

    def test_raises_on_nested_dict_secret(self):
        with self.assertRaises(CredentialLeakError):
            json_dumps_safe({"account": "acc-1", "password": SecretString(SECRET_VALUE)})

    def test_raises_on_secret_inside_list(self):
        with self.assertRaises(CredentialLeakError):
            json_dumps_safe({"values": [SecretString(SECRET_VALUE)]})

    def test_serializes_normally_once_secret_is_revealed_and_dropped(self):
        rendered = json_dumps_safe({"account": "acc-1", "server": "188.42.136.4:443"})
        self.assertEqual(json.loads(rendered), {"account": "acc-1", "server": "188.42.136.4:443"})


class FakeCredentialProviderTests(unittest.TestCase):
    def test_round_trips_registered_credentials(self):
        provider = FakeCredentialProvider()
        provider.register("acc-1", login="12345", password=SECRET_VALUE, investor_password="investor-pw")
        credentials = provider.get("acc-1")
        self.assertIsInstance(credentials, Credentials)
        self.assertEqual(credentials.password.reveal(), SECRET_VALUE)
        self.assertEqual(credentials.investor_password.reveal(), "investor-pw")

    def test_unknown_account_raises_key_error(self):
        provider = FakeCredentialProvider()
        with self.assertRaises(KeyError):
            provider.get("missing")


if __name__ == "__main__":
    unittest.main()
