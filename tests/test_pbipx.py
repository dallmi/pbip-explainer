#!/usr/bin/env python3
"""Regression tests. Run: python3 tests/test_pbipx.py  (stdlib unittest, no installs)."""
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
SCRIPTS = ROOT / "skills" / "pbip-explain" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pbipx import dax, mquery, tmdl  # noqa: E402
from pbipx.analysis import _attach_impact, relationship_path  # noqa: E402
from pbipx.pack import extract, load_pack  # noqa: E402
from pbipx.query import Q  # noqa: E402
from pbipx.unpack import UnsafeArchive, safe_extract  # noqa: E402
from pbipx import docs  # noqa: E402

EXAMPLES = ROOT / "examples"


def setUpModule():
    subprocess.run([sys.executable, str(EXAMPLES / "generate_examples.py")], check=True, capture_output=True)


class TmdlTests(unittest.TestCase):
    def test_measures_flags_descriptions(self):
        src = (
            "table 'My Table'\n"
            "\tlineageTag: abc\n\n"
            "\t/// Total of things\n"
            "\tmeasure 'It''s Total' = SUM('My Table'[Amount])\n"
            "\t\tformatString: #,0\n"
            "\t\tisHidden\n\n"
            "\tmeasure Multi =\n"
            "\t\t\tVAR x = 1\n"
            "\t\t\tRETURN\n"
            "\t\t\t    x + 1\n"
            "\t\tdisplayFolder: KPIs\n\n"
            "\tmeasure Fenced = ```\n"
            "\t\t\tCALCULATE ( [Multi] )\n"
            "\t\t\t```\n\n"
            "\tcolumn Amount\n"
            "\t\tdataType: decimal\n"
            "\t\tsummarizeBy: sum\n\n"
            "\t\tannotation SummarizationSetBy = Automatic\n\n"
            "\tpartition p = m\n"
            "\t\tmode: import\n"
            "\t\tsource =\n"
            "\t\t\t\tlet\n"
            "\t\t\t\t    Source = 1\n"
            "\t\t\t\tin\n"
            "\t\t\t\t    Source\n"
        )
        doc = tmdl.parse(src)
        t = doc.children[0]
        self.assertEqual(t.name, "My Table")
        ms = t.all("measure")
        self.assertEqual(ms[0].name, "It's Total")
        self.assertEqual(ms[0].description, "Total of things")
        self.assertTrue(ms[0].props.get("isHidden"))
        self.assertEqual(ms[1].expr, "VAR x = 1\nRETURN\n    x + 1")
        self.assertEqual(ms[1].props["displayFolder"], "KPIs")
        self.assertEqual(ms[2].expr.strip(), "CALCULATE ( [Multi] )")
        col = t.child("column")
        self.assertEqual(col.props["dataType"], "decimal")
        self.assertEqual(col.child("annotation").name, "SummarizationSetBy")
        part = t.child("partition")
        self.assertEqual(part.expr, "m")
        self.assertIn("Source = 1", part.props["source"])

    def test_object_ref(self):
        self.assertEqual(tmdl.parse_object_ref("'Org Bridge'.'Row ID'"), ("Org Bridge", "Row ID"))
        self.assertEqual(tmdl.parse_object_ref("Sales.Amount"), ("Sales", "Amount"))
        self.assertEqual(tmdl.parse_object_ref("Visits.'Article Key'"), ("Visits", "Article Key"))


