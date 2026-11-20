#!/usr/bin/env python3
"""pbip-explainer desktop UI: a local web app on the Python standard library.

    python app/server.py [--port 8765] [--data-dir work/ui-runs] [--no-browser]

Starts a web server on 127.0.0.1 only and opens the browser. Drop a PBIP zip on the page;
the server runs the same extraction and docs build as the CLI and serves the results.
No admin rights, no installs, nothing leaves the machine.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import io
import json
import mimetypes
import re
import shutil
import socket
import sys
import threading
import traceback
import webbrowser
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "pbip-explain" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pbipx import __version__  # noqa: E402
from pbipx import docs as _docs  # noqa: E402
from pbipx.pack import extract, load_pack  # noqa: E402
from pbipx.query import Q  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
MAX_UPLOAD = 1024 ** 3          # 1 GB zip
RUN_ID = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
QUERY_COMMANDS = {"summary", "find", "measure", "table", "column", "page", "path", "impact", "unused", "findings"}
_lock = threading.Lock()


# ---------------------------------------------------------------------------- runs

class Runs:
    def __init__(self, base: Path):
        self.base = base
        self.base.mkdir(parents=True, exist_ok=True)

    def dir(self, run_id: str) -> Path:
        if not RUN_ID.match(run_id or ""):
            raise KeyError(run_id)
        d = (self.base / run_id).resolve()
        if d.parent != self.base.resolve() or not d.is_dir():
            raise KeyError(run_id)
        return d

    def meta(self, run_id: str) -> dict:
        p = self.dir(run_id) / "run.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {"id": run_id, "status": "unknown"}

    def save_meta(self, run_id: str, meta: dict) -> None:
        with _lock:
            (self.base / run_id / "run.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")

    def new(self, filename: str) -> str:
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(filename).stem).strip("-.")[:60] or "report"
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        run_id = f"{stem}-{stamp}"
        i = 2
        while (self.base / run_id).exists():
            run_id = f"{stem}-{stamp}-{i}"
            i += 1
        (self.base / run_id).mkdir(parents=True)
        return run_id

    def list(self) -> list:
        out = []
        for d in sorted(self.base.iterdir(), reverse=True):
            if d.is_dir() and (d / "run.json").is_file():
                try:
                    m = json.loads((d / "run.json").read_text(encoding="utf-8"))
                except ValueError:
                    continue
                out.append({k: m.get(k) for k in ("id", "name", "created", "status", "title", "stats", "error")})
        return sorted(out, key=lambda m: m.get("created") or "", reverse=True)

    def delete(self, run_id: str) -> None:
        shutil.rmtree(self.dir(run_id))


def _zip_name(filename: str) -> str:
    """Safe local name for the uploaded zip; keeps the user's file name for the docs."""
    stem = re.sub(r"[^A-Za-z0-9 ._()-]+", "-", Path(filename.replace("\\", "/")).stem).strip(" .-")[:80] or "report"
    return stem + ".zip"


def analyse(runs: Runs, run_id: str, filename: str) -> None:
    """Background job: extract + docs, recording progress in run.json."""
    d = runs.base / run_id
    meta = {"id": run_id, "name": filename, "created": _dt.datetime.now().isoformat(timespec="seconds"),
            "status": "running", "step": "Unpacking and reading the project", "steps": []}

    def step(label: str):
        meta["steps"].append(meta["step"])
        meta["step"] = label
        runs.save_meta(run_id, meta)

    runs.save_meta(run_id, meta)
    try:
        extract(d / _zip_name(filename), d)
        step("Writing documentation")
        pack = load_pack(d)
        enr = None
        if (d / "enrichment.json").is_file():
            try:
                enr = json.loads((d / "enrichment.json").read_text(encoding="utf-8"))
            except ValueError:
                enr = None
        files = _docs.build(pack, d / "docs", enr)
        meta["steps"].append(meta["step"])
        meta.update(_summary(pack, d, files))
        meta["status"] = "done"
        meta["step"] = None
    except SystemExit as e:                     # the pipeline's "not a PBIP" messages
        meta.update(status="error", error=str(e))
    except Exception as e:                      # noqa: BLE001 - surface any failure in the UI
        meta.update(status="error", error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()[-3000:])
    finally:
        (d / _zip_name(filename)).unlink(missing_ok=True)   # 00-input/ keeps the unpacked project
        runs.save_meta(run_id, meta)


