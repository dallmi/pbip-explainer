"""Cross-model analysis over several knowledge packs (a portfolio of semantic models).

Answers the questions a grown semantic layer raises:
- Which measures share a name across models, and is the logic the same?
- Which tables exist in several models, and do their columns differ?
- Which source tables or files feed several models?
- Do the same two tables join on different keys in different models?
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from . import dax


def norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def norm_dax(expr: Optional[str]) -> str:
    """Comparable form of a DAX expression: no comments, no whitespace, upper case outside strings."""
    if not expr:
        return ""
    clean = dax.blank_out(expr)          # comments and string contents become spaces
    # put string contents back so "Join" vs "Leave" still differ
    out, i = [], 0
    for m in re.finditer(r'"(?:[^"]|"")*"', expr):
        out.append(re.sub(r"\s+", "", clean[i:m.start()]).upper())
        out.append(m.group(0))
        i = m.end()
    out.append(re.sub(r"\s+", "", clean[i:]).upper())
    return "".join(out)


def _source_signatures(t: dict) -> List[Tuple[str, str]]:
    """(signature, readable label) for each upstream object a table reads."""
    s = t.get("source") or {}
    sigs: List[Tuple[str, str]] = []
    exprs = " ".join(p.get("expression") or "" for p in t.get("partitions", []))
    for f in sorted(set(re.findall(r'([A-Za-z0-9_.\- ]+\.(?:parquet|csv|xlsx|xls|json|txt))"', exprs, re.I))):
        f = f.strip().lstrip("\\/")
        sigs.append(("file:" + f.lower(), f"file {f}"))
    nav = [n for n in s.get("navigation", []) if n.get("Name") or n.get("Item") or n.get("Entity")]
    if nav:
        kinds = {c["kind"] for c in s.get("connectors", [])}
        path = ".".join(str(n.get("Name") or n.get("Item") or n.get("Entity")) for n in nav)
        kind = sorted(kinds)[0] if kinds else "source"
        sigs.append((f"nav:{kind}:{path.lower()}", f"{kind} {path}"))
    for q in s.get("nativeQueries", []):
        for tbl in re.findall(r"\b(?:FROM|JOIN)\s+([\w.\[\]\"`]+)", q, re.I):
            name = tbl.strip('[]"`')
            sigs.append(("sql:" + name.lower(), f"SQL table {name}"))
    seen, out = set(), []
    for sig, label in sigs:
        if sig not in seen:
            seen.add(sig)
            out.append((sig, label))
    return out


def analyse(units: List[dict]) -> dict:
    """`units`: [{"key", "model", "reports", "run"}] - one per semantic model (see collect_units)."""
    measures: Dict[str, List[dict]] = defaultdict(list)
    tables: Dict[str, List[dict]] = defaultdict(list)
    sources: Dict[str, dict] = {}
    rels: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    models_out = []

    for u in units:
        m, key = u["model"], u["key"]
        st = m["analysis"]["stats"]
        sev = defaultdict(int)
        for f in m["analysis"]["findings"]:
            sev[f["severity"]] += 1
        kinds = sorted({c["kind"] for t in m["tables"] for c in (t.get("source") or {}).get("connectors", [])})
        models_out.append({
            "key": key, "name": m["name"], "run": u.get("run"), "reports": [r["name"] for r in u["reports"]],
            "pages": sum(len(r["pages"]) for r in u["reports"]), "stats": st, "findings": dict(sev),
            "sourceKinds": kinds, "purpose": ((u.get("enrichment") or {}).get("report") or {}).get("purpose") or "",
        })
        for t in m["tables"]:
            if t.get("isAutoDateTable"):
                continue
            for me in t["measures"]:
                measures[norm_name(me["name"])].append({
                    "model": key, "table": t["name"], "name": me["name"], "expression": me["expression"] or "",
                    "norm": norm_dax(me["expression"]), "description": me.get("description") or "",
                    "formatString": me.get("formatString") or "", "hidden": me["isHidden"],
                })
            if t["role"] in ("measureTable", "calculationGroup", "fieldParameter", "whatIfParameter"):
                continue
            tables[norm_name(t["name"])].append({
                "model": key, "name": t["name"], "role": t["role"],
                "columns": sorted(c["name"] for c in t["columns"]),
            })
            for sig, label in _source_signatures(t):
                sources.setdefault(sig, {"label": label, "uses": []})["uses"].append({"model": key, "table": t["name"]})
        for r in m["relationships"]:
            pair = tuple(sorted([norm_name(r["fromTable"]), norm_name(r["toTable"])]))
            rels[pair].append({"model": key, "from": f"{r['fromTable']}[{r['fromColumn']}]",
                               "to": f"{r['toTable']}[{r['toColumn']}]", "active": r["isActive"],
                               "direction": r["crossFilteringBehavior"], "cardinality": r["cardinality"]})

    conflicts, identical = [], []
    for k, items in sorted(measures.items()):
        mods = {i["model"] for i in items}
        if len(mods) < 2:
            continue
        variants: Dict[str, dict] = {}
        for i in items:
            v = variants.setdefault(i["norm"], {"expression": i["expression"], "models": [], "tables": [],
                                                "descriptions": [], "formats": []})
            v["models"].append(i["model"])
            v["tables"].append(i["table"])
            if i["description"]:
                v["descriptions"].append(i["description"])
            if i["formatString"]:
                v["formats"].append(i["formatString"])
        entry = {"name": items[0]["name"], "models": sorted(mods),
                 "variants": sorted(variants.values(), key=lambda v: -len(v["models"]))}
        (identical if len(variants) == 1 else conflicts).append(entry)

    shared_tables = []
    for k, items in sorted(tables.items()):
        mods = {i["model"] for i in items}
        if len(mods) < 2:
            continue
        allcols = set().union(*[set(i["columns"]) for i in items])
        diff = {i["model"]: sorted(allcols - set(i["columns"])) for i in items}
        shared_tables.append({"name": items[0]["name"], "models": sorted(mods),
                              "roles": {i["model"]: i["role"] for i in items},
                              "sameColumns": all(not v for v in diff.values()),
                              "missingColumns": {k2: v for k2, v in diff.items() if v}})

    shared_sources = [{"label": v["label"], "uses": v["uses"]} for v in sources.values()
                      if len({u["model"] for u in v["uses"]}) >= 2]
    shared_sources.sort(key=lambda s: (-len(s["uses"]), s["label"].lower()))

    rel_diffs = []
    for pair, items in rels.items():
        if len({i["model"] for i in items}) < 2:
            continue
        keys = {(i["from"].lower(), i["to"].lower()) for i in items}
        dirs = {i["direction"] for i in items}
        if len(keys) > 1 or len(dirs) > 1:
            rel_diffs.append({"tables": list(pair), "variants": items})

    return {
        "models": models_out,
        "measureConflicts": conflicts,
        "sharedMeasures": identical,
        "sharedTables": shared_tables,
        "sharedSources": shared_sources,
        "relationshipDifferences": rel_diffs,
        "totals": {
            "models": len(models_out),
            "reports": sum(len(m["reports"]) for m in models_out),
            "pages": sum(m["pages"] for m in models_out),
            "tables": sum(m["stats"]["tables"] for m in models_out),
            "measures": sum(m["stats"]["measures"] for m in models_out),
        },
    }


def collect_units(packs: List[dict]) -> List[dict]:
    """Turn [{"pack", "enrichment", "run"}] into one unit per semantic model, with unique keys."""
    units, used = [], defaultdict(int)
    for p in packs:
        pack = p["pack"]
        for m in pack["semanticModels"]:
            name = m["name"]
            used[name] += 1
            key = name if used[name] == 1 else f"{name} ({used[name]})"
            reports = [r for r in pack["reports"] if r.get("semanticModel") == m["name"]]
            units.append({"key": key, "model": m, "reports": reports, "pack": pack,
                          "enrichment": p.get("enrichment"), "run": p.get("run")})
    return units