class DaxTests(unittest.TestCase):
    def setUp(self):
        self.cat = dax.Catalog([
            {"name": "Sales", "columns": [{"name": "Amount"}, {"name": "Qty"}, {"name": "Date"}],
             "measures": [{"name": "Revenue"}, {"name": "Margin %"}]},
            {"name": "Date", "columns": [{"name": "Date"}, {"name": "Year"}], "measures": []},
        ])

    def test_refs(self):
        r = dax.analyse("// [Ignored]\nVAR x = \"[NotAMeasure]\" RETURN DIVIDE([Revenue], SUMX(Sales, [Qty]) ) + "
                        "CALCULATE([Margin %], 'Date'[Year] = 2024, USERELATIONSHIP(Sales[Date], 'Date'[Date]))",
                        self.cat, "Sales")
        self.assertEqual(sorted(r["measures"]), ["Margin %", "Revenue"])
        self.assertIn(["Sales", "Qty"], r["columns"])
        self.assertIn(["Date", "Year"], r["columns"])
        self.assertIn("Sales", r["tables"])
        self.assertEqual(r["userelationship"], [[["Sales", "Date"], ["Date", "Date"]]])
        self.assertNotIn("[Ignored]", r["unresolved"])
        self.assertIn("SUMX", r["functions"])

    def test_unresolved(self):
        r = dax.analyse("SUM(Sales[Bounces])", self.cat)
        self.assertEqual(r["unresolved"], ["Sales[Bounces]"])

    def test_auto_describe(self):
        self.assertEqual(dax.auto_describe("SUM ( Sales[Amount] )"), "Sum of Sales[Amount]")
        self.assertIn("divided by", dax.auto_describe("DIVIDE([Revenue], [Margin %])"))
        self.assertIsNone(dax.auto_describe("VAR a = 1\nVAR b = 2\nVAR c = 3\nRETURN a + b + c"))


class MTests(unittest.TestCase):
    def test_databricks_navigation_and_filter(self):
        m = ('let Source = Databricks.Catalogs(Host, HttpPath, [Catalog = null]),\n'
             'c = Source{[Name="comms",Kind="Database"]}[Data],\n'
             't = c{[Name="fact",Kind="Table"]}[Data],\n'
             '#"Only valid" = Table.SelectRows(t, each [ok] = true)\nin #"Only valid"')
        a = mquery.analyse(m, ["Host", "HttpPath"])
        self.assertEqual(a["sources"][0]["kind"], "Databricks")
        self.assertEqual([n["Name"] for n in a["navigation"]], ["comms", "fact"])
        self.assertEqual(a["rowFilters"][0]["condition"], "each [ok] = true")
        self.assertEqual(sorted(a["references"]), ["Host", "HttpPath"])

    def test_native_query_and_join(self):
        m = ('let S = Sql.Database("srv", "db"), Q = Value.NativeQuery(S, "SELECT 1"),\n'
             'J = Table.NestedJoin(Q, {"k"}, #"Other Query", {"k2"}, "x", JoinKind.Inner) in J')
        a = mquery.analyse(m, ["Other Query"])
        self.assertEqual(a["sources"][0]["arguments"], ["srv", "db"])
        self.assertEqual(a["nativeQueries"], ["SELECT 1"])
        j = a["joins"][0]
        self.assertEqual((j["leftKeys"], j["right"], j["rightKeys"], j["kind"]), (["k"], "Other Query", ["k2"], "JoinKind.Inner"))
        self.assertEqual(a["references"], ["Other Query"])


