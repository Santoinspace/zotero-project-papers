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


    def test_config_v1_migrates_to_v6(self):
        cfg = zpp.normalize_project_config({
            "schemaVersion": 1,
            "collectionPath": "Projects/Test",
            "collectionKey": "AAAA1111",
            "referenceDir": "refs",
            "manifestFile": "refs/papers.json",
            "bibFile": "papers/references.bib",
            "fallback": "copy",
        })
        self.assertEqual(cfg["schemaVersion"], 6)
        self.assertEqual(cfg["claimsFile"], "papers/claims.json")
        self.assertEqual(cfg["citationsFile"], "papers/citations.json")
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



    def test_manifest_search_is_local_fast_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ref = root / "refs"
            ref.mkdir()
            config = zpp.normalize_project_config({
                "schemaVersion": 3,
                "collectionPath": "Projects/Test",
                "collectionKey": "COLL0001",
                "referenceDir": "refs",
                "manifestFile": "refs/papers.json",
                "bibFile": "papers/references.bib",
                "fallback": "copy",
                "zoteroServerID": "SERVER1",
            })
            zpp.save_project_config(root, config)
            zpp.json_dump({"papers": [{
                "itemKey": "ITEM0001",
                "itemType": "conferencePaper",
                "title": "Fast Local Retrieval for Agents",
                "creators": ["Ada Example"],
                "date": "2026",
                "DOI": "10.1/example",
                "publicationTitle": "Example Conference",
                "projectPath": "refs/paper.pdf",
            }]}, ref / "papers.json")
            args = type("Args", (), {
                "project_root": str(root), "scope": "project", "query": "local retrieval",
                "fulltext": False, "no_fulltext_fallback": False, "verbose": False,
                "refresh_cache": False, "limit": 10,
            })()
            original = zpp.ZoteroClient
            class BombClient:
                def __init__(self, *a, **k):
                    raise AssertionError("Zotero must not be contacted on project-manifest hit")
            try:
                zpp.ZoteroClient = BombClient
                result = zpp.cmd_search(args)
            finally:
                zpp.ZoteroClient = original
            self.assertEqual(result["source"], "project-manifest")
            self.assertFalse(result["zoteroContacted"])
            self.assertEqual(result["items"][0]["key"], "ITEM0001")

    def test_metadata_cache_searches_abstract_without_returning_it_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            old_xdg = os.environ.get("XDG_CACHE_HOME")
            old_local = os.environ.get("LOCALAPPDATA")
            os.environ["XDG_CACHE_HOME"] = td
            os.environ["LOCALAPPDATA"] = td
            try:
                conn = zpp._cache_connect("SERVER-ABSTRACT")
                zpp._cache_upsert_item(conn, {
                    "key": "ITEM0002",
                    "version": 1,
                    "data": {
                        "key": "ITEM0002", "version": 1, "itemType": "conferencePaper",
                        "title": "A Generic Method", "creators": [{"creatorType": "author", "lastName": "Lee"}],
                        "date": "2026", "abstractNote": "Evaluation includes the RareBench-X benchmark.",
                        "collections": ["COLL0001"],
                    },
                })
                conn.commit(); conn.close()
                hits = zpp.search_metadata_cache("SERVER-ABSTRACT", "RareBench-X", 10)
                self.assertEqual(hits[0]["key"], "ITEM0002")
                self.assertNotIn("abstractNote", hits[0])
            finally:
                if old_xdg is None: os.environ.pop("XDG_CACHE_HOME", None)
                else: os.environ["XDG_CACHE_HOME"] = old_xdg
                if old_local is None: os.environ.pop("LOCALAPPDATA", None)
                else: os.environ["LOCALAPPDATA"] = old_local

    def test_metadata_cache_refresh_uses_since_after_first_build(self):
        with tempfile.TemporaryDirectory() as td:
            old_xdg = os.environ.get("XDG_CACHE_HOME")
            old_local = os.environ.get("LOCALAPPDATA")
            os.environ["XDG_CACHE_HOME"] = td
            os.environ["LOCALAPPDATA"] = td
            class FakeClient:
                server_id = "SERVER-SINCE"
                def bootstrap(self):
                    return None
                def read(self, path, params=None, raw=False):
                    params = params or {}
                    if path == "users/0/items/top":
                        if "since" not in params:
                            body = [{"key":"A","version":7,"data":{"key":"A","version":7,"itemType":"journalArticle","title":"First","creators":[],"collections":[]}}]
                            return zpp.HTTPResult(200, {"Last-Modified-Version":"7"}, __import__('json').dumps(body).encode())
                        self.seen_since = params["since"]
                        body = [{"key":"B","version":8,"data":{"key":"B","version":8,"itemType":"journalArticle","title":"Second","creators":[],"collections":[]}}]
                        return zpp.HTTPResult(200, {"Last-Modified-Version":"8"}, __import__('json').dumps(body).encode())
                    if path == "users/0/deleted":
                        self.deleted_since = params["since"]
                        return zpp.HTTPResult(200, {"Last-Modified-Version":"8"}, b'{"items":[]}')
                    raise AssertionError(path)
            try:
                c = FakeClient()
                first = zpp.refresh_metadata_cache(c, force=True, max_age=0)
                self.assertEqual(first["strategy"], "full")
                second = zpp.refresh_metadata_cache(c, force=True, max_age=0)
                self.assertEqual(second["strategy"], "incremental")
                self.assertEqual(c.seen_since, 7)
                self.assertEqual(c.deleted_since, 7)
                self.assertEqual(zpp.cache_status("SERVER-SINCE")["libraryVersion"], 8)
            finally:
                if old_xdg is None: os.environ.pop("XDG_CACHE_HOME", None)
                else: os.environ["XDG_CACHE_HOME"] = old_xdg
                if old_local is None: os.environ.pop("LOCALAPPDATA", None)
                else: os.environ["LOCALAPPDATA"] = old_local

    def test_http_headers_are_case_insensitive(self):
        r = zpp.HTTPResult(200, {"Zotero-Server-Id": "SERVER1", "Content-Type": "application/json"}, b"{}")
        self.assertEqual(r.header("Zotero-Server-ID"), "SERVER1")
        self.assertEqual(r.header("content-type"), "application/json")

    def test_classify_match_prefers_exact_title_over_abstract(self):
        exact = zpp.classify_match({"title": "Progressive Cross Scale Semantic Alignment"}, "Progressive cross-scale semantic alignment")
        weak = zpp.classify_match({"title": "Other Method", "abstractNote": "We discuss progressive cross scale semantic alignment."}, "Progressive cross-scale semantic alignment")
        self.assertEqual(exact["matchType"], "normalized-title-exact")
        self.assertGreater(exact["score"], weak["score"])
        self.assertEqual(weak["matchType"], "abstract-token")

    def test_classify_match_doi_exact(self):
        m = zpp.classify_match({"title": "Whatever", "DOI": "10.1234/ABC"}, "https://doi.org/10.1234/abc")
        self.assertEqual(m["matchType"], "doi-exact")
        self.assertEqual(m["score"], 1.0)

    def test_auth_store_override_is_writable(self):
        with tempfile.TemporaryDirectory() as td:
            old = os.environ.get("ZPP_AUTH_STORE")
            try:
                os.environ["ZPP_AUTH_STORE"] = str(Path(td) / "private" / "auth.json")
                status = zpp.auth_store_preflight(create=True)
                self.assertTrue(status["writable"])
                self.assertTrue(status["path"].endswith("auth.json"))
            finally:
                if old is None: os.environ.pop("ZPP_AUTH_STORE", None)
                else: os.environ["ZPP_AUTH_STORE"] = old

    def test_metadata_only_import_marks_missing_pdf(self):
        class FakeClient:
            def ensure_write_auth(self, require_remembered=True):
                self.authed = True
            def read(self, path, params=None):
                if path == "itemTypeFields":
                    return [{"field": "title"}, {"field": "DOI"}]
                raise AssertionError(path)
            def create_item(self, item):
                self.created = item
                return "ITEMNEW1"
        client = FakeClient()
        original_dup = zpp.find_duplicates
        original_sync = zpp.sync_project
        try:
            zpp.find_duplicates = lambda client, metadata: []
            zpp.sync_project = lambda client, root, config, prune=False: {
                "papers": [{"itemKey": "ITEMNEW1", "title": "Paper", "pdfStatus": "missing", "projectPath": None}]
            }
            cfg = {"collectionKey": "COLL1"}
            out = zpp.import_metadata_record(client, Path("."), cfg, {"itemType": "journalArticle", "title": "Paper", "DOI": "10.1/x"})
            self.assertEqual(out["action"], "imported-metadata")
            self.assertEqual(out["pdfStatus"], "missing")
            self.assertEqual(client.created["collections"], ["COLL1"])
        finally:
            zpp.find_duplicates = original_dup
            zpp.sync_project = original_sync

    def test_validate_pdf_lightweight(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.pdf"
            p.write_bytes(b"%PDF-1.4\n" + b"x" * 1100 + b"\n/Type /Page\n%%EOF\n")
            out = zpp.validate_pdf_file(p, content_type="application/pdf")
            self.assertTrue(out["validPdf"])
            self.assertEqual(len(out["sha256"]), 64)

    def test_possible_legacy_reference_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "papers" / "references").mkdir(parents=True)
            (root / "papers" / "references" / "old.pdf").write_bytes(b"x")
            rows = zpp.possible_legacy_reference_dirs(root, "papers/reference")
            self.assertTrue(any(r["path"] == "papers/references" for r in rows))


    def test_doctor_accepts_server_id_header_case_variant(self):
        with tempfile.TemporaryDirectory() as td:
            old_auth = os.environ.get("ZPP_AUTH_STORE")
            try:
                os.environ["ZPP_AUTH_STORE"] = str(Path(td) / "auth.json")
                class FakeClient:
                    base_url = "http://127.0.0.1:23119/api/"
                    server_id = None
                    api_version = None
                    api_key = None
                    def _raw_request(self, method, path_or_url, **kwargs):
                        return zpp.HTTPResult(200, {
                            "Zotero-Server-Id": "SERVER1",
                            "Zotero-API-Version": "3",
                            "X-Zotero-Version": "10.0.1",
                        }, b"{}")
                    def read(self, path, params=None):
                        raise AssertionError(path)
                original = zpp.ZoteroClient
                try:
                    zpp.ZoteroClient = FakeClient
                    args = type("Args", (), {"project_root": td})()
                    out = zpp.cmd_doctor(args)
                finally:
                    zpp.ZoteroClient = original
                self.assertTrue(out["serverIdDetected"])
                self.assertTrue(out["localApiEnabled"])
                self.assertEqual(out["zoteroVersion"], "10.0.1")
                self.assertEqual(out["diagnosis"], "authorization_required")
            finally:
                if old_auth is None: os.environ.pop("ZPP_AUTH_STORE", None)
                else: os.environ["ZPP_AUTH_STORE"] = old_auth

    def test_compact_sync_result_hides_papers(self):
        data = {"paperCount": 2, "papers": [{"itemKey": "A"}, {"itemKey": "B"}]}
        self.assertNotIn("papers", zpp.compact_sync_result(data))
        self.assertIn("papers", zpp.compact_sync_result(data, include_papers=True))


    def test_weak_metadata_results_fall_back_to_fulltext(self):
        with tempfile.TemporaryDirectory() as td:
            class FakeClient:
                def __init__(self):
                    self.server_id = None
                def bootstrap(self):
                    self.server_id = "SERVER-NOCACHE"
            original_client = zpp.ZoteroClient
            original_search = zpp.library_search
            original_cache_status = zpp.cache_status
            try:
                zpp.ZoteroClient = FakeClient
                zpp.cache_status = lambda server_id: {"exists": False, "libraryVersion": None}
                def fake_search(client, query, fulltext=False, limit=30):
                    if not fulltext:
                        return [{"data": {"key": "WEAK1", "itemType": "journalArticle", "title": "Unrelated Work", "creators": []}}]
                    return [{"data": {"key": "FULL1", "itemType": "journalArticle", "title": "Another Work", "creators": []}}]
                zpp.library_search = fake_search
                args = type("Args", (), {
                    "project_root": td, "scope": "library", "query": "rare internal phrase",
                    "fulltext": False, "no_fulltext_fallback": False, "verbose": False,
                    "refresh_cache": False, "limit": 10,
                })()
                out = zpp.cmd_search(args)
                self.assertEqual(out["source"], "zotero-fulltext")
                self.assertEqual(out["items"][0]["matchType"], "zotero-fulltext")
            finally:
                zpp.ZoteroClient = original_client
                zpp.library_search = original_search
                zpp.cache_status = original_cache_status


    def test_library_search_uses_top_level_endpoint_to_avoid_false_zero(self):
        class FakeClient:
            def __init__(self):
                self.calls = []
            def read(self, path, params=None, raw=False):
                self.calls.append((path, params))
                if path == "users/0/items/top":
                    return [{"data": {"key": "PAPER001", "itemType": "journalArticle", "title": "Missing Modality Study", "creators": []}}]
                if path == "users/0/items":
                    # This simulates the old bug: a small /items limit can be consumed by child attachments.
                    return [{"data": {"key": "ATTACH01", "itemType": "attachment", "parentItem": "PAPER001", "title": "PDF"}}]
                return []
        client = FakeClient()
        rows = zpp.library_search(client, "missing modality", fulltext=False, limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(zpp.item_data(rows[0])["key"], "PAPER001")
        self.assertEqual(client.calls[0][0], "users/0/items/top")
        self.assertFalse(any(path == "users/0/items" for path, _ in client.calls))

    def test_library_fulltext_fallback_promotes_child_attachment_to_parent(self):
        class FakeClient:
            def __init__(self):
                self.calls = []
            def read(self, path, params=None, raw=False):
                self.calls.append((path, params))
                if path == "users/0/items/top" and params and params.get("q"):
                    return []
                if path == "users/0/items":
                    return [{"data": {"key": "ATTACH01", "itemType": "attachment", "parentItem": "PAPER001", "title": "PDF"}}]
                if path == "users/0/items/top" and params and params.get("itemKey") == "PAPER001":
                    return [{"data": {"key": "PAPER001", "itemType": "journalArticle", "title": "A Parent Paper", "creators": []}}]
                return []
        client = FakeClient()
        rows = zpp.library_search(client, "rare phrase", fulltext=True, limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(zpp.item_data(rows[0])["key"], "PAPER001")
        self.assertTrue(any(path == "users/0/items" for path, _ in client.calls))

    def test_zero_library_search_explains_non_absence(self):
        with tempfile.TemporaryDirectory() as td:
            class FakeClient:
                def __init__(self):
                    self.server_id = None
                def bootstrap(self):
                    self.server_id = "SERVER-ZERO"
            original_client = zpp.ZoteroClient
            original_search = zpp.library_search
            original_cache_status = zpp.cache_status
            try:
                zpp.ZoteroClient = FakeClient
                zpp.cache_status = lambda server_id: {"exists": False, "libraryVersion": None}
                zpp.library_search = lambda client, query, fulltext=False, limit=30: []
                args = type("Args", (), {
                    "project_root": td, "scope": "library", "query": "broad research concept",
                    "fulltext": False, "no_fulltext_fallback": False, "verbose": False,
                    "refresh_cache": False, "limit": 10,
                })()
                out = zpp.cmd_search(args)
                self.assertEqual(out["count"], 0)
                self.assertIn("not proof", out["zeroResultNote"])
                self.assertTrue(out["webNeeded"])
            finally:
                zpp.ZoteroClient = original_client
                zpp.library_search = original_search
                zpp.cache_status = original_cache_status

    def test_crossref_metadata_mapping(self):
        import json as _json
        payload = {"message": {
            "type": "proceedings-article",
            "title": ["A Conference Paper"],
            "author": [{"given": "Ada", "family": "Lovelace"}],
            "issued": {"date-parts": [[2026, 5, 1]]},
            "container-title": ["Proceedings of Example Conference"],
            "DOI": "10.1234/example",
            "URL": "https://doi.org/10.1234/example",
            "page": "1-10",
        }}
        class Resp:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return _json.dumps(payload).encode()
        original = zpp.urllib.request.urlopen
        try:
            zpp.urllib.request.urlopen = lambda req, timeout=30: Resp()
            out = zpp.crossref_metadata("10.1234/example")
        finally:
            zpp.urllib.request.urlopen = original
        self.assertEqual(out["metadata"]["itemType"], "conferencePaper")
        self.assertEqual(out["metadata"]["proceedingsTitle"], "Proceedings of Example Conference")
        self.assertEqual(out["metadata"]["creators"][0]["lastName"], "Lovelace")

    def test_source_trust_known_academic_host(self):
        self.assertTrue(zpp.source_trust("https://openaccess.thecvf.com/paper.pdf")["knownAcademicHost"])
        self.assertFalse(zpp.source_trust("https://example.invalid/paper.pdf")["knownAcademicHost"])


    def test_record_primary_claim_writes_local_ledger(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "refs").mkdir()
            config = {
                "schemaVersion": 5,
                "collectionKey": "COLL1",
                "referenceDir": "refs",
                "manifestFile": "refs/papers.json",
                "claimsFile": "papers/claims.json",
            }
            zpp.json_dump({"papers": [{"itemKey": "ITEM1", "title": "Paper", "pdfStatus": "available"}]}, root / "refs" / "papers.json")
            out = zpp.record_claim_local(
                root, config, claim="Method A improves boundary quality.", claim_type="primary",
                papers=["ITEM1"], locator={"page": "7", "table": "2"},
            )
            self.assertTrue(out["recorded"])
            ledger = zpp.load_claims(root, config)
            self.assertEqual(ledger["claims"][0]["sourceLayer"], "original-paper")
            self.assertEqual(ledger["claims"][0]["locator"]["page"], "7")

    def test_record_claim_rejects_untracked_paper(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "refs").mkdir()
            config = {"collectionKey": "COLL1", "referenceDir": "refs", "manifestFile": "refs/papers.json", "claimsFile": "papers/claims.json"}
            zpp.json_dump({"papers": []}, root / "refs" / "papers.json")
            with self.assertRaises(zpp.ZPPError) as ctx:
                zpp.record_claim_local(root, config, claim="A claim", claim_type="summary", papers=["NOPE"])
            self.assertEqual(ctx.exception.code, "claim_untracked_paper")

    def test_record_claim_deduplicates_same_claim(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "refs").mkdir()
            config = {"collectionKey": "COLL1", "referenceDir": "refs", "manifestFile": "refs/papers.json", "claimsFile": "papers/claims.json"}
            zpp.json_dump({"papers": [{"itemKey": "ITEM1", "pdfStatus": "available"}]}, root / "refs" / "papers.json")
            first = zpp.record_claim_local(root, config, claim="A useful result.", claim_type="summary", papers=["ITEM1"])
            second = zpp.record_claim_local(root, config, claim="A useful result.", claim_type="summary", papers=["ITEM1"])
            self.assertTrue(first["recorded"])
            self.assertTrue(second["duplicate"])
            self.assertEqual(len(zpp.load_claims(root, config)["claims"]), 1)

    def test_audit_distinguishes_source_layers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "refs").mkdir()
            config = {"collectionKey": "COLL1", "referenceDir": "refs", "manifestFile": "refs/papers.json", "claimsFile": "papers/claims.json"}
            zpp.json_dump({"papers": [
                {"itemKey": "P1", "pdfStatus": "available"},
                {"itemKey": "P2", "pdfStatus": "missing"},
            ]}, root / "refs" / "papers.json")
            ledger = zpp.load_claims(root, config)
            ledger["claims"] = [
                {"id": "c1", "claim": "direct", "type": "primary", "sourceLayer": "original-paper", "papers": ["P1"], "locator": {"page": "2"}},
                {"id": "c2", "claim": "summary", "type": "summary", "sourceLayer": "ai-summary", "papers": ["P1"], "locator": {}},
                {"id": "c3", "claim": "inference", "type": "inference", "sourceLayer": "agent-inference", "papers": ["P1", "P2"], "locator": {}},
                {"id": "c4", "claim": "hypothesis", "type": "hypothesis", "sourceLayer": "agent-inference", "papers": [], "locator": {}},
            ]
            zpp.save_claims(root, config, ledger)
            out = zpp.audit_claims_local(root, config, include_claims=True)
            self.assertEqual(out["originalPaperClaims"], 1)
            self.assertEqual(out["aiSummaryClaims"], 1)
            self.assertEqual(out["inferenceClaims"], 1)
            self.assertEqual(out["hypothesisClaims"], 1)
            self.assertEqual(out["verdict"], "clean")
            self.assertFalse(out["semanticVerificationPerformed"])

    def test_audit_flags_original_claim_without_pdf(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "refs").mkdir()
            config = {"collectionKey": "COLL1", "referenceDir": "refs", "manifestFile": "refs/papers.json", "claimsFile": "papers/claims.json"}
            zpp.json_dump({"papers": [{"itemKey": "P1", "pdfStatus": "missing"}]}, root / "refs" / "papers.json")
            ledger = zpp.load_claims(root, config)
            ledger["claims"] = [{"id": "c1", "claim": "direct", "type": "primary", "sourceLayer": "original-paper", "papers": ["P1"], "locator": {"section": "Results"}}]
            zpp.save_claims(root, config, ledger)
            out = zpp.audit_claims_local(root, config)
            self.assertEqual(out["missingOriginalPdfClaimIds"], ["c1"])

    def test_record_claim_rejects_source_ref_outside_project(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside:
            root = Path(td)
            (root / "refs").mkdir()
            config = {"collectionKey": "COLL1", "referenceDir": "refs", "manifestFile": "refs/papers.json", "claimsFile": "papers/claims.json"}
            zpp.json_dump({"papers": [{"itemKey": "ITEM1", "pdfStatus": "available"}]}, root / "refs" / "papers.json")
            with self.assertRaises(zpp.ZPPError) as ctx:
                zpp.record_claim_local(root, config, claim="Summary", claim_type="summary", papers=["ITEM1"], source_ref=str(Path(outside) / "summary.md"))
            self.assertEqual(ctx.exception.code, "claim_source_ref_outside_project")


    def test_paper_evidence_status_distinguishes_indexed_fulltext(self):
        class FakeClient:
            def read(self, path, params=None):
                if path.endswith("/fulltext"):
                    return {"content": "paper text", "indexedPages": 5, "totalPages": 5}
                raise AssertionError(path)
        original_get = zpp.get_item
        original_pdf = zpp.pdf_attachments
        try:
            zpp.get_item = lambda client, key: {"data": {"key": key, "itemType": "journalArticle", "title": "Paper", "creators": []}}
            with tempfile.TemporaryDirectory() as td:
                pdf = Path(td) / "paper.pdf"
                pdf.write_bytes(b"pdf")
                zpp.pdf_attachments = lambda client, key: [{"key": "ATT1", "filename": "paper.pdf", "path": str(pdf), "linkMode": "imported_file"}]
                out = zpp.paper_evidence_status(FakeClient(), "ITEM1")
                self.assertEqual(out["sourceAvailability"], "indexed-fulltext")
                self.assertEqual(out["recommendedSourceLayer"], "original-paper")
        finally:
            zpp.get_item = original_get
            zpp.pdf_attachments = original_pdf


class CitationProvenanceTests(unittest.TestCase):
    def _project(self, root: Path):
        cfg = zpp.normalize_project_config({
            "schemaVersion": 5,
            "collectionPath": "Projects/Test",
            "collectionKey": "AAAA1111",
            "referenceDir": "papers/reference",
            "manifestFile": "papers/reference/papers.json",
            "bibFile": "papers/references.bib",
            "claimsFile": "papers/claims.json",
            "fallback": "copy",
        })
        (root / "papers" / "reference").mkdir(parents=True)
        zpp.json_dump({"papers": [{
            "itemKey": "ITEM0001", "title": "A Paper", "DOI": "10.1234/example",
            "pdfStatus": "missing", "projectPath": None,
        }]}, root / cfg["manifestFile"])
        return cfg

    def test_validate_bibtex(self):
        raw = '@article{smith2026demo, title={Demo}, year={2026}}'
        result = zpp.validate_bibtex_text(raw)
        self.assertTrue(result["validBibtex"])
        self.assertEqual(result["entryKey"], "smith2026demo")

    def test_record_citation_preserves_raw_bibtex_and_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = self._project(root)
            raw = '@article{officialKey, title={A Paper}, doi={10.1234/example}}'
            result = zpp.record_citation_provenance(
                root, cfg, item_key="ITEM0001", raw_bibtex=raw,
                source={"type": "doi-registry", "provider": "DOI", "url": "https://doi.org/10.1234/example", "retrievedAt": zpp.utc_now()},
            )
            self.assertTrue(result["recorded"])
            ledger = zpp.load_citations(root, cfg)
            row = ledger["citations"]["ITEM0001"]
            self.assertEqual(row["sources"][0]["rawBibtex"], raw)
            self.assertEqual(row["sources"][0]["source"]["type"], "doi-registry")
            self.assertEqual(cfg["bibFile"], "papers/references.bib")

    def test_higher_authority_source_becomes_preferred_without_rewriting_project_bib(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = self._project(root)
            zpp.record_citation_provenance(root, cfg, item_key="ITEM0001", raw_bibtex='@article{doiKey,title={A}}', source={"type":"doi-registry","provider":"DOI","retrievedAt":zpp.utc_now()})
            result = zpp.record_citation_provenance(root, cfg, item_key="ITEM0001", raw_bibtex='@inproceedings{cvfKey,title={A}}', source={"type":"proceedings","provider":"CVF","retrievedAt":zpp.utc_now()})
            self.assertEqual(result["preferredSource"]["type"], "proceedings")
            self.assertFalse((root / cfg["bibFile"]).exists())

    def test_citation_audit_reports_missing_and_doi_resolvable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = self._project(root)
            out = zpp.citation_audit_local(root, cfg)
            self.assertEqual(out["paperCount"], 1)
            self.assertEqual(out["missingCitationProvenance"], 1)
            self.assertEqual(out["doiResolvableItemKeys"], ["ITEM0001"])


if __name__ == "__main__":
    unittest.main()
