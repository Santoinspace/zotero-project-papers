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
