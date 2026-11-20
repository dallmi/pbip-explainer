"""Data model diagram as self-contained SVG (no JS libraries, no CDN).

Layout: per connected component a deterministic force-directed layout with
fact tables seeded in the centre, then overlap removal, then components packed
left to right. Tables without relationships go in a row underneath.
"""
from __future__ import annotations

import math
from collections import defaultdict
from html import escape
from typing import Dict, List, Tuple

ROW_H = 16
HEAD_H = 28
FOOT_H = 20
MAX_KEYS = 7

STYLE = """
.erd text{font-family:"Frutiger 45 Light",Frutiger,"Helvetica Neue",Arial,"Segoe UI",sans-serif;user-select:none;-webkit-user-select:none}
.erd .box{fill:#fff;stroke:#9A978E;stroke-width:1}
.erd .head{fill:#F2F0EA}
.erd .n-fact .head{fill:#111111}.erd .n-fact .title{fill:#fff}
.erd .n-date .head{fill:#B98E2C}.erd .n-date .title{fill:#fff}
.erd .n-bridge .head{fill:#66645C}.erd .n-bridge .title{fill:#fff}
.erd .n-param .box,.erd .n-disc .box{stroke-dasharray:4 3}
.erd .title{font-size:12.5px;font-weight:700;fill:#000}
.erd .key{font-size:11px;fill:#404040}
.erd .foot{font-size:10px;fill:#7A7870}
.erd .edge line{stroke:#5A5D5C;stroke-width:1.3}
.erd .edge.inactive line{stroke-dasharray:5 4;stroke:#8E8D83}
.erd .edge.both line{stroke:#B00000}
.erd .card{font-size:11px;fill:#000;font-weight:600}
.erd .arrow{fill:#5A5D5C}.erd .edge.both .arrow{fill:#B00000}
.erd .hit{stroke:transparent;stroke-width:10}
.erd .lane{font-size:11px;fill:#7A7870;letter-spacing:.04em}
.erd .node{cursor:pointer}
.erd .node:focus{outline:none}
.erd .node:focus-visible .box{stroke:#E60000;stroke-width:2}
.erd.focus .node,.erd.focus .edge{opacity:.15}
.erd.focus .node.on,.erd.focus .edge.on{opacity:1}
.erd.focus .node.sel .box{stroke:#E60000;stroke-width:2}
"""

ROLE_CLASS = {"fact": "n-fact", "date": "n-date", "bridge": "n-bridge", "fieldParameter": "n-param",
              "whatIfParameter": "n-param", "measureTable": "n-param", "calculationGroup": "n-param",
              "disconnected": "n-disc"}


def _boxes(model: dict) -> Dict[str, dict]:
    keys: Dict[str, List[str]] = defaultdict(list)
    for r in model["relationships"]:
        for t, c in ((r["fromTable"], r["fromColumn"]), (r["toTable"], r["toColumn"])):
            if c not in keys[t]:
                keys[t].append(c)
    out = {}
    for t in model["tables"]:
        if t.get("isAutoDateTable"):
            continue
        ks = keys.get(t["name"], [])
        shown = ks[:MAX_KEYS]
        longest = max([len(t["name"]) * 7.4] + [len(k) * 6.4 + 22 for k in shown] + [120])
        w = min(max(longest + 24, 150), 280)
        h = HEAD_H + ROW_H * len(shown) + (6 if shown else 0) + FOOT_H
        out[t["name"]] = {"name": t["name"], "role": t.get("role"), "w": w, "h": h, "keys": shown,
                          "more": len(ks) - len(shown), "cols": len(t["columns"]), "meas": len(t["measures"]),
                          "x": 0.0, "y": 0.0}
    return out


def _components(nodes: List[str], edges: List[Tuple[str, str]]) -> List[List[str]]:
    adj = defaultdict(set)
    for a, b in edges:
        adj[a].add(b)
        adj[b].add(a)
    seen, comps = set(), []
    for n in nodes:
        if n in seen or not adj[n]:
            continue
        stack, comp = [n], []
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            comp.append(x)
            stack.extend(adj[x] - seen)
        comps.append(sorted(comp))
    return sorted(comps, key=len, reverse=True)


