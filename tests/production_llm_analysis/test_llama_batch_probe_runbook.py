from pathlib import Path


def test_batch_probe_runbook_uses_one_call_non_reasoning_contract():
    text = Path("docs/runbooks/r10-1-llama-batch-probe.md").read_text(
        encoding="utf-8"
    )

    assert "probe_llama_batch_shape" in text
    assert "one provider call only" in text
    assert "zero automatic retries" in text
    assert "reasoning_enabled=false" in text
    assert "Do not run the controlled customer runner" in text
