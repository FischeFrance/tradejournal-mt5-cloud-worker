from __future__ import annotations

import json

import httpx
import pytest

from windows_agent.mtapi_search import (
    MTAPI_SEARCH_URL,
    MtApiSearchClient,
    MtApiSearchContractError,
    MtApiSearchUnavailable,
)


def _client(handler, *, now=1_785_190_000_000):
    return MtApiSearchClient(
        httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False),
        now_unix_ms=lambda: now,
    )


def _response(payload, status=200, content_type="application/json"):
    return httpx.Response(status, json=payload, headers={"content-type": content_type})


def test_search_exact_uses_only_server_identifier_as_query_and_no_credentials():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request):
        requests.append(request)
        return _response([])

    result = _client(handler).search_exact("PepperstoneUK-Live")

    assert result.outcome == "NO_MATCH"
    assert len(requests) == 1
    assert str(requests[0].url).split("?")[0] == MTAPI_SEARCH_URL
    assert dict(requests[0].url.params) == {"company": "PepperstoneUK-Live"}
    assert requests[0].content == b""
    assert "authorization" not in requests[0].headers


def test_search_exact_returns_deduplicated_sorted_exact_ip_candidates():
    payload = [
        {
            "companyName": "Pepperstone Limited",
            "results": [
                {"name": "PepperstoneUK-Live", "access": ["35.179.214.239:1950", "13.134.102.187:443", "13.134.102.187:443"]},
                {"name": "PepperstoneEU-Live", "access": ["13.134.102.187:443"]},
            ],
        }
    ]
    result = _client(lambda request: _response(payload)).search_exact("pepperstoneuk-live")

    assert result.outcome == "EXACT_MATCH"
    assert result.company_names == ("Pepperstone Limited",)
    assert [item.server_address for item in result.candidates] == ["13.134.102.187:443", "35.179.214.239:1950"]
    assert all(item.status == "CANDIDATE" for item in result.candidates)
    audit = result.to_audit_document("PepperstoneUK-Live")
    assert audit["source_url"] == MTAPI_SEARCH_URL
    assert "password" not in json.dumps(audit).lower()


def test_search_exact_preserves_safe_hostname_and_orders_it_before_ips():
    payload = [{
        "companyName": "Fortune Prime Limited",
        "results": [{
            "name": "FortunePrime-Live2",
            "access": [
                "38.76.16.208:443",
                "ga-bp14h62c0bwpfhso8fwxp.aliyunga0017.com:443",
                "192.229.23.47:443",
            ],
        }],
    }]

    result = _client(lambda request: _response(payload)).search_exact("FortunePrime-Live2")

    assert [item.server_address for item in result.candidates] == [
        "ga-bp14h62c0bwpfhso8fwxp.aliyunga0017.com:443",
        "38.76.16.208:443",
        "192.229.23.47:443",
    ]
    assert result.candidates[0].is_ip_address is False


def test_search_exact_ignores_one_unsafe_access_value_without_losing_valid_routes():
    payload = [{
        "companyName": "Fortune Prime Limited",
        "results": [{
            "name": "FortunePrime-Live2",
            "access": [
                "172.24.73.9:443",
                "ga-bp14h62c0bwpfhso8fwxp.aliyunga0017.com:443",
                "38.76.16.208:443",
            ],
        }],
    }]

    result = _client(lambda request: _response(payload)).search_exact("FortunePrime-Live2")

    assert [item.server_address for item in result.candidates] == [
        "ga-bp14h62c0bwpfhso8fwxp.aliyunga0017.com:443",
        "38.76.16.208:443",
    ]


def test_search_exact_returns_no_match_and_ambiguous_with_untrusted_candidates():
    no_match = _client(lambda request: _response([{"companyName": "FPM Trading", "results": [{"name": "FPMTrading-Live", "access": ["188.42.136.4:443"]}]}])).search_exact("Other-Live")
    ambiguous = _client(lambda request: _response([
        {"companyName": "One", "results": [{"name": "Same-Live", "access": ["8.8.8.8:443"]}]},
        {"companyName": "Two", "results": [{"name": "Same-Live", "access": ["1.1.1.1:443"]}]},
    ])).search_exact("Same-Live")

    assert no_match.outcome == "NO_MATCH" and not no_match.candidates
    assert ambiguous.outcome == "AMBIGUOUS" and ambiguous.company_names == ("One", "Two")
    assert [item.server_address for item in ambiguous.candidates] == ["1.1.1.1:443", "8.8.8.8:443"]


def test_search_exact_preserves_company_ambiguity_for_one_shared_endpoint():
    result = _client(lambda request: _response([
        {"companyName": "Pepperstone Limited", "results": [{"name": "PepperstoneUK-Live", "access": ["1.1.1.1:443"]}]},
        {"companyName": "Pepperstone Group", "results": [{"name": "PepperstoneUK-Live", "access": ["1.1.1.1:443"]}]},
    ])).search_exact("PepperstoneUK-Live")

    assert result.outcome == "AMBIGUOUS"
    assert len(result.candidates) == 1
    assert result.candidates[0].company_names == (
        "Pepperstone Group",
        "Pepperstone Limited",
    )
    assert result.to_audit_document("PepperstoneUK-Live")["candidates"][0]["company_names"] == [
        "Pepperstone Group",
        "Pepperstone Limited",
    ]


@pytest.mark.parametrize("server", ["", " bad\nserver", "../server", "x" * 129])
def test_search_exact_rejects_invalid_server_before_network(server):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return _response([])

    with pytest.raises(MtApiSearchContractError, match="server identifier"):
        _client(handler).search_exact(server)
    assert calls == 0


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [{"companyName": "FPM", "results": "invalid"}],
        [{"companyName": "FPM", "results": [{"name": "FPMTrading-Live", "access": ["127.0.0.1:443"]}]}],
        [{"companyName": "FPM", "results": [{"name": "FPMTrading-Live", "access": ["203.0.113.10:443"]}]}],
        [{"companyName": "FPM", "results": [{"name": "FPMTrading-Live", "access": ["localhost:443"]}]}],
        [{"companyName": "FPM", "results": [{"name": "FPMTrading-Live", "access": ["8.8.8.8:70000"]}]}],
    ],
)
def test_search_exact_rejects_malformed_or_unsafe_exact_results(payload):
    with pytest.raises(MtApiSearchContractError):
        _client(lambda request: _response(payload)).search_exact("FPMTrading-Live")


def test_search_exact_rejects_redirect_and_non_json_response():
    with pytest.raises(MtApiSearchContractError, match="redirect"):
        _client(lambda request: httpx.Response(302, headers={"location": "https://elsewhere.example"})).search_exact("FPMTrading-Live")
    with pytest.raises(MtApiSearchContractError, match="response is invalid"):
        _client(lambda request: _response([], content_type="text/plain")).search_exact("FPMTrading-Live")


def test_search_exact_retries_one_transient_failure_only():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503) if calls == 1 else _response([])

    assert _client(handler).search_exact("FPMTrading-Live").outcome == "NO_MATCH"
    assert calls == 2


def test_search_exact_surfaces_only_sanitized_unavailable_error():
    with pytest.raises(MtApiSearchUnavailable) as exc_info:
        _client(lambda request: httpx.Response(401, text="password=unsafe-token")).search_exact("FPMTrading-Live")
    assert "unsafe-token" not in str(exc_info.value)
    assert "password" not in str(exc_info.value).lower()
