from datetime import datetime, timezone
from uuid import uuid4

import pytest

from windows_agent.maintenance_recovery import MaintenanceRecovery
from windows_agent.state_store import atomic_json, read_json
from windows_agent.worker.maintenance_mt5_runtime import NativeMt5Runtime


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
    assert recovery.allows(connection_id)
    assert not recovery.allows(str(uuid4()))
    expected_status = object()
    monkeypatch.setattr(NativeMt5Runtime, 'resume', lambda *_args, **_kwargs: expected_status)
    cutoff = datetime.now(timezone.utc)
    assert recovery.runtime(root, connection_id).resume(history_from=cutoff) is expected_status
    queued = [call for call in api.calls if call[2]['operation'] == 'recover']
    if candidate:
        assert queued == []
        assert not (root / 'state/job_progress.json').exists()
    else:
        assert len(queued) == 1
        assert queued[0][2]['from_date'] == cutoff.isoformat()
        assert queued[0][2]['release_id'] == 'a' * 64
        assert read_json(root / 'state/job_progress.json')['maintenance_recovery_job_id'] == api.job_id
