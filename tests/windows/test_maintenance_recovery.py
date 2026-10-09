from datetime import datetime, timezone
from uuid import uuid4
from types import SimpleNamespace

import pytest

from windows_agent.maintenance_recovery import MaintenanceRecovery, MaintenanceRecoveryUnavailable
from windows_agent.state_store import atomic_json, read_json
from windows_agent.worker.maintenance_mt5_runtime import NativeMt5Runtime
from windows_agent.worker.native_mt5_runtime import NativeMt5Runtime as CurrentNativeMt5Runtime


class Api:
    def __init__(self, connection_id):
        self.connection_id, self.job_id = connection_id, str(uuid4())
        self.calls = []

    def request(self, method, path, payload):
        self.calls.append((method, path, payload))
        if payload['operation'] == 'inventory':
            return {'api_version': '1', 'connection_ids': [self.connection_id]}
        return {'api_version': '1', 'job_id': self.job_id, 'status': 'pending'}


@pytest.mark.parametrize('candidate', [False, True])
def test_only_published_active_accounts_queue_a_bounded_recovery(tmp_path, monkeypatch, candidate):
    connection_id = str(uuid4())
    instances = tmp_path / 'instances'
    root = (tmp_path / 'candidate' if candidate else instances) / connection_id
    (root / 'state').mkdir(parents=True)
    atomic_json(root / 'state/instance.json', {'template_code_manifest_sha256': 'a' * 64})
    api = Api(connection_id)
    recovery = MaintenanceRecovery(api, instances)
    recovery.refresh()
    monkeypatch.setattr(recovery, 'identity', lambda _cid: (42, 'Demo'))
    assert recovery.allows(connection_id)
    assert not recovery.allows(str(uuid4()))
    expected_status = object()
    monkeypatch.setattr(NativeMt5Runtime, 'resume', lambda *_args, **_kwargs: expected_status)
    cutoff = datetime.now(timezone.utc)
    atomic_json(root / 'state/maintenance-recovery.json', {'baseline_sha256': 'b' * 64})
    monkeypatch.setattr('windows_agent.maintenance_recovery.load_baseline', lambda *_args: {'captured_at': cutoff.isoformat()})
    assert recovery.runtime(root, connection_id).resume(history_from=cutoff) is expected_status
    queued = [call for call in api.calls if call[2]['operation'] == 'recover']
    if candidate:
        assert queued == []
        assert not (root / 'state/job_progress.json').exists()
    else:
        assert len(queued) == 1
        assert queued[0][2]['from_date'] == cutoff.isoformat()
        assert queued[0][2]['release_id'] == 'a' * 64
        assert queued[0][2]['baseline_sha256'] == 'b' * 64
        assert read_json(root / 'state/job_progress.json')['maintenance_recovery_job_id'] == api.job_id
        assert read_json(root / 'state/job_progress.json')['status'] == 'recovering_history'


def test_rotation_fails_before_restart_when_native_gap_delivery_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(CurrentNativeMt5Runtime, '_setting', staticmethod(lambda _name: ''))
    connection_id = str(uuid4())
    api = Api(connection_id)
    recovery = MaintenanceRecovery(api, tmp_path / 'instances')
    with pytest.raises(MaintenanceRecoveryUnavailable, match='native_maintenance_recovery_unavailable'):
        recovery.refresh_for_rotation()
    assert [call[2]['operation'] for call in api.calls] == ['inventory']


def test_rotation_requires_ticket_capture_for_all_assigned_accounts(tmp_path, monkeypatch):
    connection_id = str(uuid4())
    recovery = MaintenanceRecovery(Api(connection_id), tmp_path / 'instances')
    monkeypatch.setattr(CurrentNativeMt5Runtime, '_setting', staticmethod(lambda _name: '1'))
    monkeypatch.setattr('windows_agent.maintenance_recovery.verify_ticket_reader', lambda: None)
    monkeypatch.setattr(recovery, 'identity', lambda _cid: (42, 'Demo'))
    captured = []
    monkeypatch.setattr('windows_agent.maintenance_recovery.capture_baseline', lambda root, cid, *_args: captured.append(cid))
    recovery.refresh_for_rotation()
    assert captured == [connection_id]


def test_unfinished_recovery_blocks_next_rotation_before_capture_or_stop(tmp_path, monkeypatch):
    connection_id = str(uuid4())
    root = tmp_path / 'instances' / connection_id
    (root / 'state').mkdir(parents=True)
    atomic_json(root / 'state/maintenance-recovery.json', {'status': 'queued'})
    recovery = MaintenanceRecovery(Api(connection_id), tmp_path / 'instances')
    monkeypatch.setattr(CurrentNativeMt5Runtime, '_setting', staticmethod(lambda _name: '1'))
    monkeypatch.setattr('windows_agent.maintenance_recovery.verify_ticket_reader', lambda: None)
    with pytest.raises(MaintenanceRecoveryUnavailable):
        recovery.refresh_for_rotation()


def test_maintenance_runtime_retains_shared_terminal_action_dispatcher():
    assert NativeMt5Runtime._run_terminal_ui_action is CurrentNativeMt5Runtime._run_terminal_ui_action


def test_queue_failure_restores_live_and_retains_unconfirmed_gap(tmp_path, monkeypatch):
    cid = str(uuid4())
    root = tmp_path / 'instances' / cid
    (root / 'state').mkdir(parents=True)
    atomic_json(root / 'state/instance.json', {'template_code_manifest_sha256': 'a' * 64})
    atomic_json(root / 'state/maintenance-recovery.json', {'baseline_sha256': 'b' * 64, 'status': 'prepared'})
    recovery = MaintenanceRecovery(Api(cid), tmp_path / 'instances')
    recovery.refresh()
    monkeypatch.setattr(recovery, 'identity', lambda _cid: (42, 'Demo'))
    monkeypatch.setattr('windows_agent.maintenance_recovery.load_baseline', lambda *_args: {'captured_at': datetime.now(timezone.utc).isoformat()})
    monkeypatch.setattr(NativeMt5Runtime, 'resume', lambda *_args, **_kwargs: object())
    restored = []
    monkeypatch.setattr(CurrentNativeMt5Runtime, 'switch_to_new_only', lambda *_args: restored.append(True))
    monkeypatch.setattr('windows_agent.maintenance_recovery.Mql5FileMt5Adapter', lambda *_args: SimpleNamespace(verify_identity=lambda: None, account_info=lambda: SimpleNamespace(trade_allowed=False)))
    def unavailable(*_args):
        raise RuntimeError('control plane unavailable')
    monkeypatch.setattr(recovery.api, 'request', unavailable)
    with pytest.raises(RuntimeError, match='control plane unavailable'):
        recovery.runtime(root, cid).resume()
    assert restored == [True]
    assert read_json(root / 'state/job_progress.json')['status'] == 'connected'
    assert read_json(root / 'state/maintenance-recovery.json')['status'] == 'queue_unconfirmed'
