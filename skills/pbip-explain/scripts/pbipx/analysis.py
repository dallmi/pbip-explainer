"""Cross-object analysis: dependencies, usage, lineage, table roles and model health.

Object ids used throughout the knowledge pack:
    table:<T>   column:<T>[<C>]   measure:<M>   calcItem:<T>[<Item>]   role:<R>
    expression:<Q>   page:<P>   visual:<P>/<V>   relationship:<id>
"""
from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Dict, Iterable, List, Optional, Set, Tuple

from . import dax, mquery


def cid(t: str, c: str) -> str:
    return f"column:{t}[{c}]"


def mid(m: str) -> str:
    return f"measure:{m}"


def tid(t: str) -> str:
    return f"table:{t}"


AUTO_DATE_PREFIXES = ("LocalDateTable_", "DateTableTemplate_")
KEYISH = re.compile(r"(?:^|[\s_])(?:[Ii][Dd]|[Kk]ey|[Yy]ear|[Cc]ode|[Nn]umber|[Nn]o|[Ss]ort|YM|ym|[Oo]rdinal)$|[a-z](?:ID|Id|Key|Code|Year)$")


def analyse(model: dict, reports: List[dict]) -> dict:
    cat = dax.Catalog(model["tables"])
    tables = {t["name"]: t for t in model["tables"]}
    measures = {m["name"]: m for t in model["tables"] for m in t["measures"]}
    deps: Dict[str, Set[str]] = defaultdict(set)        # object -> objects it depends on
    usage: Dict[str, List[dict]] = defaultdict(list)     # object -> who uses it
    unresolved: List[dict] = []
    expr_names = [e["name"] for e in model["expressions"]]

    def link(src: str, dst: str, kind: str, label: Optional[str] = None):
        deps[src].add(dst)
        usage[dst].append({"by": src, "kind": kind, "label": label or src})

    def refs_to_ids(refs: dict) -> List[str]:
        ids = [mid(m) for m in refs["measures"]] + [cid(t, c) for t, c in refs["columns"]]
        ids += [tid(t) for t in refs["tables"]]
        return ids

    # ---------------------------------------------------------------- DAX objects
    for t in model["tables"]:
        tn = t["name"]
        t["isAutoDateTable"] = tn.startswith(AUTO_DATE_PREFIXES)
        for m in t["measures"]:
            refs = dax.analyse(m["expression"], cat, tn)
            if m.get("formatStringExpression"):
                fr = dax.analyse(m["formatStringExpression"], cat, tn)
                for k in ("measures", "columns", "tables"):
                    for x in fr[k]:
                        if x not in refs[k]:
                            refs[k].append(x)
            m["dependsOn"] = refs
            m["complexity"] = dax.complexity(m["expression"], refs)
            m["autoDescription"] = dax.auto_describe(m["expression"])
            for x in refs_to_ids(refs):
                link(mid(m["name"]), x, "measure", f"measure [{m['name']}]")
            for u in refs["unresolved"]:
                unresolved.append({"object": mid(m["name"]), "reference": u})
        for c in t["columns"]:
            if c["columnType"] == "calculated":
                refs = dax.analyse(c["expression"], cat, tn)
                c["dependsOn"] = refs
                for x in refs_to_ids(refs):
                    if x != cid(tn, c["name"]):
                        link(cid(tn, c["name"]), x, "calculatedColumn", f"calculated column {tn}[{c['name']}]")
                for u in refs["unresolved"]:
                    unresolved.append({"object": cid(tn, c["name"]), "reference": u})
            if c.get("sortByColumn"):
                link(cid(tn, c["name"]), cid(tn, c["sortByColumn"]), "sortBy", f"sort order of {tn}[{c['name']}]")
        for h in t["hierarchies"]:
            for lv in h["levels"]:
                if lv.get("column"):
                    link(f"hierarchy:{tn}[{h['name']}]", cid(tn, lv["column"]), "hierarchy", f"hierarchy {tn}[{h['name']}]")
        cg = t.get("calculationGroup")
        if cg:
            for it in cg["items"]:
                refs = dax.analyse(it["expression"], cat, tn)
                it["dependsOn"] = refs
                for x in refs_to_ids(refs):
                    link(f"calcItem:{tn}[{it['name']}]", x, "calculationItem", f"calculation item {tn}[{it['name']}]")
        for p in t["partitions"]:
            st = (p.get("sourceType") or "").lower()
            if st == "calculated":
                refs = dax.analyse(p["expression"], cat, None)
                p["dependsOn"] = refs
                for x in refs_to_ids(refs):
                    if x != tid(tn):
                        link(tid(tn), x, "calculatedTable", f"calculated table {tn}")
            elif st in ("m", "") and p.get("expression"):
                p["mAnalysis"] = mquery.analyse(p["expression"], expr_names)
                for q in p["mAnalysis"]["references"]:
                    link(tid(tn), f"expression:{q}", "powerQuery", f"Power Query of {tn}")
        t["fieldParameter"] = _field_parameter(t)

    for e in model["expressions"]:
        e["isParameter"] = mquery.is_parameter(e["expression"])
        if e["isParameter"]:
            e["parameter"] = mquery.parameter_info(e["expression"] or "")
        else:
            e["mAnalysis"] = mquery.analyse(e["expression"], [n for n in expr_names if n != e["name"]])
            for q in e["mAnalysis"]["references"]:
                link(f"expression:{e['name']}", f"expression:{q}", "powerQuery", f"query {e['name']}")

    for r in model["roles"]:
        for tp in r["tablePermissions"]:
            refs = dax.analyse(tp["filterExpression"], cat, tp["table"])
            tp["dependsOn"] = refs
            link(f"role:{r['name']}", tid(tp["table"]), "rls", f"RLS role {r['name']}")
            for x in refs_to_ids(refs):
                link(f"role:{r['name']}", x, "rls", f"RLS role {r['name']}")

    # ---------------------------------------------------------------- relationships
    col_types = {(t["name"], c["name"]): c.get("dataType") for t in model["tables"] for c in t["columns"]}
    userel: Set[frozenset] = set()
    for m in measures.values():
        for a, b in m["dependsOn"]["userelationship"]:
            userel.add(frozenset([tuple(a), tuple(b)]))
    for r in model["relationships"]:
        rid = f"relationship:{r['name']}"
        r["id"] = rid
        r["fromDataType"] = col_types.get((r["fromTable"], r["fromColumn"]))
        r["toDataType"] = col_types.get((r["toTable"], r["toColumn"]))
        r["activatedByUserelationship"] = frozenset([(r["fromTable"], r["fromColumn"]), (r["toTable"], r["toColumn"])]) in userel
        r["cardinality"] = _card(r)
        label = f"relationship {r['fromTable']}[{r['fromColumn']}] → {r['toTable']}[{r['toColumn']}]"
        link(rid, cid(r["fromTable"], r["fromColumn"]), "relationship", label)
        link(rid, cid(r["toTable"], r["toColumn"]), "relationship", label)

    # ---------------------------------------------------------------- report usage
    known_cols = {cid(t["name"], c["name"]) for t in model["tables"] for c in t["columns"]}
    hier_levels = {(t["name"], h["name"], lv["name"]): lv.get("column")
                   for t in model["tables"] for h in t["hierarchies"] for lv in h["levels"]}
    broken: List[dict] = []
    report_roots: Set[str] = set()

    report_measures: Dict[str, dict] = {}
    for rep_ in reports:
        for rm in rep_.get("reportMeasures", []):
            report_measures[rm["name"].lower()] = rm
    for rm in report_measures.values():
        refs = dax.analyse(rm.get("expression"), cat, None)
        # report measures may use each other: those show up as unresolved [Name]
        rest = []
        for u in refs["unresolved"]:
            other = report_measures.get(u.strip("[]").lower()) if u.startswith("[") else None
            if other:
                link(f"reportMeasure:{rm['name']}", f"reportMeasure:{other['name']}", "reportMeasure",
                     f"report measure [{rm['name']}]")
            else:
                rest.append(u)
        refs["unresolved"] = rest
        rm["dependsOn"] = refs
        for x in refs_to_ids(refs):
            link(f"reportMeasure:{rm['name']}", x, "reportMeasure", f"report measure [{rm['name']}]")

    def field_id(f: dict) -> Optional[str]:
        fid = _model_field_id(f)
        if fid:
            return fid
        rm = report_measures.get((f.get("name") or "").lower())
        return f"reportMeasure:{rm['name']}" if rm and f["kind"] != "column" else None

    def _model_field_id(f: dict) -> Optional[str]:
        tname, name = f.get("table"), f.get("name")
        if f["kind"] == "measure":
            m = cat.measure(name or "")
            if m:
                return mid(m[1])
            c = cat.column(tname or "", name or "")
            return cid(*c) if c else None
        if f["kind"] == "hierarchyLevel":
            col = hier_levels.get((tname, f.get("hierarchy"), name))
            c = cat.column(tname or "", col or name or "")
            return cid(*c) if c else None
        c = cat.column(tname or "", name or "")
        if c:
            return cid(*c)
        m = cat.measure(name or "")
        return mid(m[1]) if m else None

    def use_fields(fields: Iterable[dict], src: str, kind: str, label: str, where: dict):
        for f in fields:
            if not f:
                continue
            fid = field_id(f)
            f["id"] = fid
            f["exists"] = fid is not None
            f["status"] = ("reportMeasure" if fid.startswith("reportMeasure:") else "verified") if fid else "unresolved"
            if fid:
                usage[fid].append({"by": src, "kind": kind, "label": label, "role": f.get("role"), **where})
                deps[src].add(fid)
                report_roots.add(fid)
                if f.get("implicitMeasure"):
                    f["implicit"] = True
            else:
                broken.append({"where": label, "field": f"{f.get('table')}[{f.get('name')}]", "kind": f["kind"],
                               "status": "unresolved", **where})

    vinfo: Dict[str, dict] = {}
    for rep in reports:
        rname = rep["name"]
        use_fields([fl["field"] for fl in rep["filters"] if fl.get("field")], f"report:{rname}", "reportFilter",
                   f"report-level filter ({rname})", {"report": rname})
        for p in rep["pages"]:
            pid = f"page:{p['displayName']}"
            use_fields([fl["field"] for fl in p["filters"] if fl.get("field")], pid, "pageFilter",
                       f"page filter on '{p['displayName']}'", {"report": rname, "page": p["displayName"]})
            use_fields(p.get("drillthroughFields", []), pid, "drillthrough",
                       f"drillthrough field of '{p['displayName']}'", {"report": rname, "page": p["displayName"]})
            for v in p["visuals"]:
                vid = f"visual:{p['displayName']}/{v['name']}"
                lbl = f"{v['typeLabel']} '{v['title'] or v['name']}' on '{p['displayName']}'"
                where = {"report": rname, "page": p["displayName"], "visual": v["name"], "visualTitle": v["title"],
                         "visualType": v["typeLabel"]}
                vinfo[vid] = dict(where)
                use_fields(v["fields"], vid, "visual", lbl, where)
                use_fields([fl["field"] for fl in v["filters"] if fl.get("field")], vid, "visualFilter",
                           f"visual filter of {lbl}", where)
                deps[pid].add(vid)

    for r in model["roles"]:
        report_roots.add(f"role:{r['name']}")
    for r in model["relationships"]:
        report_roots.add(r["id"])

    # ---------------------------------------------------------------- closure / unused
    def closure(start: Iterable[str]) -> Set[str]:
        seen: Set[str] = set()
        dq = deque(start)
        while dq:
            x = dq.popleft()
            if x in seen:
                continue
            seen.add(x)
            for y in deps.get(x, ()):
                if y not in seen:
                    dq.append(y)
            if x.startswith("column:"):
                tn = x[len("column:"):x.index("[")]
                if tid(tn) in deps:
                    dq.append(tid(tn))        # calculated / parameter table source
        return seen

    reachable = closure(report_roots)
    # calculation groups apply to every measure; a used calc-group column keeps its items alive
    for t in model["tables"]:
        if t.get("calculationGroup") and any(cid(t["name"], c["name"]) in reachable for c in t["columns"]):
            reachable |= closure(f"calcItem:{t['name']}[{it['name']}]" for it in t["calculationGroup"]["items"])

    for t in model["tables"]:
        tn = t["name"]
        for m in t["measures"]:
            m["usedInReport"] = mid(m["name"]) in reachable
            m["usedBy"] = usage.get(mid(m["name"]), [])
            cl = closure([mid(m["name"])]) - {mid(m["name"])}
            m["lineage"] = {
                "measures": sorted(x[8:] for x in cl if x.startswith("measure:")),
                "columns": sorted(x[7:] for x in cl if x.startswith("column:")),
                "tables": sorted({x[7:x.index("[")] for x in cl if x.startswith("column:")} |
                                 {x[6:] for x in cl if x.startswith("table:")}),
            }
        for c in t["columns"]:
            key = cid(tn, c["name"])
            c["usedBy"] = usage.get(key, [])
            c["usedInReport"] = key in reachable
            c["referenced"] = bool(c["usedBy"])

    # ---------------------------------------------------------------- change impact
    rev: Dict[str, Set[str]] = defaultdict(set)
    for s_, ds in deps.items():
        for d_ in ds:
            rev[d_].add(s_)
    rel_label = {r["id"]: f"relationship {r['fromTable']}[{r['fromColumn']}] → {r['toTable']}[{r['toColumn']}]"
                 for r in model["relationships"]}

    def label(x: str) -> str:
        if x.startswith("measure:"):
            return f"[{x[8:]}]"
        if x.startswith("reportMeasure:"):
            return f"[{x[14:]}] (report measure)"
        if x.startswith("column:"):
            return x[7:]
        if x.startswith("visual:"):
            vi = vinfo.get(x) or {}
            return f"{vi.get('visualType', 'visual')} '{vi.get('visualTitle') or vi.get('visual')}' on page '{vi.get('page')}'"
        if x.startswith("page:"):
            return f"page '{x[5:]}'"
        if x.startswith("report:"):
            return f"report-level filter of {x[7:]}"
        if x.startswith("relationship:"):
            return rel_label.get(x, x)
        if x.startswith("calcItem:"):
            return "calculation item " + x[9:]
        if x.startswith("role:"):
            return "RLS role " + x[5:]
        if x.startswith("hierarchy:"):
            return "hierarchy " + x[10:]
        if x.startswith("table:"):
            return "calculated / parameter table " + x[6:]
        return x

    def impact(start: str, cap: int = 150) -> dict:
        parent: Dict[str, Optional[str]] = {start: None}
        order: List[str] = []
        dq = deque([start])
        while dq:
            x = dq.popleft()
            for y in sorted(rev.get(x, ())):
                if y not in parent:
                    parent[y] = x
                    order.append(y)
                    dq.append(y)

        def path(n: str) -> List[str]:
            p: List[str] = []
            cur: Optional[str] = n
            while cur is not None:
                p.append(label(cur))
                cur = parent[cur]
            return list(reversed(p))

        res = {"measures": [], "reportMeasures": [], "columns": [], "direct": [], "indirect": [],
               "filters": [], "pages": set(), "other": []}
        for n in order:
            if n.startswith("measure:"):
                res["measures"].append(n[8:])
            elif n.startswith("reportMeasure:"):
                res["reportMeasures"].append(n[14:])
            elif n.startswith("column:"):
                res["columns"].append(n[7:])
            elif n.startswith("visual:"):
                vi = vinfo.get(n) or {}
                e = {"page": vi.get("page"), "visual": vi.get("visual"), "title": vi.get("visualTitle"),
                     "type": vi.get("visualType")}
                res["pages"].add(vi.get("page"))
                if parent[n] == start:
                    res["direct"].append(e)
                else:
                    e["path"] = path(n)
                    res["indirect"].append(e)
            elif n.startswith(("page:", "report:")):
                if not (parent[n] or "").startswith("visual:"):
                    res["filters"].append({"where": label(n), "path": path(n)})
                    if n.startswith("page:"):
                        res["pages"].add(n[5:])
            else:
                res["other"].append(label(n))
        out = {
            "measures": res["measures"], "reportMeasures": res["reportMeasures"],
            "columns": res["columns"],
            "visuals": {"direct": res["direct"][:cap], "indirect": res["indirect"][:cap]},
            "filters": res["filters"][:cap], "pages": sorted(p for p in res["pages"] if p), "other": res["other"],
            "counts": {"measures": len(res["measures"]) + len(res["reportMeasures"]),
                       "visuals": len(res["direct"]) + len(res["indirect"]),
                       "direct": len(res["direct"]), "indirect": len(res["indirect"]),
                       "pages": len([p for p in res["pages"] if p]), "filters": len(res["filters"]),
                       "other": len(res["other"])},
        }
        out["usage"] = ("direct" if res["direct"] or res["filters"] and any(len(f["path"]) == 2 for f in res["filters"])
                        else "indirect" if res["indirect"] or res["filters"] else "none")
        return out

    for t in model["tables"]:
        for m in t["measures"]:
            m["impact"] = impact(mid(m["name"]))
        for c in t["columns"]:
            c["impact"] = impact(cid(t["name"], c["name"]))
    for rm in report_measures.values():
        rm["impact"] = impact(f"reportMeasure:{rm['name']}")

    # ---------------------------------------------------------------- table roles
    _classify(model)

    # ---------------------------------------------------------------- sources per table
    expr_by_name = {e["name"]: e for e in model["expressions"]}
    for t in model["tables"]:
        t["source"] = _table_source(t, expr_by_name)

    # ---------------------------------------------------------------- report lineage
    for rep in reports:
        for p in rep["pages"]:
            pid = f"page:{p['displayName']}"
            cl = closure([pid])
            p["lineage"] = {
                "measures": sorted(x[8:] for x in cl if x.startswith("measure:")),
                "columns": sorted(x[7:] for x in cl if x.startswith("column:")),
                "tables": sorted({x[7:x.index("[")] for x in cl if x.startswith("column:")} |
                                 {_measure_table(measures, x[8:]) for x in cl if x.startswith("measure:")} - {None}),
            }
            for v in p["visuals"]:
                vcl = closure([f"visual:{p['displayName']}/{v['name']}"])
                v["tables"] = sorted({x[7:x.index("[")] for x in vcl if x.startswith("column:")})

    findings = _findings(model, reports, broken, unresolved)
    stats = _stats(model, reports)
    return {
        "findings": findings, "unresolvedReportReferences": broken, "unresolvedDaxReferences": unresolved,
        "referenceStatus": _ref_status(reports),
        "stats": stats,
        "unused": {
            "measures": sorted(m["name"] for m in measures.values() if not m["usedInReport"]),
            "columns": sorted(f"{t['name']}[{c['name']}]" for t in model["tables"] if not t["isAutoDateTable"]
                              for c in t["columns"] if not c["referenced"] and not c["usedInReport"]),
            "note": "Usage is computed against the report(s) in this package only. A shared semantic model may "
                    "serve other reports, so 'unused' means 'not used by the packaged report(s)'.",
        },
        "dependencies": {k: sorted(v) for k, v in deps.items()},
    }


