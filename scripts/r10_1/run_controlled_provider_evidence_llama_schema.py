#!/usr/bin/env python3
"""Run the controlled Gate 5 flow with llama.cpp server-owned grounding."""

from __future__ import annotations

from scripts.r10_1.run_controlled_provider_evidence import main
from src.modules.production_llm_analysis.llama_finalization_diagnostics import (
    install_llama_finalization_diagnostics,
)
from src.modules.production_llm_analysis.llama_manifest_profile import (
    install_llama_manifest_profile,
)
from src.modules.production_llm_analysis.llama_reasoning_control import (
    install_llama_non_reasoning_mode,
)
from src.modules.production_llm_analysis.llama_schema_constraint import (
    install_llama_schema_constraint,
)


if __name__ == "__main__":
    install_llama_schema_constraint()
    install_llama_non_reasoning_mode()
    install_llama_manifest_profile()
    install_llama_finalization_diagnostics()
    raise SystemExit(main())
