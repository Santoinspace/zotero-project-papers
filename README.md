# Zotero Project Papers

**Make your Zotero library project-aware and agent-ready.**

Zotero Project Papers connects your local Zotero library with the research or coding project you are currently working on. Coding agents such as Claude Code and Codex can search Zotero before the web, reuse PDFs you already own, keep project-specific papers together, and import genuinely useful new papers back into Zotero without duplicating your library.

Zotero remains the place where **you** manage and read papers. Your project reference directory becomes the place where **the agent** can work with the papers relevant to that project.

```text
Zotero library                  Current project

All of your papers              code/
        │                        experiments/
        │                        manuscript/
        └──── project link ────► papers/reference/
                                 papers/references.bib
```

For each project, the skill creates or reuses a matching Zotero collection under:

```text
Projects/
└── <project-name>/
```

## What it does

When you ask an agent to find, read, compare, cite, or design something from real papers, the Skill follows this workflow:

```text
Current project papers
        ↓
Project Zotero collection
        ↓
Whole local Zotero library
        ↓
Web search only if needed
        ↓
Selected new papers → Zotero
        ↓
Project reference directory
```

This means a paper that already exists in Zotero should normally never be downloaded a second time.

## Features

- Zotero-first paper search for coding agents.
- One Zotero collection per project, e.g. `Projects/MyResearchProject`.
- Configurable project reference directory — `papers/reference`, `references`, `literature`, or another relative path you prefer.
- Detects existing reference/literature folders during first setup instead of silently creating a competing directory.
- Can keep an existing directory, create a separate managed directory, or flatten existing PDFs into a new directory when you explicitly choose to merge.
- Lightweight consistency check between Zotero, `papers.json`, and actual project PDFs.
- Remembers when you choose to continue with a known mismatch, so the agent does not ask on every invocation.
- Notices new drift later and asks again only when something actually changed.
- Safe reconciliation when files were manually added, deleted, or replaced.
- Hard links are preferred when Zotero and the project are on the same filesystem, avoiding duplicate PDF storage.
- Imports selected PDFs through Zotero 10 instead of writing into `Zotero/storage` directly.
- Generates an agent-friendly `papers.json` and project BibTeX.
- Windows-safe UTF-8 CLI output and compact JSON errors.
- No third-party Python dependencies.

## Requirements

- **Zotero 10+**
- **Python 3.10+**
- Zotero must be running while the Skill is used.
- In Zotero: **Settings → Advanced → Allow other applications on this computer to communicate with Zotero**.
- A coding agent capable of running local commands, such as Claude Code or Codex.

## Install with CC Switch

CC Switch is the recommended installer and updater.

1. Open **CC Switch → Skills → Repository Management → Add Repository**.
   GitHub repository: `Santoinspace/zotero-project-papers`
2. Add this GitHub repository.
3. Use branch `main` and Skills path `skills`.
4. Refresh the Skills list.
5. Install **`zotero-project-papers`** and enable/sync it to Claude Code, Codex, or another supported agent.

After installation, let CC Switch manage updates. Do not keep permanent manual edits inside CC Switch's managed Skill directory because an update can replace them.

## Quick start

Open your research project and start your agent there:

```bash
cd my-research-project
claude
```

Then simply ask something like:

> Find the most relevant papers about long-term memory for LLM agents. Check my Zotero library first, then use real papers to propose an experiment plan for this project.

You normally do **not** need to run the helper commands yourself. The Skill handles them.

## First use: your reference folder is your choice

The default suggested working directory is:

```text
papers/reference/
```

but it is not mandatory. You can use:

```text
references/
literature/
papers/refs/
```

or another project-relative path.

If the project already contains a likely reference directory, the Skill will not silently rearrange it. The agent should ask what you want to do.

### Option 1 — use the existing directory

Keep your current directory and subdirectory structure as-is.

For example:

```text
references/
├── surveys/
│   └── survey.pdf
└── methods/
    └── method.pdf
```

The Skill can use `references/` as the project working directory without flattening it automatically.

### Option 2 — keep the old directory and create a new one

Your existing directory is left untouched and Zotero Project Papers creates a separate working directory.

### Option 3 — merge PDFs into a new directory

If you explicitly choose merge, PDFs are discovered recursively and placed into one flat destination directory:

```text
old references/
├── surveys/a.pdf
└── methods/b.pdf

        ↓ merge

papers/reference/
├── a.pdf
└── b.pdf
```

Only PDFs are merged. The original directory is preserved, non-PDF files are not moved, and filename collisions are renamed safely.

