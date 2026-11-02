# fusion-voice 技术架构方案

> 版本 v0.1（草案）· 日期 2026-11-02 · 状态：与 PRD v0.1 配套的实施方案
> 设计来源：`/Users/dahai/fusion/architecture/fusion-voice-prd.md`（what/why）；`business/ai-booking-agent/docs/tech-architecture.md` §6.1（首个消费者的热路径约束）；孵化代码 `business/ai-booking-agent/backend/booking_agent/voice/`（SPI 与 vapi adapter 的既成事实）
> 本文只回答 how：模块划分、数据流、配置、错误模型、测试与抽取路径。PRD 的目标/非目标/安全红线不再复述，仅引用。

## 1. 方案定位与约束

### 1.1 阶段状态

- **当前处于 PRD M0（孵化）**：SPI 与 vapi adapter 的代码事实在 `business/ai-booking-agent/backend/booking_agent/voice/`（`spi.py` + `vapi_adapter.py`），本仓库尚未落地代码。
- **本文定义 M1（抽库）的目标形态**：抽库触发条件为「第二个真实消费者」或「第二个 adapter 落地」（PRD §6/§10）。触发前，行为变更发生在消费者仓库；触发后，按本文 §13 迁移，验收标准是消费者集成测试全绿（PRD §9）。
- 本文与 PRD 的维护联动：SPI 签名、事件模型、适配器路线变化时，PRD 与本文同一次改动更新。

### 1.2 实现级硬约束（PRD 的落地表述）

| 约束 | 落地含义 |
|------|---------|
| 同步语义 | assistant-request 硬限 7.5s（平台），消费者目标 P99 <500ms；tool-call 默认 5s/工具。热路径禁止持久化与队列 |
| 零音频 | 库内不出现音频字节、不包含 STT/TTS/VAD 逻辑 |
| 热路径零阻塞 I/O | `verify_request` / `parse_event` / `format_*` 内不得有网络调用、磁盘写、同步日志写 |
| 验证强制 | 未实现 `verify_request` 的 adapter 注册即 `ConfigError`（与 fusion-channel `_assert_verify_implemented` 同机制） |
| Fail-closed | 输入校验/解析异常一律 4xx，攻击者输入不触发 5xx |
| 库形态 | 进程内库，无常驻服务，无端口（与 fusion-channel 一致） |
| LLM 边界 | 不做 LLM 编排；平台托管 LLM 属 fusion-gateway 规则的已批准豁免 |

### 1.3 方案级假设与决策

以下为本方案做出的选择（PRD 未逐条指定），抽取时以此为准：

1. **Python ≥3.12、setuptools src 布局**，与 fusion-core / fusion-channel 同分发模式（`pip install -e fusion-voice`）。
2. **热路径模型用 stdlib `dataclass(slots=True)`，不用 pydantic**。依据：孵化代码已是如此；热路径每事件都在 <50ms 预算内，pydantic 校验开销无收益。冷路径（AssistantSpec 部署）可容忍 pydantic，但为一致性同样用 dataclass + 显式校验。
3. **配置用 PyYAML**，加载后转为 dataclass 并立即校验（fail-fast，同 fusion-channel `load_config`）。
4. **FastAPI 为可选 extra**（`.[api]`）；库核心（SPI/registry/dispatch）不依赖 Web 框架，`build_voice_router` 是唯一触点。
5. **凭证来源由消费者注入**：库只接受 `CredentialProvider` 或显式 secret 字符串；从 fusion-identity 凭据平面拉取是消费者启动期的工作（ai-booking-agent §3.5 模式），不进库。
6. **vapi adapter 在 M1 逐字迁移**（`move, not rewrite`），行为等价由消费者回归测试担保。

## 2. 架构总览

```
托管语音平台（Vapi / Retell / Bland / ElevenLabs / Twilio …）
        │ HTTPS webhook（raw body + headers）        ▲ REST（deploy / transfer / hangup / fetch）
        ▼                                           │
┌─────────────────── fusion_voice（进程内库）────────────────────────────┐
│                                                                        │
│  build_voice_router(registry) → POST /voice/{provider_id}              │
│         │                                                              │
│         ▼                                                              │
│  VoiceRegistry.dispatch(provider_id, headers, body)                    │
│    1. adapter.verify_request(headers, body)      ← fail-closed 401     │
│    2. adapter.parse_event(body) → VoiceEvent     ← 归一化（零 I/O）     │
│    3. 预算注入 VoiceSessionContext(deadline)                             │
│    4. 分发：on_tool_call / on_assistant_request  handler（消费者业务）   │
│    5. adapter.format_tool_results / format_assistant_response          │
│                                                                        │
│  hooks: 结构化日志 · raw payload sink · 指标                            │
│  credentials: CredentialProvider（库内不落盘）                          │
└───────────────┬────────────────────────────────────────────────────────┘
                │ 窄接口：VoiceEvent / ToolResult / AssistantResponse
                ▼
消费者业务后端（状态机、幂等、报价、下单；零 provider 字段）
旁路：fusion-channel（出站短信，异步 outbox）· fusion-identity（凭据平面）
      · fusion-mlx（语音平台托管 LLM 为 gateway 规则已批准豁免）
```

