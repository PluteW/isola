# MemWeave 架构概览

> 面向使用者与贡献者：讲清「是什么、怎么流转、怎么接」。实现细节以代码为准。

## 分层

```
用户 ─单入口─▶ ChannelAdapter ─▶ Core (Facade) ─▶ HarnessAdapter ─▶ agent 框架 / LLM
                                   ├─ Router  判定层
                                   ├─ Store   SQLite 真相源
                                   └─ 写入门链
```

- **ChannelAdapter** —— 入站（外部事件 → 标准 `InboundMessage`）+ 出站（作用域卡片 / 确认卡）。内置 `CLIChannel`。
- **Core（Facade）** —— 装配与编排：`handle_message` / `handle_confirm` / `handle_correction` / `tick`。进程无核心状态。
- **Router 判定层** —— 三级归属：① 引用检测（跨项目引用 → 归当前项目并标注来源）→ ② 惯性规则（低信号消息延续最近项目）→ ③ LLM 判定（语义归属）。每类消息配匹配的处理器，而非一个模型通吃。
- **Store（SQLite）** —— 系统**唯一真相源**。6 张表（事件 / 消息 / 决策 / 纠正 / 写入任务 / 记忆），靠唯一约束保证幂等，DB 字段表达状态机，崩溃后靠恢复扫描重建——进程重启不丢未决状态。
- **HarnessAdapter** —— 把归属后的消息投给 agent 框架执行。`session_key = proj:<id>` 实现项目隔离。内置「直连 LLM」「OpenClaw CLI」两种。
- **写入门链** —— 消息提交后经 信息量门 → 作用域门 → 去重门 才落项目记忆。

## 数据流（主路径）

1. 消息入站 → 事件幂等去重 → 落库
2. Router 判定归属项目
3. **高置信**：先落 `decision`（TENTATIVE）→ dispatch 给 harness（`session_key=proj:<id>`）→ 隔离期后 `tick` 提交（COMMITTED）→ 写入门链落记忆
4. **低置信**：出确认卡，等用户确认后再 dispatch（牺牲即时性换不污染）
5. **归错**：一键纠正 → CORRECTED + 退役错误记忆 + 重投到对的项目

## 隔离怎么实现

- **判定侧**：每条消息归到一个 `project_id`。
- **执行侧**：`session_key=proj:<id>`，每个项目一个 harness session，历史互不可见。
- **记忆侧**：`recall` 强制带 `project_id`；去重只在项目内；跨项目相同内容各自保留（隔离优先于去重）。

## 软归属闭环

不强迫用户预先分类——**先投递，错了一键改**。高置信先投递、隔离期内可纠正；纠正即退役错误记忆（阻断未来污染）。这降低了「预先决定每条消息属于哪个项目」的认知负担。

## 接入新 harness / channel

实现对应 Adapter 契约即可，契约一致性由测试守护。`HarnessAdapter.dispatch` 的返回结构与两条硬约束（幂等、失败不污染 session）见 [AGENTS.md](AGENTS.md) 与 [README](README.md#接入-harness)。

## 运行形态

- **v1**：CLI 同步档（`memweave chat`，`clone` 即用，一次处理一条）。
- **路线图**：serve 常驻（可选性能档，须满足崩溃恢复 / 并发 / 可靠投递的稳健性要求才开放）、HTTP / IM 入站、跨项目审核式复用。

## 依赖与运行

- Python ≥ 3.9，唯一第三方依赖 PyYAML，其余标准库。
- SQLite 真相源默认本地文件；判定器 / 执行后端走可配置的 OpenAI 兼容端点或 OpenClaw CLI。
