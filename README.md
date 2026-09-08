# Zotero Project Papers

**Make your Zotero library project-aware and agent-ready.**

Zotero Project Papers connects your local Zotero library with the research or coding project you are currently working on.

When Claude Code, Codex, or another coding agent needs papers, the skill tells it to:

1. search the current project's Zotero collection first;
2. search your whole local Zotero library next;
3. use web search only when Zotero does not already have what is needed;
4. import only the papers that are actually useful;
5. keep those papers in Zotero while exposing them naturally inside the current project.

The goal is simple: **Zotero remains your paper library and reader; your project gets a clean `papers/reference/` working set for agents, code, experiments, and LaTeX writing — without unnecessary duplicate downloads.**

---

## Why use it?

A typical research project may contain code, experiments, notes, and a paper draft, while the papers you rely on live somewhere else inside Zotero.

That works well for people, but coding agents usually see only the current repository. They may search the web again for papers you already have, download duplicate PDFs, or miss the exact papers you have already collected.

Zotero Project Papers bridges that gap.

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

Papers used by the project remain Zotero-managed, while the project receives an agent-friendly reference view.

---

## What it can do

- Search the **current project's Zotero collection**.
- Search the **entire local Zotero library** before going to the web.
- Reuse PDFs that are already stored in Zotero instead of downloading them again.
- Create a Zotero collection that corresponds to the current repository.
- Add existing Zotero papers to the current project.
- Import selected web-downloaded PDFs into Zotero as normal stored attachments.
- Expose Zotero-managed PDFs under `papers/reference/`.
- Prefer hard links when possible so Zotero and the project can refer to the same underlying PDF data.
- Generate an agent-friendly `papers.json` manifest.
- Generate `papers/references.bib` for LaTeX and paper writing.
- Read Zotero's indexed full text when fast text retrieval is useful.

It does **not** replace Zotero, implement its own paper database, or write directly into `Zotero/storage`.

---

## Requirements

Before using the skill, install:

- **Zotero 10 or newer**
- **Python 3.10 or newer**
- a supported coding agent such as **Claude Code** or **Codex**
- **CC Switch** for recommended Skill installation and updates

In Zotero, enable:

**Settings → Advanced → Allow other applications on this computer to communicate with Zotero**

Keep Zotero running while the skill is being used.

No third-party Python packages are required for v0.1.1.

---

## Install with CC Switch

CC Switch is the recommended way to install, update, and synchronize this Skill across supported agents.

### 1. Add this repository

Open:

**CC Switch → Skills → Repository Management → Add Repository**

Add this GitHub repository and configure:

```
GitHub repository: Santoinspace/zotero-project-papers
```

```text
Branch:       main
Skills path:  skills
```

Then refresh the Skills list.

### 2. Install the Skill

Find:

```text
zotero-project-papers
```

Click **Install**, then enable or sync it to the agents you want to use, such as Claude Code or Codex.

After that, let CC Switch manage the installed copies and future updates.

> If the repository provides an `ccswitch://` install link, clicking it can add the Skill repository to CC Switch directly.

---

## Quick start

There is usually no need to call the helper commands yourself. The coding agent should invoke them through the Skill.

Open a research or coding project:

```bash
cd my-research-project
claude
```

or start Codex in the same directory.

Then ask naturally:

> Find papers about long-term memory for LLM agents. Check my Zotero library first, then use recent papers to propose an experiment plan for this project.

On first use, the Skill will:

1. check that Zotero is available;
2. initialize the current repository if needed;
3. create or reuse `Projects/<project-name>` in Zotero;
4. request Zotero write authorization only when a write operation is actually needed;
5. create the project's paper working set.

After initialization, the project will typically contain:

```text
my-research-project/
├── .zotero-project.json
├── src/
├── experiments/
├── manuscript/
└── papers/
    ├── reference/
    │   ├── papers.json
    │   └── *.pdf
    └── references.bib
```

---

## Example prompts

You do not need to mention the Skill explicitly. Requests involving research papers should trigger it naturally.

### Search before using the web

> Find the most relevant papers on KV-cache compression. Check my Zotero library before searching the web.

### Read a paper you may already have

> Read the FlashAttention paper and compare its optimization ideas with the implementation in this repository.

