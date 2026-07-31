# Host-safe Redis runtime

This profile exposes the canonical Redis service to a backend running directly
on macOS. It is a Compose overlay and must always be combined with
`docker-compose.redis.yml`.

## Safety contract

- Docker context defaults to `colima` and can be overridden with
  `ARVECTUM_DOCKER_CONTEXT`.
- Redis binds only to `127.0.0.1`.
- The default host port is `16380`; override it with
  `ARVECTUM_REDIS_HOST_PORT` in the local ignored `.env.local` file.
- `ARVECTUM_REDIS_PASSWORD` is mandatory.
- The canonical persistent volume from `docker-compose.redis.yml` is reused.
- The operator targets never remove the volume.

Real secrets and local paths belong only in `.env.local`, which must remain
untracked and mode `0600`.

## Validate without starting Redis

```bash
make redis-host-config
```

## Start and verify

```bash
make redis-host-start
make redis-host-ping
```

Configure the host backend to use loopback port `16380` (or the explicitly
configured `ARVECTUM_REDIS_HOST_PORT`) and the same local Redis password. Do not
commit the resulting URL or credentials.

## Reversible stop

```bash
make redis-host-stop
```

This stops the Redis container but preserves its network metadata and
persistent volume. Do not use `docker compose down -v` for the canonical local
runtime.
