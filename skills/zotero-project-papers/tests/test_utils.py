import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("zpp", HERE / "scripts" / "zotero_papers.py")
zpp = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = zpp
SPEC.loader.exec_module(zpp)


class UtilsTests(unittest.TestCase):
    def test_normalize_doi(self):
        self.assertEqual(zpp.normalize_doi("https://doi.org/10.1000/ABC"), "10.1000/abc")
        self.assertEqual(zpp.normalize_doi("DOI: 10.1000/ABC"), "10.1000/abc")

    def test_normalize_title(self):
        self.assertEqual(zpp.normalize_title("  Attention: Is All You Need! "), "attention is all you need")

    def test_safe_filename(self):
        self.assertEqual(zpp.safe_filename('a:b?.pdf'), "a_b_.pdf")
        self.assertEqual(zpp.safe_filename('CON.pdf'), "_CON.pdf")

    @unittest.skipIf(os.name == "nt", "file URL expectation differs on Windows")
    def test_parse_file_url_posix(self):
        self.assertEqual(zpp.parse_file_url("file:///tmp/a%20b.pdf"), Path("/tmp/a b.pdf"))


    def test_duplicate_warnings_doi(self):
        rows = [
            {"key": "AAAA1111", "data": {"key": "AAAA1111", "itemType": "journalArticle", "title": "A", "DOI": "10.1/x", "creators": []}},
            {"key": "BBBB2222", "data": {"key": "BBBB2222", "itemType": "journalArticle", "title": "B", "DOI": "https://doi.org/10.1/X", "creators": []}},
        ]
        warnings = zpp.duplicate_warnings(rows)
        self.assertEqual(warnings[0]["type"], "duplicate-doi")
        self.assertEqual(set(warnings[0]["itemKeys"]), {"AAAA1111", "BBBB2222"})

    def test_metadata_warning_editor_before_author(self):
        obj = {"data": {"itemType": "bookSection", "creators": [
            {"creatorType": "editor", "firstName": "Ed", "lastName": "One"},
            {"creatorType": "author", "firstName": "Ada", "lastName": "Author"},
        ]}}
        warnings = zpp.metadata_warnings(obj)
        self.assertTrue(any("precede" in x for x in warnings))

    def test_project_root_explicit_start(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            child = root / "a" / "b"
            child.mkdir(parents=True)
            (root / ".git").mkdir()
            self.assertEqual(zpp.project_root(child), root.resolve())

    def test_zpp_error_payload(self):
        err = zpp.ZPPError("bad", code="example", hint="fix it")
        self.assertEqual(err.as_dict(), {"type": "example", "message": "bad", "hint": "fix it"})

    def test_materialize_hardlink(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "a.pdf"
            dst = root / "b.pdf"
            src.write_bytes(b"pdf")
            mode = zpp.materialize_pdf(src, dst, fallback="copy")
            self.assertIn(mode, {"hardlink", "copy"})
            self.assertEqual(dst.read_bytes(), b"pdf")
            if mode == "hardlink":
                self.assertTrue(os.path.samefile(src, dst))


    def test_config_v1_migrates_to_v2(self):
        cfg = zpp.normalize_project_config({
            "schemaVersion": 1,
            "collectionPath": "Projects/Test",
            "collectionKey": "AAAA1111",
            "referenceDir": "refs",
            "manifestFile": "refs/papers.json",
            "bibFile": "papers/references.bib",
            "fallback": "copy",
        })
        self.assertEqual(cfg["schemaVersion"], 2)
        self.assertEqual(cfg["sync"]["driftPolicy"], "prompt")

    def test_detect_reference_dirs_with_subdirs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "references" / "survey").mkdir(parents=True)
            (root / "references" / "survey" / "a.pdf").write_bytes(b"a")
            rows = zpp.detect_reference_dirs(root)
            row = next(x for x in rows if x["path"] == "references")
            self.assertEqual(row["pdfCount"], 1)
            self.assertTrue(row["hasSubdirectories"])

    def test_flatten_reference_pdfs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "old"
            target = root / "new"
            (source / "a").mkdir(parents=True)
            (source / "b").mkdir(parents=True)
            (source / "a" / "same.pdf").write_bytes(b"one")
            (source / "b" / "same.pdf").write_bytes(b"two")
            rows = zpp.flatten_reference_pdfs(source, target, fallback="copy")
            self.assertEqual(len(rows), 2)
            self.assertEqual(len(list(target.glob("*.pdf"))), 2)
            self.assertFalse(any(x.is_dir() for x in target.iterdir()))

    def test_flatten_when_target_is_parent_of_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "references"
            source = target / "nested"
            source.mkdir(parents=True)
            (source / "a.pdf").write_bytes(b"a")
            rows = zpp.flatten_reference_pdfs(source, target, fallback="copy")
            self.assertEqual(len(rows), 1)
            self.assertTrue((target / "a.pdf").exists())

    def test_acknowledged_drift_fingerprint(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = {
                "schemaVersion": 2,
                "collectionPath": "Projects/Test",
                "collectionKey": "AAAA1111",
                "referenceDir": "refs",
                "manifestFile": "refs/papers.json",
                "bibFile": "papers/references.bib",
                "fallback": "copy",
                "sync": zpp.default_sync_config(),
            }
            snap = {
                "clean": False,
                "fingerprint": "abc",
                "needsPrompt": True,
                "driftPolicy": "prompt",
                "zoteroCount": 1,
                "manifestCount": 1,
                "localPdfCount": 2,
                "drift": {"zoteroOnlyItemKeys": [], "manifestOnlyItemKeys": [], "missingManagedFiles": [], "modifiedManagedFiles": [], "localOnlyFiles": ["refs/x.pdf"]},
            }
            updated = zpp.persist_consistency_state(root, cfg, snap, acknowledge_continue=True)
            self.assertFalse(updated["needsPrompt"])
            self.assertEqual(cfg["sync"]["acknowledgedFingerprint"], "abc")
            snap2 = dict(snap)
            snap2["fingerprint"] = "def"
            updated2 = zpp.persist_consistency_state(root, cfg, snap2)
            self.assertTrue(updated2["needsPrompt"])

    def test_user_modified_managed_path_is_not_selected_for_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ref = root / "refs"
            ref.mkdir()
            src = root / "zotero.pdf"
            src.write_bytes(b"canonical")
            prior = ref / "zotero.pdf"
            prior.write_bytes(b"user replacement")
            chosen = zpp.choose_reference_filename(src, "AAAA1111", ref, str(prior))
            self.assertNotEqual(chosen, prior)
            self.assertEqual(prior.read_bytes(), b"user replacement")

    def test_consistency_detects_modified_managed_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ref = root / "refs"
            ref.mkdir()
            source = root / "zotero.pdf"
            source.write_bytes(b"canonical")
            project_pdf = ref / "paper.pdf"
            project_pdf.write_bytes(b"replacement")
            config = {
                "schemaVersion": 2,
                "collectionPath": "Projects/Test",
                "collectionKey": "AAAA1111",
                "referenceDir": "refs",
                "manifestFile": "refs/papers.json",
                "bibFile": "papers/references.bib",
                "fallback": "copy",
                "sync": zpp.default_sync_config(),
            }
            zpp.json_dump({
                "papers": [{
                    "itemKey": "ITEM0001",
                    "projectPath": "refs/paper.pdf",
                    "zoteroPath": str(source),
                }]
            }, root / "refs" / "papers.json")
            original = zpp.collection_items
            try:
                zpp.collection_items = lambda client, collection_key, query=None, fulltext=False: [
                    {"key": "ITEM0001", "data": {"key": "ITEM0001", "itemType": "journalArticle", "title": "Paper", "creators": []}}
                ]
                snap = zpp.consistency_snapshot(object(), root, config)
            finally:
                zpp.collection_items = original
            self.assertIn("refs/paper.pdf", snap["drift"]["modifiedManagedFiles"])
            self.assertFalse(snap["clean"])

    def test_init_requires_onboarding_when_existing_reference_found(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "references").mkdir()
            (root / "references" / "a.pdf").write_bytes(b"pdf")
            args = type("Args", (), {
                "project_root": str(root),
                "force": False,
                "onboarding": None,
                "existing_dir": None,
                "reference_dir": None,
                "name": None,
                "collection_root": "Projects",
                "bib_file": None,
                "fallback": "copy",
            })()
            with self.assertRaises(zpp.ZPPError) as ctx:
                zpp.cmd_init(args)
            self.assertEqual(ctx.exception.code, "onboarding_required")
            self.assertEqual(ctx.exception.details["candidates"][0]["path"], "references")

    def test_build_item_does_not_require_items_new(self):
        class FakeClient:
            def read(self, path, params=None):
                self.path = path
                if path == "itemTypeFields":
                    return [{"field": "title"}, {"field": "DOI"}, {"field": "publicationTitle"}]
                raise AssertionError(path)
        client = FakeClient()
        item = zpp.build_item_from_metadata(client, {
            "itemType": "journalArticle",
            "title": "Paper",
            "DOI": "10.1/test",
            "publicationTitle": "Journal",
            "creators": [{"creatorType": "author", "lastName": "Smith"}],
            "notAField": "ignored",
        }, "COLL0001")
        self.assertEqual(client.path, "itemTypeFields")
        self.assertEqual(item["title"], "Paper")
        self.assertNotIn("notAField", item)
        self.assertEqual(item["collections"], ["COLL0001"])

    def test_item_type_fields_falls_back_when_schema_endpoint_missing(self):
        class FakeClient:
            def read(self, path, params=None):
                raise zpp.ZPPError("missing", code="zotero_http_404")
        fields, strategy = zpp.item_type_fields(FakeClient(), "conferencePaper")
        self.assertEqual(strategy, "builtin-fallback")
        self.assertIn("conferenceName", fields)
        self.assertIn("DOI", fields)

    def test_canonical_pdf_filename_uses_metadata(self):
        name = zpp.canonical_pdf_filename({
            "title": "A Better: Paper?",
            "date": "2025-10-01",
            "creators": [
                {"creatorType": "author", "firstName": "Ada", "lastName": "Guo"},
                {"creatorType": "author", "firstName": "Bo", "lastName": "Li"},
            ],
        })
        self.assertEqual(name, "Guo et al. - 2025 - A Better_ Paper_.pdf")

    def test_create_attachment_uses_canonical_name_without_template_endpoint(self):
        class FakeClient:
            def create_item(self, item):
                self.item = item
                return "ATTACH01"
        client = FakeClient()
        key, filename = zpp.create_imported_attachment(client, "PARENT01", Path("tmp.pdf"), {
            "title": "Paper Title",
            "date": "2026",
            "creators": [{"creatorType": "author", "lastName": "Ding"}],
        })
        self.assertEqual(key, "ATTACH01")
        self.assertEqual(filename, "Ding - 2026 - Paper Title.pdf")
        self.assertEqual(client.item["filename"], filename)
        self.assertEqual(client.item["linkMode"], "imported_file")

    def test_metadata_diff_reports_item_type_mismatch(self):
        existing = {"data": {"itemType": "journalArticle", "title": "Same", "creators": []}}
        diff = zpp.metadata_diff(existing, {"itemType": "conferencePaper", "title": "Same"})
        self.assertEqual(diff["itemType"]["existing"], "journalArticle")
        self.assertEqual(diff["itemType"]["incoming"], "conferencePaper")

    def test_update_existing_metadata_refuses_item_type_change(self):
        class FakeClient:
            pass
        original = zpp.get_item
        try:
            zpp.get_item = lambda client, key: {"data": {
                "key": key, "version": 1, "itemType": "journalArticle", "title": "Old"
            }}
            with self.assertRaises(zpp.ZPPError) as ctx:
                zpp.update_existing_metadata(FakeClient(), "ITEM0001", {
                    "itemType": "conferencePaper", "title": "New"
                })
        finally:
            zpp.get_item = original
        self.assertEqual(ctx.exception.code, "item_type_conflict")

    def test_update_existing_metadata_same_type_patches_fields(self):
        class FakeClient:
            def read(self, path, params=None):
                if path == "itemTypeFields":
                    return [{"field": "title"}, {"field": "DOI"}]
                raise AssertionError(path)
            def write(self, method, path, **kwargs):
                self.method = method
                self.path = path
                self.kwargs = kwargs
        client = FakeClient()
        original = zpp.get_item
        try:
            zpp.get_item = lambda client, key: {"data": {
                "key": key, "version": 7, "itemType": "journalArticle", "title": "Old", "DOI": ""
            }}
            out = zpp.update_existing_metadata(client, "ITEM0001", {
                "itemType": "journalArticle", "title": "New", "DOI": "10.1/x"
            })
        finally:
            zpp.get_item = original
        self.assertTrue(out["updated"])
        self.assertEqual(client.method, "PATCH")
        self.assertEqual(client.kwargs["json_body"], {"title": "New", "DOI": "10.1/x"})
        self.assertEqual(client.kwargs["extra_headers"]["If-Unmodified-Since-Version"], "7")


if __name__ == "__main__":
    unittest.main()
