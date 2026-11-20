"""Load a report (PBIR folder format or legacy report.json) into the canonical dict.

Canonical shape:
    {name, format, semanticModelRef, theme, filters[], pages[], bookmarks[], customVisuals[], settings{}}
    page   = {name, displayName, ordinal, width, height, hidden, pageType, filters[], visuals[]}
    visual = {name, type, typeLabel, title, text, position{}, hidden, group, fields[], filters[]}
    field  = {kind: column|measure|hierarchyLevel|aggregation, table, name, role, aggregation?, queryRef?}
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

VISUAL_LABELS = {
    "card": "Card", "cardVisual": "Card (new)", "multiRowCard": "Multi-row card", "kpi": "KPI",
    "gauge": "Gauge", "tableEx": "Table", "pivotTable": "Matrix", "slicer": "Slicer",
    "advancedSlicerVisual": "Button slicer", "listSlicer": "List slicer", "textSlicer": "Text slicer",
    "barChart": "Stacked bar chart", "clusteredBarChart": "Clustered bar chart",
    "hundredPercentStackedBarChart": "100% stacked bar chart",
    "columnChart": "Stacked column chart", "clusteredColumnChart": "Clustered column chart",
    "hundredPercentStackedColumnChart": "100% stacked column chart",
    "lineChart": "Line chart", "areaChart": "Area chart", "stackedAreaChart": "Stacked area chart",
    "lineStackedColumnComboChart": "Line and stacked column chart",
    "lineClusteredColumnComboChart": "Line and clustered column chart",
    "ribbonChart": "Ribbon chart", "waterfallChart": "Waterfall chart", "funnel": "Funnel",
    "scatterChart": "Scatter chart", "pieChart": "Pie chart", "donutChart": "Donut chart",
    "treemap": "Treemap", "map": "Map", "filledMap": "Filled map", "shapeMap": "Shape map",
    "azureMap": "Azure map", "textbox": "Text box", "image": "Image", "shape": "Shape",
    "basicShape": "Shape", "actionButton": "Button", "pageNavigator": "Page navigator",
    "bookmarkNavigator": "Bookmark navigator", "decompositionTreeVisual": "Decomposition tree",
    "keyDriversVisual": "Key influencers", "qnaVisual": "Q&A", "scriptVisual": "R script visual",
    "pythonVisual": "Python visual", "aiNarratives": "Smart narrative", "paginator": "Paginated report",
    "rdlVisual": "Paginated report visual",
}
DECORATIVE = {"shape", "basicShape", "image", "actionButton", "pageNavigator", "bookmarkNavigator"}
AGG = {0: "Sum", 1: "Average", 2: "Count", 3: "Min", 4: "Max", 5: "Count (not blank)", 6: "Median",
       7: "Standard deviation", 8: "Variance"}
CMP = {0: "=", 1: ">", 2: ">=", 3: "<", 4: "<=", 5: "<>"}


# ------------------------------------------------------------------------ field references

def _entity(expr: dict, aliases: Dict[str, str]) -> Optional[str]:
    if not isinstance(expr, dict):
        return None
    sr = expr.get("SourceRef")
    if isinstance(sr, dict):
        if "Entity" in sr:
            return sr["Entity"]
        if "Source" in sr:
            return aliases.get(sr["Source"], sr["Source"])
    pv = expr.get("PropertyVariationSource")
    if isinstance(pv, dict):
        return _entity(pv.get("Expression", {}), aliases)
    return None


def field_from_expr(e: dict, aliases: Dict[str, str]) -> Optional[dict]:
    if not isinstance(e, dict):
        return None
    if "Column" in e and isinstance(e["Column"], dict):
        c = e["Column"]
        ex = c.get("Expression", {})
        pv = ex.get("PropertyVariationSource") if isinstance(ex, dict) else None
        if pv:  # auto date/time variation: Sales[OrderDate].Year
            return {"kind": "column", "table": _entity(pv.get("Expression", {}), aliases),
                    "name": pv.get("Property"), "variation": c.get("Property")}
        return {"kind": "column", "table": _entity(ex, aliases), "name": c.get("Property")}
    if "Measure" in e and isinstance(e["Measure"], dict):
        m = e["Measure"]
        return {"kind": "measure", "table": _entity(m.get("Expression", {}), aliases), "name": m.get("Property")}
    if "Aggregation" in e and isinstance(e["Aggregation"], dict):
        a = e["Aggregation"]
        inner = field_from_expr(a.get("Expression", {}), aliases)
        if inner:
            inner = dict(inner)
            inner["aggregation"] = AGG.get(a.get("Function"), str(a.get("Function")))
            inner["implicitMeasure"] = True
        return inner
    if "HierarchyLevel" in e and isinstance(e["HierarchyLevel"], dict):
        hl = e["HierarchyLevel"]
        h = (hl.get("Expression") or {}).get("Hierarchy") or {}
        hex_ = h.get("Expression") or {}
        pv = hex_.get("PropertyVariationSource")
        if pv:
            return {"kind": "column", "table": _entity(pv.get("Expression", {}), aliases),
                    "name": pv.get("Property"), "variation": hl.get("Level")}
        return {"kind": "hierarchyLevel", "table": _entity(hex_, aliases), "hierarchy": h.get("Hierarchy"),
                "name": hl.get("Level")}
    return None


_FIELD_KEYS = ("Column", "Measure", "Aggregation", "HierarchyLevel")


def walk_fields(obj, aliases: Dict[str, str], context: str, out: List[dict]) -> None:
    """Collect every field reference below `obj`, tagging it with a context label."""
    if isinstance(obj, dict):
        if isinstance(obj.get("From"), list):
            aliases = dict(aliases)
            aliases.update(_aliases(obj))
        if any(k in obj and isinstance(obj[k], dict) and ("Expression" in obj[k] or k == "Aggregation") for k in _FIELD_KEYS):
            f = field_from_expr(obj, aliases)
            if f and f.get("name"):
                f["role"] = context
                out.append(f)
                return
        for k, v in obj.items():
            walk_fields(v, aliases, context, out)
    elif isinstance(obj, list):
        for v in obj:
            walk_fields(v, aliases, context, out)


def _aliases(q: dict) -> Dict[str, str]:
    return {f.get("Name"): f.get("Entity") for f in (q or {}).get("From", []) if isinstance(f, dict) and f.get("Name")}


# ------------------------------------------------------------------------ filters

def _literal(v) -> str:
    if isinstance(v, dict):
        lit = v.get("Literal", {}).get("Value") if "Literal" in v else None
        if lit is None:
            return json.dumps(v)[:60]
        s = str(lit)
        if s.startswith("'") and s.endswith("'"):
            return s[1:-1].replace("''", "'")
        if s.startswith("datetime'"):
            return s[9:-1]
        if s[-1:] in ("L", "D", "M") and s[:-1].replace(".", "", 1).lstrip("-").isdigit():
            return s[:-1]
        return s
    return str(v)


def _fname(f: Optional[dict]) -> str:
    if not f:
        return "?"
    return f"{f.get('table')}[{f.get('name')}]"


def describe_condition(cond: dict, aliases: Dict[str, str]) -> str:
    if not isinstance(cond, dict):
        return "?"
    if "In" in cond:
        i = cond["In"]
        exprs = [field_from_expr(e, aliases) for e in i.get("Expressions", [])]
        if "Values" in i:
            vals = [", ".join(_literal(x) for x in row) for row in i.get("Values", [])]
            shown = vals[:12] + (["…"] if len(vals) > 12 else [])
            return f"{', '.join(_fname(e) for e in exprs)} in ({'; '.join(shown)})"
        if "Table" in i:
            return f"{', '.join(_fname(e) for e in exprs)} in (table)"
    if "Not" in cond:
        return "NOT (" + describe_condition(cond["Not"].get("Expression", {}), aliases) + ")"
    if "And" in cond:
        return "(" + describe_condition(cond["And"].get("Left", {}), aliases) + " AND " + \
               describe_condition(cond["And"].get("Right", {}), aliases) + ")"
    if "Or" in cond:
        return "(" + describe_condition(cond["Or"].get("Left", {}), aliases) + " OR " + \
               describe_condition(cond["Or"].get("Right", {}), aliases) + ")"
    if "Comparison" in cond:
        c = cond["Comparison"]
        left = field_from_expr(c.get("Left", {}), aliases)
        right = c.get("Right", {})
        rtxt = _literal(right) if "Literal" in right else _relative(right)
        return f"{_fname(left)} {CMP.get(c.get('ComparisonKind'), '?')} {rtxt}"
    if "Between" in cond:
        b = cond["Between"]
        return f"{_fname(field_from_expr(b.get('Expression', {}), aliases))} between {_literal(b.get('LowerBound', {}))} and {_literal(b.get('UpperBound', {}))}"
    if "Contains" in cond:
        c = cond["Contains"]
        return f"{_fname(field_from_expr(c.get('Left', {}), aliases))} contains {_literal(c.get('Right', {}))}"
    if "StartsWith" in cond:
        c = cond["StartsWith"]
        return f"{_fname(field_from_expr(c.get('Left', {}), aliases))} starts with {_literal(c.get('Right', {}))}"
    return "complex condition"


def _relative(v: dict) -> str:
    if "DateSpan" in v or "DateAdd" in v or "Now" in v:
        return "relative date (" + json.dumps(v)[:80] + ")"
    return json.dumps(v)[:80]


def parse_filters(filters, scope: str) -> List[dict]:
    out = []
    for f in filters or []:
        if not isinstance(f, dict):
            continue
        fld = field_from_expr(f.get("field") or f.get("expression") or {}, {})
        filt = f.get("filter") or {}
        aliases = _aliases(filt)
        if not fld and filt:
            tmp: List[dict] = []
            walk_fields(filt, aliases, scope, tmp)
            fld = tmp[0] if tmp else None
        conds = [describe_condition(w.get("Condition", {}), aliases) for w in filt.get("Where", [])]
        out.append({
            "name": f.get("name"), "scope": scope, "type": f.get("type"),
            "field": fld, "condition": " AND ".join(conds) if conds else None,
            "isActive": bool(conds),
            "hidden": bool(f.get("isHiddenInViewMode")), "locked": bool(f.get("isLockedInViewMode")),
            "howCreated": f.get("howCreated"),
        })
    return out


# ------------------------------------------------------------------------ text helpers

def _literal_text(objs: dict, *path) -> Optional[str]:
    cur = objs
    for p in path:
        if isinstance(cur, list):
            cur = cur[0] if cur else None
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    if isinstance(cur, dict) and "expr" in cur:
        lit = cur["expr"].get("Literal", {}).get("Value")
        if isinstance(lit, str) and lit.startswith("'"):
            return lit[1:-1].replace("''", "'")
        f = field_from_expr(cur["expr"], {})
        if f and f.get("name"):
            return "(dynamic title from [" + f["name"] + "])"
    return None


def _title(visual: dict) -> Optional[str]:
    for key in ("visualContainerObjects", "vcObjects", "objects"):
        objs = visual.get(key) or {}
        t = _literal_text(objs, "title", "properties", "text")
        if t:
            return t
    return None


def _textbox(visual: dict) -> Optional[str]:
    objs = visual.get("objects") or {}
    gen = objs.get("general") or []
    paras = None
    if gen and isinstance(gen, list):
        paras = (gen[0].get("properties") or {}).get("paragraphs")
    if not paras:
        return None
    if isinstance(paras, dict):  # literal-wrapped
        return None
    lines = []
    for p in paras:
        runs = p.get("textRuns", []) if isinstance(p, dict) else []
        lines.append("".join(str(r.get("value", "")) for r in runs if isinstance(r, dict)))
    txt = "\n".join(l for l in lines if l.strip())
    return txt or None


def _dedupe(fields: List[dict]) -> List[dict]:
    seen, out = set(), []
    for f in fields:
        key = (f.get("kind"), f.get("table"), f.get("name"), f.get("role"), f.get("aggregation"), f.get("variation"))
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


# ------------------------------------------------------------------------ PBIR

def _read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return None


def load_pbir(folder: Path, name: str) -> dict:
    d = folder / "definition"
    rep = _read_json(d / "report.json") or {}
    out = _empty(name, "pbir")
    tc = rep.get("themeCollection") or {}
    out["theme"] = {"base": (tc.get("baseTheme") or {}).get("name"), "custom": (tc.get("customTheme") or {}).get("name")}
    out["filters"] = parse_filters((rep.get("filterConfig") or {}).get("filters"), "report")
    out["customVisuals"] = rep.get("publicCustomVisuals") or []
    out["settings"] = rep.get("settings") or {}
    pages_meta = _read_json(d / "pages" / "pages.json") or {}
    order = pages_meta.get("pageOrder") or []
    out["activePage"] = pages_meta.get("activePageName")
    page_dirs = [p for p in (d / "pages").iterdir() if p.is_dir()] if (d / "pages").is_dir() else []
    pages = []
    for pd in page_dirs:
        pj = _read_json(pd / "page.json") or {}
        pname = pj.get("name", pd.name)
        binding = pj.get("pageBinding") or {}
        ptype = binding.get("type") or ("Tooltip" if pj.get("type") == "Tooltip" else None)
        page = {
            "name": pname, "displayName": pj.get("displayName", pname),
            "ordinal": order.index(pname) if pname in order else 10_000 + len(pages),
            "width": pj.get("width"), "height": pj.get("height"), "displayOption": pj.get("displayOption"),
            "hidden": pj.get("visibility") == "HiddenInViewMode",
            "pageType": ptype or "Standard",
            "drillthroughFields": [],
            "filters": parse_filters((pj.get("filterConfig") or {}).get("filters"), "page"),
            "visuals": [],
        }
        for prm in binding.get("parameters", []) or []:
            tmp: List[dict] = []
            walk_fields(prm, {}, "drillthrough", tmp)
            page["drillthroughFields"].extend(tmp)
        vdir = pd / "visuals"
        if vdir.is_dir():
            for vd in sorted(p for p in vdir.iterdir() if p.is_dir()):
                vj = _read_json(vd / "visual.json")
                if vj:
                    page["visuals"].append(_pbir_visual(vj))
        page["visuals"].sort(key=lambda v: (v["position"].get("y", 0), v["position"].get("x", 0)))
        pages.append(page)
    out["pages"] = sorted(pages, key=lambda p: p["ordinal"])
    bdir = d / "bookmarks"
    if bdir.is_dir():
        for bf in sorted(bdir.glob("*.bookmark.json")):
            bj = _read_json(bf) or {}
            out["bookmarks"].append({"name": bj.get("name"), "displayName": bj.get("displayName"),
                                     "page": (bj.get("explorationState") or {}).get("activeSection")})
    return out


def _pbir_visual(vj: dict) -> dict:
    vis = vj.get("visual") or {}
    vtype = vis.get("visualType") or ("group" if "visualGroup" in vj else "unknown")
    fields: List[dict] = []
    qs = (vis.get("query") or {}).get("queryState") or {}
    for role, bucket in qs.items():
        for proj in (bucket or {}).get("projections", []) or []:
            f = field_from_expr(proj.get("field") or {}, {})
            if f and f.get("name"):
                f["role"] = role
                f["queryRef"] = proj.get("queryRef")
                if proj.get("displayName"):
                    f["displayName"] = proj["displayName"]
                fields.append(f)
        if (bucket or {}).get("fieldParameters"):
            for fp in bucket["fieldParameters"]:
                tmp: List[dict] = []
                walk_fields(fp, {}, role + " (field parameter)", tmp)
                fields.extend(tmp)
    extra: List[dict] = []
    walk_fields((vis.get("query") or {}).get("sortDefinition"), {}, "sort", extra)
    walk_fields(vis.get("objects"), {}, "formatting", extra)
    walk_fields(vis.get("visualContainerObjects"), {}, "formatting", extra)
    fields.extend(extra)
    pos = vj.get("position") or {}
    return {
        "name": vj.get("name"), "type": vtype, "typeLabel": VISUAL_LABELS.get(vtype, vtype),
        "title": _title(vis) or (vj.get("visualGroup") or {}).get("displayName"),
        "text": _textbox(vis) if vtype == "textbox" else None,
        "position": {k: pos.get(k) for k in ("x", "y", "z", "width", "height")},
        "hidden": bool(vj.get("isHidden")),
        "group": vj.get("parentGroupName"),
        "decorative": vtype in DECORATIVE or vtype == "group",
        "fields": _dedupe(fields),
        "filters": parse_filters((vj.get("filterConfig") or {}).get("filters"), "visual"),
    }


# ------------------------------------------------------------------------ legacy report.json

def _jload(s):
    if isinstance(s, (dict, list)):
        return s
    if not s:
        return {}
    try:
        return json.loads(s)
    except ValueError:
        return {}


def load_legacy(folder: Path, name: str) -> dict:
    rep = _read_json(folder / "report.json") or {}
    out = _empty(name, "legacy")
    cfg = _jload(rep.get("config"))
    tc = cfg.get("themeCollection") or {}
    out["theme"] = {"base": (tc.get("baseTheme") or {}).get("name"), "custom": (tc.get("customTheme") or {}).get("name")}
    out["filters"] = parse_filters(_jload(rep.get("filters")), "report")
    out["customVisuals"] = rep.get("publicCustomVisuals") or []
    for b in cfg.get("bookmarks", []) or []:
        out["bookmarks"].append({"name": b.get("name"), "displayName": b.get("displayName"),
                                 "page": (b.get("explorationState") or {}).get("activeSection")})
        for ch in b.get("children", []) or []:
            out["bookmarks"].append({"name": ch.get("name"), "displayName": ch.get("displayName"),
                                     "page": (ch.get("explorationState") or {}).get("activeSection")})
    for k, sec in enumerate(rep.get("sections", [])):
        scfg = _jload(sec.get("config"))
        ptype = {1: "Drillthrough", 2: "Tooltip"}.get(scfg.get("type"), "Standard") if isinstance(scfg.get("type"), int) else "Standard"
        page = {
            "name": sec.get("name"), "displayName": sec.get("displayName"), "ordinal": sec.get("ordinal", k),
            "width": sec.get("width"), "height": sec.get("height"), "displayOption": sec.get("displayOption"),
            "hidden": scfg.get("visibility") == 1, "pageType": ptype, "drillthroughFields": [],
            "filters": parse_filters(_jload(sec.get("filters")), "page"), "visuals": [],
        }
        for vc in sec.get("visualContainers", []):
            page["visuals"].append(_legacy_visual(vc))
        page["visuals"].sort(key=lambda v: (v["position"].get("y") or 0, v["position"].get("x") or 0))
        out["pages"].append(page)
    out["pages"].sort(key=lambda p: p["ordinal"])
    return out


def _legacy_visual(vc: dict) -> dict:
    cfg = _jload(vc.get("config"))
    sv = cfg.get("singleVisual") or {}
    vtype = sv.get("visualType") or ("group" if "singleVisualGroup" in cfg else "unknown")
    pq = sv.get("prototypeQuery") or {}
    aliases = _aliases(pq)
    by_ref = {}
    for sel in pq.get("Select", []) or []:
        f = field_from_expr(sel, aliases)
        if f:
            by_ref[sel.get("Name")] = f
    fields: List[dict] = []
    for role, projs in (sv.get("projections") or {}).items():
        for pr in projs or []:
            f = by_ref.get(pr.get("queryRef"))
            if f:
                f = dict(f)
                f["role"] = role
                f["queryRef"] = pr.get("queryRef")
                fields.append(f)
    extra: List[dict] = []
    walk_fields(sv.get("objects"), aliases, "formatting", extra)
    walk_fields(sv.get("vcObjects"), aliases, "formatting", extra)
    for ob in pq.get("OrderBy", []) or []:
        walk_fields(ob, aliases, "sort", extra)
    fields.extend(extra)
    return {
        "name": cfg.get("name"), "type": vtype, "typeLabel": VISUAL_LABELS.get(vtype, vtype),
        "title": _title(sv) or (cfg.get("singleVisualGroup") or {}).get("displayName"),
        "text": _textbox(sv) if vtype == "textbox" else None,
        "position": {"x": vc.get("x"), "y": vc.get("y"), "z": vc.get("z"),
                     "width": vc.get("width"), "height": vc.get("height")},
        "hidden": bool(sv.get("display", {}).get("mode") == "hidden") if isinstance(sv.get("display"), dict) else False,
        "group": cfg.get("parentGroupName"),
        "decorative": vtype in DECORATIVE or vtype == "group",
        "fields": _dedupe(fields),
        "filters": parse_filters(_jload(vc.get("filters")), "visual"),
    }


# ------------------------------------------------------------------------ entry

def _empty(name: str, fmt: str) -> dict:
    return {"name": name, "format": fmt, "semanticModelRef": None, "theme": {}, "filters": [],
            "pages": [], "bookmarks": [], "customVisuals": [], "settings": {}, "activePage": None}


def load_report(folder: Path) -> Optional[dict]:
    name = folder.name[: -len(".Report")] if folder.name.endswith(".Report") else folder.name
    if (folder / "definition" / "report.json").is_file() or (folder / "definition" / "pages").is_dir():
        rep = load_pbir(folder, name)
    elif (folder / "report.json").is_file():
        rep = load_legacy(folder, name)
    else:
        return None
    pbir = _read_json(folder / "definition.pbir") or {}
    ref = pbir.get("datasetReference") or {}
    if "byPath" in ref:
        rep["semanticModelRef"] = {"type": "byPath", "path": (ref.get("byPath") or {}).get("path")}
    elif "byConnection" in ref:
        conn = ref.get("byConnection") or {}
        # Keep only non-identifying parts: a connection string carries workspace URLs.
        rep["semanticModelRef"] = {"type": "byConnection",
                                   "modelName": conn.get("pbiModelDatabaseName") or conn.get("name"),
                                   "note": "Live connection to a published semantic model; model not in the package."}
    return rep
