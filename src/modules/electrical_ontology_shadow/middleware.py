from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from starlette.background import BackgroundTask
from starlette.middleware.base import BaseHTTPMiddleware

from src.modules.electrical_ontology_shadow.service import (
    run_shadow_for_saved_demo_run_safely,
)

_ANALYZE_PATH = re.compile(
    r"^/api/demo/tender-agent/runs/(?P<run_id>[A-Za-z0-9._:-]{1,120})/analyze$"
)


class ElectricalOntologyShadowMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        match = _ANALYZE_PATH.fullmatch(request.url.path)
        if (
            request.method.upper() != "POST"
            or match is None
            or response.status_code < 200
            or response.status_code >= 300
        ):
            return response

        run_id = match.group("run_id")
        existing_background = response.background

        async def run_existing_then_shadow() -> None:
            if existing_background is not None:
                await existing_background()
            await run_in_threadpool(run_shadow_for_saved_demo_run_safely, run_id)

        response.background = BackgroundTask(run_existing_then_shadow)
        return response


def install_electrical_ontology_shadow_middleware(app: FastAPI) -> None:
    app.add_middleware(ElectricalOntologyShadowMiddleware)
