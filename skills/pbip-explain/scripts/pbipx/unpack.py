"""Safe unpacking of a PBIP zip and discovery of semantic models and reports."""
from __future__ import annotations

import json
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Dict, List

MAX_FILES = 50_000
MAX_TOTAL_BYTES = 2 * 1024 ** 3
# Data caches and per-user settings: not needed to understand the model, may contain data.
SKIP_SUFFIXES = (".abf",)
SKIP_NAMES = {"localSettings.json", "cache.abf"}


class UnsafeArchive(Exception):
    pass


def safe_extract(zip_path: Path, dest: Path) -> List[str]:
    """Extract `zip_path` into `dest`, refusing path traversal, links and zip bombs."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    skipped: List[str] = []
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_FILES:
            raise UnsafeArchive(f"archive has {len(infos)} entries (limit {MAX_FILES})")
        total = sum(i.file_size for i in infos)
        if total > MAX_TOTAL_BYTES:
            raise UnsafeArchive(f"archive expands to {total} bytes (limit {MAX_TOTAL_BYTES})")
        for info in infos:
            name = info.filename.replace("\\", "/")
            pp = PurePosixPath(name)
            if pp.is_absolute() or ".." in pp.parts or (pp.parts and ":" in pp.parts[0]):
                raise UnsafeArchive(f"unsafe path in archive: {info.filename}")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise UnsafeArchive(f"symbolic link in archive: {info.filename}")
            if name.startswith("__MACOSX/") or pp.name == ".DS_Store":
                continue
            if pp.name in SKIP_NAMES or name.endswith(SKIP_SUFFIXES):
                skipped.append(name)
                continue
            target = (dest / Path(*pp.parts)).resolve() if pp.parts else root
            if root not in target.parents and target != root:
                raise UnsafeArchive(f"entry escapes target folder: {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    return skipped


def discover(root: Path) -> Dict[str, list]:
    """Find .pbip files, *.SemanticModel and *.Report folders below root."""
    pbips = sorted(p for p in root.rglob("*.pbip") if p.is_file())
    models = sorted(p for p in root.rglob("*.SemanticModel") if p.is_dir())
    reports = sorted(p for p in root.rglob("*.Report") if p.is_dir())
    # Older projects use *.Dataset instead of *.SemanticModel
    models += sorted(p for p in root.rglob("*.Dataset") if p.is_dir())
    pbip_info = []
    for p in pbips:
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except ValueError:
            data = {}
        arts = []
        for a in data.get("artifacts", []) or []:
            for k, v in a.items():
                arts.append({"type": k, "path": (v or {}).get("path")})
        pbip_info.append({"file": p.relative_to(root).as_posix(), "version": data.get("version"), "artifacts": arts})
    return {"pbip": pbip_info, "models": models, "reports": reports}