def _ref_status(reports: List[dict]) -> Dict[str, int]:
    counts: Dict[str, int] = defaultdict(int)
    for rep in reports:
        for fl in rep["filters"]:
            if fl.get("field"):
                counts[fl["field"].get("status", "unknown")] += 1
        for p in rep["pages"]:
            for f in [fl["field"] for fl in p["filters"] if fl.get("field")] + p.get("drillthroughFields", []):
                counts[f.get("status", "unknown")] += 1
            for v in p["visuals"]:
                for f in v["fields"] + [fl["field"] for fl in v["filters"] if fl.get("field")]:
                    counts[f.get("status", "unknown")] += 1
    return dict(counts)


def _measure_table(measures: dict, name: str) -> Optional[str]:
    m = measures.get(name)
    return m["table"] if m else None


def _card(r: dict) -> str:
    f = "*" if r["fromCardinality"] == "many" else "1"
    t = "*" if r["toCardinality"] == "many" else "1"
    return f"{f}:{t}"


def _field_parameter(t: dict) -> Optional[str]:
    for c in t["columns"]:
        meta = (c.get("extendedProperties") or {}).get("ParameterMetadata")
        if meta:
            return "fieldParameter" if re.search(r'"kind"\s*:\s*2', meta) else "whatIfParameter"
    for p in t["partitions"]:
        if p.get("expression") and "NAMEOF" in p["expression"].upper():
            return "fieldParameter"
        if (p.get("sourceType") or "") == "calculated" and p.get("expression") and \
                re.match(r"\s*GENERATESERIES\s*\(", p["expression"], re.I):
            return "whatIfParameter"
    return None


