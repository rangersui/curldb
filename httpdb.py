#!/usr/bin/env python3
"""httpdb -- HTTP exchange datastore.

Raw HTTP requests and responses in, structured queries out.
One SQLite file per session, zero daemon, zero conversion.

    echo 'HTTP/1.1 200 OK\nX-Verdict: solid\n\nhello' | httpdb add
    echo 'what about fork?' | httpdb wrap 'POST /chat' -H X-Topic:fork | httpdb add
    httpdb query 'status=200 header:X-Verdict=solid body~hello'
    httpdb tags
    httpdb get 1
    httpdb ls
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
from http import HTTPStatus

__version__ = "0.1.0"

DEFAULT_DB = "httpdb.sqlite"

# -----------------------------------------------
# PARSE
# -----------------------------------------------

_STATUS_LINE = re.compile(r"HTTP/[\d.]+\s+(\d{3})")
_REQUEST_LINE = re.compile(r"([A-Z]+)\s+(\S+)\s+HTTP/[\d.]+")


def parse_envelope(raw: str, source_path: str | None = None) -> dict:
    """Parse raw text into its parts.

    Returns {kind, status, method, path, headers, body}. kind is
    "response" (status line first), "request" (request line first),
    "note" (a --- front matter block first), or "raw" (none of those;
    the whole text is the body). Notes and raw text take source_path
    as their path; HTTP envelopes carry their own.

    Lenient: accepts \\n or \\r\\n, ignores malformed headers,
    ignores Content-Length. This is a document parser, not a
    protocol parser.
    """
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


def wrap(start: str, body: str, headers: list[tuple[str, str]]) -> str:
    """Build an HTTP envelope around body.

    start is either a status code ("200") or a request line
    ("POST /chat"). A Date header is added when the caller did not
    supply one. body is stored byte-for-byte.
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
    return "\n".join(lines) + "\n\n" + body

# -----------------------------------------------
# DB
# -----------------------------------------------


def db_path() -> str:
    return os.environ.get("HTTPDB_PATH", DEFAULT_DB)


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


def add(raw: str, conn: sqlite3.Connection | None = None,
        ts: float | None = None, source_path: str | None = None) -> int:
    """Store one raw envelope. Returns the new record id."""
    own = conn is None
    if own:
        conn = _connect()
    p = parse_envelope(raw, source_path)
    cur = conn.execute(
        "INSERT INTO records (kind, status, method, path, raw, body, ts)"
        " VALUES (?,?,?,?,?,?,?)",
        (p["kind"], p["status"], p["method"], p["path"], raw, p["body"],
         ts if ts is not None else time.time()))
    rid = cur.lastrowid
    if p["headers"]:
        conn.executemany(
            "INSERT INTO headers (record_id, name, value) VALUES (?,?,?)",
            [(rid, n, v) for n, v in p["headers"]])
    conn.commit()
    if own:
        conn.close()
    return rid


def get(rid: int) -> str | None:
    conn = _connect()
    row = conn.execute("SELECT raw FROM records WHERE id=?", (rid,)).fetchone()
    conn.close()
    return row[0] if row else None

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
               substr(r.body, 1, 120)
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
            "path": r[4], "ts": r[5], "preview": r[6]}


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
        SELECT id, kind, status, method, path, ts, substr(body, 1, 80)
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

# -----------------------------------------------
# SERVE
# -----------------------------------------------
#
# A network door onto the same file. It archives the HTTP messages it
# receives and hands them back; it never interprets a stored message as
# its own reply. Same operations as the CLI, nothing more.


DEFAULT_PORT = 200   # HTTP 200. Below 1024, so root on Linux/macOS.


