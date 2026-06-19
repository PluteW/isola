# Isola 安装与接入协议（面向 agent / 人）

> v1「CLI 同步档」：`git clone` 后 `pip install -e .` 即用（本地可编辑安装；PyPI 全局发布留后续）。一步一命令，每步可自检。
> 本文与 `isola doctor` **同源**——doctor 的每个检查项对应下面一步；agent 可用 `doctor --json` 逐项判定。

## 前置
- Python ≥ 3.9、git
- 一个 OpenAI 兼容 LLM 端点（判定器；执行后端若用 `llm_direct` 也需要），或 OpenClaw CLI（执行后端若用 `openclaw`）

## 步骤

### 1. 获取
```bash
git clone https://github.com/PluteW/isola && cd isola
```

### 2. 安装（含依赖 PyYAML）
```bash
pip install -e .
```
预期：Isola 与 PyYAML 安装成功，得到 `isola` 命令（任意目录可运行）。失败 → 检查 pip 与 Python 版本（需 ≥3.9）。

### 3. 生成配置
```bash
isola init
```
预期：`已生成 config.yaml`。

### 4. 填配置 + 设密钥
编辑 `config.yaml`：`judge.base_url/model`、`harness`（`llm_direct` 或 `openclaw`）、`projects`（**带稳定 id，从 1 连续**）。
密钥**不写进配置**，用环境变量：
```bash
export JUDGE_API_KEY=...
export HARNESS_API_KEY=...     # 仅 llm_direct 需要
```

### 5. 自检
```bash
isola doctor            # 人类可读
isola doctor --json     # 机器可读：每项 {id,status,check_command,fix_command,requires_human,evidence}
```
预期：`python / pyyaml / config / judge` 全 `pass`。
- 任一 `fail` → 按该项 `fix_command` 处理后重跑。
- `harness` 为 `need_human` → 见下「OpenClaw 接入」。

### 6. 验证端到端
```bash
isola chat --text "查一下项目A的近况"
```
预期：打印 `→ 项目 N [dispatched] ...`，消息被路由到某项目并投递落记忆。

## OpenClaw 接入（人工停点，诚实声明 — 已对真实 CLI 2026.6.5 核对）
`harness.type: openclaw` 时：**Isola 持有用户入口，OpenClaw 退为执行后端**
（`openclaw agent --local --agent <role> --session-key proj:<id> --message <msg> --json`；adapter 命令已与真实 CLI 核对一致）。

**① 可执行入口（binary）**：adapter 需要一个可执行的 `harness.binary`。若 `which openclaw` 为空（OpenClaw 常以源码 `.mjs` 部署、无全局 bin），用 doctor 探测并自动造 wrapper：
```bash
isola doctor --openclaw-dir <你的 OpenClaw 目录>                     # 探测，定位 openclaw.mjs
isola doctor --openclaw-dir <…> [--node-path <node>] --emit-wrapper  # 生成 scripts/openclaw-bin（node 不在 PATH 时用 --node-path，如 conda）
# 再把 config.yaml 的 harness.binary 指向 scripts/openclaw-bin
```

**② 真实卡点（实测确认）**：OpenClaw 有自己的 **model registry**，`--local` 直接 `--model <ollama-model>` 会报 `Unknown model: ...`——必须先在 OpenClaw 侧注册 provider（指向你的 LLM 端点，如本地 ollama 的 OpenAI 兼容 `http://localhost:11437/v1`）+ model。**此步无非交互一键命令**，故 doctor 标 `need_human`：
```bash
# 1) 在 OpenClaw 配置注册 provider(base_url→你的 LLM 端点) + model（见 OpenClaw 文档；交互配置）
# 2) 验证（message 必须用 --message，不是位置参数）：
openclaw agent --local --agent main --session-key proj:1 --message "ping" --json   # 应返回 JSON
```
通过后 Isola 即可用 OpenClaw 作执行后端。**Isola 侧（adapter/契约/config）已就绪，最后一公里是 OpenClaw 自身的 model 注册，非 Isola 缺陷。**

## 不在 v1（experimental / v0.2 路线）
serve 常驻、HTTP 入站、多用户/鉴权、具体 IM（飞书/Slack）适配。serve 档须过「稳健性准入门」方可开放。
