"""Modelli serializzabili del contratto job e dello stato delle istanze."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict, Optional


class Action(str, Enum):
    PROVISION = "provision"
    START = "start"
    STOP = "stop"
    RESTART = "restart"
    STATUS = "status"
    DEPROVISION = "deprovision"


class InstanceStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    ACTIVE = "active"
    STOPPED = "stopped"
    ERROR = "error"
    DELETING = "deleting"
    DELETED = "deleted"


@dataclass(frozen=True)
class ProvisioningJob:
    version: int
    job_id: str
    action: Action
    connection_id: str
    created_at: str
    account_number: Optional[str] = None
    server: Optional[str] = None
    tradejournal_api_url: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["action"] = self.action.value
        return data


@dataclass(frozen=True)
class InstanceState:
    connection_id: str
    project_name: str
    status: InstanceStatus
    updated_at: str
    account_number: Optional[str] = None
    server: Optional[str] = None
    tradejournal_api_url: Optional[str] = None
    last_job_id: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "InstanceState":
        return cls(
            connection_id=str(data["connection_id"]),
            project_name=str(data["project_name"]),
            status=InstanceStatus(str(data["status"])),
            updated_at=str(data["updated_at"]),
            account_number=data.get("account_number"),
            server=data.get("server"),
            tradejournal_api_url=data.get("tradejournal_api_url"),
            last_job_id=data.get("last_job_id"),
            error=data.get("error"),
        )
