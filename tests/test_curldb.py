"""Run with: python -m unittest discover -s tests -v."""
from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import curldb


SCRIPT = Path(curldb.__file__).resolve()
RAW = "HTTP/1.1 409 Conflict\r\nX-Scope: design\r\n\r\n  协议设计\r\n尾行  \r\n"
NOTE = """---
title: 'A note'
tags: [rust, iot]
metadata:
  type: feedback
links:
  - first
  - second
desc: |
  first line
  second line
---
笔记中的协议设计
"""


class EnvelopeTests(unittest.TestCase):
    def test_response_keeps_duplicate_headers_and_ignores_malformed_lines(self):
        parsed = curldb.parse_envelope(
            "HTTP/1.1 409 Conflict\r\nX-Tag: a\r\nmalformed\r\nX-Tag: b\r\n\r\nhello"
        )
        self.assertEqual((parsed["kind"], parsed["status"]), ("response", 409))
        self.assertEqual(parsed["headers"], [("X-Tag", "a"), ("X-Tag", "b")])
        self.assertEqual(parsed["body"], "hello")

    def test_http_path_comes_from_envelope(self):
        request = curldb.parse_envelope("POST /chat?q=1 HTTP/1.1\n\nhello", "file.txt")
        self.assertEqual((request["kind"], request["method"], request["path"]),
                         ("request", "POST", "/chat?q=1"))
        self.assertIsNone(curldb.parse_envelope(RAW, "file.txt")["path"])

    def test_front_matter_flattens_lists_nested_keys_and_blocks(self):
        parsed = curldb.parse_envelope(NOTE, "vault\\note.md")
        self.assertEqual((parsed["kind"], parsed["path"]), ("note", "vault/note.md"))
        self.assertEqual(parsed["headers"], [
            ("title", "A note"), ("tags", "rust"), ("tags", "iot"),
            ("metadata.type", "feedback"), ("links", "first"), ("links", "second"),
            ("desc", "first line second line"),
        ])
        self.assertEqual(parsed["body"], "笔记中的协议设计")

    def test_plain_text_and_unclosed_front_matter_remain_searchable(self):
        for text in ("first paragraph\n\nsecond paragraph", "---\ntitle: unfinished"):
            with self.subTest(text=text):
                parsed = curldb.parse_envelope(text, "note.txt")
                self.assertEqual(parsed["kind"], "raw")
                self.assertEqual(parsed["path"], "note.txt")
                self.assertEqual(parsed["body"], text)

    def test_wrap_preserves_body_and_respects_supplied_date(self):
        body = "  中文\r\nsecond\rthird\n\n"
        for start, first in (("200", "HTTP/1.1 200 OK"),
                             ("POST /chat", "POST /chat HTTP/1.1")):
            with self.subTest(start=start):
                wrapped = curldb.wrap(start, body, [("date", "fixed"), ("X-Tag", "a")])
                head, actual_body = wrapped.split("\n\n", 1)
                self.assertEqual(head.splitlines(), [first, "date: fixed", "X-Tag: a"])
                self.assertEqual(actual_body, body)
        self.assertIn("\nDate: ", curldb.wrap("200", body, []))


class TemporaryDatabase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="curldb-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.db = self.directory / "session.sqlite"
        environment = patch.dict(os.environ, {"CURLDB_PATH": str(self.db)})
        environment.start()
        self.addCleanup(environment.stop)


