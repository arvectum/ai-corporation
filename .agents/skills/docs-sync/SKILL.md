---
name: docs-sync
description: Update Arvectum technical and product documentation after a verified code or workflow change. Use to keep runbooks, API notes, acceptance criteria, and architecture boundaries aligned; do not rewrite unrelated docs.
---

# Documentation sync

1. Identify the changed observable behavior, interface, command, configuration, or operational step.
2. Find the nearest canonical document from links in `AGENTS.md`; do not scan or rewrite the whole docs tree.
3. Update only statements made stale by the change.
4. Prefer examples that are executable or tied to tested commands.
5. Separate current behavior, planned work, and known limitations.
6. Preserve safety and human-control wording; do not imply autonomy that the product does not have.
7. Update acceptance checklists/runbooks when operator steps or verification changes.
8. Check internal links and referenced paths.
9. Avoid duplicating the same procedure in multiple documents; link to one source of truth.
10. Summarize which code evidence supports each material documentation claim.