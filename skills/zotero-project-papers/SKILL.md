---
name: zotero-project-papers
description: Use Zotero as the canonical local paper library for coding and research projects. Trigger when the user asks to search, find, read, compare, cite, review, implement, or write from research papers, literature, related work, prior work, or evidence. Search the current project's Zotero collection first, then the whole local Zotero library, and use web search only when local Zotero is insufficient. Keep papers actually used by the project in its Zotero collection and expose their Zotero-managed PDFs through a configurable project reference directory.
compatibility: Requires Zotero 10+ running locally with local application communication enabled and Python 3.10+. Designed for coding agents that can run local Python scripts; web search is optional and supplied by the host agent.
metadata:
  version: "0.2.0"
---

# Zotero Project Papers

Use Zotero as the long-term source of truth and the current repository's configured reference directory as the agent-facing paper working set.

The user should not need to think about Zotero attachment keys, `storage/XXXXXXXX`, hard links, or API calls. Handle those through `scripts/zotero_papers.py`.

## Requirements

- Zotero 10+ is installed and running.
- Zotero Settings → Advanced → **Allow other applications on this computer to communicate with Zotero** is enabled.
- Python 3.10+ is available.
- The helper is stdlib-only.

Resolve `scripts/zotero_papers.py` relative to this `SKILL.md`, but **do not change the shell working directory to the skill directory**. Invoke the helper by absolute path while keeping the agent's current working directory at the user's project. If necessary pass `--project-root <path>` before the subcommand.

In examples below, `$ZPP` means:

```bash
python <this-skill-directory>/scripts/zotero_papers.py
```

When CC Switch manages the skill, do not self-install or edit the managed installed copy. Updates may replace local edits.

## Core invariant

Always use this order for literature-grounded work:

1. Current project Zotero collection / configured reference directory.
2. Whole local Zotero library.
3. Web search only if local Zotero is insufficient.
4. Import only papers actually selected for reading, citation, comparison, implementation, or design.
5. Once a paper is used by the project, put the Zotero item in the project collection and materialize its Zotero-managed PDF into the configured reference directory.
6. Read/analyze from the local project PDF when available.

Never write directly into `Zotero/storage` or `zotero.sqlite`. New PDFs must be imported through Zotero 10's Local API. Never delete a Zotero library item merely because a project file was removed.

## When to trigger

Use this skill for requests such as:

- find/search papers or references;
- literature review / related work / prior work / state of the art;
- read, summarize, compare, or critique papers;
- "has anyone done this before?";
- "design X based on actual papers/evidence";
- implement or reproduce a method from a paper;
- find BibTeX/citations for the current project;
- write a literature-grounded introduction, rationale, or related-work section.

Do not trigger for ordinary coding that does not benefit from research literature.

# Required preflight behavior

For a project-scoped literature task, use this lightweight sequence:

1. If the project is not initialized, run `doctor --json`, then initialize as described below.
2. Once initialized, run **one cheap consistency check before doing the literature task**:

```bash
$ZPP check --json
```

The check compares:

- Zotero project collection item keys;
- generated `papers.json` records;
- actual PDFs in the configured reference directory;
- whether formerly managed project PDFs are missing or have been manually replaced.

It does **not** parse or hash every PDF, so it is intended to be cheap enough to run at skill entry.

Interpret the result:

- `status=clean` → continue normally.
- `status=diverged` and `recommendation=continue-with-acknowledged-drift` → continue silently. The user already chose to tolerate this exact drift.
- `status=diverged` and `recommendation=ask-user` → tell the user briefly what changed and ask whether to **continue for now** or **unify with Zotero**.

If the user chooses **continue**, remember that exact drift fingerprint:

```bash
$ZPP check --ack-continue --json
```

Do not ask again while the drift fingerprint is unchanged. If files/collection membership change again, the fingerprint changes and the next check should ask once again.

If the user later says "统一 / sync it / reconcile / clean up the references", reset that remembered choice by running the reconciliation workflow below. A clean state automatically resets the policy back to normal prompting for future drift.

# First use and existing reference directories

Start from the user's project directory:

```bash
$ZPP doctor --json
```

If uninitialized, normally call:

```bash
$ZPP init --json
```

If the helper detects an existing likely reference directory, it returns `onboarding_required` with candidate directories. **Do not silently create another directory or flatten the user's existing tree.** Ask the user to choose one of these modes:

### A. Use the existing directory

Preserve the existing directory and its subdirectories as-is:

```bash
$ZPP init --onboarding use-existing --existing-dir references --json
```

Existing PDFs are treated as local-only until they are matched/imported into Zotero. Do not rearrange them automatically.

### B. Keep the existing directory separate

Leave the old directory untouched and create a new managed working set. The user may choose any relative name/path:

```bash
$ZPP init --onboarding separate --reference-dir papers/reference --json
```

For a user preference such as `literature/`:

```bash
$ZPP init --onboarding separate --reference-dir literature --json
```

### C. Merge PDFs into a new directory

If the user explicitly chooses merge, flatten only PDFs from the selected existing directory into the new directory:

```bash
$ZPP init --onboarding merge --existing-dir references --reference-dir papers/reference --json
```

Important merge semantics:

- recurse through existing subdirectories;
- put PDFs into one flat destination directory;
- preserve the original source directory;
- use hard links where possible, otherwise configured fallback;
- rename filename collisions safely;
- do not move notes, images, or non-PDF files;
- merged PDFs initially remain local-only until reconciled/imported into Zotero.

