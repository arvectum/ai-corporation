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


_ERROR_CATEGORIES = {
    "connection": "connection_error",
    "timeout": "timeout",
    "auth": "authentication_error",
    "pool": "pool_exhausted",
    "protocol": "protocol_error",
    "response": "protocol_error",
}


def redis_error_category(exc: Exception) -> str:
    class_name = type(exc).__name__.lower()
    for key, category in _ERROR_CATEGORIES.items():
        if key in class_name:
            return category
    return "unknown"
