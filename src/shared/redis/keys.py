from __future__ import annotations

import hashlib


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_key(
    namespace: str,
    environment: str,
    component: str,
    *,
    tenant: str | None = None,
    customer: str | None = None,
    project: str | None = None,
    case: str | None = None,
    run: str | None = None,
    operation: str | None = None,
    dimension: str | None = None,
    user_controlled: str | None = None,
) -> str:
    if tenant is not None and not tenant:
        raise ValueError("tenant must be non-empty when provided")
    parts = [namespace, environment, component]
    if tenant is not None:
        parts.append(tenant)
    if customer is not None:
        parts.append(customer)
    if project is not None:
        parts.append(project)
    if case is not None:
        parts.append(case)
    if run is not None:
        parts.append(run)
    if operation is not None:
        parts.append(operation)
    if dimension is not None:
        parts.append(dimension)
    if user_controlled is not None:
        parts.append(_sha256(user_controlled))
    key = ":".join(parts)
    return key


def build_idempotency_key(
    namespace: str,
    environment: str,
    customer: str,
    project: str,
    case: str,
    idempotency_key_raw: str,
) -> str:
    return build_key(
        namespace=namespace,
        environment=environment,
        component="idempotency",
        customer=customer,
        project=project,
        case=case,
        user_controlled=idempotency_key_raw,
    )


def build_lock_key(
    namespace: str,
    environment: str,
    customer: str,
    project: str,
    case: str,
    operation: str,
    idempotency_key_raw: str,
) -> str:
    return build_key(
        namespace=namespace,
        environment=environment,
        component="lock",
        customer=customer,
        project=project,
        case=case,
        operation=operation,
        user_controlled=idempotency_key_raw,
    )
