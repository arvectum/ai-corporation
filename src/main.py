from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI

from src.modules.electrical_ontology_shadow.middleware import (
    install_electrical_ontology_shadow_middleware,
)
from src.shared.api.errors import register_exception_handlers
from src.shared.api.middleware import install_runtime_middlewares
from src.shared.api.router_registry import install_application_routers
from src.shared.api.site_mount import install_optional_site_mount
from src.shared.config.settings import get_settings
from src.shared.redis.client import close_client, health_snapshot
from src.shared.storage.capacity import storage_metrics_dict

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    close_client()


app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)
install_runtime_middlewares(app, settings)
install_electrical_ontology_shadow_middleware(app)
register_exception_handlers(app)
install_application_routers(app)


@app.get("/health")
def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def readiness() -> dict[str, object]:
    data_dir = Path(settings.arvectum_data_dir)
    writable = data_dir.exists() and data_dir.is_dir()
    storage_metrics = storage_metrics_dict()
    redis_health = health_snapshot()
    redis_ready = redis_health.get("status") == "healthy"
    redis_required = settings.arvectum_redis_enabled
    customer_pilot_ready = not redis_required or redis_ready
    overall_status = "ok"
    if (
        not writable
        or not storage_metrics.get("ingestion_allowed", False)
        or (redis_required and not redis_ready)
    ):
        overall_status = "degraded"
    return {
        "status": overall_status,
        "data_writable": writable,
        "storage": storage_metrics,
        "redis": {
            "enabled": redis_health["enabled"],
            "status": redis_health["status"],
            "latency_ms": redis_health["latency_ms"],
            "error_category": redis_health["error_category"],
        },
        "feature_readiness": {
            "customer_pilot_run_start": "ready" if customer_pilot_ready else "blocked",
        },
        "timestamp": datetime.now(UTC).isoformat(),
    }


install_optional_site_mount(app, settings)
