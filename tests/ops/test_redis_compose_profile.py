from pathlib import Path


PROFILE = Path("docker-compose.redis-host.yml")


def test_host_redis_profile_is_loopback_only_and_persistent():
    text = PROFILE.read_text(encoding="utf-8")

    assert '"127.0.0.1:16380:6379"' in text
    assert "0.0.0.0" not in text
    assert "ARVECTUM_REDIS_PASSWORD" in text
    assert "arvectum_redis_data:/data" in text
    assert "restart: unless-stopped" in text
    assert "name: arvectum-redis" in text


def test_host_profile_does_not_use_test_port_or_test_container():
    text = PROFILE.read_text(encoding="utf-8")

    assert "16379" not in text
    assert "arvectum-redis-test" not in text
