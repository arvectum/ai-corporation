---
description: Produce an evidence-based GO, conditional GO, or NO-GO for merge or deployment
agent: reviewer
subtask: true
---

Load the `release-readiness` skill.

Assess this branch, PR, or release candidate:

$ARGUMENTS

Check the actual diff, tests run, migrations, configuration, security-sensitive boundaries, documentation, and rollback. Do not infer that checks passed when there is no execution evidence.