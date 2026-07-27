from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.modules.electrical_ontology_shadow import middleware as shadow_middleware
from src.modules.electrical_ontology_shadow.middleware import (
    install_electrical_ontology_shadow_middleware,
)


def test_shadow_middleware_preserves_primary_response(monkeypatch) -> None:
    calls: list[str] = []

    def fake_shadow(run_id: str) -> dict[str, object]:
        calls.append(run_id)
        return {"status": "BLOCKED", "production_effect": False}

    monkeypatch.setattr(
        shadow_middleware,
        "run_shadow_for_saved_demo_run_safely",
        fake_shadow,
    )
    app = FastAPI()
    install_electrical_ontology_shadow_middleware(app)

    @app.post("/api/demo/tender-agent/runs/{run_id}/analyze")
    def analyze(run_id: str) -> dict[str, object]:
        return {
            "run_id": run_id,
            "status": "completed",
            "recommendation": "needs_review",
        }

    response = TestClient(app).post(
        "/api/demo/tender-agent/runs/toa-run-1/analyze"
    )
    assert response.status_code == 200
    assert response.json() == {
        "run_id": "toa-run-1",
        "status": "completed",
        "recommendation": "needs_review",
    }
    assert calls == ["toa-run-1"]


def test_shadow_middleware_does_not_run_after_primary_failure(monkeypatch) -> None:
    calls: list[str] = []

    def fake_shadow(run_id: str) -> dict[str, object]:
        calls.append(run_id)
        return {"status": "BLOCKED"}

    monkeypatch.setattr(
        shadow_middleware,
        "run_shadow_for_saved_demo_run_safely",
        fake_shadow,
    )
    app = FastAPI()
    install_electrical_ontology_shadow_middleware(app)

    @app.post("/api/demo/tender-agent/runs/{run_id}/analyze")
    def analyze(run_id: str) -> dict[str, object]:
        raise HTTPException(status_code=409, detail="documents required")

    response = TestClient(app).post(
        "/api/demo/tender-agent/runs/toa-run-2/analyze"
    )
    assert response.status_code == 409
    assert calls == []


def test_shadow_middleware_ignores_other_routes(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(
        shadow_middleware,
        "run_shadow_for_saved_demo_run_safely",
        lambda run_id: calls.append(run_id),
    )
    app = FastAPI()
    install_electrical_ontology_shadow_middleware(app)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert calls == []
