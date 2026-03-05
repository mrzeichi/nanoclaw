# NanoClaw vs nanobot — 对比分析

> nanobot 项目地址：<https://github.com/HKUDS/nanobot>

---

## 一、Agent 实现设计概览

### NanoClaw

NanoClaw 是一个基于 **TypeScript / Node.js** 的单进程 agent 系统。其核心架构如下：

```
单 Node.js 进程
├── channels/        消息通道（WhatsApp、Email 等，通过 skills 扩展）
├── src/index.ts     主循环：消息轮询 → 触发词检测 → 分发至 GroupQueue
├── GroupQueue       每组一个串行队列，保证同一会话不并发
├── container-runner 将 agent 提示词写入 Docker/Apple Container，读取结构化输出
├── ipc.ts           容器 → 宿主机的 IPC 文件通信（注册群组、发消息、管理任务）
├── task-scheduler   基于 cron/interval 的定时任务，复用同一容器执行通道
└── db.ts (SQLite)   持久化消息、会话、群组注册信息、定时任务
```

**关键实现细节：**

1. **容器隔离执行**：每次 agent 调用都在一个新的 Linux 容器内运行 Claude Agent SDK；群组目录通过 volume mount 注入，宿主机项目代码以只读方式挂载，`.env` 被 `/dev/null` 遮蔽。
2. **流式输出解析**：容器 stdout 通过 `---NANOCLAW_OUTPUT_START---` / `---NANOCLAW_OUTPUT_END---` 哨兵标记进行结构化分割，支持多轮流式推送给用户。
3. **消息游标机制**：`lastAgentTimestamp` 记录每个群组最后处理的消息时间戳，崩溃恢复时回滚游标，避免重复或丢失响应。
4. **触发词过滤**：非主群组需要消息包含 `@<ASSISTANT_NAME>` 触发词才会激活 agent；触发词权限通过 `sender-allowlist` 独立配置。
5. **IPC 文件总线**：容器向 `/data/ipc/<group>/` 目录写入 JSON 指令文件，宿主机 IPC watcher 轮询处理（注册群组、发送消息、创建定时任务），实现容器→宿主机的反向控制。

---

### nanobot

nanobot 是一个基于 **Python** 的轻量 agent 系统（核心代码约 4,000 行）。其核心架构如下：

```
nanobot/
├── agent/
│   ├── loop.py      Agent 主循环（LLM ↔ 工具调用交替执行）
│   ├── context.py   提示词构建（系统提示、会话历史、工具列表）
│   ├── memory.py    持久记忆（工作区文件）
│   ├── skills.py    技能加载（Markdown 文件驱动的动态工具）
│   ├── subagent.py  后台子 agent（并发任务）
│   └── tools/       内置工具（shell、文件、web、spawn 等）
├── channels/        消息通道（Telegram、Discord、WhatsApp、Feishu 等 10+）
├── bus/             消息路由总线
├── cron/            定时任务
├── heartbeat/       30 分钟周期性唤醒（读取 HEARTBEAT.md）
├── providers/       LLM 提供商注册表（LiteLLM 封装）
├── session/         会话状态
└── config/          JSON 配置文件驱动
```

**关键实现细节：**

1. **多 Provider / LiteLLM**：通过 `ProviderSpec` 注册表统一管理十余个 LLM 提供商，添加新 provider 只需两步（注册表 + schema），底层由 LiteLLM 负责调用。
2. **技能（Skills）系统**：技能以 Markdown 文件形式描述，agent 动态加载并将其转化为可调用工具，社区可通过 ClawHub 发布和安装技能。
3. **MCP 支持**：原生支持 Model Context Protocol，可通过 stdio 或 HTTP 接入外部工具服务器，工具在启动时自动发现注册。
4. **Heartbeat 机制**：gateway 每 30 分钟唤醒一次，读取 `~/.nanobot/workspace/HEARTBEAT.md` 中的待办任务列表，执行后将结果推送到最近活跃的通道。
5. **进程内执行**：agent 工具（shell、文件读写）直接在宿主进程中执行，可通过 `restrictToWorkspace: true` 限制访问范围。

---

## 二、主要差异（不超过 5 项）

| # | 维度 | NanoClaw | nanobot |
|---|------|----------|---------|
| 1 | **语言与运行时** | TypeScript / Node.js，编译为 ESM | Python，直接解释执行 |
| 2 | **Agent 执行隔离** | 每次调用在独立 Linux 容器中运行，OS 级别隔离，无需应用层沙箱 | Agent 在宿主进程内运行，通过 `restrictToWorkspace` 做软限制，无容器隔离 |
| 3 | **LLM 提供商** | 绑定 Anthropic Claude Agent SDK，单一提供商 | 通过 LiteLLM 支持 OpenRouter、Anthropic、OpenAI、DeepSeek、Gemini 等十余个提供商，可自由切换模型 |
| 4 | **通道数量与扩展方式** | 默认仅 WhatsApp + Email；新通道通过"skill 脚本"对代码库进行手术式改造来添加 | 内置 10+ 通道（Telegram、Discord、WhatsApp、Feishu、DingTalk、Slack、Email、QQ、Matrix、Mochat）；通过 JSON 配置启用，零代码改动 |
| 5 | **定制化哲学** | "代码即配置"——倡导直接修改源码，通过 Claude Code 辅助的 skills 脚本实现可重复的代码变换 | "配置即代码"——通过 `~/.nanobot/config.json` 驱动几乎所有行为，尽量避免修改源码 |

---

## 三、主要相似点（不超过 5 项）

| # | 维度 | 说明 |
|---|------|------|
| 1 | **轻量化设计理念** | 两者都明确定位为 OpenClaw/ClawBot 等重量级平台的精简替代，强调代码量少、易读、易理解 |
| 2 | **多通道统一路由** | 两者都抽象出统一的通道接口（NanoClaw 的 `Channel` + `registry`；nanobot 的 `bus/`），消息在进入 agent 前经过同一套格式化/路由逻辑 |
| 3 | **持久化会话与记忆** | 两者都维护跨重启的会话状态（NanoClaw 用 SQLite session 表 + 每群组文件系统；nanobot 用 session/ 模块 + workspace 文件），使 agent 能记住上下文 |
| 4 | **定时/周期任务** | 两者都内置了周期任务机制（NanoClaw 的 DB 驱动 cron/interval 调度器；nanobot 的 heartbeat + cron/ 模块），可在无用户触发时主动执行任务并推送结果 |
| 5 | **Docker 支持** | 两者都提供 Docker/Docker Compose 部署方案，便于跨平台运行和环境隔离（NanoClaw 以容器为核心执行单元；nanobot 提供 `docker compose` 一键部署） |
