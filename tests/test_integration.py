"""集成测试（fake adapter）——守护 SDD §3 主路径与确认 / test-plan T-INT-1, T-INT-2。"""
import sys
import pathlib
import tempfile
import os
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent))

from isola.store import Store
from isola.registry import Registry
from isola.router import Router
from isola.core import IsolaCore
from isola.models import (
    InboundMessage, WAITING_CONFIRMATION, TENTATIVE, COMMITTED, CORRECTED, DISP_SENT, DISP_PENDING,
)
from fakes import FakeChannel, FakeHarness, FakeJudge

T0 = 1_000_000.0


def _build(judge_pid):
    store = Store()
    reg_path = tempfile.mktemp(suffix=".yaml")
    reg = Registry(reg_path)
    reg.add("甲公司尽调", "公司级尽调：财务/客户/订单")   # pid 1
    reg.add("乙公司尽调", "公司级尽调：主营/订单/风电")   # pid 2
    reg.add("AI技术周报", "每周 AI 动态整理")              # pid 3（T-INT-4 连续纠正用）
    ch, hn = FakeChannel(), FakeHarness()
    core = IsolaCore(store, reg, Router(FakeJudge(judge_pid)), ch, hn,
                        isolation_s=300, now_fn=lambda: T0)
    return core, store, ch, hn, reg_path


def _msg(text, eid="e1", pmid="pm1", chat="c1"):
    return InboundMessage(msg_id="m_"+eid, event_id=eid, platform_msg_id=pmid,
                          user_id="u1", text=text, chat_id=chat, platform_ts=int(T0))


def test_int1_high_confidence_main_path():
    core, store, ch, hn, p = _build(judge_pid=1)
    try:
        r = core.handle_message(_msg("查一下甲公司近三年毛利率"), now=T0)
        assert r["status"] == "dispatched" and r["project_id"] == 1, r
        d = store.get_decision(r["decision_id"])
        assert d["state"] == TENTATIVE and d["dispatch_state"] == DISP_SENT
        assert len(hn.dispatches) == 1                         # dispatch 恰 1 次
        assert hn.dispatches[0]["session_key"] == "proj:1"
        assert len(ch.cards) == 1 and ch.cards[0]["label"] == "甲公司尽调"  # 作用域标记
        assert ch.cards[0]["card_id"] == d["card_msg_id"]      # card_id 回写
        assert store.get_job_by_decision(r["decision_id"]) is not None        # 入写入队列
        assert store.count("memory_items") == 0               # 隔离期内未写入
        # 隔离期到期 → tick → COMMITTED + 写入
        core.tick(now=T0 + 301)
        assert store.get_decision(r["decision_id"])["state"] == COMMITTED
        assert store.count("memory_items") == 1
    finally:
        os.path.exists(p) and os.remove(p)


def test_int2_low_confidence_awaits_then_confirm():
    core, store, ch, hn, p = _build(judge_pid=0)   # judge 返回 0 → 新项目/低置信
    try:
        r = core.handle_message(_msg("帮我安排个全新的、跟现有都无关的事情"), now=T0)
        assert r["status"] == "awaiting_confirmation", r
        d = store.get_decision(r["decision_id"])
        assert d["state"] == WAITING_CONFIRMATION and d["dispatch_state"] == DISP_PENDING
        assert len(hn.dispatches) == 0                         # 关键：未 dispatch，不污染 session
        assert len(ch.confirm_cards) == 1 and len(ch.cards) == 0
        # 用户确认归到项目 2
        r2 = core.handle_confirm(r["decision_id"], confirmed_pid=2, now=T0 + 5)
        assert r2["status"] == "dispatched" and r2["project_id"] == 2, r2
        d2 = store.get_decision(r["decision_id"])
        assert d2["state"] == TENTATIVE and d2["dispatch_state"] == DISP_SENT
        assert d2["project_id"] == 2
        assert len(hn.dispatches) == 1                         # 确认后才 dispatch
        assert hn.dispatches[0]["session_key"] == "proj:2"
    finally:
        os.path.exists(p) and os.remove(p)


def test_int_event_dedup():
    """同 event_id 重复投递 → 第二次丢弃，不产生第二个 decision/dispatch。"""
    core, store, ch, hn, p = _build(judge_pid=1)
    try:
        core.handle_message(_msg("查甲公司订单", eid="ev9", pmid="pm9"), now=T0)
        r2 = core.handle_message(_msg("查甲公司订单", eid="ev9", pmid="pm9"), now=T0)
        assert r2["status"] == "duplicate"
        assert store.count("route_decisions") == 1
        assert len(hn.dispatches) == 1
    finally:
        os.path.exists(p) and os.remove(p)


