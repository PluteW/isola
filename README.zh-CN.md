<div align="center">
<img src="branding/logo.svg" width="88" height="88" alt="Isola">

# Isola

**面向 agent（智能体）框架的项目级记忆层**
<br><sub>Project-scoped memory for AI agent frameworks — OpenClaw, Claude Code, Codex.</sub>
<br><sub>[English](README.md) | 简体中文</sub>
<br><sub><a href="#为什么需要-isola">为什么需要 Isola</a> · <a href="#isola-是什么">Isola 是什么</a> · <a href="#特性">特性</a> · <a href="#快速开始">快速开始</a> · <a href="#工作原理">工作原理</a> · <a href="#接入-harness">接入 harness</a> · <a href="#范围与路线">范围与路线</a> · <a href="#许可">许可</a></sub>

`Apache-2.0`

</div>

---

## 为什么需要 Isola

在 Claude Code、OpenClaw、Codex 等 agent 环境里同时推进多个项目时，它们往往挤在同一个入口对话、或几个相邻会话中。问题不只是“答错项目”：项目 A 的约束、决策和偏好一旦混进项目 B 的执行上下文，后续推理和长期记忆都会被污染。手动拆分会话能降低风险，但消息归属、会话切换、记忆隔离的负担也随之压到用户身上。

Isola 要解决的，正是多项目场景下的项目归属与记忆边界：用户只用一个自然入口，判定、隔离、纠正、沉淀都交给系统在项目级别完成。

| 使用方式 | 项目归属 | 执行上下文 | 记忆边界 | 纠错成本 |
|---|---|---|---|---|
| 没有 Isola | 依赖用户逐条判断 | 手动切换会话或窗口 | 容易因复制、延续和误投递交叉污染 | 错误通常在后续推理中才暴露 |
| 使用 Isola | 由路由层自动判定，必要时确认 | 每个项目对应独立后端会话 | 召回、去重、写入都限定在项目内 | 可纠正归属，让受影响记忆失效 |

## Isola 是什么

Isola 是架在 agent 之前的项目级记忆路由层。每条消息进来，先判定它属于哪个项目，再投递到该项目独立的后端会话，召回和沉淀也只发生在这个项目范围内。归属判错了，纠正回路会把它修回来：受影响的记忆随之失效，原消息重投到正确的项目，错误归属不会继续扩散。

Isola 不替代 OpenClaw、Claude Code、Codex，也不需要你迁移到新框架，更不是又一个执行框架或通用记忆数据库。OpenClaw / Claude Code / Codex 照旧在后端运行；Isola 只接管四件事：入口侧的项目归属、执行侧的会话隔离、纠正时的错误回滚、记忆侧的项目级读写边界。

## 特性

- **自动归属与项目隔离**：三级判定将消息归入所属项目，各项目独立会话，记忆互不可见。
- **软归属与可纠正闭环**：高置信先投递，低置信先确认；归属错了可以纠正，受影响的记忆随之失效。
- **持久可靠的状态推进**：靠 SQLite 单一真相源、状态机和恢复扫描，进程中断后仍能接着处理没走完的流程。
- **harness 无关接入**：内置直连 LLM 与 OpenClaw CLI；其他 agent 后端实现统一适配契约即可接入。

## 快速开始

**交给 agent 自行安装。** 仓库提供机器可读的就绪自检，coding agent 可根据自检结果逐项完成部署：

> 克隆 https://github.com/PluteW/isola ，安装依赖，生成配置，并使 `isola doctor` 全部通过。

**或手动三步：**

```bash
git clone https://github.com/PluteW/isola && cd isola
pip install -e .                                # 安装 Isola + 依赖 PyYAML；提供 isola 命令（任意目录可运行）
isola init && isola doctor
```

编辑 `config.yaml`，将 `harness` 指向 agent 后端（OpenClaw CLI，或 ollama / DeepSeek 等 OpenAI 兼容端点）。此后消息从同一入口进入，并按项目归属分流：

```bash
isola chat --text "支付服务：梳理这次重构的回滚方案"
isola chat --text "数据平台：排查昨晚的同步任务延迟"
# 两条消息分别进入对应项目的独立会话，记忆互不可见
```

## 工作原理

```text
用户 ─▶ Isola ─▶ 后端 agent（OpenClaw / Claude Code / 直连 LLM …）
          │
          ├─ ① 判定归属：引用检测 → 惯性沿用 → 语义判定
          ├─ ② 投递到项目专属会话：session_key=proj:<id>
          └─ ③ 记忆按项目隔离，并支持归属纠正
```

