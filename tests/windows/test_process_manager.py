from pathlib import Path

from windows_agent.provisioning.process_manager import ProcessManager


def test_stale_pid_cleanup_is_idempotent(tmp_path: Path) -> None:
    manager = ProcessManager(tmp_path / "state.json")
    manager.state_path.write_text(
        '{"pid":2147483647,"executable":"C:\\\\missing\\\\terminal64.exe"}',
        encoding="utf-8",
    )

    assert manager.stop() is True
    assert manager.stop() is False
