<p align="center">
  <a href="README.md">English</a> |
  <a href="README.zh-CN.md">简体中文</a>
</p>

# Codex 的 DeepSeek 本地代理

这个项目提供一个基于 FastAPI 的本地代理，让当前版本的 Codex 命令行可以通过本地兼容端点使用 DeepSeek 的 `deepseek-v4-pro` 模型。

Codex 命令行 0.128.0 已经不再支持自定义供应商使用 `wire_api = "chat"`。本代理接收 Codex 发到 `/v1/responses` 的请求，将其转换为 DeepSeek 的 `/v1/chat/completions` 请求，再把返回内容转换回 Codex 需要的响应事件。

## 支持能力

- 为 Codex 命令行提供 `POST /v1/responses`
- 提供 `GET /v1/models` 和 `GET /v1/models/{model_id}` 供模型查询使用
- 保留 `POST /v1/chat/completions`，供兼容 OpenAI 格式的客户端直连使用
- 将响应接口中的 `tools` 转换为聊天补全接口中的 `tools`
- 将聊天补全接口中的 `tool_calls` 转换回响应接口中的 `function_call`
- 支持 Codex 后续轮次中的 `function_call_output`
- DeepSeek 上游报错时，保留原始状态码和错误内容返回给 Codex

## 环境要求

- Python 3.9 或更高版本
- 已安装 `fastapi`、`httpx` 和 `uvicorn`
- 已在进程、用户或系统环境变量中设置 `DEEPSEEK_API_KEY`

如需安装依赖，运行：

```powershell
python -m pip install fastapi httpx uvicorn
```

在 Windows 上永久设置 DeepSeek 密钥：

```powershell
[Environment]::SetEnvironmentVariable("DEEPSEEK_API_KEY", "sk-your-key", "User")
```

设置后请重新打开一个终端窗口。

## 启动代理

运行手动启动脚本：

```powershell
powershell -ExecutionPolicy Bypass -File T:\PythonItem\AI\start_deepseek_proxy.ps1
```

代理会启动在：

```text
http://127.0.0.1:3000
```

默认环境变量如下：

| 变量 | 默认值 |
| --- | --- |
| `DEEPSEEK_MODEL` | `deepseek-v4-pro` |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` |
| `DEEPSEEK_THINKING` | `disabled` |

`DEEPSEEK_THINKING=disabled` 是默认值，因为 Codex 在工具调用后的后续轮次不会重新发送 DeepSeek 聊天补全接口里的 `reasoning_content`。关闭思考模式可以让多轮工具执行保持兼容。

## 在 Codex 中使用

Codex 配置里已经新增了专用配置档 `deepseek_v4`。默认的 Codex 模型和供应商保持不变。

使用 DeepSeek 启动 Codex：

```powershell
codex -p deepseek_v4
```

运行一次快速检查：

```powershell
codex -p deepseek_v4 exec "只回答 2"
```

该配置档会把 Codex 指向：

```toml
[model_providers.deepseek_local]
base_url = "http://127.0.0.1:3000/v1"
wire_api = "responses"
requires_openai_auth = false
```

## Codex 文件示例

在只使用 DeepSeek 的配置下，`auth.json` 可以为空，因为 Codex 不需要向本地代理认证。代理会从环境变量读取 `DEEPSEEK_API_KEY`。

```json
{
  "OPENAI_API_KEY": "123"
}
```

只包含 DeepSeek 本地代理供应商的 `config.toml` 示例：

```toml
model_provider = "deepseek_local"
model = "deepseek-v4-pro"
model_reasoning_effort = "high"
network_access = "enabled"
disable_response_storage = true

[model_providers.deepseek_local]
name = "DeepSeek Local Responses Proxy"
base_url = "http://127.0.0.1:3000/v1"
wire_api = "responses"
requires_openai_auth = false

[projects.'t:\pythonitem\ai']
trust_level = "trusted"
```

## 常见问题

- 出现 `DEEPSEEK_API_KEY is not set`：请先设置密钥，然后重新打开终端。
- 出现 `Port 127.0.0.1:3000 is already in use`：请停止占用该端口的进程，或复用已经运行的代理。
- Codex 无法连接：请先启动代理，再运行 `codex -p deepseek_v4`。
- DeepSeek 返回错误：代理会把上游状态码和错误内容原样传给 Codex，便于排查。

## 本地快速检查

查询模型列表：

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:3000/v1/models
```

直接调用响应接口：

```powershell
$body = @{
  model = "deepseek-v4-pro"
  input = @(@{ role = "user"; content = @(@{ type = "input_text"; text = "只回答 OK" }) })
  stream = $false
  max_output_tokens = 16
} | ConvertTo-Json -Depth 8

Invoke-WebRequest -UseBasicParsing `
  -Method Post `
  -Uri http://127.0.0.1:3000/v1/responses `
  -ContentType "application/json" `
  -Body $body
```
