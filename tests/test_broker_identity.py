from __future__ import annotations

import json

import pytest

from windows_agent.broker_identity import (
    BrokerIdentityError,
    BrokerIdentitySuggestion,
    CachedBrokerIdentityResolver,
    FakeBrokerIdentityProvider,
    OpenAIBrokerIdentityProvider,
)


def _suggestion(generated_at_unix_ms: int = 1_000) -> BrokerIdentitySuggestion:
    return BrokerIdentitySuggestion(
        broker_label="FPM Trading",
        search_text="FPM Trading",
        confidence="HIGH",
        source_urls=("https://fpm.example/mt5",),
        generated_at_unix_ms=generated_at_unix_ms,
    )


def test_cached_resolver_calls_provider_once_within_ttl(tmp_path) -> None:
    provider = FakeBrokerIdentityProvider(_suggestion())
    resolver = CachedBrokerIdentityResolver(
        tmp_path / "identity-cache.json",
        provider,
        ttl_seconds=60,
        now_unix_ms=lambda: 1_000,
    )

    first = resolver.resolve("FPMTrading-Live")
    second = resolver.resolve("FPMTrading-Live")

    assert first == second
    assert provider.calls == ["FPMTrading-Live"]


def test_cached_resolver_refreshes_expired_candidate(tmp_path) -> None:
    provider = FakeBrokerIdentityProvider(_suggestion(generated_at_unix_ms=70_000))
    cache = tmp_path / "identity-cache.json"
    cache.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": {
                    "fpmtrading-live": {
                        "server_identifier": "FPMTrading-Live",
                        "broker_label": "Old Broker",
                        "search_text": "Old Broker",
                        "confidence": "HIGH",
                        "source_urls": ["https://old.example/mt5"],
                        "generated_at_unix_ms": 1_000,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    resolver = CachedBrokerIdentityResolver(
        cache,
        provider,
        ttl_seconds=60,
        now_unix_ms=lambda: 70_000,
    )

    result = resolver.resolve("FPMTrading-Live")

    assert result.broker_label == "FPM Trading"
    assert provider.calls == ["FPMTrading-Live"]


def test_corrupt_cache_and_inconclusive_provider_fail_closed(tmp_path) -> None:
    cache = tmp_path / "identity-cache.json"
    cache.write_text("{", encoding="utf-8")
    resolver = CachedBrokerIdentityResolver(
        cache,
        FakeBrokerIdentityProvider(_suggestion()),
        ttl_seconds=60,
        now_unix_ms=lambda: 1_000,
    )
    with pytest.raises(BrokerIdentityError, match="cache"):
        resolver.resolve("FPMTrading-Live")

    with pytest.raises(BrokerIdentityError, match="confidence"):
        CachedBrokerIdentityResolver(
            tmp_path / "new-cache.json",
            FakeBrokerIdentityProvider(
                BrokerIdentitySuggestion(
                    broker_label="Guess",
                    search_text="Guess",
                    confidence="LOW",
                    source_urls=("https://example.com/source",),
                    generated_at_unix_ms=1_000,
                )
            ),
            ttl_seconds=60,
            now_unix_ms=lambda: 1_000,
        ).resolve("Unknown-Live")


def test_cache_is_sanitized_and_contains_no_credentials(tmp_path) -> None:
    cache = tmp_path / "identity-cache.json"
    resolver = CachedBrokerIdentityResolver(
        cache,
        FakeBrokerIdentityProvider(_suggestion()),
        ttl_seconds=60,
        now_unix_ms=lambda: 1_000,
    )

    resolver.resolve("FPMTrading-Live")
    serialized = cache.read_text(encoding="utf-8").lower()

    assert "password" not in serialized
    assert "credential" not in serialized
    assert "account" not in serialized


def test_openai_provider_preserves_specific_validated_search_text(
    monkeypatch,
) -> None:
    class Response:
        output_text = json.dumps(
            {
                "broker_label": "Pepperstone",
                "search_text": "PepperstoneUK",
                "confidence": "HIGH",
                "source_urls": ["https://pepperstone.example/mt5"],
                "ambiguous": False,
            }
        )

        @staticmethod
        def model_dump():
            return {
                "output": [
                    {
                        "annotations": [
                            {"url": "https://pepperstone.example/mt5"},
                        ]
                    }
                ]
            }

    class Responses:
        @staticmethod
        def create(**_kwargs):
            return Response()

    class Client:
        responses = Responses()

    provider = object.__new__(OpenAIBrokerIdentityProvider)
    provider._client = Client()
    provider._model = "fixture-model"
    monkeypatch.setattr("windows_agent.broker_identity.time.time", lambda: 1.0)

    suggestion = provider.resolve("PepperstoneUK-Live")

    assert suggestion.broker_label == "Pepperstone"
    assert suggestion.search_text == "PepperstoneUK"


def test_openai_provider_rejects_search_text_for_another_broker(
    monkeypatch,
) -> None:
    class Response:
        output_text = json.dumps(
            {
                "broker_label": "Pepperstone",
                "search_text": "Unrelated Broker",
                "confidence": "HIGH",
                "source_urls": ["https://pepperstone.example/mt5"],
                "ambiguous": False,
            }
        )

        @staticmethod
        def model_dump():
            return {
                "output": [
                    {
                        "annotations": [
                            {"url": "https://pepperstone.example/mt5"},
                        ]
                    }
                ]
            }

    class Responses:
        @staticmethod
        def create(**_kwargs):
            return Response()

    class Client:
        responses = Responses()

    provider = object.__new__(OpenAIBrokerIdentityProvider)
    provider._client = Client()
    provider._model = "fixture-model"
    monkeypatch.setattr("windows_agent.broker_identity.time.time", lambda: 1.0)

    with pytest.raises(BrokerIdentityError, match="conflicts"):
        provider.resolve("PepperstoneUK-Live")


def test_legacy_cache_refreshes_under_specific_search_prompt(tmp_path) -> None:
    cache = tmp_path / "identity-cache.json"
    cache.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": {
                    "pepperstoneuk-live": {
                        "server_identifier": "PepperstoneUK-Live",
                        "broker_label": "Pepperstone",
                        "search_text": "Pepperstone",
                        "confidence": "MEDIUM",
                        "source_urls": ["https://pepperstone.example/mt5"],
                        "generated_at_unix_ms": 1_000,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    provider = FakeBrokerIdentityProvider(
        BrokerIdentitySuggestion(
            broker_label="Pepperstone",
            search_text="PepperstoneUK",
            confidence="HIGH",
            source_urls=("https://pepperstone.example/mt5",),
            generated_at_unix_ms=2_000,
        )
    )
    resolver = CachedBrokerIdentityResolver(
        cache,
        provider,
        ttl_seconds=60,
        now_unix_ms=lambda: 2_000,
    )

    suggestion = resolver.resolve("PepperstoneUK-Live")

    assert suggestion.search_text == "PepperstoneUK"
    assert provider.calls == ["PepperstoneUK-Live"]
    assert json.loads(cache.read_text(encoding="utf-8"))["schema_version"] == 2