Isola 的主路径分为归属判定、软归属状态机、隔离执行与记忆写回四层。

**1. 三级归属判定：先规则，后模型。** 路由器按成本从低到高处理消息：先检测跨项目引用，识别“参考项目 A 的结构写项目 B”这类场景，并将消息归入当前惯性项目、记录被引用项目；再处理短确认、指代延续等低信号消息，沿用同一会话中的最近项目；最后才调用 LLM 做语义归属。这样既省下判定开销，也免得让模型去推断那些规则本就能处理的消息。

**2. 软归属状态机：先可用，再可纠正。** 高置信消息先进入 `TENTATIVE` 状态并投递到目标项目，随后进入隔离期；隔离期内一旦发现归属错了，可以把原决策置为 `CORRECTED`，取消还没执行的写入任务，让已写入的受污染记忆失效，并把原消息重投到正确项目。隔离期结束后，`tick` 将仍有效的 `TENTATIVE` 决策提交为 `COMMITTED`，再进入记忆写回。低置信消息不会直接投递，而是先生成确认卡，等待用户确认项目后再执行。

**3. 三侧隔离：判定、执行、记忆各自设边界。** 判定侧给每条消息记一个 `project_id`；执行侧用 `session_key=proj:<id>` 调用后端，让每个项目各有一个独立的 harness session；记忆侧的 `recall` 必须带 `project_id`，去重也只在项目内做。两个项目的内容即使完全相同，也不会被全局去重合并、串到一起。

**4. 写入门链：只沉淀应进入项目记忆的内容。** 消息提交后才会进入写入链路：信息量门过滤空消息、纯确认和指代延续；作用域门要求决策已提交且存在明确项目；去重门基于项目内内容哈希避免重复沉淀。实现中还会拦截疑似密钥、凭据和私钥内容，防止敏感信息进入长期记忆。

底层的状态、事件、消息、决策、纠正、写入任务和记忆项，都存在单个 SQLite 真相源里。事件、消息、活跃决策、写入任务、记忆项的幂等，靠唯一约束来保证；恢复扫描会提交到期的未决写入，补上“决策已提交、写入任务却没做完”的中断状态，并重试超时的运行中任务。所以进程重启后，不靠内存里的状态，也能把没走完的流程接着推下去。

## 接入 harness

Isola 为 OpenClaw 这类框架而做，但不锁定任何一个后端。实现 `HarnessAdapter` 的三个方法即可接入任意 agent 执行环境。

| Harness | 状态 |
|---|---|
| 直连 LLM（OpenAI 兼容：ollama / DeepSeek 等） | 内置，已端到端验证 |
| OpenClaw CLI | 内置 |
| Claude Code / Codex / 其他 | 实现 `ensure_session` / `dispatch` / `reset_session` 即可 |

适配契约：

| 方法 | 责任 |
|---|---|
| `ensure_session(project_id, session_key)` | 为项目准备或复用后端会话 |
| `dispatch(session_key, message, meta)` | 将消息投递到项目专属会话，并返回执行结果 |
| `reset_session(session_key)` | 在需要隔离重置时清理指定项目会话 |

`dispatch` 返回 `{ok, reply, turn_id, meta}`。其中 `ok` 表示后端是否成功处理，`reply` 为后端回复，`turn_id` 用于追踪后端轮次，`meta` 保留后端原生诊断信息。

> **OpenClaw 不在 PATH？** 若 OpenClaw 以 `.mjs` 源码部署、无全局 `openclaw` 命令，用 `isola doctor --openclaw-dir <目录>` 探测，再 `--emit-wrapper` 生成 wrapper（`scripts/openclaw-bin`）并把 `harness.binary` 指过去；`node` 不在 PATH（如 conda）时用 `--node-path` 指定。

## 范围与路线

当前 v1 提供可直接落地的 CLI 同步档：单机配置、项目级路由、独立 harness 会话、持久化状态、记忆隔离、纠正闭环与机器可读自检。该形态适合本地 agent 工作流，也为后续服务化形态提供稳定的状态与适配基础。

serve 常驻、HTTP 入站、多用户与鉴权、飞书 / Slack 等 IM 适配在路线图中。后续演进将保持同一核心边界：Isola 负责项目归属、隔离、纠正和记忆边界，执行能力继续交给用户选择的 agent 后端。

## 许可

Apache-2.0
