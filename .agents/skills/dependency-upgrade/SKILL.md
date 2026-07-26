---
name: dependency-upgrade
description: Upgrade or add a Python or tooling dependency in Arvectum with compatibility, security, lockfile, and regression checks. Use for pyproject or toolchain changes; do not add production dependencies casually.
---

# Dependency upgrade

1. State why the dependency change is necessary and whether it is production, development, or optional.
2. Check authoritative release notes, migration guides, Python compatibility, and known breaking changes.
3. Prefer the smallest supported version-range change; avoid unrelated bulk upgrades.
4. Inspect direct API usage and transitive constraints before editing.
5. Update the lockfile with the project package manager and review unexpected transitive changes.
6. Check licenses and supply-chain risk for new packages.
7. Run focused tests for affected integrations and the full suite for framework, ORM, validation, HTTP, report, or test-runner upgrades.
8. Verify import/startup and one representative runtime flow.
9. Document any required environment or deployment change.
10. Report package versions, transitive impact, tests, and rollback approach.

## Stop and ask

Ask before adding a new production dependency when existing standard-library or installed-project functionality can reasonably solve the problem.