要点：

- 消费者只面向统一模型与 handler 装饰器；平台差异 100% 收敛在 adapter 内（PRD G1/G2，消费者技术架构 §6.1 Provider 中立化约束）。
- 入站（热）与出站（冷）路径在 adapter 上分方法，互不调用；冷路径失败不影响在途通话。
- fusion-channel 是平级姊妹库而非依赖方向：仅复用其通用原语（§14），不共享其异步消息语义。

## 3. 模块设计

```
fusion-voice/
├── pyproject.toml              # setuptools, src 布局, extras: [api], [test]
├── README.md / README_CN.md    # English / 中文
├── docs/tech-architecture.md   # 本文
├── examples/voice.yaml         # 示例配置（data-driven 注册）
├── src/fusion_voice/
│   ├── __init__.py             # 公共 API 导出（VoiceRegistry/build_voice_router/VoiceEvent…）
│   ├── spi.py                  # VoiceProvider Protocol + VerificationError
│   ├── models.py               # 热路径 + 冷路径数据模型
│   ├── registry.py             # VoiceRegistry 门面：from_config / handler 注册 / dispatch
│   ├── config.py               # voice.yaml → dataclass；ConfigError
│   ├── dispatch.py             # 热路径编排与预算控制（纯函数，便于单测）
│   ├── fastapi.py              # build_voice_router(registry) → POST /voice/{provider_id}
│   ├── hooks.py                # VoiceHooks：on_event / on_error / on_raw_payload
│   ├── credentials.py          # 薄封装，复用 fusion-channel CredentialProvider（§14）
│   ├── budgets.py              # VoiceSessionContext deadline 计算与剩余预算
│   └── adapters/
│       ├── __init__.py         # @register 装饰器、ADAPTERS 表、注册期校验
│       ├── _base.py            # HttpAdapterMixin：注入 httpx client（测试用 MockTransport）
│       ├── vapi.py             # P0；M1 逐字迁移孵化代码
│       ├── retell.py           # P1
│       └── bland.py            # P1
└── tests/
    ├── fixtures/vapi/*.json    # 平台事件黄金样本
    ├── test_dispatch_budget.py
    ├── test_verify_fuzz.py
    ├── test_config.py
    └── bench/test_hotpath.py
```

| 模块 | 职责 | 关键约束 |
|------|------|---------|
| `spi.py` | `VoiceProvider` Protocol（4 入站 + 4 出站方法，M1 补齐冷路径）、`VerificationError` | Protocol 而非 ABC：adapter 可用鸭子类型，与孵化代码一致；注册期做 verify 强制检查 |
| `models.py` | `VoiceEvent`、`VoiceEventType`、`ToolCall`、`ToolResult`、`AssistantResponse`、`VoiceSessionContext`；冷路径 `AssistantSpec`、`ToolSpec`、`CallRef`、`CallRecord` | `slots=True`；除 `raw` 外无自由 dict 字段；不含任何 provider 字段名 |
| `registry.py` | `from_config` → 构造 adapter 表；`on_tool_call(name)` / `on_assistant_request` / `on_event` 注册 handler；`dispatch` 入口 | handler 与 adapter 解耦；per-provider 开关；verify 强制检查在此执行 |
| `config.py` | YAML 解析、dataclass 校验、`env:VAR` 与 `provider:key` 引用解析 | 未知字段报 `ConfigError`（防拼写漂移）；`inbound.verify` 仅测试可 `disabled` |
| `dispatch.py` | `verify → parse → budget → handler → format` 单次编排；异常转状态码 | 纯函数式，不持有全局状态；每次调用只做一次 body 解析 |
| `fastapi.py` | `build_voice_router(registry, prefix="/voice")`；读取 raw body（`await request.body()`），headers 转小写 dict | 不自动解析 JSON（adapter 需要原始字节做签名） |
| `hooks.py` | 事件/错误的结构化日志与原始 payload 出口；默认实现为异步队列 sink | 热路径只入内存队列，绝不阻塞；无 hook 时丢弃原始 payload |
| `credentials.py` | 复用 `fusion_channel.credentials` 的 Provider 实现（§14） | 读值掩码；库自身不写密钥文件 |
| `adapters/` | 一平台一文件；`@register` 进 `ADAPTERS`；`_base` 提供出站 HTTP 注入 | 新增平台不改 registry/dispatch（开闭原则） |
| `budgets.py` | 由 provider 配置与事件类型计算 `deadline_seconds`；`remaining(ctx)` 供 handler 自查 | 预算只透传，不在此处强制等待 |

