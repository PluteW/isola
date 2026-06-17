"""IsolaCore（Facade，SDD v2 §2/§3）：装配真相源+判定+状态机+adapter。

handle_message 主路径 / handle_confirm 低置信确认 / handle_correction 纠正回路 / tick 轮询恢复。
时序遵循 SDD §3：事件幂等 → 消息落库 → 判定 → 先落 decision → 副作用 → 入写入队列。

v0.1 失败处理边界：
  - 输入防御：parse 返回 None / 缺字段 / 未知项目 → 返回错误状态，不炸穿；
  - dispatch 失败（session 未污染）→ 删半落库 decision 释放单活跃锁，可重发；
  - 状态如实：先落 DISP_PENDING，dispatch 成功才 DISP_SENT，失败 DISP_FAILED；
  - confirm dispatch 失败 → 回退 WAITING_CONFIRMATION，可重新确认；
  - correction 幂等：靠状态机（CORRECTED 终态，重放第二次 set 失败 → invalid）。
  v0.2 待办（已记 SDD 开放问题）：dispatch 成功后 card/enqueue 失败的自动补偿恢复、
  callback 平台级幂等、完整 DISPATCHED→CARD_SENT 多态。
"""
from __future__ import annotations
import time
import uuid
from .models import (
    RouteDecision, WAITING_CONFIRMATION, TENTATIVE, CORRECTED,
    DISP_SENT, DISP_PENDING, DISP_FAILED,
)


def _did() -> str:
    return uuid.uuid4().hex


