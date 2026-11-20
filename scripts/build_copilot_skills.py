#!/usr/bin/env python3
"""build_copilot_skills.py - package pbip-explain for Copilot Studio.

Two ways to run the explainer in Copilot Studio, both built from this repository:

  A  Q&A agent without code execution: agent-instructions-qa.txt as instructions,
     plus the knowledge pack of each report as knowledge files (--knowledge).
  B  Agent with code execution: pbip-explain.zip as an uploaded skill plus
     agent-instructions.txt. It runs scripts/pbip_explain.py on the zip the user attaches.

Steps:
  1. copies the skill, leaving out caches and hidden files
  2. inserts the Copilot Studio notes from copilot/notes/ after the front matter
  3. checks the host limits, and writes nothing if one is violated
  4. writes <out>/pbip-explain.zip, agent-instructions.txt, agent-instructions-qa.txt,
     build-report.txt and manifest.json (hashes, so the report can say what changed)
  5. with --knowledge: copies the knowledge files of each extracted pack into
     <pack>/copilot-knowledge/ (inside work/, which is git-ignored), every file named
     <pack>-<file>, ready to upload as knowledge for mode A

Usage:
  python3 scripts/build_copilot_skills.py [--out dist/copilot]
  python3 scripts/build_copilot_skills.py --knowledge work/<name> [work/<other> ...] [--with-model-json]

Exit code: 0 = built, 1 = a limit is violated (nothing written), 2 = usage problem.
"""
import argparse
import datetime
import hashlib
import io
import json
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ["pbip-explain"]
NOTES = ROOT / "copilot" / "notes"
# {file in the output folder: source}; both are pasted into an agent's instructions
INSTRUCTIONS = {
    "agent-instructions.txt": ROOT / "copilot" / "agent-instructions.md",
    "agent-instructions-qa.txt": ROOT / "copilot" / "agent-instructions-qa.md",
}

# Strictest published limits across Microsoft's skill hosts (declarative agents,
# Copilot Cowork). Copilot Studio documents none of its own yet. Same values as
# html2pbi/scripts/build_copilot_skills.py; a test keeps the two in sync.
MAX_SKILLS = 8
MAX_SKILL_MD_CHARS = 20_000
MAX_DESCRIPTION_CHARS = 1_024
MAX_COMPANIONS = 20  # files besides SKILL.md, per skill
MAX_FILE_BYTES = 5 * 1024 ** 2
MAX_COMPANION_BYTES = 10 * 1024 ** 2
MAX_ZIP_BYTES = 50 * 1024 ** 2
MAX_DEPTH = 3  # folders below the skill root
MAX_INSTRUCTIONS_CHARS = 8_000
ALLOWED = {".json", ".xml", ".yaml", ".yml", ".ini", ".config", ".utf8", ".docx", ".doc", ".docm", ".pdf", ".txt",
           ".rtf", ".md", ".ppt", ".pptx", ".ppsm", ".xlsx", ".xls", ".xlsm", ".csv", ".tsv", ".html", ".htm", ".png",
           ".jpg", ".jpeg", ".gif", ".bmp", ".log", ".py", ".js", ".mjs", ".cjs", ".ts", ".mts", ".sh", ".bash"}
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SAFE_FILE_RE = re.compile(r"^[A-Za-z0-9 ._!-]+$")

# Knowledge files (mode A), from "Upload files as a knowledge source" and "Quotas and limits"
# in the Copilot Studio docs: 512 MB per file, 500 files per agent.
KNOWLEDGE_FILES = ["agent-brief.md", "measures.csv", "columns.csv", "relationships.csv", "tables.csv",
                   "sources.csv", "visuals.csv"]
KNOWLEDGE_TYPES = {".md", ".csv", ".json"}  # the subset of accepted types the pack uses
MAX_KNOWLEDGE_FILE_BYTES = 512 * 1024 ** 2
MAX_KNOWLEDGE_FILES = 500
KNOWLEDGE_DIR = "copilot-knowledge"


