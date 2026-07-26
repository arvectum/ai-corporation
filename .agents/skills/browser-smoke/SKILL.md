---
name: browser-smoke
description: Verify an Arvectum web or demo flow in a real browser using Playwright or another approved browser tool. Use after UI, routing, upload, report, or end-to-end workflow changes; do not replace API/unit tests.
---

# Browser smoke workflow

1. Identify the exact user journey and expected visible checkpoints.
2. Start the documented local stack and confirm health before opening the browser.
3. Use synthetic inputs and a clean browser context.
4. Exercise the shortest complete path, including validation errors and loading/failure states when relevant.
5. Capture console errors, failed network requests, status codes, and screenshots only for failed or review-critical states.
6. Verify keyboard-accessible controls, labels, Russian text, and major viewport overflow for changed screens.
7. Confirm downloads/exports by checking response and file existence; do not rely only on a toast.
8. Avoid brittle selectors based on visual position. Prefer roles, labels, and stable test IDs.
9. Record exact steps and observed result. Convert repeatable critical flows into Playwright tests when stable.
10. Keep browser evidence out of Git unless explicitly intended and sanitized.

## Arvectum priority flows

- `/demo/tender-agent` search/intake/upload/analyze/report;
- unified site-to-pilot entrypoint;
- invalid archive/document handling;
- operator review and human-control checkpoints;
- generated HTML report rendering and links.