### Literature-grounded design

> Look for real papers related to this idea and propose a benchmark based on the methods and limitations reported in the literature.

### Related work

> Read the papers relevant to this project and draft the related-work structure for our paper.

### LaTeX writing

> Use this project's references to support the introduction and make sure the citations are available in `papers/references.bib`.

### Reproduction

> Find the original paper for this method, read the implementation details, and help me reproduce it in this repository.

---

## How paper lookup works

For literature-grounded tasks, the intended search order is:

```text
Current project papers
        ↓
Current project Zotero collection
        ↓
Whole local Zotero library
        ↓
Web search
```

This matters because a paper you already have should normally be reused rather than searched for and downloaded again.

When a useful paper already exists in Zotero, the Skill can add it to the current project's collection and make its PDF available under `papers/reference/`.

When a useful paper does **not** exist locally, the host agent performs web search. Only papers actually selected for reading, comparison, implementation, or citation should be imported into Zotero.

---

## How new PDFs are handled

New papers found on the web are **not** copied directly into Zotero's `storage` directory.

Instead, the Skill imports them through Zotero's local API:

```text
Web search
    ↓
Temporary PDF
    ↓
Zotero import
    ↓
Zotero-managed stored attachment
    ↓
papers/reference/
```

Zotero remains responsible for its own attachment keys, storage layout, and library records.

This keeps the Zotero library consistent and avoids creating unmanaged files inside Zotero's internal storage directory.

---

## Project references and duplicate storage

When Zotero and the project reference directory are on the same filesystem/volume, the Skill prefers a **hard link** for the project PDF.

To the user and the coding agent, this looks like a normal file:

```text
papers/reference/example-paper.pdf
```

but Zotero and the project can refer to the same underlying PDF data.

If a hard link cannot be created, the default fallback in v0.1.1 is a normal copy. A project can also be configured to use symbolic links instead.

---

## Files created in your project

### `.zotero-project.json`

Stores the connection between the current repository and its Zotero collection.

Example:

```json
{
  "schemaVersion": 1,
  "collectionPath": "Projects/my-research-project",
  "collectionKey": "ABCD2345",
  "referenceDir": "papers/reference",
  "manifestFile": "papers/reference/papers.json",
  "bibFile": "papers/references.bib",
  "fallback": "copy"
}
```

### `papers/reference/papers.json`

An automatically generated project-paper manifest for agents. It records useful metadata and the local paths of papers that belong to the current project.

Do not treat it as a manually maintained database; it can be regenerated from Zotero.

### `papers/references.bib`

BibTeX exported from the current project's Zotero collection, intended for LaTeX and citation workflows.

---

## Library safety

The Skill follows a few conservative rules:

- **Zotero is the source of truth.**
- It never edits `zotero.sqlite` directly.
- It never writes files directly into `Zotero/storage`.
- It searches locally before importing a new paper.
- It does not delete Zotero items just because a project file is removed.
- Normal synchronization is additive and safe by default.
- Removing stale helper-managed project files requires an explicit prune operation.

The project reference directory is a working view of the project's literature, not a replacement for your Zotero library.

---

## Current limitations

v0.1.1 intentionally keeps the workflow small and predictable.

- Zotero 10+ is required.
- Web search is performed by the host agent, not by this Skill itself.
- Metadata for newly downloaded papers is supplied by the host agent.
- MinerU is not yet a built-in dependency.
- Better BibTeX integration is not required yet.
- Hard links require the Zotero PDF and project directory to be on the same filesystem/volume.
- The Skill does not permanently delete Zotero library items.

These limits are deliberate: the project aims to connect Zotero and coding agents rather than build another literature-management platform.

---

## Advanced / manual commands

Most users should not need these commands directly. They are useful for troubleshooting and development.

From the installed Skill directory:

```bash
python scripts/zotero_papers.py status
python scripts/zotero_papers.py authorize
python scripts/zotero_papers.py init
python scripts/zotero_papers.py search "query" --scope project --json
python scripts/zotero_papers.py search "query" --scope library --json
python scripts/zotero_papers.py resolve ITEM_KEY --json
python scripts/zotero_papers.py add ITEM_KEY --json
python scripts/zotero_papers.py sync --json
python scripts/zotero_papers.py fulltext ITEM_KEY
```

