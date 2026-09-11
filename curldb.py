#!/usr/bin/env python3
"""curldb -- HTTP exchange datastore.

Raw HTTP requests and responses in, structured queries out.
One SQLite file per session, zero daemon, zero conversion.

    echo 'HTTP/1.1 200 OK\nX-Verdict: solid\n\nhello' | curldb add
    echo 'what about fork?' | curldb wrap 'POST /chat' -H X-Topic:fork | curldb add
    curldb query 'status=200 header:X-Verdict=solid body~hello'
    curldb tags
    curldb get 1
    curldb ls
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
from email.utils import formatdate
from http import HTTPStatus

__version__ = "0.4.0"

DEFAULT_DB = "curldb.sqlite"

# -----------------------------------------------
# PARSE
# -----------------------------------------------

_STATUS_LINE = re.compile(r"HTTP/[\d.]+\s+(\d{3})")
_REQUEST_LINE = re.compile(r"([A-Z]+)\s+(\S+)\s+HTTP/[\d.]+")


def parse_envelope(raw: bytes | str, source_path: str | None = None) -> dict:
    """Parse a raw record into its parts.

    Returns {kind, status, method, path, headers, body}. kind is
    "response" (status line first), "request" (request line first),
    "note" (a --- front matter block first), or "raw" (none of those;
    the whole text is the body). Notes and raw text take source_path
    as their path; HTTP envelopes carry their own.

    Lenient: accepts \\n or \\r\\n, ignores malformed headers,
    ignores Content-Length. This is a document parser, not a
    protocol parser.

    Bytes that are not UTF-8 go through _parse_binary: the head is
    still indexed, the body stays out of the index.
    """
    if isinstance(raw, bytes):
        if not _is_text(raw):
            return _parse_binary(raw, source_path)
        raw = raw.decode("utf-8")
    text = raw.replace("\r\n", "\n")
    lines = text.split("\n")
    first = lines[0] if lines else ""
    path = source_path.replace("\\", "/") if source_path else None

    out = {"kind": "raw", "status": None, "method": None, "path": path,
           "headers": [], "body": text.strip()}

    if first.strip() == "---":
        try:
            end = lines.index("---", 1)
        except ValueError:
            return out
        out["kind"] = "note"
        out["headers"] = _flatten_front_matter(lines[1:end])
        out["body"] = "\n".join(lines[end + 1:]).strip()
        return out

    m = _STATUS_LINE.match(first)
    if m:
        out["kind"] = "response"
        out["status"] = int(m.group(1))
        out["path"] = None
    else:
        m = _REQUEST_LINE.match(first)
        if not m:
            return out
        out["kind"] = "request"
        out["method"] = m.group(1)
        out["path"] = m.group(2)

    if "\n\n" in text:
        head, body = text.split("\n\n", 1)
    else:
        head, body = text, ""
    out["body"] = body.strip()
    for line in head.split("\n")[1:]:
        if ":" in line:
            name, _, value = line.partition(":")
            out["headers"].append((name.strip(), value.strip()))
    return out


def _parse_binary(data: bytes, source_path: str | None) -> dict:
    """A record whose bytes are not UTF-8 text. When it starts with a
    status line or a request line, the head is parsed as text and the
    body is left out of the index (empty body column, no full-text
    entry). Anything else is a raw record with an empty body."""
    cuts = [i for i in (data.find(b"\r\n\r\n"), data.find(b"\n\n")) if i >= 0]
    head = data[:min(cuts)] if cuts else data
    out = parse_envelope(head.decode("utf-8", "replace") + "\n\n", source_path)
    if out["kind"] in ("request", "response"):
        out["body"] = ""
        return out
    path = source_path.replace("\\", "/") if source_path else None
    return {"kind": "raw", "status": None, "method": None, "path": path,
            "headers": [], "body": ""}


def _is_text(data: bytes) -> bool:
    """UTF-8 with no NUL byte. The NUL rule is the one git and grep use;
    it keeps binary data that happens to be valid UTF-8 (a run of low
    bytes) out of the text index."""
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _flatten_front_matter(lines: list[str]) -> list[tuple[str, str]]:
    """Flatten a YAML front matter subset into header pairs.

    Nested maps become dotted names (metadata.type), lists become
    repeated names (tags: a / tags: b), block scalars (| or >) join
    their lines with spaces, quotes are stripped. Anchors, flow maps
    and quoted colons are not interpreted; an unparseable line is
    stored as its own name with an empty value.
    """
    out: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []   # (indent, dotted prefix)
    i = 0
    n = len(lines)

    def prefix_at(indent: int) -> str:
        while stack and indent <= stack[-1][0]:
            stack.pop()
        return stack[-1][1] if stack else ""

    def joined(prefix: str, key: str) -> str:
        return f"{prefix}.{key}" if prefix else key

    while i < n:
        line = lines[i]
        stripped = line.strip()
        i += 1
        if not stripped or stripped.startswith("#"):
            continue
        indent = _indent(line)

        if stripped.startswith("- "):
            # list item under the nearest enclosing key
            prefix = prefix_at(indent + 1)
            item = stripped[2:].strip()
            if ":" in item and not item.startswith(("\"", "'", "[")):
                k, _, v = item.partition(":")
                out.append((joined(prefix, k.strip()), _unquote(v)))
            else:
                out.append((prefix, _unquote(item)))
            continue

        if ":" not in stripped:
            out.append((joined(prefix_at(indent), stripped), ""))
            continue

        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        prefix = prefix_at(indent)
        name = joined(prefix, key)

        if value in ("|", ">", "|-", ">-"):
            block: list[str] = []
            while i < n and (not lines[i].strip() or _indent(lines[i]) > indent):
                if lines[i].strip():
                    block.append(lines[i].strip())
                i += 1
            out.append((name, " ".join(block)))
            continue

        if value.startswith("[") and value.endswith("]"):
            for item in value[1:-1].split(","):
                item = _unquote(item)
                if item:
                    out.append((name, item))
            continue

        if value:
            out.append((name, _unquote(value)))
            continue

        # bare "key:" is a parent if something more indented follows,
        # otherwise a header with an empty value
        j = i
        while j < n and not lines[j].strip():
            j += 1
        child = lines[j] if j < n else ""
        if child and (_indent(child) > indent or
                      (_indent(child) >= indent and child.strip().startswith("- "))):
            stack.append((indent, name))
        else:
            out.append((name, ""))
    return out


def wrap(start: str, body: bytes | str, headers: list[tuple[str, str]]) -> bytes | str:
    """Build an HTTP envelope around body.

    start is either a status code ("200") or a request line
    ("POST /chat"). A Date header is added when the caller did not
    supply one. body is appended as given; a bytes body gives a bytes
    envelope, a str body a str one.
    """
    if re.fullmatch(r"\d{3}", start):
        code = int(start)
        try:
            reason = HTTPStatus(code).phrase
        except ValueError:
            reason = ""
        first = f"HTTP/1.1 {code} {reason}".rstrip()
    else:
        first = f"{start} HTTP/1.1"
    names = {n.lower() for n, _ in headers}
    lines = [first]
    if "date" not in names:
        lines.append("Date: " + time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    lines.extend(f"{n}: {v}" for n, v in headers)
    head = "\n".join(lines) + "\n\n"
    if isinstance(body, bytes):
        return head.encode("utf-8") + body
    return head + body

# -----------------------------------------------
# DB
# -----------------------------------------------


def db_path() -> str:
    return os.environ.get("CURLDB_PATH", DEFAULT_DB)


def _connect(path: str | None = None) -> sqlite3.Connection:
    path = path or db_path()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _init(conn)
    return conn


def _init(conn: sqlite3.Connection) -> None:
    old_rows = _drop_v1(conn)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS records (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            kind     TEXT    NOT NULL,
            status   INTEGER,
            method   TEXT,
            path     TEXT,
            raw      TEXT    NOT NULL,
            body     TEXT    NOT NULL DEFAULT '',
            ts       REAL    NOT NULL
        );
        CREATE TABLE IF NOT EXISTS headers (
            record_id INTEGER NOT NULL REFERENCES records(id),
            name      TEXT    NOT NULL,
            value     TEXT    NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_headers_rid ON headers(record_id);
        CREATE INDEX IF NOT EXISTS idx_headers_name ON headers(name);
        CREATE INDEX IF NOT EXISTS idx_records_status ON records(status);
        CREATE INDEX IF NOT EXISTS idx_records_path ON records(path);
    """)
    _init_fts(conn)
    # parent: the record this one answers (0.4). Older files get the
    # column added; rows written before carry no parent.
    cols = {c[1] for c in conn.execute("PRAGMA table_info(records)")}
    if "parent" not in cols:
        conn.executescript("""
            ALTER TABLE records ADD COLUMN parent INTEGER;
            CREATE INDEX IF NOT EXISTS idx_records_parent ON records(parent);
        """)
        # Rows written before the column: pair them from their Link headers.
        for rid, link in conn.execute(
                "SELECT record_id, value FROM headers WHERE LOWER(name) = 'link'").fetchall():
            parent = resolve_link(link, conn)
            if parent is not None and parent != rid:
                conn.execute("UPDATE records SET parent = ? WHERE id = ?", (parent, rid))
        conn.commit()
    for raw, ts in old_rows:
        add(raw, conn, ts=ts)


