"""Analysis package for a SharePoint folder + Microsoft 365 Copilot Agent Builder.

Agent Builder reads .docx/.pdf/.pptx/.xlsx/.txt from SharePoint (not Markdown), at most 100 files per agent,
and answers from retrieved passages. So the package is Word files with stable names, one topic per file,
every heading carrying the model name, plus a catalog that pre-computes the cross-model answers.

    <name>/
      Knowledge/                      upload to SharePoint, add the folder as agent knowledge
        00 Catalog.docx
        <Model> - 1 Overview and pages.docx
        <Model> - 2 Data model and sources.docx
        <Model> - 3 Measures.docx
      Agent-Instructions.txt          paste into the agent's instructions
      README.txt                      setup steps
      Machine-readable/<Model>/       model.json, agent-brief.md, CSVs (for tools and pipelines, not for the agent)
"""
from __future__ import annotations

import datetime as _dt
import io
import json
import re
import zipfile
from collections import defaultdict
from typing import Dict, List, Optional, Tuple
from xml.sax.saxutils import escape as _xesc

from . import __version__
from . import portfolio
from .analysis import ROLE_LABELS
from .docs import Ctx, PROV_LABEL, _fieldname, _filter_txt, _pages, _split_fields, _src_summary, _tables_sorted, glossary_entries

# ============================================================================ minimal .docx writer

_BAD_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
RED, INK, MUTED, LINE, SOFT = "E60000", "111111", "66645C", "D9D6CC", "F2F0EA"


def _x(s) -> str:
    return _xesc(_BAD_XML.sub("", str(s if s is not None else "")))


