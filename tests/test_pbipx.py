#!/usr/bin/env python3
"""Regression tests. Run: python3 tests/test_pbipx.py  (stdlib unittest, no installs)."""
import copy
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
        self.assertIn("impact: Affects 7 visuals on 1 page: Overview.", out)
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

    def test_docs_measures_are_collapsed_and_filterable(self):
        files = docs.build(self.pack, self.tmp / "campaign-measures" / "docs")
        html = files[1].read_text(encoding="utf-8")
        cards = dict(re.findall(r'<details class="measure" id="([^"]+)"([^>]*)>', html))
        self.assertEqual(len(cards), 10)
        self.assertFalse([i for i, attrs in cards.items() if "open" in re.sub(r'"[^"]*"', "", attrs)])   # one line each until opened
        self.assertIn('data-used="0"', cards["m-old-bounce-rate"])
        self.assertIn('data-used="1"', cards["m-open-rate"])
        self.assertIn('data-folder="Rates"', cards["m-open-rate"])
        summary = re.search(r'<details class="measure" id="m-open-rate"[^>]*><summary>(.*?)</summary>', html, re.S).group(1)
        self.assertIn("Open Rate", summary)
        self.assertIn("Share of recipients who opened the message.", summary)   # the line says what the measure is
        self.assertIn("1 visual", summary)                                      # ... and how far it reaches
        body = re.search(r'<details class="measure" id="m-open-rate".*?</details>', html, re.S).group(0)
        self.assertIn("DIVIDE", body)                                           # the formula is inside the card
        self.assertIn('id="mfilter"', html)                                     # filter: text, used / not used, folder
        self.assertIn('data-used="0">Not used', html)
        self.assertIn("beforeprint", html)                                      # printing opens every card

    def test_findings_read_like_sentences(self):
        fs = {f["code"]: f for f in self.model["analysis"]["findings"]}
        for f in fs.values():
            self.assertNotIn("(s)", f["message"], f["code"])                 # "1 relationship filters", not "1 relationship(s) filter"
        self.assertEqual(fs["bidirectional-relationship"]["message"], "1 relationship filters in both directions.")
        self.assertEqual(fs["unused-measures"]["message"], "1 measure is not used by the packaged report.")
        self.assertEqual(fs["unresolved-dax-reference"]["objects"], ["Engagement[Bounces] in [Old Bounce Rate]"])   # no internal ids
        files = docs.build(self.pack, self.tmp / "campaign-health" / "docs")
        html = files[1].read_text(encoding="utf-8")
        health = re.search(r'<section id="health">(.*?)</section>', html, re.S).group(1)
        text = re.sub(r"<[^>]+>", " ", health)
        self.assertNotIn("(s)", text)
        self.assertNotIn("bidirectional-relationship", text)                 # the code is a tooltip, not the headline
        self.assertIn('title="bidirectional-relationship"', health)
        self.assertIn('<a href="#m-old-bounce-rate">', health)               # objects lead to their description
        self.assertIn('<a href="#c-engagement-division-key">', health)

    def test_thin_report_docs_have_no_empty_sections(self):
        out = self.tmp / "thin-docs"
        extract(EXAMPLES / "thin-report", out)
        files = docs.build(load_pack(out), out / "docs")
        md, html = files[0].read_text(encoding="utf-8"), files[1].read_text(encoding="utf-8")
        sections = dict(re.findall(r'<section id="([^"]+)">(.*?)</section>', html, re.S))
        # without the model there is nothing to say about tables, DAX, sources or health: no bare headings
        self.assertEqual(list(sections), ["overview", "observed", "pages", "appendix"])
        for sid, body in sections.items():
            self.assertGreater(len(re.sub(r"<[^>]+>", " ", body).split()), 12, sid)
        self.assertIn('<a href="#observed">', html)                  # what the report uses of the model has its own nav entry
        self.assertIn("Open Rate", sections["observed"])
        self.assertNotIn("Observed model", sections["pages"])
        self.assertIn("not in the package", sections["overview"])    # and the overview says what is missing and why
        for href in re.findall(r'href="#([^"]+)"', html):
            self.assertIn(f'id="{href}"', html, href)                # no link into a section that is not there
        # Markdown: the same, and the contents list matches the headings
        self.assertIsNone(re.search(r"^## [^\n]+\n\s*## ", md, re.M))
        heads = re.findall(r"^## (.+)$", md, re.M)
        self.assertEqual(heads, ["Contents", "Overview", "Observed model (live connection)", "Report pages", "Appendix"])
        self.assertEqual(re.findall(r"^- \[(.+?)\]\(#", md, re.M), heads[1:])

    def test_full_model_docs_keep_all_sections(self):
        files = docs.build(self.pack, self.tmp / "campaign-sections" / "docs")
        html = files[1].read_text(encoding="utf-8")
        self.assertEqual(re.findall(r'<section id="([^"]+)">', html),
                         ["overview", "model", "lineage", "pages", "glossary", "tables", "measures", "security", "sources", "health", "appendix"])
        for href in re.findall(r'href="#([^"]+)"', html):
            self.assertIn(f'id="{href}"', html, href)

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
        self.assertIn("*Impact:* Affects 7 visuals on 1 page: Overview.", md)
        # self-contained: no external scripts, stylesheets or fonts
        self.assertIsNone(re.search(r'(src|href)="https?://', html))
        self.assertTrue((self.tmp / "campaign" / "docs" / "model-diagram.svg").is_file())
        json.loads(re.search(r'id="search-index">(.*?)</script>', html, re.S).group(1))

    def test_docs_brief_and_section_order(self):
        files = docs.build(self.pack, self.tmp / "campaign-brief" / "docs")
        html = files[1].read_text(encoding="utf-8")
        md = files[0].read_text(encoding="utf-8")
        brief = dict(docs.model_brief(docs.Ctx(self.pack)))
        self.assertIn("Engagement", brief["Model"])                 # the fact table is named
        self.assertIn("7 tables", brief["Model"])
        self.assertIn("10 measures", brief["Measures"])
        self.assertIn("Total Opens (5 visuals)", brief["Measures"])
        self.assertIn("Databricks", brief["Sources"])
        self.assertIn("3 pages (1 hidden)", brief["Report"])
        self.assertIn("both directions", brief["Worth knowing"])
        self.assertIn('id="brief"', html)
        self.assertIn("**In brief** *(auto)*", md)
        self.assertNotIn("see the skill", html)                     # no developer instruction as the first sentence
        self.assertIn(".embed header.doc", html)                    # inside the app the docs drop their own title block
        self.assertIn("embed=1", html)
        # the visual explanation comes first, the reference sections after it
        pos = [html.index(f'<section id="{s}"') for s in ("overview", "model", "lineage", "pages", "glossary", "tables", "measures")]
        self.assertEqual(pos, sorted(pos))

    def test_docs_diagram_panel_and_lineage_graph(self):
        files = docs.build(self.pack, self.tmp / "campaign-lineage" / "docs")
        html = files[1].read_text(encoding="utf-8")
        panel = json.loads(re.search(r'class="erd-data">(.*?)</script>', html, re.S).group(1))
        self.assertIn("Opens", [c[0] for c in panel["Engagement"]["c"]])
        self.assertIn("Open Rate", [m[0] for m in panel["_Measures"]["m"]])
        self.assertTrue(any("Date[Date]" in j for j in panel["Engagement"]["j"]))
        self.assertIn("e.ctrlKey", html)                            # the wheel zooms only with Ctrl, the page keeps scrolling
        self.assertIn('tabindex="0"', html)                         # tables in the diagram are reachable by keyboard
        g = json.loads(re.search(r'id="lineage-data">(.*?)</script>', html, re.S).group(1))
        ids = [n[0] for n in g["n"]]
        edges = {(ids[a], ids[b]) for a, b in g["e"]}
        for e in (("c:Engagement[Opens]", "m:Total Opens"), ("m:Total Opens", "m:Open Rate"),
                  ("m:Total Opens", "r:Opens per Recipient"), ("m:Open Rate", "v:Overview/kpiOpenRate"),
                  ("r:Opens per Recipient", "v:Overview/kpiOpensPerRecipient")):
            self.assertIn(e, edges)
        self.assertIn('data-node="m:Open Rate"', html)              # every measure card opens the graph on itself
        # the graph reaches the same visuals as the change impact shown on the cards
        out = {}
        for a, b in g["e"]:
            out.setdefault(a, []).append(b)
        for t in self.pack["semanticModels"][0]["tables"]:
            for me in t["measures"]:
                start = ids.index("m:" + me["name"])
                seen, todo = {start}, [start]
                while todo:
                    for y in out.get(todo.pop(), []):
                        if y not in seen:
                            seen.add(y)
                            todo.append(y)
                visuals = sum(1 for i in seen if g["n"][i][2] == "v")
                self.assertEqual(visuals, me["impact"]["counts"]["visuals"], me["name"])

    def test_docs_ids_unique_when_pages_share_a_name(self):
        pack = copy.deepcopy(self.pack)
        second = copy.deepcopy(pack["reports"][0])
        second["name"] = "Second report"                             # same page names as the first report
        pack["reports"].append(second)
        html = docs.render_html(docs.Ctx(pack))
        ids = re.findall(r'\sid="([^"]+)"', html)
        self.assertEqual(sorted({i for i in ids if ids.count(i) > 1}), [])
        targets = re.findall(r'<li class="[^"]*"><a href="#([^"]+)"', html)
        self.assertEqual(len(targets), len(set(targets)))            # every nav entry leads to its own place
        for t in targets:
            self.assertIn(t, ids)

    def test_docs_search_reaches_the_column(self):
        files = docs.build(self.pack, self.tmp / "campaign-search" / "docs")
        html = files[1].read_text(encoding="utf-8")
        idx = json.loads(re.search(r'id="search-index">(.*?)</script>', html, re.S).group(1))
        col = next(x for x in idx if x["n"] == "Engagement[Opens]")
        table = next(x for x in idx if x["n"] == "Engagement" and x["k"] == "table")
        self.assertNotEqual(col["a"], table["a"])                    # the hit is the column row, not the top of the table
        self.assertIn(f'<tr id="{col["a"]}"', html)
        self.assertIn("ArrowDown", html)                             # arrow keys and Enter work in the search box

    def test_docs_do_not_show_embedded_data(self):
        files = docs.build(self.pack, self.tmp / "campaign-embedded" / "docs")
        md, html = files[0].read_text(encoding="utf-8"), files[1].read_text(encoding="utf-8")
        exprs = [p["expression"] for t in self.pack["semanticModels"][0]["tables"] for p in t["partitions"] if p.get("expression")]
        blobs = [b for e in exprs for b in re.findall(r'Binary\.FromText\(\s*"([^"]+)"', e)]
        self.assertTrue(blobs)                                       # the example has an Enter-data table
        for b in blobs:
            self.assertNotIn(b, html)
            self.assertNotIn(b, md)
        self.assertIn("embedded data not shown", html)
        self.assertIn("embedded data not shown", md)

    def test_answers_for_the_app(self):
        q = Q(self.pack)

        def refs(a):
            out = []

            def walk(items):
                for i in items:
                    if i.get("ref"):
                        out.append((i["ref"]["k"], i["ref"]["n"]))
                    walk(i.get("children") or [])
            for b in a["blocks"]:
                walk(b.get("items") or [])
            return out

        a = q.answer("open rate")                                    # no question type needed, case does not matter
        self.assertEqual((a["kind"], a["title"]), ("Measure", "Open Rate"))
        labels = [b["label"] for b in a["blocks"]]
        for label in ("Formula (DAX)", "Built from", "Shown in", "If it changes"):
            self.assertIn(label, labels)
        self.assertIn(("measure", "Total Opens"), refs(a))
        self.assertIn(("page", "Overview"), refs(a))
        self.assertIn("DIVIDE", a["text"])                           # plain text for the Copy button
        self.assertEqual(q.answer("Engagement[Opens]")["kind"], "Column")
        self.assertIn(("measure", "Total Opens"), refs(q.answer("Engagement[Opens]")))
        t = q.answer("Engagement (table)")
        self.assertEqual((t["kind"], t["title"]), ("Table", "Engagement"))
        self.assertIn(("column", "Engagement[Opens]"), refs(t))
        self.assertEqual(q.answer("Overview (page)")["kind"], "Page")
        # a typo gets suggestions, not a command-line hint
        miss = q.answer("Opnes")
        self.assertEqual(miss["kind"], "Search")
        self.assertIn(("column", "Engagement[Opens]"), refs(miss))
        self.assertNotIn("Try: find", json.dumps(miss))
        self.assertEqual(q.answer("")["kind"], "Search")
        self.assertEqual(q.answer("zzzz-nothing")["kind"], "Search")
        # a name that is both a table and a column lets the user choose
        both = q.answer("Division")
        self.assertEqual(both["kind"], "Search")
        self.assertTrue({("table", "Division"), ("column", "Division[Division]")} <= set(refs(both)))
        path = q.answer_path("Division", "Campaign")
        self.assertEqual(path["kind"], "Join")
        self.assertIn(("table", "Engagement"), refs(path))
        self.assertEqual(q.answer_path("Division", "Nope")["kind"], "Search")
        self.assertIn(("measure", "Old Bounce Rate"), refs(q.answer_unused()))


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
