"""Load a semantic model (TMDL folder or legacy model.bim) into the canonical dict.

Canonical shape (see references/knowledge-schema.md):
    {name, format, culture, compatibilityLevel, settings, tables[], relationships[],
     roles[], perspectives[], expressions[], functions[], cultures[], dataSources[], annotations{}}
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from . import tmdl
from .tmdl import Node, unquote, parse_object_ref

BOOL_TRUE = {"true", "1", ""}


def _b(v) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in BOOL_TRUE


def _s(v) -> Optional[str]:
    if v is None or v is True:
        return None
    return unquote(str(v))


def _expr(v) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, list):
        return "\n".join(v)
    return str(v)


def _annotations(node: Node) -> Dict[str, str]:
    return {a.name: (a.expr or "") for a in node.all("annotation") if a.name}


# ---------------------------------------------------------------------------- TMDL

def load_tmdl(definition_dir: Path, name: str) -> dict:
    files = sorted(definition_dir.rglob("*.tmdl"))
    nodes: List[Node] = []
    file_list = []
    for f in files:
        rel = f.relative_to(definition_dir).as_posix()
        file_list.append(rel)
        doc = tmdl.parse(f.read_text(encoding="utf-8-sig", errors="replace"), file=rel)
        nodes.extend(doc.children)

    model = _empty(name, "tmdl")
    model["files"] = file_list
    for n in nodes:
        if n.kind == "model":
            model["culture"] = _s(n.props.get("culture"))
            model["settings"] = {k: (v if v is not True else True) for k, v in n.props.items() if k != "culture"}
            model["annotations"].update(_annotations(n))
            model["description"] = n.description
            model["tableOrder"] = [c.name for c in n.children if c.kind == "table" and c.is_ref]
            for qg in n.all("queryGroup"):
                model["queryGroups"].append(qg.name)
        elif n.kind == "database":
            cl = n.props.get("compatibilityLevel")
            model["compatibilityLevel"] = int(cl) if cl and str(cl).isdigit() else cl
        elif n.kind == "table" and not n.is_ref:
            model["tables"].append(_tmdl_table(n))
        elif n.kind == "relationship":
            model["relationships"].append(_tmdl_rel(n))
        elif n.kind == "expression":
            model["expressions"].append({
                "name": n.name, "expression": n.expr, "description": n.description,
                "queryGroup": _s(n.props.get("queryGroup")), "kind": _s(n.props.get("kind")) or "m",
                "annotations": _annotations(n),
            })
        elif n.kind == "function":
            model["functions"].append({"name": n.name, "expression": n.expr, "description": n.description})
        elif n.kind == "role":
            model["roles"].append(_tmdl_role(n))
        elif n.kind == "perspective":
            model["perspectives"].append({
                "name": n.name,
                "tables": [c.name for c in n.all("perspectiveTable")],
            })
        elif n.kind == "cultureInfo":
            model["cultures"].append({"name": n.name, "hasTranslations": any(c.kind == "translations" for c in n.children)})
        elif n.kind == "dataSource":
            model["dataSources"].append({"name": n.name, "type": _s(n.props.get("type")),
                                         "connectionString": _s(n.props.get("connectionString"))})
        elif n.kind == "queryGroup":
            model["queryGroups"].append(n.name)
    return model


def _tmdl_table(n: Node) -> dict:
    t = {
        "name": n.name, "description": n.description,
        "isHidden": _b(n.props.get("isHidden")) if "isHidden" in n.props else False,
        "isPrivate": "isPrivate" in n.props,
        "dataCategory": _s(n.props.get("dataCategory")),
        "showAsVariationsOnly": "showAsVariationsOnly" in n.props,
        "columns": [], "measures": [], "hierarchies": [], "partitions": [],
        "calculationGroup": None, "annotations": _annotations(n),
        "excludeFromModelRefresh": "excludeFromModelRefresh" in n.props,
    }
    for c in n.children:
        if c.kind == "column":
            ext = {e.name: e.expr for e in c.all("extendedProperty")}
            col = {
                "name": c.name, "dataType": _s(c.props.get("dataType")),
                "columnType": "calculated" if c.expr is not None else "data",
                "expression": c.expr, "sourceColumn": _s(c.props.get("sourceColumn")),
                "formatString": _s(c.props.get("formatString")),
                "isHidden": "isHidden" in c.props and _b(c.props.get("isHidden")),
                "isKey": "isKey" in c.props, "summarizeBy": _s(c.props.get("summarizeBy")),
                "sortByColumn": _s(c.props.get("sortByColumn")),
                "dataCategory": _s(c.props.get("dataCategory")),
                "displayFolder": _s(c.props.get("displayFolder")),
                "description": c.description,
                "isNameInferred": "isNameInferred" in c.props,
                "extendedProperties": ext,
                "annotations": _annotations(c),
            }
            if col["sourceColumn"] and col["sourceColumn"].startswith("[") and col["expression"] is None:
                col["columnType"] = "calculatedTableColumn"
            t["columns"].append(col)
        elif c.kind == "measure":
            t["measures"].append({
                "name": c.name, "table": n.name, "expression": c.expr,
                "formatString": _s(c.props.get("formatString")),
                "formatStringExpression": c.props.get("formatStringDefinition") if isinstance(c.props.get("formatStringDefinition"), str) else None,
                "displayFolder": _s(c.props.get("displayFolder")),
                "description": c.description,
                "isHidden": "isHidden" in c.props and _b(c.props.get("isHidden")),
                "dataType": _s(c.props.get("dataType")),
                "detailRowsExpression": c.props.get("detailRowsDefinition") if isinstance(c.props.get("detailRowsDefinition"), str) else None,
                "kpi": bool(c.child("kpi")),
                "annotations": _annotations(c),
            })
        elif c.kind == "hierarchy":
            t["hierarchies"].append({
                "name": c.name, "description": c.description,
                "isHidden": "isHidden" in c.props,
                "levels": [{"name": l.name, "column": _s(l.props.get("column"))} for l in c.all("level")],
            })
        elif c.kind == "partition":
            src = c.props.get("source")
            t["partitions"].append({
                "name": c.name, "sourceType": (c.expr or "").strip() or None,
                "mode": _s(c.props.get("mode")), "queryGroup": _s(c.props.get("queryGroup")),
                "expression": src if isinstance(src, str) else None,
                "entityName": _s(c.props.get("entityName")),
                "expressionSource": _s(c.props.get("expressionSource")),
                "schemaName": _s(c.props.get("schemaName")),
            })
        elif c.kind == "calculationGroup":
            items = []
            for k, it in enumerate(c.all("calculationItem")):
                items.append({
                    "name": it.name, "expression": it.expr, "description": it.description,
                    "ordinal": int(it.props["ordinal"]) if str(it.props.get("ordinal", "")).isdigit() else k,
                    "formatStringExpression": it.props.get("formatStringDefinition") if isinstance(it.props.get("formatStringDefinition"), str) else None,
                })
            t["calculationGroup"] = {
                "precedence": c.props.get("precedence"), "items": items,
                "noSelectionExpression": c.props.get("noSelectionExpression"),
            }
        elif c.kind == "refreshPolicy":
            t["refreshPolicy"] = {k: v for k, v in c.props.items()}
    return t


def _tmdl_rel(n: Node) -> dict:
    ft, fc = parse_object_ref(str(n.props.get("fromColumn", "")))
    tt, tc = parse_object_ref(str(n.props.get("toColumn", "")))
    return {
        "name": n.name, "fromTable": ft, "fromColumn": fc, "toTable": tt, "toColumn": tc,
        "fromCardinality": _s(n.props.get("fromCardinality")) or "many",
        "toCardinality": _s(n.props.get("toCardinality")) or "one",
        "crossFilteringBehavior": _s(n.props.get("crossFilteringBehavior")) or "oneDirection",
        "isActive": _b(n.props["isActive"]) if "isActive" in n.props else True,
        "securityFilteringBehavior": _s(n.props.get("securityFilteringBehavior")) or "oneDirection",
        "joinOnDateBehavior": _s(n.props.get("joinOnDateBehavior")),
        "relyOnReferentialIntegrity": "relyOnReferentialIntegrity" in n.props,
    }


def _tmdl_role(n: Node) -> dict:
    return {
        "name": n.name, "description": n.description,
        "modelPermission": _s(n.props.get("modelPermission")),
        "tablePermissions": [{"table": tp.name, "filterExpression": tp.expr} for tp in n.all("tablePermission")],
        "memberCount": len(n.all("member")),
    }


# ---------------------------------------------------------------------------- BIM

def load_bim(path: Path, name: str) -> dict:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    m = data.get("model", data)
    model = _empty(name, "bim")
    model["files"] = [path.name]
    model["compatibilityLevel"] = data.get("compatibilityLevel")
    model["culture"] = m.get("culture")
    model["description"] = m.get("description")
    model["settings"] = {k: v for k, v in m.items() if k in (
        "defaultPowerBIDataSourceVersion", "discourageImplicitMeasures", "sourceQueryCulture",
        "defaultMode", "dataAccessOptions")}
    model["annotations"] = {a.get("name"): str(a.get("value")) for a in m.get("annotations", [])}
    for t in m.get("tables", []):
        model["tables"].append(_bim_table(t))
    for r in m.get("relationships", []):
        model["relationships"].append({
            "name": r.get("name"), "fromTable": r.get("fromTable"), "fromColumn": r.get("fromColumn"),
            "toTable": r.get("toTable"), "toColumn": r.get("toColumn"),
            "fromCardinality": r.get("fromCardinality", "many"), "toCardinality": r.get("toCardinality", "one"),
            "crossFilteringBehavior": r.get("crossFilteringBehavior", "oneDirection"),
            "isActive": r.get("isActive", True),
            "securityFilteringBehavior": r.get("securityFilteringBehavior", "oneDirection"),
            "joinOnDateBehavior": r.get("joinOnDateBehavior"),
            "relyOnReferentialIntegrity": bool(r.get("relyOnReferentialIntegrity")),
        })
    for e in m.get("expressions", []):
        model["expressions"].append({
            "name": e.get("name"), "expression": _expr(e.get("expression")), "description": e.get("description"),
            "queryGroup": e.get("queryGroup"), "kind": e.get("kind", "m"),
            "annotations": {a.get("name"): str(a.get("value")) for a in e.get("annotations", [])},
        })
    for f in m.get("functions", []):
        model["functions"].append({"name": f.get("name"), "expression": _expr(f.get("expression")), "description": f.get("description")})
    for r in m.get("roles", []):
        model["roles"].append({
            "name": r.get("name"), "description": r.get("description"), "modelPermission": r.get("modelPermission"),
            "tablePermissions": [{"table": tp.get("name"), "filterExpression": _expr(tp.get("filterExpression"))}
                                 for tp in r.get("tablePermissions", [])],
            "memberCount": len(r.get("members", [])),
        })
    for p in m.get("perspectives", []):
        model["perspectives"].append({"name": p.get("name"), "tables": [t.get("name") for t in p.get("tables", [])]})
    for c in m.get("cultures", []):
        model["cultures"].append({"name": c.get("name"), "hasTranslations": bool(c.get("translations"))})
    for d in m.get("dataSources", []):
        model["dataSources"].append({"name": d.get("name"), "type": d.get("type"),
                                     "connectionString": d.get("connectionString")})
    for qg in m.get("queryGroups", []):
        model["queryGroups"].append(qg.get("folder"))
    return model


def _bim_table(t: dict) -> dict:
    out = {
        "name": t.get("name"), "description": t.get("description"), "isHidden": bool(t.get("isHidden")),
        "isPrivate": bool(t.get("isPrivate")), "dataCategory": t.get("dataCategory"),
        "showAsVariationsOnly": bool(t.get("showAsVariationsOnly")),
        "columns": [], "measures": [], "hierarchies": [], "partitions": [], "calculationGroup": None,
        "annotations": {a.get("name"): str(a.get("value")) for a in t.get("annotations", [])},
        "excludeFromModelRefresh": bool(t.get("excludeFromModelRefresh")),
    }
    for c in t.get("columns", []):
        ctype = c.get("type", "data")
        if ctype == "rowNumber":
            continue
        out["columns"].append({
            "name": c.get("name"), "dataType": c.get("dataType"), "columnType": ctype,
            "expression": _expr(c.get("expression")), "sourceColumn": c.get("sourceColumn"),
            "formatString": c.get("formatString"), "isHidden": bool(c.get("isHidden")),
            "isKey": bool(c.get("isKey")), "summarizeBy": c.get("summarizeBy"),
            "sortByColumn": c.get("sortByColumn"), "dataCategory": c.get("dataCategory"),
            "displayFolder": c.get("displayFolder"), "description": c.get("description"),
            "isNameInferred": bool(c.get("isNameInferred")),
            "extendedProperties": {e.get("name"): json.dumps(e.get("value")) for e in c.get("extendedProperties", [])},
            "annotations": {a.get("name"): str(a.get("value")) for a in c.get("annotations", [])},
        })
    for m in t.get("measures", []):
        fsd = m.get("formatStringDefinition") or {}
        drd = m.get("detailRowsDefinition") or {}
        out["measures"].append({
            "name": m.get("name"), "table": t.get("name"), "expression": _expr(m.get("expression")),
            "formatString": m.get("formatString"), "formatStringExpression": _expr(fsd.get("expression")),
            "displayFolder": m.get("displayFolder"), "description": m.get("description"),
            "isHidden": bool(m.get("isHidden")), "dataType": m.get("dataType"),
            "detailRowsExpression": _expr(drd.get("expression")), "kpi": bool(m.get("kpi")),
            "annotations": {a.get("name"): str(a.get("value")) for a in m.get("annotations", [])},
        })
    for h in t.get("hierarchies", []):
        out["hierarchies"].append({
            "name": h.get("name"), "description": h.get("description"), "isHidden": bool(h.get("isHidden")),
            "levels": [{"name": l.get("name"), "column": l.get("column")} for l in h.get("levels", [])],
        })
    for p in t.get("partitions", []):
        src = p.get("source") or {}
        out["partitions"].append({
            "name": p.get("name"), "sourceType": src.get("type"), "mode": p.get("mode"),
            "queryGroup": p.get("queryGroup"), "expression": _expr(src.get("expression") or src.get("query")),
            "entityName": src.get("entityName"), "expressionSource": src.get("expressionSource"),
            "schemaName": src.get("schemaName"),
        })
    cg = t.get("calculationGroup")
    if cg:
        out["calculationGroup"] = {
            "precedence": cg.get("precedence"),
            "items": [{
                "name": it.get("name"), "expression": _expr(it.get("expression")), "description": it.get("description"),
                "ordinal": it.get("ordinal", k),
                "formatStringExpression": _expr((it.get("formatStringDefinition") or {}).get("expression")),
            } for k, it in enumerate(cg.get("calculationItems", []))],
            "noSelectionExpression": None,
        }
    if t.get("refreshPolicy"):
        out["refreshPolicy"] = t["refreshPolicy"]
    return out


def _empty(name: str, fmt: str) -> dict:
    return {
        "name": name, "format": fmt, "culture": None, "compatibilityLevel": None, "description": None,
        "settings": {}, "tables": [], "relationships": [], "roles": [], "perspectives": [],
        "expressions": [], "functions": [], "cultures": [], "dataSources": [], "queryGroups": [],
        "annotations": {}, "tableOrder": [], "files": [],
    }


def load_semantic_model(folder: Path) -> Optional[dict]:
    """`folder` is a <Name>.SemanticModel directory."""
    name = folder.name[: -len(".SemanticModel")] if folder.name.endswith(".SemanticModel") else folder.name
    definition = folder / "definition"
    if definition.is_dir() and any(definition.rglob("*.tmdl")):
        model = load_tmdl(definition, name)
    elif (folder / "model.bim").is_file():
        model = load_bim(folder / "model.bim", name)
    else:
        return None
    pbism = folder / "definition.pbism"
    if pbism.is_file():
        try:
            model["pbism"] = json.loads(pbism.read_text(encoding="utf-8-sig"))
        except ValueError:
            pass
    dax_queries = folder / "DAXQueries"
    if dax_queries.is_dir():
        model["daxQueries"] = [{"name": f.stem, "query": f.read_text(encoding="utf-8-sig", errors="replace")}
                               for f in sorted(dax_queries.glob("*.dax"))]
    return model
