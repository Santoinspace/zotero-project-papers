---
name: zotero-project-papers
description: Use Zotero as the canonical local paper library for coding and research projects. Trigger when the user asks to search, find, read, compare, cite, review, implement, or write from research papers, literature, related work, prior work, or evidence. Search the current project's Zotero collection first, then the whole local Zotero library, and use web search only when local Zotero is insufficient. Keep papers actually used by the project in its Zotero collection and expose their Zotero-managed PDFs through the project's papers/reference directory.
compatibility: Requires Zotero 10+ running locally with local application communication enabled and Python 3.10+. Designed for coding agents that can run local Python scripts; web search is optional and supplied by the host agent.
metadata:
  version: "0.1.2"
---

# Zotero Project Papers

Use Zotero as the long-term source of truth and the current repository's `papers/reference/` directory as the agent-facing working set.

The user should not need to think about Zotero attachment keys, `storage/XXXXXXXX`, hard links, or API calls. Handle those through `scripts/zotero_papers.py`.

## Requirements

- Zotero 10+ is installed and running.
- Zotero Settings → Advanced → **Allow other applications on this computer to communicate with Zotero** is enabled.
- Python 3.10+ is available.
- The helper is stdlib-only; do not install Python packages for v0.1.x.

Resolve `scripts/zotero_papers.py` relative to this `SKILL.md`, but **do not change the shell working directory to the skill directory**. Invoke the helper by absolute path while keeping the agent's current working directory at the user's project. Project discovery is based on the current working directory (or explicit `--project-root`). Do not copy the helper into the user's repository.

In shell examples below, `$ZPP` means the helper invoked by absolute path from the current project directory:

```bash
ZPP="python <this-skill-directory>/scripts/zotero_papers.py"
```

When CC Switch manages the skill, do not self-install or copy it into Claude/Codex directories; let CC Switch handle distribution and updates.

## Core invariant

Always use this order for literature-grounded work:

1. Current project Zotero collection / `papers/reference`.
2. Whole local Zotero library.
3. Web search only if the local library is insufficient.
4. Import only papers that are actually selected for use, not every web-search candidate.
5. Once a paper is used by the project, put the Zotero item in the project collection and materialize its Zotero-managed stored PDF into `papers/reference/`.
6. Read/analyze from the local project PDF when available.

Never write files directly into `Zotero/storage`. New PDFs must be imported through Zotero 10's Local API so Zotero creates and owns the stored attachment.

Never delete a Zotero item merely because a project reference file is missing or removed.

## When to trigger

Use this skill for requests such as:

- find/search papers or references
- literature review / related work / prior work / state of the art
- read, summarize, compare, or critique papers
- "has anyone done this before?"
- "design X based on actual papers/evidence"
- implement or reproduce a method from a paper
- find BibTeX/citations for the current paper/repository
- write a literature-grounded introduction, methods rationale, or related-work section

Do not trigger for ordinary coding that does not benefit from research literature.

## First use in a repository

1. Run one preflight check from the user's project directory:

```bash
$ZPP doctor --json
```

Use `doctor` to check Zotero connectivity, likely Zotero executable location, local write authorization state, and the detected project root. If the detected project root is wrong, keep the shell cwd at the actual repository or pass `--project-root <path>` before the subcommand.

2. If the repo is not initialized, initialize it:

```bash
$ZPP init
```

This creates/uses a Zotero collection `Projects/<repo-name>`, creates `papers/reference/`, and writes `.zotero-project.json`.

3. If a write is needed and the helper reports that authorization is missing, run:

```bash
$ZPP authorize
```

Tell the user to choose **Always Allow** in Zotero. The helper waits up to 5 minutes for the human authorization dialog instead of timing out after 30 seconds. Persistent authorization is needed for multi-step stored-PDF imports. Do not request authorization for read-only work.

## Search workflow

### A. Search the current project first

```bash
$ZPP search "QUERY" --scope project --json
```

Search output is intentionally brief (key/title/creators/date/DOI/venue) to reduce agent token usage. If the metadata search returns zero items, the helper automatically retries Zotero's `everything` search so terms that occur only in abstracts or indexed PDFs can still be found. Use `--no-fulltext-fallback` only when this retry is undesirable.

If results are promising, fetch details for a specific paper rather than requesting large abstracts for every candidate:

```bash
$ZPP show ITEM_KEY --json
```

If the result is sufficient, use it. Resolve/read PDFs as needed:

```bash
$ZPP resolve ITEM_KEY --json
```

### B. Search the full local Zotero library

If project search is insufficient:

```bash
$ZPP search "QUERY" --scope library --json
```

The zero-result fallback is automatic. Use `--fulltext` immediately when the query is a dataset name, acronym, method nickname, benchmark name, or another term likely to appear only in abstracts/PDF text:

```bash
$ZPP search "QUERY" --scope library --fulltext --json
```

If metadata results are non-empty but clearly weak/irrelevant, also retry with `--fulltext`. If a locally available paper becomes relevant to the current project:

```bash
$ZPP add ITEM_KEY --json
```

This adds the existing Zotero item to the project collection, materializes its stored PDF into `papers/reference/`, and refreshes project metadata/BibTeX. Do not download it again.

