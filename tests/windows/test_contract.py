from __future__ import annotations

import json
from pathlib import Path

import httpx
import jsonschema
import pytest

from windows_agent.api_client import AgentApiClient, AgentContractError

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
        ("claimResponseJob", "claimResponseJob_liveSync"),
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
        ("progressRequest", "progressRequest"),
        ("progressResponseOk", "progressResponseOk"),
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


def test_claim_rejects_unknown_job_type_before_dispatch() -> None:
    body = {**FIXTURES["claimResponseJob_liveSync"], "job_type": "future_job"}
    client = AgentApiClient(
        "https://agent.example/",
        "fixture",
        httpx.MockTransport(lambda r: httpx.Response(200, json=body)),
    )
    with pytest.raises(AgentContractError, match="job_type"):
        client.claim()


def test_claim_rejects_provision_without_typed_secret_envelope() -> None:
    body = {**FIXTURES["claimResponseJob_provision"], "payload": {}}
    client = AgentApiClient(
        "https://agent.example/",
        "fixture",
        httpx.MockTransport(lambda r: httpx.Response(200, json=body)),
    )
    with pytest.raises(AgentContractError, match="provision payload"):
        client.claim()


def test_claim_accepts_null_broker_for_backend_identity_resolution() -> None:
    fixture = FIXTURES["claimResponseJob_provision"]
    body = {
        **fixture,
        "payload": {**fixture["payload"], "broker_label": None},
    }
    client = AgentApiClient(
        "https://agent.example/",
        "fixture",
        httpx.MockTransport(lambda r: httpx.Response(200, json=body)),
    )

    result = client.claim()

    assert result["payload"]["broker_label"] is None
    validate_against("claimResponseJob", result)


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


def test_progress_request_body_matches_schema_and_contains_no_arbitrary_metadata() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=FIXTURES["progressResponseOk"])

    client = AgentApiClient(
        "https://agent.example/", "fixture", httpx.MockTransport(handler)
    )
    job = FIXTURES["claimResponseJob_provision"]
    result = client.progress(
        job["job_id"],
        job["lease_id"],
        "broker_discovery",
        "started",
        "wizard_required",
    )

    validate_against("progressRequest", captured["body"])
    validate_against("progressResponseOk", result)
    assert set(captured["body"]) == {
        "api_version",
        "lease_id",
        "event_code",
        "event_status",
        "detail_code",
    }


def test_progress_rejects_unallowlisted_or_free_text_values_locally() -> None:
    client = AgentApiClient(
        "https://agent.example/",
        "fixture",
        httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    job = FIXTURES["claimResponseJob_provision"]
    with pytest.raises(ValueError, match="event code"):
        client.progress(job["job_id"], job["lease_id"], "read_password", "started")
    with pytest.raises(ValueError, match="detail code"):
        client.progress(
            job["job_id"],
            job["lease_id"],
            "broker_discovery",
            "started",
            "raw detail!",
        )


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
