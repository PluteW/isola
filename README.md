<div align="center">
<img src="branding/logo.svg" width="88" height="88" alt="Isola">

# Isola

**Project-scoped memory for agent frameworks**
<br><sub>Built for OpenClaw, Claude Code, and Codex.</sub>
<br><sub>English | [简体中文](README.zh-CN.md)</sub>
<br><sub><a href="#why-isola">Why Isola</a> · <a href="#what-isola-is">What Isola Is</a> · <a href="#features">Features</a> · <a href="#quick-start">Quick Start</a> · <a href="#how-it-works">How It Works</a> · <a href="#harness-integration">Harness Integration</a> · <a href="#status-and-roadmap">Status and Roadmap</a> · <a href="#license">License</a></sub>

`Apache-2.0`

</div>

---

## Why Isola

When Claude Code, OpenClaw, Codex, or similar agent environments are used across several projects, the work often still flows through one entry conversation or a cluster of nearby sessions. The risk is not only sending a request to the wrong project. Once project A's constraints, decisions, and preferences enter project B's execution context, later reasoning and long-term memory can be contaminated. Splitting sessions by hand reduces that risk, but it pushes attribution, session switching, and memory isolation back onto the user.

Isola addresses project attribution and memory boundaries in multi-project agent work: the user keeps one natural entry point, while the system handles attribution, isolation, correction, and memory writeback at project scope.

| Usage | Project attribution | Execution context | Memory boundary | Correction cost |
|---|---|---|---|---|
| Without Isola | Judged manually message by message | Manually switched sessions or windows | Easily cross-contaminated by copying, continuation, and misdispatch | Errors often surface only in later reasoning |
| With Isola | Routed automatically, with confirmation when needed | One isolated backend session per project | Recall, deduplication, and writes stay inside the project | Attribution can be corrected, and affected memories can be retired |

## What Isola Is

Isola is a project-scoped memory routing layer placed in front of an agent. Each incoming message is attributed to a project first, then dispatched to that project's isolated backend session. Memory is recalled and written only within the attributed project. If attribution is wrong, the correction loop can fix the decision; affected memories are retired, and the original message is redispatched to the correct project so the error does not keep spreading.

Isola does not replace OpenClaw, Claude Code, or Codex, and it does not require migration to a new agent framework. It is not a new execution framework or a general-purpose memory database. OpenClaw, Claude Code, Codex, or another agent backend still performs the work; Isola owns project attribution at the entry side, session isolation at the execution side, rollback on correction, and project-scoped read/write boundaries for memory.

Isola supports two integration modes over one shared core. In **delegated mode**, Isola fronts the conversation and dispatches each attributed message to that project's backend session — the form described above. In **memory-service mode**, the agent is itself the entry point (Claude Code, Codex): it calls Isola over MCP to attribute a message (`route`), recall that project's memory (`recall`), runs the work itself, and writes the result back (`remember`). In memory-service mode Isola never dispatches and never touches a backend — it is purely the project-scoped attribution and memory layer. Attribution, soft assignment, correction, and the writeback gates are the same code in both modes.

## Features

- **Automatic attribution and project isolation**: a three-stage attribution path assigns messages to projects; each project has its own backend session, and memories are not visible across projects.
- **Soft assignment with a correction loop**: high-confidence messages are dispatched first, low-confidence messages require confirmation; wrong attribution can be corrected, and affected memories are retired.
- **Durable state progression**: a SQLite source of truth, state machine, and recovery scan keep pending flows moving after process interruption.
- **Harness-neutral integration**: direct LLM and OpenClaw CLI backends are built in; other agent backends can plug in through the same harness adapter contract.
- **Two integration modes, one core**: delegated mode (Isola fronts and dispatches to a backend) and memory-service mode (an agent such as Claude Code or Codex calls Isola over MCP and executes itself). Both reuse the same attribution, isolation, correction, and memory code.

## Quick Start

**Let an agent install it.** The repository includes a machine-readable readiness check, so a coding agent can complete deployment step by step from the check results:

> Clone https://github.com/PluteW/isola, install dependencies, generate the configuration, and make `isola doctor` pass completely.

**Or run the three manual steps:**

```bash
git clone https://github.com/PluteW/isola && cd isola
pip install -e .                                # installs Isola + PyYAML; adds the isola command (any directory)
isola init && isola doctor
```

Edit `config.yaml` and point `harness` to the agent backend: OpenClaw CLI, or an OpenAI-compatible endpoint such as ollama or DeepSeek. After that, messages can enter through the same doorway and be routed by project attribution:

```bash
isola chat --text "Payment service: outline the rollback plan for this refactor"
isola chat --text "Data platform: investigate last night's sync job latency"
# The two messages enter separate project sessions, and their memories are isolated.
```

**Memory-service mode (MCP).** For agents that are themselves the entry point — Claude Code, Codex — run Isola as an MCP server instead of a fronting dispatcher. The agent calls `route` → `recall` → (executes the work itself) → `remember`; Isola never dispatches.

