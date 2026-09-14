# Zotero Project Papers

**A Zotero-first paper workflow for Claude Code, Codex, and other coding agents — only when you explicitly ask for papers.**

Zotero Project Papers connects the research/coding project you are currently working on with your local Zotero library. When you explicitly ask an agent to find, compare, cite, or verify academic papers, it checks your project and Zotero before going to the web, reuses papers you already have, and archives the papers actually used by the project.

Zotero remains where **you** manage and read papers. Your project's reference directory is where **the agent** works with the papers relevant to that project.

```text
Zotero library                                  Current project

All of your papers                              code/
        │                                       experiments/
        │                                       manuscript/
        └──── project collection ─────────────► papers/reference/
                                                papers/references.bib
                                                papers/reference/papers.json

Evidence provenance

original paper ──► AI summary ──► Agent inference ──► project claim / manuscript
      ▲                                                    │
      └──────── citation audit / source traceability ──────┘
```

## Core behavior

This Skill is intentionally **not always-on**. It should activate only when you explicitly ask to search, use, cite, or verify academic literature.

Typical triggers:

> 请参考 2026 年的顶级会议论文，给我几个动机强、改动小、逻辑自洽的方向。

> 从我的 Zotero 里查找相关论文，本地没有再上网搜。

> Find recent ICLR/NeurIPS papers about this topic.

> 核验这些引用是否真的支持对应结论。

Ordinary technical questions should **not** activate the Skill just because papers might be useful.

## Requirements

- **Zotero 10+**
- **Python 3.10+**
- Zotero running when Zotero operations are needed
- Zotero → **Settings → Advanced → Allow other applications on this computer to communicate with Zotero**
- Claude Code, Codex, or another coding agent that can run local commands

## Install with CC Switch

CC Switch is the recommended installer and updater.

1. Open **CC Switch → Skills → Repository Management → Add Repository**.
2. Add repository **`Santoinspace/zotero-project-papers`**.
3. Use branch **`main`** and Skills path **`skills`**.
4. Refresh the Skills list.
5. Install **`zotero-project-papers`** and sync it to Claude Code/Codex.

### Upgrading

Updates are designed to be **in-place and backward compatible**:

- existing `.zotero-project.json` files are migrated automatically when needed;
- your configured reference directory, BibTeX path, and existing project binding are preserved;
- updates do not require you to manually move or delete papers;
- an upgrade never treats a missing local PDF as permission to delete the Zotero item;
- destructive cleanup still requires an explicit user action.

Do not keep permanent edits inside CC Switch's managed Skill directory; an update may replace the installed copy. Submit fixes upstream instead.

## Quick start

Open your research project and start your agent there:

```bash
cd my-research-project
claude
```

Then work normally. The Skill should remain inactive until you explicitly ask for papers.

Examples:

> 请参考 2026 年顶级会议论文，给我几个适合当前项目的创新方向。

> 从 Zotero 和当前 reference 里找关于这个问题的论文，本地不够再搜索网页。

> 根据本项目 reference 里的论文写 related work，并更新 BibTeX。

> 检查这段 related work 的引用是否真的支持对应结论，区分原文、AI 总结和推断。

The intended retrieval flow is:

```text
project papers.json
    ↓
local Zotero
    ↓
Zotero full text if needed
    ↓
web only for missing coverage
    ↓
papers actually used as evidence
    ↓
Zotero project collection + project reference directory
```

## Project setup and existing reference folders

The default reference directory is `papers/reference/`, but it is configurable. You can use `references/`, `literature/`, `papers/refs/`, or another project-relative path.

If the project already has a reference folder, the Skill should ask whether to:

- **use the existing directory** and preserve its subdirectories;
- **keep it separate** and create a new managed directory;
- **merge PDFs** into a new flat directory while preserving the original folder and safely renaming collisions.

Manual edits are allowed. If you add, delete, or replace PDFs yourself, the Skill compares the Zotero project collection, `papers.json`, and the actual reference directory. If you choose “continue for now”, the same mismatch is remembered and will not repeatedly interrupt you. You can later ask the agent to **reconcile / 统一** at any time.

Zotero remains the canonical source for managed papers; local-only and user-modified files are preserved unless you explicitly request deletion.

## Local-first should actually be fast

The search order is:

```text
1. Current-project papers.json
   ↓ local JSON, no Zotero call
2. Local Zotero metadata cache, if already available
   ↓ compact SQLite + incremental refresh
3. Direct Zotero Local API metadata search
   ↓ localhost
4. Zotero indexed full-text search
   ↓ bounded fallback
5. Web search
```

Search output is brief by default and limited to a small candidate set. The agent should request details only for promising papers instead of dumping many abstracts into context.

For users who frequently search a large Zotero library, the metadata cache can be warmed once:

```bash
python scripts/zotero_papers.py cache --refresh --json
```

The first search does **not** build a full-library cache automatically.

## Automatic evidence archiving

A search result is only a **candidate**. A paper becomes **project evidence** when the agent actually relies on it in the final analysis, design, comparison, implementation, or citation.