def _classify(model: dict) -> None:
    many_side: Dict[str, int] = defaultdict(int)
    one_side: Dict[str, int] = defaultdict(int)
    for r in model["relationships"]:
        if r["fromCardinality"] == "many":
            many_side[r["fromTable"]] += 1
        else:
            one_side[r["fromTable"]] += 1
        if r["toCardinality"] == "one":
            one_side[r["toTable"]] += 1
        else:
            many_side[r["toTable"]] += 1
    for t in model["tables"]:
        n = t["name"]
        part_types = {(p.get("sourceType") or "").lower() for p in t["partitions"]}
        visible_cols = [c for c in t["columns"] if not c["isHidden"]]
        has_date = any((c.get("dataType") or "").lower() == "datetime" for c in t["columns"])
        if t["isAutoDateTable"]:
            role = "autoDate"
        elif t.get("calculationGroup"):
            role = "calculationGroup"
        elif t.get("fieldParameter"):
            role = t["fieldParameter"]
        elif t["measures"] and not visible_cols and not many_side[n] and not one_side[n]:
            role = "measureTable"
        elif (t.get("dataCategory") or "").lower() == "time" or (
                has_date and re.search(r"date|calendar|kalender|datum", n, re.I) and one_side[n]):
            role = "date"
        elif re.search(r"bridge", n, re.I) and many_side[n]:
            role = "bridge"
        elif many_side[n] and not one_side[n]:
            role = "fact"
        elif many_side[n] and one_side[n]:
            role = "bridge" if many_side[n] >= 2 and len(t["columns"]) <= 4 else "fact"
        elif one_side[n]:
            role = "dimension"
        else:
            role = "disconnected"
        t["role"] = role
        t["storageMode"] = next((p.get("mode") for p in t["partitions"] if p.get("mode")), None)
        t["isCalculatedTable"] = "calculated" in part_types
        t["relationshipCount"] = {"many": many_side[n], "one": one_side[n]}


