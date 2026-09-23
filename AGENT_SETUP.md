# 把本代理接入 AI Agent

本代理在本地提供一个 **OpenAI 兼容** 端点。任何支持"自定义 OpenAI 兼容端点"的客户端
（Continue、Cline、Open WebUI、aider、litellm、自研脚本……）都能直接用，不需要改客户端代码。

## 三个值：填到任何客户端里都一样

| 项 | 值 |
| --- | --- |
| **Base URL** | `http://127.0.0.1:55990/v1` |
| **API Key** | 任意非空字符串，例如 `commandcode` |
| **Model** | 短别名如 `step3.5`，或完整 ID 如 `stepfun/Step-3.5-Flash` |

> API Key 填什么都行——**前提是 `.env` 里设置了 `API_KEY`**（也就是"本地模式"，见下文）。
> 那种情况下代理用自己的 key 请求上游，客户端填的值只是过一道非空校验，不会转发出去。
> 真正要维护的只有 `models.json` 和 `.env`。

启动：双击 `start.bat`（首次运行会自动建 `.venv` 并装依赖）。

## 两种密钥模式（`.env` 里改一行切换）

| 模式 | `.env` | 客户端要填什么 | 适用场景 |
| --- | --- | --- | --- |
| **本地模式**（默认） | `API_KEY=your_key` | 任意非空字符串，如 `commandcode` | 一个人用，agent 随便配 |
| **透传模式** | `API_KEY=`（留空） | **真实的上游 key**，代理原样转发 | 一台代理服务多人，各用各的额度 |

本地模式下代理始终用自己的 key 鉴权，客户端填 `commandcode`、`test`、`sk-fake`
都无所谓。透传模式下代理自己不鉴权，客户端不传 key 会直接 `401`。

这个区分是必要的：agent 客户端通常强制你填一个 key。如果代理把那个占位符原样转发给上游，
每个请求都会收到 401。本地模式就是为了让"随便填一个值"真正可用。

## 先验证代理活着

```bash
curl http://127.0.0.1:55990/v1/models

curl http://127.0.0.1:55990/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer commandcode" \
  -d '{"model":"step3.5","max_tokens":2000,"messages":[{"role":"user","content":"Say hi"}]}'
```

> `max_tokens` 给到 2000 以上。原因见下面"接入前必读"第 1 条。

## 通用做法：环境变量

大多数工具和 SDK 认 `OPENAI_BASE_URL`，配一次到处生效：

```bash
export OPENAI_BASE_URL="http://127.0.0.1:55990/v1"
export OPENAI_API_KEY="commandcode"
```

## ZCode

ZCode 的 provider 表在 `~/.zcode/v2/config.json`，本项目带了一个安装脚本：

```bash
python install_to_zcode.py            # 写入配置（先自动备份）
python install_to_zcode.py --dry-run  # 只看会写什么
python install_to_zcode.py --base-url http://127.0.0.1:8080/v1   # 改端口
```

注册一个 `CommandCode (local proxy)` 自定义 provider（`kind: openai-compatible`），
带上全部 44 个模型。流程：**先跑 `start.bat` 起代理 → 再重启 ZCode → 模型选择器里选
`commandcode/<model-id>`**，例如 `commandcode/deepseek/deepseek-v4-flash`。

provider 的 key 是 `commandcode`，而模型 ID 本身带 `/`，所以完整写法是两段 `/`。

`models.json` 改了之后重跑一次即可，脚本幂等（重复运行是替换而不是重复追加）。
每次运行会先备份成 `config.json.bak-<时间戳>`，7 个原有 provider 已核对为逐字节未变。

限额不是装饰：脚本先按 ID 后缀去 ZCode 自带目录
`resources/model-providers/*.json` 里匹配真实的 context / output（13 个模型命中，例如
`deepseek-v4-pro` 拿到 1000000 / 384000），其余用 262144 / 16384 的保守默认。
这个值决定 ZCode 什么时候压缩对话——填小了过早压缩，填大了撑爆窗口。

**刻意没写 `reasoning` 配置块。** ZCode 的 `reasoning.variants` 会往请求里塞
`reasoningEffort`，但上游 `/alpha/generate` 的参数只有
`model/messages/tools/system/max_tokens/temperature/stream`，没有推理强度这一项——
CLI 是靠 system 提示词诱导模型思考的。所以那个开关发了也只是被我静默忽略，等于一个做不到的
承诺。模型该思考还是照样思考，reasoning 照常出现在响应的 `reasoning` 字段里。想让 ZCode 侧的
推理开关真正生效，得在代理里把 `reasoning_effort` 翻译成提示词，那会改变请求语义，没擅自做。

