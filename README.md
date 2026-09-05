# commandcode-proxy (Python)

把 [CommandCode API](https://github.com/dev2k6/command-code-proxy-server) 包装成 OpenAI 兼容接口的本地代理。
Python 版实现，参考实现是 dev2k6 的 Go 项目（`v1.0.8`，约 1660 行 Go）。

任何已经配好 OpenAI 协议的客户端（Continue、Cline、Open WebUI、aider、litellm、自研脚本……）
都可以直接指向 `http://127.0.0.1:55990/v1` 使用 CommandCode 的模型，不需要改一行客户端代码。

**把代理接到某个具体 agent 里？** 看 [`AGENT_SETUP.md`](AGENT_SETUP.md)——里面有各客户端的
可直接粘贴的配置片段，以及三条会咬人的注意事项（推理模型会吃掉 `max_tokens` 预算、
reasoning 走非标准字段、只有视觉模型能读图）。

```text
OpenAI 客户端
   └─ POST /v1/chat/completions
        └─ 代理: 抽 system → 模型名映射 → 消息/工具转换 → 包信封
             └─ POST https://api.commandcode.ai/alpha/generate
                  └─ 上游 NDJSON 事件流 → OpenAI SSE / 聚合为单个 JSON
```

## 快速开始

Windows 上双击 `start.bat` 即可——首次运行会自动建 `.venv` 并装依赖，之后秒开。
参数会原样透传给服务：

```bat
start.bat
start.bat --port 8080
start.bat --debug
```

命令行手动跑：

```bash
pip install fastapi uvicorn httpx
python -m commandcode_proxy.main --api-key sk-xxxxxxxx
```

或者装成命令：

```bash
pip install -e ".[dev]"
commandcode-proxy --api-key sk-xxxxxxxx
```

也可以复制 `.env.example` 为 `.env`，不传任何参数直接跑。

用现有客户端验证：

```bash
curl http://127.0.0.1:55990/v1/models \
  -H "Authorization: Bearer sk-xxxxxxxx"

curl http://127.0.0.1:55990/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-xxxxxxxx" \
  -d '{"model":"deepseek-v4","messages":[{"role":"user","content":"Hello"}],"stream":false}'
```

## 配置

优先级：**命令行参数 > 环境变量 > `.env` > 内置默认值**。

| 命令行 | 环境变量 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `--host` | `CC_PROXY_HOST` | `127.0.0.1` | 绑定地址 |
| `--port` | `CC_PROXY_PORT` | `55990` | 监听端口 |
| `--api-key` | `CC_PROXY_API_KEY` | 空 | 默认密钥；请求头的 `Authorization` 优先级更高 |
| `--base-url` | `CC_PROXY_BASE_URL` | `https://api.commandcode.ai` | 上游地址，实际请求发到 `<BASE_URL>/alpha/generate` |
| `--models-file` | `CC_PROXY_MODELS_FILE` | `./models.json` | 模型对照表文件 |
| `--command-code-version` | `CC_PROXY_COMMAND_CODE_VERSION` | 空 | 固定 `x-command-code-version`；为空则查 npm 并缓存 30 分钟 |
| `--debug` | `CC_PROXY_DEBUG` | `false` | 打印出入站完整请求体（每条截断到 20000 字符） |
| `--version` | — | — | 打印版本号后退出 |

`.env` 里的 key 可以写成裸名（`PORT=55990`）也可以写成带前缀（`CC_PROXY_PORT=55990`），两者都认。

密钥解析分两种模式，由 `--api-key` / `CC_PROXY_API_KEY` 是否设置决定：

- **设置了默认 key（本地模式）**：代理始终用自己的 key 请求上游。客户端的
  `Authorization` 只当作本地校验，**不会**转发给上游。这样 agent 客户端被强制填入的占位符
  （`commandcode`、`test`……）不会导致上游 `401`。
- **没有设置默认 key（透传模式）**：代理自己不鉴权，客户端必须传真实的上游 key，原样转发。
  不传 → `401`。

注意：Go 版是"请求头优先，覆盖默认 key"，那意味着客户端填个占位符就会把代理打挂。这里
改成了本地模式下忽略客户端 key，是刻意偏离，参见「与 Go 参考实现的差异」。

## 模型对照表

**这是日常唯一需要改的文件：`models.json`。**

```json
{
  "aliases": {
    "deepseek-v4-pro": "deepseek/deepseek-v4-pro",
    "deepseek-v4": "deepseek/deepseek-v4-pro",
    "deepseek-pro": "deepseek/deepseek-v4-pro"
  },
  "models": [
    { "id": "deepseek/deepseek-v4-pro", "owned_by": "deepseek" }
  ]
}
```

规则：

- `aliases` 是**完整替换**，不是合并。文件里没写的旧别名会失效。
- 匹配大小写不敏感，前后空格会被去掉；未命中的名字**原样透传**给上游。所以上游新增模型时通常什么都不用改，只有想要短别名才需要加一行。
- `models` 供 `GET /v1/models` 使用。条目可以是字符串（`"deepseek/deepseek-v4-pro"`，`owned_by` 从 `/` 前的前缀推导）或对象（可显式指定 `owned_by`）。
- `models` 省略时，从 `aliases` 的目标值去重推导（只保留含 `/` 的）。
- 保存文件即生效，**不用重启**：每次请求会检查 mtime。
- 文件缺失或 JSON 损坏时回退到 `models.py` 里的 `seed_table()`，它从 `models.json` 自身
  加载一次作为兜底，并打一条 warning。没有第二份手写的目录要同步维护。

随附的 `models.json` 有 42 个模型 / 87 条别名，覆盖当前套餐页列出的全部模型。模型 ID 取自
`command-code` CLI 包 `dist/cli.mjs` 里的 **`canonicalId` 表**，不是那个扁平的展示名数组——
后者混着 provider 内部 slug。`tencent/hy3` 就是 slug，上游对每个请求回
`403 Model/provider not recognized`；真实 ID 是 `tencent/hy3-paid`（`tencent/hy3` 保留为兼容
别名，避免已保存的配置突然 403）。`gpt-5.6-luna` 本身是 canonical ID、没有 vendor 段，上游
内部才映射成 `openai/gpt-5.6-luna`。CLI 里另有免费档 `tencent/Hy3`，但上游明确说该档已停服，
所以没放进列表——把它别名到付费版会静默产生费用。
另一个容易猜错的：套餐页显示 `GLM-5.3 Flash`，真实 ID 是 `z-ai/glm-5.3-flash`
（命名空间是 `z-ai`，不是 `zai-org`）。

注意 Go 版 `bin/` 里那个 `v1.0.8` 的二进制是**过期构建**：它只带 12 个模型，而 `proxy.go`
源码里是 18 个。本实现跟着源码走，不要拿那个二进制当参照。

`models.py` 里的兜底表不再是手写副本，而是从 `models.json` 自举：文件损坏时用它自身加载到的
内容回退，不会和 `models.json` 分叉成两份要同步维护的目录。

## 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | `{"status":"ok"}` |
| `GET` | `/v1/models` | OpenAI 模型列表 |
| `POST` | `/v1/chat/completions` | OpenAI Chat Completions，流式与非流式 |
| `POST` | `/chat/completions` | 上一行的别名路由 |

流式响应为 `text/event-stream`，每个 chunk 是 `chat.completion.chunk`，结束时追加 `data: [DONE]`。
两个额外约定：

- reasoning 走 `choices[0].delta.reasoning`，非流式时进 `message.reasoning`。推理内容不会混进
  `content`，也不会走 OpenAI 的 `delta.reasoning_content`（上游用的是不同字段名）。
- `stream_options.include_usage` 会在流末尾追加一个 `choices: []` 且带 `usage` 的 chunk，
  顺序是 `…内容 → finish_reason → usage → [DONE]`。

## 控制台输出

启动横幅打印监听地址、上游地址、模型/别名数量、模型表路径、密钥模式、端点清单和全部模型 ID
（按控制台实际宽度自动决定列数，80 列窗口下降为单列，不会折行）。

每个 chat 请求打一行，流式与非流式都打：

```text
[chat] step3.5 -> stepfun/Step-3.5-Flash  HTTP 200  in=7574 out=43 total=7617
       reason=43 text=0  2.91s ttft=1.11s  events=46 finish=stop
```

字段含义：客户端提交的名字 → 映射后的上游模型 ID；`in/out/total` 是
prompt/completion/total token；`reason/text` 是 reasoning 与正文的拆分；
`ttft` 是上游首个事件的到达时间；`events` 是上游事件总数；`finish` 是结束原因。
流式时前缀是 `[chat stream]`，中途断流会显示 `HTTP stream-broken`。

流式请求里如果上游报错，同一行末尾会追加 `ERROR` 段，例如：

```text
[chat stream] poolside/laguna-s-2.1-free -> poolside/laguna-s-2.1-free  HTTP 200
       tokens=?  2.02s ttft=1.91s  events=2
       ERROR HTTP 503 server_error: Service temporarily unavailable. Please try again shortly.
```

出错时打印客户端模型名、映射结果和耗时，例如：

```text
[totally-bogus-model] model=totally-bogus-model upstream HTTP 403 after 0.30s: ...
```

所有日志行强制 ASCII（cmd.exe 按 GBK 渲染，任何非 ASCII 字符都会显示成乱码），
uvicorn 自己的访问日志已关闭以免与上面那行重复。`--debug` 额外打印出入站完整请求体和
上游每条事件行。

## 协议细节

上游请求信封（`params` 之外的字段是 `/alpha/generate` 的结构要求，不是客户端输入）：

```json
{
  "config": {
    "workingDir": ".",
    "date": "2026-09-05",
    "environment": "cli",
    "structure": [],
    "isGitRepo": false,
    "currentBranch": "",
    "mainBranch": "main",
    "gitStatus": "",
    "recentCommits": []
  },
  "memory": "", "taste": "", "skills": "",
  "params": {
    "model": "deepseek/deepseek-v4-pro",
    "messages": [...],
    "tools": [...],
    "system": "抽出来的 system 提示词",
    "max_tokens": 64000,
    "temperature": 0.3,
    "stream": true
  },
  "threadId": "uuid"
}
```

上游请求头：

```http
Content-Type: application/json
Authorization: Bearer <key>
x-command-code-version: <npm command-code 最新版本，30 分钟缓存，失败回退 "unknown">
x-cli-environment: production
Accept: text/event-stream
```

其它约定：

- 默认 `temperature = 0.3`，`max_tokens = 64000`；`max_completion_tokens` 覆盖 `max_tokens`。
- **`max_tokens` 上限 200000**：上游对 `params.max_tokens` 有硬校验，`200000` 通过、`200001`
  直接 400 `Too big`（在 3 个互不相关的模型上验证一致）。超限值被压到 200000 并打一条 warning。
  这个 clamp 不是可选项：客户端普遍按模型声明的输出上限来填这个字段，而 ZCode 自己的目录里
  `deepseek-v4-flash` 声明的是 `384000`，不压的话请求在到达模型之前就被拒。`install_to_zcode.py`
  写 provider 时也会把 output 上限一并压到 200000，别让它发出不可能成功的值。
- `config.date` 取本地时区的 `YYYY-MM-DD`。
- **上游永远按流式拉取**（`params.stream` 恒为 `true`）。非流式客户端的结果是把整条流读完再聚合，
  与 Go 版行为一致。
- 响应 `id` 形如 `chatcmpl-` + UUID 前 29 位，与 Go 版同格式。
- 上游返回非 200 时：4xx 原样透传状态码，5xx 与连接失败一律转 `502`。
- 错误体统一为 `{"error": {"message", "type", "param": null, "code": null}}`。

### 上游错误透传

上游会在事件流中间插入 `{"type":"error","error":{message, type, statusCode, isRetryable}}`
（例如 `503 Service temporarily unavailable`）。这个错误**原样透传给客户端**，不吞掉：

- 流式：作为一条 `data: {"error": {...}}` 的 SSE payload 发出，随后照旧 `data: [DONE]` 收尾。
  信封里带上游自己的 `message`、`type`，并把 `statusCode` 放进 `error.status`。之前这里被静默
  丢弃，客户端只收到一个 200 加空补全，agent 于是报"模型未返回内容"。
- 非流式：返回上游自己的状态码（如 `503`、`403`），不再一律压成 `502`；上游没给状态码时才回 `502`。

流式场景下响应头已经是 200，改不了状态行，所以只能靠这个 payload 把错误送到客户端手里——
这也是它必须带 `error.status` 的原因。

代理不支持自动重试：`isRetryable` 只是上游给客户端的建议，要不要重试、重试几次由客户端决定。

消息转换有几处刻意保留的怪癖（它们是上游契约，不是风格问题）：

- `tool` 结果文本以 `Error:` 开头时，`output.type` 用 `error-text`；
- 工具 schema 的 `parameters` 改名为 `input_schema`，缺失时补 `{"type":"object","properties":{}}`。

有两处**故意不**照抄 Go 版：

- 图片 content part 转成真正的上游 image part
  `{"type":"image","image":"<url>","mimeType":"image/png"}`，而不是 Go 版压成的文本
  `[Image URL: <url>]`。Go 版那样做等于让视觉模型只看到一个 URL 字符串、看不到像素。
  实测 `deepseek/deepseek-v4-flash-vision-exp` 能读出图片内容，本实现可正常工作。
- `thinking` / `reasoning` content part 保留为 `{"type":"reasoning","text":...}`，
  而不是塌平成 `text`。这是上游读回 thinking 的方式，也是把 reasoning 续进去的唯一途径。

推理模型（几乎所有套餐内模型都算）会把 `max_tokens` 先花在 reasoning 上。实测
`max_tokens: 120` 可能被 reasoning 整块吃掉，`finish_reason` 为 `length` 且 `content` 为空；
这种场景需要把 `max_tokens` 调大。另有一个上游自己的算术怪癖：reasoning 超预算时
`completion_tokens_details.text_tokens` 会是负数（如 `completion_tokens: 120,
reasoning_tokens: 130, text_tokens: -10`），Go 版同样原样透传。

## 测试

```bash
python -m pytest -q        # 236 passed
```

覆盖模型映射（含 Go 版 `model_test.go` 的全部用例）、消息与工具转换、流式/聚合事件转换、
信封构建、版本头缓存 TTL、以及经 `httpx.MockTransport` 驱动的真实 FastAPI 端点（请求头、
请求体、状态码映射、鉴权、错误体）。测试不访问任何真实服务。

## 与 Go 参考实现的差异

刻意为之的改动：

| 项 | Go 版 | 本实现 | 原因 |
| --- | --- | --- | --- |
| 上游流内错误 | 只记日志后继续，客户端拿到空补全 | 流式发 `data:{"error":...}`，非流式透传上游状态码 | 空补全在 agent 里表现为"模型未返回内容"，比上游自己的报错信息更没用的那种 |
| 密钥优先级 | 请求头覆盖默认 key | 本地模式下默认 key 始终生效，请求头只校验 | Go 版会把客户端填的占位符 key 转发上游导致 401；agent 客户端强制填 key，这等于默认配置不可用 |
| 图片输入 | 压成文本 `[Image URL: ...]` | 真正的 upstream image part | Go 版让视觉模型只看到 URL 字符串，看不到像素；本实现实测视觉模型能读出图内内容 |
| reasoning 回传 | `thinking` 塌平成 `text` | 保留为 `{type:"reasoning"}` part | 这是上游读回 thinking 的方式，也才能让多轮 reasoning 续接 |
| reasoning 输出 | 不区分，全进 `content` | 单独进 `delta.reasoning` / `message.reasoning` | 上游 `reasoning-delta` 与 `text-delta` 是分开的两个事件 |
| 模型目录 | 12（`bin` 二进制）/ 18（源码） | 42 模型 / 87 别名 | 从 CLI 的 `canonicalId` 表取，不是扁平展示数组——后者混着 provider slug，`tencent/hy3` 就是错的 |
| `max_tokens` 上限 | 无校验 | 压到上游硬上限 200000 | 客户端按模型声明的输出上限填这个字段；ZCode 目录里 `deepseek-v4-flash` 是 384000，上游硬拒 >200000，不压等于这类请求必 400 |
| `/v1/responses` | 有 | **无**（404） | 按需求只实现核心三件套；要补回见下 |
| GitHub tag 版本自检 | 有 | 无 | 指向 Go 仓库，移植后无意义 |
| `404` 响应体 | `text/plain` 的 `404 page not found` | OpenAI 错误信封 | OpenAI 客户端期望 JSON |
| `tool_use` 里的 `arguments` | 原样透传字符串 | 解析成对象 | Go 版在这里漏了 `parseToolInput`，字符串 `input` 很可能不是上游期望的形态 |
| npm 版本预热 | `init()` 里抓取后丢弃结果（缓存其实是空的） | 启动时真正写入缓存 | 原实现是无效代码 |

已删除的 Go 死代码：非流式聚合里 `toolInputBuffers` 只写不读。

已知无害差异：

- `Invalid JSON` 的错误文本随解析器不同（Go 的 `encoding/json` vs Python 的 `json`），错误信封结构一致。
- Go 的 `json.Encoder.Encode` 会在响应末尾多一个换行，本实现没有。
- 上游流里 `tool-delta` 出现在任何 `tool-use` 之前时，Go 会发出 `index: -1`；本实现沿用同样逻辑。

补回 `/v1/responses` 的话，Go 版的做法是把 Responses 协议的 `input` / `instructions` 折算成
Chat Completions 请求，然后复用同一个 handler（见参考实现 `proxy.go` 的
`HandleResponses` / `responsesToChatRequest` / `responsesInputToMessages`）。

## 项目结构

```text
.
├── start.bat           # Windows 一键启动: 建 venv、装依赖、起服务
├── AGENT_SETUP.md      # 把代理接入具体 AI agent 的配置片段与注意事项
├── models.json         # 模型对照表 (42 模型 / 87 别名)，热重载
├── .env.example        # 配置模板
├── pyproject.toml
├── commandcode_proxy/
│   ├── api.py         # FastAPI 应用工厂、路由、错误信封、密钥解析
│   ├── config.py      # CLI / 环境变量 / .env
│   ├── convert.py     # OpenAI → CommandCode 消息与工具转换
│   ├── main.py        # 命令行入口
│   ├── models.py      # 模型对照表: 文件加载、mtime 热重载、兜底默认值
│   ├── responses.py   # 上游 NDJSON → SSE 流 / 聚合为单个 JSON
│   └── upstream.py    # 信封构建、请求头、finish_reason、npm 版本缓存
└── tests/              # 236 个用例，全部走 httpx.MockTransport，不联网
```

## 已知边界

上游协议（`/alpha/generate` 的信封、请求头、事件类型）是按 Go 版 v1.0.8 原样照搬的。
如果上游已经变了，报错时会体现在 `--debug` 的日志里；`--base-url` 和
`--command-code-version` 可以先顶住最外层的两处。

## 许可证

MIT，见 [LICENSE](LICENSE)。参考实现是 [dev2k6/command-code-proxy-server](https://github.com/dev2k6/command-code-proxy-server)（Go 版），
本项目是独立的 Python 重写，不是其 fork。
