import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("zpp_compat", HERE / "scripts" / "zotero_papers.py")
zpp = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = zpp
SPEC.loader.exec_module(zpp)


class UpgradeCompatibilityTests(unittest.TestCase):
    def test_all_historical_config_versions_preserve_user_paths_and_binding(self):
        for version in range(1, zpp.SCHEMA_VERSION + 1):
            with self.subTest(version=version):
                original = {
                    "schemaVersion": version,
                    "projectName": "Existing Project",
                    "collectionPath": "Projects/Existing Project",
                    "collectionKey": "COLL1234",
                    "referenceDir": "literature/custom-refs",
                    "manifestFile": "literature/custom-refs/index.json",
                    "bibFile": "manuscript/my-library.bib",
                    "claimsFile": "notes/evidence-ledger.json",
                    "citationsFile": "notes/citation-provenance.json",
                    "fallback": "symlink",
                    "zoteroServerID": "server-existing",
                    "sync": {
                        "state": "diverged",
                        "driftPolicy": "continue",
                        "acknowledgedFingerprint": "abc123",
                        "acknowledgedAt": "2026-01-01T00:00:00Z",
                    },
                }
                migrated = zpp.normalize_project_config(original)
                self.assertEqual(migrated["schemaVersion"], zpp.SCHEMA_VERSION)
                for key in (
                    "collectionPath", "collectionKey", "referenceDir", "manifestFile",
                    "bibFile", "claimsFile", "citationsFile", "fallback", "zoteroServerID",
                ):
                    self.assertEqual(migrated[key], original[key], key)
                self.assertEqual(migrated["sync"]["state"], "diverged")
                self.assertEqual(migrated["sync"]["driftPolicy"], "continue")
                self.assertEqual(migrated["sync"]["acknowledgedFingerprint"], "abc123")

    def test_loading_old_config_does_not_move_or_delete_reference_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            refs = root / "references" / "nested"
            refs.mkdir(parents=True)
            pdf = refs / "keep-me.pdf"
            pdf.write_bytes(b"existing-user-file")
            config = {
                "schemaVersion": 1,
                "projectName": "Existing Project",
                "collectionPath": "Projects/Existing Project",
                "collectionKey": "COLL1234",
                "referenceDir": "references",
                "manifestFile": "references/papers.json",
                "bibFile": "refs.bib",
                "fallback": "copy",
            }
            (root / zpp.CONFIG_NAME).write_text(json.dumps(config), encoding="utf-8")

            loaded_root, loaded = zpp.load_project(root)
            self.assertEqual(loaded_root, root.resolve())
            self.assertEqual(loaded["referenceDir"], "references")
            self.assertEqual(loaded["manifestFile"], "references/papers.json")
            self.assertEqual(loaded["bibFile"], "refs.bib")
            self.assertTrue(pdf.exists())
            self.assertEqual(pdf.read_bytes(), b"existing-user-file")
            self.assertFalse((root / "papers" / "reference").exists())

    def test_previously_published_cli_commands_remain_available(self):
        parser = zpp.build_parser()
        subparsers_action = next(
            action for action in parser._actions
            if action.__class__.__name__ == "_SubParsersAction"
        )
        available = set(subparsers_action.choices)
        historical_commands = {
            "doctor", "status", "check", "authorize", "init", "set-reference-dir",
            "search", "cache", "show", "resolve", "add", "sync", "reconcile",
            "fulltext", "evidence", "record-claim", "audit", "import-metadata",
            "attach-pdf", "import-doi", "enrich", "fetch", "tag", "organize",
            "import-pdf", "citation-resolve", "citation-record", "citation-audit",
        }
        self.assertTrue(historical_commands.issubset(available), historical_commands - available)

    def test_minor_release_adds_citation_ledger_via_automatic_schema_migration(self):
        self.assertEqual(zpp.SCHEMA_VERSION, 6)
        migrated = zpp.normalize_project_config({
            "schemaVersion": 5,
            "collectionPath": "Projects/Existing",
            "collectionKey": "AAAA1111",
            "referenceDir": "refs",
            "manifestFile": "refs/papers.json",
            "bibFile": "custom/references.bib",
            "claimsFile": "custom/claims.json",
        })
        self.assertEqual(migrated["bibFile"], "custom/references.bib")
        self.assertEqual(migrated["claimsFile"], "custom/claims.json")
        self.assertEqual(migrated["citationsFile"], "papers/citations.json")


if __name__ == "__main__":
    unittest.main()
