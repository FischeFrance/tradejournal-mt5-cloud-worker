"""Serialize maintenance with the existing per-account ingestion lock."""
from .event_supervisor import connection_sync_lock


class Mt5LifecycleCoordinator:
    connection = staticmethod(connection_sync_lock)
