"""测试替身（fake adapter / judge）——离线集成测试用，不依赖真飞书/OpenClaw/模型。
对应 SDD §4 的 ChannelAdapter / HarnessAdapter / Judge 契约。"""
from __future__ import annotations
from memweave.models import InboundMessage


class FakeChannel:
    """记录所有出站卡片，便于断言作用域标记/确认卡/dispatch 次数。"""
    def __init__(self):
        self.cards = []           # 作用域卡片
        self.confirm_cards = []   # 确认卡
        self.updated = []         # update_card 调用
        self._n = 0

    def parse_inbound(self, raw):
        return raw if isinstance(raw, InboundMessage) else InboundMessage(**raw)

    def send_scoped_card(self, chat_id, text, scope_label, decision_id):
        self._n += 1
        cid = f"card{self._n}"
        self.cards.append({"chat_id": chat_id, "text": text, "label": scope_label,
                           "decision_id": decision_id, "card_id": cid})
        return cid

    def send_confirm_card(self, chat_id, text, projects, decision_id):
        self._n += 1
        cid = f"confirm{self._n}"
        self.confirm_cards.append({"chat_id": chat_id, "decision_id": decision_id, "card_id": cid})
        return cid

    def update_card(self, card_msg_id, new_state):
        self.updated.append({"card_msg_id": card_msg_id, "new_state": new_state})

    def parse_correction(self, raw):
        return raw


class FakeHarness:
    """记录 dispatch；同 idempotency_key 不重投（验证 T-INV-6 风格幂等）。
    fail=True 时 dispatch 抛异常（测试 dispatch 失败路径）。"""
    def __init__(self, fail=False):
        self.dispatches = []
        self._seen = {}
        self.fail = fail

    def ensure_session(self, session_key, role):
        pass

    def dispatch(self, session_key, message, idempotency_key, timeout_s=120):
        if self.fail:
            raise RuntimeError("fake harness dispatch failure")
        if idempotency_key in self._seen:
            return self._seen[idempotency_key]      # 幂等：同 key 返回首次结果，不重投
        self.dispatches.append({"session_key": session_key, "message": message,
                                "key": idempotency_key})
        res = {"ok": True, "reply": f"[{session_key}] 已处理：{message[:16]}",
               "turn_id": f"t{len(self.dispatches)}", "meta": {}}
        self._seen[idempotency_key] = res
        return res

    def reset_session(self, session_key):
        pass


class FakeJudge:
    """ret_pid 控制 router 走向：>0=高置信归该项目；0=新项目(低置信)；None=无法判断(低置信)。"""
    def __init__(self, ret_pid=1):
        self.ret_pid = ret_pid
        self.calls = 0

    def attribute(self, text, projects, history):
        self.calls += 1
        return self.ret_pid, f"fake:{self.ret_pid}"
