# NanoClaw Python 化：可从 nanobot 参考或复用的功能与代码

> 本文档面向"将 NanoClaw 从 TypeScript/Node.js 迁移到 Python"的工程场景，
> 按模块逐一说明可以**直接复用、最小改动复用或仅参考设计**的 nanobot 代码。
>
> nanobot 源码：<https://github.com/HKUDS/nanobot>

---

## 总体迁移思路

NanoClaw 的核心职责可以用一张对应表来表达：

| NanoClaw（TypeScript）模块 | 对应 nanobot（Python）模块 | 复用难度 |
|---------------------------|--------------------------|---------|
| `src/channels/` + 注册表 | `nanobot/channels/` + `BaseChannel` | ⭐ 直接参考/复用 |
| `src/index.ts` 消息主循环 | `nanobot/bus/queue.py` + `AgentLoop` | ⭐ 设计可复用 |
| `src/group-queue.ts` 并发控制 | `nanobot/bus/queue.py` | ⭐ 逻辑可直接翻译 |
| `src/router.ts` 消息格式化 | `nanobot/agent/context.py` | ⭐ 设计参考 |
| `src/db.ts` SQLite 持久化 | `nanobot/session/manager.py` + `cron/` | ⭐ 参考设计 |
| `src/task-scheduler.ts` 定时任务 | `nanobot/cron/service.py` | ⭐⭐ 可直接复用 |
| `src/sender-allowlist.ts` 权限控制 | `nanobot/channels/base.py::is_allowed()` | ⭐⭐ 可直接复用 |
| `src/container-runner.ts` 执行隔离 | （nanobot 无对应；保留 NanoClaw 原始设计） | ❌ 需自行保留 |

---

## 一、消息总线（`nanobot/bus/`）— 直接复用

### 为什么可以复用

NanoClaw 的消息主循环（`src/index.ts`）用 `setInterval` 轮询 SQLite，将消息路由到 `GroupQueue`，再由 GroupQueue 回调 `processGroupMessages`。这套逻辑和 nanobot 的 `MessageBus` 完全同构，只是 nanobot 用了 Python `asyncio.Queue` 而非轮询。

### 可直接复用的文件

```
nanobot/bus/queue.py      # MessageBus — asyncio.Queue 封装
nanobot/bus/events.py     # InboundMessage / OutboundMessage 数据类
```

**`queue.py` 核心实现（约 40 行，可零修改复用）：**

```python
class MessageBus:
    def __init__(self):
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()

    async def publish_inbound(self, msg: InboundMessage) -> None:
        await self.inbound.put(msg)

    async def consume_inbound(self) -> InboundMessage:
        return await self.inbound.get()
    # … publish_outbound / consume_outbound 同理
```

**迁移说明：**
- 将 NanoClaw 中 SQLite 轮询 + `getNewMessages()` 替换为 `bus.consume_inbound()` 阻塞等待。
- 通道层（WhatsApp / Telegram 等）调用 `bus.publish_inbound()` 投递消息。
- GroupQueue 改为以 `session_key`（channel:chat_id）为键的 `asyncio.Lock` 或 `asyncio.Semaphore`，与 nanobot 的 `_processing_lock` 对应。

---

## 二、通道抽象层（`nanobot/channels/`）— 高价值复用

### 为什么可以复用

NanoClaw 的 `Channel` 接口（TypeScript）和 nanobot 的 `BaseChannel`（Python）高度同构：

| NanoClaw `Channel` 接口 | nanobot `BaseChannel` 方法 |
|------------------------|--------------------------|
| `connect()` | `start()` |
| `sendMessage(jid, text)` | `send(OutboundMessage)` |
| `disconnect()` | `stop()` |
| `ownsJid(jid)` | —（通道天然拥有自己的 chat_id） |
| `setTyping?(jid, bool)` | （各通道自行实现） |

### 可直接复用的文件

```
nanobot/channels/base.py          # BaseChannel 抽象类（含 is_allowed() 权限检查）
nanobot/channels/telegram.py      # Telegram 实现（含 markdown→HTML 格式化、消息分割）
nanobot/channels/whatsapp.py      # WhatsApp 实现（Python↔Node.js WebSocket 桥接）
nanobot/channels/discord.py       # Discord 实现
nanobot/channels/slack.py         # Slack Socket Mode 实现
nanobot/channels/email.py         # Email IMAP/SMTP 实现
nanobot/channels/feishu.py        # 飞书 WebSocket 实现
nanobot/channels/dingtalk.py      # 钉钉 Stream Mode 实现
```

**`base.py` 中的 `is_allowed()` 权限检查（可替换 NanoClaw 的 `sender-allowlist.ts`）：**

