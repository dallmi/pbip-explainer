#!/usr/bin/env python3
"""Tests for the local web UI (app/server.py). Run: python3 tests/test_app.py"""
import http.client
import io
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
import server  # noqa: E402

EXAMPLE = ROOT / "examples" / "campaign-analytics"


def zip_example() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for f in EXAMPLE.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(EXAMPLE.parent).as_posix())
    return buf.getvalue()


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run([sys.executable, str(ROOT / "examples" / "generate_examples.py")], check=True, capture_output=True)
        cls.tmp = Path(tempfile.mkdtemp(prefix="pbipx-app-"))
        cls.port = server._free_port(18765)
        cls.httpd = server.make_server(cls.tmp, cls.port)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Host": f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, dict(r.getheaders()), data

    def upload(self, data: bytes, name="CampaignAnalytics.zip") -> str:
        st, _, body = self.req("POST", "/api/analyse", data, {"X-PBIPX": "1", "X-Filename": name})
        self.assertEqual(st, 202, body)
        run_id = json.loads(body)["id"]
        for _ in range(200):
            meta = json.loads(self.req("GET", f"/api/runs/{run_id}")[2])
            if meta["status"] in ("done", "error"):
                return run_id
            time.sleep(0.05)
        self.fail("analysis did not finish")

    def test_full_flow(self):
        run_id = self.upload(zip_example())
        meta = json.loads(self.req("GET", f"/api/runs/{run_id}")[2])
        self.assertEqual(meta["status"], "done")
        self.assertEqual(meta["title"], "CampaignAnalytics")
        self.assertEqual(meta["stats"]["measures"], 10)
        self.assertTrue(any(f["code"] == "broken-visual-reference" for f in meta["findings"]))
        # docs are served, the HTML carries its own restrictive CSP
        st, h, html = self.req("GET", f"/runs/{run_id}/{meta['files']['html']}")
        self.assertEqual(st, 200)
        self.assertIn(b"CampaignAnalytics.zip", html)            # original file name, not a temp name
        self.assertIn("default-src 'none'", h.get("Content-Security-Policy", ""))
        st, h, _ = self.req("GET", f"/runs/{run_id}/{meta['files']['md']}?download")
        self.assertIn("attachment", h.get("Content-Disposition", ""))
        st, _, z = self.req("GET", f"/api/runs/{run_id}/knowledge.zip")
        names = zipfile.ZipFile(io.BytesIO(z)).namelist()
        self.assertIn("knowledge/model.json", names)
        self.assertTrue(any(n.endswith(".html") for n in names))
        # names + query
        nm = json.loads(self.req("GET", f"/api/runs/{run_id}/names")[2])
        self.assertIn("Open Rate", nm["measures"])
        st, _, body = self.req("POST", f"/api/runs/{run_id}/query", json.dumps({"command": "path", "args": ["Division", "Campaign"]}),
                               {"X-PBIPX": "1", "Content-Type": "application/json"})
        self.assertIn("Engagement", json.loads(body)["text"])
        # enrichment upload rebuilds docs
        enr = {"measures": {"Total Clicks": {"description": "Clicks on links in the message."}}}
        st, _, body = self.req("POST", f"/api/runs/{run_id}/enrichment", json.dumps(enr), {"X-PBIPX": "1"})
        self.assertEqual(st, 200, body)
        html = self.req("GET", f"/runs/{run_id}/{meta['files']['html']}")[2]
        self.assertIn(b"Clicks on links in the message.", html)
        # list + delete
        self.assertTrue(any(r["id"] == run_id for r in json.loads(self.req("GET", "/api/runs")[2])))
        self.assertEqual(self.req("POST", f"/api/runs/{run_id}/delete", b"", {"X-PBIPX": "1"})[0], 200)
        self.assertEqual(self.req("GET", f"/api/runs/{run_id}")[0], 404)

    def test_package_endpoints(self):
        a = self.upload(zip_example(), "First.zip")
        b = self.upload(zip_example(), "Second.zip")
        hdr = {"X-PBIPX": "1", "Content-Type": "application/json"}
        st, _, body = self.req("POST", "/api/portfolio", json.dumps({"runs": [a, b]}), hdr)
        self.assertEqual(st, 200, body)
        pf = json.loads(body)
        self.assertEqual(pf["totals"]["models"], 2)
        self.assertTrue(pf["sharedMeasures"])                    # same model twice: every measure identical
        st, h, data = self.req("POST", "/api/package", json.dumps({"runs": [a, b], "name": "Team models"}), hdr)
        self.assertEqual(st, 200)
        self.assertIn('filename="Team models.zip"', h.get("Content-Disposition", ""))
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        self.assertIn("Team models/Knowledge/00 Catalog.docx", names)
        self.assertEqual(json.loads(h["X-Package-Manifest"])["knowledgeFiles"], 7)
        self.assertEqual(self.req("POST", "/api/package", json.dumps({"runs": []}), hdr)[0], 400)
        self.assertEqual(self.req("POST", "/api/package", json.dumps({"runs": ["../x"]}), hdr)[0], 404)
        self.assertEqual(self.req("POST", "/api/package", json.dumps({"runs": [a]}))[0], 403)   # no custom header

    def test_not_a_pbip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("readme.txt", "x")
        run_id = self.upload(buf.getvalue(), "other.zip")
        meta = json.loads(self.req("GET", f"/api/runs/{run_id}")[2])
        self.assertEqual(meta["status"], "error")
        self.assertIn("Save the file as PBIP", meta["error"])
        st, _, _ = self.req("POST", "/api/analyse", b"hello", {"X-PBIPX": "1"})
        self.assertEqual(st, 400)

    def test_guards(self):
        z = zip_example()
        self.assertEqual(self.req("POST", "/api/analyse", z)[0], 403)                                   # no custom header
        self.assertEqual(self.req("POST", "/api/analyse", z, {"X-PBIPX": "1", "Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.req("GET", "/api/runs", headers={"Host": "evil.example"})[0], 403)        # DNS rebinding
        self.assertEqual(self.req("GET", "/runs/..%2f..%2fapp/server.py")[0], 404)
        self.assertEqual(self.req("GET", "/runs/x/../../app/server.py")[0], 404)
        st, h, _ = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn("frame-ancestors 'none'", h.get("Content-Security-Policy", ""))

    def test_binds_localhost_only(self):
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")


if __name__ == "__main__":
    unittest.main(verbosity=1)