def _layout_component(comp: List[str], boxes: Dict[str, dict], edges: List[Tuple[str, str]]) -> None:
    n = len(comp)
    if n == 1:
        boxes[comp[0]]["x"] = boxes[comp[0]]["y"] = 0.0
        return
    cedges = [(a, b) for a, b in edges if a in comp and b in comp and a != b]
    deg = defaultdict(int)
    for a, b in cedges:
        deg[a] += 1
        deg[b] += 1
    facts = [c for c in comp if boxes[c]["role"] in ("fact", "bridge")] or [max(comp, key=lambda c: deg[c])]
    others = [c for c in comp if c not in facts]
    k = 120.0
    R = max(200.0, 55.0 * math.sqrt(n) * 2.0)
    pos = {}
    for i, f in enumerate(facts):
        pos[f] = [(i - (len(facts) - 1) / 2) * 300.0, (i % 2) * 60.0]
    for i, o in enumerate(others):
        ang = 2 * math.pi * i / max(len(others), 1) + 0.3
        pos[o] = [R * math.cos(ang) * 1.4, R * math.sin(ang)]
    temp = R / 2
    for it in range(450):
        disp = {c: [0.0, 0.0] for c in comp}
        for i in range(n):
            a = comp[i]
            for j in range(i + 1, n):
                b = comp[j]
                dx = pos[a][0] - pos[b][0]
                dy = pos[a][1] - pos[b][1]
                d = math.hypot(dx, dy) or 0.01
                size = (boxes[a]["w"] + boxes[b]["w"]) / 2 + 20
                f = (k * k) / d + (size * size * 0.6 / d if d < size * 1.6 else 0.0)
                disp[a][0] += dx / d * f
                disp[a][1] += dy / d * f
                disp[b][0] -= dx / d * f
                disp[b][1] -= dy / d * f
        for a, b in cedges:
            dx = pos[a][0] - pos[b][0]
            dy = pos[a][1] - pos[b][1]
            d = math.hypot(dx, dy) or 0.01
            f = d * d / k * 0.6
            disp[a][0] -= dx / d * f
            disp[a][1] -= dy / d * f
            disp[b][0] += dx / d * f
            disp[b][1] += dy / d * f
        for c in comp:
            dx, dy = disp[c]
            d = math.hypot(dx, dy) or 0.01
            step = min(d, temp)
            pos[c][0] += dx / d * step
            pos[c][1] += dy / d * step * 0.8   # prefer wide over tall layouts
        temp = max(temp * 0.985, 2.0)
    # overlap removal
    gap = 60
    for _ in range(300):
        moved = False
        for i in range(n):
            a = comp[i]
            for j in range(i + 1, n):
                b = comp[j]
                ox = (boxes[a]["w"] + boxes[b]["w"]) / 2 + gap - abs(pos[a][0] - pos[b][0])
                oy = (boxes[a]["h"] + boxes[b]["h"]) / 2 + gap - abs(pos[a][1] - pos[b][1])
                if ox > 0 and oy > 0:
                    moved = True
                    if ox < oy:
                        s = 1 if pos[a][0] >= pos[b][0] else -1
                        pos[a][0] += s * ox / 2
                        pos[b][0] -= s * ox / 2
                    else:
                        s = 1 if pos[a][1] >= pos[b][1] else -1
                        pos[a][1] += s * oy / 2
                        pos[b][1] -= s * oy / 2
        if not moved:
            break
    for c in comp:
        boxes[c]["x"], boxes[c]["y"] = pos[c]


def _clip(cx, cy, w, h, tx, ty):
    """Point where the segment centre->(tx,ty) leaves the rectangle."""
    dx, dy = tx - cx, ty - cy
    if dx == 0 and dy == 0:
        return cx, cy
    sx = (w / 2) / abs(dx) if dx else float("inf")
    sy = (h / 2) / abs(dy) if dy else float("inf")
    s = min(sx, sy)
    return cx + dx * s, cy + dy * s