def _init_fts(conn: sqlite3.Connection) -> None:
    """trigram tokenizer (SQLite 3.34+) searches CJK substrings
    directly; older SQLite falls back to unicode61 plus LIKE."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='body_fts'").fetchone()
    if exists:
        return
    try:
        conn.execute("""
            CREATE VIRTUAL TABLE body_fts USING fts5(
                body, content=records, content_rowid=id,
                tokenize='trigram')""")
    except sqlite3.OperationalError:
        conn.execute("""
            CREATE VIRTUAL TABLE body_fts USING fts5(
                body, content=records, content_rowid=id)""")
    conn.executescript("""
        CREATE TRIGGER IF NOT EXISTS trg_fts_insert AFTER INSERT ON records
        BEGIN
            INSERT INTO body_fts(rowid, body) VALUES (new.id, new.body);
        END;
        CREATE TRIGGER IF NOT EXISTS trg_fts_delete AFTER DELETE ON records
        BEGIN
            INSERT INTO body_fts(body_fts, rowid, body)
                VALUES ('delete', old.id, old.body);
        END;
    """)


def _fts_is_trigram(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='body_fts'").fetchone()
    return bool(row and "trigram" in row[0])


def _drop_v1(conn: sqlite3.Connection) -> list[tuple[str, float]]:
    """The first local layout had a responses table and headers keyed
    by response_id. Pull its raw rows out, drop the old layout, and
    hand the rows back so _init can re-add them into records."""
    old = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='responses'").fetchone()
    if not old:
        return []
    rows = conn.execute(
        "SELECT raw, ts FROM responses ORDER BY id").fetchall()
    conn.executescript("""
        DROP TRIGGER IF EXISTS trg_fts_insert;
        DROP TRIGGER IF EXISTS trg_fts_delete;
        DROP TABLE IF EXISTS body_fts;
        DROP TABLE IF EXISTS headers;
        DROP TABLE responses;
    """)
    return rows


def add(raw: bytes | str, conn: sqlite3.Connection | None = None,
        ts: float | None = None, source_path: str | None = None,
        parent: int | None = None) -> int:
    """Store one raw record, as bytes. Returns the new record id.

    parent is the id of the record this one answers. When not given,
    a Link header with rel="parent" inside the message supplies it."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    own = conn is None
    if own:
        conn = _connect()
    p = parse_envelope(raw, source_path)
    if parent is None:
        parent = _parent_from_headers(p["headers"], conn)
    cur = conn.execute(
        "INSERT INTO records (kind, status, method, path, raw, body, ts, parent)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (p["kind"], p["status"], p["method"], p["path"], raw, p["body"],
         ts if ts is not None else time.time(), parent))
    rid = cur.lastrowid
    if p["headers"]:
        conn.executemany(
            "INSERT INTO headers (record_id, name, value) VALUES (?,?,?)",
            [(rid, n, v) for n, v in p["headers"]])
    conn.commit()
    if own:
        conn.close()
    return rid


