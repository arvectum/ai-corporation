from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.p8_05_temporal_acceptance_binding import (
    AUTHORIZED_STATUS,
    DRIFT_BLOCKING,
    P8_05_RESULT_SCHEMA_VERSION,
    P805AcceptanceBindingBlocked,
    build_authorization_manifest,
    build_fresh_material_snapshot,
    build_revalidation_manifest,
    canonical_sha256,
    execute_authorized_once,
    load_and_verify_frozen_baseline,
    load_frozen_candidate_material,
    safe_child_failure,
    write_manifest,
)
from src.modules.tender_operator_agent_demo.procurement_intake_service import (
    create_run_from_eis_docs_archive,
)
from src.modules.tender_operator_agent_demo.schemas import EisDocsArchiveRunRequest
from src.modules.tender_operator_agent_demo.upload_service_legacy import (
    get_demo_run_input_dir,
    load_demo_run_metadata,
)

_SUCCESS_MARKER = "ARV-001_COMPLETE_CORPUS_REPORT_READY_FOR_PRODUCT_OWNER_REVIEW"
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_ACCEPTANCE_MODULE = "scripts.arv001.run_complete_corpus_acceptance"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "P8.05: revalidate frozen ARV-001 baseline against fresh read-only EIS "
            "and, only on pass, consume one authorization for the controlled "
            "complete-corpus generation acceptance."
        )
    )
    parser.add_argument(
        "--baseline-descriptor",
        type=Path,
        default=PROJECT_ROOT / "config/arv001/acceptance_baseline.json",
    )
    parser.add_argument("--baseline-candidate-root", type=Path, required=True)
    parser.add_argument("--baseline-intake-root", type=Path)
    parser.add_argument("--database-path", type=Path, required=True)
    parser.add_argument("--initialize-database", action="store_true")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--approved-policy", type=Path, required=True)
    parser.add_argument("--acceptance-output-root", type=Path, required=True)
    parser.add_argument("--binding-root", type=Path, required=True)
    parser.add_argument("--expected-head", required=True)
    return parser.parse_args()


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _outside_repository(path: Path, code: str) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    repo = PROJECT_ROOT.resolve(strict=True)
    if resolved == repo or repo in resolved.parents:
        raise P805AcceptanceBindingBlocked(code)
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _git_preflight(expected_head: str) -> None:
    try:
        actual_head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            text=True,
        ).strip()
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"],
            cwd=PROJECT_ROOT,
            text=True,
        ).strip()
        status = subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=PROJECT_ROOT,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise P805AcceptanceBindingBlocked("BLOCKED_GIT_PREFLIGHT_UNAVAILABLE") from exc
    if actual_head != expected_head:
        raise P805AcceptanceBindingBlocked("BLOCKED_REPOSITORY_HEAD_MISMATCH")
    if branch not in {"", "main"}:
        raise P805AcceptanceBindingBlocked("BLOCKED_REPOSITORY_NOT_MAIN_OR_DETACHED")
    if status:
        raise P805AcceptanceBindingBlocked("BLOCKED_REPOSITORY_WORKTREE_NOT_CLEAN")


