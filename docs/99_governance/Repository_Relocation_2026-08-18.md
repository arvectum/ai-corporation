# Tender Agent Repository Relocation Record

Status: `Recorded`
Date: `2026-08-18`
Owner: `ООО «Арвектум»`

## Purpose

This record preserves the non-destructive repository identity relocation of the Tender Agent product.

## Repository identity

- Current canonical GitHub repository: `arvectum/tender-agent`
- Historical GitHub locator: `arvectum/ai-corporation`
- GitHub repository ID: `1333401651`
- Pre-relocation canonical `main` SHA: `4558880d43455ca9ed482b5bbdefe6b9c137277a`
- GitVerse mirror repository: `arvectum/tender-agent`

The unchanged GitHub repository ID and preserved pre-relocation `main` SHA demonstrate that this was a repository rename, not a copied or replacement repository.

The historical `arvectum/ai-corporation` name is reserved for redirect/history compatibility and MUST NOT be reused for another repository while that compatibility is relied upon.

## Scope

This relocation changes repository naming and locators only. It does not change:

- product identity or procurement-domain semantics;
- Product Contract scope or lifecycle;
- database or Alembic history;
- Python package/module identities;
- `AI_CORP_*` compatibility configuration names;
- runtime behavior, authorization, Organizational Authority, or external-action boundaries.

Internal namespace cleanup, if performed, is a separate refactoring change and must preserve compatibility explicitly.

## Verification required for closure

Closure requires:

1. GitHub CI PASS on the renamed repository;
2. GitHub-to-GitVerse mirror PASS after the destination variable is reconciled;
3. canonical Arvectum OS Product Contract locator reconciliation;
4. read-after-write verification that `arvectum/tender-agent` remains repository ID `1333401651` and preserves the pre-relocation history.
