---
name: zotero-project-papers
description: Use only when the user explicitly asks to find, search, survey, retrieve, or reference academic papers/literature; explicitly asks for top-conference/top-journal papers; explicitly asks to use Zotero or the project's reference papers; or explicitly asks for an answer grounded in actual papers. Do not activate merely because a technical question could benefit from citations, because the user asks whether something "has been studied", or for ordinary design/coding discussion without an explicit literature-retrieval request. When activated, search the current project and local Zotero first, use web only as needed, and archive papers actually used as evidence into Zotero and the project reference set.
compatibility: Requires Zotero 10+ running locally with local application communication enabled and Python 3.10+. Designed for coding agents that can run local Python scripts; web search is supplied by the host agent.
metadata:
  version: "0.3.0"
---

# Zotero Project Papers

Zotero is the long-term paper library. The current project's configurable reference directory is the agent-facing working set.

The user should not need to think about Zotero attachment keys, `storage/XXXXXXXX`, hard links, Local API details, or cache files. Handle those through `scripts/zotero_papers.py`.

## Activation policy — intentionally narrow

**Do not run any helper command merely because research literature might be useful.** This skill is deliberately opt-in by user intent.

Activate only when the user explicitly requests at least one of the following:

- find/search/retrieve papers or references;
- survey/review academic literature or related work;
- reference papers from a named year, venue tier, conference, or journal;
- use the user's Zotero library or the current project's reference papers;
- answer/design/compare **based on actual papers**, **based on top-conference papers**, or equivalent explicit evidence-grounding language;
- read or cite a specifically identified paper.

Typical positive triggers:

- “请查找 2026 年顶级会议论文，再给我几个方案。”
- “从 Zotero 里找和这个方法有关的论文。”
- “根据本项目 reference 里的论文写 related work。”
- “Find recent NeurIPS/ICLR papers on this topic.”
- “Use actual papers as evidence for the design.”

Do **not** activate for requests such as:

- “这个思路从理论上成立吗？”
- “这个模块应该怎么训练？”
- “这种拒绝预测机制会有什么影响？”
- “这个方案有什么问题？”
- “这个方向有人研究吗？” when the user did not explicitly ask to search/retrieve literature.

For mixed conversations, activate **only at the turn where the user explicitly switches to literature retrieval**. Earlier technical discussion does not retroactively justify running this skill.

A privacy-sanitized activation regression set lives in `tests/activation_cases.json`.

## Performance contract

Local-first is useful only if it is faster and cheaper than web search. Follow this order and stop as soon as enough evidence is found:

1. Current project `papers.json` manifest — local JSON, no Zotero call.
2. Local Zotero metadata cache if it already exists — compact SQLite, incrementally refreshed with Zotero local versions.
3. Direct Zotero Local API metadata search — no network.
4. Zotero indexed `everything`/full-text search — bounded fallback.
5. Host agent web search — only for missing or insufficient literature.

Search results are brief by default. Do not request abstracts/full metadata for every candidate. Default result limit is 10; normally inspect details for only the top 3–5 promising papers.

Never dump many abstracts or full texts into context just to rank candidates. Use:

```bash
$ZPP show ITEM_KEY --json
```

only after selecting a likely candidate, and read the PDF/full text only for papers actually needed.

## Requirements

- Zotero 10+ is installed and running.
- Zotero Settings → Advanced → **Allow other applications on this computer to communicate with Zotero** is enabled.
- Python 3.10+ is available.
- The helper is stdlib-only.
- Import does not require `GET /items/new`.

Resolve `scripts/zotero_papers.py` relative to this `SKILL.md`, but **keep the shell working directory at the user's project**. Invoke the helper by absolute path. If necessary pass `--project-root <path>` before the subcommand.

In examples below, `$ZPP` means:

```bash
python <this-skill-directory>/scripts/zotero_papers.py
```

When CC Switch manages the skill, never self-install or edit the managed installed copy.

# Fast preflight

Only after the activation policy is satisfied, perform one consistency preflight:

```bash
$ZPP check --json
```

In v0.3 this is normally a **quick marker check**. If the project files and cached Zotero library version are unchanged since the last full check, it returns without contacting Zotero.

Interpret:

- `status=clean` → continue.
- `status=diverged`, `recommendation=continue-with-acknowledged-drift` → continue silently.
- `status=diverged`, `recommendation=ask-user` → briefly ask whether to continue for now or unify with Zotero.

If the user chooses continue:

```bash
$ZPP check --ack-continue --json
```

Do not ask again while the drift fingerprint is unchanged.

Use a forced full comparison only when needed:

```bash
$ZPP check --full --json
```

Do not run `doctor`, `status`, `cache --refresh`, or a full consistency scan opportunistically on unrelated turns.

# Search workflow

## 1. Search current project first

```bash
$ZPP search "QUERY" --scope project --json
```

If `source=project-manifest`, the result came from local `papers.json` and Zotero was not contacted.

If enough relevant papers are already in the project, stop searching and use them.

## 2. Search whole local Zotero library

If project results are insufficient:

```bash
$ZPP search "QUERY" --scope library --json
```

Possible sources:

