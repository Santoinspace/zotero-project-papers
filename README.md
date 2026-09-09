# Zotero Project Papers

**A Zotero-first paper workflow for Claude Code, Codex, and other coding agents — but only when you explicitly ask for papers.**

Zotero Project Papers connects the research/coding project you are currently working on with your local Zotero library. When you explicitly ask an agent to find or reference academic papers, it checks your project and Zotero before going to the web, reuses PDFs you already have, and archives the papers actually used by the project.

Zotero remains where **you** manage and read papers. Your project's reference directory is where **the agent** works with the papers relevant to that project.

```text
Zotero library                  Current project

All of your papers              code/
        │                        experiments/
        │                        manuscript/
        └──── project link ────► papers/reference/
                                 papers/references.bib
```

## The most important behavior in v0.3

This Skill is intentionally **not always-on**.

It should activate when you explicitly say things like:

> 请参考 2026 年的顶级会议论文，给我几个动机强、改动小、逻辑自洽的方向。

> 从我的 Zotero 里查找相关论文，本地没有再上网搜。

> Find recent ICLR/NeurIPS papers about this topic.

> 根据当前项目 reference 里的论文写 related work。

It should **not** activate just because a technical question could benefit from citations:

> 这个思路从理论上成立吗？

> 训练时这个模块应该怎么做？

> 这种拒绝预测机制有什么影响？

> 这个方向有人研究吗？

In a mixed conversation, the Skill should wait until the user explicitly switches to paper/literature retrieval.

## Why local-first should actually be fast

v0.4 uses this search order:

```text
1. Current-project papers.json
   ↓ local JSON, no Zotero call
2. Local Zotero metadata cache, if already warmed
   ↓ compact SQLite + incremental refresh
3. Direct Zotero Local API metadata search
   ↓ localhost
4. Zotero indexed full-text search
   ↓ bounded fallback
5. Web search
```

The CLI reports `source`, `latencyMs`, `zoteroContacted`, and `webNeeded` so the fast path is observable.

Search output is brief by default and limited to 10 candidates. The agent should fetch details only for promising papers instead of dumping many abstracts into context.

### Optional metadata-cache warmup

The first search **does not build a full-library cache automatically**. That could make first use slower than simply querying Zotero.

If you do many broad literature searches and want the fastest repeated local lookup, warm the cache once:

```bash
python scripts/zotero_papers.py cache --refresh --json
```

Later cache refreshes use Zotero's local object versions and `?since=<version>` to fetch only changes. Cache data is partitioned by `Zotero-Server-ID`.

## Automatic evidence archiving

A paper found during search is only a **candidate**.

A paper becomes **project evidence** when the agent actually relies on it in the final analysis, design, comparison, implementation, or citation.

For an explicit literature task, the Skill should automatically:

- add Zotero-local evidence papers to the project's Zotero collection;
- expose their Zotero-managed PDFs in the configured project reference directory;
- import web-found papers actually used as evidence into Zotero when a reliable PDF is available;
- avoid importing the rest of the search candidates.

So a top-conference search might inspect 20 candidates but archive only the 5 papers actually used in the answer.

## Features

- Narrow, explicit activation policy — no surprise literature searches during ordinary technical discussion.
- Project manifest fast path with no Zotero API call on a hit.
- Optional compact SQLite Zotero metadata cache.
- Incremental cache updates using Zotero local library versions.
- Direct Zotero metadata and indexed full-text fallback before web search.
- One Zotero collection per project, e.g. `Projects/MyResearchProject`.
- Configurable project reference directory (`papers/reference`, `references`, `literature`, etc.).
- Existing-reference-directory onboarding: use existing, keep separate, or explicitly flatten/merge PDFs.
- Cheap consistency preflight that can reuse previous state without contacting Zotero.
- Drift acknowledgement so the same mismatch does not repeatedly interrupt you.
- Safe reconciliation for manually added/deleted/replaced PDFs.
- Hard links when possible to avoid duplicate PDF storage.
- New PDFs imported through Zotero 10 rather than copied into `Zotero/storage` manually.
- `papers.json` and project BibTeX generation.
- Case-insensitive Zotero HTTP headers and layered `doctor` diagnostics.
- Sandbox-safe authorization preflight with optional `ZPP_AUTH_STORE`.
- Metadata-only evidence items (`import-metadata` / `import-doi`) and later `attach-pdf`.
- Match explanations (`matchType`, `matchedFields`, score) with DOI/arXiv/title-first ranking.
- Compact sync JSON by default; full paper arrays are opt-in.
- DOI-based Crossref enrichment and a conservative validated `fetch` helper.
- Optional Zotero tags and project child-collection organization.
- Compact machine-readable errors and Windows-safe UTF-8 output.
- No third-party Python dependencies.

## Requirements

- **Zotero 10+**
- **Python 3.10+**
- Zotero running while Zotero operations are needed
- Zotero → **Settings → Advanced → Allow other applications on this computer to communicate with Zotero**
- Claude Code, Codex, or another coding agent that can run local commands

