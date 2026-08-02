#!/usr/bin/env python3
"""Repository-owned one-shot ARV-001 complete-corpus acceptance runner."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from scripts.arv001.application_workflow import (
    create_application_data,
    database_preflight,
    post_persistence_preflight,
    provider_preflight,
    static_contract_preflight,
)
from scripts.arv001.complete_corpus_contract import (
    DEFAULT_CORPUS_SHA256,
    DEFAULT_CUSTOMER_NAME,
    DEFAULT_POLICY_SHA256,
    DEFAULT_PROJECT_NAME,
    DEFAULT_REGISTRY_NUMBER,
    AcceptanceBlocked,
    artifact_shape as _artifact_shape,
    corpus_hash as _corpus_hash,
    load_candidate,
    prepare_documents as _prepare_documents,
    read_json as _read_json,
    sha256_file as _sha256_file,
    validate_customer_report as _validate_customer_report,
    validate_document_set,
    write_json as _write_json,
)

_static_contract_preflight = static_contract_preflight
_SAFE_CODE = re.compile(r"^[a-z0-9_.:-]{1,180}$")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the complete ARV-001 corpus through the accepted local contour."
    )
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--intake-root", type=Path)
    parser.add_argument("--database-path", type=Path, required=True)
    parser.add_argument("--initialize-database", action="store_true")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--approved-policy", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--registry-number", default=DEFAULT_REGISTRY_NUMBER)
    parser.add_argument("--expected-corpus-sha", default=DEFAULT_CORPUS_SHA256)
    parser.add_argument("--expected-policy-sha", default=DEFAULT_POLICY_SHA256)
    parser.add_argument("--customer-name", default=DEFAULT_CUSTOMER_NAME)
    parser.add_argument("--project-name", default=DEFAULT_PROJECT_NAME)
    parser.add_argument("--execute-provider", action="store_true")
    return parser.parse_args()


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _git_preflight(repo_root: Path, expected_head: str) -> dict[str, Any]:
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, text=True
    ).strip()
    status = subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=repo_root, text=True
    ).strip()
    if head != expected_head:
        raise AcceptanceBlocked("repository_head_mismatch")
    if status:
        raise AcceptanceBlocked("repository_worktree_not_clean")
    return {"head_sha": head, "worktree_clean": True}


def _outside_repository(repo_root: Path, path: Path, code: str) -> None:
    if path == repo_root or repo_root in path.parents:
        raise AcceptanceBlocked(code)


def _configure(args: argparse.Namespace, repo_root: Path) -> dict[str, Path]:
    """Validate paths and set process environment without writing anything."""

    candidate_root = args.candidate_root.expanduser().resolve()
    intake_root = (args.intake_root or candidate_root).expanduser().resolve()
    database_path = args.database_path.expanduser().resolve()
    data_dir = args.data_dir.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    policy_path = args.approved_policy.expanduser().resolve()
    if not candidate_root.is_dir() or not intake_root.is_dir():
        raise AcceptanceBlocked("candidate_or_intake_root_missing")
    if output_root.exists():
        raise AcceptanceBlocked("output_root_already_exists")
    if not policy_path.is_file() or policy_path.is_symlink():
        raise AcceptanceBlocked("approved_policy_missing")
    if database_path.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
        raise AcceptanceBlocked("local_sqlite_path_invalid")
    if database_path.name == "ai_corporation.db" or database_path.is_symlink():
        raise AcceptanceBlocked("canonical_or_symlink_database_forbidden")
    for path, code in (
        (database_path, "database_path_inside_repository"),
        (data_dir, "data_dir_inside_repository"),
        (output_root, "output_root_inside_repository"),
    ):
        _outside_repository(repo_root, path, code)
    if args.initialize_database:
        if database_path.exists():
            raise AcceptanceBlocked("new_local_test_database_already_exists")
    elif not database_path.is_file():
        raise AcceptanceBlocked("migrated_local_test_database_missing")
    os.environ["AI_CORP_DATABASE_URL"] = f"sqlite:///{database_path.as_posix()}"
    os.environ["AI_CORP_ARVECTUM_DATA_DIR"] = str(data_dir)
    os.environ["AI_CORP_REDIS_ENABLED"] = "false"
    os.environ["AI_CORP_LLM_MAX_RETRIES"] = "0"
    return {
        "candidate_root": candidate_root,
        "intake_root": intake_root,
        "database_path": database_path,
        "data_dir": data_dir,
        "output_root": output_root,
        "policy_path": policy_path,
    }


def _initialize_local_runtime(
    paths: dict[str, Path], repo_root: Path, *, initialize_database: bool
) -> None:
    """Create only the isolated schema after repository preflight succeeds."""

    paths["data_dir"].mkdir(parents=True, exist_ok=False)
    if not initialize_database:
        return
    paths["database_path"].parent.mkdir(parents=True, exist_ok=True)
    migration = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=repo_root,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )
    if migration.returncode != 0 or not paths["database_path"].is_file():
        raise AcceptanceBlocked("local_test_database_migration_failed")


def _safe_failure(stderr: str) -> str:
    value = (
        stderr.strip().splitlines()[-1].strip().lower()
        if stderr.strip()
        else "controlled_provider_evidence_failed"
    )
    return value if _SAFE_CODE.fullmatch(value) else "controlled_provider_evidence_failed"


def _run_controlled_once(
    repo_root: Path,
    run_id: str,
    registry_number: str,
    policy_path: Path,
    output_root: Path,
) -> dict[str, Any]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.r10_1.run_controlled_provider_evidence",
            "--run-id",
            run_id,
            "--expected-registry-number",
            registry_number,
            "--approved-policy",
            str(policy_path),
            "--output-root",
            str(output_root),
        ],
        cwd=repo_root,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AcceptanceBlocked(
            "controlled_invocation_failed:" + _safe_failure(result.stderr)
        )
    try:
        response = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise AcceptanceBlocked("controlled_runner_success_output_invalid") from exc
    if response.get("status") != "controlled_evidence_complete":
        raise AcceptanceBlocked("controlled_runner_status_invalid")
    return {
        "controlled_invocation_count": 1,
        "runner_return_code": 0,
        "provider": response.get("provider"),
        "model": response.get("model"),
        "manifest_hash": response.get("manifest_hash"),
        "stdout_recorded": False,
        "stderr_recorded": False,
    }


def _controlled_manifest_metrics(manifest: dict[str, Any]) -> dict[str, Any]:
    executions = (
        manifest.get("executions")
        if isinstance(manifest.get("executions"), list)
        else []
    )
    if (
        manifest.get("repeat_count") != 2
        or manifest.get("repeat_identity_verified") is not True
        or len(executions) != 2
    ):
        raise AcceptanceBlocked("controlled_repeat_identity_not_verified")
    summaries: list[dict[str, Any]] = []
    for item in executions:
        if not isinstance(item, dict):
            raise AcceptanceBlocked("controlled_execution_summary_invalid")
        accepted = item.get("accepted_claims")
        rejected = item.get("rejected_claims")
        if not isinstance(accepted, list) or not isinstance(rejected, list):
            raise AcceptanceBlocked("controlled_claim_summary_invalid")
        unsupported = sum(
            1
            for claim in accepted
            if not isinstance(claim, dict)
            or claim.get("support_status") != "supported"
        )
        batch_count = int(item.get("batch_count") or 0)
        provider_calls = int(item.get("provider_call_count") or 0)
        if (
            item.get("status") != "success"
            or item.get("canonical_input_eligible") is not True
            or int(item.get("retry_count") or 0) != 0
            or int(item.get("rejected_claim_count") or 0) != 0
            or rejected
            or unsupported
            or not accepted
            or batch_count <= 0
            or provider_calls != batch_count
            or item.get("raw_response_stored") is not False
        ):
            raise AcceptanceBlocked("controlled_execution_contract_failed")
        summaries.append(
            {
                "accepted_claim_count": len(accepted),
                "rejected_claim_count": 0,
                "unsupported_claim_count": 0,
                "batch_count": batch_count,
                "provider_call_count": provider_calls,
                "retry_count": 0,
                "raw_response_stored": False,
            }
        )
    if summaries[0] != summaries[1]:
        raise AcceptanceBlocked("controlled_execution_metrics_mismatch")
    safety = manifest.get("safety")
    if not isinstance(safety, dict) or any(
        safety.get(key) is not False
        for key in (
            "credential_value_recorded",
            "raw_tender_text_recorded",
            "raw_provider_body_recorded",
            "raw_response_stored",
            "evidence_quotes_recorded",
            "local_paths_recorded",
        )
    ):
        raise AcceptanceBlocked("controlled_manifest_safety_failed")
    return {
        "execution_count": 2,
        "repeat_identity_verified": True,
        "executions": summaries,
        "batch_count_per_execution": summaries[0]["batch_count"],
        "accepted_claim_count_per_execution": summaries[0][
            "accepted_claim_count"
        ],
        "rejected_claim_count_per_execution": 0,
        "unsupported_claim_count_per_execution": 0,
        "retry_count_per_execution": 0,
        "raw_provider_response_stored": False,
        "provider_reasoning_stored": False,
    }


def _finalize(
    stage: Path,
    controlled_root: Path,
    final_root: Path,
    application: dict[str, Any],
    preflight: dict[str, Any],
    invocation: dict[str, Any],
    registry_number: str,
) -> dict[str, Any]:
    sources = {
        "controlled-run-manifest.json": controlled_root
        / "controlled-evidence.manifest.json",
        "canonical-output.json": controlled_root
        / "execution-1"
        / "canonical_report.json",
        "customer-report.html": controlled_root / "execution-1" / "report.html",
        "upload-ready-report.html": controlled_root
        / "execution-1"
        / "report.html",
    }
    if any(not path.is_file() for path in sources.values()):
        raise AcceptanceBlocked("controlled_output_missing")
    manifest = _read_json(sources["controlled-run-manifest.json"])
    if not isinstance(manifest, dict):
        raise AcceptanceBlocked("controlled_manifest_invalid")
    metrics = _controlled_manifest_metrics(manifest)
    content_check = _validate_customer_report(
        sources["customer-report.html"].read_text(encoding="utf-8"),
        registry_number,
    )
    for name, source in sources.items():
        shutil.copyfile(source, stage / name)
    _write_json(stage / "application-data-summary.json", application)
    _write_json(
        stage / "document-registry.json",
        {
            "physical_document_count": application["document_count"],
            "extracted_document_count": application["extracted_document_count"],
            "chunk_count": application["chunk_count"],
            "corpus_sha256": application["corpus_sha256"],
        },
    )
    _write_json(
        stage / "source-graph-summary.json",
        {
            "source_graph_hash": application["source_graph_hash"],
            "chunk_count": application["chunk_count"],
            "verified": True,
        },
    )
    _write_json(
        stage / "immutable-snapshot-summary.json",
        {
            "snapshot_verified": application["snapshot_verified"],
            "snapshot_report_bytes": application["snapshot_report_bytes"],
            "run_status": application["run_status"],
            "case_status": application["case_status"],
        },
    )
    _write_json(stage / "post-persistence-preflight.json", preflight)
    _write_json(stage / "controlled-invocation-summary.json", invocation)
    _write_json(stage / "controlled-run-metrics.json", metrics)
    _write_json(stage / "report-content-check.json", content_check)
    hashes = {name: _sha256_file(stage / name) for name in sources}
    if hashes["customer-report.html"] != hashes["upload-ready-report.html"]:
        raise AcceptanceBlocked("customer_report_copy_not_identical")
    _write_json(stage / "artifact-hashes.json", hashes)
    (stage / "README.md").write_text(
        "# ARV-001 complete-corpus controlled candidate\n\n"
        "Generated by the repository-owned one-shot local acceptance runner.\n",
        encoding="utf-8",
    )
    os.replace(stage, final_root)
    return {"manifest": manifest, "hashes": hashes, "metrics": metrics}


def main() -> int:
    args = _arguments()
    repo_root = _repository_root()
    try:
        git = _git_preflight(repo_root, args.expected_head)
        paths = _configure(args, repo_root)
        _initialize_local_runtime(
            paths, repo_root, initialize_database=args.initialize_database
        )
        values, shapes = load_candidate(paths["candidate_root"])
        physical = values["physical-files.json"]
        metadata = values["metadata.json"]
        if not isinstance(physical, list) or len(physical) != 10 or any(
            not isinstance(item, dict) for item in physical
        ):
            raise AcceptanceBlocked("physical_files_contract_invalid")
        actual_corpus_sha = _corpus_hash(physical)
        if actual_corpus_sha != args.expected_corpus_sha:
            raise AcceptanceBlocked("canonical_corpus_sha_mismatch")
        intake_summary = values["intake-summary.json"]
        if not isinstance(intake_summary, dict) or intake_summary.get(
            "corpus_sha256"
        ) != args.expected_corpus_sha:
            raise AcceptanceBlocked("intake_summary_corpus_sha_mismatch")
        validate_document_set(values, 10)
        contract = static_contract_preflight()
        database = database_preflight()
        provider = provider_preflight(
            paths["policy_path"], args.expected_policy_sha
        )
        from src.shared.config.settings import get_settings

        settings = get_settings()
        documents = _prepare_documents(
            physical=physical,
            metadata=metadata,
            intake_root=paths["intake_root"],
            max_chars=settings.document_extract_max_chars,
            chunk_size=settings.rag_chunk_size_chars,
            chunk_overlap=settings.rag_chunk_overlap_chars,
        )
        static = {
            "git": git,
            "artifact_shapes": shapes,
            "contract": contract,
            "database": database,
            "provider": provider,
            "corpus_sha256": actual_corpus_sha,
            "physical_file_count": 10,
            "mapped_file_count": len(documents),
            "total_prepared_chunks": sum(len(item.chunks) for item in documents),
            "source_file_mutations": 0,
        }
        if not args.execute_provider:
            print(
                json.dumps(
                    {"status": "static_preflight_complete", **static},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        final_root = paths["output_root"]
        stage = final_root.parent / f".{final_root.name}.partial.{uuid4().hex}"
        stage.mkdir(parents=True, mode=0o750)
        _write_json(stage / "static-preflight.json", static)
        application = create_application_data(
            customer_name=args.customer_name,
            project_name=args.project_name,
            registry_number=args.registry_number,
            corpus_sha=args.expected_corpus_sha,
            metadata=metadata,
            parse_summary=values["deterministic-parse-summary.json"],
            logical_documents=values["logical-documents.json"],
            documents=documents,
        )
        preflight = post_persistence_preflight(application["run_id"])
        controlled_root = stage / "controlled-evidence"
        invocation = _run_controlled_once(
            repo_root,
            application["run_id"],
            args.registry_number,
            paths["policy_path"],
            controlled_root,
        )
        final = _finalize(
            stage,
            controlled_root,
            final_root,
            application,
            preflight,
            invocation,
            args.registry_number,
        )
        metrics = final["metrics"]
        print(
            json.dumps(
                {
                    "status": "complete_corpus_report_ready_for_product_owner_review",
                    "marker": "ARV-001_COMPLETE_CORPUS_REPORT_READY_FOR_PRODUCT_OWNER_REVIEW",
                    "head_sha": git["head_sha"],
                    "customer_id": application["customer_id"],
                    "project_id": application["project_id"],
                    "case_id": application["case_id"],
                    "run_id": application["run_id"],
                    "document_count": application["document_count"],
                    "extracted_document_count": application[
                        "extracted_document_count"
                    ],
                    "chunk_count": application["chunk_count"],
                    "corpus_sha256": application["corpus_sha256"],
                    "source_graph_hash": application["source_graph_hash"],
                    "snapshot_verified": application["snapshot_verified"],
                    "post_persistence_gate5_ready": preflight[
                        "ready_for_controlled_execution"
                    ],
                    "controlled_invocation_count": 1,
                    **metrics,
                    "manifest_hash": final["manifest"].get("manifest_hash"),
                    "artifact_hashes": final["hashes"],
                    "output_root": str(final_root),
                    "production_db_mutations": 0,
                    "old_arv003_mutations": 0,
                    "git_mutations": 0,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except AcceptanceBlocked as exc:
        value = str(exc)
        safe = (
            value
            if value.isascii() and len(value) <= 300
            else "arv001_complete_corpus_acceptance_blocked"
        )
        print(safe, file=sys.stderr)
        return 2
    except Exception:
        print("arv001_complete_corpus_acceptance_failed", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
