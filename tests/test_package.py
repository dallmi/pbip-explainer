#!/usr/bin/env python3
"""Tests for the cross-model analysis and the SharePoint / Agent Builder package. Run: python3 tests/test_package.py"""
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skills" / "pbip-explain" / "scripts"))

from pbipx import package, portfolio  # noqa: E402
from pbipx.pack import extract, load_pack  # noqa: E402

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_text(data: bytes):
    """(headings, full text) of a .docx, parsed with the standard library only."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        for part in ("[Content_Types].xml", "_rels/.rels", "word/document.xml", "word/styles.xml", "docProps/core.xml"):
            assert part in names, part
            ET.fromstring(zf.read(part))                       # well-formed XML
        root = ET.fromstring(zf.read("word/document.xml"))
    heads, text = [], []
    for p in root.iter(W + "p"):
        style = p.find(f"{W}pPr/{W}pStyle")
        t = "".join(x.text or "" for x in p.iter(W + "t"))
        text.append(t)
        if style is not None and style.get(W + "val", "").startswith("Heading"):
            heads.append(t)
    return heads, "\n".join(text)


class PackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run([sys.executable, str(ROOT / "examples" / "generate_examples.py")], check=True, capture_output=True)
        cls.tmp = Path(tempfile.mkdtemp(prefix="pbipx-pkg-"))
        # a second copy of the campaign model with one measure changed -> a cross-model conflict
        variant = cls.tmp / "variant" / "campaign-analytics"
        shutil.copytree(ROOT / "examples" / "campaign-analytics", variant)
        meas = variant / "CampaignAnalytics.SemanticModel" / "definition" / "tables" / "_Measures.tmdl"
        meas.write_text(meas.read_text(encoding="utf-8").replace(
            "measure 'Open Rate' = DIVIDE ( [Total Opens], [Recipients] )",
            "measure 'Open Rate' = DIVIDE ( [Unique Visitors], [Recipients] )"), encoding="utf-8")
        packs = []
        for name, src in (("campaign", ROOT / "examples" / "campaign-analytics"), ("variant", variant),
                          ("legacy", ROOT / "examples" / "legacy-sales")):
            extract(src, cls.tmp / name)
            packs.append({"pack": load_pack(cls.tmp / name), "enrichment": None, "run": name})
        packs[0]["enrichment"] = {"report": {"purpose": "Campaign engagement."}}
        cls.packs = packs
        cls.units = portfolio.collect_units(packs)
        cls.pf = portfolio.analyse(cls.units)
        cls.data, cls.man = package.build(packs, "Test catalog")
        cls.zf = zipfile.ZipFile(io.BytesIO(cls.data))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_unique_model_keys(self):
        self.assertEqual([u["key"] for u in self.units], ["CampaignAnalytics", "CampaignAnalytics (2)", "LegacySales"])

    def test_cross_model(self):
        conflicts = {c["name"]: c for c in self.pf["measureConflicts"]}
        self.assertIn("Open Rate", conflicts)
        self.assertEqual(len(conflicts["Open Rate"]["variants"]), 2)
        shared = {s["name"] for s in self.pf["sharedMeasures"]}
        self.assertIn("Total Opens", shared)                      # identical in both copies
        self.assertNotIn("Open Rate", shared)
        self.assertIn("Engagement", {t["name"] for t in self.pf["sharedTables"]})
        self.assertTrue(any("fact_engagement" in s["label"] for s in self.pf["sharedSources"]))
        self.assertTrue(any("campaign" in s["label"].lower() for s in self.pf["sharedSources"]))   # native SQL table

    def test_norm_dax(self):
        self.assertEqual(portfolio.norm_dax("SUM ( T[a] ) // x"), portfolio.norm_dax("sum(T[a])"))
        self.assertNotEqual(portfolio.norm_dax('T[a] IN {"Join"}'), portfolio.norm_dax('T[a] IN {"Leave"}'))

    def test_layout(self):
        names = self.zf.namelist()
        know = sorted(n for n in names if "/Knowledge/" in n)
        self.assertEqual(len(know), 1 + 3 * 3)
        self.assertTrue(all(n.endswith(".docx") for n in know))
        self.assertIn("Test catalog/Knowledge/00 Catalog.docx", names)
        self.assertIn("Test catalog/Knowledge/CampaignAnalytics (2) - 3 Measures.docx", names)
        self.assertIn("Test catalog/Agent-Instructions.txt", names)
        self.assertIn("Test catalog/README.txt", names)
        self.assertIn("Test catalog/Machine-readable/LegacySales/model.json", names)
        self.assertEqual(self.man["knowledgeFiles"], 10)
        # Agent Builder instructions must stay below 8,000 characters
        self.assertLess(len(self.zf.read("Test catalog/Agent-Instructions.txt")), 8000)

    def test_docx_content(self):
        heads, text = docx_text(self.zf.read("Test catalog/Knowledge/00 Catalog.docx"))
        self.assertIn("Measures with the same name but different logic", heads)
        self.assertIn("[Open Rate] differs across models: CampaignAnalytics, CampaignAnalytics (2)", heads)
        self.assertIn("Campaign engagement.", text)
        heads, text = docx_text(self.zf.read("Test catalog/Knowledge/CampaignAnalytics - 3 Measures.docx"))
        self.assertIn("Measure [Click-to-Open Rate] — CampaignAnalytics", heads)
        self.assertIn("DIVIDE ( [Total Clicks], _opens )", text)
        heads, text = docx_text(self.zf.read("Test catalog/Knowledge/CampaignAnalytics - 2 Data model and sources.docx"))
        self.assertIn("Relationships and join keys in CampaignAnalytics", heads)
        self.assertIn("Engagement[Send Date]", text)
        heads, _ = docx_text(self.zf.read("Test catalog/Knowledge/LegacySales - 1 Overview and pages.docx"))
        self.assertIn("Page 'Sales Overview' — LegacySales", heads)

    def test_xml_safety(self):
        d = package.Docx("t")
        d.p("bad \x01 char & <tag> \"q\"")
        _, text = docx_text(d.to_bytes())
        self.assertIn("bad  char & <tag>", text)

    def test_safe_filename(self):
        self.assertEqual(package.safe_filename('A/B: "C"*?'), "A-B- -C")


if __name__ == "__main__":
    unittest.main(verbosity=1)