class Docx:
    """Just enough WordprocessingML: title, headings 1-3, paragraphs with bold/italic/code runs,
    bullets, tables with a repeating header row, code blocks. Opens in Word, LibreOffice and is indexed by Copilot."""

    def __init__(self, title: str, subject: str = ""):
        self.title, self.subject = title, subject
        self.body: List[str] = []

    # runs: str | list of (text, "b"|"i"|"c"|"r"|"m"|"") tuples
    def _runs(self, runs) -> str:
        if isinstance(runs, str):
            runs = [(runs, "")]
        out = []
        for text, fmt in runs:
            props = ""
            if "b" in fmt:
                props += "<w:b/>"
            if "i" in fmt:
                props += "<w:i/>"
            if "c" in fmt:
                props += '<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/><w:sz w:val="18"/>'
            if "r" in fmt:
                props += f'<w:color w:val="{RED}"/>'
            if "m" in fmt:
                props += f'<w:color w:val="{MUTED}"/>'
            rpr = f"<w:rPr>{props}</w:rPr>" if props else ""
            parts = str(text).split("\n")
            for k, part in enumerate(parts):
                if k:
                    out.append(f"<w:r>{rpr}<w:br/></w:r>")
                if part == "•\t":
                    out.append(f"<w:r>{rpr}<w:t>•</w:t><w:tab/></w:r>")
                    continue
                out.append(f'<w:r>{rpr}<w:t xml:space="preserve">{_x(part.replace(chr(9), "    "))}</w:t></w:r>')
        return "".join(out)

    def p(self, runs, style: Optional[str] = None):
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        self.body.append(f"<w:p>{ppr}{self._runs(runs)}</w:p>")

    def heading(self, text: str, level: int = 1):
        self.p(text, f"Heading{level}")

    def kicker(self, text: str):
        self.p([(text.upper(), "br")], "Kicker")

    def small(self, runs):
        self.p(runs, "Small")

    def bullets(self, items: List):
        for it in items:
            runs = [(it, "")] if isinstance(it, str) else list(it)
            self.p([("•\t", "")] + runs, "ListBullet")

    def code(self, text: str):
        self.p([(text or "", "c")], "Code")

    def table(self, header: List[str], rows: List[List], widths: Optional[List[int]] = None):
        if not rows:
            return
        n = len(header)
        widths = widths or [int(9000 / n)] * n
        grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)
        border = "".join(f'<w:{b} w:val="single" w:sz="4" w:space="0" w:color="{LINE}"/>'
                         for b in ("top", "left", "bottom", "right", "insideH", "insideV"))
        xml = [f'<w:tbl><w:tblPr><w:tblW w:w="5000" w:type="pct"/><w:tblBorders>{border}</w:tblBorders>'
               '<w:tblLayout w:type="autofit"/><w:tblCellMar><w:left w:w="80" w:type="dxa"/><w:right w:w="80" w:type="dxa"/>'
               f'</w:tblCellMar><w:tblLook w:firstRow="1"/></w:tblPr><w:tblGrid>{grid}</w:tblGrid>']

        def row(cells, head=False):
            trpr = "<w:trPr><w:tblHeader/><w:cantSplit/></w:trPr>" if head else "<w:trPr><w:cantSplit/></w:trPr>"
            r = [f"<w:tr>{trpr}"]
            for k, c in enumerate(cells):
                shade = f'<w:shd w:val="clear" w:color="auto" w:fill="{SOFT}"/>' if head else ""
                runs = [(c, "b")] if head and isinstance(c, str) else (c if isinstance(c, list) else [(c, "")])
                r.append(f'<w:tc><w:tcPr><w:tcW w:w="{widths[k]}" w:type="dxa"/>{shade}</w:tcPr>'
                         f'<w:p><w:pPr><w:pStyle w:val="TableText"/></w:pPr>{self._runs(runs)}</w:p></w:tc>')
            r.append("</w:tr>")
            return "".join(r)

        xml.append(row(header, True))
        for rw in rows:
            xml.append(row([("" if c is None else c) for c in rw]))
        xml.append("</w:tbl>")
        self.body.append("".join(xml))
        self.p("", "Spacer")

    def page_break(self):
        self.body.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')

    # ------------------------------------------------------------------ package parts
    def to_bytes(self) -> bytes:
        W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        doc = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {W} '
               'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body>'
               + "".join(self.body)
               + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" w:bottom="1134" '
                 'w:left="1134" w:header="567" w:footer="567" w:gutter="0"/></w:sectPr></w:body></w:document>')
        now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        files = {
            "[Content_Types].xml":
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
                '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
                '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
                '</Types>',
            "_rels/.rels":
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
                '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
                '</Relationships>',
            "word/_rels/document.xml.rels":
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                '</Relationships>',
            "word/styles.xml": _STYLES,
            "word/document.xml": doc,
            "docProps/core.xml":
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
                'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
                f"<dc:title>{_x(self.title)}</dc:title><dc:subject>{_x(self.subject)}</dc:subject>"
                f"<dc:creator>pbip-explainer</dc:creator>"
                f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
                f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified></cp:coreProperties>',
            "docProps/app.xml":
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
                f"<Application>pbip-explainer {__version__}</Application></Properties>",
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in ("[Content_Types].xml", "_rels/.rels", "word/_rels/document.xml.rels", "word/styles.xml",
                         "word/document.xml", "docProps/core.xml", "docProps/app.xml"):
                zf.writestr(name, files[name])
        return buf.getvalue()


def _style(sid, name, ppr="", rpr="", based="Normal", nxt="Normal", extra=""):
    return (f'<w:style w:type="paragraph" w:styleId="{sid}"><w:name w:val="{name}"/><w:basedOn w:val="{based}"/>'
            f'<w:next w:val="{nxt}"/>{extra}<w:pPr>{ppr}</w:pPr><w:rPr>{rpr}</w:rPr></w:style>')


_FONT = '<w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:cs="Arial"/>'
_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    f'<w:docDefaults><w:rPrDefault><w:rPr>{_FONT}<w:sz w:val="20"/><w:szCs w:val="20"/><w:color w:val="{INK}"/>'
    '<w:lang w:val="en-GB"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="100" w:line="276" w:lineRule="auto"/>'
    '</w:pPr></w:pPrDefault></w:docDefaults>'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>'
    + _style("Title", "Title", '<w:spacing w:after="120"/>', '<w:b/><w:color w:val="000000"/><w:sz w:val="48"/>', extra="<w:qFormat/>")
    + _style("Heading1", "heading 1", '<w:keepNext/><w:spacing w:before="360" w:after="120"/><w:outlineLvl w:val="0"/>'
             f'<w:pBdr><w:top w:val="single" w:sz="4" w:space="8" w:color="{LINE}"/></w:pBdr>',
             '<w:b/><w:color w:val="000000"/><w:sz w:val="32"/>', extra="<w:qFormat/>")
    + _style("Heading2", "heading 2", '<w:keepNext/><w:spacing w:before="240" w:after="80"/><w:outlineLvl w:val="1"/>',
             '<w:b/><w:color w:val="000000"/><w:sz w:val="26"/>', extra="<w:qFormat/>")
    + _style("Heading3", "heading 3", '<w:keepNext/><w:spacing w:before="180" w:after="60"/><w:outlineLvl w:val="2"/>',
             '<w:b/><w:color w:val="000000"/><w:sz w:val="22"/>', extra="<w:qFormat/>")
    + _style("Kicker", "Kicker", '<w:spacing w:after="40"/>', f'<w:b/><w:color w:val="{RED}"/><w:sz w:val="16"/><w:spacing w:val="10"/>')
    + _style("Small", "Small", '<w:spacing w:after="80"/>', f'<w:color w:val="{MUTED}"/><w:sz w:val="17"/>')
    + _style("ListBullet", "List Bullet", '<w:tabs><w:tab w:val="left" w:pos="360"/></w:tabs><w:ind w:left="360" w:hanging="240"/><w:spacing w:after="40"/>', "")
    + _style("Code", "Code", f'<w:shd w:val="clear" w:color="auto" w:fill="{SOFT}"/><w:ind w:left="120" w:right="120"/>'
             f'<w:pBdr><w:left w:val="single" w:sz="12" w:space="6" w:color="{LINE}"/></w:pBdr><w:spacing w:before="60" w:after="120" w:line="240" w:lineRule="auto"/>',
             '<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/><w:sz w:val="17"/>')
    + _style("TableText", "Table Text", '<w:spacing w:after="0" w:line="240" w:lineRule="auto"/>', '<w:sz w:val="17"/>')
    + _style("Spacer", "Spacer", '<w:spacing w:after="60"/>', '<w:sz w:val="8"/>')
    + "</w:styles>"
)