def test_int3_correction_loop():
    """纠正：旧→CORRECTED+记录+取消/retire+禁用原卡+重投新session(parent链)。"""
    core, store, ch, hn, p = _build(judge_pid=1)
    try:
        r = core.handle_message(_msg("查甲公司毛利率"), now=T0)      # 归 proj1
        old = r["decision_id"]
        core.tick(now=T0 + 301)                                       # 先固化写入（测已写记忆的纠正）
        assert store.count("memory_items") == 1
        r2 = core.handle_correction({"decision_id": old, "to_pid": 2, "user_id": "u1"}, now=T0 + 400)
        assert r2["status"] == "corrected" and r2["to"] == 2
        assert store.get_decision(old)["state"] == CORRECTED
        assert store.count("corrections") == 1
        assert len(store.recall({"level": "project", "project_id": 1})) == 0   # 旧记忆被 retire
        assert any(u["new_state"] == "corrected" for u in ch.updated)          # 原卡禁用
        nd = store.get_decision(r2["new_decision_id"])
        assert nd["state"] == TENTATIVE and nd["parent_decision_id"] == old and nd["project_id"] == 2
        assert hn.dispatches[-1]["session_key"] == "proj:2"                     # 重投到正确 session
    finally:
        os.path.exists(p) and os.remove(p)


def test_int4_consecutive_corrections():
    """A→B→C：parent 链完整，旧的全 CORRECTED，最终单活跃。"""
    core, store, ch, hn, p = _build(judge_pid=1)
    try:
        d1 = core.handle_message(_msg("查甲公司"), now=T0)["decision_id"]              # A=1
        d2 = core.handle_correction({"decision_id": d1, "to_pid": 2, "user_id": "u"}, now=T0+10)["new_decision_id"]  # →B=2
        d3 = core.handle_correction({"decision_id": d2, "to_pid": 3, "user_id": "u"}, now=T0+20)["new_decision_id"]  # →C=3
        assert store.get_decision(d2)["parent_decision_id"] == d1
        assert store.get_decision(d3)["parent_decision_id"] == d2
        assert store.get_decision(d1)["state"] == CORRECTED
        assert store.get_decision(d2)["state"] == CORRECTED
        assert store.get_decision(d3)["state"] == TENTATIVE and store.get_decision(d3)["project_id"] == 3
    finally:
        os.path.exists(p) and os.remove(p)


def test_int_dispatch_failure_clean_rollback():
    """dispatch 失败（session 未污染）→ 删半落库 decision、不入队、不写入，可重发。"""
    core, store, ch, hn, p = _build(judge_pid=1)
    hn.fail = True
    try:
        r = core.handle_message(_msg("查甲公司"), now=T0)
        assert r["status"] == "dispatch_failed", r
        assert store.count("route_decisions") == 0          # 半落库已删，单活跃锁释放
        assert store.count("write_jobs") == 0               # 未入队
        core.tick(now=T0 + 301)
        assert store.count("memory_items") == 0             # 不写入污染
        # 用户重发（新 event）→ dispatch 恢复 → 成功
        hn.fail = False
        r2 = core.handle_message(_msg("查甲公司", eid="e2", pmid="pm2"), now=T0 + 400)
        assert r2["status"] == "dispatched"
    finally:
        os.path.exists(p) and os.remove(p)


def test_int_bad_correction_callback():
    """脏 callback（None / 缺字段）→ bad_request，不炸穿。"""
    core, store, ch, hn, p = _build(judge_pid=1)
    try:
        assert core.handle_correction(None)["status"] == "bad_request"
        assert core.handle_correction({"to_pid": 2})["status"] == "bad_request"      # 缺 decision_id
        assert core.handle_correction({"decision_id": "x"})["status"] == "bad_request"  # 缺 to_pid
    finally:
        os.path.exists(p) and os.remove(p)


def test_int_correction_replay_idempotent():
    """重复纠正同一 decision → 第二次 invalid（CORRECTED 终态护栏），不产生第二个新 decision。"""
    core, store, ch, hn, p = _build(judge_pid=1)
    try:
        old = core.handle_message(_msg("查甲公司"), now=T0)["decision_id"]
        r1 = core.handle_correction({"decision_id": old, "to_pid": 2, "user_id": "u"}, now=T0+10)
        assert r1["status"] == "corrected"
        n_after_first = store.count("route_decisions")
        r2 = core.handle_correction({"decision_id": old, "to_pid": 3, "user_id": "u"}, now=T0+20)
        assert r2["status"] == "invalid"                    # 旧已 CORRECTED，重放被拒
        assert store.count("route_decisions") == n_after_first   # 未产生第二个派生 decision
    finally:
        os.path.exists(p) and os.remove(p)


def test_int_unknown_project_rejected():
    """确认/纠正到不存在的项目 → 拒绝，不 dispatch。"""
    core, store, ch, hn, p = _build(judge_pid=0)   # 低置信 → 等确认
    try:
        r = core.handle_message(_msg("全新的事"), now=T0)
        rc = core.handle_confirm(r["decision_id"], confirmed_pid=99, now=T0+5)
        assert rc["status"] == "unknown_project"
        assert len(hn.dispatches) == 0
    finally:
        os.path.exists(p) and os.remove(p)


