#!/usr/bin/env python3
"""pbipx - understand and document Power BI projects (PBIP).

  run     <zip|folder|.pbip> --out <dir>     extract + docs in one go
  extract <zip|folder|.pbip> --out <dir>     knowledge pack only (knowledge/model.json, CSVs, agent-brief.md)
  docs    <dir> [--enrichment file]          (re)build documentation from a knowledge pack + enrichment.json
  query   <dir> <command> [args]             look things up:
          summary | find <text> | measure <name> | table <name> | column <T[C]> | page <name>
          path <tableA> <tableB> | impact <measure|T[C]|table> | unused | findings

Stdlib only, Python 3.9+.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pbipx import docs as _docs  # noqa: E402
from pbipx.pack import extract, load_pack  # noqa: E402
from pbipx.query import Q  # noqa: E402


def _docs_for(out: Path, enrichment: Path = None) -> list:
    pack = load_pack(out)
    enr_path = enrichment or (out / "enrichment.json")
    enr = None
    if enr_path and Path(enr_path).is_file():
        try:
            enr = json.loads(Path(enr_path).read_text(encoding="utf-8"))
        except ValueError as e:
            raise SystemExit(f"enrichment.json is not valid JSON: {e}")
    return _docs.build(pack, out / "docs", enr)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pbip_explain.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "extract"):
        p = sub.add_parser(name)
        p.add_argument("source", type=Path)
        p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("docs")
    p.add_argument("dir", type=Path)
    p.add_argument("--enrichment", type=Path)
    p = sub.add_parser("query")
    p.add_argument("dir", type=Path)
    p.add_argument("command")
    p.add_argument("args", nargs="*")
    a = ap.parse_args(argv)

    if a.cmd in ("run", "extract"):
        mpath = extract(a.source, a.out)
        print(f"knowledge pack: {mpath.parent}")
        pack = load_pack(a.out)
        for m in pack["semanticModels"]:
            st = m["analysis"]["stats"]
            sev = {}
            for f in m["analysis"]["findings"]:
                sev[f["severity"]] = sev.get(f["severity"], 0) + 1
            print(f"model {m['name']}: {st['tables']} tables, {st['measures']} measures, {st['relationships']} relationships; "
                  f"findings {sev}")
        for r in pack["reports"]:
            print(f"report {r['name']}: {len(r['pages'])} pages, {sum(len(p['visuals']) for p in r['pages'])} visuals")
        if a.cmd == "run":
            for f in _docs_for(a.out):
                print(f"docs: {f}")
        print(f"enrichment template: {a.out / 'enrichment.json'}")
        return 0
    if a.cmd == "docs":
        for f in _docs_for(a.dir, a.enrichment):
            print(f"docs: {f}")
        return 0
    q = Q(load_pack(a.dir))
    cmd, args = a.command, a.args
    fn = {"summary": lambda: q.summary(), "find": lambda: q.find(" ".join(args)),
          "measure": lambda: q.measure(" ".join(args)), "table": lambda: q.table(" ".join(args)),
          "column": lambda: q.column(" ".join(args)), "page": lambda: q.page(" ".join(args)),
          "path": lambda: q.path(args[0], args[1]), "impact": lambda: q.impact(" ".join(args)),
          "unused": lambda: q.unused(), "findings": lambda: q.findings()}.get(cmd)
    if not fn:
        raise SystemExit(f"Unknown query command '{cmd}'. See --help.")
    print(fn())
    return 0


if __name__ == "__main__":
    sys.exit(main())