# ============================================================================ helpers

def safe_filename(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|#%{}~&]+', "-", s).strip(" .-")
    return re.sub(r"\s+", " ", s)[:90] or "Model"


def _desc_runs(text: Optional[str], prov: Optional[str], empty: str = "No description yet.") -> List[Tuple[str, str]]:
    if not text:
        return [(empty, "m")]
    runs = [(text, "")]
    if prov in ("ai", "auto"):
        runs.append((f"  ({PROV_LABEL[prov]})", "im"))
    return runs


def _short(expr: Optional[str], n: int = 220) -> str:
    s = " ".join((expr or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _intro(d: Docx, key: str, what: str, generated: str):
    d.kicker(f"Semantic model · {key}")
    d.p(f"{key} — {what}", "Title")
    d.small(f"Generated {generated} by pbip-explainer {__version__} from the Power BI project files (metadata only, no data). "
            "Text marked AI-drafted needs review; 'auto' descriptions are derived from the DAX pattern.")


# ============================================================================ per-model documents

def doc_overview(u: dict, ctx: Ctx, generated: str) -> Docx:
    key, m = u["key"], u["model"]
    d = Docx(f"{key} - Overview and report pages", "Power BI semantic model documentation")
    _intro(d, key, "overview and report pages", generated)
    ri = ctx.report_info()
    d.heading(f"What {key} is for", 1)
    if ri.get("purpose"):
        d.p(_desc_runs(ri["purpose"], "ai"))
    else:
        d.p([("No business purpose captured yet.", "m")])
    for k, label in (("audience", "Audience"), ("howToUse", "How to use"), ("dataFreshness", "Data freshness"), ("owner", "Owner")):
        if ri.get(k):
            d.p([(f"{label}: ", "b")] + _desc_runs(ri[k], "ai"))
    st = m["analysis"]["stats"]
    d.table(["Reports", "Pages", "Tables", "Measures", "Relationships", "Columns", "RLS roles"],
            [[", ".join(r["name"] for r in u["reports"]) or "none in package", str(sum(len(r["pages"]) for r in u["reports"])),
              str(st["tables"]), str(st["measures"]), str(st["relationships"]), str(st["columns"]), str(st["roles"])]])
    notes = [n for n in (ctx.enr.get("notes") or []) if isinstance(n, str) and n.strip()]
    if notes:
        d.heading(f"Open points for review ({key})", 2)
        d.bullets([[(n, ""), ("  (AI-drafted · review)", "im")] for n in notes])

    for r in u["reports"]:
        d.heading(f"Report pages ({key})" if r["name"] == key else f"Report pages of {r['name']} ({key})", 1)
        if r["filters"]:
            d.p([("Report-level filters (every page): ", "b"), ("; ".join(_filter_txt(f) for f in r["filters"]), "")])
        for p in r["pages"]:
            tags = (["hidden"] if p["hidden"] else []) + ([p["pageType"]] if p["pageType"] != "Standard" else [])
            d.heading(f"Page '{p['displayName']}' — {r['name'] if r['name'] == key else r['name'] + ' (' + key + ')'}"
                      f"{' (' + ', '.join(tags) + ')' if tags else ''}", 2)
            pi = ctx.page_info(p)
            if pi.get("purpose"):
                d.p(_desc_runs(pi["purpose"], "ai"))
            if pi.get("keyQuestions"):
                d.p([("Questions this page answers:", "b")])
                d.bullets(pi["keyQuestions"])
            slicers = [v for v in p["visuals"] if "slicer" in v["type"].lower()]
            if slicers:
                names = []
                for v in slicers:
                    for f in v["fields"]:
                        if f.get("role") not in ("formatting", "sort"):
                            names.append(_fieldname(f))
                d.p([("Filter by: ", "b"), (", ".join(names), "")])
            for f in p["filters"]:
                d.p([("Page filter: ", "b"), (_filter_txt(f), "")])
            if p.get("drillthroughFields"):
                d.p([("Drill-through on: ", "b"), (", ".join(_fieldname(f) for f in p["drillthroughFields"]), "")])
            rows = []
            for v in p["visuals"]:
                if "slicer" in v["type"].lower():
                    continue
                shows, by = _split_fields(v)
                if v["type"] == "textbox" and v.get("text"):
                    rows.append(["Text", _short(v["text"], 300), "", ""])
                elif shows or by:
                    vf = "; ".join(fl["condition"] for fl in v["filters"] if fl.get("condition"))
                    rows.append([v["typeLabel"], (v["title"] or "—") + (f" (visual filter: {vf})" if vf else ""),
                                 ", ".join(_fieldname(f) for f in shows), ", ".join(_fieldname(f) for f in by)])
            d.table(["Visual", "Title", "Shows", "By / columns"], rows, [1500, 2500, 2600, 2400])
            if p.get("lineage", {}).get("tables"):
                d.small(f"Data on this page comes from tables: {', '.join(p['lineage']['tables'])}.")

    entries = [g for g in glossary_entries(ctx)]
    if entries:
        d.heading(f"Data glossary ({key})", 1)
        d.table(["Term", "Kind", "Definition", "Format", "Used on pages"],
                [[g["term"], g["kind"] + (f" · {g['table']}" if g["table"] else ""),
                  _desc_runs(g["definition"], g["prov"], "—"), g["format"] or "", ", ".join(g["pages"])] for g in entries],
                [1800, 1500, 3700, 900, 1100])
    return d


def doc_model(u: dict, ctx: Ctx, generated: str) -> Docx:
    key, m = u["key"], u["model"]
    d = Docx(f"{key} - Data model and sources", "Power BI semantic model documentation")
    _intro(d, key, "data model, join keys and sources", generated)

    d.heading(f"Tables in {key}", 1)
    rows = []
    for t in _tables_sorted(m):
        desc, prov = ctx.table_desc(t)
        rows.append([t["name"], ROLE_LABELS.get(t["role"], t["role"]), t.get("storageMode") or "", _src_summary(t),
                     f"{len(t['columns'])} / {len(t['measures'])}", _desc_runs(desc, prov, "—")])
    d.table(["Table", "Role", "Storage", "Source", "Cols / measures", "Description"], rows, [1500, 1100, 800, 2000, 900, 2700])

    d.heading(f"Relationships and join keys in {key}", 1)
    d.p("Filters flow from the one side to the many side; bi-directional relationships filter both ways; "
        "inactive relationships work only where a measure uses USERELATIONSHIP.")
    rrows = []
    for r in m["relationships"]:
        notes = []
        if r.get("fromDataType") and r.get("toDataType") and r["fromDataType"] != r["toDataType"]:
            notes.append(f"type mismatch {r['fromDataType']}/{r['toDataType']}")
        if not r["isActive"]:
            notes.append("activated via USERELATIONSHIP" if r["activatedByUserelationship"] else "never activated")
        rrows.append([f"{r['fromTable']}[{r['fromColumn']}]", f"{r['toTable']}[{r['toColumn']}]", r["cardinality"],
                      "both" if r["crossFilteringBehavior"] == "bothDirections" else "single",
                      "yes" if r["isActive"] else "no", "; ".join(notes)])
    d.table(["From (many side)", "To (one side)", "Card.", "Filter", "Active", "Notes"], rrows, [2400, 2400, 700, 800, 700, 2000])

    d.heading(f"Table details ({key})", 1)
    rel_by_table = defaultdict(list)
    for r in m["relationships"]:
        rel_by_table[r["fromTable"]].append(r)
        rel_by_table[r["toTable"]].append(r)
    for t in _tables_sorted(m):
        d.heading(f"Table {t['name']} ({key})", 2)
        desc, prov = ctx.table_desc(t)
        d.p(_desc_runs(desc, prov))
        d.p([("Role: ", "b"), (ROLE_LABELS.get(t["role"], t["role"]), ""), ("   Storage: ", "b"), (t.get("storageMode") or "—", ""),
             ("   Source: ", "b"), (_src_summary(t), "")])
        if rel_by_table[t["name"]]:
            d.p([("Joins: ", "b"), ("; ".join(f"{r['fromTable']}[{r['fromColumn']}] → {r['toTable']}[{r['toColumn']}] ({r['cardinality']}"
                                              f"{', inactive' if not r['isActive'] else ''})" for r in rel_by_table[t["name"]]), "")])
        if t["measures"]:
            d.p([("Measures: ", "b"), (", ".join(me["name"] for me in t["measures"]), "")])
        crow = []
        for c in t["columns"]:
            cd, cp = ctx.column_desc(t, c)
            kind = {"data": "data", "calculated": "calculated", "calculatedTableColumn": "calc. table"}.get(c["columnType"], c["columnType"])
            if c["columnType"] == "data" and c.get("sourceColumn") and c["sourceColumn"] != c["name"]:
                kind += f" ← {c['sourceColumn']}"
            if c["columnType"] == "calculated" and c.get("expression"):
                kind += f": {_short(c['expression'], 120)}"
            flags = ", ".join(x for x in ("hidden" if c["isHidden"] else "", "key" if c.get("isKey") else "",
                                          "unused" if not (c["referenced"] or c["usedInReport"]) else "") if x)
            crow.append([c["name"], c.get("dataType") or "", kind, flags, _desc_runs(cd, cp, ""), ", ".join(_pages(c["usedBy"]))])
        d.table(["Column", "Type", "Kind", "Flags", "Description", "Used on pages"], crow, [1600, 800, 2100, 900, 2400, 1400])
        if t["source"].get("daxExpression"):
            d.p([("Table expression (DAX):", "b")])
            d.code(t["source"]["daxExpression"])
        if t.get("calculationGroup"):
            d.p([("Calculation items:", "b")])
            for it in t["calculationGroup"]["items"]:
                d.p([(it["name"], "b")])
                d.code(it["expression"] or "")

    d.heading(f"Data sources and Power Query ({key})", 1)
    for t in _tables_sorted(m):
        s = t["source"]
        if not s.get("type") or s.get("type") == "DAX calculated table":
            continue
        d.heading(f"Source of {t['name']} ({key})", 3)
        items = []
        for c in s["connectors"]:
            items.append(f"{c['kind']}: {c['function']}({', '.join(str(a) for a in c['arguments'])})" + (f" via query {c['via']}" if c.get("via") else ""))
        for n in s["navigation"]:
            items.append("Navigation: " + ", ".join(f"{k}={v}" for k, v in n.items() if v))
        for p in s["parameters"]:
            items.append(f"Parameter {p['name']} = {p['value']}")
        for j in s["joins"]:
            items.append(f"Power Query join ({j['kind']}): {j['left']} {j['leftKeys']} = {j['right']} {j['rightKeys']}")
        for f in s["rowFilters"]:
            items.append(f"Row filter (step {f['step']}): {f['condition']}")
        if s["steps"]:
            items.append("Applied steps: " + " → ".join(st["name"] + (f" ({st['function']})" if st.get("function") else "") for st in s["steps"]))
        d.bullets(items)
        for q in s["nativeQueries"]:
            d.p([("Native SQL:", "b")])
            d.code(q)

    if m["roles"]:
        d.heading(f"Row-level security ({key})", 1)
        for r in m["roles"]:
            d.p([(f"Role {r['name']}", "b"), (f" ({r.get('modelPermission') or 'read'})", "m")])
            for tp in r["tablePermissions"]:
                d.p(f"Filter on {tp['table']}:")
                d.code(tp["filterExpression"] or "")
    params = [e for e in m["expressions"] if e.get("isParameter")]
    if params:
        d.heading(f"Power Query parameters ({key})", 2)
        d.table(["Parameter", "Value", "Type"], [[e["name"], str((e.get("parameter") or {}).get("value")),
                                                  (e.get("parameter") or {}).get("type") or ""] for e in params])

    d.heading(f"Model health findings ({key})", 1)
    fs = m["analysis"]["findings"]
    if not fs:
        d.p("No findings.")
    for f in fs:
        d.p([(f"{f['severity'].upper()} · {f['code']}: ", "b"), (f["message"], "")])
        if f.get("advice"):
            d.small(f["advice"])
        if f["objects"]:
            d.bullets([str(o) for o in f["objects"][:20]])
    un = m["analysis"]["unused"]
    if un["measures"] or un["columns"]:
        d.heading(f"Unused objects ({key})", 2)
        d.small(un["note"])
        if un["measures"]:
            d.p([("Measures not used by the packaged report(s): ", "b"), (", ".join(un["measures"]), "")])
        if un["columns"]:
            d.p([("Columns not referenced anywhere: ", "b"), (", ".join(un["columns"]), "")])
    return d


def doc_measures(u: dict, ctx: Ctx, generated: str) -> Docx:
    key, m = u["key"], u["model"]
    d = Docx(f"{key} - Measures", "Power BI semantic model documentation")
    _intro(d, key, "measures (DAX)", generated)
    d.p(f"Every measure of {key} with its formula, what it depends on and where it is shown. "
        "Each heading carries the model name, so answers can say which model a measure comes from.")
    for t in _tables_sorted(m):
        if not t["measures"]:
            continue
        d.heading(f"Measures in table {t['name']} ({key})", 1)
        for me in sorted(t["measures"], key=lambda x: ((x.get("displayFolder") or ""), x["name"].lower())):
            d.heading(f"Measure [{me['name']}] — {key}", 2)
            desc, prov = ctx.measure_desc(me)
            d.p(_desc_runs(desc, prov))
            meta = [f"Model: {key}", f"Table: {t['name']}"]
            if me.get("displayFolder"):
                meta.append(f"Folder: {me['displayFolder']}")
            if me.get("formatString"):
                meta.append(f"Format: {me['formatString']}")
            meta.append(f"Complexity: {me['complexity']['level']}")
            if me["isHidden"]:
                meta.append("hidden")
            if not me["usedInReport"]:
                meta.append("not used in the packaged report(s)")
            d.small(" · ".join(meta))
            d.code(me["expression"] or "")
            dep = me["dependsOn"]
            items = []
            if dep["measures"]:
                items.append([("Uses measures: ", "b"), (", ".join(f"[{x}]" for x in dep["measures"]), "")])
            if dep["columns"]:
                items.append([("Uses columns: ", "b"), (", ".join(f"{a}[{b}]" for a, b in dep["columns"]), "")])
            if dep["userelationship"]:
                items.append([("Activates relationships: ", "b"),
                              (", ".join(f"{p[0][0]}[{p[0][1]}] ↔ {p[1][0]}[{p[1][1]}]" for p in dep["userelationship"]), "")])
            if me["lineage"]["columns"]:
                items.append([("Ultimately reads: ", "b"), (", ".join(me["lineage"]["columns"]), "")])
            used_m = sorted({u2["by"][8:] for u2 in me["usedBy"] if u2["kind"] == "measure"})
            if used_m:
                items.append([("Used by measures: ", "b"), (", ".join(f"[{x}]" for x in used_m), "")])
            vis = sorted({f"{u2.get('page')}: {u2.get('visualTitle') or u2.get('visual')}" for u2 in me["usedBy"] if u2.get("page")})
            if vis:
                items.append([("Shown in: ", "b"), ("; ".join(vis), "")])
            d.bullets(items)
    return d


# ============================================================================ catalog

def doc_catalog(units: List[dict], pf: dict, name: str, generated: str, files: Dict[str, List[str]]) -> Docx:
    d = Docx(f"{name} - Catalog", "Catalog of Power BI semantic models")
    d.kicker("Semantic model catalog")
    d.p(name, "Title")
    tot = pf["totals"]
    d.p(f"This catalog covers {tot['models']} semantic model(s) with {tot['reports']} report(s), {tot['pages']} pages, "
        f"{tot['tables']} tables and {tot['measures']} measures. It lists every model, the documents that describe it, "
        "and what the models have in common or do differently.")
    d.small(f"Generated {generated} by pbip-explainer {__version__} from Power BI project files (metadata only, no data).")

    d.heading("Models in this catalog", 1)
    rows = []
    for mo in pf["models"]:
        sev = mo["findings"]
        rows.append([mo["key"], mo["purpose"] or "—", ", ".join(mo["reports"]) or "—",
                     f"{mo['stats']['tables']} tables · {mo['stats']['measures']} measures · {mo['stats']['relationships']} relationships",
                     ", ".join(mo["sourceKinds"]) or "—",
                     ", ".join(f"{v} {k}" for k, v in sorted(sev.items())) or "none"])
    d.table(["Model", "Purpose", "Reports", "Size", "Sources", "Findings"], rows, [1500, 2600, 1400, 1700, 1100, 1100])
    d.heading("Which document answers what", 2)
    for mo in pf["models"]:
        d.p([(mo["key"], "b")])
        d.bullets(files.get(mo["key"], []))

    d.heading("Measures with the same name but different logic", 1)
    if pf["measureConflicts"]:
        d.p("These measures carry the same name in several models but are calculated differently. Numbers with the same "
            "label can therefore differ between reports. Check which definition is intended before comparing.")
        for c in pf["measureConflicts"]:
            d.heading(f"[{c['name']}] differs across models: {', '.join(c['models'])}", 2)
            rows = []
            for v in c["variants"]:
                rows.append([", ".join(f"{mo} ({tb})" for mo, tb in zip(v["models"], v["tables"])),
                             [(_short(v["expression"], 600), "c")], "; ".join(sorted(set(v["descriptions"])))[:300] or "—"])
            d.table(["Model (table)", "DAX", "Description"], rows, [2200, 4800, 2000])
    else:
        d.p("No measure name is defined differently in two models.")

    d.heading("Measures defined identically in several models", 1)
    if pf["sharedMeasures"]:
        d.table(["Measure", "Models", "DAX"], [[f"[{s['name']}]", ", ".join(s["models"]), [(_short(s["variants"][0]["expression"], 200), "c")]]
                                                for s in pf["sharedMeasures"]], [2000, 3000, 4000])
    else:
        d.p("None.")

    d.heading("Tables that appear in several models", 1)
    if pf["sharedTables"]:
        rows = []
        for t in pf["sharedTables"]:
            diff = "same columns" if t["sameColumns"] else "; ".join(
                f"{mo} lacks {', '.join(cols[:8])}{' …' if len(cols) > 8 else ''}" for mo, cols in t["missingColumns"].items())
            rows.append([t["name"], ", ".join(f"{mo} ({ROLE_LABELS.get(r, r)})" for mo, r in t["roles"].items()), diff])
        d.table(["Table", "Models (role)", "Column differences"], rows, [1800, 3400, 3800])
    else:
        d.p("None.")

    d.heading("Sources used by several models", 1)
    if pf["sharedSources"]:
        d.table(["Source", "Used by (model: table)"], [[s["label"], "; ".join(f"{u['model']}: {u['table']}" for u in s["uses"])]
                                                       for s in pf["sharedSources"]], [3400, 5600])
    else:
        d.p("No source table or file is used by more than one model.")

    d.heading("Same tables joined differently", 1)
    if pf["relationshipDifferences"]:
        for rd in pf["relationshipDifferences"]:
            d.p([(" ↔ ".join(rd["tables"]), "b")])
            d.bullets([f"{v['model']}: {v['from']} → {v['to']} ({v['cardinality']}, {v['direction']}{', inactive' if not v['active'] else ''})"
                       for v in rd["variants"]])
    else:
        d.p("Where two models relate the same tables, they use the same join keys and filter direction.")

    d.heading("Model health across the catalog", 1)
    rows = []
    for u in units:
        for f in u["model"]["analysis"]["findings"]:
            if f["severity"] in ("error", "warning"):
                rows.append([u["key"], f["severity"], f["message"]])
    d.table(["Model", "Severity", "Finding"], rows, [2000, 1000, 6000]) if rows else d.p("No errors or warnings.")
    return d


# ============================================================================ agent texts

AGENT_INSTRUCTIONS = """You are the Semantic Model Guide for {name}. You explain how our Power BI semantic models and reports work: what a measure means and how it is calculated, which tables join on which keys, where data comes from, which pages show what, and how the models differ.

Knowledge
- The knowledge files were generated from the Power BI project files of these models: {models}.
- Start with "00 Catalog" for questions across models (which model has what, measures with the same name but different logic, shared tables and sources). Per model there are three files: "1 Overview and pages", "2 Data model and sources", "3 Measures".
- The files describe metadata only. They contain no data values.

How to answer
- Always name the model an answer comes from. If several models match, list each with its own answer, and say when they differ.
- Use the exact object names: measures in brackets like [Headcount], columns as Table[Column], pages in quotes.
- For "how is X calculated", give the plain-language meaning first, then the DAX, then what it depends on and where it is shown.
- For "how are A and B connected", give the join keys, cardinality and filter direction from the relationships table.
- Separate facts from interpretation. Text marked "AI-drafted · review" in the files was written by an AI from the model; say so when you rely on it. Descriptions marked "auto" were derived from the DAX pattern.
- Never state numbers, totals, row counts, refresh times or owners unless the files say so. If the files do not answer the question, say what is missing and suggest checking the report in Power BI.
- "Unused" in the files means unused by the reports that were analysed; other reports may still use the object.
- Keep answers short: the answer first, then the evidence.
"""

README = """Analysis package: {name}
Generated {generated} by pbip-explainer {version}

Contents
- Knowledge/                   {nknow} Word files. Upload this folder to SharePoint; it is the agent's knowledge.
- Agent-Instructions.txt       Paste into the agent's Instructions field.
- Machine-readable/            model.json, agent-brief.md and CSVs per model, for tools and pipelines. Do not add to the agent.

Set up the agent (Microsoft 365 Copilot > Agent Builder)
1. Create a SharePoint folder, e.g. "Semantic model catalog", in a site your audience can read.
   The agent answers each user only from files that user is allowed to open.
2. Upload all files from Knowledge/ into that folder.
3. In Microsoft 365 Copilot, choose Create agent, open the Configure tab.
   Name: e.g. Semantic Model Guide. Instructions: paste Agent-Instructions.txt.
4. Under Knowledge, add the SharePoint folder (or the files; Agent Builder allows up to 100 SharePoint files per agent;
   this package has {nknow}). Turn off web search so the agent answers only from these files.
5. Suggested starter prompts:
   - Which models do we have and what is each one for?
   - How is [Headcount] calculated in {first}?
   - Which measures have the same name but different logic across models?
   - How are the tables joined in {first}?
6. Test, then Share with a security group.

Update the package later
- Re-run the analysis and upload the new Knowledge files with the same names (overwrite). File names are stable on
  purpose, so the agent's knowledge links keep working. SharePoint re-indexes changed files after a few hours.

Notes
- Copilot Studio reads SharePoint .docx as well, so the same folder works there.
- Files with a "Confidential" sensitivity label may not be indexed. Check your label before uploading.
"""


# ============================================================================ build

def build(packs: List[dict], name: str = "Semantic model catalog", include_machine_readable: bool = True) -> Tuple[bytes, dict]:
    """`packs`: [{"pack": dict, "enrichment": dict|None, "run": str}]. Returns (zip bytes, manifest)."""
    generated = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    units = portfolio.collect_units(packs)
    if not units:
        raise ValueError("None of the selected analyses contains a semantic model.")
    pf = portfolio.analyse(units)
    root = safe_filename(name)
    out: Dict[str, bytes] = {}
    files_by_model: Dict[str, List[str]] = {}
    for u in units:
        sub_pack = dict(u["pack"])
        sub_pack["semanticModels"] = [u["model"]]
        sub_pack["reports"] = u["reports"]
        ctx = Ctx(sub_pack, u.get("enrichment"))
        base = safe_filename(u["key"])
        names = [f"{base} - 1 Overview and pages.docx", f"{base} - 2 Data model and sources.docx", f"{base} - 3 Measures.docx"]
        for fname, builder in zip(names, (doc_overview, doc_model, doc_measures)):
            out[f"{root}/Knowledge/{fname}"] = builder(u, ctx, generated).to_bytes()
        files_by_model[u["key"]] = [
            f"{names[0]}: purpose, report pages, visuals, filters, glossary",
            f"{names[1]}: tables, relationships and join keys, columns, sources and Power Query, security, health findings",
            f"{names[2]}: every measure with DAX, dependencies and where it is shown",
        ]
        if include_machine_readable:
            mr = f"{root}/Machine-readable/{base}"
            out[f"{mr}/model.json"] = json.dumps(sub_pack, indent=1, ensure_ascii=False, default=list).encode("utf-8")
            from .pack import agent_brief  # local import: pack imports docs which this module imports too
            out[f"{mr}/agent-brief.md"] = agent_brief(sub_pack).encode("utf-8")
            if u.get("enrichment"):
                out[f"{mr}/enrichment.json"] = json.dumps(u["enrichment"], indent=2, ensure_ascii=False).encode("utf-8")
    out[f"{root}/Knowledge/00 Catalog.docx"] = doc_catalog(units, pf, name, generated, files_by_model).to_bytes()
    nknow = sum(1 for k in out if "/Knowledge/" in k)
    model_keys = [u["key"] for u in units]
    out[f"{root}/Agent-Instructions.txt"] = AGENT_INSTRUCTIONS.format(name=name, models=", ".join(model_keys)).encode("utf-8")
    out[f"{root}/README.txt"] = README.format(name=name, generated=generated, version=__version__, nknow=nknow,
                                              first=model_keys[0]).encode("utf-8")
    if include_machine_readable:
        out[f"{root}/Machine-readable/portfolio.json"] = json.dumps(pf, indent=1, ensure_ascii=False).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for k in sorted(out):
            zf.writestr(k, out[k])
    manifest = {"name": name, "root": root, "files": sorted(out), "knowledgeFiles": nknow, "models": model_keys,
                "portfolio": {k: pf[k] for k in ("totals",)} | {
                    "measureConflicts": len(pf["measureConflicts"]), "sharedMeasures": len(pf["sharedMeasures"]),
                    "sharedTables": len(pf["sharedTables"]), "sharedSources": len(pf["sharedSources"]),
                    "relationshipDifferences": len(pf["relationshipDifferences"])},
                "warnings": (["More than 100 knowledge files: Agent Builder accepts at most 100 SharePoint files per agent. "
                              "Split the models into several agents."] if nknow > 100 else [])}
    return buf.getvalue(), manifest
