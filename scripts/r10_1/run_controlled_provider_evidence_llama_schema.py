#!/usr/bin/env python3
"""Run the controlled Gate 5 flow with llama.cpp schema-constrained JSON."""

from __future__ import annotations

from scripts.r10_1.run_controlled_provider_evidence import main
from src.modules.production_llm_analysis.llama_schema_constraint import (
    install_llama_schema_constraint,
)


if __name__ == "__main__":
    install_llama_schema_constraint()
    raise SystemExit(main())
