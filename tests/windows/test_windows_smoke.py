from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from windows_agent.job_runner import JobRunner
from windows_agent.provisioning.mt5_instance import InstanceProvisioner
from windows_agent.provisioning.secret_store import WindowsSecretStore
from windows_agent.worker.dedup import PersistentDedup
from windows_agent.worker.fake_mt5_adapter import FakeMt5Adapter
from windows_agent.worker.history_sync import HistorySync
from windows_agent.worker.live_sync import LiveSync


class MemorySnapshot:
    def __init__(self) -> None:
        self.value = {"positions": {}, "orders": {}, "deals": {}}

    def get(self):
        return self.value

    def save(self, value):
        self.value = value


class QueueApi:
    def __init__(self, jobs):
        self.jobs = list(jobs)
        self.completed = []

    def claim(self):
        return self.jobs.pop(0) if self.jobs else {}

    def heartbeat(self, job_id, lease_id):
        return {"lease_valid": True}

    def transition(self, job_id, lease_id, status, result=None):
        if status == "complete":
            self.completed.append(job_id)
        return {}


def test_full_mock_agent_smoke(tmp_path, monkeypatch):
    monkeypatch.setattr(WindowsSecretStore, "delete_connection", lambda *args: None)
    cid = str(uuid4())
    provisioner = InstanceProvisioner(tmp_path / "instances", tmp_path / "secrets")
    snapshots = [
        {"positions": {}, "orders": {}, "deals": {}},
        {
            "positions": {
                "1": {
                    "ticket": "1",
                    "symbol": "EURUSD",
                    "direction": "buy",
                    "volume": 0.1,
                    "open_price": 1.1,
                }
            },
            "orders": {},
            "deals": {},
        },
    ]
    fake = FakeMt5Adapter(snapshots)
    events = []
    live = LiveSync(
        fake,
        MemorySnapshot(),
        PersistentDedup(tmp_path / "dedup.sqlite"),
        events.append,
    )
    history = HistorySync(fake, tmp_path / "history.json", lambda value: None)
    jobs = [
        {"job_id": "1", "job_type": "provision", "connection_id": cid, "lease_id": "l1"},
        {"job_id": "2", "job_type": "live", "connection_id": cid, "lease_id": "l2"},
        {"job_id": "3", "job_type": "historical_sync", "connection_id": cid, "lease_id": "l3"},
        {"job_id": "4", "job_type": "deprovision", "connection_id": cid, "lease_id": "l4"},
    ]
    api = QueueApi(jobs)
    runner = JobRunner(
        tmp_path / "agent-state.json",
        api,
        {
            "provision": lambda job: {"root": str(provisioner.provision(cid))},
            "live": lambda job: {"events": (live.poll_once(), live.poll_once())[-1]},
            "historical_sync": lambda job: history.run(
                "from_date", datetime.now(timezone.utc)
            ),
            "deprovision": lambda job: (
                provisioner.deprovision(cid) or {"deprovisioned": True}
            ),
        },
    )
    while runner.run_once():
        pass
    assert api.completed == ["1", "2", "3", "4"]
    assert len(events) == 1


def test_powershell_scripts_parse():
    import subprocess

    root = Path(__file__).parents[2] / "scripts" / "windows"
    for script in root.glob("*.ps1"):
        command = f"[scriptblock]::Create((Get-Content -Raw -LiteralPath '{script}')) | Out-Null"
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{script.name}: {result.stderr}"