def _preflight(
    args: argparse.Namespace,
) -> tuple[Path, Path, Path, Path, Path, Path, Path, str]:
    head = str(args.expected_head or "").strip().lower()
    if not _HEX40.fullmatch(head):
        raise P805AcceptanceBindingBlocked("BLOCKED_EXPECTED_HEAD_INVALID")
    _git_preflight(head)
    if os.environ.get("ZAKUPKI_GOV_RU_SOAP_ENABLED") != "1":
        raise P805AcceptanceBindingBlocked("BLOCKED_EIS_NOT_ENABLED")
    if not os.environ.get("ZAKUPKI_GOV_RU_SOAP_TOKEN"):
        raise P805AcceptanceBindingBlocked("BLOCKED_EIS_CREDENTIAL_MISSING")
    if os.environ.get("ARVECTUM_ETP_TLS_ENABLED") != "true":
        raise P805AcceptanceBindingBlocked("BLOCKED_TLS_NOT_ENABLED")
    if not os.environ.get("ARVECTUM_ETP_TLS_POLICY_PATH"):
        raise P805AcceptanceBindingBlocked("BLOCKED_TLS_POLICY_MISSING")

    try:
        candidate = args.baseline_candidate_root.expanduser().resolve(strict=True)
        intake = (args.baseline_intake_root or candidate).expanduser().resolve(strict=True)
        policy = args.approved_policy.expanduser().resolve(strict=True)
        descriptor = args.baseline_descriptor.expanduser().resolve(strict=True)
    except OSError as exc:
        raise P805AcceptanceBindingBlocked("BLOCKED_REQUIRED_LOCAL_INPUT_MISSING") from exc
    if not candidate.is_dir() or not intake.is_dir():
        raise P805AcceptanceBindingBlocked("BLOCKED_FROZEN_BASELINE_ROOT_MISSING")
    if not policy.is_file() or policy.is_symlink():
        raise P805AcceptanceBindingBlocked("BLOCKED_APPROVED_POLICY_MISSING")
    if not descriptor.is_file() or descriptor.is_symlink():
        raise P805AcceptanceBindingBlocked("BLOCKED_FROZEN_BASELINE_MISSING")

    binding = _outside_repository(args.binding_root, "BLOCKED_BINDING_ROOT_INSIDE_REPOSITORY")
    acceptance = _outside_repository(
        args.acceptance_output_root,
        "BLOCKED_ACCEPTANCE_OUTPUT_INSIDE_REPOSITORY",
    )
    database = _outside_repository(args.database_path, "BLOCKED_DATABASE_INSIDE_REPOSITORY")
    data_dir = _outside_repository(args.data_dir, "BLOCKED_DATA_DIR_INSIDE_REPOSITORY")
    if binding.exists():
        raise P805AcceptanceBindingBlocked("BLOCKED_BINDING_ROOT_ALREADY_EXISTS")
    if acceptance.exists():
        raise P805AcceptanceBindingBlocked("BLOCKED_ACCEPTANCE_OUTPUT_ALREADY_EXISTS")
    return candidate, intake, binding, acceptance, database, data_dir, policy, head


def _acceptance_command(
    *,
    candidate_root: Path,
    intake_root: Path,
    database_path: Path,
    data_dir: Path,
    approved_policy: Path,
    output_root: Path,
    expected_head: str,
    registry_number: str,
    corpus_sha256: str,
    policy_sha256: str,
    initialize_database: bool,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        _ACCEPTANCE_MODULE,
        "--candidate-root",
        str(candidate_root),
        "--intake-root",
        str(intake_root),
        "--database-path",
        str(database_path),
        "--data-dir",
        str(data_dir),
        "--approved-policy",
        str(approved_policy),
        "--output-root",
        str(output_root),
        "--expected-head",
        expected_head,
        "--registry-number",
        registry_number,
        "--expected-corpus-sha",
        corpus_sha256,
        "--expected-policy-sha",
        policy_sha256,
        "--execute-provider",
    ]
    if initialize_database:
        command.append("--initialize-database")
    return command


def _flag_value(command: list[str], flag: str) -> str | None:
    if command.count(flag) != 1:
        return None
    index = command.index(flag)
    if index + 1 >= len(command):
        return None
    return command[index + 1]


def _validate_acceptance_command(command: list[str], authorization: dict[str, Any]) -> None:
    if (
        len(command) < 4
        or command[1:3] != ["-m", _ACCEPTANCE_MODULE]
        or command.count("--execute-provider") != 1
        or any(
            flag in command
            for flag in ("--static-only", "--prepare-only", "--verify-pre-provider-stage-boundary")
        )
        or _flag_value(command, "--expected-head") != authorization.get("expected_head")
        or _flag_value(command, "--registry-number") != authorization.get("registry_number")
        or _flag_value(command, "--expected-corpus-sha") != authorization.get("corpus_sha256")
        or _flag_value(command, "--expected-policy-sha") != authorization.get("policy_sha256")
    ):
        raise P805AcceptanceBindingBlocked("BLOCKED_ACCEPTANCE_COMMAND_NOT_BOUND")


