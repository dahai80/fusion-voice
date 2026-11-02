# fusion-voice

Fusion 生态的语音智能体平台接入层：用一套同步 SPI 与统一事件模型屏蔽托管语音平台差异。

**状态：M2/M3 已实现（2026-11-02）。** 热路径（webhook 验签、事件归一化、assistant-request 与 tool-call 同步响应）、冷路径（助理部署、呼叫转移、挂断、呼叫查询）、五个平台 adapter、Prometheus 兼容指标均已落地。

## 安装

```bash
cd /Users/dahai/fusion && source .venv/bin/activate
pip install -e "fusion-voice[test,api]"
```

核心库不依赖 Web 框架；`[api]` 增加 FastAPI，`[test]` 增加 pytest。

## 快速开始

```python
from fastapi import FastAPI
from fusion_voice import AssistantResponse, ToolResult, VoiceRegistry
from fusion_voice.fastapi import build_voice_router

registry = VoiceRegistry.from_config("voice.yaml")


@registry.on_assistant_request
async def on_assistant_request(ctx):
    return AssistantResponse(assistant_id="asst_123")


@registry.on_tool_call("book_slot")
async def book_slot(ctx, call):
    return ToolResult(tool_call_id=call.tool_call_id, name=call.name, result={"slot": call.args["slot"]})


@registry.on_event
def on_event(provider_id, event):
    # 呼叫开始/结束、状态更新、转移/挂断请求、未知事件
    ...


app = FastAPI()
app.include_router(build_voice_router(registry))
```

`voice.yaml` 注册 provider、凭证引用、每个 provider 的时间预算与 adapter `options`（例如 Twilio 的 `webhook_url`、自定义转移路径），示例见 [examples/voice.yaml](examples/voice.yaml)。凭证支持字面量、`env:VAR`、CredentialProvider 引用（`<provider_id>:<key>`）；库本身不持久化任何凭证。

请求处理约定：

| 条件 | 状态码 |
|------|--------|
| 未知或已禁用 provider | 404 |
| 请求体超过 `max_body_bytes`（默认 1 MiB） | 413 |
| 验签失败 | 401 |
| 载荷格式错误 | 400 |
| 助理处理超时 / 工具超时 | 助理 504；工具返回该工具的 error 结果 |
| 处理器崩溃 | 500，响应体脱敏 |

assistant-request 与 tool-call 的响应走同一条 HTTP 连接。多个 tool call 并发执行，每个受 provider `tool_call_s` 预算约束；超时或崩溃只影响对应工具的错误结果，不会拖垮整批。

## 支持的平台

| Provider | 热路径 | 冷路径 |
|----------|--------|--------|
| Vapi（P0） | assistant 请求、工具、生命周期 | 部署、转移、挂断、查询 |
| Retell（P1） | assistant 请求、工具、生命周期 | 部署（LLM + agent）、转移、挂断、查询 |
| Bland（P1） | assistant 请求、工具、生命周期 | 部署、转移、挂断、查询 |
| Twilio（P2） | 状态回调 | 号码配置、转移、挂断、查询 |
| ElevenLabs（P2） | server 工具、通话结束事件 | agent 部署、查询 |

Twilio ConversationRelay 的媒体与工具流量走 WebSocket，不属于同步 HTTP 连接器（PRD V4）；Twilio adapter 覆盖签名状态回调与 REST 呼叫控制。ElevenLabs 无公开 REST 实时转移/挂断接口，这两个方法显式抛 `NotImplementedError`。LiveKit/Pipecat 仅观察。

Retell、Bland、Twilio、ElevenLabs 的字段名依据公开文档实现，沙箱验收见 PRD V1。adapter 相互隔离，字段名修正只需改单个文件。

## 指标

`VoiceMetrics` 按 provider 记录请求数、结果、耗时与验签结果。挂载可选的 Prometheus 文本端点：

```python
from fusion_voice.fastapi import build_metrics_router

app.include_router(build_metrics_router(registry))   # GET /voice/metrics
```

暴露 `fusion_voice_requests_total`、`fusion_voice_request_duration_ms_sum`、`fusion_voice_verification_total`。`registry.metrics.snapshot()` 返回同源字典，便于对接其他观测后端。多个 app 需要汇总时，把共享的 `VoiceMetrics()` 传给 `VoiceRegistry.from_config(..., metrics=...)`。

## 非目标

不含音频运行时（STT/TTS/VAD/barge-in）、不做 LLM 编排、不做号码购买/计费/运营台、不发短信（属 `fusion-channel`）、不做归档优先/outbox 持久化。语音流量直接发生在托管平台与消费者后端之间；库无常驻服务、无端口。

## 文档

- [技术架构](docs/tech-architecture.md) — 模块划分、热路径分发与超时控制、状态码矩阵、配置 schema、测试策略。
- [架构方案](../../architecture/fusion-voice-archi-plan-1102.md) — M0–M3 里程碑与生态边界。
- [PRD v0.1](../../architecture/fusion-voice-prd.md) — 需求、安全红线、性能预算。

## 许可

Apache License 2.0，见 [LICENSE](LICENSE)。
