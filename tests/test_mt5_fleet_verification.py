from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

from windows_agent.mt5_fleet_verification import Mt5FleetVerifier
from windows_agent.provisioning.mt5_instance import InstanceProvisioner
from windows_agent.state_store import atomic_json
from windows_agent.worker.mql5_file_adapter import Mql5FileIdentityMismatch, Mql5FileStale

PINNED = b"bridge-1.01"


class Secrets:
    def read(self, _connection_id, name):
        return "42" if name == "mt5_login" else "Broker-Live"


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _account(instances: Path, *, bridge: bytes = PINNED, build: bytes = b"6274") -> str:
    connection_id = str(uuid4())
    root = instances / connection_id
    (root / "state").mkdir(parents=True)
    expert = root / "terminal" / "MQL5" / "Experts" / "TradeJournal" / "TradeJournalBridge.ex5"
    expert.parent.mkdir(parents=True)
    expert.write_bytes(bridge)
    (root / "terminal" / "terminal64.exe").write_bytes(build)
    atomic_json(root / "state" / "instance.json", {"connection_id": connection_id, "status": "provisioned"})
    return connection_id


def _verifier(tmp_path: Path, heartbeats: dict, pids: dict, ids=None, clock=None):
    """``heartbeats`` maps an account to a list of outcomes, one per read."""
    template = tmp_path / "template" / "terminal64.exe"
    template.parent.mkdir(exist_ok=True)
    template.write_bytes(b"6274")
    pinned = tmp_path / "pinned.ex5"
    pinned.write_bytes(PINNED)

    class Adapter:
        def __init__(self, files, connection_id, login, server, state):
            assert (login, server) == (42, "Broker-Live")
            self.outcome = heartbeats[connection_id].pop(0) if len(heartbeats[connection_id]) > 1 else heartbeats[connection_id][0]

        def terminal_info(self):
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return SimpleNamespace(connected=self.outcome.get("connected", True))

        def account_info(self):
            return SimpleNamespace(trade_allowed=self.outcome.get("trade_allowed", False))

    clock = clock or Clock()
    return Mt5FleetVerifier(
        instances_root=tmp_path / "instances",
        secrets=Secrets(),
        expert_sha256=InstanceProvisioner._sha256(pinned),
        template_terminal=template,
        build_reader=lambda path: int(Path(path).read_bytes()),
        connection_ids=(lambda: ids) if ids is not None else None,
        adapter_factory=Adapter,
        terminal_pids=lambda root, connection_id: pids.get(connection_id, [100]),
        settle_seconds=30,
        poll_seconds=5,
        sleep=clock.sleep,
        clock=clock,
    ), clock


def test_every_check_names_the_first_problem_of_each_account(tmp_path: Path) -> None:
    instances = tmp_path / "instances"
    healthy = _account(instances)
    stopped = _account(instances)
    doubled = _account(instances)
    old_bridge = _account(instances, bridge=b"bridge-1.00")
    old_build = _account(instances, build=b"6249")
    trading = _account(instances)
    other_account = _account(instances)
    offline = _account(instances)
    ids = [healthy, stopped, doubled, old_bridge, old_build, trading, other_account, offline]
    heartbeats = {cid: [{}] for cid in ids}
    heartbeats[trading] = [{"trade_allowed": True}]
    heartbeats[other_account] = [Mql5FileIdentityMismatch("account_identity_mismatch")]
    heartbeats[offline] = [{"connected": False}]
    verifier, _clock = _verifier(tmp_path, heartbeats, {stopped: [], doubled: [1, 2]}, ids)

    result = verifier.verify(Event())

    assert result.checked == tuple(sorted(ids))
    assert dict(result.issues) == {
        stopped: "not_running",
        doubled: "process_ambiguous",
        old_bridge: "expert_outdated",
        old_build: "mt5_outdated",
        trading: "trading_enabled",
        other_account: "identity_mismatch",
        offline: "disconnected",
    }
    assert result.healthy == 1


def test_a_terminal_still_logging_in_gets_a_second_look(tmp_path: Path) -> None:
    instances = tmp_path / "instances"
    restarting = _account(instances)
    heartbeats = {restarting: [Mql5FileStale("heartbeat_stale"), {"connected": False}, {}]}
    verifier, clock = _verifier(tmp_path, heartbeats, {}, [restarting])

    result = verifier.verify(Event())

    assert result.issues == ()
    assert clock.sleeps == [5, 5]


def test_a_problem_that_stays_is_reported_once_the_wait_ends(tmp_path: Path) -> None:
    instances = tmp_path / "instances"
    silent = _account(instances)
    verifier, clock = _verifier(tmp_path, {silent: [Mql5FileStale("heartbeat_stale")]}, {}, [silent])

    result = verifier.verify(Event())

    assert result.issues == ((silent, "heartbeat_stale"),)
    assert sum(clock.sleeps) == 30


def test_without_tonights_inventory_every_provisioned_terminal_is_checked(tmp_path: Path) -> None:
    instances = tmp_path / "instances"
    first = _account(instances)
    second = _account(instances)
    (instances / "not-an-account").mkdir()
    verifier, _clock = _verifier(tmp_path, {first: [{}], second: [{}]}, {}, ids=[])

    result = verifier.verify(Event())

    assert result.checked == tuple(sorted((first, second)))
    assert result.issues == ()


def test_an_unreadable_account_never_stops_the_others(tmp_path: Path) -> None:
    instances = tmp_path / "instances"
    healthy = _account(instances)
    missing = str(uuid4())
    verifier, _clock = _verifier(tmp_path, {healthy: [{}]}, {}, [healthy, missing])

    result = verifier.verify(Event())

    assert dict(result.issues) == {missing: "not_provisioned"}
