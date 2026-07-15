from __future__ import annotations

import json
from pathlib import Path

import httpx
import jsonschema
import pytest

from windows_agent.api_client import AgentApiClient

CONTRACT_DIR = Path(__file__).parents[2] / "contracts" / "mt5-agent-v1"
SCHEMA = json.loads((CONTRACT_DIR / "schema.json").read_text(encoding="utf-8"))
FIXTURES = json.loads((CONTRACT_DIR / "fixtures.json").read_text(encoding="utf-8"))


def validate_against(def_name: str, value: object) -> None:
    resolver = jsonschema.RefResolver.from_schema(SCHEMA)
    definition = SCHEMA["$defs"][def_name]
    jsonschema.validate(instance=value, schema=definition, resolver=resolver)


@pytest.mark.parametrize(
    "def_name,fixture_key",
    [
        ("claimRequest", "claimRequest"),
        ("claimResponseJob", "claimResponseJob_provision"),
        ("claimResponseJob", "claimResponseJob_historicalSync"),
        ("heartbeatRequest", "heartbeatRequest"),
        ("heartbeatResponseOk", "heartbeatResponseOk"),
        ("heartbeatResponseLeaseLost", "heartbeatResponseLeaseLost"),
        ("transitionRequest", "runningRequest"),
        ("transitionResponseOk", "runningResponseOk"),
        ("transitionRequest", "completeRequest"),
        ("transitionResponseOk", "completeResponseOk"),
        ("transitionRequest", "failRequest"),
        ("transitionResponseOk", "failResponseOk"),
        ("transitionResponseLeaseLost", "transitionResponseLeaseLost"),
        ("errorResponse", "errorResponse_unauthorized"),
    ],
)
def test_fixtures_match_schema(def_name: str, fixture_key: str) -> None:
    validate_against(def_name, FIXTURES[fixture_key])


def test_claim_request_body_matches_schema() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        job = FIXTURES["claimResponseJob_provision"]
        return httpx.Response(200, json=job)

    client = AgentApiClient("https://agent.example/", "fixture", httpx.MockTransport(handler))
    response = client.claim()
    validate_against("claimRequest", captured["body"])
    validate_against("claimResponseJob", response)


def test_claim_returns_falsy_on_204_no_job() -> None:
    """Regression test: claim() used to call response.json() unconditionally,
    which raises on an empty 204 body -- a real 'no job available' response
    would have crashed the agent instead of returning falsy."""

    client = AgentApiClient(
        "https://agent.example/", "fixture", httpx.MockTransport(lambda r: httpx.Response(204))
    )
    assert not client.claim()


def test_heartbeat_lease_lost_returns_body_instead_of_raising() -> None:
    """Regression test: a 409 lease_lost response used to hit raise_for_status()
    and raise HTTPStatusError, bypassing JobRunner's lease_valid check entirely."""

    body = FIXTURES["heartbeatResponseLeaseLost"]
    client = AgentApiClient(
        "https://agent.example/", "fixture", httpx.MockTransport(lambda r: httpx.Response(409, json=body))
    )
    result = client.heartbeat(FIXTURES["claimResponseJob_provision"]["job_id"], FIXTURES["claimResponseJob_provision"]["lease_id"])
    assert result == body
    validate_against("heartbeatResponseLeaseLost", result)


def test_transition_lease_lost_returns_body_instead_of_raising() -> None:
    body = FIXTURES["transitionResponseLeaseLost"]
    client = AgentApiClient(
        "https://agent.example/", "fixture", httpx.MockTransport(lambda r: httpx.Response(409, json=body))
    )
    job = FIXTURES["claimResponseJob_provision"]
    result = client.transition(job["job_id"], job["lease_id"], "complete")
    assert result == body


def test_transient_5xx_is_retried_then_succeeds() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json=FIXTURES["heartbeatResponseOk"])

    sleeps: list[float] = []
    client = AgentApiClient(
        "https://agent.example/",
        "fixture",
        httpx.MockTransport(handler),
        sleep_fn=sleeps.append,
    )
    job = FIXTURES["claimResponseJob_provision"]
    result = client.heartbeat(job["job_id"], job["lease_id"])
    assert result == FIXTURES["heartbeatResponseOk"]
    assert calls["count"] == 3
    assert len(sleeps) == 2


def test_persistent_5xx_raises_after_max_retries() -> None:
    client = AgentApiClient(
        "https://agent.example/",
        "fixture",
        httpx.MockTransport(lambda r: httpx.Response(500)),
        max_retries=2,
        sleep_fn=lambda _seconds: None,
    )
    with pytest.raises(httpx.HTTPStatusError):
        client.claim()


def test_401_unauthorized_is_not_retried() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(401, json={"error_code": "unauthorized"})

    client = AgentApiClient("https://agent.example/", "fixture", httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        client.claim()
    assert calls["count"] == 1
