"""Generic TMDL (Tabular Model Definition Language) parser.

TMDL is indentation based. Every line is one of:

    /// description            -> attaches to the next object declaration
    <kind> [<name>] [= <expr>]  -> object declaration (measure, column, table, annotation ...)
    <property>: <value>         -> property of the enclosing object
    <flag>                      -> boolean property (isHidden, isKey ...)

Multi-line expressions follow a declaration ending in `=`, indented at least two
levels deeper than the declaration, or are fenced in ``` blocks.

The parser does not know the TMDL object catalogue. It builds a neutral tree of
`Node`s; `model.py` gives the tree its meaning. That keeps it tolerant of new
object types that later Power BI versions add.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

PROP_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:(?:\s(.*)|)$")
KIND_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(.*)$")


@dataclass
class Node:
    kind: str
    name: Optional[str] = None
    expr: Optional[str] = None
    props: Dict[str, object] = field(default_factory=dict)
    children: List["Node"] = field(default_factory=list)
    description: Optional[str] = None
    is_ref: bool = False
    line: int = 0
    file: Optional[str] = None

    def child(self, kind: str) -> Optional["Node"]:
        for c in self.children:
            if c.kind == kind:
                return c
        return None

    def all(self, kind: str) -> List["Node"]:
        return [c for c in self.children if c.kind == kind]


def unquote(name: Optional[str]) -> Optional[str]:
    """'Sales Table' -> Sales Table ; 'It''s' -> It's ; "x" -> x"""
    if name is None:
        return None
    s = name.strip()
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        return s[1:-1].replace("''", "'")
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1].replace('""', '"')
    return s


def split_name(rest: str) -> Tuple[Optional[str], str]:
    """Split '<name> = expr' or "'quoted name' = expr" into (name, remainder)."""
    rest = rest.strip()
    if not rest:
        return None, ""
    if rest[0] == "'":
        i = 1
        while i < len(rest):
            if rest[i] == "'":
                if i + 1 < len(rest) and rest[i + 1] == "'":
                    i += 2
                    continue
                break
            i += 1
        return unquote(rest[: i + 1]), rest[i + 1:].strip()
    if rest.startswith("="):
        return None, rest
    m = re.match(r"^(.*?)\s*(=.*)?$", rest)
    name = m.group(1).strip() if m else rest
    remainder = (m.group(2) or "") if m else ""
    return (name or None), remainder.strip()


