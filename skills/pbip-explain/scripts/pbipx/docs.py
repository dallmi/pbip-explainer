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
from .analysis import ROLE_LABELS

HIDDEN_BADGE = '<span class="badge hid">hidden</span>'
PROV_LABEL = {"model": "", "ai": "AI-drafted · review", "auto": "auto"}


class Ctx:
    def __init__(self, pack: dict, enrichment: Optional[dict] = None):
        self.pack = pack
        self.enr = enrichment or {}
        self.models = pack["semanticModels"]
        self.reports = pack["reports"]
        self.title = self._title()
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
    def anchor(self, kind: str, name: str) -> str:
        key = (kind, name)
        if key not in self.slugs:
            base = kind + "-" + (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "x")
            s, i = base, 2
            while s in self._used:
                s, i = f"{base}-{i}", i + 1
            self._used.add(s)
            self.slugs[key] = s
        return self.slugs[key]

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


# ============================================================================ Markdown

def render_md(ctx: Ctx) -> str:
    L: List[str] = []
    a = L.append
    a(f"# {ctx.title} — Power BI documentation")
    a("")
    a(f"*Generated {ctx.pack['generatedAt']} from `{ctx.pack['source']['name']}` by {ctx.pack['generator']}. "
      "Facts are extracted from the PBIP files; text marked **AI-drafted** needs review; **auto** descriptions are "
      "pattern-generated from the DAX.*")
    a("")
    a("## Contents")
    a("")
    for s in ["Overview", "Report pages", "Data glossary", "Data model", "Tables", "Measures", "Security, parameters & calculation groups",
              "Data sources & Power Query", "Model health", "Appendix"]:
        a(f"- [{s}](#{re.sub(r'[^a-z0-9 -]', '', s.lower()).replace(' ', '-')})")
    a("")

    # Overview ------------------------------------------------------------------
    a("## Overview")
    a("")
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

    # Pages ---------------------------------------------------------------------
    a("## Report pages")
    a("")
    for r in ctx.reports:
        if r["filters"]:
            a("**Report-level filters (apply to every page):** " + "; ".join(_filter_txt(f) for f in r["filters"]))
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
    a("## Data glossary")
    a("")
    a("| Term | Kind | Table | Definition | Format | Used on pages |")
    a("|---|---|---|---|---|---|")
    for g in glossary_entries(ctx):
        d = g["definition"] or "*(no description yet)*"
        if g["prov"] in ("ai", "auto"):
            d += f" *({PROV_LABEL[g['prov']]})*"
        a(f"| {_md(g['term'])} | {g['kind']} | {_md(g['table'])} | {_md(d)} | {_md(g['format'] or '')} | {_md(', '.join(g['pages']))} |")
    a("")

    # Data model ----------------------------------------------------------------
    a("## Data model")
    a("")
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
    a("## Tables")
    a("")
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
    a("## Measures")
    a("")
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
                    used_m = [u["label"] for u in me["usedBy"] if u["kind"] == "measure"]
                    used_v = sorted({f"{u.get('visualTitle') or u.get('visual')} ({u.get('page')})" for u in me["usedBy"] if u.get("page")})
                    if used_m:
                        a("Used by: " + ", ".join(sorted(set(used_m))))
                    if used_v:
                        a("Shown in: " + ", ".join(used_v))
                    a("")

    # Security / params / calc groups --------------------------------------------
    a("## Security, parameters & calculation groups")
    a("")
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
    a("## Data sources & Power Query")
    a("")
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
                a(f"- `{c['function']}`{via}: {', '.join('`' + str(x) + '`' for x in c['arguments'])}")
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
                    a(p["expression"])
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
                a(e["expression"] or "")
                a("```")
                a("")

    # Health --------------------------------------------------------------------
    a("## Model health")
    a("")
    for m in ctx.models:
        an = m["analysis"]
        for f in an["findings"]:
            a(f"- **{f['severity'].upper()}** — {f['message']}" + (f" {f['advice']}" if f.get("advice") else ""))
            for o in f["objects"][:15]:
                a(f"  - {o}")
        a("")
        a(f"*{an['unused']['note']}*")
        a("")
        if an["unused"]["columns"]:
            a("Columns not referenced anywhere (visuals, measures, relationships, sort, RLS): " + ", ".join(an["unused"]["columns"]))
            a("")

    # Appendix ------------------------------------------------------------------
    a("## Appendix")
    a("")
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
:root{--primary:#E60000;--bordeaux:#8A000A;--bronze:#B98E2C;--g1:#CCCABC;--g2:#B8B3A2;--g3:#8E8D83;--g4:#7A7870;--g5:#5A5D5C;--g6:#404040;
--bg:#FFFFFF;--surface:#ECEBE4;--surface2:#F5F0E1;--row:#F8F7F2;--page:#F7F7F5;--ok:#6F7A1A;--warn:#E4A911;--bad:#BD000C;--info:#0C7EC6;
--font:"Frutiger 45 Light",Frutiger,"Helvetica Neue",Helvetica,Arial,"Segoe UI",system-ui,sans-serif;--mono:Consolas,"SF Mono",Menlo,monospace}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth;scroll-padding-top:16px}
body{font-family:var(--font);background:var(--bg);color:var(--g6);line-height:1.5;font-size:14px}
a{color:var(--g6);text-decoration:underline;text-decoration-color:var(--g2);text-underline-offset:2px}
a:hover{color:var(--primary)}
.layout{display:grid;grid-template-columns:270px 1fr;min-height:100vh}
nav{position:sticky;top:0;height:100vh;overflow:auto;border-right:1px solid var(--surface);padding:20px 16px;background:var(--bg)}
nav .brand{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--g4);margin-bottom:4px}
nav .rname{font-size:16px;font-weight:600;color:#000;margin-bottom:14px;word-break:break-word}
nav input{width:100%;padding:7px 9px;border:1px solid var(--g2);border-radius:2px;font:inherit;margin-bottom:14px}
nav input:focus{outline:2px solid var(--g6);outline-offset:0}
nav ul{list-style:none}
nav li a{display:block;padding:4px 8px;text-decoration:none;border-left:2px solid transparent;color:var(--g5);font-size:13.5px}
nav li a:hover,nav li a.active{border-left-color:var(--primary);color:#000;background:var(--row)}
nav li.sub a{padding-left:20px;font-size:12.5px}
#results{list-style:none;margin:-8px 0 12px;max-height:50vh;overflow:auto}
#results li a{display:block;padding:4px 8px;font-size:12.5px;text-decoration:none;border-bottom:1px solid var(--row)}
#results .kind{color:var(--g4);font-size:11px;margin-left:6px}
main{padding:32px 48px 80px;max-width:1280px;min-width:0}
header.doc h1{font-size:28px;font-weight:400;color:#000;line-height:1.2}
header.doc .meta{color:var(--g4);font-size:12.5px;margin:6px 0 20px}
h2{font-size:22px;font-weight:400;color:#000;margin:44px 0 12px;padding-top:14px;border-top:1px solid #000}
h3{font-size:17px;font-weight:600;color:#000;margin:26px 0 8px}
h4{font-size:14.5px;font-weight:600;color:#000;margin:18px 0 6px}
p{margin:6px 0}
.lead{font-size:15px;max-width:820px}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:1px;background:var(--surface);border:1px solid var(--surface);margin:12px 0 4px}
.tile{background:var(--bg);padding:12px 14px}
.tile .v{font-size:26px;color:#000;line-height:1.1}
.tile .l{font-size:12px;color:var(--g4)}
table{border-collapse:collapse;width:100%;margin:8px 0 14px;font-size:13px}
th{text-align:left;font-weight:600;font-size:12px;color:#000;border-bottom:1px solid #000;padding:6px 8px;vertical-align:bottom}
td{padding:5px 8px;border-bottom:1px solid var(--surface);vertical-align:top}
tbody tr:nth-child(even){background:var(--row)}
.tw{overflow-x:auto}
code,pre{font-family:var(--mono);font-size:12.5px}
code{background:var(--row);padding:0 3px;border-radius:2px}
pre{background:var(--page);border-left:2px solid var(--g2);padding:10px 14px;overflow:auto;margin:6px 0 10px;line-height:1.45}
pre .c{color:var(--g4);font-style:italic}pre .s{color:#946F29}pre .k{color:var(--bordeaux);font-weight:600}
pre .f{color:#2E476B;font-weight:600}pre .r{color:#295B40}pre a.m{color:#295B40;text-decoration-color:#7FA88F}
.badge{display:inline-block;font-size:10.5px;padding:0 6px;border:1px solid var(--g2);border-radius:2px;color:var(--g5);margin-left:6px;vertical-align:1px;white-space:nowrap}
.badge.ai{border-color:var(--bronze);color:#6C5312;background:#FBF6EA}
.badge.auto{border-color:var(--g2);color:var(--g4)}
.badge.hid{color:var(--g4)}
.badge.unused{border-color:var(--warn);color:#7a5a00}
.badge.role{background:var(--surface);border-color:var(--surface);color:var(--g6)}
.card{border:1px solid var(--surface);padding:14px 18px;margin:14px 0}
.card h3{margin-top:0}
.muted{color:var(--g4)}
.small{font-size:12.5px}
.chips span{display:inline-block;border:1px solid var(--surface);padding:1px 7px;margin:2px 4px 2px 0;font-size:12px}
.find{border-left:3px solid var(--g3);padding:8px 12px;margin:8px 0;background:var(--bg)}
.find.error{border-left-color:var(--bad)}.find.warning{border-left-color:var(--warn)}.find.info{border-left-color:var(--info)}
.find .sev{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--g4)}
.find ul{margin:4px 0 0 18px;font-size:12.5px;color:var(--g5)}
.diagram{border:1px solid var(--surface);position:relative;overflow:hidden;height:620px;background:var(--bg);cursor:grab}
.diagram svg{width:100%;height:100%}
.dtools{position:absolute;right:8px;top:8px;display:flex;gap:4px;z-index:2}
.dtools button{font:inherit;font-size:13px;width:30px;height:28px;border:1px solid var(--g2);background:var(--bg);border-radius:2px;cursor:pointer}
.dtools button.wide{width:auto;padding:0 8px}
.dtools button:hover{border-color:var(--g6)}
.legend{font-size:12px;color:var(--g4);margin:6px 0 0}
.legend i{display:inline-block;width:12px;height:10px;margin:0 4px -1px 10px;border:1px solid var(--g3)}
details{margin:4px 0}
summary{cursor:pointer;color:var(--g5);font-size:12.5px}
.measure{border-top:1px solid var(--surface);padding:10px 0 4px}
.measure h4{margin:0 0 4px}
.kv{display:grid;grid-template-columns:150px 1fr;gap:2px 12px;font-size:12.5px;margin:6px 0}
.kv dt{color:var(--g4)}
.pagegrid{position:relative;background:var(--page);border:1px solid var(--surface);margin:8px 0 12px}
.pagegrid .vb{position:absolute;border:1px solid var(--g2);background:#fff;font-size:10px;overflow:hidden;padding:2px 4px;color:var(--g5);line-height:1.2}
.pagegrid .vb.data{border-color:var(--g5)}
.pagegrid .vb.deco{background:transparent;border-style:dotted;color:transparent}
.az a{display:inline-block;min-width:20px;text-align:center;font-size:12.5px;text-decoration:none;color:var(--g5)}
@media (max-width:900px){.layout{grid-template-columns:1fr}nav{position:static;height:auto;border-right:0;border-bottom:1px solid var(--surface)}main{padding:20px 16px}}
@media print{nav,.dtools{display:none}.layout{display:block}main{max-width:none;padding:0}.diagram{height:auto;border:0}h2{break-before:page}}
"""

JS = r"""
(function(){
const idx = JSON.parse(document.getElementById('search-index').textContent);
const q = document.getElementById('q'), res = document.getElementById('results');
q.addEventListener('input', () => {
  const v = q.value.trim().toLowerCase(); res.innerHTML='';
  if (v.length < 2) return;
  const hits = idx.filter(x => x.n.toLowerCase().includes(v) || (x.d||'').toLowerCase().includes(v))
    .sort((a,b)=> (a.n.toLowerCase().startsWith(v)?0:1)-(b.n.toLowerCase().startsWith(v)?0:1)).slice(0,40);
  for (const h of hits){ const li=document.createElement('li'); const a=document.createElement('a'); a.href='#'+h.a;
    a.textContent=h.n; const s=document.createElement('span'); s.className='kind'; s.textContent=h.k; a.appendChild(s); li.appendChild(a); res.appendChild(li);}
  if(!hits.length){res.innerHTML='<li class="muted small" style="padding:4px 8px">No match</li>';}
});
document.addEventListener('keydown', e => { if(e.key==='/' && document.activeElement!==q){ e.preventDefault(); q.focus(); }});
// diagram pan / zoom / focus
document.querySelectorAll('.diagram').forEach(box => {
  const svg = box.querySelector('svg'); if(!svg) return;
  const vb0 = svg.viewBox.baseVal; let vb = {x:vb0.x,y:vb0.y,w:vb0.width,h:vb0.height}; const orig={...vb};
  const set=()=>svg.setAttribute('viewBox',`${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
  const zoom=(f,cx,cy)=>{const r=svg.getBoundingClientRect(); cx=cx??r.width/2; cy=cy??r.height/2;
    const px=vb.x+vb.w*cx/r.width, py=vb.y+vb.h*cy/r.height; vb.w*=f; vb.h*=f; vb.x=px-vb.w*cx/r.width; vb.y=py-vb.h*cy/r.height; set();};
  box.addEventListener('wheel',e=>{e.preventDefault(); const r=svg.getBoundingClientRect(); zoom(e.deltaY>0?1.12:1/1.12,e.clientX-r.left,e.clientY-r.top);},{passive:false});
  let drag=null;
  box.addEventListener('pointerdown',e=>{ if(e.target.closest('.dtools'))return; drag={x:e.clientX,y:e.clientY,vx:vb.x,vy:vb.y,moved:false}; box.style.cursor='grabbing';});
  window.addEventListener('pointermove',e=>{ if(!drag)return; const r=svg.getBoundingClientRect(); const s=Math.max(vb.w/r.width, vb.h/r.height);
    if(Math.abs(e.clientX-drag.x)+Math.abs(e.clientY-drag.y)>3) drag.moved=true; vb.x=drag.vx-(e.clientX-drag.x)*s; vb.y=drag.vy-(e.clientY-drag.y)*s; set();});
  window.addEventListener('pointerup',e=>{ const d=drag; drag=null; box.style.cursor=''; if(d && !d.moved) clickAt(e.target);});
  const clickAt=(t)=>{ const n=t.closest && t.closest('.node'); svg.classList.remove('focus');
    svg.querySelectorAll('.on,.sel').forEach(x=>x.classList.remove('on','sel')); if(!n) return;
    const name=n.dataset.t; svg.classList.add('focus'); n.classList.add('on','sel');
    svg.querySelectorAll('.edge').forEach(ed=>{ if(ed.dataset.a===name||ed.dataset.b===name){ ed.classList.add('on');
      const other=ed.dataset.a===name?ed.dataset.b:ed.dataset.a; svg.querySelectorAll('.node').forEach(m=>{ if(m.dataset.t===other) m.classList.add('on');});}});
    const info=box.parentElement.querySelector('.dinfo'); const link=document.querySelector(`[data-table-anchor="${CSS.escape(name)}"]`);
    if(info){ info.innerHTML=''; const b=document.createElement('b'); b.textContent=name; info.appendChild(b);
      if(link){ info.append(' · '); const a=document.createElement('a'); a.href='#'+link.id; a.textContent='open table details'; info.appendChild(a);} }
  };
  box.querySelector('[data-z="in"]').onclick=()=>zoom(1/1.25);
  box.querySelector('[data-z="out"]').onclick=()=>zoom(1.25);
  box.querySelector('[data-z="fit"]').onclick=()=>{vb={...orig}; set(); clickAt(document.body);};
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
    h = H.append
    nav: List[Tuple[str, str, bool]] = []
    index: List[dict] = []
    measure_names = {me["name"] for m in ctx.models for t in m["tables"] for me in t["measures"]}

    def section(id_: str, title: str):
        nav.append((id_, title, False))
        h(f'<section id="{id_}"><h2>{escape(title)}</h2>')

    # header --------------------------------------------------------------------
    h('<header class="doc">')
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
    if not any(ri.get(k) for k in ("purpose", "audience", "howToUse")):
        h('<p class="muted small">No business purpose captured yet. An agent can draft it into <code>enrichment.json</code> '
          '(see the skill), then re-run the docs.</p>')
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
    for r in ctx.reports:
        if r.get("semanticModelRef", {}) and r["semanticModelRef"].get("type") == "byConnection":
            h(f'<p class="small">Report <b>{escape(r["name"])}</b> is a thin report with a live connection to '
              f'<code>{escape(str(r["semanticModelRef"].get("modelName")))}</code>. The model is not in the package; fields are listed as the report uses them.</p>')
    h("</section>")

    # pages ---------------------------------------------------------------------
    section("pages", "Report pages")
    h('<p class="lead">What each page shows, which numbers it uses and how you can filter it.</p>')
    for r in ctx.reports:
        if r["filters"]:
            h('<p><b>Report-level filters</b> (every page): ' + "; ".join(escape(_filter_txt(f)) for f in r["filters"]) + "</p>")
        for p in r["pages"]:
            pa = ctx.anchor("p", p["displayName"])
            nav.append((pa, p["displayName"], True))
            index.append({"n": p["displayName"], "k": "page", "a": pa, "d": ctx.page_info(p).get("purpose", "")})
            tags = ""
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
                h('<p class="small muted">Data comes from: ' + ", ".join(
                    f'<a href="#{ctx.anchor("t", t)}">{escape(t)}</a>' for t in p["lineage"]["tables"]) + "</p>")
            h("</div>")
    h("</section>")

    # glossary ------------------------------------------------------------------
    section("glossary", "Data glossary")
    h('<p class="lead">Every metric and attribute a report user sees, in plain language.</p>')
    entries = glossary_entries(ctx)
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

    # model ---------------------------------------------------------------------
    section("model", "Data model")
    for m in ctx.models:
        svg, w, hgt = diagram.render(m)
        h(f'<h3>{escape(m["name"])}</h3>')
        h('<p class="small muted">Drag to pan, scroll to zoom, click a table to highlight its relationships. '
          'Hover a line for the join key.</p>')
        h(f'<div class="diagram"><div class="dtools"><button data-z="in" title="Zoom in">+</button>'
          f'<button data-z="out" title="Zoom out">−</button><button class="wide" data-z="fit">Reset</button></div>{svg}</div>')
        h('<p class="legend">Header:<i style="background:#404040"></i>fact<i style="background:#ECEBE4"></i>dimension'
          '<i style="background:#B98E2C"></i>date<i style="background:#7A7870"></i>bridge'
          '<i style="border-style:dashed"></i>parameter / helper · line: <b>1</b> one side, <b>*</b> many side, '
          'arrow = filter direction, dashed = inactive, dark red = both directions.</p><p class="dinfo small"></p>')
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

    # tables --------------------------------------------------------------------
    section("tables", "Tables")
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
            h(f"<dt>Source</dt><dd>{escape(_src_summary(t))} · <a href=\"#{ctx.anchor('src', t['name'])}\">details</a></dd>")
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
                    index.append({"n": f"{t['name']}[{c['name']}]", "k": "column", "a": ta, "d": cd or ""})
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
                    h(f"<tr><td>{escape(c['name'])}{flags}{calc}</td><td>{escape(c.get('dataType') or '')}</td><td>{kind}{src}</td>"
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
    section("measures", "Measures (DAX)")
    h('<p class="lead">Every measure with its formula, what it depends on and where it is shown.</p>')
    for m in ctx.models:
        for t in _tables_sorted(m):
            if not t["measures"]:
                continue
            h(f'<h3>{escape(t["name"])} <span class="muted small">({len(t["measures"])})</span></h3>')
            by_folder = defaultdict(list)
            for me in t["measures"]:
                by_folder[me.get("displayFolder") or ""].append(me)
            for folder in sorted(by_folder):
                if folder:
                    h(f'<p class="small muted">Folder: {escape(folder)}</p>')
                for me in by_folder[folder]:
                    ma = ctx.anchor("m", me["name"])
                    d, prov = ctx.measure_desc(me)
                    index.append({"n": me["name"], "k": "measure", "a": ma, "d": d or ""})
                    badges = ("<span class=\"badge hid\">hidden</span>" if me["isHidden"] else "") + \
                             ("" if me["usedInReport"] else "<span class=\"badge unused\">not used in report</span>") + \
                             f'<span class="badge">{escape(me["complexity"]["level"])}</span>'
                    h(f'<div class="measure" id="{ma}"><h4>{escape(me["name"])}{badges}</h4>')
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
                    vis = sorted({(u.get("page"), u.get("visualTitle") or u.get("visual") or u["kind"]) for u in me["usedBy"] if u.get("page")})
                    if vis:
                        h("<dt>Shown in</dt><dd class=\"small\">" + "<br>".join(
                            f'<a href="#{ctx.anchor("p", pg)}">{escape(pg)}</a>: {escape(str(vt))}' for pg, vt in vis) + "</dd>")
                    h("</dl></div>")
    h("</section>")

    # security / params ---------------------------------------------------------
    section("security", "Security & parameters")
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
    section("sources", "Data sources & Power Query")
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
                  + " ".join(f"<code>{escape(str(x))}</code>" for x in c["arguments"]) + "</dd>")
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
                    h(f'<details><summary>Power Query (M) — partition {escape(p["name"] or "")}</summary><pre>{escape(p["expression"])}</pre></details>')
            h("</div>")
        shared = [e for e in m["expressions"] if not e.get("isParameter")]
        if shared:
            h("<h3>Shared queries</h3>")
            for e in shared:
                h(f'<h4>{escape(e["name"])}</h4><pre>{escape(e["expression"] or "")}</pre>')
    h("</section>")

    # health --------------------------------------------------------------------
    section("health", "Model health")
    for m in ctx.models:
        an = m["analysis"]
        if not an["findings"]:
            h('<p class="muted">No findings.</p>')
        for f in an["findings"]:
            h(f'<div class="find {f["severity"]}"><div class="sev">{escape(f["severity"])} · {escape(f["code"])}</div>'
              f'<div>{escape(f["message"])}</div>' + (f'<div class="small muted">{escape(f["advice"])}</div>' if f.get("advice") else ""))
            if f["objects"]:
                h("<ul>" + "".join(f"<li>{escape(str(o))}</li>" for o in f["objects"][:25]) + "</ul>")
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
               '<ul id="results"></ul><ul>']
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


def _tlink(t: str, ctx: Ctx) -> str:
    return f'<a href="#{ctx.anchor("t", t)}">{escape(t)}</a>'


def _link_field(f: dict, ctx: Ctx) -> str:
    label = escape(f.get("displayName") or f["name"])
    if f["kind"] == "measure" and f.get("exists", True):
        return f'<a href="#{ctx.anchor("m", f["name"])}">{label}</a>'
    txt = label
    if f.get("aggregation"):
        txt = f"{escape(f['aggregation'])} of {label} <span class=\"badge unused\" title=\"Implicit measure\">implicit</span>"
    if f.get("variation"):
        txt += f" ({escape(f['variation'])})"
    if not f.get("exists", True):
        txt += ' <span class="badge unused">missing in model</span>'
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
