"""Run one digest-addressed live-sync dead-letter repair as LocalSystem."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from windows_agent.deadletter_repair import (  # noqa: E402
    DeadLetterRepairError,
    load_service_environment,
    read_repair_request,
    repair_live_dead_letter,
    require_local_system_and_stopped_service,
    runtime_paths_from_environment,
    write_private_result,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    arguments = parser.parse_args()
    result_path = Path(arguments.result)
    try:
        require_local_system_and_stopped_service()
        request = read_repair_request(Path(arguments.request))
        environment = load_service_environment()
        instances_root, secrets_root, rollback_root, ingestion_url = (
            runtime_paths_from_environment(environment)
        )
        result = repair_live_dead_letter(
            request,
            instances_root=instances_root,
            secrets_root=secrets_root,
            rollback_root=rollback_root,
            ingestion_url=ingestion_url,
        )
        write_private_result(result_path, result)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except DeadLetterRepairError as exc:
        result = {
            "schema_version": 1,
            "status": "failed",
            "error": str(exc),
        }
    except Exception:
        result = {
            "schema_version": 1,
            "status": "failed",
            "error": "unexpected_error",
        }
    try:
        write_private_result(result_path, result)
    except Exception:
        pass
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