## 4. 热路径时序与预算

### 4.1 assistant-request 时序

```
平台 ──POST /voice/vapi（raw body + x-vapi-signature）──▶ build_voice_router
  ▼
dispatch(provider_id, headers, body)
  1. adapter.verify_request(headers, body)            # 失败 → VerificationError → 401
  2. adapter.parse_event(body) → VoiceEvent(ASSISTANT_REQUEST)
  3. ctx = VoiceSessionContext(call_id, deadline_seconds=config.assistant_request_s)
  4. assistant_handler(ctx)                            # 消费者业务，例如静态 assistantId
  5. adapter.format_assistant_response(resp)           # {"assistantId": "..."}
  ◀── 200 JSON ──
```

### 4.2 tool-call 时序

```
平台 ──POST /voice/vapi（tool-calls）──▶ dispatch
  1. verify_request → 401 on failure
  2. parse_event → VoiceEvent(TOOL_CALL, tool_calls=[…])
  3. ctx.deadline_seconds = config.tool_call_s（默认 5s，逐工具）
  4. asyncio.gather(每个 tool_call → on_tool_call 注册的 handler)
       单工具异常/超时 → ToolResult(error=...)（不外抛，不中断同批其他工具）
  5. format_tool_results → {"results": [{"toolCallId", "result"|"error"}]}
  ◀── 200 JSON ──
```

### 4.3 预算分配

| 环节 | assistant-request | tool-call | 依据 |
|------|------------------|-----------|------|
| 签名校验 | <1ms | <1ms | HMAC-SHA256 单次 |
| 归一化（parse） | <2ms | <5ms | `json.loads` + 字段提取 |
| **adapter 自身合计** | **<50ms** | **<20ms** | PRD §9 门槛 |
| 消费者 handler | 目标 P99 <500ms（平台硬限 7.5s） | ≤5s/工具 | PRD G3、消费者 P99 |
| 应答封装（format） | <1ms | <1ms | dict 构造 |

预算实现手段：dataclass `slots` 减少分配；`json.loads` 每请求仅一次（`verify` 用原始 bytes，不重复解码）；签名校验先于解析（无效请求不付解析成本）；日志与原始 payload 仅入内存队列，由 hooks 后台消费。

### 4.4 冷热分界

| 路径 | 方法 | 触发时机 | I/O |
|------|------|---------|-----|
| 热（入站） | `verify_request` / `parse_event` / `format_assistant_response` / `format_tool_results` | 平台同步回调 | 禁止任何阻塞 I/O |
| 冷（出站管理） | `deploy_assistant` / `transfer_call` / `hang_up` / `fetch_call` | 消费者显式调用（配置部署、通话中控制、对账补拉） | httpx REST，可重试 |

热路径不得调用冷路径方法；冷路径失败不回写热路径状态。

## 5. 统一事件模型与归一化

### 5.1 VoiceEvent 结构（与孵化代码一致）

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | `VoiceEventType` 字符串常量 | 9 类，见下表 |
| `call_id` | `str \| None` | 平台调用 ID；缺失时消费者按 `None` 兜底 |
| `tool_calls` | `list[ToolCall]` | 仅 `TOOL_CALL` 非空；`ToolCall(name, args, tool_call_id)` |
| `payload` | `dict` | 归一化字段，键名平台无关 |
| `raw` | `dict` | 原始 payload；**按 PRD §8.4，仅 `ERROR`/`UNKNOWN` 携带**（M1 对齐点：孵化代码目前所有事件都填 `raw`，抽取时收紧） |

### 5.2 事件与 payload 键

| VoiceEventType | payload 键 | 需要同步应答 |
|----------------|-----------|-------------|
| `ASSISTANT_REQUEST` | `from`, `to`, `direction` | 是（≤7.5s） |
| `TOOL_CALL` | —（用 `tool_calls`） | 是（默认 5s） |
| `CALL_STARTED` | `started_at` | 否 |
| `CALL_ENDED` | `ended_at`, `transcript?`, `recording_url?`, `cost?` | 否 |
| `STATUS_UPDATE` | `status`（dialing / in-progress / ended …） | 否 |
| `TRANSFER_REQUESTED` / `HANGUP_REQUESTED` | `reason` | 异步转冷路径 |
| `ERROR` | `detail` | 否 |
| `UNKNOWN` | —（全部原始字段在 `raw`） | 否 |

### 5.3 Vapi → 统一事件映射（现有实现即契约）