ROLE_LABELS = {
    "fact": "Fact", "dimension": "Dimension", "date": "Date", "bridge": "Bridge",
    "measureTable": "Measure table", "calculationGroup": "Calculation group",
    "fieldParameter": "Field parameter", "whatIfParameter": "What-if parameter",
    "disconnected": "Disconnected / helper", "autoDate": "Auto date/time (generated)",
}


def _table_source(t: dict, exprs: Dict[str, dict]) -> dict:
    src = {"mode": t.get("storageMode"), "type": None, "connectors": [], "navigation": [], "nativeQueries": [],
           "steps": [], "joins": [], "rowFilters": [], "references": [], "parameters": [], "daxExpression": None}
    for p in t["partitions"]:
        st = (p.get("sourceType") or "").lower()
        if st == "calculated":
            src["type"] = "DAX calculated table"
            src["daxExpression"] = p.get("expression")
            continue
        if st == "entity":
            src["type"] = "Entity (Direct Lake / composite)"
            src["navigation"].append({"Entity": p.get("entityName"), "Schema": p.get("schemaName"),
                                      "Expression": p.get("expressionSource")})
            continue
        ma = p.get("mAnalysis")
        if not ma:
            continue
        src["type"] = "Power Query (M)"
        _merge_m(src, ma)
        seen = set()
        todo = list(ma["references"])
        while todo:
            q = todo.pop()
            if q in seen or q not in exprs:
                continue
            seen.add(q)
            e = exprs[q]
            if e.get("isParameter"):
                src["parameters"].append({"name": q, "value": (e.get("parameter") or {}).get("value")})
            elif e.get("mAnalysis"):
                _merge_m(src, e["mAnalysis"], via=q)
                todo.extend(e["mAnalysis"]["references"])
    src["connectors"] = _uniq(src["connectors"])
    return src


