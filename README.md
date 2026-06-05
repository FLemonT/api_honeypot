# 🔍 API Honeypot — 多协议 AI 请求拦截与分析工具

一个本地 OpenAI 兼容 API 蜜罐工具，用于拦截、记录并转发 AI 请求，支持 **OpenAI** 与 **Anthropic (Claude)** 双协议，帮助分析 Prompt 行为。

## ✨ 功能特性

- **双协议支持**：同时兼容 OpenAI Chat Completions API (`/v1/chat/completions`) 和 Anthropic Messages API (`/v1/messages`)
- **透明转发**：请求被记录后原样转发至上游服务商，客户端零感知
- **流式支持**：正确处理 `stream=True` 的 SSE 流式响应
- **请求日志**：每个请求以 JSON 格式保存至 `prompt_get/` 目录，按协议分类命名
- **认证透传**：支持 `x-api-key` 和 `Authorization: Bearer` 两种认证头
- **查询参数保留**：上游转发时保留原始查询参数（如 `?beta=true`）
- **错误格式匹配**：根据协议返回对应格式的错误响应（OpenAI 风格 / Anthropic 风格）
- **向后兼容**：配置文件支持新旧两种格式

## ⚙️ 配置

编辑 `config.json`：

```json
{
  "port": 8000,
  "fake_api_key": "sk-fake-honeypot-key-12345",
  "openai": {
    "real_api_url": "https://api.deepseek.com",
    "real_api_key": "你的真实API密钥",
    "target_model": "deepseek-v4-flash"
  },
  "anthropic": {
    "real_api_url": "https://api.anthropic.com",
    "real_api_key": "你的真实API密钥",
    "target_model": "claude-sonnet-4-6"
  }
}
```

| 字段 | 说明 |
|------|------|
| `port` | 蜜罐服务器监听端口（默认 8000） |
| `fake_api_key` | 蜜罐接受的虚拟 API Key，客户端使用此密钥连接 |
| `openai.real_api_url` | OpenAI 兼容上游 API 地址（支持 DeepSeek 等） |
| `openai.real_api_key` | 上游真正的 API Key |
| `openai.target_model` | 强制覆盖请求中的 model 字段（留空则透传） |
| `anthropic.real_api_url` | Anthropic 兼容上游 API 地址 |
| `anthropic.real_api_key` | 上游真正的 API Key |
| `anthropic.target_model` | 强制覆盖请求中的 model 字段（留空则透传） |

> **兼容旧格式**：如果配置文件中没有 `openai`/`anthropic` 嵌套块，程序会自动将顶层字段视为 OpenAI 配置。

## 🚀 安装与运行

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置

编辑 `config.json`，填入上游 API 的真实地址和密钥。

### 3. 启动服务

```bash
python honeypot.py
```

服务启动后监听 `http://127.0.0.1:8000`。

## 🔌 客户端使用

### OpenAI SDK（Python）

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="sk-fake-honeypot-key-12345",
)

response = client.chat.completions.create(
    model="gpt-4",
    messages=[{"role": "user", "content": "你好"}],
)
print(response.choices[0].message.content)
```

### Anthropic SDK（Python）

```python
import anthropic

client = anthropic.Anthropic(
    base_url="http://127.0.0.1:8000/v1",
    api_key="sk-fake-honeypot-key-12345",
)

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=1024,
    messages=[{"role": "user", "content": "你好"}],
)
print(response.content[0].text)
```

### Claude Code CLI

在 Claude Code 的配置中将 API endpoint 指向蜜罐：

```bash
# 设置环境变量
ANTHROPIC_BASE_URL=http://127.0.0.1:8000/v1
ANTHROPIC_API_KEY=sk-fake-honeypot-key-12345
```

> 蜜罐会自动识别 Claude Code 发送的 `Authorization: Bearer` 认证头并正确路由。

### curl 测试

**OpenAI 格式：**
```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-fake-honeypot-key-12345" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

**Anthropic 格式：**
```bash
curl http://127.0.0.1:8000/v1/messages \
  -H "x-api-key: sk-fake-honeypot-key-12345" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-6",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

## 📁 目录结构

```
fake_api/
├── honeypot.py          # 主程序
├── config.json          # 配置文件
├── requirements.txt     # Python 依赖
├── README.md            # 本文件
└── prompt_get/          # 请求日志目录（自动创建）
    ├── openai_prompt_20260605_120000_a1b2c3d4.json
    └── anthropic_prompt_20260605_120001_e5f6g7h8.json
```

## 📋 日志格式

每个请求保存为一个独立的 JSON 文件，命名规则：`{协议}_prompt_{时间戳}_{随机ID}.json`

日志内容为完整的请求体（包括 system prompt、用户消息、参数等），便于后续分析。

## 🛡️ 错误处理

- **上游不可达**：返回对应协议格式的错误，不会崩溃
- **上游返回错误**：原样透传上游的 HTTP 状态码和错误体
- **非 JSON 响应**：自动生成协议匹配的 fallback 错误体

## ⚠️ 安全提示

- 本工具仅在 `127.0.0.1` 监听，不会暴露到公网
- `config.json` 包含真实 API 密钥，请勿提交至版本控制
- 建议将 `config.json` 和 `prompt_get/` 加入 `.gitignore`
