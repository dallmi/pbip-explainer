"""Lookups over a knowledge pack - for agents answering questions and for humans on the command line."""
from __future__ import annotations

import difflib
import re
from typing import Dict, List, Optional, Tuple

from .analysis import PRIORITY_RULE, ROLE_LABELS, finding_impact_text, relationship_path


class Q:
    def __init__(self, pack: dict, enrichment: Optional[dict] = None):
        self.pack = pack
        self.enrichment = enrichment
        self._ctx = None
        self.model = pack["semanticModels"][0] if pack["semanticModels"] else None
        self.measures = {m["name"].lower(): m for mm in pack["semanticModels"] for t in mm["tables"] for m in t["measures"]}
        self.tables = {t["name"].lower(): t for mm in pack["semanticModels"] for t in mm["tables"]}
        self.pages = {p["displayName"].lower(): p for r in pack["reports"] for p in r["pages"]}
        self.deps = self.model["analysis"]["dependencies"] if self.model else {}

    # ------------------------------------------------------------------ resolution
    def _pick(self, d: dict, name: str, what: str):
        k = name.lower().strip().strip("[]'")
        if k in d:
            return d[k]
        cands = [v for kk, v in d.items() if k in kk]
        if len(cands) == 1:
            return cands[0]
        if cands:
            names = sorted(c.get("name") or c.get("displayName") for c in cands)[:15]
            raise SystemExit(f"Ambiguous {what} '{name}'. Candidates: {', '.join(names)}")
        raise SystemExit(f"No {what} named '{name}'. Try: find {name}")

    def _column(self, ref: str):
        m = re.match(r"^\s*'?(.+?)'?\s*\[(.+)\]\s*$", ref)
        if not m:
            raise SystemExit("Columns are written Table[Column]")
        t = self._pick(self.tables, m.group(1), "table")
        c = next((c for c in t["columns"] if c["name"].lower() == m.group(2).lower()), None)
        if not c:
            raise SystemExit(f"Table {t['name']} has no column {m.group(2)}")
        return t, c

    # ------------------------------------------------------------------ commands
    def summary(self) -> str:
        out = []
        for m in self.pack["semanticModels"]:
            st = m["analysis"]["stats"]
            out.append(f"Model {m['name']} ({m['format']}): " + ", ".join(f"{k}={v}" for k, v in st.items()))
            for t in m["tables"]:
                if not t["isAutoDateTable"]:
                    out.append(f"  {t['name']:<32} {ROLE_LABELS.get(t['role'], t['role']):<22} cols={len(t['columns'])} measures={len(t['measures'])}")
        for r in self.pack["reports"]:
            out.append(f"Report {r['name']} ({r['format']}): pages = " + ", ".join(p["displayName"] for p in r["pages"]))
        return "\n".join(out)

    def find(self, text: str) -> str:
        t = text.lower()
        out = []
        for m in self.measures.values():
            hit = t in m["name"].lower() or t in (m.get("description") or "").lower() or t in (m["expression"] or "").lower()
            if hit:
                out.append(f"measure  [{m['name']}]  ({m['table']})")
        for tb in self.tables.values():
            if tb["isAutoDateTable"]:
                continue
            if t in tb["name"].lower():
                out.append(f"table    {tb['name']}")
            for c in tb["columns"]:
                if t in c["name"].lower() or t in (c.get("description") or "").lower() or t in (c.get("sourceColumn") or "").lower():
                    out.append(f"column   {tb['name']}[{c['name']}]")
        for p in self.pages.values():
            if t in p["displayName"].lower():
                out.append(f"page     {p['displayName']}")
            for v in p["visuals"]:
                if t in (v.get("title") or "").lower() or t in (v.get("text") or "").lower():
                    out.append(f"visual   {v['typeLabel']} '{v.get('title') or v['name']}' on {p['displayName']}")
        return "\n".join(out) or "No match."

    def measure(self, name: str) -> str:
        m = self._pick(self.measures, name, "measure")
        out = [f"[{m['name']}]  (table {m['table']}{', folder ' + m['displayFolder'] if m.get('displayFolder') else ''}"
               f"{', hidden' if m['isHidden'] else ''})"]
        if m.get("description"):
            out.append(f"Description (model): {m['description']}")
        elif m.get("autoDescription"):
            out.append(f"Description (auto from DAX): {m['autoDescription']}")
        out.append(f"Format: {m.get('formatString') or '-'}   Complexity: {m['complexity']['level']}")
        out.append("DAX:")
        out.extend("    " + l for l in (m["expression"] or "").splitlines())
        out.append("Dependency tree:")
        out.extend(self._tree(f"measure:{m['name']}", 1, set()))
        if m["dependsOn"]["userelationship"]:
            out.append("Activates inactive relationships: " + "; ".join(
                f"{a[0]}[{a[1]}] - {b[0]}[{b[1]}]" for a, b in m["dependsOn"]["userelationship"]))
        out.append("Used by:")
        if not m["usedBy"]:
            out.append("    (nothing in the packaged report)")
        for u in m["usedBy"]:
            out.append(f"    {u['label']}" + (f"  [role {u['role']}]" if u.get("role") else ""))
        return "\n".join(out)

    def _tree(self, node: str, depth: int, seen: set) -> List[str]:
        lines = []
        for d in self.deps.get(node, []):
            if d.startswith(("visual:", "page:")):
                continue
            lines.append("    " * depth + d + (" (cycle)" if d in seen else ""))
            if d.startswith("measure:") and d not in seen and depth < 8:
                lines.extend(self._tree(d, depth + 1, seen | {node}))
        return lines

    def table(self, name: str) -> str:
        t = self._pick(self.tables, name, "table")
        out = [f"{t['name']}  role={ROLE_LABELS.get(t['role'], t['role'])}  storage={t.get('storageMode')}"
               f"{'  hidden' if t['isHidden'] else ''}"]
        if t.get("description"):
            out.append(f"Description: {t['description']}")
        s = t["source"]
        out.append(f"Source: {s.get('type')}")
        for c in s["connectors"]:
            out.append(f"    {c['function']} {c['arguments']}" + (f" via {c['via']}" if c.get("via") else ""))
        for n in s["navigation"]:
            out.append(f"    navigate {n}")
        for p in s["parameters"]:
            out.append(f"    parameter {p['name']} = {p['value']}")
        for j in s["joins"]:
            out.append(f"    PQ join {j['left']}{j['leftKeys']} = {j['right']}{j['rightKeys']} {j['kind']}")
        for f in s["rowFilters"]:
            out.append(f"    PQ row filter: {f['condition']}")
        out.append("Relationships:")
        for r in self.model["relationships"]:
            if t["name"] in (r["fromTable"], r["toTable"]):
                out.append(f"    {r['fromTable']}[{r['fromColumn']}] -> {r['toTable']}[{r['toColumn']}] {r['cardinality']} "
                           f"{r['crossFilteringBehavior']}{'' if r['isActive'] else ' INACTIVE'}")
        out.append("Columns:")
        for c in t["columns"]:
            out.append(f"    {c['name']:<30} {str(c.get('dataType')):<9} {c['columnType']:<11}"
                       f"{' hidden' if c['isHidden'] else ''}{' UNUSED' if not c['referenced'] else ''}"
                       + (f"  = {' '.join((c['expression'] or '').split())[:80]}" if c['columnType'] == 'calculated' else ""))
        if t["measures"]:
            out.append("Measures: " + ", ".join(m["name"] for m in t["measures"]))
        return "\n".join(out)

    def column(self, ref: str) -> str:
        t, c = self._column(ref)
        out = [f"{t['name']}[{c['name']}]  {c.get('dataType')}  {c['columnType']}{'  hidden' if c['isHidden'] else ''}",
               f"Source column: {c.get('sourceColumn')}   Format: {c.get('formatString')}   Sort by: {c.get('sortByColumn')}"
               f"   Summarize: {c.get('summarizeBy')}"]
        if c.get("description"):
            out.append(f"Description: {c['description']}")
        if c.get("expression"):
            out.append("DAX: " + c["expression"])
        out.append("Used by:")
        out.extend(f"    {u['label']}" for u in c["usedBy"]) if c["usedBy"] else out.append("    (nothing)")
        return "\n".join(out)

    def page(self, name: str) -> str:
        p = self._pick(self.pages, name, "page")
        out = [f"Page '{p['displayName']}' ({p['pageType']}{', hidden' if p['hidden'] else ''}) {p.get('width')}x{p.get('height')}"]
        for f in p["filters"]:
            out.append(f"Page filter: {f.get('condition') or f.get('field')}")
        for v in p["visuals"]:
            fields = [f for f in v["fields"] if f.get("role") not in ("formatting",)]
            if not fields and not v.get("text"):
                continue
            out.append(f"- {v['typeLabel']} '{v.get('title') or v['name']}'")
            if v.get("text"):
                out.append("    text: " + v["text"].replace("\n", " / ")[:200])
            for f in fields:
                nm = f"[{f['name']}]" if f["kind"] == "measure" else f"{f['table']}[{f['name']}]"
                out.append(f"    {f.get('role')}: {(f.get('aggregation') + ' of ') if f.get('aggregation') else ''}{nm}")
            for f in v["filters"]:
                if f.get("condition"):
                    out.append(f"    visual filter: {f['condition']}")
        lin = p.get("lineage") or {}
        out.append("Tables feeding this page: " + ", ".join(lin.get("tables", [])))
        out.append("Measures (incl. indirect): " + ", ".join(lin.get("measures", [])))
        return "\n".join(out)

    def path(self, a: str, b: str) -> str:
        res = relationship_path(self.model, a, b)
        inactive = False
        if res is None:
            res = relationship_path(self.model, a, b, include_inactive=True)
            inactive = True
        if res is None:
            return f"No relationship path between {a} and {b}."
        out = [f"Path {a} -> {b}" + (" (only via INACTIVE relationships)" if inactive else "") + ":"]
        for step in res:
            r = step["relationship"]
            out.append(f"  {step['from']} -> {step['to']}: join {r['fromTable']}[{r['fromColumn']}] = {r['toTable']}[{r['toColumn']}] "
                       f"({r['cardinality']}, filter {r['crossFilteringBehavior']}{'' if r['isActive'] else ', inactive'})")
        out.append("Filters flow from the one side to the many side unless the relationship is bi-directional.")
        return "\n".join(out)

    def impact(self, ref: str) -> str:
        """What (transitively) depends on an object: measures, visuals (direct / indirect with path), filters."""
        from .docs import impact_lines, impact_summary
        r = ref.strip()
        if "[" in r and not r.startswith("["):
            t, c = self._column(r)
            items = [(f"{t['name']}[{c['name']}]", c.get("impact"))]
        elif r.lower().strip("[]") in self.measures:
            m = self.measures[r.lower().strip("[]")]
            items = [(f"[{m['name']}]", m.get("impact"))]
        else:
            t = self._pick(self.tables, r, "table")
            items = [(f"{t['name']}[{c['name']}]", c.get("impact")) for c in t["columns"]]
            items = [i for i in items if i[1] and i[1]["counts"]["visuals"] + i[1]["counts"]["measures"] + i[1]["counts"]["other"]]
            if not items:
                return f"Nothing in the package depends on table {t['name']}."
        out = []
        for name, imp in items:
            if not imp:
                out.append(f"{name}: no impact data (re-run the extraction).")
                continue
            out.append(impact_summary(imp, name))
            if imp["measures"]:
                out.append("  Dependent measures: " + ", ".join(f"[{x}]" for x in imp["measures"]))
            if imp["columns"]:
                out.append("  Dependent columns (calculated, or sorted by it): " + ", ".join(imp["columns"]))
            if imp["pages"]:
                out.append("  Pages: " + ", ".join(imp["pages"]))
            out.extend("  " + line for line in impact_lines(imp, 60 if len(items) == 1 else 8))
            out.append("")
        return "\n".join(out).rstrip()

    def unused(self) -> str:
        u = self.model["analysis"]["unused"]
        return (f"{u['note']}\nUnused measures ({len(u['measures'])}): {', '.join(u['measures'])}\n"
                f"Unreferenced columns ({len(u['columns'])}): {', '.join(u['columns'])}")

    def findings(self, priority: str = "") -> str:
        priority = priority.strip().lower()
        if priority and priority not in ("high", "medium", "low"):
            return "Use 'findings', or 'findings high|medium|low' to filter by priority."
        fs = [f for f in self.model["analysis"]["findings"] if not priority or f.get("priority") == priority]
        out = [f"Priority: {PRIORITY_RULE}."] if fs and "priority" in fs[0] else []
        for f in fs:
            tag = f"{f['priority']} | {f['severity']}" if f.get("priority") else f["severity"]
            out.append(f"[{tag}] {f['code']}: {f['message']}")
            out.extend(f"    - {o}" for o in f["objects"][:20])
            imp = finding_impact_text(f)
            if imp:
                out.append(f"    impact: {imp}")
            if (f.get("impact") or {}).get("visuals"):
                more = f["impact"]["visuals"] - len(f["impact"]["examples"])
                out.append(f"    e.g. {'; '.join(f['impact']['examples'])}" + (f" and {more} more" if more > 0 else ""))
                out.append(f"    basis: {f['impact']['basis']}")
            if f.get("advice"):
                out.append(f"    advice: {f['advice']}")
        if not fs:
            return f"No {priority}-priority findings." if priority else "No findings."
        return "\n".join(out)

    # ------------------------------------------------------------------ structured answers (desktop app)
    # An answer is {kind, title, meta, badges, blocks, text}. Blocks: text | code | list | tree, each with a label.
    # A ref {k, n, q} names an object; q is what to ask to open it.

    @property
    def ctx(self):
        if self._ctx is None:
            from .docs import Ctx
            self._ctx = Ctx(self.pack, self.enrichment)
        return self._ctx

    def _page_list(self) -> List[Tuple[str, dict]]:
        return [(r["name"], p) for r in self.pack["reports"] for p in r["pages"]]

    def _ref(self, kind: str, name: str, report: Optional[str] = None) -> dict:
        ref = {"k": kind, "n": name}
        if kind in ("measure", "column"):
            ref["q"] = name
        elif kind == "table":
            ref["q"] = f"{name} (table)"
        elif kind == "page":
            same = [r for r, p in self._page_list() if p["displayName"] == name]
            ref["q"] = f"{name} (page · {report})" if len(same) > 1 and report else f"{name} (page)"
            if len(same) > 1 and report:
                ref["n"] = f"{name} · {report}"
        return ref

    def _node_item(self, node: str) -> dict:
        kind, _, name = node.partition(":")
        if kind in ("measure", "column", "table"):
            return {"ref": self._ref(kind, name)}
        if kind == "reportMeasure":
            return {"text": name, "sub": "measure defined in the report"}
        return {"text": name, "sub": kind}

    def _use_item(self, u: dict) -> dict:
        kind, ref = u["kind"], u["by"].split(":", 1)[-1]
        if kind == "measure":
            return {"ref": self._ref("measure", ref), "sub": "measure"}
        if kind in ("calculatedColumn", "sortBy"):
            return {"ref": self._ref("column", ref), "sub": "calculated column" if kind == "calculatedColumn" else "is sorted by it"}
        if kind in ("visual", "visualFilter") and u.get("page"):
            what = f"{u.get('visualType') or 'Visual'}: {u.get('visualTitle') or u.get('visual')}"
            return {"text": what + (" (as a filter)" if kind == "visualFilter" else ""), "ref": self._ref("page", u["page"], u.get("report")),
                    "sub": "on page"}
        if u.get("page"):
            return {"text": u["label"], "ref": self._ref("page", u["page"], u.get("report")), "sub": "on page"}
        return {"text": u["label"]}

    def _dep_items(self, node: str, seen: frozenset, depth: int = 0) -> List[dict]:
        items = []
        for d in self.deps.get(node, []):
            if d.startswith(("visual:", "page:")):
                continue
            it = self._node_item(d)
            if d.startswith("measure:") and d not in seen and depth < 8:
                kids = self._dep_items(d, seen | {node}, depth + 1)
                if kids:
                    it["children"] = kids
            items.append(it)
        return items

    def _impact_blocks(self, imp: Optional[dict], name: str) -> List[dict]:
        from .docs import impact_summary
        if not imp:
            return []
        blocks = [{"t": "text", "label": "If it changes", "text": impact_summary(imp, name)}]
        deps = [{"ref": self._ref("measure", x)} for x in imp["measures"]] + \
               [{"text": x, "sub": "measure defined in the report"} for x in imp.get("reportMeasures") or []]
        if deps:
            blocks.append({"t": "list", "label": f"Measures that change with it ({len(deps)})", "items": deps})
        vis = []
        for e in imp["visuals"]["direct"]:
            vis.append({"text": f"{e.get('type') or 'Visual'}: {e.get('title') or e.get('visual')}", "tag": "direct",
                        "ref": self._ref("page", e["page"]), "sub": "on page"})
        for e in imp["visuals"]["indirect"]:
            via = " → ".join(e.get("path", [])[1:-1])
            vis.append({"text": f"{e.get('type') or 'Visual'}: {e.get('title') or e.get('visual')}", "tag": "indirect",
                        "ref": self._ref("page", e["page"]), "sub": (f"via {via} · " if via else "") + "on page"})
        if vis:
            blocks.append({"t": "list", "label": f"Visuals that change ({len(vis)})", "items": vis})
        rest = [{"text": f["where"], "tag": "filter"} for f in imp["filters"]] + [{"text": o} for o in imp["other"]]
        if rest:
            blocks.append({"t": "list", "label": "Also affected", "items": rest})
        return blocks

    def _done(self, a: dict) -> dict:
        """Add the plain-text form (Copy button) and drop empty blocks."""
        a["blocks"] = [b for b in a["blocks"] if b.get("text") or b.get("items") or b.get("empty")]
        lines = [f"{a['title']} ({a['kind']})"] + ([a["meta"]] if a.get("meta") else [])

        def item_text(it: dict) -> str:
            ref = it.get("ref")
            txt = it.get("text") or (ref["n"] if ref else "")
            if it.get("text") and ref:
                txt += f" · {it.get('sub') or ''} {ref['n']}".replace("  ", " ")
            elif it.get("sub"):
                txt += f" ({it['sub']})"
            return (f"[{it['tag']}] " if it.get("tag") else "") + txt

        def walk(items: List[dict], depth: int):
            for it in items:
                lines.append("    " * depth + "- " + item_text(it))
                walk(it.get("children") or [], depth + 1)
        for b in a["blocks"]:
            lines.append("")
            lines.append(b["label"] + ":")
            if b.get("text"):
                lines.extend("    " + l for l in b["text"].splitlines())
            if b.get("items"):
                walk(b["items"], 1)
            elif b.get("empty"):
                lines.append("    " + b["empty"])
        a["text"] = "\n".join(lines)
        return a

    # -- one answer per kind of object
    def _a_measure(self, m: dict) -> dict:
        from .docs import PROV_LABEL
        d, prov = self.ctx.measure_desc(m)
        meta = [f"in table {m['table']}"]
        if m.get("displayFolder"):
            meta.append(f"folder {m['displayFolder']}")
        if m.get("formatString"):
            meta.append(f"format {m['formatString']}")
        badges = (["hidden"] if m["isHidden"] else []) + ([] if m["usedInReport"] else ["not used in report"])
        blocks = [{"t": "text", "label": "What it is", "text": d or "", "badge": PROV_LABEL.get(prov or "", "")},
                  {"t": "code", "label": "Formula (DAX)", "text": m["expression"] or ""},
                  {"t": "tree", "label": "Built from", "items": self._dep_items(f"measure:{m['name']}", frozenset())}]
        if m["dependsOn"]["userelationship"]:
            blocks.append({"t": "text", "label": "Activates an inactive relationship", "text": "; ".join(
                f"{a[0]}[{a[1]}] ↔ {b[0]}[{b[1]}]" for a, b in m["dependsOn"]["userelationship"])})
        seen, shown = set(), []
        for u in m["usedBy"]:
            if u["kind"] == "visual" and u["by"] not in seen:
                seen.add(u["by"])
                shown.append(self._use_item(u))
        blocks.append({"t": "list", "label": "Shown in", "items": shown,
                       "empty": "Not shown directly in a visual of the packaged report."})
        blocks += self._impact_blocks(m.get("impact"), f"[{m['name']}]")
        return self._done({"kind": "Measure", "title": m["name"], "meta": " · ".join(meta), "badges": badges,
                           "ref": {"k": "measure", "n": m["name"]}, "blocks": blocks})

    def _a_column(self, t: dict, c: dict) -> dict:
        from .docs import PROV_LABEL
        name = f"{t['name']}[{c['name']}]"
        d, prov = self.ctx.column_desc(t, c)
        meta = [f"column of {t['name']}", str(c.get("dataType") or "")]
        if c.get("formatString"):
            meta.append(f"format {c['formatString']}")
        if c.get("sortByColumn"):
            meta.append(f"sorted by {c['sortByColumn']}")
        badges = (["key"] if c.get("isKey") else []) + (["hidden"] if c["isHidden"] else []) + \
                 (["calculated"] if c["columnType"] == "calculated" else []) + \
                 ([] if c["referenced"] or c["usedInReport"] else ["unused"])
        blocks = [{"t": "text", "label": "What it is", "text": d or "", "badge": PROV_LABEL.get(prov or "", "")}]
        if c.get("expression"):
            blocks.append({"t": "code", "label": "Formula (DAX)", "text": c["expression"]})
        blocks.append({"t": "list", "label": "Used by", "items": [self._use_item(u) for u in c["usedBy"]],
                       "empty": "Nothing in the package uses this column."})
        blocks += self._impact_blocks(c.get("impact"), name)
        blocks.append({"t": "list", "label": "Table", "items": [{"ref": self._ref("table", t["name"])}]})
        return self._done({"kind": "Column", "title": name, "meta": " · ".join(x for x in meta if x), "badges": badges,
                           "ref": {"k": "column", "n": name}, "blocks": blocks})

    def _a_table(self, t: dict) -> dict:
        from .docs import PROV_LABEL, _src_summary
        d, prov = self.ctx.table_desc(t)
        joins = []
        for r in (self.model or {}).get("relationships", []):
            if t["name"] in (r["fromTable"], r["toTable"]):
                other = r["toTable"] if r["fromTable"] == t["name"] else r["fromTable"]
                joins.append({"text": f"{r['fromTable']}[{r['fromColumn']}] = {r['toTable']}[{r['toColumn']}]",
                              "ref": self._ref("table", other), "sub": r["cardinality"] + ("" if r["isActive"] else ", inactive")
                              + (", both directions" if r["crossFilteringBehavior"] == "bothDirections" else "") + " · joins"})
        cols = []
        for c in t["columns"]:
            flags = [str(c.get("dataType") or "")] + (["key"] if c.get("isKey") else []) + (["hidden"] if c["isHidden"] else []) \
                    + (["calculated"] if c["columnType"] == "calculated" else [])
            cols.append({"ref": self._ref("column", f"{t['name']}[{c['name']}]"), "sub": " · ".join(x for x in flags if x)})
        readers = [me["name"] for tt in self.tables.values() for me in tt["measures"]
                   if tt["name"] != t["name"] and t["name"] in (me.get("lineage") or {}).get("tables", [])]
        pages = [(r, p["displayName"]) for r, p in self._page_list() if t["name"] in (p.get("lineage") or {}).get("tables", [])]
        blocks = [{"t": "text", "label": "What it is", "text": d or "", "badge": PROV_LABEL.get(prov or "", "")},
                  {"t": "text", "label": "Comes from", "text": _src_summary(t) if t["source"].get("type") else ""},
                  {"t": "list", "label": "Joined to", "items": joins, "empty": "No relationship to another table."},
                  {"t": "list", "label": f"Measures in this table ({len(t['measures'])})",
                   "items": [{"ref": self._ref("measure", me["name"])} for me in t["measures"]]},
                  {"t": "list", "label": f"Measures that read this table ({len(readers)})",
                   "items": [{"ref": self._ref("measure", x)} for x in readers]},
                  {"t": "list", "label": f"Columns ({len(cols)})", "items": cols},
                  {"t": "list", "label": "Feeds report pages", "items": [{"ref": self._ref("page", n, r)} for r, n in pages],
                   "empty": "No page of the packaged report reads this table."}]
        return self._done({"kind": "Table", "title": t["name"], "meta": f"{ROLE_LABELS.get(t['role'], t['role'])} · storage {t.get('storageMode') or '—'}",
                           "badges": ["hidden"] if t["isHidden"] else [], "ref": {"k": "table", "n": t["name"]}, "blocks": blocks})

    def _a_page(self, report: str, p: dict) -> dict:
        visuals = []
        for v in p["visuals"]:
            fields = [f for f in v["fields"] if f.get("role") not in ("formatting", "sort")]
            if not fields:
                continue
            kids = []
            for f in fields:
                if f["kind"] == "measure" and f.get("status", "verified") == "verified":
                    kids.append({"ref": self._ref("measure", f["name"]), "sub": f.get("role") or ""})
                elif f.get("status", "verified") == "verified" and f.get("table"):
                    kids.append({"ref": self._ref("column", f"{f['table']}[{f['name']}]"),
                                 "sub": " · ".join(x for x in (f.get("aggregation") and f"{f['aggregation']} in the visual", f.get("role")) if x)})
                else:
                    kids.append({"text": f["name"], "sub": {"unresolved": "not found in the model", "reportMeasure": "measure defined in the report",
                                                           "observedExternal": "from a model outside the package"}.get(f.get("status"), "")})
            visuals.append({"text": f"{v['typeLabel']}: {v.get('title') or v['name']}", "children": kids})
        lin = p.get("lineage") or {}
        meta = [f"report {report}", p["pageType"] + " page"] + ([f"{len(visuals)} visuals with data"])
        blocks = [{"t": "text", "label": "Page filters", "text": "; ".join(str(f.get("condition") or f.get("field")) for f in p["filters"])},
                  {"t": "tree", "label": "Visuals and what they show", "items": visuals, "empty": "No visual with data on this page."},
                  {"t": "list", "label": "Reads these tables", "items": [{"ref": self._ref("table", x)} for x in lin.get("tables", [])
                                                                          if x.lower() in self.tables]}]
        return self._done({"kind": "Page", "title": p["displayName"], "meta": " · ".join(meta), "badges": ["hidden"] if p["hidden"] else [],
                           "ref": {"k": "page", "n": p["displayName"], "report": report}, "blocks": blocks})

    # -- resolution: what did the user mean?
    def _exact(self, name: str, hint: Optional[str] = None, report: Optional[str] = None) -> List[tuple]:
        k = name.strip().lower()
        bare = k.strip("[]'")
        out: List[tuple] = []
        if hint in (None, "measure") and bare in self.measures:
            out.append(("measure", self.measures[bare]))
        m = re.match(r"^\s*'?(.+?)'?\s*\[(.+)\]\s*$", name)
        if hint in (None, "column") and m and m.group(1).lower() in self.tables:
            t = self.tables[m.group(1).lower()]
            out += [("column", (t, c)) for c in t["columns"] if c["name"].lower() == m.group(2).lower()]
        tabs = [t for t in self.tables.values() if not t["isAutoDateTable"]]
        if hint in (None, "table"):
            out += [("table", t) for t in tabs if t["name"].lower() == bare]
        if hint is None:
            out += [("column", (t, c)) for t in tabs for c in t["columns"] if c["name"].lower() == bare]
        if hint in (None, "page"):
            out += [("page", (r, p)) for r, p in self._page_list() if p["displayName"].lower() == bare and report in (None, r)]
        return out

    def _open(self, kind: str, obj) -> dict:
        if kind == "measure":
            return self._a_measure(obj)
        if kind == "column":
            return self._a_column(*obj)
        if kind == "table":
            return self._a_table(obj)
        return self._a_page(*obj)

    def _hit(self, kind: str, obj) -> dict:
        if kind == "measure":
            return {"ref": self._ref("measure", obj["name"]), "sub": "measure"}
        if kind == "column":
            return {"ref": self._ref("column", f"{obj[0]['name']}[{obj[1]['name']}]"), "sub": "column"}
        if kind == "table":
            return {"ref": self._ref("table", obj["name"]), "sub": "table"}
        return {"ref": self._ref("page", obj[1]["displayName"], obj[0]), "sub": "report page"}

    def _search(self, text: str, lead: Optional[str] = None, exact: Optional[List[tuple]] = None) -> dict:
        t = text.strip().lower()
        blocks: List[dict] = []
        if not t:
            return self._done({"kind": "Search", "title": "What would you like to know?", "meta": "", "badges": [], "blocks": [
                {"t": "text", "label": "How to ask", "text": "Type the name of a measure, a table, a column (Table[Column]) or a report page, "
                                                             "or pick one of the suggestions."}]})
        if not self.model:
            blocks.append({"t": "text", "label": "Note", "text": "The semantic model is not in the package (live connection), "
                                                                 "so only report pages can be looked up."})
        tabs = [tb for tb in self.tables.values() if not tb["isAutoDateTable"]]
        everything = ([("measure", m) for m in self.measures.values()] + [("table", tb) for tb in tabs]
                      + [("column", (tb, c)) for tb in tabs for c in tb["columns"]] + [("page", rp) for rp in self._page_list()])

        def label(kind, obj) -> str:
            return (obj["name"] if kind in ("measure", "table") else obj[1]["name"] if kind == "column" else obj[1]["displayName"]).lower()
        named = exact or [(k, o) for k, o in everything if t in label(k, o)]
        if not named:                                                 # typo: closest names
            close = set(difflib.get_close_matches(t, sorted({label(k, o) for k, o in everything}), n=8, cutoff=0.6))
            named = [(k, o) for k, o in everything if label(k, o) in close]
        blocks.append({"t": "list", "label": "Did you mean" if not exact else "Pick one",
                       "items": [self._hit(k, o) for k, o in named[:40]]})
        if not exact:
            inside = [{"ref": self._ref("measure", m["name"]), "sub": "mentions it in the formula or description"}
                      for m in self.measures.values() if t not in m["name"].lower()
                      and (t in (m["expression"] or "").lower() or t in (m.get("description") or "").lower())]
            blocks.append({"t": "list", "label": "Mentioned in", "items": inside[:40]})
        if not any(b.get("items") for b in blocks):
            blocks.append({"t": "text", "label": "Nothing found", "text": "No measure, table, column or page has a name like this, and no "
                                                                         "formula mentions it. Check the spelling or pick one of the suggestions."})
        return self._done({"kind": "Search", "title": lead or f"Results for “{text.strip()}”", "meta": "", "badges": [], "blocks": blocks})

    def answer(self, text: str) -> dict:
        """Everything about one object, found by name alone (no question type). Unknown or ambiguous names give choices."""
        raw = (text or "").strip()
        m = re.match(r"^(.*\S)\s+\((table|page)(?:\s·\s(.+))?\)$", raw)
        name, hint, report = (m.group(1), m.group(2), m.group(3)) if m else (raw, None, None)
        if not name:
            return self._search("")
        found = self._exact(name, hint, report)
        if len(found) == 1:
            return self._open(*found[0])
        if len(found) > 1:
            return self._search(name, f"Several things are called “{name}”", found)
        return self._search(name)

    def answer_path(self, a: str, b: str) -> dict:
        ta, tb = self._exact(a, "table"), self._exact(b, "table")
        if not ta or not tb:
            return self._search(a if not ta else b, f"There is no table called “{(a if not ta else b).strip()}”")
        a, b = ta[0][1]["name"], tb[0][1]["name"]
        res = relationship_path(self.model, a, b)
        inactive = res is None
        if res is None:
            res = relationship_path(self.model, a, b, include_inactive=True)
        if res is None:
            return self._done({"kind": "Join", "title": f"{a} and {b} are not joined", "meta": "", "badges": [], "blocks": [
                {"t": "text", "label": "Result", "text": "There is no chain of relationships between these two tables, so a filter on one "
                                                         "does not reach the other."}]})
        steps = []
        for st in res:
            r = st["relationship"]
            steps.append({"text": f"{r['fromTable']}[{r['fromColumn']}] = {r['toTable']}[{r['toColumn']}]", "ref": self._ref("table", st["to"]),
                          "sub": r["cardinality"] + ("" if r["isActive"] else ", inactive")
                          + (", both directions" if r["crossFilteringBehavior"] == "bothDirections" else ", single direction") + " · to"})
        note = "Filters flow from the one side to the many side unless a relationship filters in both directions."
        if inactive:
            note = "This path only works through an inactive relationship: a measure has to switch it on with USERELATIONSHIP. " + note
        return self._done({"kind": "Join", "title": f"{a} → {b}", "meta": f"{len(steps)} step{'s' if len(steps) != 1 else ''}", "badges": [],
                           "blocks": [{"t": "list", "label": "Join path", "items": steps}, {"t": "text", "label": "How filters travel", "text": note}]})

    def answer_unused(self) -> dict:
        u = self.model["analysis"]["unused"]
        return self._done({"kind": "Unused", "title": "Not used in the packaged report", "meta": "", "badges": [], "blocks": [
            {"t": "text", "label": "Read this first", "text": u["note"]},
            {"t": "list", "label": f"Measures ({len(u['measures'])})", "items": [{"ref": self._ref("measure", x)} for x in u["measures"]],
             "empty": "Every measure is used."},
            {"t": "list", "label": f"Columns not referenced anywhere ({len(u['columns'])})",
             "items": [{"ref": self._ref("column", x)} for x in u["columns"]], "empty": "Every column is referenced."}]})
