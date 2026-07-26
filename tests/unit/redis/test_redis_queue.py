from datetime import datetime, timezone
from uuid import uuid4
import pytest
from src.shared.redis.queue import QueueEnvelope


class TestQueueEnvelopeValidation:
    def test_valid_envelope(self):
        envelope = QueueEnvelope(
            queue_name="analysis",
            message_id=uuid4().hex,
            job_type="analyze",
            tenant="tenant_a",
            customer_id="cust_1",
            project_id="proj_1",
            procurement_case_id="case_1",
            run_id="run_1",
            payload={"key": "value"},
            enqueued_at=datetime.now(timezone.utc).isoformat(),
            deduplication_key=uuid4().hex,
        )
        envelope.validate()

    def test_missing_queue_name_raises(self):
        envelope = QueueEnvelope(message_id="m1", job_type="j", tenant="t", customer_id="c")
        with pytest.raises(ValueError, match="queue_name"):
            envelope.validate()

    def test_missing_message_id_raises(self):
        envelope = QueueEnvelope(queue_name="q", job_type="j", tenant="t", customer_id="c")
        with pytest.raises(ValueError, match="message_id"):
            envelope.validate()

    def test_missing_job_type_raises(self):
        envelope = QueueEnvelope(queue_name="q", message_id="m1", tenant="t", customer_id="c")
        with pytest.raises(ValueError, match="job_type"):
            envelope.validate()

    def test_missing_tenant_raises(self):
        envelope = QueueEnvelope(queue_name="q", message_id="m1", job_type="j", customer_id="c")
        with pytest.raises(ValueError, match="tenant"):
            envelope.validate()

    def test_missing_customer_raises(self):
        envelope = QueueEnvelope(queue_name="q", message_id="m1", job_type="j", tenant="t")
        with pytest.raises(ValueError, match="customer_id"):
            envelope.validate()

    def test_payload_size_limit(self):
        envelope = QueueEnvelope(
            queue_name="q", message_id="m1", job_type="j", tenant="t", customer_id="c",
            payload={"data": "x" * 70000},
        )
        with pytest.raises(ValueError, match="payload exceeds"):
            envelope.validate()

    def test_versioned_schema(self):
        envelope = QueueEnvelope(
            queue_name="q", message_id="m1", job_type="j", tenant="t", customer_id="c"
        )
        assert envelope.version == 1

    def test_tenant_dimensions(self):
        envelope = QueueEnvelope(
            queue_name="q", message_id="m1", job_type="j", tenant="t1",
            customer_id="c1", project_id="p1", procurement_case_id="case1", run_id="r1",
        )
        assert envelope.tenant == "t1"
        assert envelope.customer_id == "c1"
        assert envelope.project_id == "p1"

    def test_retry_metadata(self):
        envelope = QueueEnvelope(
            queue_name="q", message_id="m1", job_type="j", tenant="t", customer_id="c",
            attempt=1, max_attempts=3, visibility_timeout_seconds=300,
        )
        assert envelope.attempt == 1
        assert envelope.max_attempts == 3
        assert envelope.visibility_timeout_seconds == 300

    def test_to_dict(self):
        envelope = QueueEnvelope(
            queue_name="q", message_id="m1", job_type="j", tenant="t", customer_id="c",
        )
        d = envelope.to_dict()
        assert d["queue_name"] == "q"
        assert d["version"] == 1
        assert d["job_type"] == "j"
        assert "enqueued_at" in d
