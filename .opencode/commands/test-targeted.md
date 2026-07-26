---
description: Design or run the smallest pytest set that proves a requested behavior
agent: test-engineer
subtask: true
---

Load the `regression-test-design` and `verify-change` skills.

Target behavior or changed area:

$ARGUMENTS

Find the nearest tests, choose the narrowest useful node IDs, and run targeted checks before proposing a broader suite. Do not run every test by default when a focused contract is sufficient.