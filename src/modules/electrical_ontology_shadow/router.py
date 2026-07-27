from fastapi import APIRouter, HTTPException

from src.modules.electrical_ontology_shadow.service import (
    get_shadow_summary_for_saved_demo_run,
)

router = APIRouter(tags=["electrical-ontology-shadow"])


@router.get("/api/demo/tender-agent/runs/{run_id}/shadow/electrical-ontology")
def get_electrical_ontology_shadow_summary(run_id: str) -> dict[str, object]:
    try:
        return get_shadow_summary_for_saved_demo_run(run_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Shadow audit summary is not available") from exc