## What happens when you manually edit the reference directory?

That is allowed.

You may manually:

- add a PDF;
- delete a PDF;
- replace a PDF;
- change the corresponding Zotero project collection.

Before a project-scoped literature task, the Skill performs a cheap consistency check between:

```text
Zotero project collection
        ↕
papers.json
        ↕
actual reference PDFs
```

If everything matches, nothing is shown.

If something changed, the agent can tell you, for example:

```text
Reference workspace changed:
+ 2 local-only PDFs
- 1 Zotero-managed PDF is missing locally

Continue for now, or unify it with Zotero?
```

### If you choose “continue”

The Skill remembers the exact current mismatch and continues working. It will not keep asking about the same state.

If you later add/delete/change something else, the mismatch fingerprint changes and the agent can ask once again.

### If you ask to “unify” later

The Skill reconciles the project toward the Zotero project collection:

- missing Zotero-managed project PDFs are restored;
- stale generated metadata is refreshed;
- manually added PDFs are reported rather than silently deleted;
- wanted local-only PDFs can be imported into Zotero;
- a manually replaced managed PDF is preserved instead of being overwritten.

Once the project is clean again, the remembered “continue despite mismatch” state is reset automatically.

## Zotero remains the source of truth

The project directory is a working view, not a second paper library.

The Skill never treats deleting a project PDF as permission to delete the Zotero item. It never writes directly to `zotero.sqlite` or fabricates folders inside `Zotero/storage`.

For new papers:

```text
Web / local PDF
      ↓
Zotero Local API
      ↓
Zotero-managed stored attachment
      ↓
Project reference view
```

## Avoiding duplicate PDF storage

When possible, the project PDF is a **hard link** to the Zotero-managed PDF. To you, VS Code, Claude Code, and Codex it behaves like a normal PDF, while the filesystem stores one underlying file.

Hard links require Zotero and the project directory to be on the same filesystem/volume. If that is not possible, the default fallback is a normal copy; symbolic-link fallback can also be configured.

## Project files

A typical initialized project may look like:

```text
my-research-project/
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

If you chose another reference directory, that path is recorded in `.zotero-project.json` and the Skill uses it instead of assuming `papers/reference`.

Zotero gets a matching collection such as:

```text
Projects/
└── my-research-project/
```

## Example prompts

> Find papers about KV-cache compression. Search my Zotero library before the web.

> Read the FlashAttention paper and compare it with the attention implementation in this repository.

> Look for real papers related to this idea and propose a benchmark grounded in their methods and limitations.

> I manually added a few PDFs to the references folder. Help me unify them with Zotero.

> Rename this project's paper directory to `literature` and keep the Zotero collection in sync.

> Use the papers relevant to this project to help structure the related-work section and update the BibTeX references.

## Local search behavior

Searches return compact metadata by default to save agent context. If title/author/year search returns no result, the helper automatically retries Zotero's broader `everything` search, allowing dataset names, acronyms, benchmark names, or terms present only in abstracts/indexed PDF text to match.

The Skill can then request full metadata or the actual PDF only for promising candidates.

## Safety behavior

- No direct writes to `Zotero/storage`.
- No direct writes to `zotero.sqlite`.
- No automatic deletion of Zotero library items.
- Manually modified project PDFs are not silently overwritten.
- `sync --prune` skips a formerly managed file if its content appears to have been replaced by the user.
- Local-only PDFs are not deleted during reconciliation unless the user explicitly asks for that destructive action.
- Duplicate DOI/title and suspicious creator-role metadata are surfaced as warnings.

## Troubleshooting

### Zotero cannot be reached

Make sure Zotero 10+ is running and local application communication is enabled.

### Zotero asks for permission

On the first write operation, Zotero may show an authorization dialog. Choose **Always Allow** for the smoothest experience. The helper waits up to five minutes for the human action.

### The wrong project root is detected

The agent should keep its shell working directory inside the intended repository. It can also pass an explicit `--project-root` to the helper.

### The project and Zotero disagree

Ask the agent:

> Check my project papers and tell me what is out of sync.

or:

> Unify this project's reference directory with Zotero.

## Current scope

Zotero Project Papers deliberately stays small:

- Zotero manages the library and human reading experience.
- The host agent provides web search.
- The Skill provides Zotero-first lookup, project membership, local paper views, consistency checks, reconciliation, and import orchestration.

MinerU and other advanced PDF parsing backends remain optional rather than mandatory dependencies.

## Version

Current release: **v0.2.0**
