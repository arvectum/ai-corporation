---
name: large-repo-navigation
description: Explore the Arvectum repository with a strict context budget. Use when ownership is unclear, the repository feels large, or the task risks reading many unrelated files.
---

# Token-efficient repository navigation

Use progressive narrowing rather than loading documents wholesale.

1. Start from the task's nouns: route, service, model, command, document type, or user-visible label.
2. Search file names and exact symbols first.
3. Read definitions, direct callers, and the nearest focused tests before opening broad documentation.
4. Follow imports and call chains one hop at a time.
5. Read only relevant line ranges from large files.
6. Keep a compact working map containing:
   - owning file and symbol;
   - direct inputs and outputs;
   - relevant tests;
   - constraints or documents still needed.
7. Stop exploration once the change boundary and verification path are clear.

## Documentation policy

- Do not read the full root README by default.
- Do not recursively load `docs/`.
- Open only the source-of-truth documents named by `AGENTS.md` when the task touches their boundary.
- For library/API behavior, retrieve current targeted documentation through Context7 or another documentation MCP instead of pasting an entire manual.

## Output

Before editing, provide a short repository map and name the files that are intentionally out of scope.