"""Reject releases that silently drop configured MT5 fleet maintenance.

This check parses source without importing or starting the candidate worker. It
must run before an installer changes the service, its registry or release ACLs.
The ordinary release manifest proves byte integrity, not feature compatibility.
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path
from typing import Mapping, Sequence


MAINTENANCE_ENV = "TRADEJOURNAL_MT5_MAINTENANCE_ENABLED"
MAINTENANCE_FILES = (
    "mt5_maintenance.py",
    "mt5_maintenance_scheduler.py",
    "mt5_lifecycle.py",
    "mt5_recovery_window.py",
    "maintenance_recovery.py",
    "maintenance_ticket_recovery.py",
    "provisioning/mt5_instance_rotation.py",
    "provisioning/mt5_template.py",
    "provisioning/mt5_update_store.py",
    "provisioning/mt5_public_release.py",
)
MAINTENANCE_TYPES = {
    "mt5_maintenance.py": "Mt5MaintenanceCoordinator",
    "mt5_maintenance_scheduler.py": "Mt5MaintenanceScheduler",
    "mt5_lifecycle.py": "Mt5LifecycleCoordinator",
    "provisioning/mt5_instance_rotation.py": "Mt5InstanceRotator",
    "provisioning/mt5_template.py": "Mt5TemplateManager",
    "provisioning/mt5_update_store.py": "Mt5PendingUpdateStore",
    "provisioning/mt5_public_release.py": "Mt5PublicReleaseProbe",
    "maintenance_recovery.py": "MaintenanceRecovery",
    "maintenance_ticket_recovery.py": "TicketDeltaAdapter",
}

MAINTENANCE_HELPERS = ("Capture-Mt5TicketBaseline.py", "Refresh-Mt5HistoryChart.ps1")


class MaintenanceCapabilityError(RuntimeError):
    """Candidate cannot preserve configured maintenance; do not alter service."""


def _source(root: Path, relative: str) -> ast.Module:
    path = root / "windows_agent" / relative
    try:
        if path.is_symlink() or not path.is_file():
            raise MaintenanceCapabilityError("mt5_maintenance_module_missing")
        return ast.parse(path.read_text(encoding="utf-8"), filename=relative)
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise MaintenanceCapabilityError("mt5_maintenance_source_invalid") from exc


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise MaintenanceCapabilityError("mt5_maintenance_wiring_missing")


def _calls(node: ast.AST, name: str) -> bool:
    return any(
        isinstance(child, ast.Call)
        and (
            isinstance(child.func, ast.Name) and child.func.id == name
            or isinstance(child.func, ast.Attribute) and child.func.attr == name
        )
        for child in ast.walk(node)
    )


def verify_maintenance_capability(release_root: Path | str) -> None:
    """Require modules *and* their config/builder/run-loop integration."""
    root = Path(release_root).resolve()
    for relative in MAINTENANCE_FILES:
        tree = _source(root, relative)
        expected_type = MAINTENANCE_TYPES.get(relative)
        if expected_type and not any(
            isinstance(node, ast.ClassDef) and node.name == expected_type
            for node in tree.body
        ):
            raise MaintenanceCapabilityError("mt5_maintenance_implementation_missing")
    for name in MAINTENANCE_HELPERS:
        path = root / "scripts" / "windows" / name
        if path.is_symlink() or not path.is_file():
            raise MaintenanceCapabilityError("mt5_maintenance_helper_missing")
    daemon = _source(root, "agent_daemon.py")
    builder = _function(daemon, "build_runner")
    loop = _function(daemon, "run_forever")
    config = _source(root, "runtime_config.py")
    loader = _function(config, "load_runtime_config")
    required_calls = (
        "Mt5MaintenanceScheduler", "Mt5MaintenanceCoordinator", "Mt5PublicReleaseProbe", "MaintenanceRecovery",
    )
    wired = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "JobRunner"
        and any(
            keyword.arg == "scheduled_maintenance"
            and isinstance(keyword.value, ast.Name)
            for keyword in node.keywords
        )
        for node in ast.walk(builder)
    )
    reads_flag = any(
        isinstance(node, ast.Constant) and node.value == MAINTENANCE_ENV
        for node in ast.walk(loader)
    )
    if not (
        wired and reads_flag and _calls(loop, "run_if_due")
        and all(_calls(builder, name) for name in required_calls)
    ):
        raise MaintenanceCapabilityError("mt5_maintenance_wiring_missing")


def parse_service_environment(values: Sequence[str]) -> dict[str, str]:
    environment: dict[str, str] = {}
    for item in values:
        if not isinstance(item, str) or "=" not in item:
            continue
        name, value = item.split("=", 1)
        name = name.upper()
        if name.startswith("TRADEJOURNAL_MT5_MAINTENANCE_"):
            if name in environment:
                raise MaintenanceCapabilityError("mt5_maintenance_config_duplicate")
            environment[name] = value
    return environment


def assert_service_compatibility(
    release_root: Path | str,
    environment: Mapping[str, str],
) -> None:
    enabled = environment.get(MAINTENANCE_ENV, "").strip()
    if enabled not in ("", "0", "1"):
        raise MaintenanceCapabilityError("mt5_maintenance_config_invalid")
    if enabled == "1":
        verify_maintenance_capability(release_root)


def _windows_service_environment() -> dict[str, str]:
    import winreg

    key_path = r"SYSTEM\CurrentControlSet\Services\TradeJournalMT5Agent"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            try:
                values, value_type = winreg.QueryValueEx(key, "Environment")
            except FileNotFoundError:
                return {}
    except FileNotFoundError:
        return {}
    if value_type != winreg.REG_MULTI_SZ or not isinstance(values, list):
        raise MaintenanceCapabilityError("mt5_maintenance_config_invalid")
    return parse_service_environment(values)


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-root", required=True)
    parser.add_argument("--windows-service", action="store_true")
    parser.add_argument("--require-maintenance", action="store_true")
    args = parser.parse_args(arguments)
    try:
        environment = _windows_service_environment() if args.windows_service else {}
        if args.require_maintenance:
            environment[MAINTENANCE_ENV] = "1"
        assert_service_compatibility(args.release_root, environment)
    except MaintenanceCapabilityError as exc:
        print(json.dumps({"compatible": False, "error_code": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({"compatible": True, "maintenance_required": environment.get(MAINTENANCE_ENV) == "1"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