def _merge_m(src: dict, ma: dict, via: Optional[str] = None) -> None:
    for s in ma["sources"]:
        s2 = dict(s)
        if via:
            s2["via"] = via
        src["connectors"].append(s2)
    src["navigation"] += ma["navigation"]
    src["nativeQueries"] += ma["nativeQueries"]
    if not via:
        src["steps"] = ma["steps"]
    src["joins"] += ma["joins"]
    src["rowFilters"] += ma["rowFilters"]
    src["references"] = _uniq(src["references"] + ma["references"])


def _uniq(items: list) -> list:
    out, seen = [], set()
    for i in items:
        k = repr(i)
        if k not in seen:
            seen.add(k)
            out.append(i)
    return out


# ---------------------------------------------------------------------------- findings

def _f(sev: str, code: str, msg: str, objects: Optional[list] = None, advice: Optional[str] = None) -> dict:
    return {"severity": sev, "code": code, "message": msg, "objects": objects or [], "advice": advice}


def _findings(model: dict, reports: List[dict], broken: list, unresolved: list) -> List[dict]:
    out: List[dict] = []
    rels = model["relationships"]
    bi = [r for r in rels if r["crossFilteringBehavior"] == "bothDirections"]
    if bi:
        out.append(_f("warning", "bidirectional-relationship",
                      f"{len(bi)} relationship(s) filter in both directions.",
                      [f"{r['fromTable']}[{r['fromColumn']}] ↔ {r['toTable']}[{r['toColumn']}]" for r in bi],
                      "Bi-directional filters can create ambiguous paths and slow queries. Keep them only where "
                      "intended (e.g. bridge tables) and document why."))
    mm = [r for r in rels if r["cardinality"] == "*:*"]
    if mm:
        out.append(_f("warning", "many-to-many", f"{len(mm)} many-to-many relationship(s).",
                      [f"{r['fromTable']}[{r['fromColumn']}] — {r['toTable']}[{r['toColumn']}]" for r in mm],
                      "Many-to-many relationships aggregate differently than users expect; check totals."))
    inactive = [r for r in rels if not r["isActive"]]
    for r in inactive:
        if r["activatedByUserelationship"]:
            out.append(_f("info", "inactive-relationship-used",
                          f"Inactive relationship {r['fromTable']}[{r['fromColumn']}] → {r['toTable']}[{r['toColumn']}] "
                          "is activated by USERELATIONSHIP in at least one measure."))
        else:
            out.append(_f("warning", "inactive-relationship-unused",
                          f"Inactive relationship {r['fromTable']}[{r['fromColumn']}] → {r['toTable']}[{r['toColumn']}] "
                          "is not activated by any measure.", advice="Remove it or add the measure that needs it."))
    mismatch = [r for r in rels if r["fromDataType"] and r["toDataType"] and r["fromDataType"] != r["toDataType"]]
    for r in mismatch:
        out.append(_f("warning", "relationship-type-mismatch",
                      f"Relationship {r['fromTable']}[{r['fromColumn']}] ({r['fromDataType']}) → "
                      f"{r['toTable']}[{r['toColumn']}] ({r['toDataType']}) joins different data types."))
    missing = [r for r in rels if r["fromTable"] not in {t["name"] for t in model["tables"]}
               or r["toTable"] not in {t["name"] for t in model["tables"]}]
    for r in missing:
        out.append(_f("error", "relationship-missing-table", f"Relationship {r['name']} points to a table that does not exist."))

    autod = [t["name"] for t in model["tables"] if t["isAutoDateTable"]]
    if autod:
        out.append(_f("warning", "auto-date-time",
                      f"Auto date/time is on: {len(autod)} hidden date tables were generated.",
                      autod[:10], "Turn off auto date/time and use one marked date table; it bloats the model."))
    dates = [t for t in model["tables"] if t.get("role") == "date"]
    if dates and not any((t.get("dataCategory") or "").lower() == "time" for t in dates):
        out.append(_f("info", "date-table-not-marked",
                      "A date table exists but is not marked as date table.", [t["name"] for t in dates],
                      "Mark it as date table so time-intelligence functions behave predictably."))

    if broken:
        out.append(_f("warning", "unresolved-report-reference",
                      f"{len(broken)} report field reference(s) could not be matched to the semantic model or to "
                      "report-level measures.",
                      [f"{b['field']} in {b['where']}" for b in broken[:20]],
                      "Status 'unresolved', not proven broken. If the packaged model is the one the report uses, "
                      "these visuals show an error in Power BI: check for renamed or deleted fields."))
    ur = [u for u in unresolved if not u["reference"].startswith("[")]
    if ur:
        out.append(_f("warning", "unresolved-dax-reference",
                      f"{len(ur)} DAX reference(s) could not be matched to a model object.",
                      [f"{u['reference']} in {u['object']}" for u in ur[:20]]))

    implicit = []
    for rep in reports:
        for p in rep["pages"]:
            for v in p["visuals"]:
                for f in v["fields"]:
                    if f.get("implicit"):
                        implicit.append(f"{f.get('aggregation')} of {f['table']}[{f['name']}] in '{v['title'] or v['name']}' ({p['displayName']})")
    if implicit:
        out.append(_f("warning", "implicit-measures",
                      f"{len(implicit)} visual field(s) use implicit measures (column aggregated in the visual).",
                      implicit[:20], "Replace with explicit measures so the logic is defined once and documented."))

    keyish = []
    for t in model["tables"]:
        if t["isAutoDateTable"]:
            continue
        for c in t["columns"]:
            if (c.get("dataType") or "").lower() in ("int64", "double", "decimal") and KEYISH.search(c["name"]) \
                    and (c.get("summarizeBy") or "").lower() not in ("none", "") and not c["isHidden"]:
                keyish.append(f"{t['name']}[{c['name']}] (summarizeBy: {c.get('summarizeBy')})")
    if keyish:
        out.append(_f("info", "summarize-keys", f"{len(keyish)} visible ID/key/year column(s) are summed by default.",
                      keyish[:20], "Set summarizeBy to none so they are not added up in visuals."))

    no_desc = [m["name"] for t in model["tables"] for m in t["measures"] if not m.get("description") and not m["isHidden"]]
    total = sum(1 for t in model["tables"] for m in t["measures"] if not m["isHidden"])
    if no_desc:
        out.append(_f("info", "missing-descriptions",
                      f"{len(no_desc)} of {total} visible measures have no description in the model.",
                      advice="Descriptions show as tooltips in Power BI and feed Copilot. Use enrichment to draft them."))
    unused_m = [m["name"] for t in model["tables"] for m in t["measures"] if not m.get("usedInReport")]
    if unused_m:
        out.append(_f("info", "unused-measures", f"{len(unused_m)} measure(s) are not used by the packaged report(s).",
                      unused_m[:30], "Check other reports on the same model before deleting."))
    calc_cols = [f"{t['name']}[{c['name']}]" for t in model["tables"] for c in t["columns"] if c["columnType"] == "calculated"]
    if len(calc_cols) >= 10:
        out.append(_f("info", "many-calculated-columns", f"{len(calc_cols)} calculated columns.", calc_cols[:20],
                      "Consider moving row-level logic upstream (Power Query or the source)."))
    local_paths = []
    for t in model["tables"]:
        for s in t["source"]["connectors"]:
            for a in s.get("arguments", []):
                if re.match(r"^[A-Za-z]:\\|^\\\\", str(a)):
                    local_paths.append(f"{t['name']}: {a}")
    for e in model["expressions"]:
        v = (e.get("parameter") or {}).get("value")
        if v and re.match(r"^[A-Za-z]:\\|^\\\\", str(v)):
            local_paths.append(f"parameter {e['name']}: {v}")
    if local_paths:
        out.append(_f("warning", "local-file-source", "Data is loaded from local or network file paths.",
                      local_paths[:10], "Scheduled refresh in the service needs a gateway or a cloud source."))
    if model["roles"]:
        out.append(_f("info", "row-level-security", f"{len(model['roles'])} security role(s) defined.",
                      [r["name"] for r in model["roles"]]))
    for rep in reports:
        hidden = [p["displayName"] for p in rep["pages"] if p["hidden"]]
        if hidden:
            out.append(_f("info", "hidden-pages", f"Report '{rep['name']}' has {len(hidden)} hidden page(s).", hidden))
        if rep.get("customVisuals"):
            out.append(_f("info", "custom-visuals", f"Report '{rep['name']}' uses custom visuals.", rep["customVisuals"],
                          "Check that these visuals are approved in your tenant."))
    order = {"error": 0, "warning": 1, "info": 2}
    return sorted(out, key=lambda f: order.get(f["severity"], 3))


