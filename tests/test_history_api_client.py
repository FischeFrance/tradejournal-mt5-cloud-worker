import gzip
import io
import json
import logging

import httpx
import pytest

from windows_agent.api_client import AgentApiClient, AgentContractError
from windows_agent.security import RedactionFilter


JOB_ID = "11111111-1111-4111-8111-111111111111"
CONNECTION_ID = "22222222-2222-4222-8222-222222222222"
USER_ID = "44444444-4444-4444-8444-444444444444"
OBJECT_PATH = f"{USER_ID}/{CONNECTION_ID}/{JOB_ID}/history.json.gz"


def _document(trades=None):
    return {
        "schema_version": 1,
        "job_id": JOB_ID,
        "connection_id": CONNECTION_ID,
        "account_number": "42",
        "server": "Demo",
        "generated_at": "2026-01-02T00:00:00Z",
        "history_mode": "all_available",
        "from_date": None,
        "trades": [] if trades is None else trades,
    }


def _prepare_response(upload_url, *, already_imported=False, object_path=OBJECT_PATH):
    return {
        "api_version": "1",
        "already_imported": already_imported,
        "object_path": object_path,
        "upload_url": upload_url,
        "expires_in": 0 if already_imported else 7200,
        "accepted": 0,
        "inserted": 0,
        "duplicates": 0,
    }


def _signed_url(host="agent.example", object_path=OBJECT_PATH, token="signed"):
    return (
        f"https://{host}/storage/v1/object/upload/sign/"
        f"mt5-history-imports/{object_path}?token={token}"
    )


def test_history_file_uses_lease_route_and_does_not_leak_agent_token():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.startswith("/storage/v1/object/upload/sign/"):
            assert "authorization" not in request.headers
            assert request.headers["content-type"] == "application/gzip"
            decoded = json.loads(gzip.decompress(request.content))
            assert decoded["job_id"] == job_id
            return httpx.Response(200, json={})
        if request.url.path.endswith("/history-upload"):
            return httpx.Response(
                200,
                json={
                    "api_version": "1",
                    "already_imported": False,
                    "object_path": OBJECT_PATH,
                    "upload_url": _signed_url(),
                    "expires_in": 7200,
                    "accepted": 0,
                    "inserted": 0,
                    "duplicates": 0,
                },
            )
        if request.url.path.endswith("/history-import"):
            return httpx.Response(
                200,
                json={
                    "api_version": "1",
                    "accepted": 1,
                    "inserted": 0,
                    "duplicates": 1,
                    "object_deleted": True,
                },
            )
        raise AssertionError(f"unexpected URL: {request.url}")

    job_id = JOB_ID
    transport = httpx.MockTransport(handler)
    client = AgentApiClient("https://agent.example/", "agent-secret", transport)
    document = _document([
        {
            "external_trade_id": "position-1",
            "events": [{"event_id": "same-existing-id", "external_trade_id": "position-1"}],
        }
    ])

    result = client.import_history_file(
        job_id, "33333333-3333-4333-8333-333333333333", document
    )

    assert result["duplicates"] == 1
    agent_requests = [
        request
        for request in requests
        if not request.url.path.startswith("/storage/v1/object/upload/sign/")
    ]
    assert [request.url.path.rsplit("/", 1)[-1] for request in agent_requests] == [
        "history-upload",
        "history-import",
    ]
    assert all(request.headers["authorization"] == "Bearer agent-secret" for request in agent_requests)


def test_history_file_accepts_storage_origin_for_same_supabase_project():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "project-ref.supabase.co":
            assert request.url.path.startswith("/storage/v1/object/upload/sign/")
            assert "authorization" not in request.headers
            return httpx.Response(200)
        if request.url.path.endswith("/history-upload"):
            return httpx.Response(200, json=_prepare_response(
                _signed_url("project-ref.supabase.co")
            ))
        if request.url.path.endswith("/history-import"):
            return httpx.Response(
                200,
                json={
                    "api_version": "1",
                    "accepted": 0,
                    "inserted": 0,
                    "duplicates": 0,
                    "object_deleted": True,
                },
            )
        raise AssertionError(f"unexpected URL: {request.url}")

    client = AgentApiClient(
        "https://project-ref.functions.supabase.co/trading-agent",
        "agent-secret",
        httpx.MockTransport(handler),
    )

    result = client.import_history_file(
        JOB_ID,
        "33333333-3333-4333-8333-333333333333",
        _document(),
    )

    assert result["accepted"] == 0
    assert any(request.url.host == "project-ref.supabase.co" for request in requests)


