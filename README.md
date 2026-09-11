# httpdb

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

## 设计选择

- **原文存储** -- raw 信封原样进 SQLite TEXT 字段。
- **索引可重建** -- kind / status / method / path / headers / body_fts 都从 raw 解析出来。
- **宽松解析** -- 接受 `\n` 和 `\r\n`,忽略畸形 header,Content-Length 可选。这是文档解析器。
- **一个 session 一个文件** -- 备份是 `cp`,同步是 rsync。
- **一个 CLI** -- 进出,用完退出。`serve` 是同一个档案柜的第二扇门,想开就开。
- **只追加** -- 没有 update,没有 delete。
- **存 HTTP 消息,查 HTTP 消息** -- `GET /<id>` 回的是存的那条消息本身(`message/http`),存的消息永远是数据,不当 server 自己的回复回放。想看 AI 写的 HTML:`httpdb get 42 > x.html`,本地打开。