def test_int5_cross_ref_annotate_not_inject():
    """跨项目引用：归当前项目(惯性)不被《被引项目》吸走，卡片标注引用，无跨库注入（T-INT-5）。"""
    core, store, ch, hn, p = _build(judge_pid=1)
    try:
        core.handle_message(_msg("查甲公司毛利率", eid="e1", pmid="pm1"), now=T0)   # 惯性→proj1
        r = core.handle_message(
            _msg("参考《乙公司尽调》的目录结构把这个也整理一下", eid="e2", pmid="pm2"), now=T0+1)
        assert r["status"] == "dispatched"
        assert r["project_id"] == 1 and r["referenced_id"] == 2       # 归当前(1)，标注引用(2)
        assert hn.dispatches[-1]["session_key"] == "proj:1"           # 未被《乙公司》吸走
        assert "引用了《乙公司尽调》" in ch.cards[-1]["label"]
    finally:
        os.path.exists(p) and os.remove(p)


def test_int7_judge_unparseable_degrades_to_confirm():
    """判定无法解析(None，含超时降级)→ 低置信确认卡，不 dispatch（T-INT-7 降级）。"""
    core, store, ch, hn, p = _build(judge_pid=None)   # judge 返回 None = 无法判断/超时
    try:
        r = core.handle_message(_msg("含糊不清的一句话"), now=T0)
        assert r["status"] == "awaiting_confirmation", r
        assert len(hn.dispatches) == 0                # 不 dispatch（不污染 session）
        assert len(ch.confirm_cards) == 1
    finally:
        os.path.exists(p) and os.remove(p)


def test_neg_only_project_scope_written():
    """负向（T-NEG）：v0.1 只写 project 作用域，无 global/role 自动写入。"""
    core, store, ch, hn, p = _build(judge_pid=1)
    try:
        core.handle_message(_msg("查甲公司订单"), now=T0)
        core.tick(now=T0 + 301)
        rows = store.db.execute("SELECT DISTINCT scope_level FROM memory_items").fetchall()
        assert {r["scope_level"] for r in rows} <= {"project"}
    finally:
        os.path.exists(p) and os.remove(p)


def test_int_blank_message_robust(*_):
    """负向（§4 ⑤）：空白文本消息全链路不炸穿，且信息量门兜底不沉淀垃圾。"""
    core, store, ch, hn, p = _build(judge_pid=1)
    try:
        r = core.handle_message(_msg("   ", eid="eb", pmid="pmb"), now=T0)
        assert r["status"] in ("dispatched", "awaiting_confirmation", "ignored"), r
        core.tick(now=T0 + 301)
        assert store.count("memory_items") == 0      # 空白被信息量门挡，不沉淀
    finally:
        os.path.exists(p) and os.remove(p)


def test_int_redelivery_high_typed_duplicate():
    """重投同 platform_msg_id、异 event_id（高置信路径）→ typed duplicate，不抛 IntegrityError（外审 P2，与模式 B route 对齐）。"""
    core, store, ch, hn, p = _build(judge_pid=1)
    try:
        r1 = core.handle_message(_msg("查一下甲公司近三年毛利率", eid="e1", pmid="pmX"), now=T0)
        assert r1["status"] == "dispatched", r1
        r2 = core.handle_message(_msg("查一下甲公司近三年毛利率", eid="e2", pmid="pmX"), now=T0)
        assert r2["status"] == "duplicate", r2          # 同 pmid 异 eid：msg 级唯一索引兜底，不炸穿
        r3 = core.handle_message(_msg("查一下甲公司近三年毛利率", eid="e1", pmid="pmX"), now=T0)
        assert r3["status"] == "duplicate", r3          # 同 eid：event 级幂等
        assert store.count("route_decisions") == 1
        assert len(hn.dispatches) == 1                  # 第二、三次未触达后端
    finally:
        os.path.exists(p) and os.remove(p)


def test_int_redelivery_low_typed_duplicate():
    """低置信路径同样：重投同 platform_msg_id、异 event_id → duplicate，不抛、不重复建 decision / 不再发卡。"""
    core, store, ch, hn, p = _build(judge_pid=0)
    try:
        r1 = core.handle_message(_msg("帮我整理一下这个东西", eid="e1", pmid="pmL"), now=T0)
        assert r1["status"] == "awaiting_confirmation", r1
        r2 = core.handle_message(_msg("帮我整理一下这个东西", eid="e2", pmid="pmL"), now=T0)
        assert r2["status"] == "duplicate", r2
        assert store.count("route_decisions") == 1
        assert len(ch.confirm_cards) == 1               # 第二次未再发确认卡
    finally:
        os.path.exists(p) and os.remove(p)


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"PASS {name}")
            except AssertionError as e:
                fails += 1; print(f"FAIL {name}: {e}")
            except Exception as e:
                fails += 1; print(f"ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{'ALL GREEN' if not fails else str(fails)+' FAILED'}")
    sys.exit(1 if fails else 0)