def _summary(pack: dict, d: Path, files: list) -> dict:
    models, reports = pack["semanticModels"], pack["reports"]
    title = reports[0]["name"] if len(reports) == 1 else (models[0]["name"] if models else pack["source"]["name"])
    stats = {"pages": sum(len(r["pages"]) for r in reports),
             "visuals": sum(len(p["visuals"]) for r in reports for p in r["pages"])}
    findings = []
    for m in models:
        st = m["analysis"]["stats"]
        for k in ("tables", "measures", "relationships", "columns", "roles"):
            stats[k] = stats.get(k, 0) + st[k]
        findings += [{"severity": f["severity"], "code": f["code"], "message": f["message"],
                      "objects": f["objects"][:8], "advice": f.get("advice")} for f in m["analysis"]["findings"]]
    rel = lambda p: Path(p).resolve().relative_to(d.resolve()).as_posix()  # noqa: E731
    out_files = {"html": None, "md": None, "svg": []}
    for f in files:
        if f.suffix == ".html":
            out_files["html"] = rel(f)
        elif f.suffix == ".md":
            out_files["md"] = rel(f)
        elif f.suffix == ".svg":
            out_files["svg"].append(rel(f))
    return {"title": title, "stats": stats, "findings": findings, "files": out_files,
            "pages": [p["displayName"] for r in reports for p in r["pages"]],
            "liveConnection": any((r.get("semanticModelRef") or {}).get("type") == "byConnection" for r in reports),
            "modelFormat": ", ".join(m["format"].upper() for m in models),
            "reportFormat": ", ".join(r["format"].upper() for r in reports)}


def names(pack: dict) -> dict:
    """Object names for autocomplete in the UI."""
    tabs = [t for m in pack["semanticModels"] for t in m["tables"] if not t.get("isAutoDateTable")]
    return {
        "measures": sorted({me["name"] for t in tabs for me in t["measures"]}, key=str.lower),
        "tables": sorted({t["name"] for t in tabs}, key=str.lower),
        "columns": sorted({f"{t['name']}[{c['name']}]" for t in tabs for c in t["columns"]}, key=str.lower),
        "pages": [p["displayName"] for r in pack["reports"] for p in r["pages"]],
    }