def _parse_success(stdout: str, *, expected_head: str, expected_corpus: str) -> dict[str, Any]:
    try:
        value = json.loads(stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise P805AcceptanceBindingBlocked("BLOCKED_ACCEPTANCE_SUCCESS_OUTPUT_INVALID") from exc
    if not isinstance(value, dict):
        raise P805AcceptanceBindingBlocked("BLOCKED_ACCEPTANCE_SUCCESS_OUTPUT_INVALID")
    if (
        value.get("status") != "complete_corpus_report_ready_for_product_owner_review"
        or value.get("marker") != _SUCCESS_MARKER
        or value.get("head_sha") != expected_head
        or value.get("corpus_sha256") != expected_corpus
        or value.get("controlled_invocation_count") != 1
        or value.get("production_db_mutations") != 0
        or value.get("old_arv003_mutations") != 0
        or value.get("git_mutations") != 0
    ):
        raise P805AcceptanceBindingBlocked("BLOCKED_ACCEPTANCE_SUCCESS_CONTRACT_MISMATCH")
    artifact_hashes = value.get("artifact_hashes")
    if not isinstance(artifact_hashes, dict) or not artifact_hashes:
        raise P805AcceptanceBindingBlocked("BLOCKED_ACCEPTANCE_ARTIFACT_HASHES_MISSING")
    return value


def _failure_result(
    *,
    code: str,
    acceptance_invocations: int,
    revalidation: dict[str, Any] | None,
    authorization: dict[str, Any] | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": P8_05_RESULT_SCHEMA_VERSION,
        "status": "FAIL_CLOSED",
        "failure_code": code,
        "authorization_status": (
            authorization.get("status") if isinstance(authorization, dict) else None
        ),
        "authorization_consumed": acceptance_invocations == 1,
        "acceptance_invocations": acceptance_invocations,
        "revalidation_manifest_sha256": (
            revalidation.get("manifest_sha256") if isinstance(revalidation, dict) else None
        ),
        "authorization_manifest_sha256": (
            authorization.get("manifest_sha256") if isinstance(authorization, dict) else None
        ),
        "external_actions": False,
    }
    return {**body, "manifest_sha256": canonical_sha256(body)}


def main() -> int:
    binding: Path | None = None
    revalidation: dict[str, Any] | None = None
    authorization: dict[str, Any] | None = None
    acceptance_invocations = 0
    try:
        args = _arguments()
        candidate, intake, binding, acceptance, database, data_dir, policy, head = _preflight(args)
        baseline = load_and_verify_frozen_baseline(args.baseline_descriptor)
        if _sha256_file(policy) != baseline["policy"]["sha256"]:
            raise P805AcceptanceBindingBlocked("BLOCKED_APPROVED_POLICY_HASH_MISMATCH")
        baseline_snapshot = load_frozen_candidate_material(candidate, baseline)

        # The fresh EIS acquisition is a read-only temporal gate. The controlled
        # generation deliberately runs on the frozen baseline bytes after the gate
        # passes, so the accepted input identity cannot drift between revalidation
        # and model execution.
        result = create_run_from_eis_docs_archive(
            EisDocsArchiveRunRequest(
                reestr_number=baseline["registry_number"],
                law="44fz",
                subsystem_type="PRIZ",
                method="getDocsByReestrNumber",
                download_archive=True,
                analyze_after_download=False,
            )
        )
        fresh_metadata = load_demo_run_metadata(result.run_id)
        fresh_snapshot = build_fresh_material_snapshot(
            fresh_metadata,
            input_dir=get_demo_run_input_dir(result.run_id),
            baseline=baseline,
        )
        revalidation = build_revalidation_manifest(
            baseline,
            baseline_snapshot,
            fresh_snapshot,
        )

        binding.mkdir(parents=True, mode=0o700)
        write_manifest(binding / "p8-05-revalidation.json", revalidation)
        if revalidation["drift_classification"] == DRIFT_BLOCKING:
            _print(
                {
                    "status": "FAIL_CLOSED",
                    "failure_code": "BLOCKED_TEMPORAL_REVALIDATION",
                    "revalidation_status": revalidation["status"],
                    "drift_classification": revalidation["drift_classification"],
                    "revalidation_manifest_sha256": revalidation["manifest_sha256"],
                    "acceptance_invocations": 0,
                    "provider_execution_authorized": False,
                    "external_actions": False,
                }
            )
            return 2

        authorization = build_authorization_manifest(
            baseline,
            revalidation,
            expected_head=head,
        )
        write_manifest(binding / "p8-05-authorization.json", authorization)
        command = _acceptance_command(
            candidate_root=candidate,
            intake_root=intake,
            database_path=database,
            data_dir=data_dir,
            approved_policy=policy,
            output_root=acceptance,
            expected_head=head,
            registry_number=baseline["registry_number"],
            corpus_sha256=baseline["corpus"]["sha256"],
            policy_sha256=baseline["policy"]["sha256"],
            initialize_database=args.initialize_database,
        )
        _validate_acceptance_command(command, authorization)
        acceptance_invocations = 1
        completed = execute_authorized_once(
            authorization,
            command,
            expected_head=head,
            env=os.environ.copy(),
        )
        if completed.returncode != 0:
            failure = _failure_result(
                code=safe_child_failure(completed.stderr),
                acceptance_invocations=1,
                revalidation=revalidation,
                authorization=authorization,
            )
            write_manifest(binding / "p8-05-bound-acceptance-result.json", failure)
            _print(failure)
            return 3

        success = _parse_success(
            completed.stdout,
            expected_head=head,
            expected_corpus=baseline["corpus"]["sha256"],
        )
        body = {
            "schema_version": P8_05_RESULT_SCHEMA_VERSION,
            "status": "PASS",
            "marker": "P8_05_BOUND_ACCEPTANCE_COMPLETE",
            "authorization_status": AUTHORIZED_STATUS,
            "authorization_consumed": True,
            "acceptance_invocations": 1,
            "expected_head": head,
            "baseline_id": baseline["baseline_id"],
            "registry_number": baseline["registry_number"],
            "corpus_sha256": baseline["corpus"]["sha256"],
            "revalidation_drift_classification": revalidation["drift_classification"],
            "revalidation_manifest_sha256": revalidation["manifest_sha256"],
            "authorization_manifest_sha256": authorization["manifest_sha256"],
            "controlled_invocation_count": success["controlled_invocation_count"],
            "execution_count": success.get("execution_count"),
            "repeat_identity_verified": success.get("repeat_identity_verified"),
            "artifact_hashes": success["artifact_hashes"],
            "production_db_mutations": 0,
            "old_arv003_mutations": 0,
            "git_mutations": 0,
            "external_actions": False,
        }
        final = {**body, "manifest_sha256": canonical_sha256(body)}
        write_manifest(binding / "p8-05-bound-acceptance-result.json", final)
        _print(final)
        return 0
    except P805AcceptanceBindingBlocked as exc:
        failure = _failure_result(
            code=exc.code,
            acceptance_invocations=acceptance_invocations,
            revalidation=revalidation,
            authorization=authorization,
        )
        if binding is not None and binding.is_dir() and acceptance_invocations == 1:
            write_manifest(binding / "p8-05-bound-acceptance-result.json", failure)
        _print(failure)
        return 2
    except Exception as exc:  # noqa: BLE001 - sanitize the terminal boundary.
        failure = _failure_result(
            code=f"runtime_error:{type(exc).__name__}",
            acceptance_invocations=acceptance_invocations,
            revalidation=revalidation,
            authorization=authorization,
        )
        if binding is not None and binding.is_dir() and acceptance_invocations == 1:
            write_manifest(binding / "p8-05-bound-acceptance-result.json", failure)
        _print(failure)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
