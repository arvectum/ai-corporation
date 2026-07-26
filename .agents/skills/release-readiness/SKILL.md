---
name: release-readiness
description: Assess whether an Arvectum branch or release candidate is ready to merge or deploy. Use after implementation to verify tests, migrations, security, docs, control boundaries, and operational rollback.
---

# Release readiness

1. State the release scope and compare the branch against its base.
2. Review changed files, public behavior, dependencies, migrations, configuration, and data handling.
3. Confirm required targeted tests, full-suite conditions, lint, and relevant browser/live smoke checks were actually run.
4. Verify migration order, compatibility, backups, and rollback for persistence changes.
5. Verify secrets/configuration are documented but not committed.
6. Verify security-sensitive changes received focused review.
7. Verify runbooks, API docs, and acceptance criteria match the implementation.
8. Confirm restricted-pilot and human-control boundaries remain intact.
9. List known issues and classify them as blocker, accepted risk, or follow-up.
10. Return a clear GO, GO WITH CONDITIONS, or NO-GO with evidence.

## Mandatory no-go examples

- failing or unrun required tests;
- irreversible migration without approved recovery;
- real partner data or secrets in the diff;
- unreviewed auth, EDS, external submission, or destructive side effect;
- documentation claiming behavior not present in code.