# Changelog

## 0.1.2

- Increased interactive Zotero authorization timeout to 300 seconds and added a clear waiting prompt.
- Forced UTF-8-safe stdout/stderr handling on Windows to prevent GBK `UnicodeEncodeError` crashes.
- Made CLI failures compact and structured; `--json` remains valid JSON and tracebacks require `ZPP_DEBUG=1`.
- Added automatic `everything`/full-text fallback when metadata search returns zero results.
- Made search results brief by default and added `--verbose` plus `show ITEM_KEY` for on-demand details.
- Added `doctor` preflight for Local API, authorization state, project root, and best-effort Zotero executable discovery.
- Added explicit `--project-root` support and corrected Skill instructions so the helper is invoked by absolute path without changing cwd to the Skill directory.
- Added duplicate DOI/title warnings and creator-role metadata warnings to reduce silent library-quality problems.

## 0.1.1

- Repackaged as a CC Switch-friendly GitHub skill repository.
- Moved the installable skill to `skills/zotero-project-papers/`.
- Removed the self-copy installer from the managed skill; CC Switch is now the intended installer and SSOT manager.
- Added Agent Skills `compatibility` and version metadata.
- Updated helper version to `0.1.1`.

## 0.1.0

- Initial Zotero-first project-papers prototype.
