from pathlib import Path


BASE_PROFILE = Path("docker-compose.redis.yml")
HOST_OVERLAY = Path("docker-compose.redis-host.yml")
MAKEFILE = Path("Makefile")


def test_host_redis_overlay_is_loopback_only() -> None:
    text = HOST_OVERLAY.read_text(encoding="utf-8")

    assert '"127.0.0.1:${ARVECTUM_REDIS_HOST_PORT:-16380}:6379"' in text
    assert "0.0.0.0" not in text
    assert "16379" not in text
    assert "arvectum-redis-test" not in text


def test_host_overlay_reuses_canonical_redis_definition() -> None:
    overlay = HOST_OVERLAY.read_text(encoding="utf-8")
    base = BASE_PROFILE.read_text(encoding="utf-8")

    for duplicated_key in (
        "image:",
        "container_name:",
        "command:",
        "environment:",
        "healthcheck:",
        "restart:",
        "networks:",
        "volumes:",
    ):
        assert duplicated_key not in overlay

    assert "name: arvectum-redis" in base
    assert "ARVECTUM_REDIS_PASSWORD" in base
    assert "arvectum_redis_data:/data" in base
    assert "restart: unless-stopped" in base


def test_makefile_exposes_safe_host_runtime_commands() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")

    assert "redis-host-config:" in text
    assert "redis-host-start:" in text
    assert "redis-host-ping:" in text
    assert "redis-host-stop:" in text
    assert "docker-compose.redis.yml -f docker-compose.redis-host.yml" in text
    assert 'ARVECTUM_DOCKER_CONTEXT:-colima' in text
    assert ". ./.env.local" in text
    assert "redis-host-clean" not in text
