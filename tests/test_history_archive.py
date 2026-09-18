from datetime import datetime, timezone

import pytest

from windows_agent.worker.history_archive import load_or_create_history_document


def test_history_archive_chunks_same_trade_and_is_immutable_on_retry(tmp_path):
    path = tmp_path / "history.json"
    events = [
        {"event_id": f"event-{index}", "external_trade_id": "position-1"}
        for index in range(5)
    ]
    first = load_or_create_history_document(
        path,
        job_id="11111111-1111-4111-8111-111111111111",
        connection_id="22222222-2222-4222-8222-222222222222",
        account_number="42",
        server="Demo",
        history_mode="from_date",
        from_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        events=events,
    )
    retry = load_or_create_history_document(
        path,
        job_id=first["job_id"],
        connection_id=first["connection_id"],
        account_number="42",
        server="Demo",
        history_mode="from_date",
        from_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        events=[],
    )

    assert [len(group["events"]) for group in first["trades"]] == [4, 1]
    assert retry == first


@pytest.mark.parametrize(
    ("history_mode", "from_date"),
    [
        ("all_available", None),
        ("from_date", datetime(2026, 1, 2, tzinfo=timezone.utc)),
    ],
)
def test_history_archive_rejects_retry_with_changed_scope(
    tmp_path, history_mode, from_date
):
    path = tmp_path / "history.json"
    load_or_create_history_document(
        path,
        job_id="11111111-1111-4111-8111-111111111111",
        connection_id="22222222-2222-4222-8222-222222222222",
        account_number="42",
        server="Demo",
        history_mode="from_date",
        from_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        events=[],
    )

    with pytest.raises(ValueError, match="archive identity mismatch"):
        load_or_create_history_document(
            path,
            job_id="11111111-1111-4111-8111-111111111111",
            connection_id="22222222-2222-4222-8222-222222222222",
            account_number="42",
            server="Demo",
            history_mode=history_mode,
            from_date=from_date,
            events=[],
        )


def test_history_archive_never_rewrites_an_existing_invalid_file(tmp_path):
    path = tmp_path / "history.json"
    original = b"{}\n"
    path.write_bytes(original)

    with pytest.raises(ValueError, match="archive identity mismatch"):
        load_or_create_history_document(
            path,
            job_id="11111111-1111-4111-8111-111111111111",
            connection_id="22222222-2222-4222-8222-222222222222",
            account_number="42",
            server="Demo",
            history_mode="all_available",
            from_date=None,
            events=[],
        )

    assert path.read_bytes() == original
