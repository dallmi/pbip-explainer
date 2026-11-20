"""Lookups over a knowledge pack - for agents answering questions and for humans on the command line."""
from __future__ import annotations

import re
from typing import List, Optional

from .analysis import ROLE_LABELS, relationship_path


class Q:
    def __init__(self, pack: dict):
        self.pack = pack
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
        """Everything that (transitively) depends on an object - what breaks if it changes."""
        if "[" in ref and not ref.strip().startswith("["):
            t, c = self._column(ref)
            start = f"column:{t['name']}[{c['name']}]"
        elif ref.lower().strip("[]") in self.measures:
            start = f"measure:{self.measures[ref.lower().strip('[]')]['name']}"
        else:
            t = self._pick(self.tables, ref, "table")
            start = f"table:{t['name']}"
        rev = {}
        for src, dsts in self.deps.items():
            for d in dsts:
                rev.setdefault(d, set()).add(src)
        if start.startswith("table:"):
            tn = start[6:]
            seeds = {k for k in rev if k.startswith(f"column:{tn}[")} | {start}
        else:
            seeds = {start}
        seen, todo = set(), list(seeds)
        while todo:
            x = todo.pop()
            for y in rev.get(x, ()):
                if y not in seen:
                    seen.add(y)
                    todo.append(y)
        groups = {}
        for x in sorted(seen):
            groups.setdefault(x.split(":")[0], []).append(x.split(":", 1)[1])
        out = [f"Impact of changing {start}:"]
        for k in ("measure", "column", "calcItem", "relationship", "role", "visual", "page", "report", "table", "hierarchy"):
            if k in groups:
                out.append(f"  {k}s ({len(groups[k])}): " + ", ".join(groups[k]))
        return "\n".join(out) if len(out) > 1 else f"Nothing in the package depends on {start}."

    def unused(self) -> str:
        u = self.model["analysis"]["unused"]
        return (f"{u['note']}\nUnused measures ({len(u['measures'])}): {', '.join(u['measures'])}\n"
                f"Unreferenced columns ({len(u['columns'])}): {', '.join(u['columns'])}")

    def findings(self) -> str:
        out = []
        for f in self.model["analysis"]["findings"]:
            out.append(f"[{f['severity']}] {f['code']}: {f['message']}")
            out.extend(f"    - {o}" for o in f["objects"][:20])
            if f.get("advice"):
                out.append(f"    advice: {f['advice']}")
        return "\n".join(out) or "No findings."
