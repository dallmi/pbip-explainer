"""Power Query (M) source and transformation extraction.

Pulls out what a reader needs to understand where a table's data comes from:
connector, server / database / path arguments, navigation (catalog, schema, table),
native SQL, the applied steps, joins with their keys, row filters, and which
other queries or parameters it references.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

CONNECTORS: Dict[str, str] = {
    "Sql.Database": "SQL Server / Azure SQL",
    "Sql.Databases": "SQL Server / Azure SQL",
    "Databricks.Catalogs": "Databricks",
    "Databricks.Contents": "Databricks",
    "Databricks.Query": "Databricks",
    "DatabricksMultiCloud.Catalogs": "Databricks",
    "AzureDatabricks.Catalogs": "Databricks",
    "Snowflake.Databases": "Snowflake",
    "PostgreSQL.Database": "PostgreSQL",
    "MySQL.Database": "MySQL",
    "Oracle.Database": "Oracle",
    "Teradata.Database": "Teradata",
    "Odbc.DataSource": "ODBC",
    "Odbc.Query": "ODBC",
    "OleDb.DataSource": "OLE DB",
    "AnalysisServices.Database": "Analysis Services",
    "AnalysisServices.Databases": "Analysis Services",
    "AzureDataExplorer.Contents": "Azure Data Explorer (Kusto)",
    "Kusto.Contents": "Azure Data Explorer (Kusto)",
    "AzureStorage.Blobs": "Azure Blob Storage",
    "AzureStorage.DataLake": "Azure Data Lake Storage",
    "AzureStorage.Tables": "Azure Table Storage",
    "SharePoint.Files": "SharePoint folder",
    "SharePoint.Contents": "SharePoint",
    "SharePoint.Tables": "SharePoint list",
    "OData.Feed": "OData",
    "Web.Contents": "Web / REST API",
    "Web.Page": "Web page",
    "File.Contents": "File",
    "Folder.Files": "Folder",
    "Folder.Contents": "Folder",
    "Excel.Workbook": "Excel",
    "Excel.CurrentWorkbook": "Excel",
    "Csv.Document": "CSV",
    "Parquet.Document": "Parquet",
    "Json.Document": "JSON",
    "Xml.Tables": "XML",
    "Xml.Document": "XML",
    "PowerBI.Dataflows": "Power BI dataflow",
    "PowerPlatform.Dataflows": "Power Platform dataflow",
    "Lakehouse.Contents": "Fabric Lakehouse",
    "Fabric.Warehouse": "Fabric Warehouse",
    "Salesforce.Data": "Salesforce",
    "Salesforce.Reports": "Salesforce",
    "GoogleAnalytics.Accounts": "Google Analytics",
    "ApplicationInsights.Query": "Application Insights",
    "Exchange.Contents": "Exchange",
    "ActiveDirectory.Domains": "Active Directory",
    "Dynamics365BusinessCentral.ApiContentsWithOptions": "Dynamics 365",
    "CommonDataService.Database": "Dataverse",
    "Cds.Entities": "Dataverse",
    "Table.FromRows": "Embedded data (Enter data)",
}

_FUNC_CALL = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:\.[A-Z][A-Za-z0-9]*)+)\s*\(")
_STRING = re.compile(r'"((?:[^"]|"")*)"')
_STEP = re.compile(r'^\s*(#"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$')


def _strip_comments(m: str) -> str:
    out, i, n = [], 0, len(m)
    while i < n:
        if m.startswith("//", i):
            j = m.find("\n", i)
            i = n if j < 0 else j
        elif m.startswith("/*", i):
            j = m.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif m[i] == '"':
            j = i + 1
            while j < n:
                if m[j] == '"':
                    if j + 1 < n and m[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            out.append(m[i:j + 1])
            i = j + 1
        else:
            out.append(m[i])
            i += 1
    return "".join(out)


def _balanced_args(text: str, open_idx: int) -> str:
    depth, q = 0, False
    for k in range(open_idx, len(text)):
        ch = text[k]
        if ch == '"':
            q = not q
        if q:
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:k]
    return text[open_idx + 1:]


def _split_top(s: str) -> List[str]:
    args, depth, buf, q = [], 0, "", False
    for ch in s:
        if ch == '"':
            q = not q
        if not q:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif ch == "," and depth == 0:
                args.append(buf.strip())
                buf = ""
                continue
        buf += ch
    if buf.strip():
        args.append(buf.strip())
    return args


def _lit(arg: str) -> str:
    a = arg.strip()
    m = _STRING.fullmatch(a)
    if m:
        return m.group(1).replace('""', '"')
    return a


def _unstep(name: str) -> str:
    name = name.strip()
    if name.startswith('#"'):
        return name[2:-1].replace('""', '"')
    return name


def analyse(m: Optional[str], known_queries: Optional[List[str]] = None) -> dict:
    res = {"sources": [], "navigation": [], "nativeQueries": [], "steps": [], "joins": [],
           "rowFilters": [], "references": [], "embeddedData": False}
    if not m:
        return res
    text = _strip_comments(m)

    for fm in _FUNC_CALL.finditer(text):
        fn = fm.group(1)
        if fn not in CONNECTORS:
            continue
        args = _split_top(_balanced_args(text, fm.end() - 1))
        src = {"function": fn, "kind": CONNECTORS[fn], "arguments": [_lit(a) for a in args[:3] if not a.startswith("[")]}
        opts = [a for a in args if a.startswith("[")]
        if opts:
            q = re.search(r'Query\s*=\s*"((?:[^"]|"")*)"', opts[0])
            if q:
                res["nativeQueries"].append(q.group(1).replace('""', '"'))
        # File.Contents inside Csv.Document etc.: keep both, the outer tells the format
        res["sources"].append(src)
    if re.search(r"Binary\.Decompress\s*\(\s*Binary\.FromText", text):
        res["embeddedData"] = True
        res["sources"].append({"function": "Binary.Decompress", "kind": "Embedded data (Enter data)", "arguments": []})

    for nm in re.finditer(r"\{\s*\[([^\]]*)\]\s*\}\s*\[\s*(Data|Content)\s*\]", text):
        rec = dict(re.findall(r'(\w+)\s*=\s*"((?:[^"]|"")*)"', nm.group(1)))
        if rec:
            res["navigation"].append(rec)

    for qm in re.finditer(r"Value\.NativeQuery\s*\(", text):
        args = _split_top(_balanced_args(text, qm.end() - 1))
        if len(args) >= 2:
            res["nativeQueries"].append(_lit(args[1]))

    # applied steps of the outermost let ... in
    body = text
    lm = re.search(r"\blet\b(.*)\bin\b\s*(.*)$", text, re.S)
    if lm:
        body = lm.group(1)
    for line in _split_steps(body):
        sm = _STEP.match(line)
        if not sm:
            continue
        step, rhs = _unstep(sm.group(1)), sm.group(2).strip()
        fm = re.match(r"([A-Z][A-Za-z0-9]*(?:\.[A-Za-z0-9]+)+)\s*\(", rhs)
        res["steps"].append({"name": step, "function": fm.group(1) if fm else None})
        if fm and fm.group(1) in ("Table.NestedJoin", "Table.Join", "Table.FuzzyNestedJoin"):
            args = _split_top(_balanced_args(rhs, fm.end() - 1))
            if len(args) >= 4:
                kind = next((a for a in args[4:] if "JoinKind" in a), "JoinKind.LeftOuter (default)")
                res["joins"].append({
                    "step": step, "function": fm.group(1),
                    "left": _unstep(args[0]), "leftKeys": [_lit(x) for x in _split_top(args[1].strip("{} "))],
                    "right": _unstep(args[2]), "rightKeys": [_lit(x) for x in _split_top(args[3].strip("{} "))],
                    "kind": kind.strip(),
                })
        if fm and fm.group(1) == "Table.SelectRows":
            args = _split_top(_balanced_args(rhs, fm.end() - 1))
            if len(args) >= 2:
                cond = re.sub(r"\s+", " ", args[1]).strip()
                res["rowFilters"].append({"step": step, "condition": cond[:400]})

    if known_queries:
        refs = []
        for q in known_queries:
            pat = r'#"' + re.escape(q.replace('"', '""')) + r'"' if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", q) \
                else r"(?<![\w.#\"])" + re.escape(q) + r"(?![\w\"])"
            if re.search(pat, text):
                refs.append(q)
        res["references"] = refs
    return res


def _split_steps(body: str) -> List[str]:
    """Split the let-body on top-level commas into 'Step = expr' strings."""
    steps, depth, buf, q = [], 0, "", False
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == '"':
            q = not q
        if not q:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif ch == "," and depth == 0:
                steps.append(buf.strip())
                buf = ""
                i += 1
                continue
        buf += ch
        i += 1
    if buf.strip():
        steps.append(buf.strip())
    return [re.sub(r"\s+", " ", s) for s in steps]


def is_parameter(expr: Optional[str]) -> bool:
    return bool(expr and re.search(r"meta\s*\[[^\]]*IsParameterQuery\s*=\s*true", expr, re.I))


def parameter_info(expr: str) -> dict:
    info = {"value": None, "type": None, "required": None, "allowedValues": None}
    vm = re.match(r'\s*("(?:[^"]|"")*"|[^\s]+)\s+meta', expr)
    if vm:
        info["value"] = _lit(vm.group(1))
    tm = re.search(r'Type\s*=\s*"([^"]+)"', expr)
    if tm:
        info["type"] = tm.group(1)
    rm = re.search(r"IsParameterQueryRequired\s*=\s*(true|false)", expr, re.I)
    if rm:
        info["required"] = rm.group(1).lower() == "true"
    am = re.search(r"List\s*=\s*\{([^}]*)\}", expr)
    if am:
        info["allowedValues"] = [_lit(x) for x in _split_top(am.group(1))]
    return info
