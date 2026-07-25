"""Provider-neutral, evidence-grounded production LLM analysis contracts."""

from src.modules.production_llm_analysis.evidence import build_evidence_packet
from src.modules.production_llm_analysis.openai_compatible import (
    OpenAICompatibleProductionLLMProvider,
    OpenAICompatibleTransportConfig,
)
from src.modules.production_llm_analysis.service import run_production_llm_analysis

__all__ = [
    "OpenAICompatibleProductionLLMProvider",
    "OpenAICompatibleTransportConfig",
    "build_evidence_packet",
    "run_production_llm_analysis",
]
