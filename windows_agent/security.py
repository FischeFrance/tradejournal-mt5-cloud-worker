from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any
from uuid import UUID

SECRET_MARKERS = ("password", "token", "secret", "authorization")


def canonical_uuid(value: str) -> str:
    parsed = UUID(str(value))
    if str(parsed) != str(value) or parsed.variant != UUID(str(value)).variant:
        raise ValueError("connection_id must be a canonical UUID")
    return str(parsed)


def safe_child(root: Path, connection_id: str) -> Path:
    child = (Path(root).resolve() / canonical_uuid(connection_id)).resolve()
    if child.parent != Path(root).resolve():
        raise ValueError("unsafe instance path")
    return child


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (
                "<redacted>"
                if any(m in k.lower() for m in SECRET_MARKERS)
                else redact(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class RedactionFilter(logging.Filter):
    _bearer = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._bearer.sub("Bearer <redacted>", str(record.msg))
        if record.args:
            record.args = (
                tuple(redact(v) for v in record.args)
                if isinstance(record.args, tuple)
                else redact(record.args)
            )
        return True
