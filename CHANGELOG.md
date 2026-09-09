# Changelog

## 0.4.0 — Robust Local API, Metadata-Only Evidence, Better Matching

### P0 compatibility and diagnosis

- Fixed Zotero response-header handling by normalizing HTTP header names case-insensitively (`Zotero-Server-Id` and `Zotero-Server-ID` are equivalent).
- Reworked `doctor --json` into layered diagnostics for unreachable Zotero, disabled Local API, missing/unsupported server identity, write authorization, auth-store writability, project binding, and project collection presence.
- Added optional `/connector/ping` diagnostics for version/service identification without changing Zotero settings.
- Explicitly prevents uncertain diagnostics from escalating to GUI/computer-use changes.
- Added `ZPP_AUTH_STORE` for sandbox-friendly write-key persistence. Authorization persistence is preflighted before opening Zotero's approval dialog.
- Authorization results now distinguish `authorizationGranted`, `remembered`, and `tokenPersisted`.

### Metadata-only evidence and PDF attachment

- Added `import-metadata` so a paper can enter Zotero/current project without a PDF.
- Added `attach-pdf ITEM_KEY paper.pdf` to attach a stored PDF later.
- Project manifest records `pdfStatus=available|missing`, separating the evidence set from the local PDF set.
- Added `import-doi` and `enrich` using Crossref metadata. Existing-item type conflicts remain conservative and are never silently rewritten.

### Search and deduplication quality

- Search results now expose `matchType`, `matchedFields`, and normalized `score`.
- Matching priority is DOI exact → arXiv exact → normalized title exact → title phrase → title token / author+year → venue/tags → abstract/full-text fallback.
- Weak abstract-only matches no longer short-circuit more reliable local/full-text/web lookup.
- Duplicate detection now follows DOI → arXiv ID → normalized title → strict title+first-author+year.
- Title normalization treats punctuation/hyphens as boundaries for more stable exact matching.
- Metadata cache now stores arXiv ID/extra fields and migrates existing SQLite caches in place.

### Fetch and metadata quality

- Added conservative `fetch` for HTTPS academic PDF URLs with redirect checks, PDF header/EOF/size validation, SHA-256, and host classification.
- The helper does not claim to verify copyright/access rights and refuses unfamiliar hosts by default unless explicitly overridden after verification.
- New DOI enrichment reduces hand-written venue/DOI/date metadata.

### Agent-token efficiency and project UX

- `sync --json` is compact by default; use `--include-papers` for the full paper array.
- `check --json` hides per-file quick-marker details unless `--verbose` is requested.
- `check` reports possible legacy reference directories such as `papers/references` when the active directory is different.
- Added optional `tag` and JSON-based `organize` commands for research-question tags and project child collections.
- Project config schema upgraded to v4; older configs migrate automatically.

## 0.3.0 — Activation & Fast Search

### Activation

- Narrowed the Skill trigger policy: it now activates only for explicit paper/literature retrieval, explicit Zotero/reference use, named top-conference/top-journal requests, or explicit requests to ground an answer in actual papers.
- Explicitly prevents activation for ordinary technical/design questions merely because citations could be useful.
- Added privacy-sanitized activation regression cases, including a multi-turn case where only the final explicit top-conference-paper request should trigger.

### Search performance

- Added a zero-API project fast path: project-scope search checks generated `papers.json` first and returns without contacting Zotero when it finds enough candidates.
- Reduced the default search limit from 20 to 10 and kept results brief by default.
- Added observable search diagnostics: `source`, `latencyMs`, `zoteroContacted`, and `webNeeded`.
- Added an optional compact SQLite metadata cache with title, creators, year/date, DOI, venue, tags, abstract text, collection membership, and URL.
- Metadata cache is partitioned by `Zotero-Server-ID`.
- Added incremental metadata-cache refresh using Zotero local object versions and `?since=<version>` plus deleted-item tracking.
- First search deliberately does not build a full-library cache. Cache warmup is explicit via `cache --refresh` so first-use latency does not become worse than direct local Zotero search.
- When no cache exists, search uses direct Zotero metadata first and bounded Zotero `everything`/full-text fallback before web.

### Preflight cost

- `check` now has a quick-marker fast path based on project PDF/manifest metadata plus cached Zotero library version.
- If nothing changed since the last full check, `check` reuses the previous state without contacting Zotero.
- Added `check --full` for explicit full consistency comparison.

### Evidence persistence

- Clarified the evidence-set rule: search candidates are not imported automatically.
- Papers actually used as evidence in an explicitly literature-grounded answer should automatically be added to the project Zotero collection; selected web evidence should be imported into Zotero when a reliable PDF is available.

### Compatibility

- Project config schema upgraded to v3 with `zoteroServerID`; v1/v2 configs are normalized automatically.
- Retains v0.2.x safe reference-directory onboarding, drift handling, metadata conflict reporting, and Zotero Local API template fallbacks.