| Vapi `message.type` | VoiceEvent.type | 字段映射 |
|--------------------|----------------|---------|
| `assistant-request` | `ASSISTANT_REQUEST` | `from = message.call.customer.number` |
| `tool-calls` | `TOOL_CALL` | `message.toolCallList[]`（或 `toolCalls[]`）：`id` → `tool_call_id`，`function.name` → `name`，`function.arguments` → `args` |
| `status-update` | `STATUS_UPDATE` | `status = message.status` |
| `end-of-call-report` | `CALL_ENDED` | `endedAt`、`artifact.recordingUrl`、`artifact.transcript` |
| 其余已签名事件 | `UNKNOWN` | 原始 payload 全保留，消费者可审计 |
| JSON 解析失败 / 非对象 body | `ERROR` | `detail`；router 按 §10 返回 400 |

归一化规则：

- `call_id` 解析顺序：`message.call.id` → `raw.callId`；均无则为 `None`。
- **未知事件不丢弃、不 500**：归一为 `UNKNOWN` 并交消费者（PRD §5）。
- 平台字段名不得越过 adapter：消费者代码只见 §5.2 的键。
- `raw` 的 PII 策略由消费者承担（PRD §8.4）；库在 `hooks.on_raw_payload` 之外不持久化 `raw`。

## 6. 配置设计（voice.yaml）

```yaml
# fusion-voice 示例配置（data-driven 注册，风格同 fusion-channel channels.yaml）
providers:
  - id: booking_vapi              # 路由段：POST /voice/booking_vapi
    adapter: vapi                 # 必须已注册于 ADAPTERS
    enabled: true                 # per-provider 开关（PRD §4）
    credentials:
      webhook_secret: booking_vapi:webhook_secret   # CredentialProvider 键
      # 或 env:FUSION_VOICE_VAPI_SECRET
    budgets:
      assistant_request_s: 7.0    # 平台硬限 7.5s，留 0.5s 余量
      tool_call_s: 5.0            # PRD 默认值；平台差异见 V2
    inbound:
      max_body_bytes: 1048576     # 1 MiB，超限 413（PRD §8.2）
      verify: required            # required | disabled（仅测试，启动告警）

unknown_event_policy: record      # record（交消费者）| drop（仅计数）
logging:
  level: INFO
  raw_payload: hooks              # hooks（交 on_raw_payload）| off
```

字段与校验：

| 字段 | 默认 | 校验（`ConfigError`） |
|------|------|----------------------|
| `providers[].id` | 必填 | 非空、全局唯一、`^[a-z0-9_-]+$` |
| `providers[].adapter` | 必填 | 存在于 `ADAPTERS` |
| `providers[].enabled` | `true` | — |
| `credentials.*` | 必填（按 adapter 声明） | `env:` 或 `<provider_id>:<key>` 两种引用；缺失即报错 |
| `budgets.assistant_request_s` | `7.0` | `0 < v ≤ 7.5` |
| `budgets.tool_call_s` | `5.0` | `0 < v ≤ 10`（PRD V2） |
| `inbound.max_body_bytes` | `1048576` | ≥ 1024 |
| `inbound.verify` | `required` | `disabled` 时启动写 WARN 日志 |
| `unknown_event_policy` | `record` | 枚举 |
| 未知字段 | — | 报错（防拼写漂移静默失效） |

与 `channels.yaml` 的异同：同为 `providers/channels` 数组 + `adapter` 名 + 凭据引用 + per-instance 开关；无 `groups` / `mentions_required` / `capabilities_override` / `rate_limit`（语音回调无群聊与主动外发语义）；新增 `budgets`（同步时限是语音独有的一等配置）。

## 7. 适配器契约与 vapi 实现

### 7.1 VoiceProvider 契约（PRD §4，8 个方法）

| 方法 | 路径 | 强制程度 | 契约 |
|------|------|---------|------|
| `verify_request(headers, body)` | 热 | **强制**：未实现不可注册 | 失败抛 `VerificationError`；不得 I/O |
| `parse_event(body)` | 热 | 强制 | 永不抛异常给 router：解析失败返回 `VoiceEvent(ERROR)`；未知返回 `UNKNOWN` |
| `format_assistant_response(resp)` | 热 | 强制 | 统一应答 → 平台 JSON |
| `format_tool_results(results)` | 热 | 强制 | 按 `tool_call_id` 对齐；`result` 与 `error` 二选一 |
| `deploy_assistant(spec)` | 冷 | 可选（未实现 → `NotImplementedError`） | 平台 REST；返回 `AssistantRef` |
| `transfer_call(call_ref, target)` | 冷 | 可选 | 平台 REST |
| `hang_up(call_ref)` | 冷 | 可选 | 平台 REST |
| `fetch_call(call_ref)` | 冷 | 可选（对账兜底） | 平台 REST；返回 `CallRecord` |

