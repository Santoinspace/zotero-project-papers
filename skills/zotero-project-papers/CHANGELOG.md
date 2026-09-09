# Changelog

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