def split_front_matter(text):
    """(fields, body) for a SKILL.md with single-line `key: value` front matter."""
    lines = text.split("\n")
    if lines[0] != "---" or "---" not in lines[1:]:
        raise ValueError("SKILL.md has no front matter")
    end = lines.index("---", 1)
    fields = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(":")
        if not sep or not key or key[0].isspace():
            raise ValueError(f"front matter line is not `key: value`: {line[:60]!r}")
        value = value.strip()
        fields[key.strip()] = json.loads(value) if value.startswith('"') else value
    return fields, "\n".join(lines[end + 1:])


def join_front_matter(fields, body):
    # double-quoted values are valid YAML whatever the text contains (colons, quotes, #)
    head = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fields.items())
    return f"---\n{head}\n---\n{body}"


def environment_notes(skill):
    notes = (NOTES / "_all.md").read_text(encoding="utf-8").rstrip("\n")
    own = NOTES / f"{skill}.md"
    if own.exists():
        notes += "\n" + own.read_text(encoding="utf-8").rstrip("\n")
    return notes


def stage(skill, dest, log):
    """Copy one skill into dest in its Copilot Studio form."""
    src = ROOT / "skills" / skill
    for p in sorted(src.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        if "__pycache__" in rel.parts or any(part.startswith(".") for part in rel.parts) or p.suffix == ".pyc":
            continue
        if rel.parts[0] == "evals":  # skill-creator test cases, not part of the skill
            continue
        if rel.name == "SKILL.md" and len(rel.parts) == 1:
            continue
        out = rel if p.suffix in ALLOWED else rel.with_name(rel.name + ".txt")
        if out != rel:
            log["renamed"].append(f"{skill}/{rel.as_posix()} -> {out.name}")
        (dest / out).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, dest / out)

    fields, body = split_front_matter((src / "SKILL.md").read_text(encoding="utf-8"))
    body = "\n" + environment_notes(skill) + "\n" + body
    (dest / "SKILL.md").write_text(join_front_matter(fields, body), encoding="utf-8")


def problems(skill_dir):
    """Every limit a staged skill violates."""
    found, name = [], skill_dir.name
    md = skill_dir / "SKILL.md"
    if not md.exists():
        return [f"{name}: SKILL.md missing"]
    text = md.read_text(encoding="utf-8")
    try:
        fields, _ = split_front_matter(text)
    except ValueError as e:
        return [f"{name}: {e}"]
    if fields.get("name") != name:
        found.append(f"{name}: front matter name {fields.get('name')!r} differs from the folder name")
    if not NAME_RE.match(name):
        found.append(f"{name}: name must be lowercase letters, digits and single hyphens")
    desc = fields.get("description") or ""
    if not desc or "\n" in desc or len(desc) > MAX_DESCRIPTION_CHARS:
        found.append(f"{name}: description must be one line of 1-{MAX_DESCRIPTION_CHARS} characters (has {len(desc)})")
    if len(text) >= MAX_SKILL_MD_CHARS:
        found.append(f"{name}: SKILL.md has {len(text)} characters, limit {MAX_SKILL_MD_CHARS}")
    companions = [p for p in sorted(skill_dir.rglob("*")) if p.is_file() and p != md]
    if len(companions) > MAX_COMPANIONS:
        found.append(f"{name}: {len(companions)} files besides SKILL.md, limit {MAX_COMPANIONS}")
    if sum(p.stat().st_size for p in companions) > MAX_COMPANION_BYTES:
        found.append(f"{name}: files besides SKILL.md exceed {MAX_COMPANION_BYTES // 1024 ** 2} MB")
    for p in companions + [md]:
        rel = p.relative_to(skill_dir)
        if p.stat().st_size > MAX_FILE_BYTES:
            found.append(f"{name}/{rel}: larger than {MAX_FILE_BYTES // 1024 ** 2} MB")
        if len(rel.parts) - 1 > MAX_DEPTH:
            found.append(f"{name}/{rel}: deeper than {MAX_DEPTH} folders")
        if p.suffix not in ALLOWED:
            found.append(f"{name}/{rel}: file type {p.suffix or '(none)'} not accepted")
        if any(not SAFE_FILE_RE.match(part) or part.startswith(".") for part in rel.parts):
            found.append(f"{name}/{rel}: unsafe or hidden file name")
    return found


