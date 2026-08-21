#!/usr/bin/env python3
"""One-call synthetic probe for Gemma 4 reasoning/content separation.

This diagnostic is customer-data-free and database-free. It reuses the approved
batch-shaped synthetic request, keeps thinking disabled, verifies the production
``reasoning_format=auto`` response-separation boundary, and emits sanitized
structural diagnostics. Raw prompts, model output, reasoning text and credentials
are never printed or persisted by this script.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from scripts.r10_1.probe_llama_batch_shape import (
    _provider_boundary,
    _shape_request,
    _validate_response,
)
from src.modules.production_llm_analysis.batching import tokenizer_from_environment
from src.modules.production_llm_analysis.controlled_evidence import (
    load_approved_provider_policy,
)
from src.modules.production_llm_analysis.evidence import canonical_json_bytes, canonical_sha256
from src.modules.production_llm_analysis.llama_reasoning_control import (
    install_llama_non_reasoning_mode,
)
from src.modules.production_llm_analysis.llama_schema_constraint import (
    install_llama_schema_constraint,
)
from src.modules.production_llm_analysis.openai_compatible import (
    OpenAICompatibleProductionLLMProvider,
    OpenAICompatibleTransportConfig,
)
from src.shared.llm.transport import HTTPRequest, UrllibHTTPClient


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--approved-policy", required=True, type=Path)
    parser.add_argument("--expected-head", required=True)
    return parser.parse_args()


def _git_preflight(expected_head: str) -> None:
    root = Path(__file__).resolve().parents[2]
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    status = subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=root, text=True
    ).strip()
    if head != expected_head:
        raise RuntimeError("probe_repository_head_mismatch")
    if status:
        raise RuntimeError("probe_repository_worktree_not_clean")


def _build_auto_reasoning_body(provider, request) -> dict[str, Any]:
    body = provider._build_request_body(request)
    response_format = body.get("response_format")
    if not isinstance(response_format, dict) or response_format.get("type") != "json_object":
        raise RuntimeError("probe_response_format_invalid")
    schema = response_format.get("schema")
    if not isinstance(schema, dict):
        raise RuntimeError("probe_schema_missing")
    try:
        task = json.loads(body["messages"][1]["content"])
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        raise RuntimeError("probe_task_contract_invalid") from None
    if canonical_sha256(task.get("output_contract")) != canonical_sha256(schema):
        raise RuntimeError("probe_schema_contract_mismatch")
    if body.get("chat_template_kwargs", {}).get("enable_thinking") is not False:
        raise RuntimeError("probe_thinking_not_disabled")
    if body.get("reasoning_effort") != "none":
        raise RuntimeError("probe_reasoning_effort_not_none")
    if body.get("reasoning_format") != "auto":
        raise RuntimeError("probe_reasoning_format_not_auto")
    if body.get("max_tokens") != 4096:
        raise RuntimeError("probe_max_tokens_mismatch")
    return body


def _inspect_envelope(body: bytes) -> dict[str, Any]:
    result = {
        "envelope_valid_json": False,
        "message_content_string": False,
        "message_content_valid_json": False,
        "reasoning_content_present": False,
        "reasoning_content_bytes": 0,
        "claims_object_valid": False,
    }
    try:
        envelope = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return result
    if not isinstance(envelope, dict):
        return result
    result["envelope_valid_json"] = True
    try:
        message = envelope["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return result
    if not isinstance(message, dict):
        return result
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str):
        result["reasoning_content_present"] = True
        result["reasoning_content_bytes"] = len(reasoning.encode("utf-8"))
    content = message.get("content")
    if not isinstance(content, str):
        return result
    result["message_content_string"] = True
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return result
    result["message_content_valid_json"] = True
    result["claims_object_valid"] = (
        isinstance(parsed, dict)
        and set(parsed) == {"claims"}
        and isinstance(parsed.get("claims"), list)
    )
    return result


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True))


def main() -> int:
    args = _arguments()
    provider_call_count = 0
    try:
        _git_preflight(args.expected_head)
        policy = load_approved_provider_policy(args.approved_policy)
        base_url, api_key = _provider_boundary(policy)
        install_llama_schema_constraint()
        install_llama_non_reasoning_mode()
        tokenizer = tokenizer_from_environment()
        provider = OpenAICompatibleProductionLLMProvider(
            OpenAICompatibleTransportConfig(base_url=base_url, api_key=api_key),
            clock=lambda: 0.0,
        )
        request, _measurement, _fragment_count, _batch_policy = _shape_request(
            policy, provider, tokenizer
        )
        request_body = _build_auto_reasoning_body(provider, request)
        http_request = HTTPRequest(
            url=provider._config.endpoint_url,
            body=canonical_json_bytes(request_body),
            headers=provider._headers(),
            timeout_ms=request.budget_policy.limits.timeout_ms,
        )
        provider_call_count = 1
        response = UrllibHTTPClient().send(http_request)
        structural = _inspect_envelope(response.body)

        provider_contract_valid = False
        failure_code = ""
        try:
            parsed = provider._parse_success_response(
                response=response,
                request=request,
                attempt_latencies_ms=[0],
                retry_count=0,
                analysis_started=0.0,
            )
            _validate_response(request, parsed)
            provider_contract_valid = True
        except Exception as exc:  # sanitized below; raw response is never emitted.
            candidate = str(exc).strip().lower()
            failure_code = (
                candidate
                if candidate and candidate.replace("_", "").replace(":", "").isalnum() and len(candidate) <= 120
                else "probe_provider_contract_invalid"
            )

        payload = {
            "status": "pass" if provider_contract_valid else "fail",
            "provider_call_count": provider_call_count,
            "retry_count": 0,
            "enable_thinking": False,
            "reasoning_effort": "none",
            "reasoning_format": "auto",
            "http_status": response.status_code,
            **structural,
            "provider_contract_valid": provider_contract_valid,
            "failure_code": failure_code,
        }
        _print(payload)
        return 0 if provider_contract_valid else 2
    except Exception as exc:
        code = str(exc).strip().lower()
        if not code or not code.replace("_", "").isalnum() or len(code) > 120:
            code = "probe_launcher_error"
        _print(
            {
                "status": "launcher_error",
                "provider_call_count": provider_call_count,
                "retry_count": 0,
                "failure_code": code,
            }
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
