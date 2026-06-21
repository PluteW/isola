"""模式 B：MCP 记忆服务（agent 自执行）的 core facade（SDD v3 §3B）。

模式 A（IsolaCore）持入口 + dispatch 给后端执行；模式 B 把 Isola 当"记忆/归属服务"——
agent（Claude Code/Codex 等本身就是入口）调：route(判定归属) → recall(取该项目记忆)
→ 自己执行 → remember(回写)；纠错走 correct。**无 dispatch、无隔离期、不碰 harness/channel**。

与模式 A 复用同一组 core 原子（router 判定 / store.recall / writeback 门 / 状态机 / retire），
仅编排不同（§3B：无 dispatch 分支、无隔离期 tick）。门槛见 §3B.6：
  ① COMMITTED 插入写 committed_at；② apply_correction_and_retire 真事务；
  ③ remember gated（门在 core，不绕 info/sensitive）；④ remember 结构化结果；
  ⑤ recall 默认只 decision_id（project_id 限 admin）；⑥ confirm 单事务直达 COMMITTED。

统一 typed 返回 {status, ...payload}；不抛异常表业务失败（异常只留编程错误）。
"""
from __future__ import annotations
import time
import uuid
import sqlite3
from .models import (
    RouteDecision, MemoryItem,
    WAITING_CONFIRMATION, COMMITTED, DISP_PENDING,
    DUR_STABLE, DUR_TENTATIVE,
)
from .writeback import info_gate, sensitive_gate, durability as _durability


def _did() -> str:
    return uuid.uuid4().hex