For an explicit literature task, the Skill should automatically:

- add Zotero-local evidence papers to the project's Zotero collection;
- expose Zotero-managed PDFs in the configured project reference directory;
- preserve useful metadata-only papers even when a PDF is not yet available;
- import web-found papers actually used as evidence when reliable metadata/PDFs are available;
- avoid importing the rest of the search candidates.

## Evidence provenance and citation audit

A real citation does not automatically mean the cited paper supports the sentence attached to it. Zotero Project Papers can keep a lightweight project evidence ledger in `papers/claims.json` and distinguish:

- **primary** — directly supported by the original paper;
- **summary** — an AI-generated summary or paraphrase;
- **inference** — an Agent synthesis across paper evidence;
- **hypothesis** — a new project idea, not an established literature claim.

The provenance flow is:

```text
paper / full text
      ↓
primary evidence
      ↓
AI summary
      ↓
Agent inference
      ↓
project claim / manuscript

citation audit ──► trace important claims back to the original source
```

`audit` checks **traceability**, not semantic truth. When you ask the Agent to verify citations or simulate peer review, it should still return to the relevant original page, section, table, or figure before claiming that a paper truly supports a statement.

## Core features

- Explicit activation: no surprise literature search during ordinary technical discussion.
- Zotero-first local search with project-manifest and optional metadata-cache fast paths.
- One Zotero collection per project with a configurable reference directory.
- Reuse Zotero-managed PDFs, using hard links when possible to avoid duplicate storage.
- Safe handling of existing folders and manual PDF changes.
- Metadata-only papers, DOI-based import/enrichment, and later PDF attachment.
- Automatic `papers.json` and project BibTeX generation.
- Claim provenance and lightweight citation/source audit.
- Compact machine-readable output designed for coding agents.
- No third-party Python dependencies.

## Zotero remains the source of truth

The Skill never writes directly to `zotero.sqlite` or fabricates folders inside `Zotero/storage`.

```text
Web/local PDF
      ↓
Zotero Local API
      ↓
Zotero-managed attachment
      ↓
project reference view
```

Deleting a project PDF never means “delete this paper from my Zotero library”.

## Typical project layout

```text
my-project/
├── .zotero-project.json
├── src/
├── experiments/
├── manuscript/
└── papers/
    ├── reference/
    │   ├── papers.json
    │   ├── paper-a.pdf
    │   └── paper-b.pdf
    ├── references.bib
    └── claims.json
```

Zotero gets a matching collection:

```text
Projects/
└── my-project/
```

## Useful commands

Normal users should rarely need to run these manually; agents invoke them automatically.

Base command:

```bash
python scripts/zotero_papers.py <command> [options]
```

| Command | Common options | What it does |
|---|---|---|
| `doctor` | `--json` | Diagnose Zotero, Local API, authorization, and project binding. |
| `check` | `--json`, `--full`, `--ack-continue` | Check project/Zotero/reference consistency. |
| `search "QUERY"` | `--scope project\|library`, `--fulltext`, `--json` | Search current project or the local Zotero library. |
| `show ITEM_KEY` | `--json` | Show detailed metadata for one candidate paper. |
| `cache` | `--refresh`, `--json` | Build or incrementally refresh the optional local metadata cache. |
| `add ITEM_KEY` | `--json` | Add an existing Zotero paper to the current project. |
| `sync` | `--json`, `--prune` | Refresh project PDFs, `papers.json`, and BibTeX. |
| `reconcile` | `--json` | Reconcile project state toward the Zotero project collection. |
| `import-doi DOI` | `--json` | Add/reuse a paper from DOI metadata even without a PDF. |
| `import-metadata FILE` | `--json` | Add/reuse a metadata-only paper from JSON. |
| `attach-pdf ITEM_KEY PDF` | `--json` | Attach a PDF later to an existing Zotero paper. |
| `evidence ITEM_KEY` | `--json` | Report whether original PDF/full text is available for a project paper. |
| `record-claim` | `--type`, `--paper`, `--claim`, source locator options | Record an important project claim with explicit provenance. |
| `audit` | `--json`, `--check-sources` | Audit claim provenance and citation traceability. |
| `enrich ITEM_KEY` | `--apply`, `--json` | Preview/apply DOI-based bibliographic metadata enrichment. |
| `fetch URL` | `--metadata FILE`, `--import`, `--json` | Validate a selected academic PDF URL and optionally import it. |

Use `python scripts/zotero_papers.py <command> --help` for the complete arguments.

## Feedback and issues

This project is still evolving through real research workflows. If you encounter a bug, unexpected Agent behavior, Zotero compatibility issue, or a workflow that feels unnecessarily slow or intrusive, please open an issue on GitHub.

When possible, include:

- Zotero version and operating system;
- the command/Agent request that triggered the issue;
- the compact JSON error or diagnostic output;
- a privacy-sanitized description of the project state.

Please avoid posting private paper libraries, unpublished manuscript content, local paths, tokens, or sensitive project data.
