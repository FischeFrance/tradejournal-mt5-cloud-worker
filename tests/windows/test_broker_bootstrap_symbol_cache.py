from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from windows_agent.provisioning.secret_store import WindowsSecretStore
from windows_agent.worker.broker_bootstrap_symbol_cache import BrokerBootstrapSymbolCache


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(WindowsSecretStore, "restrict_shared_service_acl", staticmethod(lambda path: None))
    return BrokerBootstrapSymbolCache(tmp_path / "state" / "broker-bootstrap-symbols", now=lambda: 1_800_000_000)


def store(cache, server="Demo", build=6249, preferred="EURUSD", symbol="EURUSD.raw"):
    return cache.store_verified(
        canonical_server=server, terminal_build=build, preferred_base=preferred, verified_symbol=symbol,
    )


def lookup(cache, server="Demo", build=6249, preferred="EURUSD"):
    return cache.lookup(canonical_server=server, terminal_build=build, preferred_base=preferred)


def test_verified_hint_is_broker_build_and_preference_scoped_with_minimal_payload(cache):
    assert store(cache)
    assert lookup(cache, server="dEMO") == "EURUSD.raw"
    assert lookup(cache, server="Other") is None
    assert lookup(cache, build=6250) is None
    assert lookup(cache, preferred="GBPUSD") is None
    record = json.loads(next(cache.root.glob("*.json")).read_text())
    assert record == {
        "canonical_server": "demo", "terminal_build": 6249, "preferred_base": "EURUSD",
        "verified_symbol": "EURUSD.raw", "verified_at": 1_800_000_000,
    }


def test_concurrent_broker_publications_do_not_overwrite_each_other(cache):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda index: store(cache, server=f"Broker-{index}", symbol=f"EURUSD.{index}"), range(24)))
    assert all(results)
    assert len(list(cache.root.glob("*.json"))) == 24
    assert not list(cache.root.glob(".*"))
    for index in range(24):
        assert lookup(cache, server=f"Broker-{index}") == f"EURUSD.{index}"


@pytest.mark.parametrize("mutation", [
    {"canonical_server": "other"}, {"terminal_build": 6250}, {"terminal_build": True},
    {"preferred_base": "GBPUSD"}, {"verified_symbol": "EURUSD\nPassword=bad"},
    {"verified_symbol": ""}, {"verified_at": True}, {"verified_at": 1_800_000_301},
    {"verified_at": 1_800_000_000 - BrokerBootstrapSymbolCache.MAX_AGE_SECONDS - 1},
    {"verified_at": 10**1000}, {"login": 42}, {"token": "fixture"},
])
def test_tampered_stale_future_or_account_bearing_record_is_a_cache_miss(cache, mutation):
    assert store(cache)
    path = next(cache.root.glob("*.json"))
    record = json.loads(path.read_text())
    record.update(mutation)
    path.write_text(json.dumps(record), encoding="utf-8")
    assert lookup(cache) is None


@pytest.mark.parametrize("content", ["{", "[]", "{}", "x" * 4097, "[" * 2000 + "]" * 2000])
def test_unreadable_or_malformed_record_is_a_cache_miss(cache, content):
    assert store(cache)
    next(cache.root.glob("*.json")).write_text(content, encoding="utf-8")
    assert lookup(cache) is None


@pytest.mark.parametrize("params", [
    {"server": "../Demo"}, {"server": " Demo"}, {"server": "Demo\0"},
    {"server": ""}, {"build": True}, {"build": 0}, {"preferred": "EURUSD\r\n"},
    {"symbol": "EURUSD\n"}, {"symbol": "x" * 65},
])
def test_invalid_key_or_symbol_does_not_create_state(cache, params):
    assert not store(cache, **params)
    assert not cache.root.exists()


def test_atomic_publication_is_protected_for_system_and_administrators(cache, monkeypatch):
    acl = Mock()
    monkeypatch.setattr(WindowsSecretStore, "restrict_shared_service_acl", acl)
    assert store(cache)
    assert acl.call_args_list[0].args == (cache.root,)
    assert acl.call_args_list[1].args == (next(cache.root.glob("*.json")),)


def test_acl_publication_failure_is_non_fatal_and_never_publishes_a_hint(cache, monkeypatch):
    monkeypatch.setattr(WindowsSecretStore, "restrict_shared_service_acl", Mock(side_effect=OSError("denied")))
    assert not store(cache)
    assert lookup(cache) is None
    assert not list(cache.root.glob("*.json"))


@pytest.mark.parametrize("failure_index", [0, 1])
def test_non_oserror_from_either_windows_acl_call_remains_non_fatal(cache, monkeypatch, failure_index):
    side_effect = [RuntimeError("fixture pywin32-like error")]
    if failure_index:
        side_effect.insert(0, None)
    acl = Mock(side_effect=side_effect)
    monkeypatch.setattr(WindowsSecretStore, "restrict_shared_service_acl", acl)
    assert not store(cache)
    assert acl.call_count == failure_index + 1
    # The file ACL is applied after atomic publication beneath the protected directory.
    assert lookup(cache) == ("EURUSD.raw" if failure_index else None)


def test_symbol_file_or_directory_symlink_is_ignored(cache, tmp_path):
    assert store(cache)
    path = next(cache.root.glob("*.json"))
    original = tmp_path / "original.json"
    path.replace(original)
    path.symlink_to(original)
    assert lookup(cache) is None
    assert not store(cache)
    path.unlink()
    cache.root.rmdir()
    other = tmp_path / "other"
    other.mkdir()
    cache.root.symlink_to(other, target_is_directory=True)
    assert lookup(cache) is None
    assert not store(cache)
    assert not list(other.iterdir())


def test_cache_under_symlinked_state_parent_is_not_trusted(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    parent = tmp_path / "state"
    parent.symlink_to(outside, target_is_directory=True)
    cache = BrokerBootstrapSymbolCache(parent / "broker-bootstrap-symbols")
    assert not store(cache)
    assert lookup(cache) is None
    assert not list(outside.iterdir())