```bash
pip install -e ".[mcp]"             # adds the official mcp SDK and the isola-mcp command
isola-mcp --config config.yaml      # stdio MCP server
```

Point your agent's MCP client at the `isola-mcp` command (stdio). It exposes five tools: `isola_route`, `isola_recall`, `isola_confirm`, `isola_remember`, `isola_correct`. `isola_recall` only takes a `decision_id`, so a project's memory cannot be read without first attributing the message (and confirming it, when attribution is low-confidence).

## How It Works

```text
User ─▶ Isola ─▶ Backend agent (OpenClaw / Claude Code / direct LLM ...)
          │
          ├─ ① Attribute: reference detection → inertia reuse → semantic judgment
          ├─ ② Dispatch to project session: session_key=proj:<id>
          └─ ③ Keep memory project-scoped, with attribution correction
```

Isola's main path has four layers: attribution, soft assignment state management, isolated execution, and memory writeback.

**1. Three-stage attribution: rules first, model second.** The router processes messages from low cost to high cost. It first detects cross-project references, such as "use project A's structure while writing project B", attributes the message to the current inertia project, and records the referenced project. It then handles short confirmations, pronoun continuations, and other low-signal messages by reusing the latest project in the same conversation. Only after those stages does it call an LLM for semantic attribution. This lowers judgment cost and avoids asking the model to infer cases that rules can already handle cleanly.

**2. Soft assignment state machine: usable first, correctable afterward.** High-confidence messages enter `TENTATIVE`, are dispatched to the target project, and then remain in an isolation window. If attribution is found to be wrong during that window, the original decision can become `CORRECTED`; pending write tasks are canceled, polluted memories already written are retired, and the original message is redispatched to the correct project. When the isolation window expires, `tick` commits still-valid `TENTATIVE` decisions as `COMMITTED`, then moves them into memory writeback. Low-confidence messages are not dispatched directly; Isola creates a confirmation card and waits for the user to choose the project.

**3. Three-sided isolation: attribution, execution, and memory each have their own boundary.** On the attribution side, every message is stored with a `project_id`. On the execution side, the backend is called with `session_key=proj:<id>`, giving each project its own harness session. On the memory side, `recall` must include `project_id`, and content deduplication happens only inside that project. Even identical cross-project content is not shared through global deduplication.

**4. Writeback gate chain: only durable project memory is written.** After a message is committed, it enters the writeback chain. The information gate filters empty messages, pure confirmations, and referential continuations. The scope gate requires a committed decision and an explicit project. The deduplication gate uses an in-project content hash to prevent repeated writes. The implementation also blocks likely API keys, credentials, and private keys so sensitive material does not enter long-term memory.

All underlying state, events, messages, decisions, corrections, write tasks, and memory items are stored in one SQLite source of truth. Unique constraints provide idempotency for events, messages, active decisions, write tasks, and memory items. The recovery scan commits expired pending writes, compensates for interrupted states where a decision was committed but its write task did not finish, and retries timed-out running tasks. After a process restart, unfinished flows continue without relying on in-memory state.

## Harness Integration

Isola is built for frameworks such as OpenClaw, but it does not lock into any one backend. Implement the three `HarnessAdapter` methods to connect any agent execution environment.

| Harness | Status |
|---|---|
| Direct LLM (OpenAI-compatible: ollama / DeepSeek, etc.) | Built in, end-to-end verified |
| OpenClaw CLI | Built in |
| Claude Code / Codex / others | Implement `ensure_session` / `dispatch` / `reset_session` |

Adapter contract:

| Method | Responsibility |
|---|---|
| `ensure_session(project_id, session_key)` | Prepare or reuse a backend session for the project |
| `dispatch(session_key, message, meta)` | Send a message to the project session and return the execution result |
| `reset_session(session_key)` | Clear the specified project session when isolation reset is needed |

`dispatch` returns `{ok, reply, turn_id, meta}`. `ok` reports whether the backend handled the message successfully, `reply` contains the backend response, `turn_id` tracks the backend turn, and `meta` preserves backend-native diagnostics.

> **OpenClaw not on PATH?** If OpenClaw ships as `.mjs` source with no global `openclaw` binary, run `isola doctor --openclaw-dir <dir>` to locate it, then `--emit-wrapper` to generate a launcher (`scripts/openclaw-bin`) and point `harness.binary` at it. Use `--node-path` when `node` isn't on PATH (e.g. conda).

## Status and Roadmap

The current release provides two usable forms. A CLI sync workflow (delegated mode): single-machine configuration, project-scoped routing, isolated harness sessions, durable state, memory isolation, a correction loop, and machine-readable readiness checks. And an MCP memory-service mode (`isola-mcp`, stdio): attribution, recall, confirmation, writeback, and correction exposed as tools for agents that execute themselves. Both share one core boundary and the state foundation needed for later service deployments.

Persistent serve mode, HTTP inbound traffic, multi-user support and authentication, and IM integrations such as Feishu and Slack are on the roadmap. Future work keeps the same core boundary: Isola owns project attribution, isolation, correction, and memory boundaries; execution remains delegated to the agent backend chosen by the user.

## License

Apache-2.0
