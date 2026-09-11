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
        self.assertTrue(reply.startswith(b"HTTP/1.0 201"), reply)
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
                self.assertTrue(reply.startswith(b"HTTP/1.0 400"), (name, reply))
                self.assertIn(b"incomplete chunked body", reply)
        self.assertEqual(curldb.stats()["count"], 0)

    def test_short_content_length_bodies_are_refused(self):
        head = b"POST /x HTTP/1.1\r\nHost: t\r\n"
        reply = self._raw_exchange(head + b"Content-Length: 5\r\n\r\nabc")
        self.assertTrue(reply.startswith(b"HTTP/1.0 400"), reply)
        self.assertIn(b"body ended after 3 of 5 bytes", reply)
        reply = self._raw_exchange(head + b"Content-Length: five\r\n\r\nabc")
        self.assertTrue(reply.startswith(b"HTTP/1.0 400"), reply)
        self.assertEqual(curldb.stats()["count"], 0)

    def test_chunked_with_content_length_stores_one_true_length(self):
        wire = (b"POST /x HTTP/1.1\r\nHost: t\r\nContent-Length: 999\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n\r\n")
        reply = self._raw_exchange(wire)
        self.assertTrue(reply.startswith(b"HTTP/1.0 201"), reply)
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
