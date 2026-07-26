---
name: security-review
description: Review an Arvectum change or subsystem for concrete security defects and control-boundary regressions. Use for auth, uploads, archives, HTML, secrets, external calls, personal data, dependencies, and deployment changes; default to read-only analysis.
---

# Security review

1. Define assets, trust boundaries, entry points, and realistic attacker capabilities.
2. Inspect the narrow diff or subsystem before scanning broadly.
3. Check authentication, authorization, tenant/workspace isolation, and object-level access.
4. Check input validation, injection, SSRF, path traversal, unsafe deserialization, archive bombs, and file-type confusion.
5. Check HTML/report rendering for XSS and unsafe links or embedded content.
6. Check secrets, certificates, tokens, cookies, logs, error payloads, and generated artifacts for exposure.
7. Check outbound network calls for allowlists, timeouts, redirects, TLS verification, and credential forwarding.
8. Check destructive operations, migrations, external side effects, and human-approval gates.
9. Check dependency advisories only against authoritative package/project sources when network access is available.
10. Rank findings by exploitability and impact. Give file/line evidence and a minimal remediation path.

## Output format

For each finding include: severity, affected boundary, evidence, realistic scenario, fix, and regression test. Do not inflate style issues into vulnerabilities. State when no concrete issue was found and list residual blind spots.