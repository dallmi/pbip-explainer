"""Build the knowledge pack: zip/folder -> knowledge/model.json + CSVs + agent-brief.md."""
from __future__ import annotations

import csv
import datetime as _dt
import json
import shutil
from pathlib import Path
from typing import List, Optional

from . import KNOWLEDGE_SCHEMA_VERSION, __version__
from .analysis import ROLE_LABELS, analyse, finding_impact_text
from .model import load_semantic_model
from .report import load_report, observed_model
from .unpack import discover, safe_extract


def extract(source: Path, out: Path) -> Path:
    """Unpack (if zip), parse, analyse and write the knowledge pack. Returns path to model.json."""
    source = source.resolve()
    out.mkdir(parents=True, exist_ok=True)
    skipped: List[str] = []
    if source.is_file() and source.suffix.lower() == ".zip":
        input_dir = out / "00-input"
        if input_dir.exists():
            shutil.rmtree(input_dir)
        skipped = safe_extract(source, input_dir)
        root = input_dir
    elif source.is_file() and source.suffix.lower() == ".pbip":
        root = source.parent
    elif source.is_dir():
        root = source
    else:
        raise SystemExit(f"Not a .zip, .pbip or folder: {source}")

    found = discover(root)
    if not found["models"] and not found["reports"]:
        raise SystemExit("No *.SemanticModel or *.Report folder found. Save the file as PBIP in Power BI Desktop "
                         "(File > Save as > Power BI project) and zip the whole project folder.")

    models = []
    for mf in found["models"]:
        m = load_semantic_model(mf)
        if m:
            m["folder"] = mf.relative_to(root).as_posix()
            models.append(m)
    reports = []
    for rf in found["reports"]:
        r = load_report(rf)
        if r:
            r["folder"] = rf.relative_to(root).as_posix()
            reports.append(r)

    # Pair reports with models (byPath), fall back to the single model.
    for r in reports:
        ref = r.get("semanticModelRef") or {}
        target = None
        if ref.get("type") == "byPath" and ref.get("path"):
            tname = Path(ref["path"]).name
            target = next((m for m in models if Path(m["folder"]).name == tname), None)
        if target is None and len(models) == 1 and ref.get("type") != "byConnection":
            target = models[0]
        r["semanticModel"] = target["name"] if target else None

    for r in reports:
        if not r.get("semanticModel"):
            r["observedModel"] = observed_model(r)
    for m in models:
        linked = [r for r in reports if r.get("semanticModel") == m["name"]]
        m["analysis"] = analyse(m, linked)
        m["reports"] = [r["name"] for r in linked]

    pack = {
        "schemaVersion": KNOWLEDGE_SCHEMA_VERSION,
        "generator": f"pbipx {__version__}",
        "generatedAt": _dt.datetime.now().isoformat(timespec="seconds"),
        "source": {"name": source.name, "pbipFiles": found["pbip"], "skippedFiles": skipped},
        "semanticModels": models,
        "reports": reports,
    }
    kdir = out / "knowledge"
    kdir.mkdir(exist_ok=True)
    mpath = kdir / "model.json"
    mpath.write_text(json.dumps(pack, indent=1, ensure_ascii=False, default=list), encoding="utf-8")
    write_csvs(pack, kdir)
    (kdir / "agent-brief.md").write_text(agent_brief(pack), encoding="utf-8")
    enr = out / "enrichment.json"
    if not enr.exists():
        enr.write_text(json.dumps(enrichment_template(pack), indent=2, ensure_ascii=False), encoding="utf-8")
    return mpath


