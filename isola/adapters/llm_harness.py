"""LLMHarness：最小真实 HarnessAdapter——每 session_key 独立对话历史，dispatch 调真 LLM。

session 隔离 = 历史按 session_key（proj:<id>）分桶。这正是 Isola 要保证的"项目隔离"
在 harness 侧的下游体现：归属判对 → 消息进对的 session → 该项目的回复只看到该项目历史。
SDD §4 HarnessAdapter 契约（含 idempotency_key / reset_session / dispatch 返回 dict）。
OpenClawAdapter 是同契约的另一实现（之后做，验证 harness 无关）。
"""
from __future__ import annotations
import json
import urllib.request


class LLMHarness:
    def __init__(self, base_url, model, api_key, timeout=120):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.sessions: dict[str, list] = {}   # session_key -> [messages]
        self._seen: dict[str, dict] = {}        # idempotency_key -> result

    def ensure_session(self, session_key, role=""):
        self.sessions.setdefault(session_key, [])

    def reset_session(self, session_key):
        self.sessions[session_key] = []

    def dispatch(self, session_key, message, idempotency_key, timeout_s=120):
        if idempotency_key in self._seen:
            return self._seen[idempotency_key]           # 幂等：同 key 不重投
        hist = self.sessions.setdefault(session_key, [])
        hist.append({"role": "user", "content": message})
        try:
            reply = self._chat(hist)
        except Exception as e:
            hist.pop()                                   # 失败回滚本轮 user，session 未污染
            return {"ok": False, "error": f"{type(e).__name__}:{e}"}
        hist.append({"role": "assistant", "content": reply})
        res = {"ok": True, "reply": reply, "turn_id": f"{session_key}#{len(hist)//2}", "meta": {}}
        self._seen[idempotency_key] = res
        return res

    def _chat(self, messages):
        sysm = [{"role": "system", "content": "你是项目助手，只基于本对话历史回答，简洁（1-2句）。"}]
        payload = json.dumps({
            "model": self.model, "messages": sysm + messages[-10:],
            "temperature": 0.3, "max_tokens": 200}).encode()
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r)["choices"][0]["message"]["content"].strip()