class IsolaCore:
    def __init__(self, store, registry, router, channel, harness,
                 isolation_s: float = 300, now_fn=time.time):
        self.store = store
        self.registry = registry
        self.router = router
        self.channel = channel
        self.harness = harness
        self.isolation_s = isolation_s
        self.now_fn = now_fn
        self.inertia: dict[str, int] = {}     # chat_id -> last project_id（v0.1 内存，重启丢，已知限制）

    def handle_message(self, raw_event, now: float | None = None) -> dict:
        now = now if now is not None else self.now_fn()
        msg = self.channel.parse_inbound(raw_event)
        if msg is None:
            return {"status": "ignored"}
        if not self.store.record_event(msg.event_id, msg.platform_msg_id, "msg", now=now):
            return {"status": "duplicate"}                 # 事件幂等（T-INV-1）
        msg_id = self.store.upsert_message(msg, now=now)

        projects = self.registry.active_projects()
        last_pid = self.inertia.get(msg.chat_id)
        rd = self.router.route(msg.text, projects, last_pid)
        decision_id = _did()
        low = (rd.confidence == "low") or (rd.project_id is None)

        if low:
            self.store.insert_decision(RouteDecision(
                decision_id=decision_id, msg_id=msg_id, route_type=rd.route,
                project_id=rd.project_id, referenced_id=rd.referenced_id,
                confidence_score=0.4, needs_confirmation=True, judge_raw=rd.judge_raw,
                state=WAITING_CONFIRMATION, dispatch_state=DISP_PENDING), now=now)
            try:
                card = self.channel.send_confirm_card(msg.chat_id, msg.text, projects, decision_id)
                self.store.update_dispatch(decision_id, card_msg_id=card)
            except Exception as e:                          # 确认卡发送失败：标记，不静默炸穿
                self.store.update_dispatch(decision_id, dispatch_state=DISP_FAILED)
                return {"status": "confirm_card_failed", "decision_id": decision_id, "error": str(e)}
            return {"status": "awaiting_confirmation", "decision_id": decision_id}

        # 高置信：先落 decision(TENTATIVE, DISP_PENDING)，再 dispatch（满足 record 先于副作用）
        self.store.insert_decision(RouteDecision(
            decision_id=decision_id, msg_id=msg_id, route_type=rd.route,
            project_id=rd.project_id, referenced_id=rd.referenced_id,
            confidence_score=0.9, judge_raw=rd.judge_raw,
            state=TENTATIVE, dispatch_state=DISP_PENDING), now=now)
        reply = self._dispatch_and_card(decision_id, rd.project_id, msg.text, msg.chat_id,
                                        referenced_id=rd.referenced_id)
        if reply is None:                                   # dispatch 失败（session 未污染）→ 删除可重发
            self.store.delete_decision(decision_id)
            return {"status": "dispatch_failed", "project_id": rd.project_id}
        self.store.enqueue_write(decision_id, msg_id, due_at=now + self.isolation_s, now=now)
        self.inertia[msg.chat_id] = rd.project_id
        return {"status": "dispatched", "decision_id": decision_id,
                "project_id": rd.project_id, "referenced_id": rd.referenced_id, "reply": reply}

    def handle_confirm(self, decision_id: str, confirmed_pid: int, now: float | None = None) -> dict:
        now = now if now is not None else self.now_fn()
        d = self.store.get_decision(decision_id)
        if d is None or d["state"] != WAITING_CONFIRMATION:
            return {"status": "invalid"}
        if self.registry.get(confirmed_pid) is None:
            return {"status": "unknown_project", "project_id": confirmed_pid}
        self.store.set_decision_project(decision_id, confirmed_pid)
        if not self.store.set_decision_state(decision_id, TENTATIVE, now=now):
            return {"status": "invalid"}
        self.store.update_dispatch(decision_id, dispatch_state=DISP_PENDING)
        msg = self.store.get_message(d["msg_id"])
        reply = self._dispatch_and_card(decision_id, confirmed_pid, msg["text"], msg["chat_id"])
        if reply is None:                                   # dispatch 失败 → 回退 WAITING，可重新确认
            self.store.set_decision_state(decision_id, WAITING_CONFIRMATION, now=now)
            self.store.update_dispatch(decision_id, dispatch_state=DISP_PENDING)
            return {"status": "dispatch_failed", "decision_id": decision_id}
        self.store.enqueue_write(decision_id, d["msg_id"], due_at=now + self.isolation_s, now=now)
        self.inertia[msg["chat_id"]] = confirmed_pid
        return {"status": "dispatched", "decision_id": decision_id,
                "project_id": confirmed_pid, "reply": reply}

    def handle_correction(self, raw_callback, now: float | None = None) -> dict:
        """旧→CORRECTED + 记录 + 取消写任务 + retire 已写记忆 + 禁原卡 + 重投正确 session。"""
        now = now if now is not None else self.now_fn()
        corr = self.channel.parse_correction(raw_callback)
        if not corr or "decision_id" not in corr or "to_pid" not in corr:
            return {"status": "bad_request"}               # 脏 callback 防御
        d = self.store.get_decision(corr["decision_id"])
        if d is None:
            return {"status": "not_found"}
        to_pid = corr["to_pid"]
        if self.registry.get(to_pid) is None:
            return {"status": "unknown_project", "project_id": to_pid}
        from_pid = d["project_id"]
        if not self.store.set_decision_state(corr["decision_id"], CORRECTED, now=now):
            return {"status": "invalid"}                   # 幂等：重放第二次 set 失败（CORRECTED 终态）
        self.store.insert_correction(corr["decision_id"], from_pid, to_pid,
                                     corr.get("user_id", "?"), d["card_msg_id"], now=now)
        self.store.cancel_write_job(corr["decision_id"])
        self.store.retire(decision_id=corr["decision_id"], now=now)
        if d["card_msg_id"]:
            try:
                self.channel.update_card(d["card_msg_id"], "corrected")
            except Exception:
                pass                                        # 禁原卡失败不阻断重投（v0.1）
        msg = self.store.get_message(d["msg_id"])
        new_id = _did()
        self.store.insert_decision(RouteDecision(
            decision_id=new_id, msg_id=d["msg_id"], route_type="correction",
            project_id=to_pid, confidence_score=1.0, state=TENTATIVE,
            dispatch_state=DISP_PENDING, parent_decision_id=corr["decision_id"]), now=now)
        reply = self._dispatch_and_card(new_id, to_pid, msg["text"], msg["chat_id"])
        if reply is None:                                   # 重投 dispatch 失败：删新的（旧已 CORRECTED，用户可重发）
            self.store.delete_decision(new_id)
            return {"status": "corrected_dispatch_failed", "from": from_pid, "to": to_pid}
        self.store.enqueue_write(new_id, d["msg_id"], due_at=now + self.isolation_s, now=now)
        self.inertia[msg["chat_id"]] = to_pid
        return {"status": "corrected", "new_decision_id": new_id, "from": from_pid, "to": to_pid}

    def tick(self, now: float | None = None) -> dict:
        """轮询器一拍 = 恢复扫描（隔离期到期提交 + 崩溃补偿）。生产由外部循环调用。"""
        return self.store.recovery_scan(now=now if now is not None else self.now_fn())

    def _dispatch_and_card(self, decision_id, pid, text, chat_id, referenced_id=None):
        """dispatch + 发作用域卡片。dispatch 失败返回 None（session 未污染）；
        dispatch 成功后即使发卡失败也返回 reply（dispatch 已生效，v0.1 仅缺合流回复）。"""
        proj = self.registry.get(pid)
        if proj is None:
            self.store.update_dispatch(decision_id, dispatch_state=DISP_FAILED)
            return None
        label = proj["name"]
        if referenced_id:                                   # cross_ref：仅标注引用来源，不注入（v0.1）
            ref = self.registry.get(referenced_id)
            label += f"（引用了《{ref['name']}》）" if ref else "（含跨项目引用）"
        try:
            res = self.harness.dispatch(f"proj:{pid}", text, idempotency_key=decision_id)
            if not isinstance(res, dict) or not res.get("ok"):
                raise RuntimeError(f"harness 返回异常: {res!r}")
        except Exception:
            self.store.update_dispatch(decision_id, dispatch_state=DISP_FAILED)
            return None                                     # dispatch 未生效，session 未污染
        reply = res.get("reply", "")
        self.store.update_dispatch(decision_id, dispatch_state=DISP_SENT)
        try:
            card = self.channel.send_scoped_card(chat_id, reply, label, decision_id)
            self.store.update_dispatch(decision_id, card_msg_id=card)
        except Exception:
            pass                                            # 发卡失败不回滚 dispatch（v0.2 补自动补发）
        return reply