def load_pack(path: Path) -> dict:
    p = Path(path)
    if p.is_dir():
        p = p / "knowledge" / "model.json" if (p / "knowledge").is_dir() else p / "model.json"
    return json.loads(p.read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------ CSV

def _w(path: Path, header: List[str], rows: List[list]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def _pages_of(usages: list) -> str:
    return "; ".join(sorted({u["page"] for u in usages if u.get("page")}))


def write_csvs(pack: dict, kdir: Path) -> None:
    t_rows, c_rows, m_rows, r_rows, v_rows, s_rows = [], [], [], [], [], []
    for m in pack["semanticModels"]:
        for t in m["tables"]:
            if t["isAutoDateTable"]:
                continue
            src = t["source"]
            t_rows.append([m["name"], t["name"], ROLE_LABELS.get(t["role"], t["role"]), t.get("storageMode"),
                           len(t["columns"]), len(t["measures"]), t["isHidden"], src.get("type"),
                           "; ".join(sorted({c["kind"] for c in src["connectors"]})), t.get("description") or ""])
            for c in t["columns"]:
                c_rows.append([m["name"], t["name"], c["name"], c.get("dataType"), c["columnType"], c["isHidden"],
                               c.get("formatString") or "", c.get("sortByColumn") or "", c.get("sourceColumn") or "",
                               c.get("expression") or "", c.get("description") or "", c["usedInReport"],
                               _pages_of(c["usedBy"])])
            for me in t["measures"]:
                m_rows.append([m["name"], t["name"], me["name"], me.get("displayFolder") or "",
                               me.get("formatString") or "", me["isHidden"], me.get("description") or "",
                               me.get("autoDescription") or "", me["expression"] or "",
                               "; ".join(me["dependsOn"]["measures"]),
                               "; ".join(f"{a}[{b}]" for a, b in me["dependsOn"]["columns"]),
                               me["complexity"]["level"], me["usedInReport"], _pages_of(me["usedBy"])])
            for p in t["partitions"]:
                ma = p.get("mAnalysis") or {}
                for s in ma.get("sources", []):
                    s_rows.append([m["name"], t["name"], s["kind"], s["function"], " | ".join(map(str, s["arguments"]))])
        for r in m["relationships"]:
            r_rows.append([m["name"], r["fromTable"], r["fromColumn"], r["toTable"], r["toColumn"], r["cardinality"],
                           r["crossFilteringBehavior"], r["isActive"], r.get("activatedByUserelationship")])
    for rep in pack["reports"]:
        for p in rep["pages"]:
            for v in p["visuals"]:
                if v["decorative"] and not v["fields"]:
                    continue
                v_rows.append([rep["name"], p["displayName"], p["hidden"], v["name"], v["typeLabel"], v["title"] or "",
                               "; ".join(f"{f.get('role')}: {f.get('table')}[{f.get('name')}]" for f in v["fields"]
                                         if f.get("role") not in ("formatting", "sort")),
                               (v.get("text") or "").replace("\n", " / ")])
    _w(kdir / "tables.csv", ["model", "table", "role", "storage_mode", "columns", "measures", "hidden", "source_type",
                             "connectors", "description"], t_rows)
    _w(kdir / "columns.csv", ["model", "table", "column", "data_type", "column_type", "hidden", "format", "sort_by",
                              "source_column", "dax_expression", "description", "used_in_report", "pages"], c_rows)
    _w(kdir / "measures.csv", ["model", "table", "measure", "display_folder", "format", "hidden", "description",
                               "auto_description", "dax", "depends_on_measures", "depends_on_columns", "complexity",
                               "used_in_report", "pages"], m_rows)
    _w(kdir / "relationships.csv", ["model", "from_table", "from_column", "to_table", "to_column", "cardinality",
                                    "cross_filter", "active", "activated_by_userelationship"], r_rows)
    _w(kdir / "visuals.csv", ["report", "page", "page_hidden", "visual_id", "visual_type", "title", "fields", "text"], v_rows)
    _w(kdir / "sources.csv", ["model", "table", "source_kind", "m_function", "arguments"], s_rows)


# ------------------------------------------------------------------------------ agent brief

def _short(expr: Optional[str], n: int = 160) -> str:
    if not expr:
        return ""
    s = " ".join(expr.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def agent_brief(pack: dict) -> str:
    """Compact, LLM-oriented overview. Complete facts live in model.json and documentation.md."""
    L: List[str] = []
    a = L.append
    a(f"# Knowledge pack: {pack['source']['name']}")
    a("")
    a(f"Generated {pack['generatedAt']} by {pack['generator']} (schema {pack['schemaVersion']}).")
    a("")
    a("## How to use this pack")
    a("")
    a("- This file is the overview. `model.json` holds every extracted fact; the CSVs are flat views of it.")
    a("- A PBIP contains **metadata only, no data**. Never state data values, row counts or totals from this pack.")
    a("- Facts here come from the files. Descriptions marked *auto* are pattern-generated; anything marked "
      "*AI-drafted* in the docs needs review. Say which kind of statement you make.")
    a("- 'Unused' means not used by the report(s) in this package; a shared model may serve other reports.")
    a("")
    for m in pack["semanticModels"]:
        st = m["analysis"]["stats"]
        a(f"## Semantic model: {m['name']}")
        a("")
        a(f"Format {m['format'].upper()}, compatibility level {m.get('compatibilityLevel')}, culture {m.get('culture')}. "
          f"{st['tables']} tables, {st['columns']} columns ({st['calculatedColumns']} calculated), {st['measures']} measures, "
          f"{st['relationships']} relationships, {st['roles']} RLS roles. Used by report(s): {', '.join(m['reports']) or 'none in package'}.")
        a("")
        a("### Tables")
        a("")
        a("| Table | Role | Mode | Source | Cols | Measures | Description |")
        a("|---|---|---|---|---|---|---|")
        for t in m["tables"]:
            if t["isAutoDateTable"]:
                continue
            src = t["source"]
            kinds = ", ".join(sorted({c["kind"] for c in src["connectors"]})) or (src.get("type") or "")
            nav = "; ".join("/".join(str(v) for v in n.values()) for n in src["navigation"][:2])
            a(f"| {t['name']} | {ROLE_LABELS.get(t['role'], t['role'])} | {t.get('storageMode') or ''} | "
              f"{kinds}{(' — ' + nav) if nav else ''} | {len(t['columns'])} | {len(t['measures'])} | {_short(t.get('description'), 100)} |")
        a("")
        a("### Relationships (join keys)")
        a("")
        for r in m["relationships"]:
            flags = []
            if not r["isActive"]:
                flags.append("INACTIVE" + (" (used via USERELATIONSHIP)" if r["activatedByUserelationship"] else ""))
            if r["crossFilteringBehavior"] == "bothDirections":
                flags.append("bi-directional")
            a(f"- {r['fromTable']}[{r['fromColumn']}] ({r['fromCardinality']}) → {r['toTable']}[{r['toColumn']}] "
              f"({r['toCardinality']}){' — ' + ', '.join(flags) if flags else ''}")
        a("")
        a("### Measures")
        a("")
        for t in m["tables"]:
            if not t["measures"]:
                continue
            a(f"**{t['name']}**")
            a("")
            for me in t["measures"]:
                desc = me.get("description") or (("auto: " + me["autoDescription"]) if me.get("autoDescription") else "")
                ic = (me.get("impact") or {}).get("counts") or {}
                used = (f" · impact: {ic.get('measures', 0)} measures, {ic.get('visuals', 0)} visuals "
                        f"({ic.get('direct', 0)} direct)") if me["usedInReport"] else " · not used in report"
                a(f"- [{me['name']}]{' (hidden)' if me['isHidden'] else ''}{used} = `{_short(me['expression'])}`"
                  f"{(' — ' + _short(desc, 140)) if desc else ''}")
            a("")
        cgs = [t for t in m["tables"] if t.get("calculationGroup")]
        if cgs:
            a("### Calculation groups")
            a("")
            for t in cgs:
                a(f"- {t['name']}: " + ", ".join(i["name"] for i in t["calculationGroup"]["items"]))
            a("")
        if m["roles"]:
            a("### Row-level security")
            a("")
            for r in m["roles"]:
                for tp in r["tablePermissions"]:
                    a(f"- Role {r['name']}: {tp['table']} filtered by `{_short(tp['filterExpression'])}`")
            a("")
        params = [e for e in m["expressions"] if e.get("isParameter")]
        if params:
            a("### Parameters")
            a("")
            for e in params:
                a(f"- {e['name']} = {(e.get('parameter') or {}).get('value')}")
            a("")
        fs = m["analysis"]["findings"]
        if fs:
            a("### Findings")
            a("")
            for f in fs:
                pr = f"{f['priority']} priority, " if f.get("priority") else ""
                imp = finding_impact_text(f)
                a(f"- **{pr}{f['severity']}** {f['message']}" + (f" {imp}" if imp else ""))
            a("")
    for rep in pack["reports"]:
        om = rep.get("observedModel")
        if om:
            a(f"## Observed model of report {rep['name']} (live connection, model not in package)")
            a("")
            for t in om["tables"]:
                cols = ", ".join(e["name"] for e in t["columns"])
                meas = ", ".join(f"[{e['name']}]" for e in t["measures"])
                a(f"- {t['name']}: " + "; ".join(x for x in (f"columns {cols}" if cols else "", f"measures {meas}" if meas else "") if x))
            a("")
        if rep.get("reportMeasures"):
            a(f"## Report-level measures of {rep['name']}")
            a("")
            for rm in rep["reportMeasures"]:
                a(f"- [{rm['name']}] = `{_short(rm.get('expression'))}`")
            a("")
        a(f"## Report: {rep['name']}")
        a("")
        a(f"Format {rep['format'].upper()}, semantic model: {rep.get('semanticModel') or (rep.get('semanticModelRef') or {}).get('modelName') or 'unknown'}.")
        if rep["filters"]:
            a("Report-level filters: " + "; ".join(f.get("condition") or f"{(f.get('field') or {}).get('table')}[{(f.get('field') or {}).get('name')}] (no selection)" for f in rep["filters"]))
        a("")
        for p in rep["pages"]:
            tags = []
            if p["hidden"]:
                tags.append("hidden")
            if p["pageType"] != "Standard":
                tags.append(p["pageType"])
            a(f"### Page: {p['displayName']}{' (' + ', '.join(tags) + ')' if tags else ''}")
            a("")
            for fl in p["filters"]:
                if fl.get("condition"):
                    a(f"- Page filter: {fl['condition']}")
            for v in p["visuals"]:
                fields = [f for f in v["fields"] if f.get("role") not in ("formatting", "sort")]
                if v["type"] == "textbox" and v.get("text"):
                    a(f"- Text: \"{_short(v['text'], 160)}\"")
                    continue
                if not fields:
                    continue
                parts = []
                for f in fields:
                    nm = f"[{f['name']}]" if f["kind"] == "measure" else f"{f['table']}[{f['name']}]"
                    if f.get("aggregation"):
                        nm = f"{f['aggregation']} of {nm}"
                    parts.append(f"{f.get('role')}: {nm}")
                a(f"- {v['typeLabel']} \"{v['title'] or v['name']}\" — " + "; ".join(parts))
            a("")
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------------------ enrichment

def enrichment_template(pack: dict) -> dict:
    tmpl = {
        "_instructions": "Fill only what you can support from the extracted facts. Leave a field empty when unsure. "
                         "Text here is shown in the documentation marked as 'AI-drafted, review'. Model descriptions "
                         "always take precedence.",
        "report": {"purpose": "", "audience": "", "howToUse": "", "dataFreshness": "", "owner": ""},
        "tables": {}, "measures": {}, "columns": {}, "pages": {}, "glossary": [], "notes": [],
    }
    for m in pack["semanticModels"]:
        for t in m["tables"]:
            if t["isAutoDateTable"] or t["role"] in ("measureTable",):
                continue
            if not t.get("description"):
                tmpl["tables"][t["name"]] = {"description": "", "grain": ""}
            for me in t["measures"]:
                if not me.get("description") and not me["isHidden"]:
                    tmpl["measures"][me["name"]] = {"description": "", "businessDefinition": ""}
            for c in t["columns"]:
                if not c["isHidden"] and c["usedInReport"] and not c.get("description"):
                    tmpl["columns"][f"{t['name']}[{c['name']}]"] = {"description": ""}
    for rep in pack["reports"]:
        for p in rep["pages"]:
            tmpl["pages"][p["displayName"]] = {"purpose": "", "keyQuestions": []}
    return tmpl