```python
def is_allowed(self, sender_id: str) -> bool:
    allow_list = getattr(self.config, "allow_from", [])
    if not allow_list:
        return False          # Empty list = deny all (consistent with new nanobot behavior)
    if "*" in allow_list:
        return True
    return str(sender_id) in allow_list
```

**迁移说明：**
- 将 NanoClaw 的 `sender-allowlist.ts` 替换为继承 `BaseChannel` 并在 `_handle_message()` 中自动调用 `is_allowed()` 的模式。
- WhatsApp 通道的 `whatsapp.py` 通过 WebSocket 连接同一个 Node.js 桥（`@whiskeysockets/baileys`），与 NanoClaw 现有的 WhatsApp skill 完全兼容，**可以直接复用现有 WhatsApp 认证数据**。
- Telegram、Slack、Email 等通道在 nanobot 中已完整实现，Python 化后无需重写。

---

## 三、定时任务（`nanobot/cron/`）— 直接复用

### 为什么可以复用

NanoClaw 的 `task-scheduler.ts` 使用 SQLite 存储任务、支持 `cron`/`interval`/`once` 三种模式，与 nanobot 的 `CronService` 几乎等价，但 nanobot 版本：
- 使用 JSON 文件存储（更轻量，无需迁移 SQLite schema）
- 用 `croniter` 库（pip install）做 cron 解析，替代 NanoClaw 的 `cron-parser` npm 包
- 将定时任务作为 **LLM 工具**（`CronTool`）暴露给 agent，agent 可自行创建/删除任务

### 可直接复用的文件

```
nanobot/cron/service.py   # CronService — 任务持久化、调度、执行
nanobot/cron/types.py     # CronJob / CronSchedule / CronPayload 数据类
nanobot/agent/tools/cron.py  # CronTool — 将 cron 操作暴露为 LLM 工具
```

**`CronService` 关键设计（可直接翻译自 `task-scheduler.ts`）：**

```python
class CronService:
    # 支持 "at" (once) / "every" (interval) / "cron" (cron 表达式) 三种模式
    # 任务持久化到 jobs.json，支持外部修改后热重载
    # on_job callback injected by AgentLoop, reuses agent execution channel
```

**迁移说明：**
- 将 NanoClaw SQLite 中的 `scheduled_tasks` 表迁移到 `jobs.json`，或保留 SQLite 并用 SQLAlchemy/aiosqlite 替换 `better-sqlite3`。
- `CronTool` 让 agent 自主管理定时任务，无需宿主机 IPC 文件写入。

---

## 四、会话与记忆（`nanobot/session/` + `nanobot/agent/memory.py`）— 直接复用

### 为什么可以复用

NanoClaw 将 Claude Agent SDK 的会话 ID（`sessionId`）存入 SQLite sessions 表，并通过 `lastAgentTimestamp` 游标管理上下文窗口。nanobot 用 JSONL 文件实现了更完整的两层记忆系统：

- **`SessionManager`**：按 `channel:chat_id` 键存储对话历史（JSONL），跨重启持久化
- **`MemoryStore`**：`MEMORY.md`（长期事实）+ `HISTORY.md`（可 grep 的时间线日志），由 LLM 调用 `save_memory` 工具自动压缩整理

### 可直接复用的文件

```
nanobot/session/manager.py        # SessionManager + Session 数据类
nanobot/agent/memory.py           # MemoryStore（两层记忆）
```

**迁移说明：**
- 将 NanoClaw 每群组的 `CLAUDE.md` 文件（Claude Agent SDK 的 memory）迁移到 `MEMORY.md` + `HISTORY.md` 双文件格式。
- `SessionManager.get_or_create(key)` 直接替换 NanoClaw 的 SQLite sessions 表读写。
- 若保留容器执行方式，`MemoryStore` 的文件路径对应容器内的 `/workspace/group/memory/`。

---

## 五、LLM Provider 层（`nanobot/providers/`）— 按需复用

### 为什么值得复用

NanoClaw 当前绑定 Anthropic Claude Agent SDK（TypeScript），Python 化后若要支持多 provider，nanobot 的 Provider Registry 是现成的方案：

```
nanobot/providers/registry.py     # ProviderSpec 注册表（单一真相源）
nanobot/providers/base.py         # LLMProvider 抽象类
nanobot/providers/litellm.py      # LiteLLM 封装（统一调用接口）
nanobot/providers/anthropic.py    # Anthropic 直连（含 prompt caching）
```