def parse_object_ref(value: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse `Table.Column`, `'My Table'.'My Col'`, `Table.'Col'` into (table, column)."""
    s = value.strip()
    parts: List[str] = []
    buf = ""
    i = 0
    in_q = False
    while i < len(s):
        ch = s[i]
        if ch == "'":
            if in_q and i + 1 < len(s) and s[i + 1] == "'":
                buf += "''"
                i += 2
                continue
            in_q = not in_q
            buf += ch
        elif ch == "." and not in_q:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
        i += 1
    parts.append(buf)
    if len(parts) == 1:
        return None, unquote(parts[0])
    # A table name could itself contain a dot only when quoted, so the last part is the column.
    return unquote(".".join(parts[:-1])), unquote(parts[-1])


class _Lines:
    def __init__(self, text: str):
        text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
        self.lines = text.split("\n")
        # Detect indentation mode: tabs (Desktop default) or spaces.
        tab_lines = sum(1 for l in self.lines if l.startswith("\t"))
        space_lines = sum(1 for l in self.lines if l.startswith(" "))
        self.tabs = tab_lines >= space_lines
        self.unit = 4
        if not self.tabs:
            widths = sorted({len(l) - len(l.lstrip(" ")) for l in self.lines if l.startswith(" ") and l.strip()})
            if widths:
                self.unit = widths[0]

    def indent(self, line: str) -> int:
        if self.tabs:
            n = 0
            for ch in line:
                if ch == "\t":
                    n += 1
                else:
                    break
            return n
        return (len(line) - len(line.lstrip(" "))) // max(self.unit, 1)

    def strip_indent(self, line: str, levels: int) -> str:
        if self.tabs:
            k = 0
            while k < levels and k < len(line) and line[k] == "\t":
                k += 1
            return line[k:]
        width = levels * self.unit
        k = 0
        while k < width and k < len(line) and line[k] == " ":
            k += 1
        return line[k:]


def _collect_expression(L: _Lines, start: int, decl_indent: int, first: str) -> Tuple[str, int]:
    """Collect an expression that starts after `=` on line `start-1`.

    Returns (expression, next_line_index)."""
    lines = L.lines
    first = first.strip()
    j = start
    if first.startswith("```"):
        body: List[str] = []
        after = first[3:]
        if after.strip().endswith("```"):
            return after.strip()[:-3].strip(), j
        if after.strip():
            body.append(after)
        while j < len(lines):
            if lines[j].strip().startswith("```") or lines[j].strip().endswith("```"):
                tail = lines[j].strip()
                if tail != "```" and tail.endswith("```"):
                    body.append(lines[j].rstrip()[:-3])
                j += 1
                break
            body.append(lines[j])
            j += 1
        return _dedent(L, body), j

    body = [first] if first else []
    cont: List[str] = []
    while j < len(lines):
        ln = lines[j]
        if not ln.strip():
            cont.append(ln)
            j += 1
            continue
        if L.indent(ln) > decl_indent + 1:
            cont.append(ln)
            j += 1
            continue
        break
    # give back trailing blank lines
    while cont and not cont[-1].strip():
        cont.pop()
        j -= 1
    # j may now point at a blank line - fine, the main loop skips blanks.
    text = _dedent(L, cont)
    if body and text:
        return body[0] + "\n" + text, j
    if body:
        return body[0], j
    return text, j


def _dedent(L: _Lines, lines: List[str]) -> str:
    non_blank = [l for l in lines if l.strip()]
    if not non_blank:
        return ""
    m = min(L.indent(l) for l in non_blank)
    out = [L.strip_indent(l, m).rstrip() for l in lines]
    while out and not out[0].strip():
        out.pop(0)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def parse(text: str, file: Optional[str] = None) -> Node:
    """Parse a TMDL document into a Node tree rooted at a synthetic 'document' node."""
    L = _Lines(text)
    root = Node("document", file=file)
    stack: List[Tuple[int, Node]] = [(-1, root)]
    desc: List[str] = []
    i = 0
    lines = L.lines
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        if not s:
            i += 1
            continue
        ind = L.indent(raw)
        if s.startswith("///"):
            desc.append(s[3:].strip())
            i += 1
            continue
        if s == "createOrReplace":
            i += 1
            continue
        while stack[-1][0] >= ind:
            stack.pop()
        parent = stack[-1][1]

        pm = PROP_RE.match(s)
        if pm:
            key, val = pm.group(1), (pm.group(2) or "").strip()
            parent.props[key] = val
            desc = []
            i += 1
            continue

        is_ref = False
        body = s
        if body.startswith("ref "):
            is_ref = True
            body = body[4:].strip()
        km = KIND_RE.match(body)
        if not km:
            i += 1
            continue
        kind, rest = km.group(1), km.group(2)
        name, remainder = split_name(rest)
        node = Node(kind=kind, name=name, is_ref=is_ref, line=i + 1, file=file)
        if desc:
            node.description = "\n".join(desc).strip()
            desc = []
        i += 1
        if remainder.startswith("="):
            expr, i = _collect_expression(L, i, ind, remainder[1:])
            node.expr = expr
        parent.children.append(node)
        stack.append((ind, node))

    _fold_flags(root)
    return root


def _fold_flags(node: Node) -> None:
    """Nameless, expression-less leaf nodes are boolean flags of their parent."""
    keep: List[Node] = []
    for c in node.children:
        _fold_flags(c)
        if c.name is None and c.expr is None and not c.children and not c.props and not c.is_ref:
            node.props[c.kind] = True
        elif c.name is None and c.expr is not None and not c.children and not c.props:
            # `source = ...`, `formatStringDefinition = ...`: expression-valued property
            node.props[c.kind] = c.expr
        else:
            keep.append(c)
    node.children = keep