def _utf8_from_latin1(text: str) -> str:
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def serve(port: int = DEFAULT_PORT, host: str = "127.0.0.1") -> None:
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        server_version = f"httpdb/{__version__}"
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
            raw = get(int(rid))
            if raw is None:
                self._text(404, "not found\n", head_only=head_only)
                return
            data = raw.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "message/http")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if not head_only:
                self.wfile.write(data)

        def _read(self, head_only: bool = False) -> None:
            from urllib.parse import parse_qs, urlsplit
            url = urlsplit(self.path)
            if url.path == "/":
                q = parse_qs(url.query).get("q", [""])[0]
                results = query(q) if q else ls()
                self._text(200, _format_results(results), head_only=head_only)
            elif url.path == "/tags" or url.path.startswith("/tags/"):
                name = url.path[len("/tags/"):] or None
                self._text(200, _format_tags(tags(name)), head_only=head_only)
            else:
                self._record(head_only)

        def do_GET(self) -> None:
            self._read()

        def do_HEAD(self) -> None:
            self._read(head_only=True)

        def _store(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8", "replace")
            # http.server decodes the request line and headers as
            # latin-1; re-encode to recover the UTF-8 bytes the client
            # actually sent, otherwise non-ASCII names, values and paths
            # would be stored double-encoded.
            path = _utf8_from_latin1(self.path)
            lines = [f"{self.command} {path} {self.request_version}"]
            lines.extend(f"{_utf8_from_latin1(k)}: {_utf8_from_latin1(v)}"
                         for k, v in self.headers.items())
            raw = "\n".join(lines) + "\n\n" + body
            rid = add(raw)
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
        _die(f"port {port} needs root on this OS; try: httpdb serve 8200")
    except OSError as exc:
        _die(f"cannot bind {host}:{port}: {exc}")
    sys.stderr.write(f"httpdb {__version__} on http://{host}:{port}/  db={db_path()}\n")
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
        lines.append(f"  {r['id']:>5}  {_ts_short(r['ts'])}  {_label(r):<20}  {preview}")
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


def _write_raw(text: str) -> None:
    """Envelopes leave exactly as stored: bytes, no newline translation."""
    sys.stdout.flush()
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()


HELP = """\
httpdb -- HTTP exchange datastore (one sqlite file per session)

  add [file]           store a raw HTTP request/response, or a markdown
                       file with --- front matter (stdin or file)
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
                       GET /<id> -> the stored message (message/http)
                       GET /?q=<expr>, GET /tags[/<name>] -> same as the CLI

query DSL (tokens AND'd together):
  kind=request|response|note|raw   status=200   status=200,201
  method=POST   path=/chat   path~/tool/
  header:X-Verdict   header:X-Verdict=solid
  body~word   body~"a phrase"   anyword

db: --db PATH, or HTTPDB_PATH, else ./httpdb.sqlite
"""


def cli(argv: list[str] | None = None) -> None:
    args = list(argv if argv is not None else sys.argv[1:])

    # Envelopes are UTF-8 regardless of the console's locale (Windows
    # consoles default to a legacy code page and would mangle CJK).
    # Preserve input line endings too: raw envelopes are archival data.
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace", newline="")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if "--db" in args:
        i = args.index("--db")
        if i + 1 >= len(args):
            _die("--db needs a path")
        os.environ["HTTPDB_PATH"] = args[i + 1]
        del args[i:i + 2]

    if not args or args[0] in ("-h", "--help", "help"):
        print(HELP)
        return

    cmd, rest = args[0], args[1:]

    if cmd == "add":
        source = None
        if rest and rest[0] != "-":
            source = rest[0]
            with open(source, encoding="utf-8", newline="") as f:
                raw = f.read()
        else:
            raw = sys.stdin.read()
        if not raw.strip():
            _die("empty input")
        rid = add(raw, source_path=source)
        p = parse_envelope(raw, source)
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
        body = sys.stdin.read()
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
        s = stats()
        print(f"  records: {s['count']}  {s['kinds']}")
        print(f"  size:    {s['size_bytes'] / 1024:.1f} KB")
        print(f"  path:    {s['path']}")
        print(f"  fts:     {'trigram' if s['trigram'] else 'unicode61 + LIKE'}")

    else:
        _die(f"unknown: {cmd}")


if __name__ == "__main__":
    cli()
