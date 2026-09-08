# zotero-project-papers v0.2.1

Agent Skill + stdlib-only Python helper that makes Zotero the canonical paper library while exposing a configurable project-level paper working set to coding agents.

## v0.2.1 highlights

- configurable `referenceDir`;
- existing-reference-folder onboarding (`use-existing`, `separate`, `merge`);
- recursive PDF merge with flat destination and collision-safe names;
- cheap `check` command for Zotero ↔ manifest ↔ filesystem drift;
- remembered drift fingerprints so users can choose “continue” without repeated prompts;
- `reconcile` workflow and explicit destructive `--remove-local-only` option;
- `set-reference-dir` for later renaming/moving;
- user-modified managed PDFs are preserved instead of overwritten;
- v0.1 project configs migrate automatically to schema v2.
- import no longer depends on Local API `/items/new`;
- `itemTypeFields` dynamic schema with built-in fallback;
- canonical metadata-derived PDF filenames for new imports;
- duplicate metadata diffs and explicit same-type metadata repair;

## Requirements

- Zotero 10+ running locally.
- Zotero Settings → Advanced → **Allow other applications on this computer to communicate with Zotero**.
- Python 3.10+.

## CC Switch

Distribute this Skill from the repository `skills/` directory and let CC Switch install/update it. Do not maintain a parallel self-installer.

## Development smoke test

```bash
python scripts/zotero_papers.py --version
python scripts/zotero_papers.py --help
python -m unittest discover -s tests -v
```

No third-party Python packages are required.