### C. Web fallback

Only after local Zotero search is insufficient, use the host agent's native web-search/browser capability.

Prefer stable identifiers and authoritative sources in this order where practical:

1. DOI / publisher metadata
2. arXiv identifier/page
3. Semantic Scholar / Crossref / OpenAlex metadata
4. author/project page

Do not import all search results. First identify candidates, then import papers the user/agent will actually read, cite, compare, or use.

Before downloading a selected web paper, perform a final local duplicate check using its title and, when useful, DOI/arXiv text:

```bash
$ZPP search "EXACT PAPER TITLE" --scope library --json
$ZPP search "10.xxxx/xxxxx" --scope library --fulltext --json
```

If it already exists, use `$ZPP add ITEM_KEY` instead of downloading.

## Importing a web-found PDF into Zotero

Download the selected PDF to a temporary location outside the project reference directory. Build a small UTF-8 JSON metadata file, for example:

```json
{
  "itemType": "journalArticle",
  "title": "Example Paper",
  "creators": [
    {"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}
  ],
  "date": "2026",
  "DOI": "10.1234/example",
  "url": "https://example.org/paper",
  "abstractNote": "...",
  "publicationTitle": "Example Journal"
}
```

Then:

```bash
$ZPP import-pdf /tmp/paper.pdf --metadata /tmp/paper.json --json
```

The helper will:

- check for an obvious local duplicate by DOI/title;
- create the Zotero bibliographic item in the current project collection;
- create a Zotero `imported_file` child attachment;
- upload the PDF to Zotero via the local file-upload flow;
- let Zotero move it into `storage/<attachment-key>/...`;
- materialize the Zotero-owned PDF into `papers/reference/`;
- refresh `papers/reference/papers.json` and `papers/references.bib`.

If import fails after an item has already been created, report the partial result. Do not silently create a second item on retry; search Zotero first.

## Reading papers

Prefer the local path returned by:

```bash
$ZPP resolve ITEM_KEY --json
```

If the agent can inspect PDFs directly, read the PDF in `papers/reference/`.

For fast text-only retrieval, Zotero's indexed full text is available via:

```bash
$ZPP fulltext ITEM_KEY --max-chars 50000
```

Use indexed text for discovery and targeted evidence. Use the actual PDF when layout, equations, tables, figures, or page fidelity matters.

MinerU is intentionally not a v0.1 dependency. If a local MinerU workflow already exists, it may be used after resolving the project PDF, but do not require or configure it automatically.

## Project synchronization

When collection membership may have changed in Zotero, run:

```bash
$ZPP sync --json
```

This is additive/safe by default. It will not delete arbitrary files from the project.

If the user explicitly wants stale helper-managed project links removed:

```bash
$ZPP sync --prune --json
```

Prune only removes files previously recorded as helper-managed; it never deletes Zotero attachments.

## Writing and citations

`sync`/`add`/`import-pdf` maintain:

- `papers/reference/papers.json`: agent-friendly project-paper manifest
- `papers/references.bib`: Zotero-exported BibTeX for the project collection

Before writing a literature-grounded claim:

1. identify the supporting project paper(s);
2. inspect the relevant paper text/PDF rather than citing from title/abstract alone;
3. distinguish the paper's reported results from your inference;
4. use the generated BibTeX for citation metadata where suitable.

## Safety and library hygiene

- Zotero is canonical. The project directory is a derived working set.
- Never modify `zotero.sqlite` directly.
- Never copy/download directly into `Zotero/storage`.
- Never permanently delete library items through this skill.
- Avoid creating duplicate bibliographic items; search locally first. If search/import reports duplicate DOI/title warnings, reuse one existing item and tell the user the Zotero library contains duplicates rather than silently creating another.
- Zotero metadata is not assumed correct. If `show`, `papers.json`, or another result reports creator-role warnings (for example editors before authors), flag it before relying on generated BibTeX; do not silently rewrite bibliographic authors.
- Do not import weak web-search candidates just because they appeared in results.
- Do not overwrite unrelated files in `papers/reference/`.
- On same-volume filesystems, prefer hard links so the project and Zotero refer to one underlying PDF data stream.
- If hard linking is impossible, obey the project's configured fallback (`copy` by default in v0.1) and mention duplication only when it materially matters.

## Useful commands

```bash
$ZPP doctor --json
$ZPP status
$ZPP authorize
$ZPP init [--reference-dir papers/reference]
$ZPP search "query" --scope project|library [--fulltext] [--verbose] --json
$ZPP show ITEM_KEY --json
$ZPP resolve ITEM_KEY --json
$ZPP add ITEM_KEY --json
$ZPP import-pdf FILE.pdf --metadata metadata.json --json
$ZPP sync [--prune] --json
$ZPP fulltext ITEM_KEY [--max-chars N]
```

The helper keeps `--json` output machine-readable on failures and suppresses Python tracebacks by default. Follow the returned `error.message`/`error.hint`; use `ZPP_DEBUG=1` only when a developer traceback is actually needed. Do not bypass Zotero by manipulating its database or storage directory.
