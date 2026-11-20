"""Render the knowledge pack (+ optional enrichment.json) as Markdown and as one self-contained HTML file.

Audience: business users (overview, pages, glossary) and the tech team (model, DAX, lineage, sources, health).
Provenance of every description is shown: model (authoritative), AI-drafted (enrichment, review) or auto (pattern).
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from html import escape
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import diagram
from .analysis import PRIORITY_RULE, ROLE_LABELS, finding_impact_text
from .mquery import hide_embedded

HIDDEN_BADGE = '<span class="badge hid">hidden</span>'
PROV_LABEL = {"model": "", "ai": "AI-drafted · review", "auto": "auto"}


class Ctx:
    def __init__(self, pack: dict, enrichment: Optional[dict] = None):
        self.pack = pack
        self.enr = enrichment or {}
        self.models = pack["semanticModels"]
        self.reports = pack["reports"]
        self.title = self._title()
        self.table_names = {t["name"] for m in self.models for t in m["tables"]}   # tables that get a card to link to
        self.measure_names = {me["name"] for m in self.models for t in m["tables"] for me in t["measures"]}
        self.column_names = {(t["name"], c["name"]) for m in self.models for t in m["tables"] for c in t["columns"]}
        self.slugs: Dict[Tuple[str, str], str] = {}
        self._used: set = set()

    def _title(self) -> str:
        if len(self.reports) == 1:
            return self.reports[0]["name"]
        if self.models:
            return self.models[0]["name"]
        if self.reports:
            return self.reports[0]["name"]
        if self.models:
            return self.models[0]["name"]
        return self.pack["source"]["name"]

    # anchors -----------------------------------------------------------------
    def anchor(self, kind: str, name: str, scope=None) -> str:
        """Stable, unique id. `scope` keeps objects apart that share a name (pages of different reports)."""
        key = (kind, name, scope)
        if key not in self.slugs:
            base = kind + "-" + (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "x")
            s, i = base, 2
            while s in self._used:
                s, i = f"{base}-{i}", i + 1
            self._used.add(s)
            self.slugs[key] = s
        return self.slugs[key]

    def page_anchor(self, report: Optional[str], page: str, nth: int = 0) -> str:
        """Anchor of a report page; `nth` counts pages with the same name inside one report."""
        if report is None:
            report = next((r["name"] for r in self.reports if any(p["displayName"] == page for p in r["pages"])), None)
        return self.anchor("p", page, (report, nth))

    # descriptions with provenance --------------------------------------------
    def _e(self, section: str, key: str) -> dict:
        v = (self.enr.get(section) or {}).get(key) or {}
        return v if isinstance(v, dict) else {}

    def measure_desc(self, m: dict) -> Tuple[Optional[str], Optional[str]]:
        if m.get("description"):
            return m["description"], "model"
        e = self._e("measures", m["name"])
        txt = e.get("description") or e.get("businessDefinition")
        if txt:
            return txt, "ai"
        if m.get("autoDescription"):
            return m["autoDescription"], "auto"
        return None, None

    def table_desc(self, t: dict) -> Tuple[Optional[str], Optional[str]]:
        if t.get("description"):
            return t["description"], "model"
        e = self._e("tables", t["name"])
        if e.get("description"):
            txt = e["description"] + (f" Grain: {e['grain']}" if e.get("grain") else "")
            return txt, "ai"
        return None, None

    def column_desc(self, t: dict, c: dict) -> Tuple[Optional[str], Optional[str]]:
        if c.get("description"):
            return c["description"], "model"
        e = self._e("columns", f"{t['name']}[{c['name']}]")
        if e.get("description"):
            return e["description"], "ai"
        return None, None

    def page_info(self, p: dict) -> dict:
        return self._e("pages", p["displayName"])

    def report_info(self) -> dict:
        r = self.enr.get("report") or {}
        return r if isinstance(r, dict) else {}


# ============================================================================ helpers

def _pages(usages: List[dict]) -> List[str]:
    return sorted({u["page"] for u in usages if u.get("page")})


def _fieldname(f: dict) -> str:
    if f["kind"] == "measure":
        return f"[{f['name']}]"
    n = f"{f['table']}[{f['name']}]"
    if f.get("variation"):
        n += f" ({f['variation']})"
    if f.get("aggregation"):
        n = f"{f['aggregation']} of {n}"
    return n


def _split_fields(v: dict) -> Tuple[List[dict], List[dict]]:
    shows, by = [], []
    for f in v["fields"]:
        if f.get("role") in ("formatting", "sort"):
            continue
        if f["kind"] == "measure" or f.get("aggregation"):
            shows.append(f)
        else:
            by.append(f)
    return shows, by


def _tables_sorted(m: dict) -> List[dict]:
    order = {"fact": 0, "bridge": 1, "dimension": 2, "date": 3, "measureTable": 4, "calculationGroup": 5,
             "fieldParameter": 6, "whatIfParameter": 7, "disconnected": 8}
    return sorted((t for t in m["tables"] if not t["isAutoDateTable"]),
                  key=lambda t: (order.get(t["role"], 9), t["name"].lower()))


def _src_summary(t: dict) -> str:
    s = t["source"]
    if s.get("type") == "DAX calculated table":
        return "Calculated table (DAX)"
    kinds = []
    for c in s["connectors"]:
        if c["kind"] not in kinds and c["kind"] not in ("File",) or (c["kind"] == "File" and len(s["connectors"]) == 1):
            kinds.append(c["kind"])
    nav = []
    for n in s["navigation"]:
        vals = [str(v) for k, v in n.items() if k in ("Name", "Schema", "Item", "Entity") and v]
        if vals:
            nav.append(".".join(vals))
    out = ", ".join(kinds) or (s.get("type") or "unknown")
    if nav:
        out += " → " + " / ".join(nav[:3])
    if s["nativeQueries"]:
        out += " (native SQL)"
    return out


def impact_summary(imp: Optional[dict], name: str) -> str:
    """One sentence: what changing an object touches in the packaged report(s)."""
    if not imp:
        return ""
    c = imp["counts"]
    if not (c["measures"] or c["visuals"] or c["filters"] or c["other"]):
        return f"Nothing in the packaged report(s) depends on {name}."
    parts = []
    if c["measures"]:
        parts.append(f"{c['measures']} measure{'s' if c['measures'] != 1 else ''}")
    if c["visuals"]:
        parts.append(f"{c['visuals']} visual{'s' if c['visuals'] != 1 else ''} on {c['pages']} page{'s' if c['pages'] != 1 else ''} "
                     f"({c['direct']} direct, {c['indirect']} indirect)")
    if c["filters"]:
        parts.append(f"{c['filters']} filter{'s' if c['filters'] != 1 else ''}")
    if c["other"]:
        parts.append(f"{c['other']} other object{'s' if c['other'] != 1 else ''} (relationships, security, hierarchies)")
    return f"Changing {name} affects " + ", ".join(parts) + "."


def _vis_label(e: dict) -> str:
    return f"{e.get('type') or 'Visual'} '{e.get('title') or e.get('visual')}' on page '{e.get('page')}'"


def impact_lines(imp: Optional[dict], limit: int = 40) -> List[str]:
    """Readable lines: direct visuals, indirect visuals with the dependency path, filters, other objects."""
    if not imp:
        return []
    out = []
    for e in imp["visuals"]["direct"][:limit]:
        out.append(f"Direct: {_vis_label(e)}")
    for e in imp["visuals"]["indirect"][:limit]:
        via = " → ".join(e.get("path", [])[1:-1])
        out.append(f"Indirect: {_vis_label(e)}" + (f" via {via}" if via else ""))
    for f in imp["filters"][:limit]:
        via = " → ".join(f["path"][1:-1])
        out.append(f"Filter: {f['where']}" + (f" via {via}" if via else ""))
    for o in imp["other"][:limit]:
        out.append(f"Also: {o}")
    hidden = imp["counts"]["visuals"] - min(len(imp["visuals"]["direct"]), limit) - min(len(imp["visuals"]["indirect"]), limit)
    if hidden > 0:
        out.append(f"… and {hidden} more visual{'' if hidden == 1 else 's'}")
    return out


def glossary_entries(ctx: Ctx) -> List[dict]:
    items: List[dict] = []
    for m in ctx.models:
        for t in m["tables"]:
            if t["isAutoDateTable"]:
                continue
            for me in t["measures"]:
                if me["isHidden"]:
                    continue
                d, prov = ctx.measure_desc(me)
                items.append({"term": me["name"], "kind": "Metric", "table": t["name"], "definition": d, "prov": prov,
                              "format": me.get("formatString"), "pages": _pages(me["usedBy"]),
                              "anchor": ctx.anchor("m", me["name"])})
            for c in t["columns"]:
                if c["isHidden"] or not c["usedInReport"] or t["role"] in ("fieldParameter", "calculationGroup"):
                    continue
                d, prov = ctx.column_desc(t, c)
                items.append({"term": c["name"], "kind": "Attribute", "table": t["name"], "definition": d, "prov": prov,
                              "format": c.get("formatString"), "pages": _pages(c["usedBy"]),
                              "anchor": ctx.anchor("t", t["name"])})
    for g in ctx.enr.get("glossary") or []:
        if isinstance(g, dict) and g.get("term"):
            items.append({"term": g["term"], "kind": "Business term", "table": "", "definition": g.get("definition"),
                          "prov": "ai", "format": None, "pages": [], "anchor": None,
                          "related": g.get("relatedObjects") or []})
    return sorted(items, key=lambda x: (x["term"].lower(), x["kind"]))


def _n(n: int, word: str, plural: Optional[str] = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def _names(items: List[str], limit: int = 4) -> str:
    return ", ".join(items[:limit]) + (f" and {len(items) - limit} more" if len(items) > limit else "")


def _reach(me: dict) -> int:
    """Visuals that change when the measure changes (direct and indirect)."""
    imp = me.get("impact")
    if imp:
        return imp["counts"]["visuals"]
    return len({u["by"] for u in me["usedBy"] if u["kind"] == "visual"})


def top_measures(ctx: Ctx, limit: int = 6) -> List[Tuple[str, int]]:
    """Measures most report visuals depend on, most first."""
    ms = [(me["name"], _reach(me)) for m in ctx.models for t in m["tables"] for me in t["measures"]]
    return [x for x in sorted(ms, key=lambda x: (-x[1], x[0].lower())) if x[1] > 0][:limit]


def model_brief(ctx: Ctx) -> List[Tuple[str, str]]:
    """The model in a few sentences, as (label, text). Built from extracted facts only, shown as 'auto'."""
    out: List[Tuple[str, str]] = []
    worth: List[str] = []
    for m in ctx.models:
        pre = f"{m['name']}: " if len(ctx.models) > 1 else ""
        tables = [t for t in m["tables"] if not t["isAutoDateTable"]]
        facts = [t["name"] for t in tables if t["role"] in ("fact", "bridge")]
        dims = [t["name"] for t in tables if t["role"] in ("dimension", "date")]
        rest = [t["name"] for t in tables if t["name"] not in facts and t["name"] not in dims]
        s = f"{_n(len(tables), 'table')} joined by {_n(len(m['relationships']), 'relationship')}."
        if facts:
            s += f" The numbers come from {_names(facts)} ({'fact table' if len(facts) == 1 else 'fact tables'})."
        if dims:
            s += f" They can be sliced by {_names(dims)} ({'dimension' if len(dims) == 1 else 'dimensions'})."
        if rest:
            s += f" Helper tables without business data: {_names(rest)}."
        out.append(("Model", pre + s))

        measures = [(t["name"], me) for t in tables for me in t["measures"]]
        if measures:
            homes = sorted({t for t, _ in measures})
            unused = sum(1 for _, me in measures if not me["usedInReport"])
            s = f"{_n(len(measures), 'measure')} in {_names(homes)}"
            if ctx.reports:
                s += f"; {len(measures) - unused} used in the report, {unused} not used"
            s += "."
            top = top_measures(ctx, 3) if len(ctx.models) == 1 else []
            if top:
                s += " Most visuals depend on " + ", ".join(f"{n} ({_n(v, 'visual')})" for n, v in top) + "."
            out.append(("Measures", pre + s))

        by_kind: Dict[str, List[str]] = defaultdict(list)
        for t in _tables_sorted(m):
            if t["source"].get("type"):
                by_kind[_src_summary(t).split(" → ")[0].replace(" (native SQL)", "")].append(t["name"])
        if by_kind:
            out.append(("Sources", pre + "; ".join(f"{k}: {_names(v, 3)}" for k, v in by_kind.items()) + "."))

        both = sum(1 for r in m["relationships"] if r["crossFilteringBehavior"] == "bothDirections")
        inactive = sum(1 for r in m["relationships"] if not r["isActive"])
        if both:
            worth.append(f"{_n(both, 'relationship')} {'filters' if both == 1 else 'filter'} in both directions")
        if inactive:
            worth.append(_n(inactive, "inactive relationship"))
        if m["roles"]:
            worth.append(f"{_n(len(m['roles']), 'security role')} ({_names([r['name'] for r in m['roles']], 3)})")
        cg = [t["name"] for t in tables if t["role"] == "calculationGroup"]
        if cg:
            worth.append(f"{_n(len(cg), 'calculation group')} ({_names(cg, 3)})")
        high = sum(1 for f in m["analysis"]["findings"] if f.get("priority") == "high")
        if high:
            worth.append(f"{_n(high, 'high-priority health finding')}")
    for r in ctx.reports:
        pre = f"{r['name']}: " if len(ctx.reports) > 1 else ""
        hidden = sum(1 for p in r["pages"] if p["hidden"])
        s = _n(len(r["pages"]), "page") + (f" ({hidden} hidden)" if hidden else "")
        s += ": " + _names([p["displayName"] for p in r["pages"]], 6) + "."
        if r.get("reportMeasures"):
            n = len(r["reportMeasures"])
            s += f" {_n(n, 'measure')} {'is' if n == 1 else 'are'} defined in the report itself, not in the model."
        om = r.get("observedModel")
        if om:
            c = om["counts"]
            s += (f" The semantic model is not in the package (live connection); the report uses "
                  f"{_n(c['tables'], 'table')}, {_n(c['columns'], 'column')} and {_n(c['measures'], 'measure')} of it.")
        out.append(("Report", pre + s))
    if worth:
        out.append(("Worth knowing", "; ".join(worth) + "."))
    return out


def diagram_panel_data(ctx: Ctx, m: dict) -> dict:
    """Per table: what the side panel of the diagram shows (columns, measures, joins, pages)."""
    joins: Dict[str, List[str]] = defaultdict(list)
    for r in m["relationships"]:
        txt = (f"{r['fromTable']}[{r['fromColumn']}] → {r['toTable']}[{r['toColumn']}] · {r['cardinality']}"
               + ("" if r["isActive"] else " · inactive")
               + (" · both directions" if r["crossFilteringBehavior"] == "bothDirections" else ""))
        joins[r["fromTable"]].append(txt)
        if r["toTable"] != r["fromTable"]:
            joins[r["toTable"]].append(txt)
    data = {}
    for t in m["tables"]:
        if t["isAutoDateTable"]:
            continue
        d, prov = ctx.table_desc(t)
        pages = [p["displayName"] for r in ctx.reports for p in r["pages"]
                 if t["name"] in (p.get("lineage") or {}).get("tables", [])]
        readers = [[me["name"], ctx.anchor("m", me["name"])] for t2 in m["tables"] for me in t2["measures"]
                   if t2["name"] != t["name"] and t["name"] in (me.get("lineage") or {}).get("tables", [])]
        cols = []
        for c in t["columns"]:
            flags = ("k" if c.get("isKey") else "") + ("h" if c["isHidden"] else "") + ("c" if c["columnType"] == "calculated" else "")
            cols.append([c["name"], c.get("dataType") or "", flags])
        data[t["name"]] = {"r": ROLE_LABELS.get(t["role"], t["role"]), "d": d or "", "p": PROV_LABEL.get(prov or "", ""),
                           "a": ctx.anchor("t", t["name"]), "c": cols,
                           "m": [[me["name"], ctx.anchor("m", me["name"])] for me in t["measures"]],
                           "rm": readers, "j": joins.get(t["name"], []), "pg": pages}
    return data


def lineage_graph(ctx: Ctx) -> dict:
    """Dependency graph for the lineage explorer: n = [id, label, kind, anchor, sub, impact sentence], e = [from, to].

    An edge a -> b means b changes when a changes. Kinds: c column, t table, m measure, r report measure,
    v visual, o other (relationship, security role, hierarchy, filter, calculated table ...).
    """
    nodes: List[list] = []
    idx: Dict[str, int] = {}
    edges: set = set()
    multi = len(ctx.reports) > 1
    tables = {t["name"] for m in ctx.models for t in m["tables"]}

    def node(id_: str, label: str, kind: str, anchor: str = "", sub: str = "", summary: str = "") -> int:
        if id_ not in idx:
            idx[id_] = len(nodes)
            nodes.append([id_, label, kind, anchor, sub, summary])
        elif summary and not nodes[idx[id_]][5]:
            nodes[idx[id_]][5] = summary
        return idx[id_]

    def column(tc: str, summary: str = "") -> int:
        t, _, c = tc.partition("[")
        return node("c:" + tc, c.rstrip("]"), "c", ctx.anchor("t", t) if t in tables else "", t, summary)

    def measure(name: str, summary: str = "") -> int:
        return node("m:" + name, name, "m", ctx.anchor("m", name), "", summary)

    def visual(page: str, vis: str, vtype: Optional[str], title: Optional[str], report: Optional[str]) -> int:
        key = f"{page}/{vis}"
        if multi and report:
            key = f"{report}/{key}"
        return node("v:" + key, f"{vtype or 'Visual'}: {title or vis}", "v", ctx.page_anchor(report, page), page)

    def target(u: dict) -> int:
        kind, ref = u["kind"], u["by"].split(":", 1)[-1]
        if kind == "measure":
            return measure(ref)
        if kind == "reportMeasure":
            return node("r:" + ref, ref, "r", "", "report measure")
        if kind in ("visual", "visualFilter"):
            return visual(u.get("page") or "", u.get("visual") or ref, u.get("visualType"), u.get("visualTitle"), u.get("report"))
        if kind in ("calculatedColumn", "sortBy"):
            return column(ref)
        return node("o:" + u["by"], u.get("label") or u["by"], "o")

    for m in ctx.models:
        for t in m["tables"]:
            if t["isAutoDateTable"]:
                continue
            for c in t["columns"]:
                tc = f"{t['name']}[{c['name']}]"
                if not c["usedBy"]:
                    continue
                a = column(tc, impact_summary(c.get("impact"), tc))
                for u in c["usedBy"]:
                    edges.add((a, target(u)))
            for me in t["measures"]:
                a = measure(me["name"], impact_summary(me.get("impact"), f"[{me['name']}]"))
                for u in me["usedBy"]:
                    edges.add((a, target(u)))
                for tn in me["dependsOn"]["tables"]:
                    edges.add((node("t:" + tn, tn, "t", ctx.anchor("t", tn) if tn in tables else "", "table"), a))
    for r in ctx.reports:
        for rm in r.get("reportMeasures") or []:
            imp = rm.get("impact")
            a = node("r:" + rm["name"], rm["name"], "r", "", "report measure", impact_summary(imp, f"[{rm['name']}]"))
            for x in (rm.get("dependsOn") or {}).get("measures", []):
                edges.add((measure(x), a))
            for x, y in (rm.get("dependsOn") or {}).get("columns", []):
                edges.add((column(f"{x}[{y}]"), a))
            for e in (imp or {}).get("visuals", {}).get("direct", []):
                edges.add((a, visual(e.get("page") or "", e.get("visual") or "", e.get("type"), e.get("title"), r["name"])))
    return {"n": nodes, "e": sorted([a, b] for a, b in edges if a != b)}


# ============================================================================ Markdown

def render_md(ctx: Ctx) -> str:
    L: List[str] = []
    shown = [True]
    toc: List[str] = []
    has_model = bool(ctx.models)
    observed = [r for r in ctx.reports if r.get("observedModel")]

    def a(x: str) -> None:
        if shown[0]:
            L.append(x)

    def sec(title: str, show: bool = True) -> None:
        """Start a section. With show=False everything up to the next section is dropped (no bare headings)."""
        shown[0] = show
        if show:
            toc.append(title)
            a(f"## {title}")
            a("")
    a(f"# {ctx.title} — Power BI documentation")
    a("")
    a(f"*Generated {ctx.pack['generatedAt']} from `{ctx.pack['source']['name']}` by {ctx.pack['generator']}. "
      "Facts are extracted from the PBIP files; text marked **AI-drafted** needs review; **auto** descriptions are "
      "pattern-generated from the DAX.*")
    a("")
    a("## Contents")
    a("")
    toc_at = len(L)                                  # filled in at the end, from the sections that were written

    # Overview ------------------------------------------------------------------
    sec("Overview")
    ri = ctx.report_info()
    for key, label in (("purpose", "Purpose"), ("audience", "Audience"), ("howToUse", "How to use"),
                       ("dataFreshness", "Data freshness"), ("owner", "Owner")):
        if ri.get(key):
            a(f"**{label}** *(AI-drafted · review)*: {ri[key]}")
            a("")
    notes = [n for n in (ctx.enr.get("notes") or []) if isinstance(n, str) and n.strip()]
    if notes:
        a("**Open points for review** *(AI-drafted · review)*:")
        a("")
        for n in notes:
            a(f"- {n}")
        a("")
    brief = model_brief(ctx)
    if brief:
        a("**In brief** *(auto)*:")
        a("")
        for label, text in brief:
            a(f"- **{label}.** {_md(text)}")
        a("")
    for m in ctx.models:
        st = m["analysis"]["stats"]
        a(f"| Semantic model | Format | Tables | Columns | Measures | Relationships | RLS roles | Reports |")
        a("|---|---|---|---|---|---|---|---|")
        a(f"| {m['name']} | {m['format'].upper()} (CL {m.get('compatibilityLevel')}) | {st['tables']} | {st['columns']} | "
          f"{st['measures']} | {st['relationships']} | {st['roles']} | {', '.join(m['reports']) or '—'} |")
        a("")
    for r in ctx.reports:
        a(f"Report **{r['name']}** ({r['format'].upper()}): {len(r['pages'])} pages, "
          f"{sum(len(p['visuals']) for p in r['pages'])} visuals.")
        a("")
        if r.get("observedModel") and not has_model:
            a("This report uses a live connection: its semantic model is not in the package. This documentation covers the report "
              "and the part of the model the report uses. Tables, relationships, DAX, data sources and model health need the PBIP "
              "of the semantic model itself.")
            a("")

    # Pages ---------------------------------------------------------------------
    if observed:
        sec("Observed model (live connection)")
        a("The semantic model behind this report is not in the package. This is the part of it the report uses, "
          "read from its visuals and filters.")
        a("")
        for r in observed:
            om = r["observedModel"]
            c = om["counts"]
            a(f"**{r['name']}** uses {_n(c['tables'], 'table')}, {_n(c['columns'], 'column')} and {_n(c['measures'], 'measure')} "
              f"of the model. {om['note']}")
            a("")
            a("| Table | Field | Kind | Used on pages |")
            a("|---|---|---|---|")
            for t in om["tables"]:
                for kind, items in (("measure", t["measures"]), ("column", t["columns"]), ("hierarchy level", t["hierarchies"])):
                    for e in items:
                        a(f"| {_md(t['name'])} | {_md(e['name'])} | {kind} | {_md(', '.join(e['pages']) or 'report filter')} |")
            a("")

    sec("Report pages")
    for r in ctx.reports:
        if r["filters"]:
            a("**Report-level filters (apply to every page):** " + "; ".join(_filter_txt(f) for f in r["filters"]))
            a("")
        if r.get("reportMeasures"):
            a(f"**Report-level measures** (defined in the report, not in the model):")
            a("")
            for rm in r["reportMeasures"]:
                a(f"- [{rm['name']}] on {rm.get('table')}: `{' '.join((rm.get('expression') or '').split())}` — {impact_summary(rm.get('impact'), '[' + rm['name'] + ']')}")
            a("")
        for p in r["pages"]:
            tags = (["hidden"] if p["hidden"] else []) + ([p["pageType"]] if p["pageType"] != "Standard" else [])
            a(f"### {p['displayName']}{'  (' + ', '.join(tags) + ')' if tags else ''}")
            a("")
            pi = ctx.page_info(p)
            if pi.get("purpose"):
                a(f"*AI-drafted · review:* {pi['purpose']}")
                a("")
            if pi.get("keyQuestions"):
                a("Questions this page answers:")
                for q in pi["keyQuestions"]:
                    a(f"- {q}")
                a("")
            for f in p["filters"]:
                a(f"- Page filter: {_filter_txt(f)}")
            if p.get("drillthroughFields"):
                a("- Drill-through on: " + ", ".join(_fieldname(f) for f in p["drillthroughFields"]))
            slicers = [v for v in p["visuals"] if "slicer" in v["type"].lower() or v["type"] == "advancedSlicerVisual"]
            if slicers:
                a("- Filter by: " + ", ".join(", ".join(_fieldname(f) for f in v["fields"] if f.get("role") not in ("formatting", "sort"))
                                              for v in slicers))
            a("")
            rows = []
            for v in p["visuals"]:
                if "slicer" in v["type"].lower():
                    continue
                shows, by = _split_fields(v)
                if v["type"] == "textbox" and v.get("text"):
                    rows.append(f"| Text | {_md(v['text'][:200])} | | |")
                elif shows or by:
                    vf = "; ".join(fl["condition"] for fl in v["filters"] if fl.get("condition"))
                    rows.append(f"| {v['typeLabel']} | {_md(v['title'] or '—')}{_md(' (visual filter: ' + vf + ')') if vf else ''} | {_md(', '.join(_fieldname(f) for f in shows))} | "
                                f"{_md(', '.join(_fieldname(f) for f in by))} |")
            if rows:
                a("| Visual | Title | Shows | By / columns |")
                a("|---|---|---|---|")
                L.extend(rows)
                a("")
            if p.get("lineage", {}).get("tables"):
                a(f"Data comes from tables: {', '.join(p['lineage']['tables'])}.")
                a("")

    # Glossary ------------------------------------------------------------------
    entries = glossary_entries(ctx)
    sec("Data glossary", bool(entries))
    a("| Term | Kind | Table | Definition | Format | Used on pages |")
    a("|---|---|---|---|---|---|")
    for g in entries:
        d = g["definition"] or "*(no description yet)*"
        if g["prov"] in ("ai", "auto"):
            d += f" *({PROV_LABEL[g['prov']]})*"
        a(f"| {_md(g['term'])} | {g['kind']} | {_md(g['table'])} | {_md(d)} | {_md(g['format'] or '')} | {_md(', '.join(g['pages']))} |")
    a("")

    # Data model ----------------------------------------------------------------
    sec("Data model", has_model)
    for m in ctx.models:
        a(f"### {m['name']}")
        a("")
        a("```mermaid")
        a(diagram.mermaid(m))
        a("```")
        a("")
        a("Arrows: `}o--||` many-to-one, dotted line = inactive relationship. A rendered diagram is in `model-diagram.svg`.")
        a("")
        a("| Table | Role | Storage | Source | Columns | Measures | Description |")
        a("|---|---|---|---|---|---|---|")
        for t in _tables_sorted(m):
            d, prov = ctx.table_desc(t)
            a(f"| {_md(t['name'])} | {ROLE_LABELS.get(t['role'], t['role'])} | {t.get('storageMode') or ''} | {_md(_src_summary(t))} | "
              f"{len(t['columns'])} | {len(t['measures'])} | {_md((d or '') + (' *(' + PROV_LABEL[prov] + ')*' if prov in ('ai', 'auto') else ''))} |")
        a("")
        a("#### Relationships (join keys)")
        a("")
        a("| From (many side) | To (one side) | Cardinality | Filter direction | Active | Notes |")
        a("|---|---|---|---|---|---|")
        for r in m["relationships"]:
            notes = []
            if r.get("fromDataType") and r.get("toDataType") and r["fromDataType"] != r["toDataType"]:
                notes.append(f"type mismatch {r['fromDataType']}/{r['toDataType']}")
            if not r["isActive"]:
                notes.append("activated via USERELATIONSHIP" if r["activatedByUserelationship"] else "never activated")
            a(f"| {_md(r['fromTable'])}[{_md(r['fromColumn'])}] | {_md(r['toTable'])}[{_md(r['toColumn'])}] | {r['cardinality']} | "
              f"{'both' if r['crossFilteringBehavior'] == 'bothDirections' else 'single'} | {'yes' if r['isActive'] else 'no'} | {'; '.join(notes)} |")
        a("")

    # Tables --------------------------------------------------------------------
    sec("Tables", has_model)
    for m in ctx.models:
        rel_by_table = defaultdict(list)
        for r in m["relationships"]:
            rel_by_table[r["fromTable"]].append(r)
            rel_by_table[r["toTable"]].append(r)
        for t in _tables_sorted(m):
            a(f"### {t['name']}")
            a("")
            d, prov = ctx.table_desc(t)
            if d:
                a(d + (f" *({PROV_LABEL[prov]})*" if prov in ("ai", "auto") else ""))
                a("")
            a(f"Role: **{ROLE_LABELS.get(t['role'], t['role'])}** · Storage: {t.get('storageMode') or '—'} · "
              f"Source: {_src_summary(t)}{' · hidden' if t['isHidden'] else ''}")
            a("")
            if rel_by_table[t["name"]]:
                a("Relationships: " + "; ".join(
                    f"{r['fromTable']}[{r['fromColumn']}] → {r['toTable']}[{r['toColumn']}] ({r['cardinality']}"
                    f"{', inactive' if not r['isActive'] else ''}{', both' if r['crossFilteringBehavior'] == 'bothDirections' else ''})"
                    for r in rel_by_table[t["name"]]))
                a("")
            if t["columns"]:
                a("| Column | Type | Kind | Hidden | Format | Sort by | Description | Used on pages |")
                a("|---|---|---|---|---|---|---|---|")
                for c in t["columns"]:
                    cd, cp = ctx.column_desc(t, c)
                    kind = {"data": "data", "calculated": "calculated", "calculatedTableColumn": "calc. table"}.get(c["columnType"], c["columnType"])
                    if c["columnType"] == "data" and c.get("sourceColumn") and c["sourceColumn"] != c["name"]:
                        kind += f" ← `{c['sourceColumn']}`"
                    a(f"| {_md(c['name'])}{' 🔑' if c.get('isKey') else ''} | {c.get('dataType') or ''} | {_md(kind)} | {'yes' if c['isHidden'] else ''} | "
                      f"{_md(c.get('formatString') or '')} | {_md(c.get('sortByColumn') or '')} | "
                      f"{_md((cd or '') + (' *(AI-drafted)*' if cp == 'ai' else ''))} | {_md(', '.join(_pages(c['usedBy'])))} |")
                a("")
            for c in t["columns"]:
                if c["columnType"] == "calculated":
                    a(f"Calculated column **{c['name']}**:")
                    a("")
                    a("```dax")
                    a(c["expression"] or "")
                    a("```")
                    a("")
            if t["hierarchies"]:
                for h in t["hierarchies"]:
                    a(f"Hierarchy **{h['name']}**: " + " › ".join(l["name"] for l in h["levels"]))
                a("")
            if t["source"].get("daxExpression"):
                a("Table expression:")
                a("")
                a("```dax")
                a(t["source"]["daxExpression"])
                a("```")
                a("")

    # Measures ------------------------------------------------------------------
    sec("Measures", has_model)
    for m in ctx.models:
        for t in _tables_sorted(m):
            if not t["measures"]:
                continue
            a(f"### Measures in {t['name']}")
            a("")
            by_folder = defaultdict(list)
            for me in t["measures"]:
                by_folder[me.get("displayFolder") or ""].append(me)
            for folder in sorted(by_folder):
                if folder:
                    a(f"*Folder: {folder}*")
                    a("")
                for me in by_folder[folder]:
                    a(f"#### {me['name']}")
                    a("")
                    d, prov = ctx.measure_desc(me)
                    if d:
                        a(d + (f" *({PROV_LABEL[prov]})*" if prov in ("ai", "auto") else ""))
                        a("")
                    meta = [f"Format: `{me['formatString']}`" if me.get("formatString") else None,
                            "hidden" if me["isHidden"] else None,
                            f"complexity: {me['complexity']['level']}",
                            "not used in report" if not me["usedInReport"] else None]
                    a(" · ".join(x for x in meta if x))
                    a("")
                    a("```dax")
                    a(me["expression"] or "")
                    a("```")
                    a("")
                    dep = me["dependsOn"]
                    if dep["measures"]:
                        a("Uses measures: " + ", ".join(f"[{x}]" for x in dep["measures"]))
                    if dep["columns"]:
                        a("Uses columns: " + ", ".join(f"{x}[{y}]" for x, y in dep["columns"]))
                    if dep["userelationship"]:
                        a("Activates relationships: " + ", ".join(f"{p[0][0]}[{p[0][1]}] ↔ {p[1][0]}[{p[1][1]}]" for p in dep["userelationship"]))
                    if me["lineage"]["columns"] and me["lineage"]["columns"] != [f"{x}[{y}]" for x, y in dep["columns"]]:
                        a("Ultimately reads: " + ", ".join(me["lineage"]["columns"]))
                    imp = me.get("impact")
                    if imp:
                        a("")
                        a(f"**Change impact.** {impact_summary(imp, '[' + me['name'] + ']')}")
                        if imp["measures"]:
                            a("Dependent measures: " + ", ".join(f"[{x}]" for x in imp["measures"]))
                        for line in impact_lines(imp, 25):
                            a(f"- {line}")
                    a("")

    # Security / params / calc groups --------------------------------------------
    sec("Security, parameters & calculation groups", has_model)
    for m in ctx.models:
        if m["roles"]:
            a("### Row-level security")
            a("")
            for r in m["roles"]:
                a(f"**{r['name']}** ({r.get('modelPermission') or 'read'}, {r['memberCount']} members defined in file)")
                for tp in r["tablePermissions"]:
                    a(f"- `{tp['table']}`: `{' '.join((tp['filterExpression'] or '').split())}`")
                a("")
        cgs = [t for t in m["tables"] if t.get("calculationGroup")]
        for t in cgs:
            a(f"### Calculation group: {t['name']}")
            a("")
            for it in t["calculationGroup"]["items"]:
                a(f"**{it['name']}**")
                a("```dax")
                a(it["expression"] or "")
                a("```")
            a("")
        fps = [t for t in m["tables"] if t["role"] in ("fieldParameter", "whatIfParameter")]
        for t in fps:
            a(f"### {ROLE_LABELS[t['role']]}: {t['name']}")
            a("")
            if t["source"].get("daxExpression"):
                a("```dax")
                a(t["source"]["daxExpression"])
                a("```")
                a("")
        params = [e for e in m["expressions"] if e.get("isParameter")]
        if params:
            a("### Power Query parameters")
            a("")
            a("| Parameter | Value | Type |")
            a("|---|---|---|")
            for e in params:
                pi = e.get("parameter") or {}
                a(f"| {_md(e['name'])} | `{_md(str(pi.get('value')))}` | {pi.get('type') or ''} |")
            a("")
        if not (m["roles"] or cgs or fps or params):
            a("None defined.")
            a("")

    # Sources -------------------------------------------------------------------
    sec("Data sources & Power Query", has_model)
    for m in ctx.models:
        for t in _tables_sorted(m):
            s = t["source"]
            if not s.get("type"):
                continue
            a(f"### {t['name']}")
            a("")
            a(f"Source: {_src_summary(t)}")
            for c in s["connectors"]:
                via = f" (via query {c['via']})" if c.get("via") else ""
                a(f"- `{c['function']}`{via}: {', '.join('`' + hide_embedded(str(x)) + '`' for x in c['arguments'])}")
            for n in s["navigation"]:
                a("- Navigation: " + ", ".join(f"{k}={v}" for k, v in n.items() if v))
            for p in s["parameters"]:
                a(f"- Parameter `{p['name']}` = `{p['value']}`")
            for j in s["joins"]:
                a(f"- Join in Power Query ({j['kind']}): {j['left']}{j['leftKeys']} = {j['right']}{j['rightKeys']}")
            for f in s["rowFilters"]:
                a(f"- Row filter (step {f['step']}): `{f['condition']}`")
            if s["steps"]:
                a("- Steps: " + " → ".join(st["name"] + (f" ({st['function']})" if st.get("function") else "") for st in s["steps"]))
            for q in s["nativeQueries"]:
                a("")
                a("```sql")
                a(q)
                a("```")
            a("")
            for p in t["partitions"]:
                if p.get("expression") and (p.get("sourceType") or "").lower() == "m":
                    a("<details><summary>Power Query (M)</summary>")
                    a("")
                    a("```powerquery")
                    a(hide_embedded(p["expression"]))
                    a("```")
                    a("")
                    a("</details>")
                    a("")
        shared = [e for e in m["expressions"] if not e.get("isParameter")]
        if shared:
            a("### Shared queries")
            a("")
            for e in shared:
                a(f"**{e['name']}**")
                a("")
                a("```powerquery")
                a(hide_embedded(e["expression"]))
                a("```")
                a("")

    # Health --------------------------------------------------------------------
    sec("Model health", has_model)
    for m in ctx.models:
        an = m["analysis"]
        if an["findings"] and "priority" in an["findings"][0]:
            a(f"*Sorted by priority: {PRIORITY_RULE}.*")
            a("")
        for f in an["findings"]:
            pr = f"{f['priority'].upper()} priority · " if f.get("priority") else ""
            a(f"- **{pr}{f['severity'].upper()}** — {f['message']}" + (f" {f['advice']}" if f.get("advice") else ""))
            for o in f["objects"][:15]:
                a(f"  - {o}")
            imp = finding_impact_text(f)
            if imp:
                a(f"  - *Impact:* {imp}" + (f" Basis: {f['impact']['basis']}." if (f.get("impact") or {}).get("visuals") else ""))
        a("")
        a(f"*{an['unused']['note']}*")
        a("")
        if an["unused"]["columns"]:
            a("Columns not referenced anywhere (visuals, measures, relationships, sort, RLS): " + ", ".join(an["unused"]["columns"]))
            a("")

    # Appendix ------------------------------------------------------------------
    sec("Appendix")
    for r in ctx.reports:
        a(f"- Report `{r['name']}`: theme {r['theme'].get('custom') or r['theme'].get('base') or '—'}, "
          f"{len(r['bookmarks'])} bookmarks, custom visuals: {', '.join(r['customVisuals']) or 'none'}")
        for b in r["bookmarks"]:
            a(f"  - Bookmark: {b.get('displayName')}")
    for m in ctx.models:
        a(f"- Model `{m['name']}`: culture {m.get('culture')}, settings: "
          + ", ".join(f"{k}={v}" for k, v in m.get("settings", {}).items()))
        for q in m.get("daxQueries", []):
            a(f"- DAX query file in project: `{q['name']}.dax`")
    a("")
    L[toc_at:toc_at] = [f"- [{t}](#{re.sub(r'[^a-z0-9 -]', '', t.lower()).replace(' ', '-')})" for t in toc] + [""]
    return "\n".join(L) + "\n"


def _md(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ")


def _filter_txt(f: dict) -> str:
    if f.get("condition"):
        return f["condition"]
    fl = f.get("field") or {}
    return f"{fl.get('table')}[{fl.get('name')}] (available, no value set)"


# ============================================================================ HTML

DAX_FUNC = re.compile(r"[A-Z][A-Z0-9.]*(?=\s*\()")


def highlight_dax(expr: str, ctx: Ctx, measure_names: set) -> str:
    if not expr:
        return ""
    tok = re.compile(
        r"(?P<com>//[^\n]*|--[^\n]*|/\*.*?\*/)|(?P<str>\"(?:[^\"]|\"\")*\")"
        r"|(?P<col>(?:'(?:[^']|'')*'|[A-Za-z_][A-Za-z0-9_]*)\[(?:[^\]]|\]\])*\])"
        r"|(?P<mea>\[(?:[^\]]|\]\])*\])|(?P<kw>\b(?:VAR|RETURN|IN|NOT|AND|OR|TRUE|FALSE)\b)"
        r"|(?P<fn>\b[A-Za-z][A-Za-z0-9.]*(?=\s*\())", re.S)
    out, last = [], 0
    for m in tok.finditer(expr):
        out.append(escape(expr[last:m.start()]))
        txt = m.group(0)
        g = m.lastgroup
        if g == "com":
            out.append(f'<span class="c">{escape(txt)}</span>')
        elif g == "str":
            out.append(f'<span class="s">{escape(txt)}</span>')
        elif g == "col":
            out.append(f'<span class="r">{escape(txt)}</span>')
        elif g == "mea":
            name = txt[1:-1].replace("]]", "]")
            if name in measure_names:
                out.append(f'<a class="r m" href="#{ctx.anchor("m", name)}">{escape(txt)}</a>')
            else:
                out.append(f'<span class="r">{escape(txt)}</span>')
        elif g == "kw":
            out.append(f'<span class="k">{escape(txt)}</span>')
        elif g == "fn":
            out.append(f'<span class="f">{escape(txt)}</span>')
        last = m.end()
    out.append(escape(expr[last:]))
    return "".join(out)


CSS = """
:root{--primary:#E60000;--bordeaux:#B00000;--bronze:#B98E2C;--g1:#D9D6CC;--g2:#D9D6CC;--g3:#9A978E;--g4:#66645C;--g5:#3A3A3A;--g6:#111111;
--bg:#FFFFFF;--surface:#F2F0EA;--surface2:#F7F6F2;--row:#F7F6F2;--page:#F7F6F2;--ok:#2E7D32;--warn:#C9A227;--bad:#B00000;--info:#66645C;
--font:"Frutiger 45 Light",Frutiger,"Helvetica Neue",Helvetica,Arial,"Segoe UI",system-ui,sans-serif;--mono:Consolas,"SF Mono",Menlo,monospace}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth;scroll-padding-top:16px}
body{font-family:var(--font);background:var(--bg);color:var(--g6);line-height:1.55;font-size:14px}
a{color:var(--g6);text-decoration:underline;text-decoration-color:var(--g2);text-underline-offset:2px}
a:hover{color:var(--primary)}
.layout{display:grid;grid-template-columns:270px 1fr;min-height:100vh}
nav{position:sticky;top:0;height:100vh;overflow:auto;border-right:1px solid var(--surface);padding:20px 16px;background:var(--bg)}
nav .brand{font-size:11px;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:var(--primary);margin-bottom:4px}
nav .rname{font-size:17px;font-weight:700;color:#000;margin-bottom:14px;word-break:break-word;line-height:1.25}
nav input{width:100%;padding:7px 9px;border:1px solid var(--g2);border-radius:2px;font:inherit;margin-bottom:14px}
nav input:focus{outline:2px solid var(--g6);outline-offset:0}
nav ul{list-style:none}
nav li a{display:block;padding:4px 8px;text-decoration:none;border-left:2px solid transparent;color:var(--g5);font-size:13.5px}
nav li a:hover,nav li a.active{border-left-color:var(--primary);color:#000;background:var(--row)}
nav li.sub a{padding-left:20px;font-size:12.5px}
#results{list-style:none;margin:-8px 0 12px;max-height:50vh;overflow:auto}
#results li a{display:block;padding:4px 8px;font-size:12.5px;text-decoration:none;border-bottom:1px solid var(--row)}
#results li a.act{background:var(--row);border-left:2px solid var(--primary)}
#results .kind{color:var(--g4);font-size:11px;margin-left:6px}
main{padding:32px 48px 80px;max-width:1280px;min-width:0}
header.doc .kicker{font-size:11px;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:var(--primary);margin-bottom:8px}
header.doc h1{font-size:38px;font-weight:700;color:#000;line-height:1.12;letter-spacing:-.01em}
header.doc .meta{color:var(--g4);font-size:12.5px;margin:6px 0 20px}
h2{font-size:26px;font-weight:700;color:#000;margin:48px 0 12px;padding-top:18px;border-top:1px solid var(--g1);letter-spacing:-.005em}
h3{font-size:17px;font-weight:700;color:#000;margin:26px 0 8px}
h4{font-size:14.5px;font-weight:600;color:#000;margin:18px 0 6px}
p{margin:6px 0}
.lead{font-size:15px;max-width:820px}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));border-top:1px solid var(--surface);border-left:1px solid var(--surface);margin:12px 0 4px}
.tile{background:var(--bg);padding:12px 14px;border-right:1px solid var(--surface);border-bottom:1px solid var(--surface)}
.tile .v{font-size:26px;font-weight:700;color:#000;line-height:1.1}
.tile .l{font-size:12px;color:var(--g4)}
table{border-collapse:collapse;width:100%;margin:8px 0 14px;font-size:13px}
th{text-align:left;font-weight:700;font-size:12px;color:#000;border-bottom:1px solid var(--g6);padding:6px 8px;vertical-align:bottom}
td{padding:5px 8px;border-bottom:1px solid var(--surface);vertical-align:top}
tbody tr:nth-child(even){background:var(--row)}
tbody tr:target td{background:#FBF6EA;box-shadow:inset 0 1px 0 var(--bronze),inset 0 -1px 0 var(--bronze)}
.tw{overflow-x:auto}
code,pre{font-family:var(--mono);font-size:12.5px}
code{background:var(--row);padding:0 3px;border-radius:2px}
pre{background:var(--page);border-left:2px solid var(--g2);padding:10px 14px;overflow:auto;margin:6px 0 10px;line-height:1.45}
pre .c{color:var(--g4);font-style:italic}pre .s{color:#946F29}pre .k{color:var(--bordeaux);font-weight:600}
pre .f{color:#2E476B;font-weight:600}pre .r{color:#295B40}pre a.m{color:#295B40;text-decoration-color:#7FA88F}
.codebox{display:flex;align-items:flex-start;gap:6px}
.codebox pre{flex:1;min-width:0}
.copy{flex:none;margin-top:6px;font:inherit;font-size:11.5px;padding:1px 8px;border:1px solid var(--g2);background:var(--bg);color:var(--g5);border-radius:2px;cursor:pointer;opacity:.75}
.codebox:hover .copy,.copy:focus{opacity:1}
.copy:hover{border-color:var(--g6);color:#000}
.copy.done{border-color:var(--ok);color:var(--ok);opacity:1}
.badge{display:inline-block;font-size:11.5px;padding:0 6px;border:1px solid var(--g2);border-radius:2px;color:var(--g5);margin-left:6px;vertical-align:1px;white-space:nowrap}
.badge.ai{border-color:var(--bronze);color:#6C5312;background:#FBF6EA}
.badge.auto{border-color:var(--g2);color:var(--g4)}
.badge.hid{color:var(--g4)}
.badge.unused{border-color:var(--warn);color:#7a5a00}
.badge.role{background:var(--surface);border-color:var(--surface);color:var(--g6)}
.card{border:1px solid var(--g1);padding:16px 20px;margin:14px 0}
.card h3{margin-top:0}
.muted{color:var(--g4)}
.small{font-size:12.5px}
.chips span{display:inline-block;border:1px solid var(--surface);padding:1px 7px;margin:2px 4px 2px 0;font-size:12px}
.find{border-left:3px solid var(--g3);padding:8px 12px;margin:8px 0;background:var(--bg)}
.find.error{border-left-color:var(--bad)}.find.warning{border-left-color:var(--warn)}.find.info{border-left-color:var(--info)}
.find .sev{font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--g4)}
.find.error .sev{color:var(--bad)}.find.warning .sev{color:#8C5B00}
.find ul{margin:4px 0 0 18px;font-size:12.5px;color:var(--g5)}
.prio{font-weight:700}.prio.high{color:var(--primary)}.prio.medium{color:#8C5B00}.prio.low{color:var(--g4)}
.fimp{margin-top:6px;font-size:12.5px;color:var(--g5)}.fimp b{font-size:11px;letter-spacing:.06em;text-transform:uppercase;margin-right:6px;color:var(--g4)}
.fimp.hit{color:var(--g6)}.fimp.hit b{color:var(--primary)}
.fimp .ex,.fimp .basis{display:block;color:var(--g4)}.fimp .basis{font-size:11.5px}
.diagram{border:1px solid var(--surface);position:relative;overflow:hidden;height:620px;background:var(--bg);cursor:grab}
.diagram svg{width:100%;height:100%}
.dtools{position:absolute;right:8px;top:8px;display:flex;gap:4px;z-index:2}
.dtools button{font:inherit;font-size:13px;width:30px;height:28px;border:1px solid var(--g2);background:var(--bg);border-radius:2px;cursor:pointer}
.dtools button.wide{width:auto;padding:0 8px}
.dtools button:hover{border-color:var(--g6)}
.brief{border:1px solid var(--g1);border-left:3px solid var(--g6);padding:14px 20px;margin:12px 0 14px;max-width:980px}
.brief .bk{font-size:11px;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:var(--g5);margin-bottom:8px}
.brief dl{display:grid;grid-template-columns:120px 1fr;gap:6px 14px;font-size:14.5px}
.brief dt{font-weight:700;color:#000}
.brief .jump{margin-top:12px;font-size:13px}
.dwrap{display:flex;border:1px solid var(--surface)}
.dwrap .diagram{flex:1;min-width:0;border:0}
.dpanel{flex:none;width:320px;position:relative;border-left:1px solid var(--surface);background:var(--surface2);font-size:13px}
.dwrap.stack{flex-direction:column}.dwrap.stack .dpanel{width:auto;height:240px;border-left:0;border-top:1px solid var(--surface)}
.dpanel>div{position:absolute;top:0;right:0;bottom:0;left:0;overflow:auto;padding:14px 16px}
.dpanel .pk{font-size:11px;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:var(--g4);margin:0 0 4px}
.dpanel h4{font-size:17px;margin:0 0 4px}
.dpanel h5{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--g4);margin:14px 0 4px}
.dpanel ul{list-style:none}
.dpanel li{display:flex;justify-content:space-between;gap:8px;padding:2px 0;border-bottom:1px solid var(--g1)}
.dpanel li .ty{color:var(--g4);font-size:11.5px;white-space:nowrap}
.dhint{position:absolute;left:50%;bottom:12px;transform:translateX(-50%);background:var(--g6);color:#fff;font-size:12.5px;padding:5px 12px;border-radius:2px;z-index:2;pointer-events:none;white-space:nowrap}
.lin-bar{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;margin:10px 0 6px}
.lin-bar label{font-weight:700;font-size:13px}
.lin-bar input{font:inherit;padding:7px 9px;border:1px solid var(--g3);border-radius:2px;width:300px;max-width:100%}
.lin-bar input:focus{outline:2px solid var(--g6);outline-offset:0}
.chip{font:inherit;font-size:12.5px;padding:3px 9px;border:1px solid var(--g2);background:var(--bg);color:var(--g6);border-radius:2px;cursor:pointer;margin-left:6px}
.chip:hover{border-color:var(--g6)}
.chip.on{background:var(--g6);border-color:var(--g6);color:#fff}
.lin-sum{font-size:14.5px;margin:8px 0;max-width:900px}
.lin{border:1px solid var(--surface);overflow:auto;padding:12px;max-height:660px}
.lin svg{display:block}
.lin text{font-family:var(--font);user-select:none;-webkit-user-select:none}
.lin .ln{cursor:pointer}
.lin .ln rect{fill:#fff;stroke:var(--g3)}
.lin .ln .t{font-size:12.5px;fill:#000}
.lin .ln .s{font-size:10.5px;fill:var(--g4)}
.lin .k-c rect,.lin .k-t rect{fill:var(--surface);stroke:var(--g2)}
.lin .k-m rect{stroke:var(--g5)}
.lin .k-r rect{stroke:var(--g5);stroke-dasharray:4 3}
.lin .k-o{cursor:default}.lin .k-o rect{stroke-dasharray:3 3}.lin .k-o .t{fill:var(--g5)}
.lin .k-v rect.bar{fill:var(--bronze);stroke:none}
.lin .sel rect{fill:var(--g6);stroke:var(--g6)}.lin .sel .t{fill:#fff;font-weight:700}.lin .sel .s{fill:var(--g1)}
.lin .more{cursor:default}.lin .more .t{fill:var(--g4)}
.lin .le{fill:none;stroke:var(--g3);stroke-width:1}
.lin .le.d{stroke:var(--g6);stroke-width:1.4}
.lin.hov .le{opacity:.2}.lin.hov .le.hl{opacity:1;stroke:var(--primary)}
.lin .ch{font-size:10.5px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;fill:var(--g4)}
.lin .ln:focus{outline:none}.lin .ln:focus-visible rect{stroke:var(--primary);stroke-width:2}
.lin-btn{font:inherit;font-size:11.5px;font-weight:400;letter-spacing:0;text-transform:none;margin-left:10px;padding:1px 8px;border:1px solid var(--g2);background:var(--bg);color:var(--g6);border-radius:2px;cursor:pointer}
.lin-btn:hover{border-color:var(--g6)}
.legend{font-size:12px;color:var(--g4);margin:6px 0 0}
.legend i{display:inline-block;width:12px;height:10px;margin:0 4px -1px 10px;border:1px solid var(--g3)}
details{margin:4px 0}
summary{cursor:pointer;color:var(--g5);font-size:12.5px}
details.measure{border-top:1px solid var(--surface);margin:0}
details.measure>summary{list-style:none;display:grid;grid-template-columns:minmax(200px,36%) minmax(0,1fr) auto;gap:14px;align-items:baseline;padding:7px 8px 7px 24px;position:relative;font-size:13.5px;color:var(--g6)}
details.measure>summary::-webkit-details-marker{display:none}
details.measure>summary::before{content:"▸";position:absolute;left:6px;top:8px;color:var(--g4);font-size:12px}
details.measure[open]>summary::before{content:"▾"}
details.measure>summary:hover,details.measure[open]>summary{background:var(--row)}
details.measure>summary h4{margin:0;font-size:14.5px;min-width:0}
details.measure .md{color:var(--g4);font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
details.measure[open] .md{visibility:hidden}
details.measure .mr{color:var(--g5);font-size:12px;white-space:nowrap}
details.measure .mb{padding:8px 8px 14px 24px}
details.measure:target>summary{box-shadow:inset 3px 0 0 var(--primary)}
.mgroup>p{margin:10px 0 4px}
.mbar{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;margin:12px 0 6px}
.mbar input{font:inherit;padding:6px 9px;border:1px solid var(--g3);border-radius:2px;width:230px;max-width:100%}
.mbar input:focus,.mbar select:focus{outline:2px solid var(--g6);outline-offset:0}
.mbar select{font:inherit;font-size:13px;padding:5px 8px;border:1px solid var(--g3);border-radius:2px;background:var(--bg);color:var(--g6);max-width:220px}
.mbar .chip:first-child{margin-left:0}
.mbar .mcount{color:var(--g4);font-size:12.5px;margin-left:auto}
.impact{margin:10px 0 6px;border-left:3px solid var(--primary);padding:8px 14px;background:var(--row)}
.impact.compact{border-left-width:2px;padding:6px 10px;margin:4px 0}
.impact .ik{font-size:11px;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:var(--primary);margin-bottom:2px}
.impact p{margin:3px 0}
.ilist{list-style:none;margin:6px 0 2px;font-size:12.5px}
.ilist li{padding:3px 0;border-top:1px solid var(--g1)}
.ilist .via{color:var(--g4);font-size:12px;margin-left:66px}
.tag{display:inline-block;width:58px;margin-right:8px;font-size:10.5px;font-weight:700;letter-spacing:.06em;text-transform:uppercase}
.tag.d{color:var(--primary)}.tag.i{color:var(--g5)}.tag.f{color:#8C5B00}.tag.o{color:var(--g4)}
.kv{display:grid;grid-template-columns:150px 1fr;gap:2px 12px;font-size:12.5px;margin:6px 0}
.kv dt{color:var(--g4)}
.pagegrid{position:relative;background:var(--page);border:1px solid var(--surface);margin:8px 0 12px}
.pagegrid .vb{position:absolute;border:1px solid var(--g2);background:#fff;font-size:10px;overflow:hidden;padding:2px 4px;color:var(--g5);line-height:1.2}
.pagegrid .vb.data{border-color:var(--g5)}
.pagegrid .vb.deco{background:transparent;border-style:dotted;color:transparent}
.navtoggle{display:none;font:inherit;font-size:13px;font-weight:600;width:100%;text-align:left;padding:7px 9px;border:1px solid var(--g2);background:var(--bg);color:var(--g6);border-radius:2px;cursor:pointer}
.navtoggle::after{content:"▾";float:right;color:var(--g4)}
.embed header.doc,.embed nav .brand,.embed nav .rname{display:none}
.embed main{padding:4px 32px 80px}
.embed #overview h2{margin-top:16px;padding-top:0;border-top:0}
.az a{display:inline-block;min-width:20px;text-align:center;font-size:12.5px;text-decoration:none;color:var(--g5)}
@media (max-width:900px){.layout{grid-template-columns:1fr}nav{position:static;height:auto;border-right:0;border-bottom:1px solid var(--surface)}main{padding:20px 16px}details.measure>summary{grid-template-columns:minmax(0,1fr) auto}details.measure .md{display:none}.navtoggle{display:block}nav .toc{display:none;margin-top:8px}nav.open .toc{display:block}nav{padding:12px 16px}nav input{margin-bottom:8px}.brief dl{grid-template-columns:1fr;gap:0 0}.brief dd{margin-bottom:6px}}
@media print{nav,.dtools,.copy,.dpanel,.lin-bar,.lin-btn,.dhint,.mbar{display:none}.lin{max-height:none;overflow:visible}.layout{display:block}main{max-width:none;padding:0}.diagram{height:auto;border:0}h2{break-before:page}}
"""

JS = r"""
(function(){
// inside the desktop app (?embed=1) the app already shows the title: drop the doc's own title block
if(/[?&]embed=1(&|$)/.test(location.search)) document.documentElement.classList.add('embed');
const idx = JSON.parse(document.getElementById('search-index').textContent);
const q = document.getElementById('q'), res = document.getElementById('results');
let act = -1;
const hitLinks = () => [...res.querySelectorAll('a')];
q.addEventListener('keydown', e => {
  const it = hitLinks(), n = it.length;
  if (e.key==='ArrowDown' || e.key==='ArrowUp') { if(!n) return; e.preventDefault(); const down = e.key==='ArrowDown';
    act = act<0 ? (down?0:n-1) : (act+(down?1:-1)+n)%n; it.forEach((a,i)=>a.classList.toggle('act',i===act)); it[act].scrollIntoView({block:'nearest'}); }
  else if (e.key==='Enter') { const a = it[act<0?0:act]; if(a){ e.preventDefault(); a.click(); } }
  else if (e.key==='Escape') { q.value=''; res.innerHTML=''; act=-1; }
});
q.addEventListener('input', () => {
  const v = q.value.trim().toLowerCase(); res.innerHTML=''; act=-1;
  if (v.length < 2) return;
  const hits = idx.filter(x => x.n.toLowerCase().includes(v) || (x.d||'').toLowerCase().includes(v))
    .sort((a,b)=> (a.n.toLowerCase().startsWith(v)?0:1)-(b.n.toLowerCase().startsWith(v)?0:1)).slice(0,40);
  for (const h of hits){ const li=document.createElement('li'); const a=document.createElement('a'); a.href='#'+h.a;
    a.textContent=h.n; const s=document.createElement('span'); s.className='kind'; s.textContent=h.k; a.appendChild(s); li.appendChild(a); res.appendChild(li);}
  if(!hits.length){res.innerHTML='<li class="muted small" style="padding:4px 8px">No match</li>';}
});
document.addEventListener('keydown', e => { if(e.key==='/' && document.activeElement!==q){ e.preventDefault(); q.focus(); }});
// narrow windows: the table of contents folds away so the page starts with content
const navEl=document.querySelector('nav'), navBtn=document.querySelector('.navtoggle');
if(navBtn){ navBtn.addEventListener('click',()=>{ const o=navEl.classList.toggle('open'); navBtn.setAttribute('aria-expanded',String(o)); });
  navEl.querySelectorAll('.toc a').forEach(a=>a.addEventListener('click',()=>{ navEl.classList.remove('open'); navBtn.setAttribute('aria-expanded','false'); })); }
// diagram pan / zoom / focus / table panel
document.querySelectorAll('.dwrap').forEach(wrap => {
  const box = wrap.querySelector('.diagram'), svg = box.querySelector('svg'); if(!svg) return;
  const pane = wrap.querySelector('.dpanel>div'), home = pane.innerHTML, hint = box.querySelector('.dhint');
  const dataEl = wrap.nextElementSibling;
  const T = dataEl && dataEl.classList.contains('erd-data') ? JSON.parse(dataEl.textContent) : {};
  const vb0 = svg.viewBox.baseVal; let vb = {x:vb0.x,y:vb0.y,w:vb0.width,h:vb0.height}; const orig={...vb};
  const fitH=()=>{ wrap.classList.toggle('stack', wrap.clientWidth<860); const s=Math.min(box.clientWidth/orig.w,1.3); box.style.height=Math.round(Math.min(Math.max(orig.h*s+16,340),wrap.classList.contains('stack')?460:680))+'px'; };
  fitH(); window.addEventListener('resize',fitH);
  const set=()=>svg.setAttribute('viewBox',`${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
  const zoom=(f,cx,cy)=>{const r=svg.getBoundingClientRect(); cx=cx??r.width/2; cy=cy??r.height/2;
    const px=vb.x+vb.w*cx/r.width, py=vb.y+vb.h*cy/r.height; vb.w*=f; vb.h*=f; vb.x=px-vb.w*cx/r.width; vb.y=py-vb.h*cy/r.height; set();};
  // the wheel zooms only with Ctrl / Cmd (and on pinch); without it the page keeps scrolling
  let hintT=null;
  box.addEventListener('wheel',e=>{ if(!(e.ctrlKey||e.metaKey)){ if(hint){ hint.hidden=false; clearTimeout(hintT); hintT=setTimeout(()=>{hint.hidden=true;},1400);} return; }
    e.preventDefault(); const r=svg.getBoundingClientRect(); zoom(e.deltaY>0?1.12:1/1.12,e.clientX-r.left,e.clientY-r.top);},{passive:false});
  let drag=null;
  box.addEventListener('pointerdown',e=>{ if(e.target.closest('.dtools'))return; drag={x:e.clientX,y:e.clientY,vx:vb.x,vy:vb.y,moved:false}; box.style.cursor='grabbing';});
  window.addEventListener('pointermove',e=>{ if(!drag)return; const r=svg.getBoundingClientRect(); const s=Math.max(vb.w/r.width, vb.h/r.height);
    if(Math.abs(e.clientX-drag.x)+Math.abs(e.clientY-drag.y)>3) drag.moved=true; vb.x=drag.vx-(e.clientX-drag.x)*s; vb.y=drag.vy-(e.clientY-drag.y)*s; set();});
  window.addEventListener('pointerup',e=>{ const d=drag; drag=null; box.style.cursor=''; if(d && !d.moved) clickAt(e.target);});
  const el=(tag,cls,txt)=>{ const e=document.createElement(tag); if(cls) e.className=cls; if(txt!=null) e.textContent=txt; return e; };
  const links=(title,items)=>{ if(!items.length) return; pane.append(el('h5','',title+' ('+items.length+')')); const ul=el('ul');
    items.forEach(m=>{ const li=el('li'), a=el('a','',m[0]); a.href='#'+m[1]; li.append(a); ul.append(li); }); pane.append(ul); };
  const showPanel=(name)=>{ const t=T[name]; if(!t){ pane.innerHTML=home; return; } pane.innerHTML='';
    pane.append(el('p','pk',t.r), el('h4','',name));
    if(t.d){ const p=el('p','',t.d); if(t.p){ p.append(' ', el('span','badge '+(t.p==='auto'?'auto':'ai'),t.p)); } pane.append(p); }
    const a=el('a','','Full description of this table'); a.href='#'+t.a; const pa=el('p','small'); pa.append(a); pane.append(pa);
    if(t.j.length){ pane.append(el('h5','','Joins')); const ul=el('ul'); t.j.forEach(j=>ul.append(el('li','',j))); pane.append(ul); }
    links('Measures in this table',t.m); links('Measures that read this table',t.rm);
    if(t.c.length){ pane.append(el('h5','','Columns ('+t.c.length+')')); const ul=el('ul');
      t.c.forEach(c=>{ const li=el('li'); li.append(el('span','',c[0])); const f=[c[1]]; if(c[2].includes('k')) f.push('key');
        if(c[2].includes('c')) f.push('calculated'); if(c[2].includes('h')) f.push('hidden'); li.append(el('span','ty',f.filter(Boolean).join(' · '))); ul.append(li); }); pane.append(ul); }
    pane.append(el('h5','','Feeds report pages'), el('p','small',t.pg.length?t.pg.join(', '):'No page of the packaged report reads this table.'));
    pane.scrollTop=0; };
  const clickAt=(t)=>{ const n=t.closest && t.closest('.node'); svg.classList.remove('focus');
    svg.querySelectorAll('.on,.sel').forEach(x=>x.classList.remove('on','sel')); if(!n){ showPanel(null); return; }
    const name=n.dataset.t; svg.classList.add('focus'); n.classList.add('on','sel');
    svg.querySelectorAll('.edge').forEach(ed=>{ if(ed.dataset.a===name||ed.dataset.b===name){ ed.classList.add('on');
      const other=ed.dataset.a===name?ed.dataset.b:ed.dataset.a; svg.querySelectorAll('.node').forEach(m=>{ if(m.dataset.t===other) m.classList.add('on');});}});
    showPanel(name);
  };
  svg.querySelectorAll('.node').forEach(n=>{
    n.addEventListener('keydown',e=>{ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); clickAt(n); } else if(e.key==='Escape'){ clickAt(document.body); } });
    n.addEventListener('dblclick',()=>{ const t=T[n.dataset.t]; if(t) location.hash='#'+t.a; });
  });
  box.querySelector('[data-z="in"]').onclick=()=>zoom(1/1.25);
  box.querySelector('[data-z="out"]').onclick=()=>zoom(1.25);
  box.querySelector('[data-z="fit"]').onclick=()=>{vb={...orig}; set(); clickAt(document.body);};
});
// lineage explorer: what an object is built from (left) and what depends on it (right)
(function(){
  const src=document.getElementById('lineage-data'); if(!src) return;
  const G=JSON.parse(src.textContent), N=G.n, out=N.map(()=>[]), inn=N.map(()=>[]), byId={}, byName={};
  G.e.forEach(([a,b])=>{ out[a].push(b); inn[b].push(a); });
  const host=document.getElementById('lin-graph'), sum=document.getElementById('lin-sum'), inp=document.getElementById('lin-q'), dl=document.getElementById('lin-list');
  const full=n=> n[2]==='c' ? n[4]+'['+n[1]+']' : n[2]==='r' ? n[1]+' (report measure)' : n[1];
  const pick=k=> k==='m'||k==='c'||k==='t'||k==='r';
  N.forEach((n,i)=>{ byId[n[0]]=i; if(pick(n[2])){ const o=document.createElement('option'); o.value=full(n); dl.appendChild(o); byName[full(n).toLowerCase()]=i; } });
  const NS='http://www.w3.org/2000/svg';
  const mk=(tag,attrs,parent)=>{ const e=document.createElementNS(NS,tag); for(const k in attrs) e.setAttribute(k,attrs[k]); if(parent) parent.appendChild(e); return e; };
  const reach=(s,adj)=>{ const seen=new Set([s]), todo=[s]; while(todo.length){ const x=todo.pop(); for(const y of adj[x]) if(!seen.has(y)){ seen.add(y); todo.push(y); } } return seen; };
  const W=176,H=34,GX=40,GY=8,CAP=18, isM=k=> k==='m'||k==='r', cut=(t,n)=> t.length>n ? t.slice(0,n-1)+'…' : t, pl=(n,w)=> n+' '+w+(n===1?'':'s');
  let cur=-1;
  function draw(s){
    cur=s; const up=reach(s,inn), down=reach(s,out);
    // columns: model objects | measures it is built from | selected | direct dependents | indirect dependents | visuals
    const cols=new Map(), put=(c,i)=>{ if(!cols.has(c)) cols.set(c,[]); cols.get(c).push(i); };
    up.forEach(i=>{ if(i!==s) put(isM(N[i][2]) ? -1 : -2, i); });
    down.forEach(i=>{ if(i!==s){ const k=N[i][2]; put(k==='v'||k==='o' ? 3 : (out[s].includes(i) ? 1 : 2), i); } });
    put(0,s);
    const keys=[...cols.keys()].sort((a,b)=>a-b), ord=i=>N[i][2]+'|'+N[i][4]+'|'+N[i][1].toLowerCase();
    let rows=1; keys.forEach(c=>{ const list=cols.get(c).sort((a,b)=> ord(a)<ord(b)?-1:1), vis=list.slice(0,CAP);
      cols.set(c,{vis,more:list.length-vis.length}); rows=Math.max(rows,vis.length+(list.length>vis.length?1:0)); });
    const top=24, width=keys.length*W+(keys.length-1)*GX+2, height=top+rows*(H+GY);
    host.innerHTML=''; const svg=mk('svg',{width,height,viewBox:`0 0 ${width} ${height}`,style:`max-width:100%;height:auto;min-width:${Math.round(width*0.8)}px`},host), ge=mk('g',{},svg), gn=mk('g',{},svg), pos=new Map();
    keys.forEach((c,ci)=>{ const x=1+ci*(W+GX), col=cols.get(c), n=col.vis.length+(col.more?1:0), y0=top+(rows-n)*(H+GY)/2, kinds=new Set(col.vis.map(i=>N[i][2]));
      const head={'-2':'Reads from the model','-1':'Built from measures','0':'Selected','1':'Depends on it directly','2':'Depends on it indirectly','3':'Report visuals and other'}[c];
      mk('text',{class:'ch',x,y:12},svg).textContent=head;
      col.vis.forEach((i,ri)=>pos.set(i,{x,y:y0+ri*(H+GY)}));
      if(col.more){ const g=mk('g',{class:'ln more',transform:`translate(${x},${y0+col.vis.length*(H+GY)})`},gn); mk('text',{class:'t',x:10,y:21},g).textContent='+ '+col.more+' more'; } });
    const go=i=>{ const k=N[i][2]; if(pick(k)) select(i,false); else if(k==='v' && N[i][3]) location.hash='#'+N[i][3]; };
    pos.forEach((p,i)=>{ const n=N[i], k=n[2], g=mk('g',{class:'ln k-'+k+(i===s?' sel':''),transform:`translate(${p.x},${p.y})`,tabindex:0,role:'button'},gn);
      mk('title',{},g).textContent=full(n)+(k==='v'?' on page '+n[4]:'');
      mk('rect',{width:W,height:H,rx:2},g); if(k==='v') mk('rect',{class:'bar',width:3,height:H},g);
      const sub= k==='c' ? n[4] : k==='v' ? 'page '+n[4] : k==='r' ? 'report measure' : k==='t' ? 'table' : '';
      mk('text',{class:'t',x:10,y:sub?14:21},g).textContent=cut(n[1],25); if(sub) mk('text',{class:'s',x:10,y:27},g).textContent=cut(sub,32);
      g.addEventListener('click',()=>go(i)); g.addEventListener('keydown',e=>{ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); go(i); } });
      g.addEventListener('mouseenter',()=>{ host.classList.add('hov'); ge.querySelectorAll('.le').forEach(l=>l.classList.toggle('hl', l.dataset.a==i||l.dataset.b==i)); });
      g.addEventListener('mouseleave',()=>host.classList.remove('hov')); });
    G.e.forEach(([a,b])=>{ const A=pos.get(a), B=pos.get(b); if(!A||!B) return; if(!((up.has(a)&&up.has(b))||(down.has(a)&&down.has(b)))) return;
      const y1=A.y+H/2, y2=B.y+H/2; let d;
      if(A.x===B.x) d=`M${A.x},${y1} C${A.x-24},${y1} ${A.x-24},${y2} ${A.x},${y2}`;      // same column: bracket on the left
      else { const x1=A.x<B.x?A.x+W:A.x, x2=A.x<B.x?B.x:B.x+W, dx=(x2-x1)/2; d=`M${x1},${y1} C${x1+dx},${y1} ${x2-dx},${y2} ${x2},${y2}`; }
      mk('path',{class:'le'+(a===s||b===s?' d':''),d,'data-a':a,'data-b':b},ge); });
    // one sentence above the graph
    const n=N[s], um=[...up].filter(i=>i!==s&&(N[i][2]==='m'||N[i][2]==='r')).length, uc=[...up].filter(i=>i!==s&&N[i][2]==='c').length, ut=[...up].filter(i=>i!==s&&N[i][2]==='t').length;
    const parts=[]; if(um) parts.push(pl(um,'measure')); if(uc) parts.push(pl(uc,'column')); if(ut) parts.push(pl(ut,'table'));
    sum.textContent=''; const b=document.createElement('b'); b.textContent=full(n); sum.append(b);
    sum.append(parts.length ? ' is built from '+parts.join(' and ')+'. ' : (n[2]==='c' ? ' is a column of '+n[4]+'. ' : n[2]==='t' ? ' is a table. ' : ' does not read other objects. '));
    const dm=[...down].filter(i=>i!==s&&(N[i][2]==='m'||N[i][2]==='r')).length, dv=[...down].filter(i=>N[i][2]==='v').length;
    sum.append(n[5] || (down.size>1 ? 'Changing it affects '+pl(dm,'measure')+' and '+pl(dv,'visual')+'.' : 'Nothing in the packaged report(s) depends on it.'));
    if(n[3]){ sum.append(' '); const a=document.createElement('a'); a.href='#'+n[3]; a.textContent= n[2]==='m' ? 'Formula and details' : 'Table details'; sum.append(a); }
    inp.value=full(n); document.querySelectorAll('.chip[data-node]').forEach(c=>c.classList.toggle('on', c.dataset.node===n[0]));
  }
  function select(i,jump){ draw(i); if(jump) document.getElementById('lineage').scrollIntoView(); }
  const fromInput=()=>{ const i=byName[inp.value.trim().toLowerCase()]; if(i!==undefined && i!==cur) draw(i); };
  inp.addEventListener('input',fromInput); inp.addEventListener('change',fromInput); inp.addEventListener('focus',()=>inp.select());
  document.querySelectorAll('[data-node]').forEach(b=>b.addEventListener('click',()=>{ const i=byId[b.dataset.node]; if(i!==undefined) select(i, b.classList.contains('lin-btn')); }));
  draw(byId[G.s]!==undefined ? byId[G.s] : 0);
})();
// measures: one line each; filter by text, usage and folder; a link to a measure opens its card
(function(){
  const cards=[...document.querySelectorAll('details.measure')], bar=document.getElementById('mfilter');
  let used='all';
  const q=bar&&bar.querySelector('input'), sel=bar&&bar.querySelector('select'), count=bar&&bar.querySelector('.mcount');
  const apply=()=>{ if(!bar) return; const t=q.value.trim().toLowerCase(), f=sel?sel.value:''; let n=0;
    cards.forEach(c=>{ const ok=(!t||c.dataset.s.includes(t))&&(used==='all'||c.dataset.used===used)&&(!f||c.dataset.folder===f); c.hidden=!ok; if(ok) n++; });
    document.querySelectorAll('.mgroup,.mtab').forEach(g=>{ g.hidden=!g.querySelector('details.measure:not([hidden])'); });
    count.textContent= n===cards.length ? '' : n+' of '+cards.length+' shown'; };
  const clear=()=>{ if(!bar) return; q.value=''; if(sel) sel.value=''; used='all'; bar.querySelectorAll('[data-used]').forEach(b=>b.classList.toggle('on',b.dataset.used==='all')); apply(); };
  if(bar){ q.addEventListener('input',apply); if(sel) sel.addEventListener('change',apply);
    bar.querySelectorAll('[data-used]').forEach(b=>b.addEventListener('click',()=>{ used=b.dataset.used;
      bar.querySelectorAll('[data-used]').forEach(x=>x.classList.toggle('on',x===b)); apply(); }));
    bar.querySelectorAll('[data-all]').forEach(b=>b.addEventListener('click',()=>cards.forEach(c=>{ if(!c.hidden) c.open=b.dataset.all==='open'; }))); }
  // a link, a search hit or the address bar pointing at a measure (or into one) opens that card
  const reveal=()=>{ let id=''; try{ id=decodeURIComponent(location.hash.slice(1)); }catch(e){} const t=id&&document.getElementById(id); if(!t) return;
    const d=t.closest('details'); if(!d) return; if(d.hidden) clear(); if(!d.open){ d.open=true; t.scrollIntoView({block:'start'}); } };
  window.addEventListener('hashchange',reveal);
  document.addEventListener('click',e=>{ const a=e.target.closest&&e.target.closest('a[href^="#"]'); if(a) setTimeout(reveal,0); });
  reveal();
  // printing shows every card in full
  let closed=[];
  window.addEventListener('beforeprint',()=>{ closed=cards.filter(c=>!c.open); closed.forEach(c=>c.open=true); });
  window.addEventListener('afterprint',()=>{ closed.forEach(c=>c.open=false); closed=[]; });
})();
// copy buttons on code blocks (DAX, M, SQL)
const copyText=(t)=>{ if(navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(t);
  return new Promise((ok,fail)=>{ const ta=document.createElement('textarea'); ta.value=t; ta.setAttribute('readonly','');
    ta.style.position='fixed'; ta.style.opacity='0'; document.body.appendChild(ta); ta.select();
    try{ document.execCommand('copy')?ok():fail(); }catch(e){ fail(e); } finally{ ta.remove(); } }); };
document.querySelectorAll('main pre').forEach(pre => {
  const box=document.createElement('div'); box.className='codebox'; pre.parentNode.insertBefore(box, pre); box.appendChild(pre);
  const b=document.createElement('button'); b.type='button'; b.className='copy'; b.textContent='Copy'; b.title='Copy to clipboard';
  b.onclick=()=>{ copyText(pre.textContent).then(()=>{ b.textContent='Copied'; b.classList.add('done'); },()=>{ b.textContent='Press Ctrl+C';
      const r=document.createRange(); r.selectNodeContents(pre); const s=getSelection(); s.removeAllRanges(); s.addRange(r); })
    .finally(()=>setTimeout(()=>{ b.textContent='Copy'; b.classList.remove('done'); },1600)); };
  box.appendChild(b);
});
// active nav item
const links=[...document.querySelectorAll('nav li a')]; const secs=links.map(a=>document.getElementById(a.getAttribute('href').slice(1))).filter(Boolean);
const obs=new IntersectionObserver(es=>{es.forEach(e=>{ if(e.isIntersecting){ links.forEach(l=>l.classList.toggle('active', l.getAttribute('href')==='#'+e.target.id)); }});},{rootMargin:'0px 0px -75% 0px'});
secs.forEach(s=>obs.observe(s));
})();
"""


def _badge(prov: Optional[str]) -> str:
    if prov == "ai":
        return '<span class="badge ai" title="Drafted by an AI agent from the extracted facts. Review before relying on it.">AI-drafted · review</span>'
    if prov == "auto":
        return '<span class="badge auto" title="Generated from the DAX pattern">auto</span>'
    return ""


def _desc_html(d: Optional[str], prov: Optional[str], empty: str = "No description yet.") -> str:
    if not d:
        return f'<p class="muted small">{escape(empty)}</p>'
    return f"<p>{escape(d)}{_badge(prov)}</p>"


def render_html(ctx: Ctx) -> str:
    H: List[str] = []
    shown = [True]

    def h(x: str) -> None:
        if shown[0]:
            H.append(x)
    nav: List[Tuple[str, str, bool]] = []
    has_model = bool(ctx.models)
    observed = [r for r in ctx.reports if r.get("observedModel")]
    index: List[dict] = []
    measure_names = {me["name"] for m in ctx.models for t in m["tables"] for me in t["measures"]}

    def section(id_: str, title: str, show: bool = True):
        """Open a section. With show=False everything up to the next section is dropped (no bare headings)."""
        shown[0] = show
        if show:
            nav.append((id_, title, False))
            h(f'<section id="{id_}"><h2>{escape(title)}</h2>')

    # header --------------------------------------------------------------------
    h('<header class="doc">')
    h('<div class="kicker">Power BI documentation · report &amp; semantic model</div>')
    h(f"<h1>{escape(ctx.title)}</h1>")
    h(f'<div class="meta">Power BI report &amp; semantic model documentation · generated {escape(ctx.pack["generatedAt"])} '
      f'from <code>{escape(ctx.pack["source"]["name"])}</code> · {escape(ctx.pack["generator"])}</div>')
    h("</header>")

    # overview ------------------------------------------------------------------
    section("overview", "Overview")
    ri = ctx.report_info()
    if any(ri.get(k) for k in ("purpose", "audience", "howToUse")):
        for key, label in (("purpose", "Purpose"), ("audience", "Audience"), ("howToUse", "How to use this report"),
                           ("dataFreshness", "Data freshness"), ("owner", "Owner")):
            if ri.get(key):
                h(f'<p class="lead"><b>{label}.</b> {escape(ri[key])}{_badge("ai")}</p>')
    notes = [n for n in (ctx.enr.get("notes") or []) if isinstance(n, str) and n.strip()]
    if notes:
        h('<div class="find info"><div class="sev">Open points for review' + _badge("ai") + '</div><ul>'
          + "".join(f"<li>{escape(n)}</li>" for n in notes) + "</ul></div>")
    brief = model_brief(ctx)
    if brief:
        h('<div class="brief" id="brief"><div class="bk">In brief<span class="badge auto" '
          'title="Generated from the extracted facts, not written by a person">auto</span></div><dl>'
          + "".join(f"<dt>{escape(label)}</dt><dd>{escape(text)}</dd>" for label, text in brief) + "</dl>")
        jump = []
        if ctx.models:
            jump.append('<a href="#model">See the data model</a>')
            jump.append('<a href="#lineage">What breaks if something changes?</a>')
        if observed:
            jump.append('<a href="#observed">What the report uses from the model</a>')
        jump.append('<a href="#pages">What each report page shows</a>')
        h('<p class="jump">' + " · ".join(jump) + "</p></div>")
    if not any(ri.get(k) for k in ("purpose", "audience", "howToUse")):
        h('<p class="muted small">The business purpose and audience of this report are not described yet. '
          'They can be added with the enrichment template.</p>')
    for m in ctx.models:
        st = m["analysis"]["stats"]
        nf = len(m["analysis"]["findings"])
        h('<div class="tiles">')
        for v, l in ((sum(len(r["pages"]) for r in ctx.reports), "report pages"), (st["dataVisuals"], "data visuals"),
                     (st["tables"], "tables"), (st["measures"], "measures"), (st["relationships"], "relationships"),
                     (st["columns"], "columns"), (st["roles"], "RLS roles"), (nf, "health findings")):
            h(f'<div class="tile"><div class="v">{v}</div><div class="l">{l}</div></div>')
        h("</div>")
        h(f'<p class="small muted">Semantic model <b>{escape(m["name"])}</b> · {m["format"].upper()} · compatibility level '
          f'{escape(str(m.get("compatibilityLevel")))} · culture {escape(str(m.get("culture")))} · used by '
          f'{escape(", ".join(m["reports"]) or "no report in this package")}.</p>')
    for r in observed:
        c = r["observedModel"]["counts"]
        h('<div class="tiles">')
        for v, l in ((len(r["pages"]), "report pages"), (c["tables"], "tables used"), (c["columns"], "columns used"),
                     (c["measures"], "measures used"), (len(r.get("reportMeasures") or []), "measures in the report")):
            h(f'<div class="tile"><div class="v">{v}</div><div class="l">{l}</div></div>')
        h("</div>")
        h(f'<p class="small">Report <b>{escape(r["name"])}</b> uses a live connection: its semantic model is not in the package. '
          + ('This documentation covers the report and <a href="#observed">the part of the model the report uses</a>. '
             'Tables, relationships, DAX, data sources and model health need the PBIP of the semantic model itself.'
             if not has_model else 'See <a href="#observed">the part of that model the report uses</a>.') + "</p>")
    h("</section>")

    # model ---------------------------------------------------------------------
    section("model", "Data model", has_model)
    h('<p class="lead">How the tables are joined. Click a table to see its columns, measures and joins.</p>')
    for m in ctx.models:
        svg, w, hgt = diagram.render(m)
        panel_json = json.dumps(diagram_panel_data(ctx, m), ensure_ascii=False).replace("</", "<\\/")
        h(f'<h3>{escape(m["name"])}</h3>')
        h('<div class="dwrap"><div class="diagram"><div class="dtools"><button data-z="in" title="Zoom in" aria-label="Zoom in">+</button>'
          '<button data-z="out" title="Zoom out" aria-label="Zoom out">−</button><button class="wide" data-z="fit">Reset</button></div>'
          f'<div class="dhint" hidden>Hold Ctrl (⌘ on Mac) and scroll to zoom</div>{svg}</div>'
          '<aside class="dpanel" aria-live="polite"><div class="dph"><p class="pk">Table details</p>'
          '<p>Click a table in the diagram to see its columns, measures and joins here.</p>'
          '<p class="small muted">Drag to move the diagram. Ctrl + scroll or the buttons zoom. '
          'Hover a line for the join key. Double-click a table for its full description.</p></div></aside></div>'
          f'<script type="application/json" class="erd-data">{panel_json}</script>')
        h('<p class="legend">Header:<i style="background:#111111"></i>fact<i style="background:#F2F0EA"></i>dimension'
          '<i style="background:#B98E2C"></i>date<i style="background:#66645C"></i>bridge'
          '<i style="border-style:dashed"></i>parameter / helper · line: <b>1</b> one side, <b>*</b> many side, '
          'arrow = filter direction, dashed = inactive, dark red = both directions.</p>')
        h('<h4>Tables</h4><div class="tw"><table><thead><tr><th>Table</th><th>Role</th><th>Storage</th><th>Source</th>'
          '<th>Cols</th><th>Measures</th><th>Description</th></tr></thead><tbody>')
        for t in _tables_sorted(m):
            d, prov = ctx.table_desc(t)
            h(f'<tr><td><a href="#{ctx.anchor("t", t["name"])}">{escape(t["name"])}</a></td>'
              f'<td>{escape(ROLE_LABELS.get(t["role"], t["role"]))}</td><td>{escape(t.get("storageMode") or "")}</td>'
              f'<td class="small">{escape(_src_summary(t))}</td><td>{len(t["columns"])}</td><td>{len(t["measures"])}</td>'
              f'<td class="small">{escape(d or "")}{_badge(prov) if d else ""}</td></tr>')
        h("</tbody></table></div>")
        h('<h4>Relationships (join keys)</h4><div class="tw"><table><thead><tr><th>From (many side)</th><th>To (one side)</th>'
          '<th>Card.</th><th>Filter</th><th>Active</th><th>Notes</th></tr></thead><tbody>')
        for r in m["relationships"]:
            notes = []
            if r.get("fromDataType") and r.get("toDataType") and r["fromDataType"] != r["toDataType"]:
                notes.append(f"type mismatch {r['fromDataType']}/{r['toDataType']}")
            if not r["isActive"]:
                notes.append("activated via USERELATIONSHIP" if r["activatedByUserelationship"] else "never activated")
            h(f'<tr><td><a href="#{ctx.anchor("t", r["fromTable"])}">{escape(r["fromTable"])}</a>[{escape(r["fromColumn"])}]</td>'
              f'<td><a href="#{ctx.anchor("t", r["toTable"])}">{escape(r["toTable"])}</a>[{escape(r["toColumn"])}]</td>'
              f'<td>{r["cardinality"]}</td><td>{"both ↔" if r["crossFilteringBehavior"] == "bothDirections" else "single"}</td>'
              f'<td>{"yes" if r["isActive"] else "<b>no</b>"}</td><td class="small">{escape("; ".join(notes))}</td></tr>')
        h("</tbody></table></div>")
    h("</section>")

    # lineage -------------------------------------------------------------------
    graph = lineage_graph(ctx) if ctx.models else {"n": [], "e": []}
    graph_ids = {n[0] for n in graph["n"]} if graph["e"] else set()
    if graph["e"]:
        top = top_measures(ctx)
        graph["s"] = ("m:" + top[0][0]) if top else next((n[0] for n in graph["n"] if n[2] == "m"), graph["n"][0][0])
        section("lineage", "Lineage & change impact")
        h('<p class="lead">Pick a measure or a column. To its left is what it is built from, '
          'to its right everything that changes when it changes.</p>')
        h('<div class="lin-bar"><label for="lin-q">Show</label><input id="lin-q" list="lin-list" autocomplete="off" '
          'placeholder="Measure or Table[Column]"><datalist id="lin-list"></datalist>')
        if top:
            h('<span class="lin-chips"><span class="muted small">Most used:</span>' + "".join(
                f'<button type="button" class="chip" data-node="{escape("m:" + n)}">{escape(n)}</button>' for n, _ in top) + "</span>")
        h('</div><p id="lin-sum" class="lin-sum" aria-live="polite"></p>'
          '<div id="lin-graph" class="lin" role="group" aria-label="Lineage graph"></div>'
          '<p class="legend">Dark line = depends directly on the selected object, grey line = further along the chain. '
          'Click a measure or column to put it in the centre, a visual to open its report page.</p>')
        h('<script type="application/json" id="lineage-data">'
          + json.dumps(graph, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/") + "</script>")
        h("</section>")

    # observed model (thin report) ----------------------------------------------
    if observed:
        section("observed", "Observed model (live connection)")
        h('<p class="lead">The semantic model behind this report is not in the package. '
          'This is the part of it the report uses, read from its visuals and filters.</p>')
        for r in observed:
            om, ref = r["observedModel"], r.get("semanticModelRef") or {}
            c = om["counts"]
            h(f'<div class="card" id="{ctx.anchor("obs", r["name"])}"><h3>{escape(r["name"])}</h3>')
            h(f'<p>Connected to{(" <code>" + escape(str(ref.get("modelName"))) + "</code>") if ref.get("modelName") else " a published semantic model"}. '
              f'The report uses {_n(c["tables"], "table")}, {_n(c["columns"], "column")} and {_n(c["measures"], "measure")} of it. '
              f'{escape(om["note"])}</p>')
            rows = []
            for t in om["tables"]:
                for kind, items in (("measure", t["measures"]), ("column", t["columns"]), ("hierarchy level", t["hierarchies"])):
                    for e in items:
                        agg = f' <span class="muted small">({escape(", ".join(e["aggregations"]))} in visual)</span>' if e["aggregations"] else ""
                        rows.append(f"<tr><td>{escape(t['name'])}</td><td>{escape(e['name'])}{agg}</td><td>{kind}</td>"
                                    f"<td class=\"small\">{escape(', '.join(e['pages']) or 'report filter')}</td></tr>")
            h('<div class="tw"><table><thead><tr><th>Table</th><th>Field</th><th>Kind</th><th>Used on pages</th></tr></thead><tbody>'
              + "".join(rows) + "</tbody></table></div></div>")
        h("</section>")

    # pages ---------------------------------------------------------------------
    section("pages", "Report pages")
    h('<p class="lead">What each page shows, which numbers it uses and how you can filter it.</p>')
    for r in ctx.reports:
        if r["filters"]:
            h('<p><b>Report-level filters</b> (every page): ' + "; ".join(escape(_filter_txt(f)) for f in r["filters"]) + "</p>")
        if r.get("reportMeasures"):
            h(f'<div class="card"><h3>Report-level measures <span class="muted small">({len(r["reportMeasures"])}, defined in the report, not in the model)</span></h3>')
            for rm in r["reportMeasures"]:
                h(f'<h4>{escape(rm["name"])} <span class="muted small">on {escape(str(rm.get("table")))}</span></h4>'
                  f'<pre>{highlight_dax(rm.get("expression") or "", ctx, measure_names)}</pre>')
                h(impact_html(rm.get("impact"), f"[{rm['name']}]", ctx, compact=True,
                              node="r:" + rm["name"] if "r:" + rm["name"] in graph_ids else None))
            h("</div>")
        seen_pages: Dict[str, int] = defaultdict(int)
        many = len(ctx.reports) > 1
        for p in r["pages"]:
            pa = ctx.page_anchor(r["name"], p["displayName"], seen_pages[p["displayName"]])
            seen_pages[p["displayName"]] += 1
            nav.append((pa, p["displayName"] + (f" · {r['name']}" if many else ""), True))
            index.append({"n": p["displayName"], "k": "page" + (f" · {r['name']}" if many else ""), "a": pa,
                          "d": ctx.page_info(p).get("purpose", "")})
            tags = f'<span class="badge">{escape(r["name"])}</span>' if many else ""
            if p["hidden"]:
                tags += '<span class="badge hid">hidden</span>'
            if p["pageType"] != "Standard":
                tags += f'<span class="badge role">{escape(p["pageType"])}</span>'
            h(f'<div class="card" id="{pa}"><h3>{escape(p["displayName"])}{tags}</h3>')
            pi = ctx.page_info(p)
            if pi.get("purpose"):
                h(f'<p>{escape(pi["purpose"])}{_badge("ai")}</p>')
            if pi.get("keyQuestions"):
                h('<p class="small"><b>Questions it answers</b></p><ul class="small" style="margin-left:18px">' +
                  "".join(f"<li>{escape(q)}</li>" for q in pi["keyQuestions"]) + "</ul>")
            slicers = [v for v in p["visuals"] if "slicer" in v["type"].lower()]
            if slicers:
                chips = []
                for v in slicers:
                    for f in v["fields"]:
                        if f.get("role") not in ("formatting", "sort"):
                            t_ = v["title"] if v["title"] and not v["title"].startswith("(dynamic") else f["name"]
                            chips.append(f"<span>{escape(t_)}</span>")
                h(f'<p class="small"><b>Filter by</b></p><div class="chips">{"".join(chips)}</div>')
            for f in p["filters"]:
                h(f'<p class="small"><b>Page filter:</b> {escape(_filter_txt(f))}</p>')
            if p.get("drillthroughFields"):
                h('<p class="small"><b>Drill-through on:</b> ' + escape(", ".join(_fieldname(f) for f in p["drillthroughFields"])) + "</p>")
            h(_page_minimap(p))
            rows = []
            for v in p["visuals"]:
                if "slicer" in v["type"].lower():
                    continue
                shows, by = _split_fields(v)
                if v["type"] == "textbox" and v.get("text"):
                    rows.append(f'<tr><td>Text</td><td colspan="3" class="small">{escape(v["text"][:400])}</td></tr>')
                elif shows or by:
                    vf = "; ".join(fl["condition"] for fl in v["filters"] if fl.get("condition"))
                    vf_html = f'<div class="small muted">visual filter: {escape(vf)}</div>' if vf else ""
                    sh = ", ".join(_link_field(f, ctx) for f in shows)
                    bb = ", ".join(_link_field(f, ctx) for f in by)
                    rows.append(f"<tr><td>{escape(v['typeLabel'])}</td><td>{escape(v['title'] or '—')}{vf_html}</td><td>{sh}</td><td>{bb}</td></tr>")
            if rows:
                h('<div class="tw"><table><thead><tr><th>Visual</th><th>Title</th><th>Shows</th><th>By / columns</th></tr></thead><tbody>'
                  + "".join(rows) + "</tbody></table></div>")
            if p.get("lineage", {}).get("tables"):
                h('<p class="small muted">Data comes from: ' + ", ".join(_tlink(t, ctx) for t in p["lineage"]["tables"]) + "</p>")
            h("</div>")
    h("</section>")

    # glossary ------------------------------------------------------------------
    entries = glossary_entries(ctx)
    section("glossary", "Data glossary", bool(entries))
    h('<p class="lead">Every metric and attribute a report user sees, in plain language.</p>')
    letters = sorted({e["term"][0].upper() for e in entries if e["term"]})
    h('<p class="az">' + "".join(f'<a href="#gl-{escape(l)}">{escape(l)}</a>' for l in letters) + "</p>")
    h('<div class="tw"><table><thead><tr><th>Term</th><th>Kind</th><th>Definition</th><th>Format</th><th>Used on</th></tr></thead><tbody>')
    seen_letters = set()
    for e in entries:
        l = e["term"][0].upper() if e["term"] else "#"
        idattr = ""
        if l not in seen_letters:
            seen_letters.add(l)
            idattr = f' id="gl-{escape(l)}"'
        term = f'<a href="#{e["anchor"]}">{escape(e["term"])}</a>' if e["anchor"] else escape(e["term"])
        d = (escape(e["definition"]) + _badge(e["prov"])) if e["definition"] else '<span class="muted">—</span>'
        kind = escape(e["kind"]) + (f'<div class="small muted">{escape(e["table"])}</div>' if e["table"] else "")
        h(f"<tr{idattr}><td>{term}</td><td>{kind}</td><td>{d}</td><td><code>{escape(e['format'] or '')}</code></td>"
          f"<td class=\"small\">{escape(', '.join(e['pages']))}</td></tr>")
    h("</tbody></table></div></section>")

    # tables --------------------------------------------------------------------
    section("tables", "Tables", has_model)
    for m in ctx.models:
        rel_by_table = defaultdict(list)
        for r in m["relationships"]:
            rel_by_table[r["fromTable"]].append(r)
            rel_by_table[r["toTable"]].append(r)
        for t in _tables_sorted(m):
            ta = ctx.anchor("t", t["name"])
            nav.append((ta, t["name"], True))
            d, prov = ctx.table_desc(t)
            index.append({"n": t["name"], "k": "table", "a": ta, "d": d or ""})
            h(f'<div class="card" id="{ta}" data-table-anchor="{escape(t["name"])}"><h3>{escape(t["name"])}'
              f'<span class="badge role">{escape(ROLE_LABELS.get(t["role"], t["role"]))}</span>'
              f'{HIDDEN_BADGE if t["isHidden"] else ""}</h3>')
            h(_desc_html(d, prov))
            h('<dl class="kv">')
            src_link = f" · <a href=\"#{ctx.anchor('src', t['name'])}\">details</a>" if t["source"].get("type") else ""
            h(f"<dt>Source</dt><dd>{escape(_src_summary(t))}{src_link}</dd>")
            h(f"<dt>Storage mode</dt><dd>{escape(t.get('storageMode') or '—')}</dd>")
            if rel_by_table[t["name"]]:
                rels = "<br>".join(
                    f'{_tlink(r["fromTable"], ctx)}[{escape(r["fromColumn"])}] → {_tlink(r["toTable"], ctx)}[{escape(r["toColumn"])}] '
                    f'<span class="muted small">{r["cardinality"]}{", inactive" if not r["isActive"] else ""}'
                    f'{", both directions" if r["crossFilteringBehavior"] == "bothDirections" else ""}</span>'
                    for r in rel_by_table[t["name"]])
                h(f"<dt>Relationships</dt><dd>{rels}</dd>")
            if t["measures"]:
                h("<dt>Measures</dt><dd>" + ", ".join(f'<a href="#{ctx.anchor("m", me["name"])}">{escape(me["name"])}</a>'
                                                     for me in t["measures"]) + "</dd>")
            if t["hierarchies"]:
                h("<dt>Hierarchies</dt><dd>" + "<br>".join(
                    f"{escape(hh['name'])}: " + " › ".join(escape(l["name"]) for l in hh["levels"]) for hh in t["hierarchies"]) + "</dd>")
            h("</dl>")
            if t["columns"]:
                h('<div class="tw"><table><thead><tr><th>Column</th><th>Type</th><th>Kind</th><th>Format</th><th>Sort by</th>'
                  '<th>Description</th><th>Used on</th></tr></thead><tbody>')
                for c in t["columns"]:
                    cd, cp = ctx.column_desc(t, c)
                    ca = ctx.anchor("c", f"{t['name']}[{c['name']}]")
                    index.append({"n": f"{t['name']}[{c['name']}]", "k": "column", "a": ca, "d": cd or ""})
                    kind = {"data": "data", "calculated": "calculated", "calculatedTableColumn": "calc. table"}.get(c["columnType"], c["columnType"])
                    src = ""
                    if c["columnType"] == "data" and c.get("sourceColumn") and c["sourceColumn"] != c["name"]:
                        src = f'<div class="small muted">← {escape(c["sourceColumn"])}</div>'
                    flags = ("<span class=\"badge hid\">hidden</span>" if c["isHidden"] else "") + \
                            ("<span class=\"badge\">key</span>" if c.get("isKey") else "") + \
                            ("" if c["referenced"] or c["usedInReport"] else "<span class=\"badge unused\">unused</span>")
                    calc = ""
                    if c["columnType"] == "calculated" and c.get("expression"):
                        calc = f'<details><summary>DAX</summary><pre>{highlight_dax(c["expression"], ctx, measure_names)}</pre></details>'
                    ci = c.get("impact")
                    cnode = f"c:{t['name']}[{c['name']}]"
                    if ci and (ci["counts"]["measures"] or ci["counts"]["visuals"] or ci["counts"]["other"] or ci["counts"]["filters"]):
                        calc += (f'<details><summary>Change impact: {ci["counts"]["measures"]} measures · {ci["counts"]["visuals"]} visuals'
                                 f'</summary>{impact_html(ci, t["name"] + "[" + c["name"] + "]", ctx, compact=True, node=cnode if cnode in graph_ids else None)}</details>')
                    h(f"<tr id=\"{ca}\"><td>{escape(c['name'])}{flags}{calc}</td><td>{escape(c.get('dataType') or '')}</td><td>{kind}{src}</td>"
                      f"<td><code>{escape(c.get('formatString') or '')}</code></td><td>{escape(c.get('sortByColumn') or '')}</td>"
                      f"<td class=\"small\">{escape(cd or '')}{_badge(cp) if cd else ''}</td>"
                      f"<td class=\"small\">{escape(', '.join(_pages(c['usedBy'])))}</td></tr>")
                h("</tbody></table></div>")
            if t["source"].get("daxExpression"):
                h(f'<p class="small"><b>Table expression (DAX)</b></p><pre>{highlight_dax(t["source"]["daxExpression"], ctx, measure_names)}</pre>')
            if t.get("calculationGroup"):
                h('<p class="small"><b>Calculation items</b> (precedence ' + escape(str(t["calculationGroup"].get("precedence"))) + ")</p>")
                for it in t["calculationGroup"]["items"]:
                    h(f"<h4>{escape(it['name'])}</h4><pre>{highlight_dax(it['expression'] or '', ctx, measure_names)}</pre>")
            h("</div>")
    h("</section>")

    # measures ------------------------------------------------------------------
    section("measures", "Measures (DAX)", has_model)
    h('<p class="lead">One line per measure. Open a measure for its formula, what it depends on and where it is shown.</p>')
    all_measures = [me for m in ctx.models for t in _tables_sorted(m) for me in t["measures"]]
    if all_measures:
        n_used = sum(1 for me in all_measures if me["usedInReport"])
        folders = sorted({me["displayFolder"] for me in all_measures if me.get("displayFolder")}, key=str.lower)
        h('<div class="mbar" id="mfilter"><input type="search" placeholder="Filter measures…" aria-label="Filter measures">'
          '<span role="group" aria-label="Usage in the report">'
          f'<button type="button" class="chip on" data-used="all">All ({len(all_measures)})</button>'
          f'<button type="button" class="chip" data-used="1">Used in report ({n_used})</button>'
          f'<button type="button" class="chip" data-used="0">Not used ({len(all_measures) - n_used})</button></span>')
        if len(folders) > 1:
            h('<select aria-label="Display folder"><option value="">All folders</option>'
              + "".join(f'<option value="{escape(f)}">{escape(f)}</option>' for f in folders) + "</select>")
        h('<span class="mall"><button type="button" class="chip" data-all="open">Open all</button>'
          '<button type="button" class="chip" data-all="close">Close all</button></span>'
          '<span class="mcount" aria-live="polite"></span></div>')
    for m in ctx.models:
        for t in _tables_sorted(m):
            if not t["measures"]:
                continue
            h(f'<div class="mtab"><h3>{escape(t["name"])} <span class="muted small">({len(t["measures"])})</span></h3>')
            by_folder = defaultdict(list)
            for me in t["measures"]:
                by_folder[me.get("displayFolder") or ""].append(me)
            for folder in sorted(by_folder):
                h('<div class="mgroup">')
                if folder:
                    h(f'<p class="small muted">Folder: {escape(folder)}</p>')
                for me in by_folder[folder]:
                    ma = ctx.anchor("m", me["name"])
                    d, prov = ctx.measure_desc(me)
                    index.append({"n": me["name"], "k": "measure", "a": ma, "d": d or ""})
                    badges = ("<span class=\"badge hid\">hidden</span>" if me["isHidden"] else "") + \
                             ("" if me["usedInReport"] else "<span class=\"badge unused\">not used in report</span>") + \
                             f'<span class="badge">{escape(me["complexity"]["level"])}</span>'
                    reach = _reach(me)
                    find = " ".join(x for x in (me["name"], d or "", folder) if x).lower()
                    h(f'<details class="measure" id="{ma}" data-used="{1 if me["usedInReport"] else 0}" data-folder="{escape(folder)}" '
                      f'data-s="{escape(find)}"><summary><h4>{escape(me["name"])}{badges}</h4>'
                      f'<span class="md">{escape(d or "")}</span><span class="mr">{_n(reach, "visual") if reach else ""}</span>'
                      '</summary><div class="mb">')
                    h(_desc_html(d, prov))
                    h(f'<pre>{highlight_dax(me["expression"] or "", ctx, measure_names)}</pre>')
                    h('<dl class="kv">')
                    if me.get("formatString"):
                        h(f"<dt>Format</dt><dd><code>{escape(me['formatString'])}</code></dd>")
                    if me.get("formatStringExpression"):
                        h(f"<dt>Dynamic format</dt><dd><code>{escape(' '.join(me['formatStringExpression'].split()))}</code></dd>")
                    dep = me["dependsOn"]
                    if dep["measures"]:
                        h("<dt>Uses measures</dt><dd>" + ", ".join(f'<a href="#{ctx.anchor("m", x)}">{escape(x)}</a>' for x in dep["measures"]) + "</dd>")
                    if dep["columns"]:
                        h("<dt>Uses columns</dt><dd>" + ", ".join(f'{_tlink(x, ctx)}[{escape(y)}]' for x, y in dep["columns"]) + "</dd>")
                    if dep["tables"]:
                        h("<dt>Uses tables</dt><dd>" + ", ".join(_tlink(x, ctx) for x in dep["tables"]) + "</dd>")
                    lin = me["lineage"]
                    if lin["measures"] and set(lin["measures"]) != set(dep["measures"]):
                        h("<dt>Full measure chain</dt><dd class=\"small\">" + ", ".join(
                            f'<a href="#{ctx.anchor("m", x)}">{escape(x)}</a>' for x in lin["measures"]) + "</dd>")
                    if lin["columns"]:
                        h("<dt>Ultimately reads</dt><dd class=\"small\">" + escape(", ".join(lin["columns"])) + "</dd>")
                    if dep["userelationship"]:
                        h("<dt>Activates</dt><dd>" + escape(", ".join(f"{p[0][0]}[{p[0][1]}] ↔ {p[1][0]}[{p[1][1]}]" for p in dep["userelationship"])) + "</dd>")
                    used_m = sorted({u["by"][8:] for u in me["usedBy"] if u["kind"] == "measure"})
                    if used_m:
                        h("<dt>Used by measures</dt><dd>" + ", ".join(f'<a href="#{ctx.anchor("m", x)}">{escape(x)}</a>' for x in used_m) + "</dd>")
                    h("</dl>")
                    h(impact_html(me.get("impact"), f"[{me['name']}]", ctx, node="m:" + me["name"] if "m:" + me["name"] in graph_ids else None))
                    h("</div></details>")
                h("</div>")
            h("</div>")
    h("</section>")

    # security / params ---------------------------------------------------------
    section("security", "Security & parameters", has_model)
    any_ = False
    for m in ctx.models:
        if m["roles"]:
            any_ = True
            h("<h3>Row-level security</h3>")
            for r in m["roles"]:
                h(f'<h4>{escape(r["name"])} <span class="muted small">({escape(r.get("modelPermission") or "read")}, '
                  f'{r["memberCount"]} members in file)</span></h4>')
                for tp in r["tablePermissions"]:
                    h(f'<p class="small">{_tlink(tp["table"], ctx)}</p><pre>{highlight_dax(tp["filterExpression"] or "", ctx, measure_names)}</pre>')
        params = [e for e in m["expressions"] if e.get("isParameter")]
        if params:
            any_ = True
            h('<h3>Power Query parameters</h3><table><thead><tr><th>Parameter</th><th>Current value</th><th>Type</th></tr></thead><tbody>')
            for e in params:
                pi = e.get("parameter") or {}
                h(f"<tr><td>{escape(e['name'])}</td><td><code>{escape(str(pi.get('value')))}</code></td><td>{escape(pi.get('type') or '')}</td></tr>")
            h("</tbody></table>")
        for t in m["tables"]:
            if t["role"] in ("fieldParameter", "whatIfParameter"):
                any_ = True
                h(f'<h3>{escape(ROLE_LABELS[t["role"]])}: <a href="#{ctx.anchor("t", t["name"])}">{escape(t["name"])}</a></h3>')
                if t["source"].get("daxExpression"):
                    h(f'<pre>{highlight_dax(t["source"]["daxExpression"], ctx, measure_names)}</pre>')
    if not any_:
        h('<p class="muted">No row-level security, parameters or field parameters defined.</p>')
    h("</section>")

    # sources -------------------------------------------------------------------
    section("sources", "Data sources & Power Query", has_model)
    h('<p class="lead">Where each table comes from and what Power Query does before the data reaches the model.</p>')
    for m in ctx.models:
        for t in _tables_sorted(m):
            s = t["source"]
            if not s.get("type"):
                continue
            h(f'<div class="card" id="{ctx.anchor("src", t["name"])}"><h3>{_tlink(t["name"], ctx)}</h3>')
            h(f'<p>{escape(_src_summary(t))}</p><dl class="kv">')
            for c in s["connectors"]:
                via = f' <span class="muted small">via query {escape(c["via"])}</span>' if c.get("via") else ""
                h(f'<dt>{escape(c["kind"])}</dt><dd><code>{escape(c["function"])}</code>{via} '
                  + " ".join(f"<code>{escape(hide_embedded(str(x)))}</code>" for x in c["arguments"]) + "</dd>")
            for n in s["navigation"]:
                h("<dt>Navigation</dt><dd>" + escape(", ".join(f"{k} = {v}" for k, v in n.items() if v)) + "</dd>")
            for p in s["parameters"]:
                h(f"<dt>Parameter</dt><dd>{escape(p['name'])} = <code>{escape(str(p['value']))}</code></dd>")
            for j in s["joins"]:
                h(f"<dt>Join (Power Query)</dt><dd>{escape(j['left'])} {escape(str(j['leftKeys']))} = "
                  f"{escape(j['right'])} {escape(str(j['rightKeys']))} <span class=\"muted small\">{escape(j['kind'])}</span></dd>")
            for f in s["rowFilters"]:
                h(f"<dt>Row filter</dt><dd><code>{escape(f['condition'])}</code> <span class=\"muted small\">step {escape(f['step'])}</span></dd>")
            if s["steps"]:
                h("<dt>Applied steps</dt><dd class=\"small\">" + " → ".join(
                    escape(st["name"]) + (f' <span class="muted">({escape(st["function"])})</span>' if st.get("function") else "")
                    for st in s["steps"]) + "</dd>")
            h("</dl>")
            for q in s["nativeQueries"]:
                h(f'<p class="small"><b>Native SQL</b></p><pre>{escape(q)}</pre>')
            for p in t["partitions"]:
                if p.get("expression") and (p.get("sourceType") or "").lower() == "m":
                    h(f'<details><summary>Power Query (M) — partition {escape(p["name"] or "")}</summary><pre>{escape(hide_embedded(p["expression"]))}</pre></details>')
            h("</div>")
        shared = [e for e in m["expressions"] if not e.get("isParameter")]
        if shared:
            h("<h3>Shared queries</h3>")
            for e in shared:
                h(f'<h4>{escape(e["name"])}</h4><pre>{escape(hide_embedded(e["expression"]))}</pre>')
    h("</section>")

    # health --------------------------------------------------------------------
    section("health", "Model health", has_model)
    for m in ctx.models:
        an = m["analysis"]
        if not an["findings"]:
            h('<p class="muted">No findings.</p>')
        elif "priority" in an["findings"][0]:
            fs = an["findings"]
            nh = sum(1 for f in fs if f["priority"] == "high")
            hv = sum((f.get("impact") or {}).get("visuals", 0) for f in fs if f["priority"] == "high")
            h(f'<p class="small"><b>{_n(nh, "high-priority finding")}</b>'
              + (f', reaching {_n(hv, "visual")} in total (a visual can count under several findings).' if hv else '.')
              + f' <span class="muted">Sorted by priority: {escape(PRIORITY_RULE)}.</span></p>')
        for f in an["findings"]:
            pr = (f'<span class="prio {escape(f["priority"])}">{escape(f["priority"])} priority</span> · '
                  if f.get("priority") else "")
            h(f'<div class="find {f["severity"]}"><div class="sev" title="{escape(f["code"])}">{pr}{escape(f["severity"])}</div>'
              f'<div>{escape(f["message"])}</div>' + (f'<div class="small muted">{escape(f["advice"])}</div>' if f.get("advice") else ""))
            if f["objects"]:
                h("<ul>" + "".join(f"<li>{_link_objects(str(o), ctx)}</li>" for o in f["objects"][:25]) + "</ul>")
            imp = finding_impact_text(f)
            if imp:
                i = f.get("impact") or {}
                cls = "fimp hit" if i.get("visuals") else "fimp"
                h(f'<div class="{cls}"><b>Impact</b> {escape(imp)}')
                if i.get("visuals"):
                    more = i["visuals"] - len(i["examples"])
                    h('<span class="ex">e.g. ' + "; ".join(escape(x) for x in i["examples"])
                      + (f" and {more} more" if more > 0 else "") + "</span>"
                      + f'<span class="basis">Basis: {escape(i["basis"])}.</span>')
                h("</div>")
            h("</div>")
        h(f'<p class="small muted">{escape(an["unused"]["note"])}</p>')
        if an["unused"]["columns"]:
            h('<details><summary>Columns not referenced anywhere (' + str(len(an["unused"]["columns"])) + ')</summary><p class="small">'
              + escape(", ".join(an["unused"]["columns"])) + "</p></details>")
    h("</section>")

    # appendix ------------------------------------------------------------------
    section("appendix", "Appendix")
    h('<dl class="kv">')
    for r in ctx.reports:
        h(f"<dt>Report</dt><dd>{escape(r['name'])} · {r['format'].upper()} · theme "
          f"{escape(r['theme'].get('custom') or r['theme'].get('base') or '—')}</dd>")
        if r["bookmarks"]:
            h("<dt>Bookmarks</dt><dd>" + escape(", ".join(str(b.get("displayName")) for b in r["bookmarks"])) + "</dd>")
        h("<dt>Custom visuals</dt><dd>" + escape(", ".join(r["customVisuals"]) or "none") + "</dd>")
    for m in ctx.models:
        h(f"<dt>Model settings</dt><dd class=\"small\">{escape(', '.join(f'{k}={v}' for k, v in m.get('settings', {}).items()))}</dd>")
        if m.get("daxQueries"):
            h("<dt>DAX query files</dt><dd>" + escape(", ".join(q["name"] + ".dax" for q in m["daxQueries"])) + "</dd>")
    h(f"<dt>Package</dt><dd class=\"small\">{escape(', '.join(p['file'] for p in ctx.pack['source']['pbipFiles']))}</dd>")
    h("</dl></section>")

    # assemble ------------------------------------------------------------------
    navhtml = ['<nav><div class="brand">Power BI documentation</div>', f'<div class="rname">{escape(ctx.title)}</div>',
               '<input id="q" type="search" placeholder="Search measures, tables, pages…  ( / )" aria-label="Search">',
               '<ul id="results"></ul><button type="button" class="navtoggle" aria-expanded="false">Contents</button><ul class="toc">']
    for id_, title, sub in nav:
        if sub and id_.startswith("t-") and len([n for n in nav if n[0].startswith("t-")]) > 25:
            continue
        navhtml.append(f'<li class="{"sub" if sub else ""}"><a href="#{id_}">{escape(title)}</a></li>')
    navhtml.append("</ul></nav>")
    idx_json = json.dumps(index, ensure_ascii=False).replace("</", "<\\/")
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{escape(ctx.title)} — Power BI documentation</title><style>{CSS}</style></head><body>"
            f'<div class="layout">{"".join(navhtml)}<main>{"".join(H)}</main></div>'
            f'<script type="application/json" id="search-index">{idx_json}</script><script>{JS}</script></body></html>')


def impact_html(imp: Optional[dict], name: str, ctx: "Ctx", compact: bool = False, node: Optional[str] = None) -> str:
    if not imp:
        return ""
    out = [f'<div class="impact{" compact" if compact else ""}">']
    btn = (f'<button type="button" class="lin-btn" data-node="{escape(node)}" title="Show what it is built from and what '
           f'depends on it as a graph">Show as graph</button>') if node else ""
    if not compact:
        out.append(f'<div class="ik">Change impact{btn}</div>')
    out.append(f"<p>{escape(impact_summary(imp, name))}{btn if compact else ''}</p>")
    if imp["measures"]:
        out.append('<p class="small"><b>Dependent measures:</b> ' + ", ".join(
            f'<a href="#{ctx.anchor("m", x)}">{escape(x)}</a>' for x in imp["measures"]) + "</p>")
    rows = []
    for e in imp["visuals"]["direct"]:
        rows.append(f'<li><span class="tag d">direct</span>{escape(_vis_label(e))}</li>')
    for e in imp["visuals"]["indirect"]:
        via = " → ".join(e.get("path", [])[1:-1])
        rows.append(f'<li><span class="tag i">indirect</span>{escape(_vis_label(e))}'
                    + (f'<div class="via">via {escape(via)}</div>' if via else "") + "</li>")
    for f in imp["filters"]:
        via = " → ".join(f["path"][1:-1])
        rows.append(f'<li><span class="tag f">filter</span>{escape(f["where"])}' + (f'<div class="via">via {escape(via)}</div>' if via else "") + "</li>")
    for o in imp["other"]:
        rows.append(f'<li><span class="tag o">also</span>{escape(o)}</li>')
    if rows:
        out.append('<ul class="ilist">' + "".join(rows) + "</ul>")
    out.append("</div>")
    return "".join(out)


STATUS_BADGE = {
    "unresolved": '<span class="badge unused" title="Not found in the semantic model or the report-level measures. '
                  'Not proven broken: check for renamed or deleted fields.">unresolved</span>',
    "observedExternal": '<span class="badge" title="Used from a semantic model that is not in the package (live connection)">external model</span>',
    "reportMeasure": '<span class="badge" title="Measure defined in the report, not in the semantic model">report measure</span>',
}


def _link_objects(text: str, ctx: Ctx) -> str:
    """A finding's object as HTML: the measures and columns it names lead to their description."""
    if text in ctx.measure_names:
        return f'<a href="#{ctx.anchor("m", text)}">{escape(text)}</a>'
    out, pos = [], 0
    for m in re.finditer(r"\[([^\]]+)\]", text):
        inner, start = m.group(1), m.start()
        table = next((t for t in sorted(ctx.table_names, key=len, reverse=True)
                      if (t, inner) in ctx.column_names and text.endswith(t, pos, start)
                      and (start - len(t) == 0 or not text[start - len(t) - 1].isalnum())), None)
        if table:
            a, href = start - len(table), ctx.anchor("c", f"{table}[{inner}]")
        elif inner in ctx.measure_names and (start == 0 or not text[start - 1].isalnum()):
            a, href = start, ctx.anchor("m", inner)
        else:
            continue
        out.append(escape(text[pos:a]) + f'<a href="#{href}">{escape(text[a:m.end()])}</a>')
        pos = m.end()
    return "".join(out) + escape(text[pos:])


def _tlink(t: str, ctx: Ctx) -> str:
    if t not in ctx.table_names:                 # thin report: the table lives in a model outside the package
        return escape(t)
    return f'<a href="#{ctx.anchor("t", t)}">{escape(t)}</a>'


def _link_field(f: dict, ctx: Ctx) -> str:
    label = escape(f.get("displayName") or f["name"])
    if f["kind"] == "measure" and f.get("status", "verified") == "verified":
        return f'<a href="#{ctx.anchor("m", f["name"])}">{label}</a>'
    txt = label
    if f.get("aggregation"):
        txt = f"{escape(f['aggregation'])} of {label} <span class=\"badge unused\" title=\"Implicit measure\">implicit</span>"
    if f.get("variation"):
        txt += f" ({escape(f['variation'])})"
    if f.get("status") in STATUS_BADGE:
        txt += " " + STATUS_BADGE[f["status"]]
    tbl = f'<span class="muted">{_tlink(f["table"], ctx)}[</span>' if f.get("table") else ""
    close = '<span class="muted">]</span>' if f.get("table") else ""
    return tbl + txt + close


def _page_minimap(p: dict) -> str:
    w, hgt = p.get("width") or 1280, p.get("height") or 720
    try:
        w, hgt = float(w), float(hgt)
    except (TypeError, ValueError):
        return ""
    scale = 520.0 / w
    boxes = []
    for v in sorted(p["visuals"], key=lambda v: v["position"].get("z") or 0):
        pos = v["position"]
        if pos.get("x") is None or v["hidden"]:
            continue
        cls = "deco" if v["decorative"] and not v["fields"] else ("data" if v["fields"] else "")
        label = escape(v["title"] or v["typeLabel"]) if cls != "deco" else ""
        boxes.append(f'<div class="vb {cls}" title="{escape(v["typeLabel"])}: {escape(v["title"] or "")}" '
                     f'style="left:{pos["x"] * scale:.0f}px;top:{pos["y"] * scale:.0f}px;width:{max((pos.get("width") or 0) * scale, 4):.0f}px;'
                     f'height:{max((pos.get("height") or 0) * scale, 4):.0f}px">{label}</div>')
    if not boxes:
        return ""
    return (f'<details><summary>Page layout sketch</summary><div class="pagegrid" style="width:{w * scale:.0f}px;height:{hgt * scale:.0f}px">'
            + "".join(boxes) + "</div></details>")


# ============================================================================ entry

def build(pack: dict, out_dir: Path, enrichment: Optional[dict] = None) -> List[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    ctx = Ctx(pack, enrichment)
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", ctx.title).strip("-") or "report"
    md = out_dir / f"{base}-documentation.md"
    html = out_dir / f"{base}-documentation.html"
    md.write_text(render_md(Ctx(pack, enrichment)), encoding="utf-8")
    html.write_text(render_html(ctx), encoding="utf-8")
    written = [md, html]
    for m in pack["semanticModels"]:
        svg, _, _ = diagram.render(m)
        p = out_dir / ("model-diagram.svg" if len(pack["semanticModels"]) == 1 else f"model-diagram-{m['name']}.svg")
        p.write_text(svg, encoding="utf-8")
        written.append(p)
    return written
