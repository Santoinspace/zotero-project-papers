# Changelog

## 0.2.0

### Added

- Configurable project reference directory with `set-reference-dir`.
- Existing reference-directory detection during initialization.
- Three onboarding modes:
  - `use-existing` — preserve the existing directory/tree;
  - `separate` — leave it untouched and use a new directory;
  - `merge` — recursively collect PDFs into one flat new directory while preserving the source.
- `check` command comparing Zotero collection membership, generated manifest, and actual project PDFs.
- Drift fingerprints and `check --ack-continue`, so an acknowledged mismatch is not repeatedly shown until it changes.
- `check --reset-policy` to forget an acknowledged mismatch.
- `reconcile` command with safe Zotero-as-canonical project refresh.
- Explicit `reconcile --remove-local-only` for user-approved destructive cleanup only.
- Detection of manually replaced formerly managed PDFs (`modifiedManagedFiles`).
- Automatic absorption of a local-only reference PDF after successful Zotero import/reuse.
- Automatic migration of v0.1 `.zotero-project.json` files to schema v2.

### Changed

- `papers/reference` is now only the default, not a fixed project path.
- Skill entry now performs a cheap consistency check for initialized project-scoped literature tasks.
- A repeated identical drift can be silently tolerated after the user chooses “continue”.
- A new/changed drift fingerprint prompts again.
- `sync` preserves user-modified formerly managed files instead of overwriting them.
- `sync --prune` skips user-modified files.
- First-time initialization no longer silently ignores likely existing reference/literature directories.

### Safety

- Project-side deletion still never deletes a Zotero library item.
- Local-only PDFs are never removed by normal reconciliation.
- Merge preserves the source directory and touches PDFs only.

## 0.1.2

- Increased authorization wait to 300 seconds.
- Forced UTF-8-safe Windows output.
- Added structured JSON errors and `doctor`.
- Added metadata-search → full-text fallback.
- Reduced default search output and added `show`.
- Added duplicate DOI/title and creator-role warnings.
