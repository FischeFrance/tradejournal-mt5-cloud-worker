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
        ("historyBatchRequest", "historyBatchRequest"),
        ("historyBatchResponse", "historyBatchResponse"),
        ("historyFilePrepareRequest", "historyFilePrepareRequest"),
        ("historyFilePrepareResponse", "historyFilePrepareResponse"),
        ("historyFileImportRequest", "historyFileImportRequest"),
        ("historyFileImportResponse", "historyFileImportResponse"),
        ("sessionRequest", "sessionRequest"),
        ("sessionResponse", "sessionResponse"),
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


def test_session_request_body_matches_schema() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=FIXTURES["sessionResponse"])

    client = AgentApiClient("https://agent.example/", "fixture", httpx.MockTransport(handler))
    response = client.session()
    validate_against("sessionRequest", captured["body"])
    validate_against("sessionResponse", response)


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


def test_history_batch_request_and_response_match_contract() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=FIXTURES["historyBatchResponse"])

    client = AgentApiClient(
        "https://agent.example/", "fixture", httpx.MockTransport(handler)
    )
    job = FIXTURES["claimResponseJob_historicalSync"]
    response = client.history_batch(
        job["job_id"], job["lease_id"], FIXTURES["historyBatchRequest"]["events"]
    )
    validate_against("historyBatchRequest", captured["body"])
    validate_against("historyBatchResponse", response)


def test_history_batch_rejects_acknowledgement_count_mismatch() -> None:
    client = AgentApiClient(
        "https://agent.example/",
        "fixture",
        httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"api_version": "1", "accepted": 0, "inserted": 0, "duplicates": 0},
            )
        ),
    )
    job = FIXTURES["claimResponseJob_historicalSync"]
    with pytest.raises(Exception, match="acknowledgement count mismatch"):
        client.history_batch(
            job["job_id"], job["lease_id"], FIXTURES["historyBatchRequest"]["events"]
        )


def test_history_file_prepare_upload_and_import_match_contract() -> None:
    captured = []
    prepare_response = {
        **FIXTURES["historyFilePrepareResponse"],
        "upload_url": (
            "https://agent.example/storage/v1/object/upload/sign/"
            "mt5-history-imports/path?token=fake"
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.method == "PUT":
            return httpx.Response(200, json={"Key": "uploaded"})
        if request.url.path.endswith("history-upload"):
            return httpx.Response(200, json=prepare_response)
        return httpx.Response(200, json=FIXTURES["historyFileImportResponse"])

    client = AgentApiClient(
        "https://agent.example/", "fixture", httpx.MockTransport(handler)
    )
    job = FIXTURES["claimResponseJob_historicalSync"]
    request = FIXTURES["historyFilePrepareRequest"]
    prepared = client.history_file_prepare(
        job["job_id"],
        job["lease_id"],
        compressed_sha256=request["compressed_sha256"],
        compressed_bytes=request["compressed_bytes"],
        uncompressed_bytes=request["uncompressed_bytes"],
        event_count=request["event_count"],
    )
    client.upload_history_file(prepared["upload_url"], b"gzip-payload")
    imported = client.history_file_import(job["job_id"], job["lease_id"], 1200)

    validate_against("historyFilePrepareRequest", json.loads(captured[0].content))
    assert captured[1].headers.get("authorization") is None
    assert captured[1].headers["content-type"] == "application/gzip"
    validate_against("historyFileImportRequest", json.loads(captured[2].content))
    validate_against("historyFileImportResponse", imported)


def test_history_file_refuses_signed_upload_on_another_origin() -> None:
    client = AgentApiClient("https://agent.example/", "fixture")
    with pytest.raises(Exception, match="signed upload target is invalid"):
        client.upload_history_file(
            "https://attacker.example/storage/v1/object/upload/sign/"
            "mt5-history-imports/path?token=fake",
            b"payload",
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