## Python openai SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:55990/v1", api_key="commandcode")

resp = client.chat.completions.create(
    model="step3.5",
    max_tokens=2000,
    messages=[{"role": "user", "content": "Write a haiku about proxies"}],
)
print(resp.choices[0].message.content)
```

## Continue（`config.json`）

```json
{
  "models": [
    {
      "title": "CommandCode / Step 3.5",
      "provider": "OpenAI",
      "type": "openai",
      "model": "step3.5",
      "apiBase": "http://127.0.0.1:55990/v1",
      "apiKey": "commandcode"
    }
  ]
}
```

如果你的 Continue 版本不接受 `"type": "openai"`，改用 `"type": "chat"`。

## Cline

设置面板里：

- **API Provider** → `OpenAI Compatible`
- **Base URL** → `http://127.0.0.1:55990/v1`
- **Model ID** → `step3.5`
- **API Key** → `commandcode`

## Open WebUI

`Admin Panel → Settings → Connections`：

- **OpenAI API Base URL** → `http://127.0.0.1:55990/v1`
- **API Keys** → 加一条 `commandcode`

## litellm / aider

```python
from litellm import completion

r = completion(
    model="custom_openai/step3.5",          # custom_openai/ 前缀走自定义端点
    messages=[{"role": "user", "content": "hi"}],
    api_base="http://127.0.0.1:55990/v1",
    api_key="commandcode",
    max_tokens=2000,
)
```

aider：

```bash
aider --model custom_openai/step3.5 --openai-api-base http://127.0.0.1:55990/v1 --max-tokens 2000
```

---

## 接入前必读（三条，都会咬人）

**1. 大部分模型是推理模型，`max_tokens` 会被 reasoning 先吃光。**

套餐里几乎所有模型都会先输出 reasoning 再输出正文。实测 `max_tokens: 120` 时，120 个
token 被 reasoning 整块占掉，结果是 `finish_reason: "length"`、`content` 为空字符串。
agent 类工具通常默认设一个较小的 `max_tokens`，就会稳定拿到空回复。

**建议 `max_tokens` 至少给到 2000~4000**，或在支持的地方干脆不设这个字段
（不设时本代理用默认 `64000`）。

**2. reasoning 走的是非标准字段 `reasoning`，不是 OpenAI 的 `reasoning_content`。**

非流式在 `message.reasoning`，流式在 `delta.reasoning`。大多数 agent 不认识这个字段，
会静默忽略——**这是安全的**，`content` 里的正文照常可用，只是看不到思考过程。
`usage.completion_tokens` 是包含 reasoning 的总量。

**3. 视觉是真实可用的，但只能用视觉模型。**

图片 part 会转成上游的 image part 而不是 URL 文本，视觉模型能真正读到像素内容。
只有 `deepseek/deepseek-v4-flash-vision-exp`（别名 `deepseek-v4-flash-vision` / `ds-vision`）
带视觉能力；用其它模型发图，模型会回一句"我没看到图片"。

## 已验证可用的能力

- 流式（`stream: true`）与非流式
- 工具调用完整往返（`tool_calls` → 回传 `role: "tool"` → 模型汇总）
- 视觉输入（`image_url`，含 `data:` 内联 base64）
- `stream_options.include_usage`（末尾追加 `choices: []` + `usage` 的 chunk）
- `max_tokens`、`max_completion_tokens`、`temperature`

## 挑模型

44 个模型 / 92 条别名，完整清单在 `models.json`，或：

```bash
curl http://127.0.0.1:55990/v1/models
```

日常可用的一批短别名：`step3.5`、`deepseek-v4`、`deepseek-v4-flash`、`qwen3.8-max`、
`glm-5.3`、`glm-5.3-flash`、`kimi-k3`、`kimi-k2.7-code`、`mimo`、`minimax`、
`ds-vision`（视觉）、`inkling-small`（快）。

未写在 `models.json` 里的名字会**原样透传**给上游，所以上游新增模型时通常什么都不用改。

## 排查

`start.bat --debug` 会打印出入站完整请求体和上游每条流事件（各截断 20000 字符）。
上游返回非 200 时：4xx 原样透传状态码，5xx 与连接失败一律转 `502`，错误体形如：

```json
{"error": {"message": "Upstream error: ...", "type": "api_error", "param": null, "code": null}}
```
