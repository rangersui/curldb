# curldb

HTTP exchange datastore. One SQLite file per session; the requests and responses of an AI conversation stored as-is, queried by envelope fields.

curl is the reference client, hence the name: every operation the server accepts is one curl line, and the stored record is the text of what curl sent: start line, headers, body.

[中文说明在后面](#中文)

## What it does

Every message in an LLM conversation is an HTTP envelope: what you type is a request, what the model answers is a response, a tool call is a request plus a response. curldb stores each envelope verbatim in SQLite, parses status / method / path / headers into an index, and full-text indexes the body.

```
what you type ---------> curldb wrap 'POST /chat' -H X-Topic:x ---+
model reply (HTTP/1.1 200 OK ...) --------------------------------+--> curldb add --> session.sqlite
tool call (POST /tool/Read ... / HTTP/1.1 200 OK ...) ------------+
                                                                        |
                                         curldb query 'status=409 header:X-Scope=design'
                                         curldb tags
```

raw is the source of truth. Every index is parsed from raw and can be rebuilt.

## Three principles

**One session, one file.** The file name is the session identity: `curldb --db 2026-09-11-design.sqlite` or `CURLDB_PATH`, default `curldb.sqlite` in the current directory. Searching across sessions is querying each file in turn.

**Headers are tags, not a schema.** Names and values need no agreement up front. `X-Verdict: shaky` is unreadable to a conventional program and readable to an LLM. The program stores, counts and lists; `curldb tags` prints every header name and value this file has seen, so you look first, then query. The vocabulary grows out of the data.

**The body is never touched.** Whoever adds headers (you, the model, an annotating model, a hook) adds headers only. Annotators sign with `Via:` so records tagged by different models stay distinguishable.

## How messages get in

### Model replies

Give the model a system prompt like [SYSTEM.md](SYSTEM.md) so its output carries its own envelope: technical output starts with `HTTP/1.1 200 OK` and headers such as `X-Verdict` or `X-Scope`; small talk starts with `PUBLISH topic/path`. The first line decides the pipe:

```
HTTP/     -> curldb add
PUBLISH   -> mosquitto_pub, or append to a log file; never stored
```

### What you type

Type as usual, no HTTP. An adapter wraps it:

```bash
echo 'what about tool calls' | curldb wrap 'POST /chat' -H X-Topic:tool-call | curldb add
```

`wrap` adds only the start line, a `Date:` and the headers you pass; the body is untouched. To tag your own words, tee a copy to a cheap annotating model that emits header lines only, merge them back into the envelope, store. You stay out of the loop.

### Tool calls

A tool call is already a request and its result already a response. The translation is mechanical, no model involved:

```
tool name     -> POST /tool/Read
args (JSON)   -> body, Content-Type: application/json
result        -> response body
success/error -> status 200 / 4xx / 5xx
```

It lives in the harness hook (Claude Code's PreToolUse / PostToolUse):

```bash
# PreToolUse
echo "$ARGS_JSON" | curldb wrap "POST /tool/$TOOL" -H Content-Type:application/json -H X-Tool:$TOOL | curldb add
# PostToolUse
echo "$RESULT" | curldb wrap 200 -H X-Tool:$TOOL | curldb add
```

A turn that mixes prose with several tool calls is split at protocol boundaries and each piece is stored on its own.

Envelopes are stored as UTF-8. Tool args produced by Python's `json.dumps` with default settings escape non-ASCII to `\u4e2d`; they are stored escaped and full-text search will not find the characters. Use `json.dumps(args, ensure_ascii=False)` in the hook.

### Notes and files

The `---` YAML front matter at the top of a markdown file is header + body too. `curldb add note.md` reads a local file: `kind=note`, the file path becomes `path`, raw is the whole file. The front matter is flattened into the headers index:

```
title: hello              ->  title: hello
tags: [rust, iot]         ->  tags: rust / tags: iot        (list = repeated name)
metadata:                 ->  metadata.type: feedback       (nesting = dotted name)
  type: feedback
desc: |                   ->  desc: first line second line  (block scalar = lines joined)
  first line
  second line
```

Supported is the common front matter subset: `key: value`, indentation nesting, `- item` and `[a, b]` lists, `|` / `>` blocks, quotes. Anchors and inline maps are stored as text. A file without front matter is `kind=raw`, the whole file is the body, and it is searchable all the same.

One file per call; a whole directory is a shell loop:

```bash
for f in vault/*.md; do curldb add "$f"; done
curldb tags                     # the tag panel of this vault
curldb query 'header:tags=iot'
```

### Network door

`curldb serve` opens an HTTP door onto the same file. It is a peer of the CLI, operation for operation:

```
POST /<anything>     stored as received (request line, all headers, body), 201 + Location: /<id>
                     Link: </id>; rel="parent" on the POST names the record this one answers
POST /  + Content-Type: message/http
                     the body is the record: a response or a request stored as itself, no outer envelope
GET  /<id>           the stored bytes as they are; Content-Type says what they are
HEAD /<id>           the headers alone
GET  /?q=<expr>      same as curldb query
GET  /tags[/<name>]  same as curldb tags
GET  /stats          same as curldb stats
```

The body of `GET /<id>` is the raw column, and `Content-Type` names it: `message/http` for a request or response, `text/markdown` for a note, `text/plain` for raw text, `application/octet-stream` for raw bytes that are not text. The other headers describe the record: `X-Id`, `X-Kind`, `X-Status` or `X-Method`, `X-Path`, `Last-Modified` (when it was stored), `X-Last` (the tail), and `Link` with `rel="prev"` and `rel="next"`. `HEAD /<id>` is one line of `curldb ls` in header form.

A record can name the record it answers. Either the message itself carries `Link: </12>; rel="parent"`, or the POST that delivers it does; `curldb add --parent 12` is the same thing from the CLI. The Link target is a record id (`</12>`) or an address, matched against the `Content-Location` header of an earlier record (the pi extension writes `Content-Location: /chat/<id>` on every message and links parents that way). The pairing is kept in the index, so `GET /13` answers with `Link: </12>; rel="parent"`, `parent=12` lists every reply to 12, and one request can have any number of replies.

`GET /` and `HEAD /` answer with `X-Last: <highest id>`. Ids are never reused, so that number is both the record count and the tail of the log; a consumer polls `HEAD /` and reads `/<n>` from where it left off.

The received request is the envelope; nothing to wrap:

```bash
curldb serve                                       # 127.0.0.1:200, --db picks the file
curl -X POST localhost:200/chat -H 'X-Topic: fork' -d 'what about fork?'
curl localhost:200/42
curl 'localhost:200/?q=status=409'
```

The default port is 200. Ports below 1024 need root on Linux/macOS; `curldb serve 8200` avoids sudo.

An AI can curl straight in. A reply that already is an HTTP message (a Codex review starting with `HTTP/1.1 409 Conflict`, or a message read back from another curldb) is stored as itself by sending it with `Content-Type: message/http`; that is `curldb add` over the wire. The same message sent as an ordinary POST body would be stored one envelope deeper, as that POST.

A record is bytes. A `message/http` body is stored as sent, line endings included. An ordinary POST or PUT is rebuilt from the parsed request line and headers with LF line endings, followed by the body bytes as they arrived. The index and the full-text search cover the parts that are UTF-8 text: the start line, the headers, and a text body. A binary body (an image, an archive; anything that is not UTF-8 or contains a NUL byte) is stored and returned as it came in and has an empty preview. A chunked body is joined and stored with a `Content-Length` header in place of `Transfer-Encoding`, so the stored message is complete on its own.

Bound to localhost, no token. The trust model is the CLI's: whoever can run curl on this machine.

### Reading a file in the browser

[curldb.ai/viewer.html](https://curldb.ai/viewer.html) (`docs/viewer.html` plus `docs/viewer/` in the repo: markup, stylesheet, `render.js` for parsing and rendering, `app.js` for the page) opens a session file dropped onto it: same query language, a tag panel, one record at a time with its raw envelope. Three panes, mail-client style: tags, list, reading pane; each side folds, the reading pane can take the full width, and the widths drag. Keys: `j`/`k` move, `Enter` opens, `Esc` closes, `/` searches, `[` and `]` fold the side panes, `f` is full width. A body is shown by the `Content-Type` the message itself carries: images, audio, video and PDF render; HTML renders in a sandboxed frame with no scripts and no network; JSON is pretty-printed; markdown, CSV, diffs, form data, nested `message/http` and multipart parts render as what they are; everything else is text, and bytes that are not text get a hex dump. Above every record the viewer prints the receipt: the headers `GET /<id>` would answer with (`X-Id`, `X-Kind`, `Last-Modified`, `Link` with parent, prev and next), so the pairing is visible as header lines. `source` shows the record as stored, `save raw` downloads the record bytes, `save body` the body alone, `copy as curl` turns a stored request back into a curl command. Related records (the parent and the children of the open record) sit in a folded section; expanding it previews them with the interval between them. The list marks a reply with `re #12` and the interval since its parent. Search folds to a summary chip when not being edited; `?` shows the syntax and the keys. SQLite runs in the tab as WebAssembly; the file never leaves the machine and `serve` is not involved. Chrome refuses to open files under system folders such as `AppData` through the live picker (the message mentions system files); keep session files in a normal folder. `serve` stays curl-only and sends no CORS headers, so no web page can read the archive through it.

## Querying afterwards

```bash
curldb tags                                  # which headers exist in this file, with values
curldb tags X-Verdict                         # every value of one header

curldb query 'status=400'                     # the model said you were wrong
curldb query 'status=409'                     # conflicts with something established
curldb query 'header:X-Verdict=shaky'         # conclusions that did not hold
curldb query 'kind=request path=/chat'        # things you said
curldb query 'header:X-Tool status=500'       # failed tool calls
curldb query 'path~/tool/ body~timeout'       # tool calls that timed out
curldb query 'status=200 header:X-Scope=design body~protocol'

curldb get 42                                 # the raw envelope
curldb ls 50                                  # the latest 50
```

## Usage

```
curldb add [file]           store one envelope from stdin or a file: HTTP request/response, or markdown with front matter
curldb wrap START [-H N:V]  wrap the stdin body in an envelope; START is a status code (200) or a request line (POST /chat)
curldb get <id>             print the raw envelope by id
curldb headers <id>         print the headers of a record
curldb query '<expr>'       search
curldb tags [name]          header names and values, with counts
curldb ls [n]               the latest n records (default 20)
curldb stats                database status
curldb serve [port]         HTTP door on 127.0.0.1, default 200

--db PATH or CURLDB_PATH picks the file, default ./curldb.sqlite
```

### Query DSL

All conditions are ANDed, separated by spaces:

```
kind=request|response|note|raw
status=200              status code
status=200,201          status in set
method=POST             request method
path=/chat              request path equals
path~/tool/             request path contains
header:X-Verdict        header exists
header:X-Verdict=solid  header value equals
body~fork               body full-text search
body~"exact phrase"     body phrase search
anyword                 bare word, body search
```

Full-text search uses FTS5 with the trigram tokenizer (SQLite 3.34+), so CJK substrings match directly; older SQLite falls back to unicode61 + LIKE, and `curldb stats` shows which one is in use.

## Install

```bash
pip install curldb
```

Or copy the one file:

```bash
cp curldb.py ~/.local/bin/curldb
chmod +x ~/.local/bin/curldb
```

Zero dependencies. Python 3.10+, standard-library sqlite3.

## Development and verification

Run the standard-library tests from the repository root, no test dependencies needed:

```bash
python -m unittest discover -s tests -v
```

The tests cover raw round trips (UTF-8, LF / CRLF / CR), combined queries, CJK search and the tokenizer fallback, front matter, legacy layout migration, the CLI and the local HTTP door. Databases live in temporary directories; the HTTP tests use a system-assigned port.

Build and check a release:

```bash
python -m pip install build twine
python -m build
python -m twine check --strict dist/*
python scripts/check_dist.py dist
```

`dist` should hold exactly one wheel and one sdist from this build. The check script verifies license, version, zero runtime dependencies and sdist contents, then installs the wheel offline into a temporary virtual environment and exercises the installed command line. The sdist contains `SYSTEM.md`, the tests and the check script.

GitHub Actions runs the tests on Windows and Linux with Python 3.10 and 3.14, plus a build-and-install check of the release artifacts.

## Design choices

- **Verbatim storage** -- the raw envelope goes into a SQLite TEXT column as-is.
- **Rebuildable index** -- kind / status / method / path / headers / body_fts are all parsed from raw.
- **Lenient parsing** -- accepts `\n` and `\r\n`, ignores malformed headers, Content-Length optional. It is a document parser.
- **One session, one file** -- backup is `cp`, sync is rsync.
- **One CLI** -- in, out, exit. `serve` is a second door onto the same cabinet, opened when wanted.
- **Append only** -- no update, no delete.
- **Stores HTTP messages, queries HTTP messages** -- `GET /<id>` returns the stored message itself (`message/http`). A stored message is always data; it is never replayed as the server's own reply. To view HTML an AI wrote: `curldb get 42 > x.html` and open it locally.

---

# 中文

HTTP exchange 原生存储。一个 session 一个 SQLite 文件,AI 对话里的 request 和 response 原样存,按信封字段查。

curl 是参考客户端,名字由此而来:server 接受的每个操作都是一行 curl,存下来的记录就是 curl 发出去的那段文本:请求行、header、body。

## 干嘛的

LLM 对话里的每条消息都是一个 HTTP 信封:你的话是 request,模型的回答是 response,工具调用是 request + response。curldb 把信封原样存进 SQLite,解析出 status / method / path / headers 建索引,body 做全文索引。

```
你的话 -----------> curldb wrap 'POST /chat' -H X-Topic:x ---+
模型的回答 (HTTP/1.1 200 OK ...) ---------------------------+--> curldb add --> session.sqlite
工具调用 (POST /tool/Read ... / HTTP/1.1 200 OK ...) --------+
                                                                   |
                                    curldb query 'status=409 header:X-Scope=design'
                                    curldb tags
```

raw 是源数据,所有索引从 raw 解析出来,丢了可以重建。

## 三条原则

**一个 session 一个文件。** session 的身份就是文件名,`curldb --db 2026-09-11-design.sqlite` 或 `CURLDB_PATH`,默认当前目录的 `curldb.sqlite`。跨 session 查就是对多个文件各查一遍。

**header 是 tag,不是 schema。** 名字和值都不用事先约定。`X-Verdict: shaky` 这种传统程序读不懂的东西,读它的是 LLM。程序只管存、数、列;`curldb tags` 把这个库里出现过的 header 名和值全列出来,看一眼再查。词表是从数据里长出来的。

**body 永远原样。** 加 header 的人(你、模型、标注模型、hook)只加 header,谁都不改 body。标注者用 `Via:` 留名,以后换模型重标,新旧记录能区分。

## 三种消息怎么进来

### 模型的回答

给模型配 [SYSTEM.md](SYSTEM.md) 那样的 system prompt,让输出自带信封:技术产出以 `HTTP/1.1 200 OK` 开头,带 `X-Verdict`、`X-Scope` 这类 header;闲聊以 `PUBLISH topic/path` 开头。第一行决定这条消息走哪条管道:

```
HTTP/     -> curldb add
PUBLISH   -> mosquitto_pub,或 append 到日志文件;不进库
```

### 你的话

你照常打字,不用写 HTTP。adapter 套信封:

```bash
echo '那 tool call 呢' | curldb wrap 'POST /chat' -H X-Topic:tool-call | curldb add
```

`wrap` 只加 start line、`Date:` 和你给的 header,body 一个字不动。要给你的话打 tag,tee 一份给一个便宜的标注模型,它只出 header 行,合回信封再入库。你不在环里。

### 工具调用

tool call 本来就是 request,结果本来就是 response,机械翻译,不需要模型:

```
tool name     -> POST /tool/Read
args (JSON)   -> body, Content-Type: application/json
result        -> response body
success/error -> status 200 / 4xx / 5xx
```

放在 harness 的 hook 里(Claude Code 的 PreToolUse / PostToolUse):

```bash
# PreToolUse
echo "$ARGS_JSON" | curldb wrap "POST /tool/$TOOL" -H Content-Type:application/json -H X-Tool:$TOOL | curldb add
# PostToolUse
echo "$RESULT" | curldb wrap 200 -H X-Tool:$TOOL | curldb add
```

一个 turn 里 prose 和多个 tool call 混着,按协议边界切开各存各的。

信封一律按 UTF-8 存。tool args 如果是用 Python 的 `json.dumps` 默认参数生成的,中文会变成 `\u4e2d` 这种转义,存进去就是转义,全文搜索搜不到中文;hook 里用 `json.dumps(args, ensure_ascii=False)`。

### 笔记和文件

markdown 顶上那段 `---` 夹着的 YAML front matter 也是 header + body。`curldb add note.md` 直接读本地文件,`kind=note`,文件路径当 `path`,raw 是整个文件原文。front matter 打平进 headers 索引:

```
title: hello              ->  title: hello
tags: [rust, iot]         ->  tags: rust / tags: iot        (列表 = 同名重复)
metadata:                 ->  metadata.type: feedback       (嵌套 = 点号)
  type: feedback
desc: |                   ->  desc: first line second line  (多行块按空格拼)
  first line
  second line
```

支持的是 front matter 常见子集:`key: value`、缩进嵌套、`- item` 和 `[a, b]` 列表、`|` / `>` 块、引号。锚点和行内 map 当文本存。没有 front matter 的文件 `kind=raw`,整个文件是 body,一样能搜。

一次一个文件,整个目录用 shell 循环:

```bash
for f in vault/*.md; do curldb add "$f"; done
curldb tags                     # 这个 vault 的 tag 面板
curldb query 'header:tags=iot'
```

### 网络入口

`curldb serve` 给同一个文件开一个 HTTP 门,和 CLI 平级,功能一一对应:

```
POST /<anything>     收到什么存什么(请求行、所有 header、body),201 + Location: /<id>
                     POST 上带 Link: </id>; rel="parent",表示这条是回哪条的
POST /  + Content-Type: message/http
                     body 本身就是记录:一条 response 或 request 按它本来的样子存,不套外层
GET  /<id>           存的字节原样,Content-Type 说明它是什么
HEAD /<id>           只要 header
GET  /?q=<expr>      同 curldb query
GET  /tags[/<name>]  同 curldb tags
GET  /stats          同 curldb stats
```

`GET /<id>` 的 body 是 raw 列,`Content-Type` 说明它是什么:request 和 response 是 `message/http`,note 是 `text/markdown`,raw 是 `text/plain`,不是文本的 raw 字节是 `application/octet-stream`。其余 header 描述这条记录:`X-Id`、`X-Kind`、`X-Status` 或 `X-Method`、`X-Path`、`Last-Modified`(存入时间)、`X-Last`(尾巴)、`Link` 的 `rel="prev"` 和 `rel="next"`。`HEAD /<id>` 就是 `curldb ls` 里的一行,换成 header 的样子。

一条记录可以说明自己是回哪条的。消息里自己带 `Link: </12>; rel="parent"`,或者送它进来的那个 POST 带,效果一样;命令行是 `curldb add --parent 12`。Link 指向的可以是记录编号(`</12>`),也可以是一个地址,按更早那条记录的 `Content-Location` 头匹配(pi 扩展给每条消息写 `Content-Location: /chat/<id>`,就是这么配的)。配对记在索引里,所以 `GET /13` 的外层带 `Link: </12>; rel="parent"`,`parent=12` 列出回 12 的所有记录,一个请求可以有任意多个回复。

`GET /` 和 `HEAD /` 带 `X-Last: <最大编号>`。编号不复用,所以这个数既是记录总数也是日志的尾巴;消费者 `HEAD /` 看尾巴动没动,从自己记住的位置往后 `GET /<n>`。

收到的请求本身就是信封,不用 wrap:

```bash
curldb serve                                       # 127.0.0.1:200,--db 选文件
curl -X POST localhost:200/chat -H 'X-Topic: fork' -d 'what about fork?'
curl localhost:200/42
curl 'localhost:200/?q=status=409'
```

端口默认 200。1024 以下在 Linux/macOS 要 root,不想 sudo 就 `curldb serve 8200`。

AI 可以直接 curl 进来。本身已经是 HTTP 消息的东西(以 `HTTP/1.1 409 Conflict` 开头的 Codex review,或者从另一个 curldb 读出来的一条消息)带上 `Content-Type: message/http` 发过来,就按它本来的样子存,这是 HTTP 版的 `curldb add`。同一条消息当普通 POST body 发,会多套一层,存的是那个 POST。

记录是字节。`message/http` 的 body 照发来的样子存,换行也保留。普通 POST/PUT 的请求行和 header 从解析结果重建,换行用 LF,后面接 body 的原始字节。索引和全文搜索覆盖其中是 UTF-8 文本的部分:起始行、header、文本 body。二进制 body(图片、压缩包,凡是不是 UTF-8 或含 NUL 字节的)原样存原样取,预览为空。chunked 的 body 拼起来存,`Transfer-Encoding` 换成 `Content-Length`,存下来的消息自己就是完整的。

只绑 localhost,没有 token:信任模型和 CLI 一样,能在这台机器上跑 curl 的人。

### 在浏览器里看一个文件

[curldb.ai/viewer.html](https://curldb.ai/viewer.html)(仓库里是 `docs/viewer.html` 加 `docs/viewer/`:页面、样式、负责解析渲染的 `render.js`、负责页面逻辑的 `app.js`)把 session 文件拖进去就能看:同一套查询语法、tag 面板、逐条看原始信封。三栏,邮件客户端的样子:tag、列表、阅读窗;两边都能收起,阅读窗可以铺满,宽度可拖。按键:`j`/`k` 上下,`Enter` 打开,`Esc` 关闭,`/` 搜索,`[` 和 `]` 收放两侧,`f` 铺满。body 按消息自己带的 `Content-Type` 显示:图片、音频、视频、PDF 直接渲染;HTML 在 sandbox 的 iframe 里渲染,不跑脚本不联网;JSON 格式化;markdown、CSV、diff、表单、套在里面的 `message/http`、multipart 各按本来的样子渲染;其余当文本,不是文本的字节给十六进制。每条记录上方先印一段收据,就是 `GET /<id>` 外层会回的那几个 header(`X-Id`、`X-Kind`、`Last-Modified`、带 parent、prev、next 的 `Link`),配对关系直接以 header 的形式看得见。`source` 看存的原样,`save raw` 下载整条记录的字节,`save body` 只下载 body,`copy as curl` 把存的请求变回一条 curl 命令。打开一条记录,它的 parent 和 children 收在一个折叠区里,展开才渲染,连同彼此之间的间隔。列表里回复标 `re #12` 和距离 parent 的间隔。搜索框不编辑时折成一个摘要,`?` 展开语法和按键。SQLite 以 WebAssembly 跑在标签页里,文件不离开这台机器,和 `serve` 无关。Chrome 的 live 选择器不让打开 `AppData` 这类系统目录下的文件(提示里会说系统文件),session 文件放在普通目录里。`serve` 只给 curl 用,不发 CORS 头,任何网页都读不到档案。

## 事后查

```bash
curldb tags                                  # 这个库里有哪些 header,值是什么
curldb tags X-Verdict                         # 一个 header 的所有值

curldb query 'status=400'                     # 模型说你说错了
curldb query 'status=409'                     # 和已定的东西冲突
curldb query 'header:X-Verdict=shaky'         # 站不住的结论
curldb query 'kind=request path=/chat'        # 你说过的话
curldb query 'header:X-Tool status=500'       # 失败的工具调用
curldb query 'path~/tool/ body~timeout'       # 超时的工具调用
curldb query 'status=200 header:X-Scope=design body~协议'

curldb get 42                                 # 原始信封
curldb ls 50                                  # 最近 50 条
```

## 用法

```
curldb add [file]           从 stdin 或文件存一个信封:HTTP request/response,或带 front matter 的 markdown
curldb wrap START [-H N:V]  给 stdin 的 body 套信封;START 是 status code (200) 或 request line (POST /chat)
curldb get <id>             按 id 取原始信封
curldb headers <id>         看某条的 headers
curldb query '<expr>'       查询
curldb tags [name]          header 名和值,带计数
curldb ls [n]               最近 n 条(默认 20)
curldb stats                数据库状态
curldb serve [port]         HTTP 门,127.0.0.1,默认 200

--db PATH 或 CURLDB_PATH 选文件,默认 ./curldb.sqlite
```

### query DSL

所有条件 AND 连接,空格分隔:

```
kind=request|response|note|raw
status=200              status code
status=200,201          status in set
method=POST             request method
path=/chat              request path 等于
path~/tool/             request path 包含
header:X-Verdict        header 存在
header:X-Verdict=solid  header 值等于
body~fork               body 全文搜索
body~"exact phrase"     body 短语搜索
parent=12               回 12 号的记录
anyword                 裸词,body 搜索
```

全文索引用 FTS5 trigram(SQLite 3.34+),中文子串直接搜;老 SQLite 自动退到 unicode61 + LIKE,`curldb stats` 里能看到用的哪个。

## 安装

```bash
pip install curldb
```

或者就一个文件,复制走:

```bash
cp curldb.py ~/.local/bin/curldb
chmod +x ~/.local/bin/curldb
```

零依赖。Python 3.10+,标准库 sqlite3。

## 开发与验证

在仓库根目录运行标准库测试,不需要安装测试依赖:

```bash
python -m unittest discover -s tests -v
```

测试覆盖原文存取(UTF-8、LF / CRLF / CR)、组合查询、中文搜索及 tokenizer 回退、front matter、旧库迁移、CLI 和本地 HTTP 入口。数据库都放在临时目录,HTTP 测试使用系统分配的临时端口。

构建和检查发行包:

```bash
python -m pip install build twine
python -m build
python -m twine check --strict dist/*
python scripts/check_dist.py dist
```

`dist` 中应只有本次构建的一份 wheel 和一份源码包。检查脚本核对许可证、版本、零运行时依赖和源码包内容,再在临时虚拟环境中离线安装 wheel,验证实际安装后的命令行存取。源码包包含 `SYSTEM.md`、测试和检查脚本。

GitHub Actions 在 Windows / Linux 的 Python 3.10 / 3.14 上运行测试,另有发行包构建与安装检查。

## 设计选择

- **原文存储** -- raw 信封原样进 SQLite TEXT 字段。
- **索引可重建** -- kind / status / method / path / headers / body_fts 都从 raw 解析出来。
- **宽松解析** -- 接受 `\n` 和 `\r\n`,忽略畸形 header,Content-Length 可选。这是文档解析器。
- **一个 session 一个文件** -- 备份是 `cp`,同步是 rsync。
- **一个 CLI** -- 进出,用完退出。`serve` 是同一个档案柜的第二扇门,想开就开。
- **只追加** -- 没有 update,没有 delete。
- **存 HTTP 消息,查 HTTP 消息** -- `GET /<id>` 回的是存的那条消息本身(`message/http`),存的消息永远是数据,不当 server 自己的回复回放。想看 AI 写的 HTML:`curldb get 42 > x.html`,本地打开。