def render(model: dict) -> Tuple[str, int, int]:
    boxes = _boxes(model)
    names = list(boxes)
    edges = [(r["fromTable"], r["toTable"]) for r in model["relationships"]
             if r["fromTable"] in boxes and r["toTable"] in boxes]
    comps = _components(names, edges)
    in_comp = {c for comp in comps for c in comp}
    loose = [n for n in names if n not in in_comp]
    margin = 30
    cursor_x = margin
    max_h = 0.0
    for comp in comps:
        _layout_component(comp, boxes, edges)
        minx = min(boxes[c]["x"] - boxes[c]["w"] / 2 for c in comp)
        miny = min(boxes[c]["y"] - boxes[c]["h"] / 2 for c in comp)
        maxx = max(boxes[c]["x"] + boxes[c]["w"] / 2 for c in comp)
        maxy = max(boxes[c]["y"] + boxes[c]["h"] / 2 for c in comp)
        for c in comp:
            boxes[c]["x"] += cursor_x - minx
            boxes[c]["y"] += margin + 10 - miny
        cursor_x += (maxx - minx) + 80
        max_h = max(max_h, maxy - miny)
    width = max(cursor_x - 80 + margin, 400)
    y0 = margin + 10 + max_h + (70 if comps else 0)
    lane_y = y0 - 24
    if loose:
        x = margin
        row_h = 0.0
        max_w = max(width, 900)
        for n in sorted(loose):
            b = boxes[n]
            if x + b["w"] > max_w - margin:
                x = margin
                y0 += row_h + 30
                row_h = 0
            b["x"] = x + b["w"] / 2
            b["y"] = y0 + b["h"] / 2
            x += b["w"] + 30
            row_h = max(row_h, b["h"])
            width = max(width, x + margin)
        height = y0 + row_h + margin
    else:
        height = y0 - 40 + margin

    W, H = int(math.ceil(width)), int(math.ceil(height))
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" class="erd" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
           f'role="group" aria-label="Data model diagram">', f"<style>{STYLE}</style>",
           '<defs><marker id="ah" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
           '<path d="M0,0 L10,5 L0,10 z" class="arrow"/></marker></defs>']
    if loose and comps:
        out.append(f'<text class="lane" x="{margin}" y="{lane_y:.0f}">TABLES WITHOUT RELATIONSHIPS</text>')
    out.append('<g class="edges">')
    pair_count: Dict[frozenset, int] = defaultdict(int)
    for r in model["relationships"]:
        pair_count[frozenset((r["fromTable"], r["toTable"]))] += 1
    pair_seen: Dict[frozenset, int] = defaultdict(int)
    for r in model["relationships"]:
        a, b = boxes.get(r["fromTable"]), boxes.get(r["toTable"])
        if not a or not b:
            continue
        key = frozenset((r["fromTable"], r["toTable"]))
        k_i, k_n = pair_seen[key], pair_count[key]
        pair_seen[key] += 1
        off = (k_i - (k_n - 1) / 2) * 14.0          # parallel offset for several relationships between one pair
        dxc, dyc = b["x"] - a["x"], b["y"] - a["y"]
        Lc = math.hypot(dxc, dyc) or 1.0
        if r["fromTable"] > r["toTable"]:
            off = -off
        px, py = -dyc / Lc * off, dxc / Lc * off
        x1, y1 = _clip(a["x"], a["y"], a["w"] + 4, a["h"] + 4, b["x"] + px, b["y"] + py)
        x2, y2 = _clip(b["x"], b["y"], b["w"] + 4, b["h"] + 4, a["x"] + px, a["y"] + py)
        x1, y1, x2, y2 = x1 + px, y1 + py, x2 + px, y2 + py
        cls = ["edge"]
        if not r["isActive"]:
            cls.append("inactive")
        if r["crossFilteringBehavior"] == "bothDirections":
            cls.append("both")
        title = (f"{r['fromTable']}[{r['fromColumn']}] ({r['fromCardinality']}) → {r['toTable']}[{r['toColumn']}] "
                 f"({r['toCardinality']}) · filter: {r['crossFilteringBehavior']}" + ("" if r["isActive"] else " · inactive"))
        out.append(f'<g class="{" ".join(cls)}" data-a="{escape(r["fromTable"])}" data-b="{escape(r["toTable"])}">'
                   f"<title>{escape(title)}</title>")
        out.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"/>')
        out.append(f'<line class="hit" x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"/>')
        L = math.hypot(x2 - x1, y2 - y1) or 1
        ux, uy = (x2 - x1) / L, (y2 - y1) / L
        for (px, py, s, card) in ((x1, y1, 1, r["fromCardinality"]), (x2, y2, -1, r["toCardinality"])):
            tx, ty = px + s * ux * 12 - uy * 9, py + s * uy * 12 + ux * 9
            out.append(f'<text class="card" x="{tx:.1f}" y="{ty + 4:.1f}" text-anchor="middle">{"*" if card == "many" else "1"}</text>')
        # filter direction: from the "one" side towards the "many" side
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        if r["crossFilteringBehavior"] == "bothDirections":
            out.append(f'<line x1="{mx - ux * 9:.1f}" y1="{my - uy * 9:.1f}" x2="{mx + ux * 9:.1f}" y2="{my + uy * 9:.1f}" '
                       f'marker-start="url(#ah)" marker-end="url(#ah)"/>')
        else:
            d = -1 if r["toCardinality"] == "one" else 1
            out.append(f'<line x1="{mx - d * ux * 9:.1f}" y1="{my - d * uy * 9:.1f}" x2="{mx + d * ux * 9:.1f}" '
                       f'y2="{my + d * uy * 9:.1f}" marker-end="url(#ah)"/>')
        out.append("</g>")
    out.append("</g><g class=\"nodes\">")
    for b in boxes.values():
        x, y = b["x"] - b["w"] / 2, b["y"] - b["h"] / 2
        cls = ROLE_CLASS.get(b["role"] or "", "n-dim")
        out.append(f'<g class="node {cls}" data-t="{escape(b["name"])}" transform="translate({x:.1f},{y:.1f})" '
                   f'tabindex="0" role="button" aria-label="Table {escape(b["name"])}">')
        out.append(f'<title>{escape(b["name"])} — {escape(str(b["role"]))}</title>')
        out.append(f'<rect class="box" width="{b["w"]:.0f}" height="{b["h"]:.0f}" rx="2"/>')
        out.append(f'<rect class="head" width="{b["w"]:.0f}" height="{HEAD_H}" rx="2"/>')
        out.append(f'<text class="title" x="10" y="18">{escape(b["name"])}</text>')
        for i, kname in enumerate(b["keys"]):
            out.append(f'<text class="key" x="10" y="{HEAD_H + 15 + i * ROW_H}">⚷ {escape(kname)}</text>')
        foot = (f'{b["cols"]} column{"" if b["cols"] == 1 else "s"} · {b["meas"]} measure{"" if b["meas"] == 1 else "s"}') + (f' · +{b["more"]} keys' if b["more"] > 0 else "")
        out.append(f'<text class="foot" x="10" y="{b["h"] - 7}">{foot}</text>')
        out.append("</g>")
    out.append("</g></svg>")
    return "\n".join(out), W, H


def mermaid(model: dict) -> str:
    """Mermaid erDiagram (renders on GitHub, Azure DevOps wiki, VS Code)."""
    import re

    def ent(n: str) -> str:
        return re.sub(r"[^A-Za-z0-9_]", "_", n)

    lines = ["erDiagram"]
    tables = {t["name"] for t in model["tables"] if not t.get("isAutoDateTable")}
    for r in model["relationships"]:
        if r["fromTable"] not in tables or r["toTable"] not in tables:
            continue
        left = "}o" if r["fromCardinality"] == "many" else "||"
        right = "o{" if r["toCardinality"] == "many" else "||"
        link = "--" if r["isActive"] else ".."
        label = f"{r['fromColumn']} = {r['toColumn']}" + ("" if r["isActive"] else " (inactive)")
        if r["crossFilteringBehavior"] == "bothDirections":
            label += " (both)"
        lines.append(f'    {ent(r["fromTable"])} {left}{link}{right} {ent(r["toTable"])} : "{label.replace(chr(34), "")}"')
    related = {r["fromTable"] for r in model["relationships"]} | {r["toTable"] for r in model["relationships"]}
    for t in sorted(tables - related):
        lines.append(f"    {ent(t)}")
    return "\n".join(lines)