- `zotero-metadata-cache`: compact local cache;
- `zotero-direct-metadata`: direct localhost metadata search;
- `zotero-fulltext`: Zotero indexed full-text fallback.

The response includes `latencyMs`, `zoteroContacted`, and `webNeeded` so agent behavior can be audited.

For a term known to occur mainly inside PDFs:

```bash
$ZPP search "QUERY" --scope library --fulltext --json
```

Keep `--limit` small. Do not use `--verbose` unless metadata details are actually needed.

### Optional cache warmup

The first search deliberately does **not** download the whole Zotero library just to build a cache. If the user wants faster repeated broad searches, explicitly warm it once:

```bash
$ZPP cache --refresh --json
```

Subsequent refreshes use Zotero's local object versions and `?since=<version>` to fetch only changes. Cache partitions are isolated by `Zotero-Server-ID`.

Do not warm the cache automatically merely because the skill triggered.

## 3. Web fallback

Use host-agent web search only when local results are absent or insufficient for the explicit request.

For requests such as “参考 2026 年顶会论文”, local hits may be useful but web search can still be necessary to verify coverage/freshness. Do not skip web merely because one older local paper matched.

Before downloading a selected web paper, perform a final local Zotero duplicate check by title/DOI/arXiv when practical.

# Evidence-set persistence — automatic after explicit literature retrieval

A search candidate is not automatically library material.

A paper becomes **project evidence** when the agent actually relies on it in the final analysis, comparison, design, implementation, or citation.

Before finalizing an explicitly literature-grounded answer:

- for a paper already in Zotero, automatically run:

```bash
$ZPP add ITEM_KEY --json
```

- for a selected web paper that is actually used as evidence, download a legitimate/reliable PDF when available, build metadata JSON, and run:

```bash
$ZPP import-pdf /path/to/paper.pdf --metadata /tmp/paper.json --json
```

This should happen automatically after the user explicitly asked for literature retrieval; do not make the user separately say “add these papers to Zotero”.

Do **not** import every search result. Persist only papers actually used as evidence or explicitly requested by the user.

If a selected paper cannot be legally/reliably downloaded, do not block the answer; report that it could not be archived automatically.

# First use and project binding

From the user's project directory:

```bash
$ZPP doctor --json
```

If uninitialized:

```bash
$ZPP init --json
```

If an existing reference/literature directory is detected, do not silently create another directory. Ask the user to choose:

### Use the existing directory

```bash
$ZPP init --onboarding use-existing --existing-dir references --json
```

Preserve its subdirectories. Existing PDFs remain local-only until reconciled/imported.

### Keep it separate

```bash
$ZPP init --onboarding separate --reference-dir papers/reference --json
```

The user may choose another relative path such as `literature/`.

### Merge PDFs into a new flat directory

```bash
$ZPP init --onboarding merge --existing-dir references --reference-dir papers/reference --json
```

Merge semantics:

- recurse through existing subdirectories;
- flatten PDFs only;
- preserve the original source directory;
- safely rename filename collisions;
- do not move notes/images/non-PDF files;
- merged PDFs remain local-only until reconciled/imported.

# Changing the reference directory

```bash
$ZPP set-reference-dir literature --json
```

If the target already contains PDFs, ask the user before using:

```bash
$ZPP set-reference-dir literature --allow-existing-target --json
```

# Manual changes and reconciliation

Users may manually add, delete, or replace files in the reference directory. Treat this as drift, not as permission to delete Zotero data.

Drift categories include:

- `localOnlyFiles`;
- `missingManagedFiles`;
- `modifiedManagedFiles`;
- `zoteroOnlyItemKeys`;
- `manifestOnlyItemKeys`.

Safety rules:

- deleting a project PDF never deletes the Zotero library item;
- manually replacing a managed PDF is never silently overwritten;
- `sync --prune` skips user-modified files;
- local-only PDFs are not automatically deleted.

If the user asks to unify:

```bash
$ZPP reconcile --json
```

For each wanted local-only PDF, identify reliable metadata and use `import-pdf` so Zotero becomes canonical.

Only if the user explicitly wants local-only PDFs discarded:

```bash
$ZPP reconcile --remove-local-only --json
```

# Reading and details

Resolve a selected paper:

```bash
$ZPP resolve ITEM_KEY --json
```

Prefer the project PDF path. For fast indexed text:

```bash
$ZPP fulltext ITEM_KEY --max-chars 50000
```

Use the actual PDF when equations, figures, tables, or page layout matter.

MinerU is optional and should not be invoked just because this skill activated.

# Citations and synchronization

Normal refresh:

```bash
$ZPP sync --json
```

Project outputs:

- `<referenceDir>/papers.json` — compact agent manifest;
- `papers/references.bib` by default — Zotero-exported BibTeX.

Before literature-grounded writing, inspect supporting paper text rather than relying only on titles/abstracts. Surface duplicate DOI/title warnings and suspicious creator-role warnings instead of silently guessing.

# Error handling

Prefer `--json` for agent calls. Errors stay machine-readable and do not emit tracebacks unless `ZPP_DEBUG=1`.

If write authorization is needed:

```bash
$ZPP authorize --json
```

Tell the user to choose **Always Allow** in Zotero. The helper waits up to 300 seconds.
