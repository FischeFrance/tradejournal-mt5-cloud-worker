"""Nomi Docker e filesystem derivati soltanto da UUID gia' validati."""

from __future__ import annotations

from uuid import UUID

from .validation import validate_uuid


def connection_slug(connection_id: str) -> str:
    canonical = validate_uuid(connection_id, "connection_id")
    return UUID(canonical).hex


def project_name(connection_id: str) -> str:
    return f"tjmt5-{connection_slug(connection_id)}"


def runtime_container_name(connection_id: str) -> str:
    return f"{project_name(connection_id)}-runtime"


def worker_container_name(connection_id: str) -> str:
    return f"{project_name(connection_id)}-worker"


def network_name(connection_id: str) -> str:
    return f"{project_name(connection_id)}-network"
