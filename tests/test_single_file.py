#!/usr/bin/env python3
"""The single-file edition must be current and must work from an empty folder. Run: python3 tests/test_single_file.py"""
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import build_single_file  # noqa: E402

SINGLE = ROOT / "single-file" / "pbip_explainer.py"


class SingleFileTests(unittest.TestCase):
    def test_current(self):
        self.assertTrue(SINGLE.is_file(), "run: python3 scripts/build_single_file.py")
        self.assertEqual(SINGLE.read_text(encoding="utf-8"), build_single_file.build(),
                         "single-file/pbip_explainer.py is out of date: run python3 scripts/build_single_file.py")

    def test_size_for_copy_paste(self):
        # GitHub's "Copy raw file" and editors handle this comfortably; keep well below 1 MB
        self.assertLess(SINGLE.stat().st_size, 700 * 1024)

    def test_runs_from_empty_folder(self):
        subprocess.run([sys.executable, str(ROOT / "examples" / "generate_examples.py")], check=True, capture_output=True)
        tmp = Path(tempfile.mkdtemp(prefix="pbipx-single-"))
        try:
            shutil.copy(SINGLE, tmp / "pbip_explainer.py")
            run = lambda *a: subprocess.run([sys.executable, "-I", "pbip_explainer.py", *a], cwd=tmp,  # noqa: E731
                                            capture_output=True, text=True, timeout=120)
            r = run("run", str(ROOT / "examples" / "campaign-analytics"), "--out", "out/c")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue((tmp / "out/c/docs/CampaignAnalytics-documentation.html").is_file())
            r = run("query", "out/c", "measure", "Open Rate")
            self.assertIn("DIVIDE ( [Total Opens], [Recipients] )", r.stdout)
            r = run("package", "out/c", "--out", "pkg.zip", "--name", "T")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("T/Knowledge/00 Catalog.docx", zipfile.ZipFile(tmp / "pkg.zip").namelist())
            r = run("--make-launcher")
            self.assertTrue((tmp / "start-pbip-explainer.bat").is_file())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
