from __future__ import annotations

import io

import pytest

from windows_agent import store_agent_secret
from windows_agent.agent_secrets import AGENT_SCOPE_ID
from windows_agent.provisioning.secret_store import WindowsSecretStore


def test_store_agent_secret_round_trips_and_verifies(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("s3cret-token-value\n"))
    monkeypatch.setattr(
        "sys.argv",
        ["store_agent_secret", "--name", "agent_token", "--secrets-root", str(tmp_path)],
    )
    assert store_agent_secret.main() == 0

    captured = capsys.readouterr()
    assert "s3cret-token-value" not in captured.out
    assert "stored and verified" in captured.out

    store = WindowsSecretStore(tmp_path)
    assert store.read(AGENT_SCOPE_ID, "agent_token") == "s3cret-token-value"


def test_store_agent_secret_rejects_empty_value(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    monkeypatch.setattr(
        "sys.argv",
        ["store_agent_secret", "--name", "agent_token", "--secrets-root", str(tmp_path)],
    )
    with pytest.raises(ValueError, match="empty secret value"):
        store_agent_secret.main()


def test_store_agent_secret_rejects_unknown_name(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("value\n"))
    monkeypatch.setattr(
        "sys.argv",
        ["store_agent_secret", "--name", "not_allowed", "--secrets-root", str(tmp_path)],
    )
    with pytest.raises(SystemExit):
        store_agent_secret.main()
