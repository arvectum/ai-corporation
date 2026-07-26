from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class QueueEnvelope:
    version: int = 1
    queue_name: str = ""
    message_id: str = ""
    job_type: str = ""
    deduplication_key: str = ""
    tenant: str = ""
    customer_id: str = ""
    project_id: str = ""
    procurement_case_id: str = ""
    run_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    enqueued_at: str = ""
    attempt: int = 1
    max_attempts: int = 3
    visibility_timeout_seconds: int = 300
    correlation_id: str = ""

    MAX_PAYLOAD_BYTES = 65536

    def validate(self) -> None:
        if not self.queue_name:
            raise ValueError("queue_name is required")
        if not self.message_id:
            raise ValueError("message_id is required")
        if not self.job_type:
            raise ValueError("job_type is required")
        if not self.tenant:
            raise ValueError("tenant is required")
        if not self.customer_id:
            raise ValueError("customer_id is required")
        raw = self._raw_payload_bytes()
        if len(raw) > self.MAX_PAYLOAD_BYTES:
            raise ValueError(f"payload exceeds {self.MAX_PAYLOAD_BYTES} bytes ({len(raw)})")

    def _raw_payload_bytes(self) -> bytes:
        import json
        return json.dumps(self.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "queue_name": self.queue_name,
            "message_id": self.message_id,
            "job_type": self.job_type,
            "deduplication_key": self.deduplication_key,
            "tenant": self.tenant,
            "customer_id": self.customer_id,
            "project_id": self.project_id,
            "procurement_case_id": self.procurement_case_id,
            "run_id": self.run_id,
            "payload": self.payload,
            "enqueued_at": self.enqueued_at or datetime.now(timezone.utc).isoformat(),
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "visibility_timeout_seconds": self.visibility_timeout_seconds,
            "correlation_id": self.correlation_id,
        }
