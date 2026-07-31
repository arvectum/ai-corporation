import re
from pathlib import Path

MAKEFILE = Path("Makefile")


def test_redis_integration_target_does_not_print_secret_bearing_values():
    text = MAKEFILE.read_text(encoding="utf-8")
    echo_segments = re.findall(r"echo[^;\\n]*", text)

    assert not any("AI_CORP_REDIS_URL" in segment for segment in echo_segments)
    assert not any("ARVECTUM_REDIS_URL" in segment for segment in echo_segments)
    assert not any("ARVECTUM_REDIS_PASSWORD" in segment and "required" not in segment for segment in echo_segments)
    assert "redis_test_url_configured=yes" in text
    assert "redis_test_namespace_configured=yes" in text


def test_host_runtime_targets_remain_available():
    text = MAKEFILE.read_text(encoding="utf-8")
    for target in ("redis-host-config:", "redis-host-start:", "redis-host-ping:", "redis-host-stop:"):
        assert target in text