class DatabaseTests(TemporaryDatabase):
    def test_raw_roundtrip_and_timestamp(self):
        rid = curldb.add(RAW, ts=123.0)
        self.assertEqual(curldb.get(rid), RAW.encode("utf-8"))
        self.assertEqual(curldb.ls()[0]["ts"], 123.0)
        self.assertIsNone(curldb.get(rid + 1))

    def test_combined_query_uses_and_and_deduplicates_records(self):
        rid = curldb.add("HTTP/1.1 409 Conflict\nX-Tag: a\nX-Tag: a\n\ntimeout")
        curldb.add("HTTP/1.1 200 OK\nX-Tag: a\n\ntimeout")
        curldb.add("HTTP/1.1 409 Conflict\nX-Tag: b\n\ntimeout")
        matches = curldb.query("kind=response status=409,500 header:x-tag=a body~timeout")
        self.assertEqual([r["id"] for r in matches], [rid])
        self.assertEqual(curldb.query("status=409 header:X-Tag=a body~missing"), [])

    def test_requests_and_notes_share_header_queries(self):
        request = curldb.add(curldb.wrap("POST /tool/Read", "read a note", [("X-Tool", "Read")]))
        note = curldb.add(NOTE, source_path="vault/note.md")
        queries = {
            "kind=request method=post path=/tool/Read header:X-Tool": request,
            "path~/tool/ header:X-Tool=Read": request,
            "kind=note header:tags=iot header:metadata.type=feedback": note,
            "path=vault/note.md": note,
        }
        for expr, rid in queries.items():
            with self.subTest(expr=expr):
                self.assertEqual([r["id"] for r in curldb.query(expr)], [rid])
        self.assertEqual(curldb.tags("tags"), {"tags": [("iot", 1), ("rust", 1)]})
        self.assertIn(("metadata.type", "feedback"), curldb.headers_of(note))

    def test_cjk_short_terms_and_english_phrases(self):
        rid = curldb.add("协议设计支持中文搜索\n\nexact phrase and timeout")
        curldb.add("unrelated text")
        for expr in ("body~协议", "body~协议设计", 'body~"exact phrase"', "timeout"):
            with self.subTest(expr=expr):
                self.assertEqual([r["id"] for r in curldb.query(expr)], [rid])

    def test_unicode61_fallback_searches_cjk(self):
        connect = sqlite3.connect

        class WithoutTrigram(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if "tokenize='trigram'" in sql:
                    raise sqlite3.OperationalError("no such tokenizer: trigram")
                return super().execute(sql, *args, **kwargs)

        with patch("curldb.sqlite3.connect", side_effect=lambda path: connect(path, factory=WithoutTrigram)):
            rid = curldb.add("协议设计 supports exact phrase")
            self.assertFalse(curldb.stats()["trigram"])
            for expr in ("body~协议设计", 'body~"exact phrase"'):
                self.assertEqual([r["id"] for r in curldb.query(expr)], [rid])

    def test_legacy_layout_migrates_raw_and_timestamps(self):
        conn = sqlite3.connect(self.db)
        try:
            conn.executescript("""
                CREATE TABLE responses (id INTEGER PRIMARY KEY, raw TEXT, ts REAL);
                CREATE TABLE headers (response_id INTEGER, name TEXT, value TEXT);
            """)
            conn.execute("INSERT INTO responses VALUES (1, ?, ?)", (RAW, 123.0))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(curldb.get(1), RAW.encode("utf-8"))
        self.assertEqual(curldb.query("status=409 header:X-Scope=design")[0]["ts"], 123.0)
        self.assertEqual(curldb.stats()["count"], 1)

    def test_limit_and_separate_session_files(self):
        first = curldb.add("first")
        second = curldb.add("second")
        self.assertEqual([r["id"] for r in curldb.ls(1)], [second])
        with patch.dict(os.environ, {"CURLDB_PATH": str(self.directory / "other.sqlite")}):
            self.assertEqual(curldb.ls(), [])
        self.assertEqual(curldb.get(first), b"first")


class Target(BaseHTTPRequestHandler):
    """A server to replay against: echoes the body, chunked on /chunked."""
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.seen.append((self.requestline, dict(self.headers), body))
        if self.path.endswith("/hop"):
            self.send_response(200)
            self.send_header("Connection", "close, X-Internal")
            self.send_header("X-Internal", "secret")
            self.send_header("X-Public", "yes")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if self.path.endswith("/nobody"):
            self.send_response(204)
            self.end_headers()
            return
        if self.path.endswith("/notmodified"):
            self.send_response(304)
            self.send_header("ETag", '"v7"')
            self.send_header("Content-Length", "99")
            self.end_headers()
            return
        if self.path.endswith("/chunked"):
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for piece in (b"echo ", body):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
            self.wfile.write(b"0\r\n\r\n")
            return
        reply = b"got: " + body
        self.send_response(201)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    do_GET = do_POST
    do_HEAD = do_POST
    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST

    def log_message(self, fmt, *args):
        pass


def start_target(case):
    """A Target server for the test, stopped at cleanup; sets case.target
    and case.authority."""
    case.target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    case.target.seen = []
    case.target.daemon_threads = True
    case.target.handle_error = lambda *args: None   # a peer hanging up early is not news
    thread = threading.Thread(target=case.target.serve_forever, daemon=True)
    thread.start()
    case.addCleanup(thread.join, 5)
    case.addCleanup(case.target.server_close)
    case.addCleanup(case.target.shutdown)
    case.authority = "127.0.0.1:%d" % case.target.server_address[1]


class ReplayTests(TemporaryDatabase):
    def setUp(self):
        super().setUp()
        start_target(self)

    def stored(self, host, extra="", body="hello"):
        return curldb.add(f"POST /chat HTTP/1.1\nHost: {host}\nX-Topic: fork{extra}\n\n{body}".encode())

    def test_unchanged_request_is_sent_as_it_is_and_answered(self):
        rid = self.stored(self.authority, "\nContent-Length: 5")
        out = curldb.replay(rid)
        self.assertEqual(out["status"], 201)
        self.assertEqual(out["request"], rid)
        self.assertEqual(out["response"], rid + 1)
        self.assertTrue(out["raw"].startswith(b"HTTP/1.1 201 Created\r\n"))
        self.assertTrue(out["raw"].endswith(b"\r\n\r\ngot: hello"))
        line, headers, body = self.target.seen[0]
        self.assertEqual((line, headers["Host"], headers["X-Topic"], body), ("POST /chat HTTP/1.1", self.authority, "fork", b"hello"))
        row = curldb.get_row(rid + 1)
        self.assertEqual((row["kind"], row["status"], row["parent"], row["raw"]), ("response", 201, rid, out["raw"]))
        self.assertEqual(curldb.stats()["count"], 2)

    def test_host_and_header_changes_store_the_request_that_went_out(self):
        rid = self.stored("api.example.com")
        out = curldb.replay(rid, host="http://" + self.authority, headers=[("Authorization", "Bearer new"), ("X-Topic", "")])
        line, headers, body = self.target.seen[0]
        self.assertEqual(headers["Host"], self.authority)
        self.assertEqual(headers["Authorization"], "Bearer new")
        self.assertNotIn("X-Topic", headers)
        self.assertEqual(body, b"hello")   # Content-Length was added for the body
        sent = curldb.get_row(out["request"])
        self.assertEqual((sent["kind"], sent["parent"]), ("request", rid))
        self.assertIn(b"\r\nAuthorization: Bearer new\r\n", sent["raw"])
        self.assertIn(b"Content-Length: 5", sent["raw"])
        self.assertNotIn(b"X-Topic", sent["raw"])
        self.assertEqual(curldb.get_row(out["response"])["parent"], out["request"])
        self.assertEqual([r["id"] for r in curldb.query(f"parent={rid}")], [out["request"]])

    def test_setting_a_header_to_its_own_value_is_not_a_change(self):
        rid = self.stored(self.authority, "\nContent-Length: 5")
        out = curldb.replay(rid, host="http://" + self.authority, headers=[("X-Topic", "fork")])
        self.assertEqual(out["request"], rid)
        self.assertEqual(curldb.stats()["count"], 2)
        self.assertEqual(self.target.seen[0][1]["X-Topic"], "fork")
        # A replaced header keeps its place; a new one goes last.
        out = curldb.replay(rid, headers=[("X-Topic", "spoon"), ("X-New", "1")])
        sent = curldb.get_row(out["request"])["raw"]
        self.assertTrue(sent.startswith(b"POST /chat HTTP/1.1\r\nHost: " + self.authority.encode() + b"\r\nX-Topic: spoon\r\nContent-Length: 5\r\nX-New: 1\r\n\r\nhello"))

    def test_replayed_traffic_is_that_hosts_business(self):
        target = curldb.add(b"HTTP/1.1 200 OK\r\nX-Verdict: solid\r\n\r\nx")
        foreign = curldb.add(f"PATCH /{target} HTTP/1.1\r\nHost: api.example.com\r\nX-Verdict: shaky\r\n\r\n".encode(), host="api.example.com")
        self.assertEqual(curldb.get_row(foreign)["host"], "api.example.com")
        self.assertIsNone(curldb.get_row(foreign)["parent"])
        self.assertEqual(dict(curldb.headers_of(target)), {"X-Verdict": "solid"})
        out = curldb.replay(foreign, host="http://" + self.authority)
        sent, rep = curldb.get_row(out["request"]), curldb.get_row(out["response"])
        self.assertEqual((sent["method"], sent["path"], sent["host"], sent["parent"]), ("PATCH", f"/{target}", self.authority, foreign))
        self.assertEqual((rep["host"], rep["parent"]), (self.authority, out["request"]))
        self.assertEqual(dict(curldb.headers_of(target)), {"X-Verdict": "solid"})
        self.assertEqual(self.target.seen[0][0], f"PATCH /{target} HTTP/1.1")

    def test_chunked_response_is_stored_joined(self):
        rid = curldb.add(f"POST /chunked HTTP/1.1\r\nHost: {self.authority}\r\nContent-Length: 5\r\n\r\nhello".encode())
        out = curldb.replay(rid)
        self.assertEqual(out["status"], 200)
        raw = curldb.get(out["response"])
        self.assertNotIn(b"Transfer-Encoding", raw)
        self.assertIn(b"\r\nContent-Length: 10\r\n", raw)
        self.assertTrue(raw.endswith(b"\r\n\r\necho hello"))
        self.assertEqual(curldb.get_row(out["response"])["parent"], rid)

    def test_no_save_and_refusals(self):
        rid = self.stored(self.authority, "\nContent-Length: 5")
        out = curldb.replay(rid, save=False)
        self.assertIsNone(out["response"])
        self.assertEqual(out["status"], 201)
        self.assertEqual(curldb.stats()["count"], 1)
        rep = curldb.add(b"HTTP/1.1 200 OK\r\n\r\nx")
        with self.assertRaisesRegex(ValueError, "is a response"):
            curldb.replay(rep)
        with self.assertRaises(LookupError):
            curldb.replay(999)
        bare = curldb.add(b"GET /x HTTP/1.1\r\n\r\n")
        with self.assertRaisesRegex(ValueError, "no Host"):
            curldb.replay(bare)
        with self.assertRaisesRegex(ValueError, "not an address"):
            curldb.replay(bare, host="http://")
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            closed = s.getsockname()[1]
        with self.assertRaises(OSError):
            curldb.replay(bare, host=f"127.0.0.1:{closed}", timeout=5)
        self.assertEqual(curldb.stats()["count"], 3)

    def test_header_edits_are_checked_before_they_reach_the_wire(self):
        rid = self.stored(self.authority, "\nContent-Length: 5")
        with self.assertRaisesRegex(ValueError, "one line"):
            curldb.replay(rid, headers=[("X-Note", "a" + chr(13) + chr(10) + "X-Evil: yes")])
        with self.assertRaisesRegex(ValueError, "not a header name"):
            curldb.replay(rid, headers=[("bad name", "x")])
        with self.assertRaisesRegex(ValueError, "not an address"):
            curldb.replay(rid, host="127.0.0.1:1" + chr(10) + "X: y")
        self.assertEqual(self.target.seen, [])
        self.assertEqual(curldb.stats()["count"], 1)

    def test_changed_request_is_stored_before_a_failed_exchange(self):
        # A peer that takes the request and hangs up without answering.
        import socket
        gate = socket.socket()
        gate.bind(("127.0.0.1", 0))
        gate.listen(1)
        self.addCleanup(gate.close)
        received = []

        def take_and_drop():
            conn, _ = gate.accept()
            received.append(conn.recv(4096))
            conn.close()
        threading.Thread(target=take_and_drop, daemon=True).start()
        rid = self.stored("api.example.com", "\nContent-Length: 5")
        with self.assertRaisesRegex(ValueError, f"#{rid + 1} replay failed, remote outcome unknown, no response stored: connection closed before a response"):
            curldb.replay(rid, host="127.0.0.1:%d" % gate.getsockname()[1], timeout=5)
        self.assertEqual(curldb.get_row(rid + 1)["parent"], rid)
        self.assertEqual(curldb.get_row(rid + 1)["raw"], received[0])
        self.assertEqual(curldb.stats()["count"], 2)
        self.assertEqual(curldb.query(f"parent={rid + 1}"), [])
        # An unchanged request that cannot be delivered stores nothing.
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            closed = s.getsockname()[1]
        rid2 = self.stored("127.0.0.1:%d" % closed, "\nContent-Length: 5")
        with self.assertRaises(OSError):
            curldb.replay(rid2, timeout=5)
        self.assertEqual(curldb.stats()["count"], 3)

    def test_bad_response_framing_is_refused(self):
        import socket

        def peer(reply):
            gate = socket.socket()
            gate.bind(("127.0.0.1", 0))
            gate.listen(1)
            self.addCleanup(gate.close)

            def serve():
                conn, _ = gate.accept()
                conn.recv(4096)
                conn.sendall(reply)
                conn.close()
            threading.Thread(target=serve, daemon=True).start()
            return "127.0.0.1:%d" % gate.getsockname()[1]
        rid = self.stored("x", "\nContent-Length: 5")
        cases = [
            (b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nContent-Length: 3\r\n\r\nabc", "conflicting Content-Length"),
            (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip, chunked\r\nContent-Length: 3\r\n\r\nabc", "transfer coding gzip, chunked"),
            (b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\n\r\nabc", "ended after 3 of 9"),
            (b"HTTP/1.1 200 OK\r\nContent-Length: x\r\n\r\nabc", "not a number"),
        ]
        for reply, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    curldb.replay(rid, host=peer(reply), save=False, timeout=5)
        self.assertEqual(curldb.stats()["count"], 1)

    def test_cli_replays_one_and_by_query(self):
        first = self.stored(self.authority, "\nContent-Length: 5")
        second = self.stored(self.authority, "\nContent-Length: 5", "world")
        out = subprocess.run([sys.executable, str(SCRIPT), "replay", str(first)], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), f"#{second + 1} 201 re #{first}")
        out = subprocess.run([sys.executable, str(SCRIPT), "replay", "--query", "header:X-Topic=fork"], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split(), [f"#{second + 2}", "201", "re", f"#{first}", f"#{second + 3}", "201", "re", f"#{second}"])
        out = subprocess.run([sys.executable, str(SCRIPT), "replay", str(second + 1)], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(out.returncode, 1)
        self.assertIn("is a response", out.stderr)
        out = subprocess.run([sys.executable, str(SCRIPT), "replay", str(first), "--no-save"], capture_output=True)
        self.assertTrue(out.stdout.endswith(b"got: hello"))
        self.assertEqual(curldb.stats()["count"], 5)


class CLITests(TemporaryDatabase):
    def cli(self, *args, data=None, check=True):
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), *args], input=data,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.directory, timeout=10,
        )
        if check:
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        return result

    def test_add_get_and_wrap_roundtrip_bytes(self):
        png = b"\x89PNG\r\n\x1a\n\x00\x00\xff\xfe"
        self.assertEqual(self.cli("add", data=png).stdout.strip(), b"#1 raw +0 headers")
        self.assertEqual(self.cli("get", "1").stdout, png)
        path = Path(self.directory) / "shot.png"
        path.write_bytes(png)
        self.assertTrue(self.cli("add", str(path)).stdout.startswith(b"#2 raw "))
        self.assertEqual(self.cli("get", "2").stdout, png)
        wrapped = self.cli("wrap", "POST /shot.png", "-H", "Content-Type:image/png", data=png).stdout
        self.assertTrue(wrapped.startswith(b"POST /shot.png HTTP/1.1\n"))
        self.assertTrue(wrapped.endswith(b"\n\n" + png))
        self.assertEqual(self.cli("add", data=wrapped).stdout.strip(), b"#3 POST /shot.png +2 headers")
        self.assertEqual(self.cli("get", "3").stdout, wrapped)

    def test_stdin_and_file_roundtrip_preserve_utf8_and_line_endings(self):
        for ending in ("\n", "\r\n", "\r"):
            data = ("  中文" + ending + "second  " + ending).encode("utf-8")
            for source in ("stdin", "file"):
                with self.subTest(ending=repr(ending), source=source):
                    if source == "file":
                        path = self.directory / "input.txt"
                        path.write_bytes(data)
                        added = self.cli("add", str(path))
                    else:
                        added = self.cli("add", data=data)
                    rid = added.stdout.split()[0][1:].decode()
                    self.assertEqual(self.cli("get", rid).stdout, data)

    def test_wrap_add_get_pipeline_preserves_body(self):
        data = "  中文\r\nsecond\rthird\n\n".encode("utf-8")
        wrapped = self.cli("wrap", "POST /chat", "-H", "X-Topic:test", data=data).stdout
        self.assertEqual(wrapped.split(b"\n\n", 1)[1], data)
        rid = self.cli("add", data=wrapped).stdout.split()[0][1:].decode()
        self.assertEqual(self.cli("get", rid).stdout, wrapped)
        self.assertIn(b"POST /chat", self.cli("query", "header:X-Topic=test").stdout)

    def test_db_flag_overrides_environment(self):
        other = self.directory / "explicit.sqlite"
        self.cli("--db", str(other), "add", data=b"hello")
        self.assertTrue(other.is_file())
        self.assertFalse(self.db.exists())

    def test_help_and_errors(self):
        self.assertIn(b"HTTP exchange datastore", self.cli("--help").stdout)
        self.assertFalse(self.db.exists())
        for args, data, error in (
            (("add",), b" \n", b"empty input"),
            (("get", "999"), None, b"not found"),
            (("--db",), None, b"--db needs a path"),
        ):
            with self.subTest(args=args):
                result = self.cli(*args, data=data, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(error, result.stderr)


class Door(TemporaryDatabase):
    """A curldb door on a free port; serve_kwargs() picks its mode."""

    def serve_kwargs(self):
        return {}

    def setUp(self):
        super().setUp()
        ready = queue.Queue()

        def create_server(address, handler):
            handler.log_message = lambda *args: None
            server = ThreadingHTTPServer(address, handler)
            ready.put(server)
            return server

        self.thread = threading.Thread(target=curldb.serve, args=(0,), kwargs=self.serve_kwargs(), daemon=True)
        with patch("http.server.ThreadingHTTPServer", side_effect=create_server):
            self.thread.start()
            self.server = ready.get(timeout=5)
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive(), "HTTP server did not stop")

    def open_snapshot(self, data):
        path = os.path.join(self.directory, "snapshot-%d.sqlite" % len(os.listdir(self.directory)))
        with open(path, "wb") as f:
            f.write(data)
        return sqlite3.connect(path)

    def request(self, method, path, body=None, headers=None):
        client = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            client.request(method, path, body=body, headers=headers or {})
            response = client.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            client.close()

class UpstreamTests(Door):
    def serve_kwargs(self):
        start_target(self)
        return {"upstream": "http://%s/v1" % self.authority}

    def test_requests_go_upstream_and_both_sides_are_stored(self):
        status, headers, body = self.request("POST", "/chat", b"hello", {"X-Topic": "fork", "Connection": "close", "Host": "localhost"})
        self.assertEqual((status, body), (201, b"got: hello"))
        self.assertEqual(headers["Via"], "1.1 curldb")
        self.assertEqual(headers["Content-Type"], "text/plain")
        line, seen, sent = self.target.seen[0]
        self.assertEqual(line, "POST /v1/chat HTTP/1.1")
        self.assertEqual((seen["Host"], seen["X-Topic"], sent), (self.authority, "fork", b"hello"))
        self.assertNotIn("Connection", seen)
        req, rep = curldb.get_row(1), curldb.get_row(2)
        self.assertEqual((req["kind"], req["method"], req["path"], req["parent"]), ("request", "POST", "/v1/chat", None))
        self.assertTrue(req["raw"].startswith(b"POST /v1/chat HTTP/1.1\r\nHost: " + self.authority.encode() + b"\r\n"))
        self.assertIn(b"\r\nContent-Length: 5\r\n\r\nhello", req["raw"])
        self.assertNotIn(b"Connection", req["raw"])
        self.assertEqual((rep["kind"], rep["status"], rep["parent"]), ("response", 201, 1))
        self.assertTrue(rep["raw"].startswith(b"HTTP/1.1 201 Created\r\n"))
        self.assertTrue(rep["raw"].endswith(b"got: hello"))
        self.assertNotIn(b"Via", rep["raw"])   # Via is added on the way back, the stored answer is the upstream's
        self.assertEqual(curldb.stats()["count"], 2)

    def test_chunked_answers_and_the_doors_own_paths_go_upstream_too(self):
        status, headers, body = self.request("GET", "/chunked")
        self.assertEqual((status, body), (200, b"echo "))
        self.assertEqual(headers["Content-Length"], "5")
        self.assertNotIn("Transfer-Encoding", headers)
        rep = curldb.get_row(2)["raw"]
        self.assertNotIn(b"Transfer-Encoding", rep)
        self.assertIn(b"\r\nContent-Length: 5\r\n", rep)
        status, headers, body = self.request("GET", "/db")
        self.assertEqual((status, body), (201, b"got: "))   # not the snapshot: this port is the gateway
        self.assertEqual(self.target.seen[1][0], "GET /v1/db HTTP/1.1")
        status, headers, body = self.request("HEAD", "/chat")
        self.assertEqual((status, body), (201, b""))
        self.assertEqual(headers["Content-Length"], "5")
        self.assertEqual(curldb.stats()["count"], 6)

    def test_connection_named_headers_stop_at_the_gateway_both_ways(self):
        status, headers, body = self.request("GET", "/hop", None, {"Connection": "close, X-Secret", "X-Secret": "1", "X-Keep": "2"})
        self.assertEqual((status, body), (200, b"ok"))
        self.assertNotIn("X-Internal", headers)
        self.assertEqual(headers["X-Public"], "yes")
        line, seen, _ = self.target.seen[0]
        self.assertNotIn("X-Secret", seen)
        self.assertEqual(seen["X-Keep"], "2")
        self.assertNotIn(b"X-Secret", curldb.get(1))
        self.assertIn(b"X-Internal: secret", curldb.get(2))   # stored as the upstream sent it

    def test_answers_without_a_body_keep_their_own_length_rules(self):
        status, headers, body = self.request("GET", "/nobody")
        self.assertEqual((status, body), (204, b""))
        self.assertNotIn("Content-Length", headers)
        status, headers, body = self.request("HEAD", "/nobody")
        self.assertEqual((status, body), (204, b""))
        self.assertNotIn("Content-Length", headers)
        status, headers, body = self.request("GET", "/notmodified")
        self.assertEqual((status, body), (304, b""))
        self.assertEqual((headers["ETag"], headers["Content-Length"]), ('"v7"', "99"))
        status, headers, body = self.request("POST", "/chat", b"hello")   # the connection is still usable
        self.assertEqual((status, body), (201, b"got: hello"))
        self.assertEqual(curldb.stats()["count"], 8)

    def test_upgrades_are_refused_and_nothing_is_stored(self):
        for extra in ({"Upgrade": "websocket", "Connection": "Upgrade"}, {"Connection": "keep-alive, upgrade"}):
            status, headers, body = self.request("GET", "/chat", None, extra)
            self.assertEqual(status, 501)
            self.assertIn(b"Upgrade is not forwarded", body)
        self.assertEqual(self.target.seen, [])
        self.assertEqual(curldb.stats()["count"], 0)

    def test_gateway_traffic_carries_the_host_and_is_never_this_files_bookkeeping(self):
        rows = [curldb.get_row(i) for i in (self.request("POST", "/chat", b"hello") and (1, 2))]
        self.assertEqual([r["host"] for r in rows], [self.authority, self.authority])

    def test_unreachable_upstream_is_502_and_the_request_stays(self):
        self.target.shutdown()
        self.target.server_close()
        status, headers, body = self.request("POST", "/chat", b"hello")
        self.assertEqual(status, 502)
        self.assertTrue(body.startswith(b"#1 stored; upstream http://" + self.authority.encode()))
        self.assertEqual(headers["Via"], "1.1 curldb")
        self.assertEqual(curldb.stats()["count"], 1)
        self.assertEqual(curldb.get_row(1)["kind"], "request")
        self.assertEqual(curldb.query("parent=1"), [])

    def test_bad_upstream_addresses_are_refused(self):
        for bad in ("ftp://x", "http://", "http://a b", "http://x/p q"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    curldb._parse_upstream(bad)
        self.assertEqual(curldb._parse_upstream("api.example.com"), ("http://api.example.com", "api.example.com", ""))
        self.assertEqual(curldb._parse_upstream("https://h:8443/v1/"), ("https://h:8443", "h:8443", "/v1"))
        out = subprocess.run([sys.executable, str(SCRIPT), "serve", "0", "--upstream", "ftp://x"], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(out.returncode, 1)
        self.assertIn("not an upstream address", out.stderr)


class GatewayWithoutPrefixTests(Door):
    def serve_kwargs(self):
        start_target(self)
        return {"upstream": self.authority}

    def test_captured_patch_and_put_queries_are_the_upstreams_business(self):
        rid = curldb.add(RAW)
        curldb.save_query("mine", "status=409")
        status, headers, body = self.request("PATCH", f"/{rid}", b"", {"X-Flag": "true", "X-Scope": ""})
        self.assertEqual(status, 201)   # the upstream's answer, not the door's
        status, headers, body = self.request("PUT", "/queries/mine", b"status=200")
        self.assertEqual(status, 201)
        status, headers, body = self.request("DELETE", "/queries/mine", b"")
        self.assertEqual(status, 201)
        self.assertEqual(self.target.seen[0][0], f"PATCH /{rid} HTTP/1.1")
        self.assertEqual(dict(curldb.headers_of(rid)), {"X-Scope": "design"})
        self.assertEqual(len(curldb.header_history(rid)), 1)
        self.assertEqual(curldb.saved_queries(), {"mine": "status=409"})
        self.assertEqual(curldb.query("@mine")[0]["id"], rid)
        patch = curldb.get_row(3)
        self.assertEqual((patch["method"], patch["path"], patch["host"], patch["parent"]), ("PATCH", f"/{rid}", self.authority, None))
        self.assertEqual(curldb.query(f"parent={rid}"), [])
        self.assertEqual(curldb.stats()["count"], 8)


class HTTPTests(Door):
    def test_write_methods_archive_requests_including_delete(self):
        body = RAW.encode("utf-8")
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                status, headers, _ = self.request(method, "/review", body, {"X-Scope": "design"})
                self.assertEqual(status, 201)
                status, headers, stored = self.request("GET", headers["Location"])
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], "message/http")
                self.assertEqual(int(headers["Content-Length"]), len(stored))
                self.assertTrue(stored.startswith(f"{method} /review HTTP/1.1\n".encode()))
                self.assertEqual(stored.split(b"\n\n", 1)[1], body)
        self.assertEqual(curldb.stats()["count"], 4)
        self.assertEqual(curldb.query("kind=response"), [])

    def test_non_ascii_header_values_are_stored_as_utf8(self):
        # http.server hands header values to us decoded as latin-1; the
        # stored envelope must carry the UTF-8 text the client sent.
        topic = "协议"
        body = "中文 body".encode("utf-8")
        status, headers, _ = self.request(
            "POST", "/chat", body, {"X-Topic": topic.encode("utf-8")})
        self.assertEqual(status, 201)
        _, _, stored = self.request("GET", headers["Location"])
        text = stored.decode("utf-8")
        self.assertIn(f"X-Topic: {topic}\n", text)
        self.assertTrue(text.endswith("中文 body"))
        self.assertEqual(curldb.query(f"header:X-Topic={topic}")[0]["path"], "/chat")

    def test_query_tags_and_head(self):
        rid = curldb.add(RAW)
        path = "/?" + urlencode({"q": "status=409 header:X-Scope=design body~协议"})
        status, _, data = self.request("GET", path)
        self.assertEqual(status, 200)
        self.assertIn("协议设计".encode(), data)
        for path in ("/", f"/{rid}", "/tags", "/tags/X-Scope"):
            with self.subTest(path=path):
                status, headers, data = self.request("GET", path)
                head_status, head_headers, head_data = self.request("HEAD", path)
                self.assertEqual((status, head_status), (200, 200))
                self.assertEqual(head_data, b"")
                self.assertEqual(head_headers["Content-Length"], str(len(data)))
                self.assertEqual(headers["Content-Type"], head_headers["Content-Type"])
        self.assertIn(b"design(1)", self.request("GET", "/tags/X-Scope")[2])

    def test_binary_bodies_are_stored_as_bytes(self):
        png = b"\x89PNG\r\n\x1a\n\x00\x00\xff\xfe"
        status, headers, _ = self.request("POST", "/shot.png", png, {"Content-Type": "image/png"})
        self.assertEqual(status, 201)
        rid = int(headers["Location"].lstrip("/"))
        status, got, body = self.request("GET", headers["Location"])
        self.assertEqual((status, got["Content-Type"], got["X-Kind"]), (200, "message/http", "request"))
        self.assertTrue(body.startswith(b"POST /shot.png HTTP/1.1\n"))
        self.assertTrue(body.endswith(b"\n\n" + png))
        self.assertEqual(curldb.query("header:Content-Type=image/png")[0]["id"], rid)
        self.assertEqual(curldb.query("method=POST")[0]["preview"], "")
        message = b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\n\r\n" + png
        status, headers, _ = self.request("POST", "/", message, {"Content-Type": "message/http"})
        self.assertEqual(status, 201)
        status, got, body = self.request("GET", headers["Location"])
        self.assertEqual((got["X-Kind"], got["X-Status"], body), ("response", "200", message))
        rid = curldb.add(png, source_path="shot.png")
        status, got, body = self.request("GET", f"/{rid}")
        self.assertEqual((got["Content-Type"], got["X-Kind"], body), ("application/octet-stream", "raw", png))
        # Low bytes are valid UTF-8; the NUL rule keeps them out of the index.
        low = b"\x00\x01\x02"
        status, headers, _ = self.request("POST", "/blob", low, {"Content-Type": "application/octet-stream"})
        self.assertEqual(curldb.query("path=/blob")[0]["preview"], "")
        self.assertEqual(self.request("GET", headers["Location"])[2].endswith(b"\n\n" + low), True)
        self.assertEqual(curldb.stats()["count"], 4)

    def test_chunked_bodies_are_joined_and_other_codings_refused(self):
        import socket
        png = b"\x89PNG\r\n\x1a\n\x00\x00\xff\xfe"
        wire = (b"POST /shot.png HTTP/1.1\r\nHost: t\r\nContent-Type: image/png\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n"
                b"3\r\n" + png[:3] + b"\r\n" + b"%x\r\n" % (len(png) - 3) + png[3:] + b"\r\n0\r\n\r\n")
        with socket.create_connection(self.server.server_address, timeout=5) as s:
            s.sendall(wire)
            reply = s.recv(4096)
        self.assertTrue(reply.startswith(b"HTTP/1.1 201"), reply)
        rid = curldb.last_id()
        status, got, body = self.request("GET", f"/{rid}")
        head, _, stored = body.partition(b"\n\n")
        self.assertEqual(stored, png)
        self.assertIn(b"\nContent-Length: %d\n" % len(png), head + b"\n")
        self.assertNotIn(b"Transfer-Encoding", head)
        self.assertEqual(curldb.query("header:Content-Type=image/png")[0]["id"], rid)
        status, _, data = self.request("POST", "/x", b"abc", {"Transfer-Encoding": "gzip"})
        self.assertEqual(status, 501)
        self.assertEqual(curldb.stats()["count"], 1)

    def _raw_exchange(self, wire: bytes) -> bytes:
        import socket
        with socket.create_connection(self.server.server_address, timeout=5) as s:
            s.sendall(wire)
            s.shutdown(socket.SHUT_WR)
            reply = b""
            while True:
                part = s.recv(4096)
                if not part:
                    return reply
                reply += part

    def test_truncated_chunked_bodies_are_refused(self):
        head = b"POST /x HTTP/1.1\r\nHost: t\r\nTransfer-Encoding: chunked\r\n\r\n"
        cases = {
            "short chunk": b"5\r\nabc",
            "no terminator": b"3\r\nabc",
            "no last chunk": b"3\r\nabc\r\n",
            "bad size": b"zz\r\nabc\r\n0\r\n\r\n",
            "chunk followed by data": b"3\r\nabcd\r\n0\r\n\r\n",
        }
        for name, tail in cases.items():
            with self.subTest(name=name):
                reply = self._raw_exchange(head + tail)
                self.assertTrue(reply.startswith(b"HTTP/1.1 400"), (name, reply))
                self.assertIn(b"incomplete chunked body", reply)
        self.assertEqual(curldb.stats()["count"], 0)

    def test_short_content_length_bodies_are_refused(self):
        head = b"POST /x HTTP/1.1\r\nHost: t\r\n"
        reply = self._raw_exchange(head + b"Content-Length: 5\r\n\r\nabc")
        self.assertTrue(reply.startswith(b"HTTP/1.1 400"), reply)
        self.assertIn(b"body ended after 3 of 5 bytes", reply)
        reply = self._raw_exchange(head + b"Content-Length: five\r\n\r\nabc")
        self.assertTrue(reply.startswith(b"HTTP/1.1 400"), reply)
        self.assertEqual(curldb.stats()["count"], 0)

    def test_ambiguous_request_framing_is_refused(self):
        head = b"POST /chat HTTP/1.1\r\nHost: x\r\n"
        cases = [
            (b"Content-Length: 0\r\nContent-Length: 3\r\n\r\nabc", b"400", b"conflicting Content-Length"),
            (b"Content-Length: -1\r\n\r\n", b"400", b"not a whole number"),
            (b"Transfer-Encoding: chunked\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n", b"501", b"chunked, chunked"),
            (b"Transfer-Encoding: gzip, chunked\r\n\r\n0\r\n\r\n", b"501", b"gzip, chunked"),
        ]
        for tail, code, text in cases:
            with self.subTest(tail=tail):
                reply = self._raw_exchange(head + tail)
                self.assertTrue(reply.startswith(b"HTTP/1.1 " + code), reply[:40])
                self.assertIn(text, reply)
        self.assertEqual(curldb.stats()["count"], 0)
        # Two agreeing copies are one length.
        reply = self._raw_exchange(head + b"Content-Length: 3\r\nContent-Length: 3\r\n\r\nabc")
        self.assertTrue(reply.startswith(b"HTTP/1.1 201"))

    def test_chunked_with_content_length_stores_one_true_length(self):
        wire = (b"POST /x HTTP/1.1\r\nHost: t\r\nContent-Length: 999\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n\r\n")
        reply = self._raw_exchange(wire)
        self.assertTrue(reply.startswith(b"HTTP/1.1 201"), reply)
        stored = curldb.get(curldb.last_id())
        head, _, body = stored.partition(b"\n\n")
        self.assertEqual(body, b"abc")
        self.assertEqual(head.count(b"Content-Length:"), 1)
        self.assertIn(b"\nContent-Length: 3", head)
        self.assertNotIn(b"999", head)

    def test_message_http_body_is_stored_as_itself(self):
        body = RAW.encode("utf-8")
        status, headers, _ = self.request("POST", "/anything", body, {"Content-Type": "message/http"})
        self.assertEqual(status, 201)
        status, _, stored = self.request("GET", headers["Location"])
        self.assertEqual(status, 200)
        self.assertEqual(stored, body)
        rid = int(headers["Location"].lstrip("/"))
        row = curldb.query(f"kind=response status=409")[0]
        self.assertEqual(row["id"], rid)
        status, _, data = self.request("POST", "/", b"just prose", {"Content-Type": "message/http; charset=utf-8"})
        self.assertEqual(status, 400)
        self.assertIn(b"message/http body", data)
        self.assertEqual(curldb.stats()["count"], 1)

    def test_record_headers_describe_the_stored_message(self):
        first = curldb.add(RAW)
        second = curldb.add("GET /notes/scope HTTP/1.1\r\nX-Scope: design\r\n\r\n")
        status, headers, body = self.request("GET", f"/{first}")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "message/http")
        self.assertEqual(body, RAW.encode("utf-8"))
        self.assertEqual(headers["X-Id"], str(first))
        self.assertEqual(headers["X-Kind"], "response")
        self.assertEqual(headers["X-Status"], "409")
        self.assertNotIn("X-Method", headers)
        self.assertEqual(headers["X-Last"], str(second))
        self.assertEqual(headers["Link"], f'</{second}>; rel="next"')
        self.assertTrue(headers["Last-Modified"].endswith(" GMT"))
        status, headers, _ = self.request("HEAD", f"/{second}")
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Kind"], "request")
        self.assertEqual(headers["X-Method"], "GET")
        self.assertEqual(headers["X-Path"], "/notes/scope")
        self.assertNotIn("X-Status", headers)
        self.assertEqual(headers["Link"], f'</{first}>; rel="prev"')

    def test_content_type_names_the_stored_text(self):
        expected = {
            curldb.add(RAW): "message/http",
            curldb.add("GET /x HTTP/1.1\r\n\r\n"): "message/http",
            curldb.add(NOTE, source_path="a.md"): "text/markdown; charset=utf-8",
            curldb.add("just words"): "text/plain; charset=utf-8",
        }
        for rid, ctype in expected.items():
            with self.subTest(rid=rid):
                status, headers, body = self.request("GET", f"/{rid}")
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], ctype)
                self.assertEqual(body, curldb.get(rid))

    def test_non_ascii_path_in_x_path_header(self):
        # X-Path carries the stored path as UTF-8 bytes; http.client hands
        # them back decoded as latin-1, the same way http.server received them.
        path = "/notes/\u4e2d\u6587.md"
        rid = curldb.add(f"GET {path} HTTP/1.1\r\n\r\n")
        for verb in ("GET", "HEAD"):
            with self.subTest(verb=verb):
                status, headers, _ = self.request(verb, f"/{rid}")
                self.assertEqual(status, 200)
                self.assertEqual(headers["X-Path"].encode("latin-1").decode("utf-8"), path)

    def test_parent_links_pair_records(self):
        # The door's Link header is bookkeeping: the stored bytes stay as sent.
        status, headers, _ = self.request("POST", "/chat", b"what about fork?")
        req = int(headers["Location"].lstrip("/"))
        reply = b"HTTP/1.1 200 OK\r\n\r\nfork is fine"
        status, headers, _ = self.request("POST", "/", reply,
                                          {"Content-Type": "message/http", "Link": f'</{req}>; rel="parent"'})
        rep = int(headers["Location"].lstrip("/"))
        status, got, body = self.request("GET", f"/{rep}")
        self.assertEqual(body, reply)
        self.assertIn(f'</{req}>; rel="parent"', got["Link"])
        self.assertEqual([r["id"] for r in curldb.query(f"parent={req}")], [rep])
        self.assertEqual(curldb.get_row(rep)["parent"], req)
        # A Link inside the message works too, and fan-out is allowed.
        second = curldb.add(f'HTTP/1.1 409 Conflict\r\nLink: </{req}>; rel="parent"\r\n\r\nno'.encode())
        self.assertEqual(curldb.get_row(second)["parent"], req)
        self.assertEqual(sorted(r["id"] for r in curldb.query(f"parent={req}")), [rep, second])
        self.assertIn(f"re {req}", curldb._format_results(curldb.ls()))
        self.assertEqual(curldb.parse_link_parent('<https://x/>; rel="next", </7>; rel=parent'), 7)
        self.assertIsNone(curldb.parse_link_parent('</7>; rel="prev"'))

    def test_parent_by_address_matches_content_location(self):
        # The pi extension pairs by address: Content-Location on the request,
        # Link </chat/x>; rel="parent" on the reply. Both resolve to the id.
        req = curldb.add('POST /chat HTTP/1.1\r\nContent-Location: /chat/a5660953\r\n\r\nq')
        rep = curldb.add('HTTP/1.1 200 OK\r\nContent-Location: /chat/4e384d75\r\nLink: </chat/a5660953>; rel="parent"\r\n\r\na')
        self.assertEqual(curldb.get_row(rep)["parent"], req)
        status, headers, _ = self.request("POST", "/", b"HTTP/1.1 200 OK\r\n\r\nb",
                                          {"Content-Type": "message/http", "Link": '</chat/4e384d75>; rel="parent"'})
        self.assertEqual(curldb.get_row(int(headers["Location"].lstrip("/")))["parent"], rep)
        self.assertIsNone(curldb.get_row(curldb.add('HTTP/1.1 200 OK\r\nLink: </chat/nobody>; rel="parent"\r\n\r\nc'))["parent"])
        self.assertEqual(curldb.parse_link_target('</chat/a5660953>; rel="parent"'), "/chat/a5660953")

    def test_old_files_get_the_parent_column(self):
        path = os.path.join(self.directory, "old.sqlite")
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE records (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                status INTEGER, method TEXT, path TEXT, raw TEXT NOT NULL,
                body TEXT NOT NULL DEFAULT '', ts REAL NOT NULL);
            INSERT INTO records (kind, status, raw, body, ts) VALUES ('response', 200, 'HTTP/1.1 200 OK\n\nold', 'old', 1);
            CREATE TABLE headers (record_id INTEGER NOT NULL REFERENCES records(id), name TEXT NOT NULL, value TEXT NOT NULL DEFAULT '');
            INSERT INTO records (kind, method, path, raw, body, ts) VALUES ('request', 'POST', '/chat', 'POST /chat HTTP/1.1\nContent-Location: /chat/aa\n\nq', 'q', 2);
            INSERT INTO headers VALUES (2, 'Content-Location', '/chat/aa');
            INSERT INTO records (kind, status, raw, body, ts) VALUES ('response', 200, 'HTTP/1.1 200 OK\nLink: </chat/aa>; rel="parent"\n\na', 'a', 3);
            INSERT INTO headers VALUES (3, 'Link', '</chat/aa>; rel="parent"');
        """)
        conn.commit(); conn.close()
        os.environ["CURLDB_PATH"] = path
        rid = curldb.add(RAW, parent=1)
        self.assertEqual(curldb.get_row(rid)["parent"], 1)
        self.assertIsNone(curldb.get_row(1)["parent"])
        self.assertEqual(curldb.get(1), b"HTTP/1.1 200 OK\n\nold")
        # Existing rows were paired from their Link headers when the column arrived.
        self.assertEqual(curldb.get_row(3)["parent"], 2)
        # The host column arrived too; old rows are this file's own.
        self.assertIsNone(curldb.get_row(1)["host"])
        self.assertEqual(dict(curldb.headers_of(rid)), {"X-Scope": "design"})

    def test_transport_headers_are_stored_but_not_tags(self):
        status, headers, _ = self.request("POST", "/chat", b"hi", {"X-Topic": "fork", "Cookie": "a=b", "Referer": "http://x/"})
        rid = int(headers["Location"].lstrip("/"))
        raw = curldb.get(rid)
        self.assertIn(b"Cookie: a=b", raw)
        names = {n for n, _ in curldb.headers_of(rid)}
        self.assertIn("X-Topic", names)
        for gone in ("Host", "Cookie", "Referer", "Accept-Encoding", "Content-Length"):
            self.assertNotIn(gone, names)
        self.assertNotIn("Host", curldb.tags())

    def test_amendments_are_patch_records(self):
        rid = curldb.add(RAW)
        status, headers, _ = self.request("PATCH", f"/{rid}", b"", {"X-Flag": "true", "Via": "curldb-viewer"})
        self.assertEqual(status, 201)
        aid = int(headers["Location"].lstrip("/"))
        row = curldb.get_row(aid)
        self.assertEqual((row["kind"], row["method"], row["path"], row["parent"]), ("request", "PATCH", f"/{rid}", rid))
        self.assertEqual(dict(curldb.headers_of(rid))["X-Flag"], "true")
        self.assertEqual([r["id"] for r in curldb.query("header:X-Flag=true")], [rid])
        self.assertIn("X-Flag", curldb.tags())
        # Original bytes untouched; the amendment is its own record.
        self.assertEqual(curldb.get(rid), RAW.encode("utf-8"))
        # A later PATCH wins; an empty value clears the name.
        curldb.amend(rid, [("X-Flag", "")])
        self.assertNotIn("X-Flag", dict(curldb.headers_of(rid)))
        self.assertEqual(curldb.query("header:X-Flag=true"), [])
        curldb.amend(rid, [("X-Scope", "review")])
        self.assertEqual(dict(curldb.headers_of(rid))["X-Scope"], "review")
        history = curldb.header_history(rid)
        self.assertEqual([h["ts"] is None for h in history], [True, False, False, False])
        self.assertEqual(dict(history[0]["headers"])["X-Scope"], "design")
        with self.assertRaises(KeyError):
            curldb.amend(999, [("X-Flag", "true")])
        with self.assertRaises(ValueError):
            curldb.amend(rid, [("bad name", "x")])
        # A value is one line; a newline cannot smuggle in a second header.
        with self.assertRaises(ValueError):
            curldb.amend(rid, [("X-Note", "hello" + chr(10) + "X-Archive: true")])
        with self.assertRaises(ValueError):
            curldb.amend(rid, [("X-Note", "x")], via="a" + chr(13) + "b")
        self.assertNotIn("X-Archive", dict(curldb.headers_of(rid)))
        # Inside one PATCH the last value of a name wins, case-insensitively.
        curldb.add(f"PATCH /{rid} HTTP/1.1" + chr(10) + "X-Flag: true" + chr(10) + "x-flag: false" + chr(10) + chr(10))
        self.assertEqual([v for n, v in curldb.headers_of(rid) if n.lower() == "x-flag"], ["false"])

    def test_a_leading_minus_negates_a_token(self):
        a = curldb.add(RAW)                                        # 409, X-Scope: design, body has 协议
        b = curldb.add("HTTP/1.1 200 OK\r\nX-Scope: build\r\n\r\nplain words")
        c = curldb.add("GET /x HTTP/1.1\r\n\r\n")
        ids = lambda expr: sorted(r["id"] for r in curldb.query(expr))
        self.assertEqual(ids("-status=409"), [b, c])
        self.assertEqual(ids("-kind=request"), [a, b])
        self.assertEqual(ids("-header:X-Scope"), [c])
        self.assertEqual(ids("-header:X-Scope=design"), [b, c])
        self.assertEqual(ids("kind=response -header:X-Scope=design"), [b])
        self.assertEqual(ids("-plain"), [a, c])
        self.assertEqual(ids("-path~/x"), [a, b])
        curldb.amend(a, [("X-Archive", "true")])
        self.assertEqual(ids("-header:X-Archive=true -method=PATCH"), [b, c])

    def test_saved_queries_are_put_records(self):
        curldb.add(RAW)
        rid = curldb.save_query("conflicts", "status=409")
        self.assertEqual(curldb.get_row(rid)["path"], "/queries/conflicts")
        self.assertEqual(curldb.saved_queries(), {"conflicts": "status=409"})
        self.assertEqual(len(curldb.query("@conflicts")), 1)
        self.assertEqual(len(curldb.query("@conflicts header:X-Scope=design")), 1)
        curldb.save_query("conflicts", "status=200")
        self.assertEqual(curldb.query("@conflicts"), [])
        curldb.add("DELETE /queries/conflicts HTTP/1.1\r\n\r\n")
        self.assertEqual(curldb.saved_queries(), {})
        with self.assertRaises(KeyError):
            curldb.query("@conflicts")
        with self.assertRaises(ValueError):
            curldb.save_query("no spaces", "x")

    def test_db_snapshot_viewer_page_and_events(self):
        rid = curldb.add(RAW)
        status, headers, data = self.request("GET", "/db")
        self.assertEqual((status, headers["Content-Type"]), (200, "application/vnd.sqlite3"))
        self.assertTrue(data.startswith(b"SQLite format 3"))
        self.assertEqual(headers["ETag"], f'"{rid}"')
        status, _, _ = self.request("GET", "/db", headers={"If-None-Match": headers["ETag"]})
        self.assertEqual(status, 304)
        copy = self.open_snapshot(data)
        try:
            self.assertEqual(copy.execute("SELECT count(*) FROM records").fetchone()[0], 1)
        finally:
            copy.close()
        # The page for browsers, the text list for curl, on the same address.
        status, headers, data = self.request("GET", "/", headers={"Accept": "text/html,*/*"})
        self.assertEqual((status, headers["Content-Type"]), (200, "text/html; charset=utf-8"))
        self.assertIn(b"curldb viewer", data)
        self.assertEqual(headers["Vary"], "Accept")
        status, headers, data = self.request("GET", "/")
        self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
        status, headers, data = self.request("GET", "/viewer/app.js")
        self.assertEqual((status, headers["Content-Type"][:15]), (200, "text/javascript"))
        self.assertEqual(self.request("GET", "/viewer/../__init__.py")[0], 404)
        # Events: connect after rid, add two, read two events.
        client = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        client.request("GET", f"/events?after={rid - 1}")
        response = client.getresponse()
        self.assertEqual(response.headers["Content-Type"], "text/event-stream")
        a = curldb.add(RAW)
        b = curldb.add("GET /x HTTP/1.1\r\n\r\n")
        seen = b""
        while b"data: /%d" % b not in seen:
            seen += response.fp.readline()
        self.assertIn(b"id: %d\ndata: /%d\n\n" % (a, a), seen)
        self.assertIn(b"id: %d\ndata: /%d\n\n" % (rid, rid), seen)   # ?after=N replays from N+1
        client.close()
        import time as _time
        _time.sleep(0.8)   # the events handler notices the hangup and lets go of the file

    def test_light_snapshot_leaves_big_bytes_to_get_id(self):
        small = curldb.add(RAW)
        big = curldb.add("HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n\r\n".encode() + bytes(300 * 1024))
        status, headers, data = self.request("GET", "/db")
        self.assertEqual(headers["X-Light"], str(curldb.LIGHT_OVER))
        self.assertLess(len(data), 100 * 1024)
        copy = self.open_snapshot(data)
        try:
            rows = dict(copy.execute("SELECT id, length(raw) FROM records").fetchall())
            self.assertEqual(rows[big], 0)
            self.assertGreater(rows[small], 0)
            self.assertEqual(copy.execute("SELECT count(*) FROM headers WHERE record_id=?", (big,)).fetchone()[0], 1)
        finally:
            copy.close()
        status, headers, data = self.request("GET", "/db?full=1")
        self.assertNotIn("X-Light", headers)
        self.assertGreater(len(data), 300 * 1024)
        status, headers, body = self.request("GET", f"/{big}")
        self.assertEqual(len(body), len(curldb.get(big)))

    def test_last_id_header_and_stats(self):
        status, headers, _ = self.request("HEAD", "/")
        self.assertEqual((status, headers["X-Last"]), (200, "0"))
        rid = curldb.add(RAW)
        status, headers, data = self.request("GET", "/")
        self.assertEqual((status, headers["X-Last"]), (200, str(rid)))
        self.assertEqual(self.request("HEAD", "/")[1]["X-Last"], str(rid))
        status, headers, data = self.request("GET", "/stats")
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Last"], str(rid))
        self.assertIn(b"records: 1", data)
        self.assertIn(b"fts:", data)

    def test_missing_records_and_invalid_paths(self):
        for path in ("/999", "/missing"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)
                status, _, data = self.request("HEAD", path)
                self.assertEqual(status, 404)
                self.assertEqual(data, b"")


if __name__ == "__main__":
    unittest.main()