冷路径实现顺序（按真实需求）：`fetch_call`（事件丢失对账）→ `deploy_assistant`（配置即代码 G4）→ `transfer_call` / `hang_up`。未实现的冷方法必须显式抛 `NotImplementedError`，禁止静默空实现（fail visibly）。

### 7.2 注册机制

- `adapters/__init__.py` 提供 `@register` 装饰器，按 `adapter.provider_id` 写入 `ADAPTERS`；重复 id 报 `ConfigError`。
- `VoiceRegistry.from_config` 实例化时执行 `_assert_verify_implemented(adapter_cls)`：检查 `verify_request` 不是 Protocol 默认实现 / 不是 `NotImplementedError` 占位，否则 `ConfigError`（与 fusion-channel `registry.py:434` 同机制）。
- adapter 构造参数：`credentials: dict[str, str]`（已解析明文，库内不落盘）+ `config: dict`（provider 段其余字段）。测试通过 `config["http_transport"]` 注入 `httpx.MockTransport`。
- 新增平台 = 新文件 + `@register`，registry / dispatch / config 零改动。

### 7.3 vapi adapter（M1 逐字迁移）

现有孵化实现（`booking_agent/voice/vapi_adapter.py`）即 M1 契约：

| 项 | 实现 |
|----|------|
| 签名 | HMAC-SHA256（`hmac.new(secret, raw_body, sha256).hexdigest()`），头 `x-vapi-signature`（大小写不敏感读取），`hmac.compare_digest` 恒时比较 |
| 密钥 | 构造期注入 `webhook_secret`；来源为消费者注入的 `CredentialProvider`（§9.3） |
| IP 白名单 | **不在库内**：部署层（反向代理）做，属防御纵深，不是 SPI 职责 |
| 事件映射 | §5.3 表；`toolCallList` / `toolCalls` 双键兼容 |
| 应答格式 | assistant：`{"assistantId": "..."}`；tool：`{"results": [{"toolCallId", "result"\|"error"}]}` |
| 解析失败 | `VoiceEvent(ERROR, payload={"detail": ...})`；router 转 400（§10） |

### 7.4 HTTP 注入（测试无网络）

`adapters/_base.py::HttpAdapterMixin` 复刻 fusion-channel 做法：`_client(config)` 若存在 `config["http_transport"]` 则用 `httpx.Client(transport=...)`，否则默认 transport。冷路径方法统一经 `_client` 发请求；热路径方法不得触碰 `_client`。

## 8. 同步分发与超时控制

```python
async def dispatch(provider_id: str, headers: dict[str, str], body: bytes) -> WebhookResponse:
    started = time.monotonic()
    adapter = self._adapters.get(provider_id)          # 未知/禁用 provider → 404
    if len(body) > self._max_body_bytes:
        return WebhookResponse(413, {"error": "body too large"})
    try:
        adapter.verify_request(headers, body)          # → 401
    except VerificationError:
        self._hooks.on_error(provider_id, "verification")
        return WebhookResponse(401, {"error": "unauthorized"})

    event = adapter.parse_event(body)
    if event.type == VoiceEventType.ERROR:             # 已签名但不可解析 → 400 + 审计
        self._hooks.on_event(provider_id, event, outcome="parse_error")
        return WebhookResponse(400, {"error": "bad payload"})

    ctx = VoiceSessionContext(call_id=event.call_id or "", deadline_seconds=self._budget(event))

    if event.type == VoiceEventType.TOOL_CALL:
        results = await asyncio.gather(*(self._run_tool(ctx, call) for call in event.tool_calls))
        self._hooks.on_event(provider_id, event, outcome="ok", ms=self._ms(started))
        return WebhookResponse(200, adapter.format_tool_results(results))

    if event.type == VoiceEventType.ASSISTANT_REQUEST:
        try:
            resp = await asyncio.wait_for(self._assistant_handler(ctx), timeout=ctx.deadline_seconds)
        except asyncio.TimeoutError:
            self._hooks.on_error(provider_id, "assistant_timeout")
            return WebhookResponse(504, {"error": "assistant timeout"})
        except Exception:
            log.exception("assistant handler failed")   # 服务端日志，应答脱敏
            return WebhookResponse(500, {"error": "internal error"})
        return WebhookResponse(200, adapter.format_assistant_response(resp))

    self._hooks.on_event(provider_id, event, outcome="ok", ms=self._ms(started))
    return WebhookResponse(200, {})

async def _run_tool(self, ctx: VoiceSessionContext, call: ToolCall) -> ToolResult:
    handler = self._tool_handlers.get(call.name)
    if handler is None:
        return ToolResult(tool_call_id=call.tool_call_id, name=call.name, error="unknown tool")
    try:
        return await asyncio.wait_for(handler(ctx, call), timeout=ctx.deadline_seconds)
    except asyncio.TimeoutError:
        return ToolResult(tool_call_id=call.tool_call_id, name=call.name, error="timeout")
    except Exception:
        log.exception("tool handler failed: %s", call.name)
        return ToolResult(tool_call_id=call.tool_call_id, name=call.name, error="tool failed")
```