@pytest.mark.parametrize(
    "upload_url",
    (
        _signed_url("attacker.example"),
        _signed_url("project-ref.supabase.co.evil.example"),
        _signed_url("other-project.supabase.co"),
    ),
)
def test_history_file_rejects_upload_url_outside_same_supabase_project(upload_url):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/history-upload"):
            return httpx.Response(200, json=_prepare_response(upload_url))
        raise AssertionError(f"unexpected URL: {request.url}")

    client = AgentApiClient(
        "https://project-ref.functions.supabase.co/trading-agent",
        "agent-secret",
        httpx.MockTransport(handler),
    )

    with pytest.raises(AgentContractError, match="upload URL is invalid"):
        client.import_history_file(
            JOB_ID,
            "33333333-3333-4333-8333-333333333333",
            _document(),
        )


def test_history_upload_error_does_not_expose_signed_token():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/history-upload"):
            return httpx.Response(200, json=_prepare_response(
                _signed_url(token="super-secret")
            ))
        if request.url.path.startswith("/storage/v1/object/upload/sign/"):
            return httpx.Response(500, text="failed")
        raise AssertionError(f"unexpected URL: {request.url}")

    client = AgentApiClient(
        "https://agent.example/",
        "agent-secret",
        httpx.MockTransport(handler),
    )

    with pytest.raises(AgentContractError) as caught:
        client.import_history_file(
            JOB_ID,
            "33333333-3333-4333-8333-333333333333",
            _document(),
        )
    assert "super-secret" not in str(caught.value)


def test_history_upload_signed_token_is_redacted_from_http_logs():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/history-upload"):
            return httpx.Response(200, json=_prepare_response(
                _signed_url(token="signed-production-secret")
            ))
        if request.url.path.startswith("/storage/v1/object/upload/sign/"):
            return httpx.Response(500)
        raise AssertionError(f"unexpected URL: {request.url}")

    stream = io.StringIO()
    log_handler = logging.StreamHandler(stream)
    log_handler.addFilter(RedactionFilter())
    logger = logging.getLogger("httpx")
    old_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(log_handler)
    try:
        client = AgentApiClient(
            "https://agent.example/",
            "agent-secret",
            httpx.MockTransport(handler),
        )
        with pytest.raises(AgentContractError):
            client.import_history_file(
                JOB_ID,
                "33333333-3333-4333-8333-333333333333",
                _document(),
            )
    finally:
        logger.removeHandler(log_handler)
        logger.setLevel(old_level)

    assert "signed-production-secret" not in stream.getvalue()
    assert "token=<redacted>" in stream.getvalue()


def test_history_upload_requires_exact_storage_object_path():
    wrong_path = f"{USER_ID}/{CONNECTION_ID}/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/history.json.gz"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/history-upload"):
            return httpx.Response(
                200,
                json=_prepare_response(
                    _signed_url(object_path=wrong_path), object_path=wrong_path
                ),
            )
        raise AssertionError(f"unexpected URL: {request.url}")

    client = AgentApiClient(
        "https://agent.example/", "agent-secret", httpx.MockTransport(handler)
    )
    with pytest.raises(AgentContractError, match="object identity mismatch"):
        client.import_history_file(
            JOB_ID,
            "33333333-3333-4333-8333-333333333333",
            _document(),
        )


def test_history_import_requires_storage_object_cleanup_confirmation():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/history-upload"):
            return httpx.Response(200, json=_prepare_response(None, already_imported=True))
        if request.url.path.endswith("/history-import"):
            return httpx.Response(
                200,
                json={
                    "api_version": "1",
                    "accepted": 0,
                    "inserted": 0,
                    "duplicates": 0,
                    "object_deleted": False,
                },
            )
        raise AssertionError(f"unexpected URL: {request.url}")

    client = AgentApiClient(
        "https://agent.example/",
        "agent-secret",
        httpx.MockTransport(handler),
    )

    with pytest.raises(AgentContractError, match="history import response"):
        client.import_history_file(
            JOB_ID,
            "33333333-3333-4333-8333-333333333333",
            _document(),
        )
