# Changelog

## 0.6.0

### Citation provenance

- Added `papers/citations.json` as a separate raw citation/BibTeX provenance ledger.
- Added `citation-resolve ITEM_KEY` for DOI content negotiation or a direct HTTPS BibTeX endpoint.
- Added `citation-record ITEM_KEY FILE` for BibTeX obtained through publisher/proceedings/DBLP/browser workflows.
- Added `citation-audit` to report provenance coverage without pretending raw BibTeX is automatically correct.
- Citation sources are ranked conservatively: publisher/proceedings > bibliographic database > DOI registry > review/preprint/user-provided.
- Existing `papers/references.bib` remains Zotero-generated; raw source entries never silently replace project citation keys.
- Citation provenance is captured only after a paper becomes project evidence, not for every search candidate.

### Compatibility

- Project schema upgrades automatically from v5 to v6 and preserves all existing reference/BibTeX/claim paths and collection bindings.
- Existing commands keep their previous semantics and do not gain hidden network requests.
- Updates do not move or delete existing user files.

### Validation

- Added regression tests for raw BibTeX preservation, source ranking, missing provenance audit, and v5→v6 automatic migration.

## 0.5.2

- Fixed false-zero whole-library searches by querying Zotero top-level bibliographic items (`/items/top`) instead of limiting `/items` and filtering child attachments afterward.
- Added a bounded full-text compatibility fallback that promotes matching child attachments/notes to their parent bibliographic items.
- Added regression coverage for the attachment-limit false-zero bug.
- Clarified zero-result semantics: a conceptual search miss is not proof that Zotero contains no relevant paper.
- Added structured user-action hints for Zotero-unreachable, Local API disabled, and write-authorization states.
- Updated Skill behavior and README so agents ask users to open Zotero only when needed and warn immediately before the first Zotero write-authorization dialog.
- No schema change and no manual migration required for existing projects.

## 0.5.1 — Backward-Compatible Upgrades & User-Facing README

### Upgrade compatibility

- Formalized an in-place upgrade contract: existing projects must not require manual deletion, directory rebuilding, or bulk file moves after normal Skill updates.
- Added regression tests across project config schemas v1–v5 to ensure custom reference/BibTeX/claims paths, Zotero collection bindings, fallback policy, and acknowledged drift state are preserved.
- Added a regression test proving that loading/migrating an older project config does not move or delete existing reference PDFs or create a new default reference directory.
- Added a CLI compatibility test covering all commands published through v0.5.0.
- Kept persisted config schema at v5 because this patch release does not change the on-disk config shape.
- Added a Skill-level rule that upgrades themselves must not trigger Zotero writes, bulk syncs, cache warmups, file moves, pruning, or destructive cleanup.

### README

- Reworked the README around the user workflow rather than implementation details.
- Preserved the CC Switch repository path `Santoinspace/zotero-project-papers`.
- Added a combined Zotero/project workflow diagram and evidence-provenance diagram.
- Shortened activation and feature explanations.
- Consolidated first-use/reference-directory/manual-edit guidance.
- Added practical prompt examples and a compact command table.
- Added an explicit upgrade section and a GitHub issue/feedback section with privacy guidance.

## 0.5.0 — Evidence Provenance & Citation Audit

### Claim-level provenance

- Added `papers/claims.json`, a lightweight project claim ledger separate from `papers.json` and BibTeX.
- Added four claim types: `primary`, `summary`, `inference`, and `hypothesis`.
- Added explicit source layers: `original-paper`, `ai-summary`, `agent-inference`, and `metadata`.
- Added `record-claim` with optional page/section/table/figure locators and duplicate suppression.
- Direct `primary` claims require an original-paper source layer and warn when no reproducible source locator is provided.
- Claim recording is local-only and refuses dangling paper keys that are not in the current project manifest.

### Citation/source audit

- Added `evidence ITEM_KEY` to report metadata-only vs PDF vs Zotero-indexed full-text availability for a paper.
- Added `audit` for cheap local provenance/traceability checks without contacting Zotero.
- Added opt-in `audit --check-sources` to verify referenced Zotero items/PDF/full-text availability.
- Audits distinguish original-paper claims, AI-summary-derived claims, Agent inferences, hypotheses, metadata-only claims, missing locators, missing original PDFs, and broken project-paper references.
- Audit output explicitly states that semantic verification was **not** performed; a real citation/PDF is not treated as proof that the paper supports the claim.

### Activation and review behavior

- Citation verification is now a narrow explicit activation trigger: the Skill may activate when the user explicitly asks whether citations/papers support claims.
- Generic peer review/editing requests still do not activate the Skill unless citation/evidence verification is explicitly requested.
- Added privacy-sanitized activation regression cases for citation audit vs generic peer review.

### Privacy and compatibility

- Claim ledgers do not store the user's absolute project path.
- Project config schema upgraded to v5 with a configurable `claimsFile`; older configs migrate automatically.
- Retains v0.4's case-insensitive Zotero headers, layered diagnostics, metadata-only import, exact-first matching, compact JSON, Crossref enrichment, and conservative PDF fetch behavior.

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
