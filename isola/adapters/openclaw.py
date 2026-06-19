"""OpenClawAdapter：第二个真实 HarnessAdapter（验证 形态决策 的"harness 无关"）。

通过 OpenClaw CLI 投递：`openclaw agent --local --agent <role> --session-key proj:<id> --json`。
session_key=proj:<id> 经 --agent 作用域拼成 OpenClaw 的 agent:<role>:proj:<id>
（即 设计决策 的"角色=agent、项目=session key 后缀"映射）。

实现要点（对照 SDD §4 + 可行性点检风险清单）：
  - runner 可注入（默认 subprocess.run），便于离线单测；
  - OpenClaw --json 前常有 [diagnostic] 诊断行 → 健壮提取最后一个 JSON 对象；
  - 检查 meta.fallbackFrom 防 gateway-fallback 历史分叉（风险#4）；
  - idempotency_key 缓存防重投。
记忆立场（双记忆保留，铁律）：Isola 与 OpenClaw 各自保留记忆、并存；Isola 不接管、不关闭
  OpenClaw 的 memory/session。本类只负责投递，绝不触碰 OpenClaw 自带记忆与 session。
"""
from __future__ import annotations
import json
import subprocess


def _default_runner(cmd, timeout, env):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


def _last_json(text: str):
    """从混合输出（诊断行 + JSON）中提取最后一个可解析的 JSON 对象。"""
    obj = None
    # 整体先试
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    for line in text.splitlines():
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
    return obj


class OpenClawAdapter:
    def __init__(self, binary: str, agent: str = "main", model: str | None = None,
                 timeout: int = 600, env: dict | None = None, runner=_default_runner):
        self.binary = binary
        self.agent = agent
        self.model = model
        self.timeout = timeout
        self.env = env
        self.runner = runner
        self._seen: dict[str, dict] = {}

    def ensure_session(self, session_key: str, role: str = ""):
        pass  # OpenClaw session 由 --session-key 惰性创建，无需预建

    def reset_session(self, session_key: str):
        # OpenClaw 无直接 reset API；生产可改用新 session-key 后缀或 /reset 指令。
        # v0.1 记为已知限制（路线图 自带记忆/会话补偿待 v0.2）。
        pass

    def dispatch(self, session_key: str, message: str, *, idempotency_key: str,
                 timeout_s: int | None = None) -> dict:
        if idempotency_key in self._seen:
            return self._seen[idempotency_key]            # 幂等：同 key 不重投
        cmd = [self.binary, "agent", "--local", "--agent", self.agent,
               "--session-key", session_key, "--message", message, "--json"]
        if self.model:
            cmd += ["--model", self.model]
        try:
            r = self.runner(cmd, timeout_s or self.timeout, self.env)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "openclaw timeout"}
        if r.returncode != 0:
            return {"ok": False, "error": (r.stderr or r.stdout or "")[-300:]}
        out = _last_json(r.stdout or "")
        if out is None:
            return {"ok": False, "error": "no parsable JSON: " + (r.stdout or "")[-200:]}
        meta = out.get("meta", {}) or {}
        if meta.get("fallbackFrom"):                      # 防 gateway-fallback 历史分叉
            return {"ok": False, "error": f"gateway fallback 分叉: {meta['fallbackFrom']}"}
        texts = [p.get("text", "") for p in out.get("payloads", []) if p.get("text")]
        reply = "\n".join(t for t in texts if t)
        res = {"ok": True, "reply": reply,
               "turn_id": out.get("sessionId") or out.get("runId") or "",
               "meta": meta}
        self._seen[idempotency_key] = res
        return res