规则：

- **预算透传**：`ctx.deadline_seconds` 来自配置（§6），handler 可自查剩余预算；库只对超时兜底，不重试（重试属平台或消费者）。
- **单工具失败隔离**：错误写回该 `tool_call_id` 的 `error` 字段，同批其他工具不受影响；错误文本脱敏，细节进服务端日志。
- **无热路径重试/持久化**：符合 PRD §3.2 非目标。
- **幂等边界**：库不做去重（PRD 非目标）；消费者以 `call_id + tool_call_id` 自管。
- 状态码边界见 §10 矩阵；未知/禁用 `provider_id` → 404（不泄露存在性）。

## 9. 出站管理路径

### 9.1 数据结构

| 类型 | 字段 | 用途 |
|------|------|------|
| `ToolSpec` | `name`, `description`, `parameters`（JSON Schema dict） | 跨平台工具定义，同一份由 `deploy_assistant` 翻译 |
| `AssistantSpec` | `name`, `model`, `voice`, `transcriber`, `prompt`, `tools: list[ToolSpec]`, `metadata: dict`, `platform_extra: dict` | 配置即代码（G4）；M1 只覆盖公共交集，`platform_extra` 为 escape hatch（V3） |
| `AssistantRef` | `provider_id`, `assistant_id` | 部署结果 |
| `CallRef` | `provider_id`, `call_id` | 通话控制与补拉的句柄 |
| `CallRecord` | `call_id`, `status`, `started_at`, `ended_at?`, `transcript?`, `recording_url?`, `cost?` | 对账兜底 |

### 9.2 方法语义

- `deploy_assistant(spec) -> AssistantRef`：创建或更新平台 assistant（按 `metadata` 中的外部键做幂等更新）；请求体由 adapter 将 `AssistantSpec` 翻译为平台格式（Vapi: assistant 资源；Retell: agent 资源）。平台专属字段仅经 `platform_extra`，不进统一模型。
- `transfer_call(call_ref, target)` / `hang_up(call_ref)`：经平台 call 控制端点执行；失败抛库异常，由消费者决定降级（例如转留资）。
- `fetch_call(call_ref) -> CallRecord`：`CALL_ENDED` 事件丢失时的对账补拉（PRD §5）。
- 平台 REST 端点的具体路径与请求体差异全部隔离在 adapter 内；本方案不固定各平台 URL，实现时以平台文档为准。

### 9.3 凭据来源

- 库只接受已解析的 `dict[str, str]` 或 `CredentialProvider`；**不读 fusion-identity、不写密钥文件、不做网络取密**。
- 消费者的标准接线（ai-booking-agent §3.5 已实践）：启动期从 fusion-identity 凭据平面 GET 注入，本地 `0600` 镜像仅作缓存；会话密钥（webhook secret）经 `AESGCMCredentialProvider` 或 `env:VAR` 提供。
- 读值掩码：日志与 `describe()` 只输出掩码（复用 fusion-channel `mask()`）。

## 10. 安全设计

### 10.1 PRD §8 红线落地位置

| 红线 | 落地位置 | 验证方式 |
|------|---------|---------|
| 验证强制在 SPI 层 | `adapters/__init__.py` 注册期检查 + `registry.from_config` 复查 | 单测：未实现 verify 的 adapter 注册即 `ConfigError` |
| Fail-closed | `dispatch` 的异常边界（§8 伪代码） | 安全测试：畸形输入永不 5xx |
| 请求体上限 1 MiB | `config.inbound.max_body_bytes`，`dispatch` 首步检查 | 单测：1 MiB+1 → 413 |
| 凭据不进库 | `credentials.py` 薄封装；库不落盘、不取密 | 审计：库代码无文件写/网络取密路径 |
| 错误脱敏 | handler 异常只回通用文本；细节仅服务端日志 | 单测：异常信息不出现在应答体 |
| 热路径零日志阻塞 | `hooks` 仅入内存队列；无 hook 时丢弃 | 基准测试 p99 不随日志量增长 |

### 10.2 状态码矩阵

| 场景 | 状态码 | 应答体 | 审计 |
|------|--------|--------|------|
| 未知 / 禁用 provider | 404 | `{"error": "not found"}` | 计数指标 |
| body 超限 | 413 | `{"error": "body too large"}` | 计数指标 |
| 签名缺失 / 无效 | 401 | `{"error": "unauthorized"}` | `on_error("verification")`，含来源 IP |
| 已签名但 JSON 不可解析 / 非对象 | 400 | `{"error": "bad payload"}` | `on_event(ERROR)` 携 `raw` |
| 已签名、可解析、未知事件类型 | 200 | `{}` | `UNKNOWN` 事件交消费者（或不交，按 `unknown_event_policy`） |
| 工具 handler 失败 / 超时 | 200 | 该项 `error` 字段 | 服务端日志（脱敏后细节） |
| assistant handler 超时 | 504 | `{"error": "assistant timeout"}` | `on_error("assistant_timeout")` |
| assistant handler 抛异常 | 500 | `{"error": "internal error"}` | 完整堆栈进服务端日志 |
| 正常 | 200 | §7.3 应答格式 | `on_event(outcome="ok", ms=…)` |

