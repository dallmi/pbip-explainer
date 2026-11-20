#!/usr/bin/env python3
"""Regression tests for scripts/build_copilot_skills.py (Copilot Studio packages).

Builds the packages into a temporary folder and checks them the way the host would:
limits, file types, front matter, that the packaged tool still runs from the zip
(mode B), and that the knowledge files for the Q&A agent are complete (mode A).

Run: python3 tests/test_build_copilot_skills.py   (stdlib unittest, no installs)
"""
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "scripts" / "build_copilot_skills.py"
EXAMPLE = ROOT / "examples" / "campaign-analytics"
# Sister project with the same Copilot Studio packaging; its limits are the reference
SISTER_BUILD = ROOT.parent.parent / "html2pbi" / "scripts" / "build_copilot_skills.py"
# Employer-name check from Arbeit/CLAUDE.md, also as part of identifiers
BRAND_RE = re.compile(r"(^|[^A-Za-z])[Uu][Bb][Ss]|[a-z]U[Bb][Ss]([A-Z_]|$)", re.M)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


b = load(BUILD, "build_copilot_skills")
TMP = None


def build(*args, out=None):
    out = out or TMP / "out"
    return subprocess.run([sys.executable, str(BUILD), "--out", str(out), *map(str, args)],
                          capture_output=True, text=True)


def setUpModule():
    global TMP
    TMP = Path(tempfile.mkdtemp())
    if not EXAMPLE.is_dir():
        subprocess.run([sys.executable, str(ROOT / "examples" / "generate_examples.py")], check=True, capture_output=True)
    proc = build()
    assert proc.returncode == 0, proc.stderr


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def example_zip(dest):
    """The synthetic example as the user would hand it over: one zip of the project folder."""
    z = dest / "CampaignAnalytics.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(EXAMPLE.rglob("*")):
            if p.is_file():
                zf.write(p, (Path(EXAMPLE.name) / p.relative_to(EXAMPLE)).as_posix())
    return z


class PackageTests(unittest.TestCase):
    def test_writes_every_output(self):
        names = {p.name for p in (TMP / "out").iterdir()}
        expected = {f"{s}.zip" for s in b.SKILLS} | set(b.INSTRUCTIONS) | {"build-report.txt", "manifest.json"}
        self.assertLessEqual(expected, names, expected - names)

    def test_package_passes_host_limits(self):
        for skill in b.SKILLS:
            d = TMP / "unpacked" / skill
            with zipfile.ZipFile(TMP / "out" / f"{skill}.zip") as z:
                z.extractall(d)
            self.assertEqual(b.problems(d), [])
            self.assertLessEqual((TMP / "out" / f"{skill}.zip").stat().st_size, b.MAX_ZIP_BYTES)

    def test_skill_md_has_environment_notes_and_valid_front_matter(self):
        for skill in b.SKILLS:
            text = zipfile.ZipFile(TMP / "out" / f"{skill}.zip").read("SKILL.md").decode()
            fields, body = b.split_front_matter(text)
            self.assertEqual(fields["name"], skill)
            self.assertTrue(text.split("\n")[2].startswith('description: "'), "description must be quoted")
            self.assertIn("## Environment: Copilot Studio", body)
            self.assertLess(body.index("## Environment: Copilot Studio"), body.index("# pbip-explain"))

    def test_package_holds_the_tool_and_no_clutter(self):
        names = zipfile.ZipFile(TMP / "out" / "pbip-explain.zip").namelist()
        self.assertIn("scripts/pbip_explain.py", names)
        self.assertIn("scripts/pbipx/__init__.py", names)
        self.assertIn("references/answering-questions.md", names)
        self.assertFalse([n for n in names if "__pycache__" in n or n.endswith(".pyc")
                          or any(part.startswith(".") for part in n.split("/"))], names)

    def test_instructions_within_limit(self):
        for target in b.INSTRUCTIONS:
            text = (TMP / "out" / target).read_text(encoding="utf-8")
            self.assertLessEqual(len(text), b.MAX_INSTRUCTIONS_CHARS, target)

    def test_rebuild_reports_nothing_changed(self):
        proc = build()
        rows = [l for l in proc.stdout.splitlines() if l.split(" ")[0] in b.SKILLS or l.startswith("agent-instructions")]
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(rows), len(b.SKILLS) + len(b.INSTRUCTIONS), proc.stdout)
        self.assertTrue(all(l.endswith(" no") for l in rows), proc.stdout)

    def test_limit_violations_are_reported(self):
        d = TMP / "bad" / "bad-skill"
        (d / "a" / "b" / "c" / "d").mkdir(parents=True)
        (d / "SKILL.md").write_text(b.join_front_matter({"name": "bad-skill", "description": "x" * 1100}, "\nbody\n"))
        (d / "a" / "b" / "c" / "d" / "deep.md").write_text("x")
        (d / "notes.dax").write_text("x")
        found = " | ".join(b.problems(d))
        for part in ("description", "deeper than", "file type .dax"):
            self.assertIn(part, found)

    @unittest.skipUnless(SISTER_BUILD.is_file(), "html2pbi not next to this project")
    def test_limits_match_html2pbi(self):
        sister = load(SISTER_BUILD, "html2pbi_build")
        for name in ("MAX_SKILLS", "MAX_SKILL_MD_CHARS", "MAX_DESCRIPTION_CHARS", "MAX_COMPANIONS", "MAX_FILE_BYTES",
                     "MAX_COMPANION_BYTES", "MAX_ZIP_BYTES", "MAX_DEPTH", "MAX_INSTRUCTIONS_CHARS", "ALLOWED"):
            self.assertEqual(getattr(b, name), getattr(sister, name), name)