def zip_bytes(skill_dir):
    """The skill as a zip with SKILL.md at the root. Same input, same bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(skill_dir.rglob("*")):
            if p.is_file():
                info = zipfile.ZipInfo(p.relative_to(skill_dir).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                z.writestr(info, p.read_bytes())
    return buf.getvalue()


# ---------------------------------------------------------------- knowledge files (mode A)

def pack_prefix(pack_dir):
    """File-name prefix for one pack: the source name from model.json, made file-name safe."""
    name = json.loads((pack_dir / "knowledge" / "model.json").read_text(encoding="utf-8"))["source"]["name"]
    name = re.sub(r"\.(pbip|zip)$", "", name, flags=re.I)
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or pack_dir.name


def knowledge_files(pack_dir, with_model_json=False):
    """{target file name: source path} for one extracted pack, or raise ValueError."""
    k = pack_dir / "knowledge"
    if not (k / "model.json").is_file():
        raise ValueError(f"{pack_dir}: no knowledge/model.json, run `pbip_explain.py run` first")
    prefix = pack_prefix(pack_dir)
    names = list(KNOWLEDGE_FILES) + (["model.json"] if with_model_json else [])
    files = {f"{prefix}-{n}": k / n for n in names if (k / n).is_file()}
    if f"{prefix}-agent-brief.md" not in files:
        raise ValueError(f"{pack_dir}: knowledge/agent-brief.md missing, rerun `pbip_explain.py extract`")
    for doc in sorted((pack_dir / "docs").glob("*-documentation.md")):
        files[f"{prefix}-{doc.name}"] = doc
    return files


def knowledge_problems(files):
    """Every knowledge-file limit the {name: path} map violates."""
    found = []
    if len(files) > MAX_KNOWLEDGE_FILES:
        found.append(f"{len(files)} knowledge files, limit {MAX_KNOWLEDGE_FILES} per agent")
    for name, p in files.items():
        if Path(name).suffix not in KNOWLEDGE_TYPES:
            found.append(f"{name}: file type not used for knowledge")
        if not SAFE_FILE_RE.match(name) or name.startswith("."):
            found.append(f"{name}: unsafe or hidden file name")
        if p.stat().st_size > MAX_KNOWLEDGE_FILE_BYTES:
            found.append(f"{name}: larger than {MAX_KNOWLEDGE_FILE_BYTES // 1024 ** 2} MB")
    return found


def build_knowledge(pack_dirs, with_model_json):
    """Write <pack>/copilot-knowledge/ for each pack. Returns (report lines, problems)."""
    plans, found, seen = [], [], {}
    for d in pack_dirs:
        try:
            files = knowledge_files(d, with_model_json)
        except (ValueError, KeyError, OSError) as e:
            found.append(str(e))
            continue
        for name in files:
            if name in seen:
                found.append(f"{name}: produced by both {seen[name]} and {d}; rename one source folder")
            seen[name] = d
        plans.append((d, files))
    all_files = {n: p for _, files in plans for n, p in files.items()}
    found += knowledge_problems(all_files)
    if found:
        return [], found
    lines = []
    for d, files in plans:
        out = d / KNOWLEDGE_DIR
        if out.exists():
            shutil.rmtree(out)
        out.mkdir()
        for name, p in files.items():
            shutil.copyfile(p, out / name)
        size = sum(p.stat().st_size for p in files.values())
        shown = out.relative_to(ROOT) if ROOT in out.parents else out  # no absolute paths in the report
        lines.append(f"{shown.as_posix()}/: {len(files)} files, {size / 1024:,.0f} KB")
    return lines, []


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "dist" / "copilot"), help="output folder (default: dist/copilot)")
    ap.add_argument("--knowledge", nargs="+", type=Path, metavar="PACK_DIR",
                    help="extracted pack folder(s) (work/<name>) to turn into knowledge files for mode A")
    ap.add_argument("--with-model-json", action="store_true",
                    help="also add model.json to the knowledge files (large; the CSVs and docs usually suffice)")
    a = ap.parse_args()
    if a.with_model_json and not a.knowledge:
        ap.error("--with-model-json needs --knowledge")
    out = Path(a.out).resolve()

    log = {"renamed": []}
    found, packages, instructions = [], {}, {}
    if len(SKILLS) > MAX_SKILLS:
        found.append(f"{len(SKILLS)} skills, limit {MAX_SKILLS}")
    for target, src in INSTRUCTIONS.items():
        instructions[target] = src.read_text(encoding="utf-8")
        if len(instructions[target]) > MAX_INSTRUCTIONS_CHARS:
            found.append(f"{src.name} has {len(instructions[target])} characters, limit {MAX_INSTRUCTIONS_CHARS}")
    with tempfile.TemporaryDirectory() as tmp:
        for skill in SKILLS:
            d = Path(tmp) / skill
            d.mkdir()
            try:
                stage(skill, d, log)
            except ValueError as e:
                found.append(str(e))
                continue
            found += problems(d)
            data = zip_bytes(d)
            if len(data) > MAX_ZIP_BYTES:
                found.append(f"{skill}.zip: larger than {MAX_ZIP_BYTES // 1024 ** 2} MB")
            files = [p for p in d.rglob("*") if p.is_file()]
            packages[skill] = {"zip": data, "files": len(files),
                               "skill_md_chars": len((d / "SKILL.md").read_text(encoding="utf-8"))}
    if a.knowledge:
        knowledge_lines, k_found = build_knowledge([p.resolve() for p in a.knowledge], a.with_model_json)
        found += k_found
    else:
        knowledge_lines = []

    if found:
        print("Not built, limits violated:", file=sys.stderr)
        print("\n".join(f"- {p}" for p in found), file=sys.stderr)
        return 1

    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.json"
    before = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    manifest = {}

    def changed(key, digest):
        manifest[key] = digest
        return "new" if key not in before else ("yes" if before[key] != digest else "no")

    rows = []
    for skill, pkg in packages.items():
        (out / f"{skill}.zip").write_bytes(pkg["zip"])
        state = changed(skill, hashlib.sha256(pkg["zip"]).hexdigest())
        rows.append(f"{skill:<13} {pkg['files']:>5}  {pkg['skill_md_chars']:>8,}  {len(pkg['zip']) / 1024:>8,.0f}  {state}")
    instr_rows = []
    for target, text in instructions.items():
        (out / target).write_text(text, encoding="utf-8")
        state = changed(Path(target).stem, hashlib.sha256(text.encode()).hexdigest())
        instr_rows.append(f"{target}: {len(text):,} of {MAX_INSTRUCTIONS_CHARS:,} characters, changed: {state}")
    manifest_path.write_text(json.dumps(manifest, indent=1) + "\n")

    report = [
        f"pbip-explainer for Copilot Studio, built {datetime.date.today().isoformat()}",
        "",
        f"{'skill':<13} {'files':>5}  {'SKILL.md':>8}  {'zip KB':>8}  changed",
        *rows,
        *instr_rows,
        "Renamed (file type not accepted): " + (", ".join(log["renamed"]) or "nothing"),
        "",
        "Mode B, agent with code execution (runs the tool on an attached PBIP zip):",
        "  Copilot Studio > your agent > Build > Skills > Add skill > Upload a skill: pbip-explain.zip.",
        "  Paste agent-instructions.txt into the agent's instructions. Upload again only what is marked new or yes.",
        "",
        "Mode A, Q&A agent without code execution (answers from knowledge files):",
        "  Paste agent-instructions-qa.txt into the agent's instructions.",
        "  Per report: pbip_explain.py run <zip> --out work/<name>, then",
        "  build_copilot_skills.py --knowledge work/<name>, then Knowledge > Add knowledge > Upload files",
        "  with every file in work/<name>/copilot-knowledge/.",
    ]
    if knowledge_lines:
        report += ["", "Knowledge files written (real project metadata: keep in work/, never commit):",
                   *(f"  {l}" for l in knowledge_lines)]
    (out / "build-report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