### 10.3 原始 payload 与 PII

- 库内不持久化任何请求体；签名校验使用原始 bytes（不预先 JSON 解码）。
- `raw` 仅在 `ERROR` / `UNKNOWN` 事件中填充，且只经 `hooks.on_raw_payload` 交给消费者，由消费者的 PII 策略决定落库与保留期（PRD §8.4）。
- `call_id`、事件类型、耗时、验证结果可进结构化日志；电话号码等 payload 字段默认不进日志。

## 11. 性能与可观测性

### 11.1 性能手段清单

| 手段 | 收益 |
|------|------|
| 签名校验先于 JSON 解析 | 无效请求不付解析成本 |
| `json.loads` 每请求一次，`raw` 按引用传递 | 避免重复解码 |
| `dataclass(slots=True)` | 减少内存分配与属性查找 |
| 热路径零 I/O、零锁、零队列等待 | p99 稳定 |
| handler 并发（`asyncio.gather`） | tool 批处理总耗时 ≈ 最慢单工具 |
| adapter 无状态（每次请求只做纯计算） | 无跨请求竞争 |

### 11.2 基准测试方法

- `tests/bench/test_hotpath.py`：对典型 Vapi payload（assistant-request ≈1 KB、tool-calls ≈2 KB）跑 10k 次，断言 adapter 自身 p99 <50ms / <20ms（PRD §9）。
- 冷路径不计入热路径基准；`httpx.MockTransport` 注入使一切测试无网络。
- 基准结果与 CI 门槛：回归阈值 = 基线 ×1.5，超限即失败。

### 11.3 结构化日志字段

每个入站请求一条（异步 sink 输出 JSON）：

| 字段 | 说明 |
|------|------|
| `ts`, `level` | 时间戳、级别 |
| `provider_id`, `event_type`, `call_id` | 路由与关联键 |
| `outcome` | `ok` / `verification_failed` / `parse_error` / `timeout` / `handler_error` |
| `verify_ms`, `normalize_ms`, `dispatch_ms`, `total_ms` | 分段耗时 |
| `tool_count` | tool-call 批大小 |
| 不含 | body、电话号码、transcript、凭据 |

### 11.4 指标（PRD M3 预留）

`hooks` 暴露计数与直方图接口：验证失败率、事件类型分布、归一化耗时分位、单工具超时率。M1 仅提供接口与日志实现，Prometheus 对接留在消费者侧（库不绑定监控后端）。

## 12. 测试策略

| 层级 | 内容 | 工具/样本 |
|------|------|----------|
| 单元 | dispatch 状态码矩阵、预算超时、单工具失败隔离、config 校验 | pytest + `asyncio_mode=auto` |
| 契约（golden） | 每个平台事件样本 → 期望 `VoiceEvent`（`tests/fixtures/vapi/*.json`） | 真实平台回调脱敏样本 |
| 签名 | 正确/错误/缺失/大小写头/空白变体；模糊测试签名绕过（PRD §9：0 命中） | hypothesis 生成变体 |
| 回归 gate | ai-booking-agent 既有 `/vapi/webhook` 集成测试在迁移后全绿 | 消费者仓库测试套件 |
| 基准 | §11.2 热路径预算 | pytest-benchmark 或自写计时 |
| 无网络 | 全部测试注入 `httpx.MockTransport` / 假 transport | fusion-channel 同款 `http_transport` 注入 |

原则：测试通过但不能说明行为（例如只断言 200 不断言字段）视为无效测试；错误路径与正常路径同等覆盖。

## 13. M0→M1 抽取计划

### 13.1 触发条件（PRD §6/§10）

- 第二个真实消费者出现，或第二个 adapter 落地（retell/bland 任一）。仅当条件满足才启动，避免过早抽象。

### 13.2 迁移步骤

