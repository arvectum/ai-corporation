# ARV-096 — real EIS document-set acceptance

This runbook validates the repository fix against procurement `0388100001826000047` on the local Mac mini without modifying the accepted ARV-003 bundle or invoking an AI provider.

## Safety boundary

- Read-only access to EIS/getDocsIP and public procurement documents.
- Do not invoke LLM, VLM, OCR or the controlled R10.1 provider run.
- Do not mutate the accepted ARV-003 bundle.
- Do not mutate the production database.
- Do not submit bids, send messages, use an electronic signature or perform any other platform action.
- Store the new diagnostic run separately from the accepted bundle.

## Required repository state

Use the exact head of draft PR #97 and verify a clean worktree:

```bash
git fetch origin
git switch fix/arv096-complete-document-set
git merge --ff-only origin/fix/arv096-complete-document-set
git rev-parse HEAD
git status --short
```

## Acceptance sequence

1. Run the focused repository tests for document-set intake.
2. Request `getDocsByReestrNumber` for `0388100001826000047` with archive download enabled and automatic analysis disabled.
3. Preserve the raw getDocsIP archive separately and inspect its full entry tree, including nested archives.
4. Let the intake safely expand nested archives and supplement the run with public links discovered in notice XML and the public documents page.
5. Export two inventories:
   - physical files: original public name, source type, parent archive, content type, size and SHA-256;
   - logical documents: notice, technical specification/object description, contract draft, NMCK justification and other attachments.
6. Record every skipped, rejected, unavailable or failed download with a non-secret reason.
7. Evaluate `document_set_summary` before any analysis.

## Expected behavior

A corpus containing only notice XML records must produce:

```text
document_set_status=notice_only
status=docs_required
attachments_status=incomplete_document_set
manual_upload_required=true
analysis_status=not_started
```

A corpus is eligible for analysis only when the intake has a complete and safely extracted archive plus both:

- a technical specification or procurement-object description;
- a contract draft.

Any unresolved nested archive, archive safety rejection, missing required document type or failed public attachment download must keep analysis blocked.

## Evidence to retain locally

- exact repository HEAD;
- sanitized getDocsIP response metadata;
- archive SHA-256 and archive entry inventory;
- physical-file inventory and logical-document inventory;
- `document_set_summary`;
- focused-test output;
- provider call count (`0`);
- DB mutation count (`0`);
- accepted-bundle mutation count (`0`).

Do not publish source document contents, private paths, credentials, provider bodies or EIS tokens in GitHub.
