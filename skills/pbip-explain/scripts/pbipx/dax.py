"""DAX reference extraction.

We do not need a full DAX parser to document a model. We need to know, for every
expression, which tables, columns and measures it touches, which functions it
uses and whether it activates inactive relationships (USERELATIONSHIP).

Approach: blank out comments and string literals (keeping offsets), then scan
with one tokenizer regex. References are resolved against the model catalogue.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple

_TOKEN = re.compile(
    r"""
      (?P<qtable>'(?:[^']|'')*')\s*(?P<qcol>\[(?:[^\]]|\]\])*\])?
    | (?P<ident>[A-Za-z_][A-Za-z0-9_.]*)(?P<after>\s*(?:\[(?:[^\]]|\]\])*\]|\()?)
    | (?P<bare>\[(?:[^\]]|\]\])*\])
    """,
    re.X,
)

KEYWORDS = {
    "VAR", "RETURN", "IN", "NOT", "AND", "OR", "TRUE", "FALSE", "ASC", "DESC",
    "DEFINE", "EVALUATE", "ORDER", "BY", "START", "AT", "MEASURE", "COLUMN", "TABLE",
    "BLANK", "DATATABLE", "STRING", "INTEGER", "DOUBLE", "CURRENCY", "BOOLEAN", "DATETIME",
    "SKIP", "DENSE", "REL", "ABS", "ASC", "DAY", "MONTH", "QUARTER", "YEAR", "WEEK",
    "HOUR", "MINUTE", "SECOND", "BOTH", "NONE", "ONEWAY", "ONEWAY_LEFTFILTERS",
    "ONEWAY_RIGHTFILTERS", "INT64", "DECIMAL", "ANYVAL", "SCALAR", "ANYREF", "EXPR", "VAL",
}

ITERATORS = {
    "SUMX", "AVERAGEX", "MINX", "MAXX", "COUNTX", "COUNTAX", "PRODUCTX", "CONCATENATEX",
    "RANKX", "FILTER", "ADDCOLUMNS", "SELECTCOLUMNS", "GENERATE", "GENERATEALL",
    "MEDIANX", "PERCENTILEX.INC", "PERCENTILEX.EXC", "STDEVX.P", "STDEVX.S", "VARX.P", "VARX.S",
}
TIME_INTELLIGENCE = {
    "DATESYTD", "DATESMTD", "DATESQTD", "TOTALYTD", "TOTALMTD", "TOTALQTD", "SAMEPERIODLASTYEAR",
    "DATEADD", "DATESINPERIOD", "DATESBETWEEN", "PARALLELPERIOD", "PREVIOUSMONTH", "PREVIOUSYEAR",
    "PREVIOUSQUARTER", "PREVIOUSDAY", "NEXTMONTH", "NEXTYEAR", "NEXTQUARTER", "NEXTDAY",
    "OPENINGBALANCEMONTH", "CLOSINGBALANCEMONTH", "OPENINGBALANCEYEAR", "CLOSINGBALANCEYEAR",
    "STARTOFMONTH", "ENDOFMONTH", "STARTOFYEAR", "ENDOFYEAR", "FIRSTDATE", "LASTDATE",
}
FILTER_MODIFIERS = {
    "ALL", "ALLEXCEPT", "ALLSELECTED", "ALLNOBLANKROW", "REMOVEFILTERS", "KEEPFILTERS",
    "USERELATIONSHIP", "CROSSFILTER", "TREATAS",
}


def blank_out(expr: str) -> str:
    """Replace comments and string literal contents with spaces (offsets preserved)."""
    out = list(expr)
    i, n = 0, len(expr)
    while i < n:
        ch = expr[i]
        two = expr[i:i + 2]
        if two == "//" or two == "--":
            j = expr.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif two == "/*":
            j = expr.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif ch == '"':
            j = i + 1
            while j < n:
                if expr[j] == '"':
                    if j + 1 < n and expr[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            for k in range(i + 1, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j + 1
        elif ch == "'":
            # quoted table name - skip over it untouched so '' inside is not misread
            j = i + 1
            while j < n:
                if expr[j] == "'":
                    if j + 1 < n and expr[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
        else:
            i += 1
    return "".join(out)


def _bracket(tok: str) -> str:
    return tok.strip()[1:-1].replace("]]", "]")


def _qname(tok: str) -> str:
    return tok.strip()[1:-1].replace("''", "'")


class Catalog:
    """Name lookup for the semantic model (case-insensitive like DAX)."""

    def __init__(self, tables: List[dict]):
        self.tables: Dict[str, str] = {}
        self.columns: Dict[str, Dict[str, str]] = {}
        self.measures: Dict[str, Tuple[str, str]] = {}
        self.functions: Set[str] = set()
        for t in tables:
            tl = t["name"].lower()
            self.tables[tl] = t["name"]
            self.columns[tl] = {c["name"].lower(): c["name"] for c in t.get("columns", [])}
            for m in t.get("measures", []):
                self.measures[m["name"].lower()] = (t["name"], m["name"])
        self.column_index: Dict[str, List[Tuple[str, str]]] = {}
        for tl, cols in self.columns.items():
            for cl, cn in cols.items():
                self.column_index.setdefault(cl, []).append((self.tables[tl], cn))

    def table(self, name: str) -> Optional[str]:
        return self.tables.get(name.lower())

    def column(self, table: str, col: str) -> Optional[Tuple[str, str]]:
        t = self.tables.get(table.lower())
        if not t:
            return None
        c = self.columns[table.lower()].get(col.lower())
        return (t, c) if c else None

    def measure(self, name: str) -> Optional[Tuple[str, str]]:
        return self.measures.get(name.lower())


def analyse(expr: Optional[str], cat: Catalog, home_table: Optional[str] = None) -> dict:
    """Return the references and features of one DAX expression."""
    res = {
        "measures": [], "columns": [], "tables": [], "functions": [],
        "userelationship": [], "unresolved": [], "variables": [],
    }
    if not expr:
        return res
    clean = blank_out(expr)
    variables = {v.upper() for v in re.findall(r"\bVAR\s+([A-Za-z_][A-Za-z0-9_]*)", clean, re.I)}
    res["variables"] = sorted(variables)
    measures: List[str] = []
    columns: List[Tuple[str, str]] = []
    tables: List[str] = []
    funcs: List[str] = []
    unresolved: List[str] = []

    def add(lst, v):
        if v not in lst:
            lst.append(v)

    for m in _TOKEN.finditer(clean):
        if m.group("qtable"):
            tname = _qname(m.group("qtable"))
            col = m.group("qcol")
            if col:
                _resolve_qualified(tname, _bracket(col), cat, measures, columns, unresolved, add)
            else:
                t = cat.table(tname)
                if t:
                    add(tables, t)
                else:
                    add(unresolved, "'" + tname + "'")
        elif m.group("ident"):
            ident = m.group("ident")
            after = (m.group("after") or "").strip()
            if after.startswith("("):
                add(funcs, ident.upper())
            elif after.startswith("["):
                _resolve_qualified(ident, _bracket(after), cat, measures, columns, unresolved, add)
            else:
                up = ident.upper()
                if up in KEYWORDS or up in variables:
                    continue
                t = cat.table(ident)
                if t:
                    add(tables, t)
        elif m.group("bare"):
            name = _bracket(m.group("bare"))
            mm = cat.measure(name)
            if mm:
                add(measures, mm[1])
                continue
            if home_table:
                c = cat.column(home_table, name)
                if c:
                    add(columns, c)
                    continue
            cands = cat.column_index.get(name.lower(), [])
            if len(cands) == 1:
                add(columns, cands[0])
            else:
                add(unresolved, "[" + name + "]")

    for um in re.finditer(r"USERELATIONSHIP\s*\(", clean, re.I):
        inner = _balanced(clean, um.end() - 1)
        refs = re.findall(r"('(?:[^']|'')*'|[A-Za-z_][A-Za-z0-9_]*)\s*\[((?:[^\]]|\]\])*)\]", inner)
        if len(refs) >= 2:
            a = (_maybe_q(refs[0][0]), refs[0][1].replace("]]", "]"))
            b = (_maybe_q(refs[1][0]), refs[1][1].replace("]]", "]"))
            ca = cat.column(*a) or a
            cb = cat.column(*b) or b
            res["userelationship"].append([list(ca), list(cb)])

    res["measures"] = measures
    res["columns"] = [list(c) for c in columns]
    res["tables"] = tables
    res["functions"] = funcs
    res["unresolved"] = unresolved
    return res


def _maybe_q(s: str) -> str:
    s = s.strip()
    return _qname(s) if s.startswith("'") else s


def _balanced(text: str, open_idx: int) -> str:
    depth = 0
    for k in range(open_idx, len(text)):
        if text[k] == "(":
            depth += 1
        elif text[k] == ")":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1:k]
    return text[open_idx + 1:]


def _resolve_qualified(tname, cname, cat: Catalog, measures, columns, unresolved, add):
    c = cat.column(tname, cname)
    if c:
        add(columns, c)
        return
    mm = cat.measure(cname)
    if mm:
        add(measures, mm[1])
        return
    add(unresolved, tname + "[" + cname + "]")


def complexity(expr: Optional[str], refs: dict) -> dict:
    if not expr:
        return {"lines": 0, "functions": 0, "level": "empty"}
    clean = blank_out(expr)
    lines = len([l for l in expr.splitlines() if l.strip()])
    funcs = refs.get("functions", [])
    depth = max_depth = 0
    for ch in clean:
        if ch == "(":
            depth += 1
            max_depth = max(max_depth, depth)
        elif ch == ")":
            depth -= 1
    calls = len(re.findall(r"[A-Za-z_][A-Za-z0-9_.]*\s*\(", clean))
    score = calls + max_depth * 2 + len(refs.get("measures", [])) + (5 if refs.get("variables") else 0)
    level = "simple" if score <= 6 else "moderate" if score <= 20 else "complex"
    return {
        "lines": lines,
        "functionCalls": calls,
        "nesting": max_depth,
        "level": level,
        "usesIterators": sorted(set(funcs) & ITERATORS),
        "usesTimeIntelligence": sorted(set(funcs) & TIME_INTELLIGENCE),
        "usesFilterModifiers": sorted(set(funcs) & FILTER_MODIFIERS),
        "usesCalculate": "CALCULATE" in funcs or "CALCULATETABLE" in funcs,
    }


# ---------------------------------------------------------------------------
# Pattern-based plain-English description for simple measures.
# Labelled "auto" in the docs; anything non-trivial is left for human/agent enrichment.

_AGG = {
    "SUM": "Sum of {0}", "AVERAGE": "Average of {0}", "MIN": "Smallest value of {0}",
    "MAX": "Largest value of {0}", "COUNT": "Number of non-blank values in {0}",
    "COUNTA": "Number of non-blank values in {0}", "DISTINCTCOUNT": "Number of distinct values of {0}",
    "DISTINCTCOUNTNOBLANK": "Number of distinct non-blank values of {0}",
    "COUNTROWS": "Number of rows in {0}", "COUNTBLANK": "Number of blank values in {0}",
    "MEDIAN": "Median of {0}", "SELECTEDVALUE": "The single selected value of {0}",
}


def auto_describe(expr: Optional[str]) -> Optional[str]:
    if not expr:
        return None
    clean = blank_out(expr).strip()
    if "\n" in clean.strip() and len(clean.splitlines()) > 3:
        return None
    s = re.sub(r"\s+", " ", expr.strip())
    return _describe(s, 0)


def _ref_text(s: str) -> Optional[str]:
    s = s.strip()
    if re.fullmatch(r"\[(?:[^\]]|\]\])+\]", s):
        return s
    if re.fullmatch(r"('(?:[^']|'')*'|[A-Za-z_][A-Za-z0-9_]*)\s*\[(?:[^\]]|\]\])+\]", s):
        return s
    if re.fullmatch(r"'(?:[^']|'')*'|[A-Za-z_][A-Za-z0-9_]*", s):
        return s
    return None


def _split_args(s: str) -> List[str]:
    args, depth, buf, q = [], 0, "", None
    for ch in s:
        if q:
            buf += ch
            if ch == q:
                q = None
            continue
        if ch in "\"'":
            q = ch
            buf += ch
        elif ch in "([{":
            depth += 1
            buf += ch
        elif ch in ")]}":
            depth -= 1
            buf += ch
        elif ch == "," and depth == 0:
            args.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        args.append(buf.strip())
    return args


def _describe(s: str, depth: int) -> Optional[str]:
    if depth > 3:
        return None
    s = s.strip()
    r = _ref_text(s)
    if r:
        return r
    m = re.fullmatch(r"([A-Z][A-Z0-9.]*)\s*\((.*)\)", s, re.S)
    if not m:
        bm = re.fullmatch(r"(\[[^\]]+\])\s*([-+*/])\s*(\[[^\]]+\])", s)
        if bm:
            op = {"-": "minus", "+": "plus", "*": "multiplied by", "/": "divided by"}[bm.group(2)]
            return f"{bm.group(1)} {op} {bm.group(3)}"
        return None
    fn, inner = m.group(1).upper(), m.group(2)
    args = _split_args(inner)
    if fn in _AGG and len(args) == 1 and _ref_text(args[0]):
        return _AGG[fn].format(args[0])
    if fn == "DIVIDE" and len(args) in (2, 3):
        a, b = _describe(args[0], depth + 1), _describe(args[1], depth + 1)
        if a and b:
            return f"{a} divided by {b} (blank when the divisor is zero)"
    if fn == "CALCULATE" and args:
        base = _describe(args[0], depth + 1)
        if base and len(args) > 1:
            filt = "; ".join(a for a in args[1:])
            if len(filt) <= 160:
                return f"{base}, evaluated with filter: {filt}"
        if base and len(args) == 1:
            return f"{base} (with context transition)"
    if fn in ("SUMX", "AVERAGEX") and len(args) == 2 and _ref_text(args[0]):
        verb = "Sum" if fn == "SUMX" else "Average"
        if len(args[1]) <= 100:
            return f"{verb} over each row of {args[0]} of: {args[1]}"
    return None
