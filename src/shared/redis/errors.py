from src.shared.errors import AppError


class RedisDisabledError(AppError):
    status_code = 503
    code = "redis_disabled"


class RedisUnavailableError(AppError):
    status_code = 503
    code = "redis_unavailable"


class RedisAlreadyLockedError(AppError):
    status_code = 409
    code = "redis_already_locked"


class RedisLockTimeoutError(AppError):
    status_code = 503
    code = "redis_lock_timeout"


class RedisLockNotOwnedError(AppError):
    status_code = 409
    code = "redis_lock_not_owned"