If more than one candidate directory exists, identify the intended source with `--existing-dir`.

## Changing the reference directory later

The reference directory is configurable and stored in `.zotero-project.json`; never assume it is `papers/reference` after initialization.

If the user asks to rename/move it:

```bash
$ZPP set-reference-dir literature --json
```

This rematerializes Zotero-managed PDFs into the new directory and removes only old helper-managed files. It preserves unrelated/user-managed files. If the target already contains PDFs, ask the user first; after explicit confirmation use:

```bash
$ZPP set-reference-dir literature --allow-existing-target --json
```

Use `--keep-old` only when the user explicitly wants old managed links/copies retained too.

# Search workflow

## A. Search the current project first

```bash
$ZPP search "QUERY" --scope project --json
```

Search output is brief by default. If metadata search returns zero results, the helper automatically retries Zotero's broader `everything` search so terms found only in abstracts/indexed PDF text can still match.

For details:

```bash
$ZPP show ITEM_KEY --json
```

Resolve local PDF paths:

```bash
$ZPP resolve ITEM_KEY --json
```

## B. Search the full local Zotero library

If project search is insufficient:

```bash
$ZPP search "QUERY" --scope library --json
```

For dataset names, acronyms, benchmark names, method nicknames, or queries likely to occur only in abstract/full text:

```bash
$ZPP search "QUERY" --scope library --fulltext --json
```

If a local paper becomes relevant to the project:

```bash
$ZPP add ITEM_KEY --json
```

Do not download it again.

## C. Web fallback

Only after local Zotero search is insufficient, use the host agent's web capability.

Do not import every candidate. Select papers first. Before downloading a selected paper, perform a final Zotero duplicate check by exact title and DOI/arXiv when available.

# Importing web-found or local-only PDFs

Build a UTF-8 metadata JSON containing at least a title, then:

```bash
$ZPP import-pdf /path/to/paper.pdf --metadata /tmp/paper.json --json
```

The helper checks DOI/title duplicates, imports through Zotero, creates a Zotero stored attachment, adds the item to the project collection, and materializes the canonical PDF into the configured project reference directory.

If the input PDF is already inside the configured reference directory, a successful import/reuse **absorbs that local-only file**: once Zotero owns a recoverable PDF, the local-only file is removed and recreated as the project view of the Zotero attachment. This is the preferred way to reconcile manually added PDFs.

# Manual changes and reconciliation

Users are allowed to manually add, delete, or replace files in the reference directory. Treat these as drift, not as an instruction to delete Zotero data.

Possible drift categories include:

- `localOnlyFiles`: user-added PDFs not represented by `papers.json`;
- `missingManagedFiles`: a Zotero/project paper whose project PDF was manually removed;
- `modifiedManagedFiles`: a formerly managed project path whose content was replaced;
- `zoteroOnlyItemKeys`: collection changed after the last manifest generation;
- `manifestOnlyItemKeys`: manifest still contains an item no longer in the collection.

Safety rules:

- deleting a project PDF does **not** mean delete the Zotero paper;
- manually replacing a managed PDF is never silently overwritten;
- `sync --prune` must skip user-modified formerly managed files;
- adding a local PDF does not automatically import it until the user/agent chooses to reconcile it.

## User asks to unify

Start with:

```bash
$ZPP reconcile --json
```

This treats the Zotero project collection as canonical for managed papers: it regenerates manifest/BibTeX, restores missing canonical project PDFs, and removes stale helper-managed files only when explicitly requested with `--prune`.

If `unresolvedLocalOnlyFiles` remain, do not delete them automatically. For each wanted local-only PDF:

1. identify/fetch reliable metadata;
2. run `import-pdf` on that exact file;
3. let the helper deduplicate against Zotero and absorb the local file into Zotero management.

If the user explicitly says local-only PDFs should be discarded instead of imported, the destructive option is:

```bash
$ZPP reconcile --remove-local-only --json
```

Never use `--remove-local-only` without explicit user intent.

When reconciliation reaches `clean`, the previous "continue despite drift" acknowledgement is cleared automatically.

# Reading papers

Prefer the project path from `resolve ITEM_KEY --json`. If direct PDF reading is available, read the actual PDF.

For fast text-only retrieval:

```bash
$ZPP fulltext ITEM_KEY --max-chars 50000
```

Use actual PDF layout when equations, tables, figures, or page fidelity matter.

MinerU remains optional; use it only if already available or explicitly added by the user.

# Project synchronization and citations

Normal refresh:

```bash
$ZPP sync --json
```

Explicit cleanup of stale helper-managed files:

```bash
$ZPP sync --prune --json
```

The project maintains:

- `<referenceDir>/papers.json`: agent-friendly project-paper manifest;
- `papers/references.bib` by default: Zotero-exported BibTeX.

Before literature-grounded writing, inspect the supporting paper text rather than relying on title/abstract alone. Surface duplicate DOI/title warnings and suspicious creator-role warnings instead of silently guessing citation metadata.

# Error handling

Prefer `--json` for agent calls. Errors remain machine-readable and do not emit long tracebacks unless `ZPP_DEBUG=1` is set.

If write authorization is needed:

```bash
$ZPP authorize --json
```

Tell the user to choose **Always Allow** in Zotero. The helper waits up to 300 seconds for the human authorization action.