_LINK_PARENT = re.compile(r'<\s*([^>]+?)\s*>\s*;\s*rel\s*=\s*"?parent"?', re.I)


def parse_link_target(value: str | None) -> str | None:
    """The target of a Link header with rel="parent": `</12>; rel="parent"`
    gives "/12", `</chat/a5660953>; rel="parent"` gives "/chat/a5660953"."""
    if not value:
        return None
    m = _LINK_PARENT.search(value)
    return m.group(1) if m else None


def parse_link_parent(value: str | None) -> int | None:
    """The record id in a Link parent that names one directly: `</12>`."""
    target = parse_link_target(value)
    return int(target.lstrip("/")) if target and target.lstrip("/").isdigit() else None


def resolve_link(value: str | None, conn: sqlite3.Connection | None = None) -> int | None:
    """A Link parent as a record id. `</12>` is the id itself; any other
    target is an address, matched against the Content-Location header of
    an earlier record (the way the pi extension pairs its messages)."""
    parent = parse_link_parent(value)
    if parent is not None:
        return parent
    target = parse_link_target(value)
    if not target:
        return None
    own = conn is None
    if own:
        conn = _connect()
    row = conn.execute(
        "SELECT record_id FROM headers WHERE LOWER(name) = 'content-location' AND value = ?"
        " ORDER BY record_id LIMIT 1", (target,)).fetchone()
    if own:
        conn.close()
    return int(row[0]) if row else None


