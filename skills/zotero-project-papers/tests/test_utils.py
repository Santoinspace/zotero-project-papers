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


if __name__ == "__main__":
    unittest.main()
