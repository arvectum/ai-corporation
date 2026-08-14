"""Product-side bridge for Arvectum OS P6.03 Stage 1.

This module provides the product-owned seam for establishing a governed
connection to Arvectum OS through the internal/provisional IntegrationAdapters
boundary. It enforces strict P6.02 Product Contract 0.1.0 continuity and
limits dependencies to CAP-001 and CAP-004 only.

Procurement-domain interpretation and workflow semantics remain product-owned.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arvectum_os_ref.identity import Identity
    from arvectum_os_ref.integration_adapters import IntegrationAdapters
    from arvectum_os_ref.product_capability_consumption import (
        CapabilityConsumptionRequest,
    )
    from arvectum_os_ref.product_contract_resolution import (
        GovernedDependencyVersionEvidence,
    )
    from arvectum_os_ref.security import ActorContext


@dataclass(frozen=True, slots=True)
class ArvectumOSBridge:
    """Product-owned bridge for governed Arvectum OS interaction."""

    adapters: IntegrationAdapters
    
    @classmethod
    def compose(
        cls,
        *,
        actor: ActorContext,
        created_at: datetime,
        governed_versions: tuple[GovernedDependencyVersionEvidence, ...],
    ) -> ArvectumOSBridge:
        """Compose the bridge using the platform IntegrationAdapters factory.
        
        Requires the external 'arvectum_os_ref' package to be available on PYTHONPATH.
        """
        # Note: Imports are inside to avoid making arvectum_os_ref a hard package-level 
        # dependency of the product module if it's not present in all environments.
        from arvectum_os_ref.integration_adapters import compose_integration_adapters
        from p6_03_tender_operator_ref.contract import build_p6_02_product_contract

        contract = build_p6_02_product_contract(actor=actor, created_at=created_at)
        
        adapters = compose_integration_adapters(
            contract=contract,
            actor=actor,
            effective_product_contract=contract.version_pin,
            governed_versions=governed_versions,
        )
        return cls(adapters)

    def resolve_document(
        self,
        *,
        request: CapabilityConsumptionRequest,
        governed_versions: tuple[GovernedDependencyVersionEvidence, ...] | None,
        admitted: Any,
        artifact_id: Identity,
    ) -> Any:
        """Delegate document resolution to the platform capability adapter."""
        return self.adapters.capabilities.resolve_document(
            request=request,
            governed_versions=governed_versions,
            admitted=admitted,
            artifact_id=artifact_id,
        )

    def reconstruct_execution(
        self,
        *,
        request: CapabilityConsumptionRequest,
        governed_versions: tuple[GovernedDependencyVersionEvidence, ...] | None,
        manifest: Any,
        evidence_constraints: tuple[tuple[Identity, str, tuple[str, ...], str], ...],
    ) -> Any:
        """Delegate execution reconstruction to the platform capability adapter."""
        return self.adapters.capabilities.reconstruct_execution(
            request=request,
            governed_versions=governed_versions,
            manifest=manifest,
            evidence_constraints=evidence_constraints,
        )