def knowledge_zip(d: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for sub in ("knowledge", "docs"):
            for f in sorted((d / sub).rglob("*")):
                if f.is_file():
                    zf.write(f, f.relative_to(d).as_posix())
        if (d / "enrichment.json").is_file():
            zf.write(d / "enrichment.json", "enrichment.json")
    return buf.getvalue()


# ---------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = f"pbip-explainer/{__version__}"
    runs: Runs = None  # set in main()
    port: int = 0

    def log_message(self, fmt, *args):  # quieter console: one line per request, no IP
        sys.stderr.write("  " + (fmt % args) + "\n")

    # --- guards ---------------------------------------------------------------
    def _host_ok(self) -> bool:
        """Refuse requests whose Host is not this machine (DNS rebinding)."""
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        return host in ("127.0.0.1", "localhost")

    def _write_ok(self) -> bool:
        """State-changing calls need our custom header (cross-site forms cannot send it) and our origin."""
        if self.headers.get("X-PBIPX") != "1":
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in (f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}")

    # --- responses ------------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, code: int, msg: str):
        self._json({"error": msg}, code)

    # --- routing --------------------------------------------------------------
    def do_GET(self):
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, "Use http://127.0.0.1")
        path = unquote(urlparse(self.path).path)
        if path in ("/", "/index.html"):
            body = (STATIC / "index.html").read_bytes()
            return self._send(200, body, "text/html; charset=utf-8",
                              {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                          "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'"})
        if path == "/api/info":
            return self._json({"version": __version__, "dataDir": str(self.runs.base)})
        if path == "/api/runs":
            return self._json(self.runs.list())
        m = re.match(r"^/api/runs/([^/]+)$", path)
        if m:
            try:
                return self._json(self.runs.meta(m.group(1)))
            except KeyError:
                return self._error(404, "Unknown run")
        m = re.match(r"^/api/runs/([^/]+)/names$", path)
        if m:
            try:
                return self._json(names(load_pack(self.runs.dir(m.group(1)))))
            except (KeyError, OSError, ValueError):
                return self._error(404, "Unknown run")
        m = re.match(r"^/api/runs/([^/]+)/knowledge\.zip$", path)
        if m:
            try:
                d = self.runs.dir(m.group(1))
            except KeyError:
                return self._error(404, "Unknown run")
            return self._send(200, knowledge_zip(d), "application/zip",
                              {"Content-Disposition": f'attachment; filename="{m.group(1)}-knowledge.zip"'})
        m = re.match(r"^/runs/([^/]+)/(.+)$", path)
        if m:
            return self._file(m.group(1), m.group(2))
        return self._error(404, "Not found")

    do_HEAD = do_GET

    def _file(self, run_id: str, rel: str):
        try:
            d = self.runs.dir(run_id)
        except KeyError:
            return self._error(404, "Unknown run")
        if not (rel.startswith("docs/") or rel.startswith("knowledge/") or rel == "enrichment.json"):
            return self._error(404, "Not found")
        f = (d / rel).resolve()
        if d.resolve() not in f.parents or not f.is_file():
            return self._error(404, "Not found")
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or f.suffix in (".md", ".json", ".csv", ".svg"):
            ctype = {".md": "text/markdown", ".json": "application/json", ".csv": "text/csv",
                     ".svg": "image/svg+xml"}.get(f.suffix, ctype) + "; charset=utf-8"
        extra = {}
        if "download" in (urlparse(self.path).query or ""):
            extra["Content-Disposition"] = f'attachment; filename="{f.name}"'
        if f.suffix == ".html":   # generated docs carry inline script/style only
            extra["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:"
        return self._send(200, f.read_bytes(), ctype, extra)

    def do_POST(self):
        if not self._host_ok() or not self._write_ok():
            return self._error(HTTPStatus.FORBIDDEN, "Request refused")
        path = urlparse(self.path).path
        if path == "/api/analyse":
            return self._analyse()
        m = re.match(r"^/api/runs/([^/]+)/query$", path)
        if m:
            return self._query(m.group(1))
        m = re.match(r"^/api/runs/([^/]+)/delete$", path)
        if m:
            try:
                self.runs.delete(m.group(1))
            except KeyError:
                return self._error(404, "Unknown run")
            return self._json({"ok": True})
        m = re.match(r"^/api/runs/([^/]+)/enrichment$", path)
        if m:
            return self._enrichment(m.group(1))
        return self._error(404, "Not found")

    def _enrichment(self, run_id: str):
        """Store an uploaded enrichment.json and rebuild the docs with it."""
        try:
            d = self.runs.dir(run_id)
            enr = json.loads(self._body(20 * 1024 * 1024).decode("utf-8-sig"))
        except KeyError:
            return self._error(404, "Unknown run")
        except (ValueError, UnicodeDecodeError) as e:
            return self._error(400, f"Not a valid JSON file: {e}")
        if not isinstance(enr, dict) or not any(k in enr for k in ("report", "tables", "measures", "columns", "pages", "glossary")):
            return self._error(400, "This does not look like an enrichment.json (expected keys such as report, measures, tables).")
        try:
            pack = load_pack(d)
            (d / "enrichment.json").write_text(json.dumps(enr, indent=2, ensure_ascii=False), encoding="utf-8")
            files = _docs.build(pack, d / "docs", enr)
            meta = self.runs.meta(run_id)
            meta.update(_summary(pack, d, files))
            meta["enriched"] = _dt.datetime.now().isoformat(timespec="seconds")
            self.runs.save_meta(run_id, meta)
        except Exception as e:  # noqa: BLE001
            return self._error(500, f"Rebuilding the docs failed: {type(e).__name__}: {e}")
        filled = sum(1 for sec in ("measures", "tables", "columns", "pages")
                     for v in (enr.get(sec) or {}).values()
                     if isinstance(v, dict) and any(str(x).strip() for x in v.values() if not isinstance(x, list)))
        return self._json({"ok": True, "described": filled, "run": meta})

    def _body(self, limit: int) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > limit:
            raise ValueError(f"Upload must be between 1 byte and {limit // (1024 * 1024)} MB")
        data = bytearray()
        while len(data) < n:
            chunk = self.rfile.read(min(1 << 20, n - len(data)))
            if not chunk:
                break
            data += chunk
        return bytes(data)

    def _analyse(self):
        filename = unquote(self.headers.get("X-Filename") or "report.zip")
        try:
            data = self._body(MAX_UPLOAD)
        except ValueError as e:
            return self._error(413, str(e))
        if not data.startswith(b"PK"):
            return self._error(400, "That is not a zip file. In Power BI Desktop use File > Save as > "
                                    "Power BI project (.pbip), then zip the project folder.")
        run_id = self.runs.new(filename)
        (self.runs.base / run_id / _zip_name(filename)).write_bytes(data)
        threading.Thread(target=analyse, args=(self.runs, run_id, filename), daemon=True).start()
        return self._json({"id": run_id}, 202)

    def _query(self, run_id: str):
        try:
            d = self.runs.dir(run_id)
            req = json.loads(self._body(64 * 1024).decode("utf-8"))
        except KeyError:
            return self._error(404, "Unknown run")
        except ValueError:
            return self._error(400, "Bad request")
        cmd = str(req.get("command", ""))
        args = [str(a) for a in (req.get("args") or []) if str(a).strip()]
        if cmd not in QUERY_COMMANDS:
            return self._error(400, "Unknown command")
        try:
            q = Q(load_pack(d))
            if not q.model and cmd not in ("summary", "find", "page"):
                return self._json({"text": "This package has no semantic model (live connection), so only "
                                           "summary, find and page work."})
            if cmd == "path":
                if len(args) != 2:
                    return self._json({"text": "path needs two table names."})
                text = q.path(args[0], args[1])
            elif cmd in ("summary", "unused", "findings"):
                text = getattr(q, cmd)()
            else:
                if not args:
                    return self._json({"text": f"{cmd} needs a name."})
                text = getattr(q, cmd)(" ".join(args))
        except SystemExit as e:                  # 'no measure named ...' and similar
            text = str(e)
        except Exception as e:                   # noqa: BLE001
            text = f"{type(e).__name__}: {e}"
        return self._json({"text": text})


def _free_port(preferred: int) -> int:
    for port in [preferred] + list(range(preferred + 1, preferred + 20)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_server(data_dir: Path, port: int) -> ThreadingHTTPServer:
    Handler.runs = Runs(data_dir)
    Handler.port = port
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    return httpd


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="pbip-explainer local UI")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--data-dir", type=Path, default=ROOT / "work" / "ui-runs",
                    help="where analysed projects are kept (default: work/ui-runs, git-ignored)")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    if sys.version_info < (3, 9):
        print("Python 3.9 or newer is needed.")
        return 1
    port = _free_port(a.port)
    httpd = make_server(a.data_dir.resolve(), port)
    url = f"http://127.0.0.1:{port}/"
    print(f"pbip-explainer {__version__} is running at {url}", flush=True)
    print(f"Results are kept in {a.data_dir.resolve()}", flush=True)
    print("Close this window (or press Ctrl+C) to stop.", flush=True)
    if not a.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