Run:

```bash
python scripts/zotero_papers.py --help
```

for the complete command reference.

---

## Troubleshooting

### The agent cannot connect to Zotero

Make sure:

1. Zotero 10+ is running.
2. **Settings → Advanced → Allow other applications on this computer to communicate with Zotero** is enabled.
3. Local firewall/security software is not blocking Zotero's local application API.

### Zotero asks for permission

The first operation that modifies your library requires authorization. Review the Zotero prompt and choose the permission level you are comfortable with. Persistent authorization is useful for normal multi-step paper imports.

### A project PDF was copied instead of hard-linked

Hard links require the Zotero attachment and project directory to be on the same filesystem/volume. If they are on different drives, the configured fallback is used.

### I removed a PDF from the project. Will it delete the Zotero paper?

No. Zotero is canonical, and removing a project-side reference does not automatically delete the Zotero library item.

---

## Repository layout

```text
.
├── README.md
├── CHANGELOG.md
└── skills/
    └── zotero-project-papers/
        ├── SKILL.md
        ├── README.md
        ├── scripts/
        │   └── zotero_papers.py
        ├── examples/
        └── tests/
```

The actual Agent Skill lives in:

```text
skills/zotero-project-papers/
```

---

## Development

From the Skill directory:

```bash
python scripts/zotero_papers.py --version
python scripts/zotero_papers.py --help
python -m unittest discover -s tests -v
```

No third-party Python packages are required for v0.1.1.

---

## Project philosophy

If Zotero already does something well, this project should use Zotero rather than reimplement it.

If the coding agent already has web search, this project should use that rather than build another search engine.

Zotero Project Papers focuses on the missing bridge:

> **Use Zotero as the long-term paper library, make the current project's literature visible to the agent, and search the web only when the local library is not enoug**

# zotero-project-papers — CC Switch repository

This repository is laid out for **CC Switch** skill discovery and management.

The actual Agent Skill lives at:

```text
skills/zotero-project-papers/
```

It makes Zotero the canonical local paper library and exposes the papers relevant to the current coding/research repository through `papers/reference/` for Claude Code, Codex, and other compatible coding agents.

## Install and manage with CC Switch

After publishing this directory as a GitHub repository:

1. Open **CC Switch → Skills → Repository Management**.
2. Choose **Add Repository**.
3. Enter:
   - **Owner**: your GitHub username or organization
   - **Name**: this repository name
   - **Branch**: `main`
   - **Subdirectory**: `skills`
4. Refresh the Skills list.
5. Find **zotero-project-papers** and click **Install**.
6. Enable/sync it to Claude Code, Codex, or any other supported agent you want to use.

### Optional one-click CC Switch deep link

After the repository has a permanent GitHub owner/name, you can publish a one-click import link in this form:

```text
ccswitch://v1/import?resource=skill&repo=OWNER/REPO&branch=main&skills_path=skills&directory=zotero-project-papers
```

Replace `OWNER/REPO` with the real GitHub repository. The link opens CC Switch and imports the skill repository configuration; the user can then install/enable the skill from the Skills panel.

CC Switch keeps the managed source copy in its skill store and distributes it to the selected agent directories according to your configured sync method. When this GitHub repository changes, CC Switch can detect the changed content and offer an update.

## Why `skills/zotero-project-papers/` instead of putting `SKILL.md` at repository root?

This is the standard multi-skill repository layout and works cleanly with CC Switch custom repositories. It also avoids historical CC Switch URL-install edge cases involving a root-level `SKILL.md`.

## Repository layout

```text
.
├── README.md
├── .gitignore
└── skills/
    └── zotero-project-papers/
        ├── SKILL.md
        ├── README.md
        ├── scripts/
        │   └── zotero_papers.py
        ├── examples/
        │   └── metadata.example.json
        └── tests/
            └── test_utils.py
```

## Runtime requirements

- Zotero 10+ running locally
- Zotero local application communication enabled
- Python 3.10+

No Python package installation is required for v0.1.1.

## Development check

From the skill directory:

```bash
python scripts/zotero_papers.py --version
python scripts/zotero_papers.py --help
python -m unittest discover -s tests -v
```
