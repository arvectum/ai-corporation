"""Focused product-side tests for ArvectumOSBridge (P6.03 Stage 1).

These tests prove that the product bridge correctly establishes and maintains
the governed boundary defined by P6.02 Product Contract 0.1.0, preserving
external authority and fail-closed security.
"""

from __future__ import annotations

import ast
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

# These imports require reference/python from arvectum-os on PYTHONPATH
from arvectum_os_ref.cross_capability_enforcement import (
    AccessRequest,
    CrossCapabilityEnforcementError,
)
from arvectum_os_ref.integration_composition import (
    IntegrationCompositionEvidenceRequiredError,
)
from arvectum_os_ref.product_contract import (
    ProductContractScopeError,
)
from arvectum_os_ref.product_contract_resolution import (
    UnsupportedDependencyResolutionError,
)
from arvectum_os_ref.security import OrganizationScope
from p6_03_tender_operator_ref.scenario import (
    build_stage1_synthetic_scenario,
)

from src.modules.tender_operator_agent_demo.arvectum_os_bridge import ArvectumOSBridge


class P603ArvectumOSBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scenario = build_stage1_synthetic_scenario()
        self.bridge = ArvectumOSBridge(self.scenario.adapters)

    def test_bridge_composition_passes_with_canonical_scenario(self) -> None:
        # The setUp already proved composition via ArvectumOSBridge(adapters)
        self.assertIs(self.bridge.adapters, self.scenario.adapters)
        self.assertEqual(
            self.bridge.adapters.facade.context.product_contract.version_id.value,
            "p6-02-arvectum-tender-operator-v0.1.0",
        )

    def test_happy_path_document_resolution(self) -> None:
        reliance = self.bridge.resolve_document(
            request=self.scenario.document_request,
            governed_versions=self.scenario.governed_versions,
            admitted=self.scenario.admitted_document,
            artifact_id=self.scenario.artifact_id,
        )
        self.assertEqual(reliance.document_version_id, self.scenario.admitted_document.version_id)
        self.assertEqual(
            self.scenario.admitted_document.canonical_record.external_authority.authoritative_system,
            "synthetic-redacted-eis-source",
        )

    def test_wrong_organization_fails_closed(self) -> None:
        other_org_id = replace(self.scenario.organization.organization_id, value="other-org")
        other_org = OrganizationScope(other_org_id)
        other_product_id = replace(self.scenario.contract.product_id, scope="other-org")
        
        # Request for different organization
        bad_request = replace(
            self.scenario.document_request, 
            organization=other_org,
            product_id=other_product_id,
            access=replace(
                self.scenario.document_request.access, 
                actor=replace(self.scenario.actor, organization=other_org)
            )
        )
        
        with self.assertRaises(ProductContractScopeError):
            self.bridge.resolve_document(
                request=bad_request,
                governed_versions=self.scenario.governed_versions,
                admitted=self.scenario.admitted_document,
                artifact_id=self.scenario.artifact_id,
            )

    def test_purpose_right_denial_fails_closed(self) -> None:
        denied_access = AccessRequest(
            self.scenario.actor,
            "wrong-purpose",
            "read",
            ("restricted-pilot",),
        )
        bad_request = replace(self.scenario.document_request, access=denied_access)
        
        with self.assertRaises(CrossCapabilityEnforcementError):
            self.bridge.resolve_document(
                request=bad_request,
                governed_versions=self.scenario.governed_versions,
                admitted=self.scenario.admitted_document,
                artifact_id=self.scenario.artifact_id,
            )

    def test_missing_provider_evidence_fails_closed(self) -> None:
        cap001_only = (self.scenario.governed_versions[0],)
        with self.assertRaises(UnsupportedDependencyResolutionError):
            ArvectumOSBridge.compose(
                actor=self.scenario.actor,
                created_at=datetime.now(UTC),
                governed_versions=cap001_only,
            )

    def test_omitted_provider_evidence_after_composition_fails_closed(self) -> None:
        with self.assertRaises(IntegrationCompositionEvidenceRequiredError):
            self.bridge.resolve_document(
                request=self.scenario.document_request,
                governed_versions=None,
                admitted=self.scenario.admitted_document,
                artifact_id=self.scenario.artifact_id,
            )

    def test_truthful_incomplete_reconstruction(self) -> None:
        # Create a more restricted constraint set that will trigger redaction
        restricted_version = self.scenario.reconstruction_manifest.material_inputs[0].version_id
        constrained = tuple(
            (version_id, purpose, rights, "denied-classification")
            if version_id == restricted_version
            else (version_id, purpose, rights, classification)
            for version_id, purpose, rights, classification in self.scenario.evidence_constraints
        )
        
        view = self.bridge.reconstruct_execution(
            request=self.scenario.reconstruction_request,
            governed_versions=self.scenario.governed_versions,
            manifest=self.scenario.reconstruction_manifest,
            evidence_constraints=constrained,
        )
        self.assertFalse(view.complete)
        # Check that the restricted version is reported as redacted
        restricted_item = next(i for item in view.evidence if (i := item).version_id == restricted_version)
        from arvectum_os_ref.audit_reconstruction_support import EvidenceAvailability
        self.assertIs(restricted_item.availability, EvidenceAvailability.REDACTED)

    def test_structural_private_import_guard(self) -> None:
        bridge_path = Path(__file__).resolve().parents[1] / "src/modules/tender_operator_agent_demo/arvectum_os_bridge.py"
        with open(bridge_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
            
        allowed_prefixes = (
            "arvectum_os_ref.integration_adapters",
            "arvectum_os_ref.product_capability_consumption",
            "arvectum_os_ref.product_contract_resolution",
            "arvectum_os_ref.security",
            "p6_03_tender_operator_ref.contract",
        )
        
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self._check_module(alias.name, allowed_prefixes)
            elif isinstance(node, ast.ImportFrom) and node.module:
                self._check_module(node.module, allowed_prefixes)

    def _check_module(self, name: str, allowed_prefixes: tuple[str, ...]) -> None:
        if (
            name.startswith("arvectum_os_ref")
            and not any(name.startswith(p) for p in allowed_prefixes)
            and name != "arvectum_os_ref.identity"
        ):
            self.fail(f"Forbidden private platform import in bridge: {name}")

    def test_no_direct_network_or_process_dependencies_in_bridge(self) -> None:
        bridge_path = Path(__file__).resolve().parents[1] / "src/modules/tender_operator_agent_demo/arvectum_os_bridge.py"
        with open(bridge_path, "r", encoding="utf-8") as f:
            content = f.read()
            
        forbidden = ("requests", "httpx", "urllib", "subprocess", "socket", "webbrowser")
        for pkg in forbidden:
            self.assertNotIn(f"import {pkg}", content)
            self.assertNotIn(f"from {pkg}", content)


if __name__ == "__main__":
    unittest.main()
