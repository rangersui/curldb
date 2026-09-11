"""Run with: python -m unittest discover -s tests -v."""
from __future__ import annotations

import http.client
from http.server import HTTPServer
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
        self.assertEqual(curldb.get(rid), RAW)
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
        self.assertEqual(curldb.get(1), RAW)
        self.assertEqual(curldb.query("status=409 header:X-Scope=design")[0]["ts"], 123.0)
        self.assertEqual(curldb.stats()["count"], 1)

    def test_limit_and_separate_session_files(self):
        first = curldb.add("first")
        second = curldb.add("second")
        self.assertEqual([r["id"] for r in curldb.ls(1)], [second])
        with patch.dict(os.environ, {"CURLDB_PATH": str(self.directory / "other.sqlite")}):
            self.assertEqual(curldb.ls(), [])
        self.assertEqual(curldb.get(first), "first")


class CLITests(TemporaryDatabase):
    def cli(self, *args, data=None, check=True):
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), *args], input=data,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.directory, timeout=10,
        )
        if check:
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        return result

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


class HTTPTests(TemporaryDatabase):
    def setUp(self):
        super().setUp()
        ready = queue.Queue()

        def create_server(address, handler):
            handler.log_message = lambda *args: None
            server = HTTPServer(address, handler)
            ready.put(server)
            return server

        self.thread = threading.Thread(target=curldb.serve, args=(0,), daemon=True)
        with patch("http.server.HTTPServer", side_effect=create_server):
            self.thread.start()
            self.server = ready.get(timeout=5)
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive(), "HTTP server did not stop")

    def request(self, method, path, body=None, headers=None):
        client = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            client.request(method, path, body=body, headers=headers or {})
            response = client.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            client.close()

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
