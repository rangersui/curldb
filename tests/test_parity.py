"""The viewer builds its own SQL in the browser; curldb.query builds SQL in
Python. Both must answer the same question the same way. This runs the
viewer's query builder under node (tests/viewer_sql.mjs), executes its SQL
on the same file, and compares ids with curldb.query. Skipped without node."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest

import curldb
import test_curldb as base

EXPRS = ["", "kind=response", "status=409", "status=200,409", "method=PUT", "path=/chat", "path~/queries/",
         "header:X-Flag=true", "header:X-Verdict", "header:x-verdict=solid", "-status=409", "-header:X-Archive=true",
         "-kind=request", "body~fork", "fork", "parent=1", "kind=response header:X-Verdict=shaky", "-plain", "@conflicts"]
CRLF = "\r\n"


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ParityTests(base.TemporaryDatabase):
    def test_viewer_sql_matches_python_query(self):
        req = curldb.add("POST /chat HTTP/1.1" + CRLF + "X-Topic: fork" + CRLF + CRLF + "should we fork?")
        curldb.add("HTTP/1.1 200 OK" + CRLF + "X-Verdict: solid" + CRLF + CRLF + "fork it", parent=req)
        bad = curldb.add("HTTP/1.1 409 Conflict" + CRLF + "X-Verdict: shaky" + CRLF + CRLF + "plain words, do not fork", parent=req)
        curldb.add("GET /notes/x.md HTTP/1.1" + CRLF + CRLF)
        curldb.amend(bad, [("X-Flag", "true")])
        curldb.amend(req, [("X-Archive", "true")])
        curldb.save_query("conflicts", "status=409")
        script = Path(__file__).resolve().parent / "viewer_sql.mjs"
        out = subprocess.run(["node", str(script)], input=json.dumps({"exprs": EXPRS, "saved": curldb.saved_queries()}),
                             capture_output=True, text=True,
                             encoding="utf-8", timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        conn = curldb._connect()
        try:
            for plan in json.loads(out.stdout):
                with self.subTest(expr=plan["expr"]):
                    self.assertNotIn("error", plan, plan.get("error"))
                    python_ids = sorted(r["id"] for r in curldb.query(plan["expr"], limit=None))
                    viewer_ids = sorted(r[0] for r in conn.execute(plan["sql"].replace("LIMIT 500", ""), plan["params"]))
                    self.assertEqual(python_ids, viewer_ids)
        finally:
            conn.close()