1. **仓库骨架**：按 §3 建 `pyproject.toml`（镜像 fusion-channel）、src 布局、`examples/voice.yaml`、测试目录；`pip install -e fusion-voice` 可导入。
2. **代码 move（不改行为）**：`booking_agent/voice/spi.py` → `fusion_voice/models.py` + `spi.py`；`vapi_adapter.py` → `fusion_voice/adapters/vapi.py`。仅改 import 路径；`raw` 收紧（§5.1）作为独立提交并补测试。
3. **新增编排层**：`registry.py` / `dispatch.py` / `config.py` / `fastapi.py` / `hooks.py`。消费者现有 `verify → normalize → dispatch` 逻辑与 §8 伪代码逐条对齐。
4. **消费者切换**：ai-booking-agent 的 `/vapi/webhook` 改为 `build_voice_router(registry)`，保留业务管道（状态机/幂等/适配层）不动；删除仓内 `voice/spi.py`、`voice/vapi_adapter.py`。
5. **回归验收**：ai-booking-agent 集成测试全绿（PRD §9），热路径基准达标；观察一个生产周期后退回「孵化代码」路径。

### 13.3 风险与回滚

| 风险 | 缓解 |
|------|------|
| 导入路径变更引发消费者散落引用 | 迁移前 grep 消费者全仓引用；单提交完成 rename |
| 预算/状态码行为漂移 | §10 矩阵纳入契约测试；回归 gate 强制 |
| 凭据来源不一致（identity 平面 vs 本地） | 库只收已解析值；消费者注入层不变 |
| 抽取后消费者发现 bug | 回滚 = 恢复孵化代码提交；两边行为等价测试先行 |
| 第三个平台接入时 SPI 不足 | `platform_extra` escape hatch；SPI 变更走 PRD/本文联动更新 |

## 14. 与 fusion-channel 的复用边界

### 14.1 复用清单（import，不复制）

| 复用项 | 来源 | 理由 |
|--------|------|------|
| `CredentialProvider` / `EnvCredentialProvider` / `AESGCMCredentialProvider` / `mask()` | `fusion_channel.credentials` | PRD §8.3「沿用」，避免第三份实现 |
| `verify_hmac_sha256_hex(secret, raw_body, signature)` | `fusion_channel.verify` | Vapi 签名即可直接用；Twilio HMAC-SHA1 同源 |
| `load_config` 风格 / `ConfigError` | `fusion_channel.config`（借鉴结构，非 import） | 同类数据驱动校验模式 |
| `build_channel_router` 工厂模式 | `fusion_channel.fastapi`（借鉴结构） | `build_voice_router` 同构 |
| `HttpAdapterMixin` / `http_transport` 注入 | `fusion_channel.adapters`（借鉴或轻量复制） | 测试无网络的既有约定 |

依赖方向决策：**fusion-voice 依赖 fusion-channel（仅通用原语），反向不成立**。二者业务模型互斥（同步 vs 异步），但通用原语应单源。备选方案：若日后 fusion-channel 不希望被依赖，将 `credentials.py` 与 `verify_hmac_sha256_hex` 上提到 `fusion-core`，两边同时改 import；M1 不预先做此抽象。

### 14.2 不复用清单（明确拒绝）

| 机制 | 拒绝原因 |
|------|---------|
| archive-first 入站落档 | 语音入站是同步短事务，落档由消费者决定（PRD §3.2） |
| outbox 重试 / 投递回执 | 同步应答无「发送意图先入队」语义 |
| dedup 去重计数 | 语音事件去重键与消息模型不同，由消费者按 `call_id + tool_call_id` 处理 |
| 卡片降级渲染（`CardRenderer`） | 语音无卡片概念 |
| `ChannelRegistry.handle_inbound` 管线 | 其验证→落档→异步分发形态与热路径硬约束冲突 |

## 15. 开放问题决策映射

| 编号 | PRD 待确认项 | 本方案默认处置 | 未决时的影响 |
|------|--------------|----------------|--------------|
| V1 | Retell/Bland/ElevenLabs 签名与事件细节未实测 | P1 adapter 落地前逐家沙箱探测；`verify_request` 必须有实测依据才注册 | 不影响 M1（vapi 单 adapter） |
| V2 | 各平台 tool-call 同步超时上限 5–10s 差异 | `tool_call_s` 默认 5.0、可配 ≤10；per-provider 独立配置 | 预算透传语义已支持，不改架构 |
| V3 | AssistantSpec 跨平台表达能力上限 | M1 仅公共交集；平台特有能力走 `platform_extra` | 新增平台能力时不改 SPI |
| V4 | Twilio ConversationRelay 是否纳入 connector 形态 | P2 观察，不承诺；如纳入，其签名/事件模型差异在 adapter 内消化 | 不影响 M1/M2 |

## 16. 文档维护

- 与 PRD 的联动：SPI 签名、事件模型、适配器路线、待确认项结论变化时，PRD 与本文同一次改动更新（PRD §12 维护规则）。
- 触发本文更新的具体情形：热/冷路径边界变化；配置 schema 变化；状态码矩阵变化；与 fusion-channel 的依赖方向变化；M1 抽取完成或回滚。
- 本文不记录变更历史；用当前事实替换过期表述。