def _stats(model: dict, reports: List[dict]) -> dict:
    tabs = [t for t in model["tables"] if not t["isAutoDateTable"]]
    return {
        "tables": len(tabs), "autoDateTables": len(model["tables"]) - len(tabs),
        "columns": sum(len(t["columns"]) for t in tabs),
        "calculatedColumns": sum(1 for t in tabs for c in t["columns"] if c["columnType"] == "calculated"),
        "measures": sum(len(t["measures"]) for t in tabs),
        "relationships": len(model["relationships"]),
        "roles": len(model["roles"]),
        "calculationGroups": sum(1 for t in tabs if t.get("calculationGroup")),
        "parameters": sum(1 for e in model["expressions"] if e.get("isParameter")),
        "pages": sum(len(r["pages"]) for r in reports),
        "visuals": sum(len(p["visuals"]) for r in reports for p in r["pages"]),
        "dataVisuals": sum(1 for r in reports for p in r["pages"] for v in p["visuals"] if v["fields"]),
    }


# ---------------------------------------------------------------------------- helpers for queries

def relationship_path(model: dict, a: str, b: str, include_inactive: bool = False) -> Optional[List[dict]]:
    """Shortest relationship path between two tables (undirected), with join keys."""
    adj: Dict[str, List[Tuple[str, dict]]] = defaultdict(list)
    for r in model["relationships"]:
        if not r["isActive"] and not include_inactive:
            continue
        adj[r["fromTable"]].append((r["toTable"], r))
        adj[r["toTable"]].append((r["fromTable"], r))
    names = {t["name"].lower(): t["name"] for t in model["tables"]}
    a, b = names.get(a.lower(), a), names.get(b.lower(), b)
    prev: Dict[str, Tuple[str, dict]] = {}
    seen = {a}
    dq = deque([a])
    while dq:
        x = dq.popleft()
        if x == b:
            break
        for y, r in adj[x]:
            if y not in seen:
                seen.add(y)
                prev[y] = (x, r)
                dq.append(y)
    if b not in seen:
        return None
    path = []
    cur = b
    while cur != a:
        p, r = prev[cur]
        path.append({"from": p, "to": cur, "relationship": r})
        cur = p
    return list(reversed(path))