class ModeBTests(unittest.TestCase):
    """The packaged tool runs from the unzipped skill on a zip the user attaches."""

    def test_packaged_tool_extracts_and_answers(self):
        work = TMP / "mode-b"
        skill = work / "skill"
        with zipfile.ZipFile(TMP / "out" / "pbip-explain.zip") as z:
            z.extractall(skill)
        src = example_zip(work)
        cli = skill / "scripts" / "pbip_explain.py"
        proc = subprocess.run([sys.executable, "-I", str(cli), "run", str(src), "--out", str(work / "w")],
                              capture_output=True, text=True, cwd=work)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((work / "w" / "knowledge" / "agent-brief.md").is_file())
        self.assertTrue(list((work / "w" / "docs").glob("*-documentation.html")))
        proc = subprocess.run([sys.executable, "-I", str(cli), "query", str(work / "w"), "measure", "Open Rate"],
                              capture_output=True, text=True, cwd=work)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("DIVIDE", proc.stdout)


class ModeATests(unittest.TestCase):
    """Knowledge files for the Q&A agent, built from an extracted pack."""

    @classmethod
    def setUpClass(cls):
        cls.pack = TMP / "mode-a" / "campaign"
        proc = subprocess.run([sys.executable, "-I", str(ROOT / "skills" / "pbip-explain" / "scripts" / "pbip_explain.py"),
                               "run", str(EXAMPLE), "--out", str(cls.pack)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        cls.proc = build("--knowledge", cls.pack, "--with-model-json", out=TMP / "mode-a" / "out")
        cls.files = sorted(p.name for p in (cls.pack / b.KNOWLEDGE_DIR).iterdir())

    def test_knowledge_files_written_with_prefix(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stderr)
        prefix = b.pack_prefix(self.pack)
        self.assertEqual(prefix, "campaign-analytics")
        for n in b.KNOWLEDGE_FILES + ["model.json", "CampaignAnalytics-documentation.md"]:
            self.assertIn(f"{prefix}-{n}", self.files)
        self.assertNotIn(str(ROOT), self.proc.stdout + (TMP / "mode-a" / "out" / "build-report.txt").read_text())

    def test_knowledge_files_pass_limits(self):
        files = {n: self.pack / b.KNOWLEDGE_DIR / n for n in self.files}
        self.assertEqual(b.knowledge_problems(files), [])

    def test_qa_instructions_describe_every_file(self):
        text = b.INSTRUCTIONS["agent-instructions-qa.txt"].read_text(encoding="utf-8")
        for n in b.KNOWLEDGE_FILES + ["model.json", "<Report>-documentation.md"]:
            self.assertIn(f"`<pack>-{n}`", text)

    def test_missing_pack_is_refused_and_nothing_written(self):
        empty = TMP / "mode-a" / "empty"
        empty.mkdir()
        out = TMP / "mode-a" / "refused"
        proc = build("--knowledge", empty, out=out)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("knowledge/model.json", proc.stderr)
        self.assertFalse(out.exists())


class PublicSafeTests(unittest.TestCase):
    def test_no_employer_name_in_built_files(self):
        texts = {}
        for target in b.INSTRUCTIONS:
            texts[target] = (TMP / "out" / target).read_text(encoding="utf-8")
        with zipfile.ZipFile(TMP / "out" / "pbip-explain.zip") as z:
            for n in z.namelist():
                texts[n] = z.read(n).decode("utf-8")
        hits = [f"{n}: {m.group(0)!r}" for n, t in texts.items() for m in [BRAND_RE.search(t)] if m]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