def _parent_from_headers(headers: list[tuple[str, str]], conn: sqlite3.Connection | None = None) -> int | None:
    for name, value in headers:
        if name.lower() == "link":
            parent = resolve_link(value, conn)
            if parent is not None:
                return parent
    return None


def get(rid: int) -> bytes | None:
    row = get_row(rid)
    return row["raw"] if row else None


def get_row(rid: int) -> dict | None:
    """One record with its index fields: id, kind, status, method, path,
    ts, and raw as bytes (rows written before 0.3 were text; they come
    back encoded)."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, kind, status, method, path, ts, raw, parent FROM records WHERE id=?",
        (rid,)).fetchone()
    conn.close()
    if row is None:
        return None
    out = dict(row)
    if isinstance(out["raw"], str):
        out["raw"] = out["raw"].encode("utf-8")
    return out

# -----------------------------------------------
# QUERY
# -----------------------------------------------

_CJK_RE = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff\U00020000-\U0002a6df]")


def query(expr: str) -> list[dict]:
    """Query records.

    Tokens separated by spaces, all AND:
        kind=request|response
        status=200          status equals
        status=200,201      status in set
        method=POST         request method
        path=/chat          request path equals
        path~/tool/         request path contains
        header:Name         header exists
        header:Name=value   header equals value
        body~word           body search
        body~"a phrase"     body phrase search
        anyword             bare word, body search
    """
    conn = _connect()
    trigram = _fts_is_trigram(conn)
    wheres: list[str] = []
    params: list = []
    joins: list[str] = []
    fts_terms: list[str] = []
    header_n = 0

    for token in _tokenize(expr):
        m = re.fullmatch(r"kind=(request|response|note|raw)", token)
        if m:
            wheres.append("r.kind = ?")
            params.append(m.group(1))
            continue
        m = re.fullmatch(r"status=(\d[\d,]*)", token)
        if m:
            codes = [int(c) for c in m.group(1).split(",")]
            wheres.append(f"r.status IN ({','.join('?' * len(codes))})")
            params.extend(codes)
            continue
        m = re.fullmatch(r"method=([A-Za-z]+)", token)
        if m:
            wheres.append("r.method = ?")
            params.append(m.group(1).upper())
            continue
        m = re.fullmatch(r"parent=(\d+)", token)
        if m:
            wheres.append("r.parent = ?")
            params.append(int(m.group(1)))
            continue
        m = re.fullmatch(r"path=(\S+)", token)
        if m:
            wheres.append("r.path = ?")
            params.append(m.group(1))
            continue
        m = re.fullmatch(r"path~(\S+)", token)
        if m:
            wheres.append("r.path LIKE ?")
            params.append(f"%{m.group(1)}%")
            continue
        m = re.fullmatch(r"header:([^=]+)=(.*)", token)
        if m:
            alias = f"h{header_n}"
            header_n += 1
            joins.append(f"JOIN headers {alias} ON {alias}.record_id = r.id")
            wheres.append(f"LOWER({alias}.name) = LOWER(?)")
            wheres.append(f"{alias}.value = ?")
            params.extend([m.group(1), m.group(2)])
            continue
        m = re.fullmatch(r"header:(\S+)", token)
        if m:
            alias = f"h{header_n}"
            header_n += 1
            joins.append(f"JOIN headers {alias} ON {alias}.record_id = r.id")
            wheres.append(f"LOWER({alias}.name) = LOWER(?)")
            params.append(m.group(1))
            continue
        m = re.fullmatch(r'body[~:]"?([^"]+)"?', token)
        term = m.group(1) if m else token.strip('"')
        # trigram needs 3+ characters per term; unicode61 cannot split
        # CJK at all. Both cases go to LIKE, everything else to FTS.
        if len(term) < 3 or (not trigram and _CJK_RE.search(term)):
            wheres.append("r.body LIKE ?")
            params.append(f"%{term}%")
        else:
            fts_terms.append(term)

    if fts_terms:
        joins.append("JOIN body_fts ON body_fts.rowid = r.id")
        wheres.append("body_fts MATCH ?")
        params.append(" AND ".join(
            '"' + t.replace('"', '""') + '"' for t in fts_terms))

    sql = f"""
        SELECT DISTINCT r.id, r.kind, r.status, r.method, r.path, r.ts,
               substr(r.body, 1, 120), r.parent
        FROM records r
        {' '.join(joins)}
        WHERE {' AND '.join(wheres) if wheres else '1'}
        ORDER BY r.id DESC
        LIMIT 100
    """
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [_row(r) for r in rows]


def _row(r) -> dict:
    return {"id": r[0], "kind": r[1], "status": r[2], "method": r[3],
            "path": r[4], "ts": r[5], "preview": r[6], "parent": r[7]}


def _tokenize(expr: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    in_quote = False
    for ch in expr:
        if ch == '"':
            in_quote = not in_quote
            current.append(ch)
        elif ch == " " and not in_quote:
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(ch)
    if current:
        tokens.append("".join(current))
    return tokens


def ls(limit: int = 20) -> list[dict]:
    conn = _connect()
    rows = conn.execute("""
        SELECT id, kind, status, method, path, ts, substr(body, 1, 80), parent
        FROM records ORDER BY id DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [_row(r) for r in rows]