## Install with CC Switch

CC Switch is the recommended installer/updater.

1. Open **CC Switch → Skills → Repository Management → Add Repository**. `Santoinspace/zotero-project-papers`
2. Add this GitHub repository.
3. Use branch `main` and Skills path `skills`.
4. Refresh the Skills list.
5. Install **`zotero-project-papers`** and sync it to Claude Code/Codex.

Do not keep permanent edits inside CC Switch's managed Skill directory; updates can replace them.

## Quick start

Open your research project and start your agent there:

```bash
cd my-research-project
claude
```

Then keep working normally. The Skill should remain inactive until you explicitly ask for papers.

For example:

> 我不认可现在的方案。请参考 2026 年顶级会议论文，给我几个适合当前项目的创新方向。

At that point the intended flow is:

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

## First use and reference directories

The default suggested directory is:

```text
papers/reference/
```

but it is configurable. You can use `references/`, `literature/`, `papers/refs/`, or another project-relative path.

If the project already contains a likely reference folder, the Skill should ask whether to:

1. **Use the existing directory** and preserve its subdirectories.
2. **Keep it separate** and create a new managed directory.
3. **Merge PDFs** into a new flat directory. Only PDFs are flattened; the original directory is preserved and filename collisions are renamed safely.

## Manual edits are allowed

You can manually add, remove, or replace PDFs in the reference directory.

The Skill tracks drift between:

```text
Zotero project collection
        ↕
papers.json
        ↕
actual project PDFs
```

If you choose “continue for now”, it remembers that exact mismatch and does not ask again until something changes. If you later ask the agent to “统一 / reconcile”, Zotero remains the canonical source for managed papers, while local-only/user-modified files are preserved unless you explicitly request deletion.

## Zotero remains the source of truth

The Skill never writes directly to `zotero.sqlite` or fabricates folders inside `Zotero/storage`.

For new PDFs:

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
    └── references.bib
```

Zotero gets a matching collection:

```text
Projects/
└── my-project/
```

## Useful manual/debug commands

Normal users should rarely need these; agents invoke them automatically.

```bash
# Fast consistency preflight
python scripts/zotero_papers.py check --json

# Force full consistency comparison
python scripts/zotero_papers.py check --full --json

# Search current project
python scripts/zotero_papers.py search "query" --scope project --json

# Search whole local Zotero library
python scripts/zotero_papers.py search "query" --scope library --json

# Optional one-time cache warmup
python scripts/zotero_papers.py cache --refresh --json
```

## Metadata-only papers and later PDF attachment

A project evidence set no longer requires every paper to have a downloadable PDF. If a paper is important but only reliable metadata is available, the agent can add it to Zotero/current project first:

```bash
python scripts/zotero_papers.py import-metadata paper.json --json
# or, when a DOI is known
python scripts/zotero_papers.py import-doi 10.xxxx/xxxx --json
```

`papers.json` marks it with `pdfStatus: "missing"`. A PDF can be attached later:

```bash
python scripts/zotero_papers.py attach-pdf ITEM_KEY paper.pdf --json
```

## Better diagnostics in sandboxed agents

`doctor --json` now distinguishes Zotero unreachable, Local API disabled, authorization needed, unsupported version, malformed/unexpected local responses, and an unwritable authorization store. It does not ask the agent to change GUI settings when the evidence is ambiguous.

If a sandbox cannot write the normal user config directory, choose a **private, gitignored** path:

```bash
ZPP_AUTH_STORE=/path/to/private/auth.json python scripts/zotero_papers.py authorize --json
```

## Validated paper fetch and DOI enrichment

The helper does not replace the host agent's web search. After the agent selects an actual evidence paper, it can use a known official/OA PDF URL:

```bash
python scripts/zotero_papers.py fetch https://.../paper.pdf --metadata paper.json --import --json
```

The fetch helper uses HTTPS, follows redirects, validates PDF structure at a lightweight stdlib level, computes SHA-256, and is conservative about unfamiliar hosts. It does **not** claim that access rights are automatically verified.

For DOI-bearing records, Crossref can fill common bibliographic fields:

```bash
python scripts/zotero_papers.py enrich ITEM_KEY --json
python scripts/zotero_papers.py enrich ITEM_KEY --apply --json
```

## Topic organization (optional)

When you explicitly want research-question views, the agent can add Zotero tags or create project child collections from a small JSON topic map. This is optional; the default project collection remains flat and simple.

## Privacy of activation regression tests

The repository includes a small activation regression fixture based on a real interaction pattern. It is **synthetic and privacy-sanitized**: it preserves only the conversational structure “technical discussion → explicit request for top-conference papers”. It contains no source project names, model names, dataset names, paper titles, URLs, paths, or experimental metrics.

## Project status

This is an early-stage open-source project being tested on real research workflows. Bug reports and workflow feedback are welcome, especially around Zotero Local API compatibility, paper deduplication, metadata quality, and agent activation behavior.