class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="pbipx-test-"))
        zpath = cls.tmp / "campaign.zip"
        src = EXAMPLES / "campaign-analytics"
        with zipfile.ZipFile(zpath, "w") as zf:
            for f in src.rglob("*"):
                if f.is_file():
                    zf.write(f, f.relative_to(src.parent).as_posix())
        extract(zpath, cls.tmp / "campaign")
        cls.pack = load_pack(cls.tmp / "campaign")
        cls.model = cls.pack["semanticModels"][0]
        cls.q = Q(cls.pack)
        extract(EXAMPLES / "legacy-sales", cls.tmp / "legacy")
        cls.legacy = load_pack(cls.tmp / "legacy")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def table(self, name):
        return next(t for t in self.model["tables"] if t["name"] == name)

    def test_roles(self):
        roles = {t["name"]: t["role"] for t in self.model["tables"]}
        self.assertEqual(roles, {"Engagement": "fact", "Campaign": "dimension", "Division": "dimension", "Date": "date",
                                 "_Measures": "measureTable", "Time Intelligence": "calculationGroup",
                                 "Metric Selector": "fieldParameter"})

    def test_findings(self):
        codes = {f["code"] for f in self.model["analysis"]["findings"]}
        for c in ("unresolved-report-reference", "bidirectional-relationship", "unresolved-dax-reference",
                  "implicit-measures", "inactive-relationship-used", "unused-measures", "row-level-security",
                  "hidden-pages", "summarize-keys"):
            self.assertIn(c, codes)
        self.assertEqual(self.model["analysis"]["unused"]["measures"], ["Old Bounce Rate"])
        self.assertIn("Engagement[Legacy Flag]", self.model["analysis"]["unused"]["columns"])

    def test_sources(self):
        e = self.table("Engagement")["source"]
        self.assertEqual(e["connectors"][0]["kind"], "Databricks")
        self.assertEqual([n["Name"] for n in e["navigation"]], ["comms", "gold", "fact_engagement"])
        self.assertEqual({p["name"] for p in e["parameters"]}, {"Host", "HttpPath"})
        d = self.table("Division")["source"]
        self.assertEqual(d["joins"][0]["right"], "Region Map")
        self.assertIn("SELECT campaign_key", self.table("Campaign")["source"]["nativeQueries"][0])

    def test_lineage_and_usage(self):
        m = next(x for t in self.model["tables"] for x in t["measures"] if x["name"] == "Click-to-Open Rate")
        self.assertEqual(sorted(m["lineage"]["measures"]), ["Total Clicks", "Total Opens"])
        self.assertIn("Engagement[Opens]", m["lineage"]["columns"])
        self.assertEqual({u["page"] for u in m["usedBy"] if u.get("page")}, {"Campaign Detail"})
        # field parameter keeps referenced measures alive, calc group items are documented
        cg = self.table("Time Intelligence")["calculationGroup"]
        self.assertEqual([i["name"] for i in cg["items"]], ["Current", "YTD", "Prior Year"])

    def test_report(self):
        rep = self.pack["reports"][0]
        pages = {p["displayName"]: p for p in rep["pages"]}
        self.assertEqual(list(pages), ["Overview", "Campaign Detail", "QA"])
        self.assertTrue(pages["QA"]["hidden"])
        self.assertEqual(pages["Campaign Detail"]["pageType"], "Drillthrough")
        self.assertEqual(pages["Overview"]["filters"][0]["condition"], "Campaign[Channel] in (Email; Intranet)")
        trend = next(v for v in pages["Overview"]["visuals"] if v["name"] == "trend")
        self.assertIn(("hierarchyLevel", "Date", "Month"), {(f["kind"], f["table"], f["name"]) for f in trend["fields"]})
        self.assertIn("Campaign engagement overview", next(v for v in pages["Overview"]["visuals"] if v["type"] == "textbox")["text"])

    def test_change_impact(self):
        col = next(c for c in self.table("Engagement")["columns"] if c["name"] == "Opens")
        imp = col["impact"]
        self.assertEqual(imp["counts"]["direct"], 0)
        card = next(e for e in imp["visuals"]["indirect"] if e["title"] == "Open rate")
        self.assertEqual(card["path"][0], "Engagement[Opens]")
        self.assertEqual(card["path"][1:3], ["[Total Opens]", "[Open Rate]"])
        self.assertIn("Overview", imp["pages"])
        tot = next(m for m in self.table("_Measures")["measures"] if m["name"] == "Total Opens")["impact"]
        self.assertEqual({e["title"] for e in tot["visuals"]["direct"]}, {"Campaigns", "Opens over time"})
        self.assertIn("Opens per recipient", {e["title"] for e in tot["visuals"]["indirect"]})   # via report measure
        self.assertEqual(tot["usage"], "direct")
        # a measure only shown through another measure counts as used, indirectly
        rec = next(m for m in self.table("_Measures")["measures"] if m["name"] == "Recipients")
        self.assertTrue(rec["usedInReport"])
        key = next(c for c in self.table("Engagement")["columns"] if c["name"] == "Campaign Key")["impact"]
        self.assertTrue(any("relationship" in o for o in key["other"]))

    def test_reference_status(self):
        st = self.model["analysis"]["referenceStatus"]
        self.assertEqual(st.get("unresolved"), 1)                 # Campaign[Campaign Owner]
        self.assertEqual(st.get("reportMeasure"), 1)              # Opens per Recipient
        rep = self.pack["reports"][0]
        self.assertEqual(rep["reportMeasures"][0]["name"], "Opens per Recipient")
        f = next(f for p in rep["pages"] for v in p["visuals"] for f in v["fields"] if f["name"] == "Opens per Recipient")
        self.assertEqual(f["status"], "reportMeasure")
        sev = {x["code"]: x["severity"] for x in self.model["analysis"]["findings"]}
        self.assertEqual(sev["unresolved-report-reference"], "warning")

    def test_finding_impact(self):
        fs = self.model["analysis"]["findings"]
        by = {f["code"]: f for f in fs}
        prio = [f["priority"] for f in fs]
        self.assertEqual(prio, sorted(prio, key=["high", "medium", "low"].index))       # sorted by priority
        # relationship: visuals reading either table on pages where both are used (Division slicer + Engagement visuals)
        bi = by["bidirectional-relationship"]
        self.assertEqual(bi["priority"], "high")
        self.assertEqual(bi["impact"]["pages"], ["Overview"])
        self.assertEqual(bi["impact"]["visuals"], 7)
        self.assertIn("both tables", bi["impact"]["basis"])
        un = by["unresolved-report-reference"]["impact"]
        self.assertEqual(un["examples"], ["Table 'Campaigns' on 'Campaign Detail'"])
        self.assertEqual(by["implicit-measures"]["impact"]["pages"], ["Overview"])
        # only reaches an unused measure: a warning without report impact
        self.assertEqual(by["unresolved-dax-reference"]["impact"]["visuals"], 0)
        self.assertEqual(by["unresolved-dax-reference"]["priority"], "medium")
        self.assertIsNone(by["unused-measures"]["impact"])
        self.assertEqual(by["unused-measures"]["priority"], "low")
        loc = next(f for f in self.legacy["semanticModels"][0]["analysis"]["findings"] if f["code"] == "local-file-source")
        self.assertEqual((loc["priority"], loc["impact"]["visuals"]), ("high", 2))
        out = self.q.findings("high")
        self.assertIn("bidirectional-relationship", out)
        self.assertIn("impact: Affects 7 visual(s) on 1 page(s): Overview.", out)
        self.assertNotIn("unused-measures", out)
        self.assertIn("high|medium|low", self.q.findings("urgent"))

    def test_finding_impact_through_page_filter(self):
        # a column used only as a page filter reaches every visual on that page
        f = [{"severity": "warning", "code": "x", "message": "", "objects": [], "advice": None,
              "_scope": ("objects", ["column:T[C]"])}]
        vinfo = {f"visual:P1/{n}": {"report": "R", "page": "P1", "visual": n, "visualTitle": n, "visualType": "Card"}
                 for n in ("a", "b")}
        vinfo["visual:P2/c"] = {"report": "R", "page": "P2", "visual": "c", "visualTitle": "c", "visualType": "Card"}
        _attach_impact(f, {"column:T[C]": {"page:P1"}}, vinfo, {}, {})
        self.assertEqual((f[0]["impact"]["visuals"], f[0]["impact"]["pages"], f[0]["priority"]), (2, ["P1"], "high"))
        self.assertNotIn("_scope", f[0])

    def test_thin_report(self):
        out = self.tmp / "thin"
        extract(EXAMPLES / "thin-report", out)
        pk = load_pack(out)
        self.assertEqual(pk["semanticModels"], [])
        om = pk["reports"][0]["observedModel"]
        names = {t["name"]: t for t in om["tables"]}
        self.assertIn("Open Rate", {m["name"] for m in names["_Measures"]["measures"]})
        self.assertNotIn("Opens per Recipient", {m["name"] for m in names["_Measures"]["measures"]})   # report measure
        self.assertIn("Campaign Owner", {c["name"] for c in names["Campaign"]["columns"]})            # not "broken" here
        statuses = {f["status"] for p in pk["reports"][0]["pages"] for v in p["visuals"] for f in v["fields"]}
        self.assertEqual(statuses, {"observedExternal", "reportMeasure"})
        files = docs.build(pk, out / "docs")
        self.assertIn("Observed model (live connection)", files[0].read_text(encoding="utf-8"))

    def test_queries(self):
        self.assertIn("USERELATIONSHIP", self.q.measure("Clicks by Send Date"))
        path = relationship_path(self.model, "Division", "Campaign")
        self.assertEqual([s["to"] for s in path], ["Engagement", "Campaign"])
        imp = self.q.impact("Engagement[Opens]")
        self.assertIn("Open Rate", imp)
        self.assertIn("via [Total Opens] → [Open Rate]", imp)
        self.assertIn("Overview", imp)

    def test_legacy(self):
        m = self.legacy["semanticModels"][0]
        self.assertEqual(m["format"], "bim")
        self.assertEqual(self.legacy["reports"][0]["format"], "legacy")
        v = self.legacy["reports"][0]["pages"][0]["visuals"]
        fields = {(f["table"], f["name"]) for x in v for f in x["fields"] if f.get("role") != "formatting"}
        self.assertEqual(fields, {("Product", "Category"), ("Sales", "Revenue"), ("Sales", "Margin %")})
        self.assertEqual(m["analysis"]["unused"]["measures"], ["Unused Measure"])
        self.assertIn("auto-date-time", {f["code"] for f in m["analysis"]["findings"]})

    def test_docs(self):
        enr = {"report": {"purpose": "Shows campaign engagement."},
               "measures": {"Total Clicks": {"description": "Number of clicks on links in the message."}},
               "glossary": [{"term": "CTOR", "definition": "Click-to-open rate."}]}
        files = docs.build(self.pack, self.tmp / "campaign" / "docs", enr)
        html = files[1].read_text(encoding="utf-8")
        md = files[0].read_text(encoding="utf-8")
        for s in ("Shows campaign engagement.", "Number of clicks on links", "AI-drafted", "CTOR", "erDiagram",
                  "Engagement[Send Date]", "**Change impact.**", "Report-level measures"):
            self.assertIn(s, md)
        self.assertIn('<svg xmlns="http://www.w3.org/2000/svg" class="erd"', html)
        self.assertIn('<span class="prio high">high priority</span>', html)
        self.assertIn("Basis: visuals that read either table", html)
        self.assertIn("*Impact:* Affects 7 visual(s) on 1 page(s): Overview.", md)
        # self-contained: no external scripts, stylesheets or fonts
        self.assertIsNone(re.search(r'(src|href)="https?://', html))
        self.assertTrue((self.tmp / "campaign" / "docs" / "model-diagram.svg").is_file())
        json.loads(re.search(r'id="search-index">(.*?)</script>', html, re.S).group(1))


class SafetyTests(unittest.TestCase):
    def test_path_traversal_rejected(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            z = tmp / "evil.zip"
            with zipfile.ZipFile(z, "w") as zf:
                zf.writestr("../evil.txt", "x")
            with self.assertRaises(UnsafeArchive):
                safe_extract(z, tmp / "out")
            self.assertFalse((tmp / "evil.txt").exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_cache_skipped(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            z = tmp / "p.zip"
            with zipfile.ZipFile(z, "w") as zf:
                zf.writestr("X.SemanticModel/.pbi/cache.abf", "data")
                zf.writestr("X.SemanticModel/.pbi/localSettings.json", "{}")
            skipped = safe_extract(z, tmp / "out")
            self.assertEqual(len(skipped), 2)
            self.assertFalse((tmp / "out" / "X.SemanticModel" / ".pbi" / "cache.abf").exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
