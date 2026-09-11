# httpdb

HTTP exchange datastore. One SQLite file per session; the requests and responses of an AI conversation stored as-is, queried by envelope fields.

[中文说明在后面](#中文)

## What it does

Every message in an LLM conversation is an HTTP envelope: what you type is a request, what the model answers is a response, a tool call is a request plus a response. httpdb stores each envelope verbatim in SQLite, parses status / method / path / headers into an index, and full-text indexes the body.

```
what you type ---------> httpdb wrap 'POST /chat' -H X-Topic:x ---+
model reply (HTTP/1.1 200 OK ...) --------------------------------+--> httpdb add --> session.sqlite
tool call (POST /tool/Read ... / HTTP/1.1 200 OK ...) ------------+
                                                                        |
                                         httpdb query 'status=409 header:X-Scope=design'
                                         httpdb tags
```

raw is the source of truth. Every index is parsed from raw and can be rebuilt.

## Three principles

**One session, one file.** The file name is the session identity: `httpdb --db 2026-09-11-design.sqlite` or `HTTPDB_PATH`, default `httpdb.sqlite` in the current directory. Searching across sessions is querying each file in turn.

**Headers are tags, not a schema.** Names and values need no agreement up front. `X-Verdict: shaky` is unreadable to a conventional program and readable to an LLM. The program stores, counts and lists; `httpdb tags` prints every header name and value this file has seen, so you look first, then query. The vocabulary grows out of the data.

**The body is never touched.** Whoever adds headers (you, the model, an annotating model, a hook) adds headers only. Annotators sign with `Via:` so records tagged by different models stay distinguishable.

## How messages get in

### Model replies

Give the model a system prompt like [SYSTEM.md](SYSTEM.md) so its output carries its own envelope: technical output starts with `HTTP/1.1 200 OK` and headers such as `X-Verdict` or `X-Scope`; small talk starts with `PUBLISH topic/path`. The first line decides the pipe:

```
HTTP/     -> httpdb add
PUBLISH   -> mosquitto_pub, or append to a log file; never stored
```

### What you type

Type as usual, no HTTP. An adapter wraps it:

```bash
echo 'what about tool calls' | httpdb wrap 'POST /chat' -H X-Topic:tool-call | httpdb add
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
echo "$ARGS_JSON" | httpdb wrap "POST /tool/$TOOL" -H Content-Type:application/json -H X-Tool:$TOOL | httpdb add
# PostToolUse
echo "$RESULT" | httpdb wrap 200 -H X-Tool:$TOOL | httpdb add
```

A turn that mixes prose with several tool calls is split at protocol boundaries and each piece is stored on its own.

Envelopes are stored as UTF-8. Tool args produced by Python's `json.dumps` with default settings escape non-ASCII to `\u4e2d`; they are stored escaped and full-text search will not find the characters. Use `json.dumps(args, ensure_ascii=False)` in the hook.

### Notes and files

The `---` YAML front matter at the top of a markdown file is header + body too. `httpdb add note.md` reads a local file: `kind=note`, the file path becomes `path`, raw is the whole file. The front matter is flattened into the headers index:

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
for f in vault/*.md; do httpdb add "$f"; done
httpdb tags                     # the tag panel of this vault
httpdb query 'header:tags=iot'
```

### Network door

`httpdb serve` opens an HTTP door onto the same file. It is a peer of the CLI, operation for operation:

```
POST /<anything>     stored as received (request line, all headers, body), 201 + Location: /<id>
GET  /<id>           the whole stored message, Content-Type: message/http
HEAD /<id>           standard HEAD
GET  /?q=<expr>      same as httpdb query
GET  /tags[/<name>]  same as httpdb tags
```

The received request is the envelope; nothing to wrap:

```bash
httpdb serve                                       # 127.0.0.1:200, --db picks the file
curl -X POST localhost:200/chat -H 'X-Topic: fork' -d 'what about fork?'
curl localhost:200/42
curl 'localhost:200/?q=status=409'
```

The default port is 200. Ports below 1024 need root on Linux/macOS; `httpdb serve 8200` avoids sudo.

An AI can curl straight in. A Codex review sent as `PUT /review` is stored as that PUT request with the review in the body; to store it as a response, use `httpdb add` from the CLI.

Bound to localhost, no token. The trust model is the CLI's: whoever can run curl on this machine.

## Querying afterwards

```bash
httpdb tags                                  # which headers exist in this file, with values
httpdb tags X-Verdict                         # every value of one header

httpdb query 'status=400'                     # the model said you were wrong
httpdb query 'status=409'                     # conflicts with something established
httpdb query 'header:X-Verdict=shaky'         # conclusions that did not hold
httpdb query 'kind=request path=/chat'        # things you said
httpdb query 'header:X-Tool status=500'       # failed tool calls
httpdb query 'path~/tool/ body~timeout'       # tool calls that timed out
httpdb query 'status=200 header:X-Scope=design body~protocol'

httpdb get 42                                 # the raw envelope
httpdb ls 50                                  # the latest 50
```

## Usage

```
httpdb add [file]           store one envelope from stdin or a file: HTTP request/response, or markdown with front matter
httpdb wrap START [-H N:V]  wrap the stdin body in an envelope; START is a status code (200) or a request line (POST /chat)
httpdb get <id>             print the raw envelope by id
httpdb headers <id>         print the headers of a record
httpdb query '<expr>'       search
httpdb tags [name]          header names and values, with counts
httpdb ls [n]               the latest n records (default 20)
httpdb stats                database status
httpdb serve [port]         HTTP door on 127.0.0.1, default 200

--db PATH or HTTPDB_PATH picks the file, default ./httpdb.sqlite
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

Full-text search uses FTS5 with the trigram tokenizer (SQLite 3.34+), so CJK substrings match directly; older SQLite falls back to unicode61 + LIKE, and `httpdb stats` shows which one is in use.

## Install

```bash
pip install httpdb
```

Or copy the one file:

```bash
cp httpdb.py ~/.local/bin/httpdb
chmod +x ~/.local/bin/httpdb
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
- **Stores HTTP messages, queries HTTP messages** -- `GET /<id>` returns the stored message itself (`message/http`). A stored message is always data; it is never replayed as the server's own reply. To view HTML an AI wrote: `httpdb get 42 > x.html` and open it locally.

---

# 中文

HTTP exchange 原生存储。一个 session 一个 SQLite 文件,AI 对话里的 request 和 response 原样存,按信封字段查。

## 干嘛的

LLM 对话里的每条消息都是一个 HTTP 信封:你的话是 request,模型的回答是 response,工具调用是 request + response。httpdb 把信封原样存进 SQLite,解析出 status / method / path / headers 建索引,body 做全文索引。

```
你的话 -----------> httpdb wrap 'POST /chat' -H X-Topic:x ---+
模型的回答 (HTTP/1.1 200 OK ...) ---------------------------+--> httpdb add --> session.sqlite
工具调用 (POST /tool/Read ... / HTTP/1.1 200 OK ...) --------+
                                                                   |
                                    httpdb query 'status=409 header:X-Scope=design'
                                    httpdb tags
```

raw 是源数据,所有索引从 raw 解析出来,丢了可以重建。

## 三条原则

**一个 session 一个文件。** session 的身份就是文件名,`httpdb --db 2026-09-11-design.sqlite` 或 `HTTPDB_PATH`,默认当前目录的 `httpdb.sqlite`。跨 session 查就是对多个文件各查一遍。

**header 是 tag,不是 schema。** 名字和值都不用事先约定。`X-Verdict: shaky` 这种传统程序读不懂的东西,读它的是 LLM。程序只管存、数、列;`httpdb tags` 把这个库里出现过的 header 名和值全列出来,看一眼再查。词表是从数据里长出来的。

**body 永远原样。** 加 header 的人(你、模型、标注模型、hook)只加 header,谁都不改 body。标注者用 `Via:` 留名,以后换模型重标,新旧记录能区分。

## 三种消息怎么进来

### 模型的回答

给模型配 [SYSTEM.md](SYSTEM.md) 那样的 system prompt,让输出自带信封:技术产出以 `HTTP/1.1 200 OK` 开头,带 `X-Verdict`、`X-Scope` 这类 header;闲聊以 `PUBLISH topic/path` 开头。第一行决定这条消息走哪条管道:

```
HTTP/     -> httpdb add
PUBLISH   -> mosquitto_pub,或 append 到日志文件;不进库
```

### 你的话

你照常打字,不用写 HTTP。adapter 套信封:

```bash
echo '那 tool call 呢' | httpdb wrap 'POST /chat' -H X-Topic:tool-call | httpdb add
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
echo "$ARGS_JSON" | httpdb wrap "POST /tool/$TOOL" -H Content-Type:application/json -H X-Tool:$TOOL | httpdb add
# PostToolUse
echo "$RESULT" | httpdb wrap 200 -H X-Tool:$TOOL | httpdb add
```

一个 turn 里 prose 和多个 tool call 混着,按协议边界切开各存各的。

信封一律按 UTF-8 存。tool args 如果是用 Python 的 `json.dumps` 默认参数生成的,中文会变成 `\u4e2d` 这种转义,存进去就是转义,全文搜索搜不到中文;hook 里用 `json.dumps(args, ensure_ascii=False)`。

### 笔记和文件

markdown 顶上那段 `---` 夹着的 YAML front matter 也是 header + body。`httpdb add note.md` 直接读本地文件,`kind=note`,文件路径当 `path`,raw 是整个文件原文。front matter 打平进 headers 索引:

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
for f in vault/*.md; do httpdb add "$f"; done
httpdb tags                     # 这个 vault 的 tag 面板
httpdb query 'header:tags=iot'
```

### 网络入口

`httpdb serve` 给同一个文件开一个 HTTP 门,和 CLI 平级,功能一一对应:

```
POST /<anything>     收到什么存什么(请求行、所有 header、body),201 + Location: /<id>
GET  /<id>           整条存的消息,Content-Type: message/http
HEAD /<id>           标准 HEAD
GET  /?q=<expr>      同 httpdb query
GET  /tags[/<name>]  同 httpdb tags
```

收到的请求本身就是信封,不用 wrap:

```bash
httpdb serve                                       # 127.0.0.1:200,--db 选文件
curl -X POST localhost:200/chat -H 'X-Topic: fork' -d 'what about fork?'
curl localhost:200/42
curl 'localhost:200/?q=status=409'
```

端口默认 200。1024 以下在 Linux/macOS 要 root,不想 sudo 就 `httpdb serve 8200`。

AI 可以直接 curl 进来。Codex 的 review 以 `PUT /review` 发过来,存的是这个 PUT 请求,review 原文在 body 里;要把它当一条 response 存,走 CLI 的 `httpdb add`。

只绑 localhost,没有 token:信任模型和 CLI 一样,能在这台机器上跑 curl 的人。

## 事后查

```bash
httpdb tags                                  # 这个库里有哪些 header,值是什么
httpdb tags X-Verdict                         # 一个 header 的所有值

httpdb query 'status=400'                     # 模型说你说错了
httpdb query 'status=409'                     # 和已定的东西冲突
httpdb query 'header:X-Verdict=shaky'         # 站不住的结论
httpdb query 'kind=request path=/chat'        # 你说过的话
httpdb query 'header:X-Tool status=500'       # 失败的工具调用
httpdb query 'path~/tool/ body~timeout'       # 超时的工具调用
httpdb query 'status=200 header:X-Scope=design body~协议'

httpdb get 42                                 # 原始信封
httpdb ls 50                                  # 最近 50 条
```

## 用法

```
httpdb add [file]           从 stdin 或文件存一个信封:HTTP request/response,或带 front matter 的 markdown
httpdb wrap START [-H N:V]  给 stdin 的 body 套信封;START 是 status code (200) 或 request line (POST /chat)
httpdb get <id>             按 id 取原始信封
httpdb headers <id>         看某条的 headers
httpdb query '<expr>'       查询
httpdb tags [name]          header 名和值,带计数
httpdb ls [n]               最近 n 条(默认 20)
httpdb stats                数据库状态
httpdb serve [port]         HTTP 门,127.0.0.1,默认 200

--db PATH 或 HTTPDB_PATH 选文件,默认 ./httpdb.sqlite
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
anyword                 裸词,body 搜索
```

全文索引用 FTS5 trigram(SQLite 3.34+),中文子串直接搜;老 SQLite 自动退到 unicode61 + LIKE,`httpdb stats` 里能看到用的哪个。

## 安装

```bash
pip install httpdb
```

或者就一个文件,复制走:

```bash
cp httpdb.py ~/.local/bin/httpdb
chmod +x ~/.local/bin/httpdb
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
- **存 HTTP 消息,查 HTTP 消息** -- `GET /<id>` 回的是存的那条消息本身(`message/http`),存的消息永远是数据,不当 server 自己的回复回放。想看 AI 写的 HTML:`httpdb get 42 > x.html`,本地打开。