**`ProviderSpec` 注册表模式的价值：**
- 添加新 provider 只需在 `registry.py` 增加一条 `ProviderSpec`，环境变量注入、模型名称前缀、LiteLLM 路由均自动处理
- 若 Python 化后仍只用 Anthropic，只需保留 `anthropic.py`；若要支持 OpenAI/Gemini/DeepSeek，直接引入整个 `providers/` 目录

**迁移说明：**
- Python 的 `anthropic` 官方 SDK 可替换 TypeScript 的 `@anthropic-ai/claude-code`，`streaming` 接口基本一一对应
- 若放弃容器执行改为进程内执行，可直接复用 nanobot 的 `AgentLoop`（见下节）

---

## 六、Agent 主循环（`nanobot/agent/loop.py`）— 容器化路径下参考设计

### 两种 Python 化路径的选择

Python 化时有两条路可走，对 `agent/loop.py` 的复用程度不同：

#### 路径 A：保留容器执行（推荐，保持 NanoClaw 安全隔离特性）

```
宿主进程（Python）
├── channels/           ← 复用 nanobot BaseChannel + 各通道实现
├── bus/                ← 复用 nanobot MessageBus
├── session/ + memory/  ← 复用 nanobot SessionManager + MemoryStore
├── cron/               ← 复用 nanobot CronService
└── container_runner.py ← 自行翻译 src/container-runner.ts
                           （subprocess.Popen + sentinel 标记解析）

容器内（Node.js 不变）
└── container/agent-runner/  ← 不需修改
```

此路径下，`agent/loop.py` **不需要复用**，容器内的 Claude Agent SDK 仍负责 LLM 调用与工具执行。

#### 路径 B：放弃容器，进程内执行（更简单，牺牲 OS 级隔离）

```
宿主进程（Python）
├── channels/           ← 复用 nanobot
├── bus/                ← 复用 nanobot
└── agent/loop.py       ← 直接复用 nanobot AgentLoop
```

此路径下，`agent/loop.py` 可**完整复用**。NanoClaw 的 agent 执行权限控制改为 `restrictToWorkspace=True`。

---

## 七、提示词构建（`nanobot/agent/context.py`）— 参考设计

nanobot 的 `ContextBuilder` 展示了一种清晰的提示词分层结构，可为 Python 化后的系统提示组装提供参考：

```
系统提示 = 身份（SOUL.md/AGENTS.md）
         + 记忆（MEMORY.md）
         + 技能摘要（skills/*/SKILL.md）
         + 运行时元数据（当前时间、Channel、Chat ID）
```

与 NanoClaw 的 `groups/<name>/CLAUDE.md` 对应：`SOUL.md` 存放群组级别的 agent 个性，`MEMORY.md` 存放记忆，`AGENTS.md` 存放工具权限说明。

---

## 八、迁移优先级建议

按复用价值从高到低排列，建议按如下顺序进行迁移：

| 优先级 | 模块 | 理由 |
|--------|------|------|
| 🥇 1 | `nanobot/channels/` (base + whatsapp) | 立即解锁多通道，WhatsApp 桥可复用现有认证 |
| 🥇 1 | `nanobot/bus/` | 消除 SQLite 轮询，改为 asyncio 事件驱动，架构更简洁 |
| 🥈 2 | `nanobot/session/` + `nanobot/agent/memory.py` | 替换 SQLite sessions 表，获得持久记忆压缩能力 |
| 🥈 2 | `nanobot/cron/` | 替换 task-scheduler，获得 agent 自管理任务能力 |
| 🥉 3 | `nanobot/providers/` | 按需引入：若只用 Anthropic 可跳过；若要多 provider 则直接复用 |
| 🥉 3 | `nanobot/agent/loop.py` | 仅在选择"路径 B（放弃容器）"时复用 |

---

## 九、依赖包清单（Python 化所需）

复用上述 nanobot 模块所需的主要 Python 依赖：

```
# 核心
litellm              # LLM 统一调用（可选，若只用 Anthropic 可用 anthropic 直连）
anthropic            # Anthropic Python SDK
loguru               # 日志（替换 NanoClaw 的 pino）
pydantic             # 配置 schema 验证（替换 zod）

# 通道
python-telegram-bot  # Telegram
discord.py           # Discord
slack-sdk            # Slack
websockets           # WhatsApp 桥（nanobot 的 whatsapp.py 通过 WebSocket 连 Node.js 桥）
aiosmtplib + aioimaplib  # Email

# 定时任务
croniter             # cron 表达式解析（替换 cron-parser npm）
apscheduler          # 可选：更完整的调度器

# 数据库（若保留 SQLite）
aiosqlite            # 异步 SQLite
sqlalchemy[asyncio]  # 可选：ORM

# 容器控制（路径 A）
# Python subprocess 标准库即可，无需额外依赖
```