def headers_of(rid: int) -> list[tuple[str, str]]:
    conn = _connect()
    rows = conn.execute(
        "SELECT name, value FROM headers WHERE record_id=?", (rid,)).fetchall()
    conn.close()
    return rows


def tags(name: str | None = None) -> dict[str, list[tuple[str, int]]]:
    """Every header name and value seen in this db, with counts."""
    conn = _connect()
    if name:
        rows = conn.execute("""
            SELECT name, value, count(*) FROM headers
            WHERE LOWER(name) = LOWER(?)
            GROUP BY name, value ORDER BY count(*) DESC, value
        """, (name,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT name, value, count(*) FROM headers
            GROUP BY name, value ORDER BY name, count(*) DESC, value
        """).fetchall()
    conn.close()
    out: dict[str, list[tuple[str, int]]] = {}
    for n, v, c in rows:
        out.setdefault(n, []).append((v, c))
    return out


def stats() -> dict:
    conn = _connect()
    count = conn.execute("SELECT count(*) FROM records").fetchone()[0]
    kinds = dict(conn.execute(
        "SELECT kind, count(*) FROM records GROUP BY kind").fetchall())
    trigram = _fts_is_trigram(conn)
    conn.close()
    path = db_path()
    size = os.path.getsize(path) if os.path.exists(path) else 0
    return {"count": count, "kinds": kinds, "size_bytes": size,
            "path": path, "trigram": trigram}


def last_id() -> int:
    """Highest record id, 0 when empty. Ids are never reused, so this
    is also the record count and the tail a consumer follows."""
    conn = _connect()
    last = conn.execute("SELECT max(id) FROM records").fetchone()[0]
    conn.close()
    return int(last or 0)

# -----------------------------------------------
# SERVE
# -----------------------------------------------
#
# A network door onto the same file. It archives the HTTP messages it
# receives and hands them back; it never interprets a stored message as
# its own reply. Same operations as the CLI, nothing more.


DEFAULT_PORT = 200   # HTTP 200. Below 1024, so root on Linux/macOS.


_CONTENT_TYPES = {
    "request": "message/http",
    "response": "message/http",
    "note": "text/markdown; charset=utf-8",
    "raw": "text/plain; charset=utf-8",
}


def _utf8_from_latin1(text: str) -> str:
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _latin1_from_utf8(text: str) -> str:
    # The inverse, for headers we send: http.server encodes header lines as
    # latin-1, so hand it the UTF-8 bytes disguised as latin-1 and the wire
    # carries the same UTF-8 the stored envelope has.
    return text.encode("utf-8").decode("latin-1")


def serve(port: int = DEFAULT_PORT, host: str = "127.0.0.1") -> None:
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        server_version = f"curldb/{__version__}"
        sys_version = ""

        def _text(self, code: int, text: str, extra: dict | None = None,
                  head_only: bool = False) -> None:
            data = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if not head_only:
                self.wfile.write(data)

        def _record(self, head_only: bool) -> None:
            rid = self.path.lstrip("/")
            if not rid.isdigit():
                self._text(404, "not found\n", head_only=head_only)
                return
            row = get_row(int(rid))
            if row is None:
                self._text(404, "not found\n", head_only=head_only)
                return
            data = row["raw"]
            self.send_response(200)
            # The body is the raw column, so the type names what those
            # bytes are: an HTTP message, a markdown note, plain text, or
            # bytes that are none of those.
            ctype = _CONTENT_TYPES.get(row["kind"], "text/plain; charset=utf-8")
            if row["kind"] == "raw" and not _is_text(data):
                ctype = "application/octet-stream"
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            # The outer headers describe the stored message; the body is
            # the message itself. Same fields as one line of `curldb ls`.
            self.send_header("X-Id", str(row["id"]))
            self.send_header("X-Kind", row["kind"])
            if row["status"] is not None:
                self.send_header("X-Status", str(row["status"]))
            if row["method"]:
                self.send_header("X-Method", _latin1_from_utf8(row["method"]))
            if row["path"]:
                self.send_header("X-Path", _latin1_from_utf8(row["path"]))
            self.send_header("Last-Modified", formatdate(row["ts"], usegmt=True))
            last = last_id()
            self.send_header("X-Last", str(last))
            links = []
            if row["parent"]:
                links.append(f'</{row["parent"]}>; rel="parent"')
            if row["id"] > 1:
                links.append(f'</{row["id"] - 1}>; rel="prev"')
            if row["id"] < last:
                links.append(f'</{row["id"] + 1}>; rel="next"')
            if links:
                self.send_header("Link", ", ".join(links))
            self.end_headers()
            if not head_only:
                self.wfile.write(data)

        def _read(self, head_only: bool = False) -> None:
            from urllib.parse import parse_qs, urlsplit
            url = urlsplit(self.path)
            if url.path == "/":
                q = parse_qs(url.query).get("q", [""])[0]
                results = query(q) if q else ls()
                self._text(200, _format_results(results), {"X-Last": str(last_id())},
                           head_only=head_only)
            elif url.path == "/stats":
                self._text(200, _format_stats(stats()), {"X-Last": str(last_id())},
                           head_only=head_only)
            elif url.path == "/tags" or url.path.startswith("/tags/"):
                name = url.path[len("/tags/"):] or None
                self._text(200, _format_tags(tags(name)), head_only=head_only)
            else:
                self._record(head_only)

        def do_GET(self) -> None:
            self._read()

        def do_HEAD(self) -> None:
            self._read(head_only=True)

        def _read_chunked(self) -> bytes:
            """Join a chunked body. Raises ValueError on a short chunk, a
            missing chunk terminator, a bad size, or a stream that ends
            before the zero-length chunk."""
            parts = []
            while True:
                line = self.rfile.readline()
                if not line:
                    raise ValueError("stream ended before the last chunk")
                size = int(line.split(b";")[0].strip(), 16)
                if size == 0:
                    while True:
                        trailer = self.rfile.readline()
                        if not trailer:
                            raise ValueError("stream ended inside the trailer")
                        if not trailer.strip():
                            return b"".join(parts)
                chunk = self.rfile.read(size)
                if len(chunk) != size:
                    raise ValueError(f"chunk of {size} bytes ended after {len(chunk)}")
                if self.rfile.readline().strip():
                    raise ValueError("chunk not followed by an empty line")
                parts.append(chunk)

        def _store(self) -> None:
            # Body bytes are kept as they came. A chunked body is joined and
            # stored with its length, so the stored message is self-contained.
            coding = (self.headers.get("Transfer-Encoding") or "").strip().lower()
            if coding == "chunked":
                try:
                    body = self._read_chunked()
                except ValueError as e:
                    self._text(400, f"incomplete chunked body: {e}" + chr(10))
                    return
            elif coding:
                self._text(501, f"Transfer-Encoding {coding}: send Content-Length or Transfer-Encoding: chunked" + chr(10))
                return
            else:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    self._text(400, "Content-Length is not a number" + chr(10))
                    return
                body = self.rfile.read(length)
                if len(body) != length:
                    self._text(400, f"body ended after {len(body)} of {length} bytes" + chr(10))
                    return
            # Link: </12>; rel="parent" on the door names the record this
            # one answers. It is the post office's bookkeeping, like the id
            # and the time; the stored message is not touched.
            parent = resolve_link(self.headers.get("Link"))
            # message/http says the body IS an HTTP message. Store the body
            # itself and drop this request's envelope, so a response (or a
            # request) can be archived as what it is, not wrapped in a POST.
            # This is the HTTP twin of `curldb add`.
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype == "message/http":
                kind = parse_envelope(body)["kind"]
                if kind not in ("request", "response"):
                    self._text(400, "message/http body must start with a status line or a request line" + chr(10))
                    return
                rid = add(body, parent=parent)
                self._text(201, f"#{rid}" + chr(10), {"Location": f"/{rid}"})
                return
            # http.server decodes the request line and headers as
            # latin-1; re-encode to recover the UTF-8 bytes the client
            # actually sent, otherwise non-ASCII names, values and paths
            # would be stored double-encoded.
            path = _utf8_from_latin1(self.path)
            lines = [f"{self.command} {path} {self.request_version}"]
            # A chunked message is stored joined: Transfer-Encoding goes,
            # and so does any Content-Length it carried, replaced by the
            # length of the bytes actually stored.
            skip = {"transfer-encoding", "content-length"} if coding else set()
            lines.extend(f"{_utf8_from_latin1(k)}: {_utf8_from_latin1(v)}"
                         for k, v in self.headers.items()
                         if k.lower() not in skip)
            if coding:
                lines.append(f"Content-Length: {len(body)}")
            raw = ("\n".join(lines) + "\n\n").encode("utf-8") + body
            rid = add(raw, parent=parent)
            self._text(201, f"#{rid}\n", {"Location": f"/{rid}"})

        do_POST = _store
        do_PUT = _store
        do_PATCH = _store
        do_DELETE = _store

        def log_message(self, fmt: str, *args) -> None:
            sys.stderr.write(f"{self.command} {self.path} {args[1] if len(args) > 1 else ''}\n")

    try:
        httpd = HTTPServer((host, port), Handler)
    except PermissionError:
        _die(f"port {port} needs root on this OS; try: curldb serve 8200")
    except OSError as exc:
        _die(f"cannot bind {host}:{port}: {exc}")
    sys.stderr.write(f"curldb {__version__} on http://{host}:{port}/  db={db_path()}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

# -----------------------------------------------
# CLI
# -----------------------------------------------


def _ts_short(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def _label(r: dict) -> str:
    if r["kind"] == "response":
        return f"{r['status']:>4}"
    if r["kind"] == "request":
        return f"{r['method']} {r['path']}"
    path = r["path"] or ""
    if len(path) > 14:
        path = "..." + path[-11:]
    return f"{r['kind']} {path}".rstrip()


def _format_results(results: list[dict]) -> str:
    if not results:
        return "(no matches)\n"
    lines = []
    for r in results:
        preview = r["preview"].replace("\n", " ")[:60]
        re_ = f"re {r['parent']}" if r.get("parent") else ""
        lines.append(f"  {r['id']:>5}  {_ts_short(r['ts'])}  {_label(r):<20}  {re_:<8}{preview}")
    return "\n".join(lines) + "\n"


def _format_tags(t: dict[str, list[tuple[str, int]]]) -> str:
    if not t:
        return "(no headers)\n"
    lines = []
    for n, values in t.items():
        shown = " ".join(f"{v}({c})" for v, c in values[:12])
        more = f" +{len(values) - 12}" if len(values) > 12 else ""
        lines.append(f"  {n}: {shown}{more}")
    return "\n".join(lines) + "\n"


def _format_stats(s: dict) -> str:
    lines = [
        f"  records: {s['count']}  {s['kinds']}",
        f"  size:    {s['size_bytes'] / 1024:.1f} KB",
        f"  path:    {s['path']}",
        f"  fts:     {'trigram' if s['trigram'] else 'unicode61 + LIKE'}",
    ]
    return chr(10).join(lines) + chr(10)


def _print_results(results: list[dict]) -> None:
    sys.stdout.write(_format_results(results))


def _int_arg(args: list[str], i: int, usage: str) -> int:
    if len(args) <= i:
        _die(f"usage: {usage}")
    try:
        return int(args[i])
    except ValueError:
        _die(f"not an id: {args[i]}")
    return 0


def _die(msg: str) -> None:
    print(f"ERR {msg}", file=sys.stderr)
    sys.exit(1)


def _write_raw(data: bytes) -> None:
    """Records leave exactly as stored: bytes, no newline translation."""
    sys.stdout.flush()
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


HELP = """\
curldb -- HTTP exchange datastore (one sqlite file per session)

  add [file]           store a raw HTTP request/response, a markdown file
                       with --- front matter, or any bytes (stdin or file);
                       --parent <id> names the record this one answers
  wrap START [-H N:V]  wrap stdin body in an envelope; START is a status
                       code (200) or a request line (POST /chat)
  get <id>             print raw record by id
  headers <id>         print headers of a record
  query '<expr>'       search records
  tags [name]          every header name/value seen, with counts
  ls [n]               list recent records (default 20)
  stats                db stats
  serve [port]         HTTP door on 127.0.0.1 (default 200; root below 1024 on unix):
                       POST/PUT anything -> stored as received, 201 + Location
                       Link: </id>; rel="parent" on the POST -> this record
                       answers that one (also read from the message's own Link)
                       POST with Content-Type: message/http -> the body is the
                       record (a response or request stored as itself)
                       GET /<id> -> the stored bytes as they are: message/http for
                       a request or response, text/markdown for a note, text/plain
                       for raw text, application/octet-stream for other bytes;
                       X-Id, X-Kind, X-Status, X-Method, X-Path,
                       Last-Modified, X-Last, Link prev/next describe it, so
                       HEAD /<id> is one line of ls
                       GET /?q=<expr>, GET /tags[/<name>], GET /stats -> same as the CLI
                       GET / and HEAD / carry X-Last: <highest id> (the log tail)

query DSL (tokens AND'd together):
  kind=request|response|note|raw   status=200   status=200,201
  method=POST   path=/chat   path~/tool/   parent=12
  header:X-Verdict   header:X-Verdict=solid
  body~word   body~"a phrase"   anyword

db: --db PATH, or CURLDB_PATH, else ./curldb.sqlite
"""


def cli(argv: list[str] | None = None) -> None:
    args = list(argv if argv is not None else sys.argv[1:])

    # Listings are UTF-8 regardless of the console's locale (Windows
    # consoles default to a legacy code page and would mangle CJK).
    # Records themselves go in and out through the binary streams.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if "--db" in args:
        i = args.index("--db")
        if i + 1 >= len(args):
            _die("--db needs a path")
        os.environ["CURLDB_PATH"] = args[i + 1]
        del args[i:i + 2]

    if not args or args[0] in ("-h", "--help", "help"):
        print(HELP)
        return

    cmd, rest = args[0], args[1:]

    if cmd == "add":
        source = None
        parent = None
        if "--parent" in rest:
            i = rest.index("--parent")
            parent = _int_arg(rest, i + 1, "add --parent <id>")
            del rest[i:i + 2]
        if rest and rest[0] != "-":
            source = rest[0]
            with open(source, "rb") as f:
                data = f.read()
        else:
            data = sys.stdin.buffer.read()
        if not data.strip():
            _die("empty input")
        rid = add(data, source_path=source, parent=parent)
        p = parse_envelope(data, source)
        label = (p["status"] if p["kind"] == "response"
                 else f"{p['method']} {p['path']}" if p["kind"] == "request"
                 else f"{p['kind']} {p['path'] or ''}".rstrip())
        print(f"#{rid} {label} +{len(p['headers'])} headers")

    elif cmd == "wrap":
        if not rest:
            _die("usage: wrap START [-H Name:value]...")
        start = rest[0]
        hdrs: list[tuple[str, str]] = []
        i = 1
        while i < len(rest):
            if rest[i] == "-H" and i + 1 < len(rest):
                n, _, v = rest[i + 1].partition(":")
                hdrs.append((n.strip(), v.strip()))
                i += 2
            else:
                _die(f"unexpected argument: {rest[i]}")
        body = sys.stdin.buffer.read()
        _write_raw(wrap(start, body, hdrs))

    elif cmd in ("get", "export"):
        raw = get(_int_arg(rest, 0, f"{cmd} <id>"))
        if raw is None:
            _die("not found")
        _write_raw(raw)

    elif cmd == "headers":
        hdrs = headers_of(_int_arg(rest, 0, "headers <id>"))
        if not hdrs:
            print("(no headers)")
        for n, v in hdrs:
            print(f"  {n}: {v}")

    elif cmd == "query":
        if not rest:
            _die("usage: query '<expr>'")
        _print_results(query(" ".join(rest)))

    elif cmd == "tags":
        sys.stdout.write(_format_tags(tags(rest[0] if rest else None)))

    elif cmd == "serve":
        serve(int(rest[0]) if rest else DEFAULT_PORT)

    elif cmd == "ls":
        _print_results(ls(int(rest[0]) if rest else 20))

    elif cmd == "stats":
        sys.stdout.write(_format_stats(stats()))

    else:
        _die(f"unknown: {cmd}")


if __name__ == "__main__":
    cli()
