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
try:
    from arvectum_os_ref.audit_reconstruction_support import EvidenceAvailability
    from arvectum_os_ref.cross_capability_enforcement import (
        AccessRequest,
        CrossCapabilityEnforcementError,
    )
    from arvectum_os_ref.integration_composition import (
        IntegrationCompositionEvidenceRequiredError,
    )
    from arvectum_os_ref.product_capability_consumption import (
        CAP_001_DOCUMENT_ARTIFACT,
        CAP_002_MEMORY_KNOWLEDGE,
        CAP_003_SEARCH_PROJECTION,
        CAP_004_AUDIT_RECONSTRUCTION,
    )
    from arvectum_os_ref.product_contract import (
        ProductContractScopeError,
    )
    from arvectum_os_ref.product_contract_resolution import (
        DependencySupportDisposition,
        DeprecatedDependencyResolutionError,
        IncompatibleDependencyVersionError,
        UnsupportedDependencyResolutionError,
    )
    from arvectum_os_ref.security import OrganizationScope
    from p6_03_tender_operator_ref.scenario import (
        build_stage1_synthetic_scenario,
    )
    PLATFORM_PRESENT = True
except ImportError:
    PLATFORM_PRESENT = False

from src.modules.tender_operator_agent_demo.arvectum_os_bridge import ArvectumOSBridge


@unittest.skipUnless(PLATFORM_PRESENT, "Arvectum OS reference platform not present on PYTHONPATH")
class P603ArvectumOSBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scenario = build_stage1_synthetic_scenario()
        self.bridge = ArvectumOSBridge(self.scenario.adapters)

    def test_exact_p6_02_identity_and_dependencies(self) -> None:
        contract_pin = self.bridge.adapters.facade.context.product_contract
        self.assertEqual(
            contract_pin.version_id.value,
            "p6-02-arvectum-tender-operator-v0.1.0",
        )
        
        # Verify exact dependency set from the actual contract record
        # Note: IntegrationCompositionFacade._contract is not public, 
        # but the facade's creation_actor.organization and other context 
        # should match the contract's declaration.
        
        # We check the evaluations from compatibility report
        actual_deps = {
            e.dependency_id for e in self.bridge.adapters.facade.compatibility_evidence.evaluations
        }
        self.assertEqual(
            actual_deps, 
            {CAP_001_DOCUMENT_ARTIFACT, CAP_004_AUDIT_RECONSTRUCTION}
        )
        
        self.assertNotIn(CAP_002_MEMORY_KNOWLEDGE, actual_deps)
        self.assertNotIn(CAP_003_SEARCH_PROJECTION, actual_deps)

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
        
        # Request for different organization
        bad_request = replace(
            self.scenario.document_request, 
            organization=other_org,
            product_id=replace(self.scenario.contract.product_id, scope="other-org"),
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

    def test_access_denials_fail_closed(self) -> None:
        test_cases = [
            ("wrong_purpose", AccessRequest(self.scenario.actor, "wrong", "read", ("restricted-pilot",))),
            ("wrong_right", AccessRequest(self.scenario.actor, "prebid-review", "write", ("restricted-pilot",))),
            ("wrong_classification", AccessRequest(self.scenario.actor, "prebid-review", "read", ("unrestricted",))),
        ]
        
        for name, denied_access in test_cases:
            with self.subTest(case=name):
                bad_request = replace(self.scenario.document_request, access=denied_access)
                with self.assertRaises(CrossCapabilityEnforcementError):
                    self.bridge.resolve_document(
                        request=bad_request,
                        governed_versions=self.scenario.governed_versions,
                        admitted=self.scenario.admitted_document,
                        artifact_id=self.scenario.artifact_id,
                    )

    def test_incompatible_provider_version_fails_closed(self) -> None:
        base_versions = list(self.scenario.governed_versions)
        
        for i, ev in enumerate(base_versions):
            if ev.dependency_id == CAP_004_AUDIT_RECONSTRUCTION:
                base_versions[i] = replace(ev, contract_version="2.0.0")
                
        with self.assertRaises(IncompatibleDependencyVersionError):
            ArvectumOSBridge.compose(
                actor=self.scenario.actor,
                created_at=datetime.now(UTC),
                governed_versions=tuple(base_versions),
            )

    def test_deprecated_provider_evidence_fails_closed(self) -> None:
        base_versions = list(self.scenario.governed_versions)
        
        for i, ev in enumerate(base_versions):
            if ev.dependency_id == CAP_004_AUDIT_RECONSTRUCTION:
                base_versions[i] = replace(
                    ev, 
                    disposition=DependencySupportDisposition.DEPRECATED,
                    migration_obligation="upgrade required"
                )
                
        with self.assertRaises(DeprecatedDependencyResolutionError):
            ArvectumOSBridge.compose(
                actor=self.scenario.actor,
                created_at=datetime.now(UTC),
                governed_versions=tuple(base_versions),
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
        restricted_item = next(i for i in view.evidence if i.version_id == restricted_version)
        self.assertIs(restricted_item.availability, EvidenceAvailability.REDACTED)

    def test_structural_private_import_guard(self) -> None:
        bridge_path = Path(__file__).resolve().parents[1] / "src/modules/tender_operator_agent_demo/arvectum_os_bridge.py"
        with open(bridge_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
            
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self._check_module(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                self._check_module(node.module)

    def _check_module(self, name: str) -> None:
        if name.startswith("arvectum_os_ref") and name != "arvectum_os_ref.integration_adapters":
            self.fail(f"Forbidden private platform import in bridge: {name}")

    def test_no_direct_network_or_process_dependencies_in_bridge(self) -> None:
        bridge_path = Path(__file__).resolve().parents[1] / "src/modules/tender_operator_agent_demo/arvectum_os_bridge.py"
        with open(bridge_path, "r", encoding="utf-8") as f:
            content = f.read()
            
        forbidden = (
            "requests", "httpx", "urllib", "socket", "subprocess", 
            "webbrowser", "playwright", "selenium"
        )
        for pkg in forbidden:
            self.assertNotIn(f"import {pkg}", content)
            self.assertNotIn(f"from {pkg}", content)


if __name__ == "__main__":
    unittest.main()
