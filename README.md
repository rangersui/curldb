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
httpdb add [file]           从 stdin 或文件存一个 raw HTTP 信封
httpdb wrap START [-H N:V]  给 stdin 的 body 套信封;START 是 status code (200) 或 request line (POST /chat)
httpdb get <id>             按 id 取原始信封
httpdb headers <id>         看某条的 headers
httpdb query '<expr>'       查询
httpdb tags [name]          header 名和值,带计数
httpdb ls [n]               最近 n 条(默认 20)
httpdb stats                数据库状态

--db PATH 或 HTTPDB_PATH 选文件,默认 ./httpdb.sqlite
```

### query DSL

所有条件 AND 连接,空格分隔:

```
kind=request|response
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
cp httpdb.py ~/.local/bin/httpdb
chmod +x ~/.local/bin/httpdb
```

零依赖。Python 3.10+,标准库 sqlite3。0.1.x 的库文件第一次打开时自动升级。

## 设计选择

- **原文存储** -- raw 信封原样进 SQLite TEXT 字段。
- **索引可重建** -- kind / status / method / path / headers / body_fts 都从 raw 解析出来。
- **宽松解析** -- 接受 `\n` 和 `\r\n`,忽略畸形 header,Content-Length 可选。这是文档解析器。
- **一个 session 一个文件** -- 备份是 `cp`,同步是 rsync。
- **一个 CLI** -- 进出,用完退出。
- **只追加** -- 没有 update,没有 delete。