class MemoryService:
    """模式 B facade。依赖 store + registry + router；**无 channel/harness**（不 dispatch）。"""

    def __init__(self, store, registry, router, now_fn=time.time):
        self.store = store
        self.registry = registry
        self.router = router
        self.now_fn = now_fn

    # ---------- §3B.1 route：判定归属 → 落 decision（高置信即 COMMITTED，无 dispatch/隔离期）----------
    def route(self, msg, now: float | None = None) -> dict:
        now = now if now is not None else self.now_fn()
        if not self.store.record_event(msg.event_id, msg.platform_msg_id, "msg", now=now):
            return {"status": "duplicate"}                       # 事件幂等
        msg_id = self.store.upsert_message(msg, now=now)
        projects = self.registry.active_projects()
        rd = self.router.route(msg.text, projects, None)         # 模式 B 无 chat 惯性（无状态请求）
        decision_id = _did()
        low = (rd.confidence == "low") or (rd.project_id is None)
        # 初始态：低置信 WAITING_CONFIRMATION；高置信即 COMMITTED（写 committed_at，§3B.1 + 第⑤章模式B插入态）
        decision = RouteDecision(
            decision_id=decision_id, msg_id=msg_id, route_type=rd.route,
            project_id=rd.project_id, referenced_id=rd.referenced_id,
            confidence_score=0.4 if low else 0.9, needs_confirmation=low, judge_raw=rd.judge_raw,
            state=WAITING_CONFIRMATION if low else COMMITTED, dispatch_state=DISP_PENDING)
        try:
            self.store.insert_decision(decision, now=now)
        except sqlite3.IntegrityError:
            # 同 msg 已有活跃 decision（ux_active_decision 部分唯一索引）：同 platform_msg_id 重投
            # 但 event_id 不同时 record_event 放行、msg 级唯一索引兜底 → 降级 typed duplicate
            # （不抛异常表业务失败，外审 P2 加固）
            return {"status": "duplicate"}
        if low:
            return {"status": "needs_confirmation", "decision_id": decision_id,
                    "candidates": [{"project_id": p["id"], "name": p["name"]} for p in projects]}
        return {"status": "ok", "decision_id": decision_id, "project_id": rd.project_id}

    # ---------- §3B.1b confirm：低置信确认，单事务直达 COMMITTED，不 dispatch ----------
    def confirm(self, decision_id: str, project_id: int, actor_id: str,
                now: float | None = None) -> dict:
        now = now if now is not None else self.now_fn()
        d = self.store.get_decision(decision_id)
        if d is None or d["state"] != WAITING_CONFIRMATION:
            return {"status": "invalid"}
        if self.registry.get(project_id) is None:
            return {"status": "unknown_project", "project_id": project_id}
        if not self.store.confirm_to_committed(decision_id, project_id, now=now):
            return {"status": "invalid"}                         # 并发/状态变更兜底
        return {"status": "ok", "decision_id": decision_id, "project_id": project_id}

    # ---------- §3B.2 recall：仅 active；默认只 decision_id（project_id 限 admin，防绕过 confirm）----------
    def recall(self, decision_id: str | None = None, project_id: int | None = None,
               query: str = "", k: int = 5, allow_project: bool = False) -> dict:
        if decision_id is not None:
            d = self.store.get_decision(decision_id)
            if d is None or d["state"] != COMMITTED or d["project_id"] is None:
                return {"status": "invalid"}                     # 须 active COMMITTED（重 route / 先 confirm）
            pid = d["project_id"]
        elif project_id is not None:
            if not allow_project:                                # 门槛⑤：project_id recall 限 admin
                return {"status": "invalid"}
            pid = project_id
        else:
            return {"status": "invalid"}
        items = self.store.recall({"level": "project", "project_id": pid}, query=query, k=k)
        return {"status": "ok", "items": [
            {"content": r["content"], "durability": r["durability"], "hash": r["content_hash"]}
            for r in items]}

    # ---------- §3B.3 remember：core gated wrapper（门在 core）+ 结构化结果 + 一 decision 一记忆 ----------
    def remember(self, decision_id: str, content: str, durability: str | None = None,
                 now: float | None = None) -> dict:
        now = now if now is not None else self.now_fn()
        d = self.store.get_decision(decision_id)
        if d is None or d["state"] != COMMITTED or d["project_id"] is None:
            return {"status": "invalid"}
        if not info_gate(content) or sensitive_gate(content):    # 门槛③：门在 core，不绕
            return {"status": "skipped"}
        dur = durability if durability in (DUR_STABLE, DUR_TENTATIVE) else _durability(content)
        res = self.store.remember_for_decision(MemoryItem(
            scope_level="project", project_id=d["project_id"], content=content,
            source_msg_id=d["msg_id"], decision_id=decision_id,
            content_hash="", durability=dur), now=now)
        if res["result"] == "invalid":                          # 与 correct 并发落败（store 内事务 re-check）
            return {"status": "invalid"}
        if res["result"] == "written":
            return {"status": "written", "item_id": res["item_id"]}
        return {"status": "duplicate", "kind": res["kind"], "item_id": res["item_id"]}

    # ---------- §3B.4 correct：共享原子 apply_correction_and_retire；模式 B 不重投 ----------
    def correct(self, decision_id: str, to_pid: int, actor_id: str,
                correction_event_id: str | None = None, now: float | None = None) -> dict:
        now = now if now is not None else self.now_fn()
        d = self.store.get_decision(decision_id)
        if d is None:
            return {"status": "not_found"}
        if self.registry.get(to_pid) is None:
            return {"status": "unknown_project", "project_id": to_pid}
        if correction_event_id is not None:                     # 提供则走 events 幂等（重放→duplicate）
            if not self.store.record_event(correction_event_id, decision_id, "correction", now=now):
                return {"status": "duplicate"}
        from_pid = d["project_id"]
        if not self.store.apply_correction_and_retire(
                decision_id, from_pid, to_pid, actor_id, d["card_msg_id"], now=now):
            return {"status": "invalid"}                         # CORRECTED 终态重放（无 event_id 时退化为状态机判定）
        return {"status": "corrected", "to_pid": to_pid, "from": from_pid}
