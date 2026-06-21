"""模式 B（MCP 记忆服务，agent 自执行）core 测试——守护 SDD v3 §3B + test-plan-mode-b 的 20 条 TB。
纯 assert 自测（无 pytest），与 test_store.py / test_integration.py 一致。
模式 B：agent 自执行，core 不 dispatch、不碰 harness/channel —— 故装配里没有 channel/harness。"""
import sys
import pathlib
import tempfile
import os
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).parent))

from isola.store import Store
from isola.registry import Registry
from isola.router import Router
from isola.mode_b import MemoryService
from isola.models import (
    InboundMessage, WAITING_CONFIRMATION, COMMITTED, CORRECTED, DISP_PENDING, DISP_SENT,
)
from fakes import FakeJudge

T0 = 1_000_000.0


def _build(judge_pid):
    """模式 B 装配：store + registry(3 projects) + MemoryService（无 channel/harness）。"""
    store = Store()
    reg_path = tempfile.mktemp(suffix=".yaml")
    reg = Registry(reg_path)
    reg.add("甲公司尽调", "公司级尽调：财务/客户/订单")   # pid 1
    reg.add("乙公司尽调", "公司级尽调：主营/订单/风电")   # pid 2
    reg.add("AI技术周报", "每周 AI 动态整理")              # pid 3
    svc = MemoryService(store, reg, Router(FakeJudge(judge_pid)), now_fn=lambda: T0)
    return svc, store, reg_path


_N = [0]


def _msg(text, eid=None, pmid=None, chat="c1"):
    _N[0] += 1
    eid = eid or f"e{_N[0]}"
    pmid = pmid or f"pm{_N[0]}"
    return InboundMessage(msg_id="m_" + eid, event_id=eid, platform_msg_id=pmid,
                          user_id="u1", text=text, chat_id=chat, platform_ts=int(T0))


def _cleanup(p):
    os.path.exists(p) and os.remove(p)


def _active_mem(store, did):
    return store.db.execute(
        "SELECT COUNT(*) c FROM memory_items WHERE decision_id=? AND state='active'",
        (did,)).fetchone()["c"]


# ============================ 不变量层（7） ============================

def test_tb_inv1_high_route_committed_no_dispatch():
    """高置信 route → state=COMMITTED + committed_at 非空 + 无 write_job + 从不 dispatch（§3B.1/3B.6①）。"""
    svc, store, p = _build(judge_pid=1)
    try:
        r = svc.route(_msg("查一下甲公司近三年毛利率"), now=T0)
        assert r["status"] == "ok" and r["project_id"] == 1, r
        d = store.get_decision(r["decision_id"])
        assert d["state"] == COMMITTED, d["state"]
        assert d["committed_at"] is not None                 # 门槛①：插入即写 committed_at
        assert d["dispatch_state"] == DISP_PENDING           # 从未 dispatch（无 sent）
        assert store.count("write_jobs") == 0                # 无隔离期 → 无写任务
    finally:
        _cleanup(p)


def test_tb_inv2_low_route_waiting_recall_remember_invalid():
    """低置信 route → WAITING_CONFIRMATION；该 decision 的 recall / remember 均 invalid（§3B.1/2/3）。"""
    svc, store, p = _build(judge_pid=0)
    try:
        r = svc.route(_msg("帮我整理一下这个东西"), now=T0)
        assert r["status"] == "needs_confirmation", r
        did = r["decision_id"]
        assert store.get_decision(did)["state"] == WAITING_CONFIRMATION
        assert svc.recall(decision_id=did)["status"] == "invalid"
        assert svc.remember(did, "试图回写内容够长", now=T0)["status"] == "invalid"
        assert store.count("memory_items") == 0
    finally:
        _cleanup(p)


def test_tb_inv3_confirm_committed_no_dispatch():
    """B confirm → COMMITTED + committed_at 写入 + 不 dispatch（§3B.1b）。"""
    svc, store, p = _build(judge_pid=0)
    try:
        r = svc.route(_msg("帮我整理一下这个东西"), now=T0)
        did = r["decision_id"]
        c = svc.confirm(did, 2, actor_id="u1", now=T0 + 1)
        assert c["status"] == "ok" and c["project_id"] == 2, c
        d = store.get_decision(did)
        assert d["state"] == COMMITTED
        assert d["committed_at"] is not None
        assert d["project_id"] == 2
        assert d["dispatch_state"] == DISP_PENDING           # 不 dispatch
        assert store.count("write_jobs") == 0
    finally:
        _cleanup(p)


def test_tb_inv4_corrected_recall_remember_invalid():
    """CORRECTED 终态：corrected decision 不能 recall、不能 remember（§3B.2/3）。"""
    svc, store, p = _build(judge_pid=1)
    try:
        r = svc.route(_msg("甲公司的订单数据"), now=T0)
        did = r["decision_id"]
        assert svc.correct(did, 2, actor_id="u1", now=T0 + 1)["status"] == "corrected"
        assert store.get_decision(did)["state"] == CORRECTED
        assert svc.recall(decision_id=did)["status"] == "invalid"
        assert svc.remember(did, "纠正后还想回写够长", now=T0 + 2)["status"] == "invalid"
    finally:
        _cleanup(p)


def test_tb_inv5_one_decision_one_memory():
    """一 decision 一记忆：同 decision 第二次 remember → duplicate(decision)，DB 仍一条（§3B.3）。"""
    svc, store, p = _build(judge_pid=1)
    try:
        r = svc.route(_msg("甲公司2023年营收12亿"), now=T0)
        did = r["decision_id"]
        a = svc.remember(did, "甲公司2023年营收12亿", now=T0 + 1)
        assert a["status"] == "written", a
        b = svc.remember(did, "另一条不同内容但同 decision 够长", now=T0 + 2)
        assert b["status"] == "duplicate" and b["kind"] == "decision", b
        assert store.count("memory_items") == 1
    finally:
        _cleanup(p)


def test_tb_inv6_remember_gated_skipped():
    """remember 被 info / sensitive 门拦 → skipped，DB 无 active memory（§3B.3）。"""
    svc, store, p = _build(judge_pid=1)
    try:
        r = svc.route(_msg("甲公司尽调要点"), now=T0)
        did = r["decision_id"]
        assert svc.remember(did, "好的", now=T0 + 1)["status"] == "skipped"                       # info 门
        assert svc.remember(did, "key 是 sk-abcdefghijklmnop1234", now=T0 + 2)["status"] == "skipped"  # 敏感门
        assert store.count("memory_items") == 0
    finally:
        _cleanup(p)


def test_tb_inv7_retired_not_recalled():
    """retire 后 memory 不被 recall（§3B.4：correct 即 retire）。"""
    svc, store, p = _build(judge_pid=1)
    try:
        r = svc.route(_msg("甲公司核心客户名单"), now=T0)
        did = r["decision_id"]
        svc.remember(did, "甲公司核心客户名单：A/B/C", now=T0 + 1)
        assert len(svc.recall(decision_id=did)["items"]) == 1
        svc.correct(did, 2, actor_id="u1", now=T0 + 2)                  # → retire 记忆
        got = svc.recall(project_id=1, allow_project=True)             # admin recall 验证已 retired
        assert got["status"] == "ok" and got["items"] == [], got
    finally:
        _cleanup(p)


# ============================ 集成层（6） ============================

def test_tb_int1_full_chain_high():
    """route → recall → [agent 执行] → remember 高置信全链路 → 一条 active memory（§3B 全）。"""
    svc, store, p = _build(judge_pid=1)
    try:
        r = svc.route(_msg("甲公司应收账款周转天数明显上升"), now=T0)
        assert r["status"] == "ok"
        did = r["decision_id"]
        rc = svc.recall(decision_id=did)
        assert rc["status"] == "ok" and rc["items"] == []              # 初始空
        w = svc.remember(did, "甲公司应收账款周转天数明显上升", now=T0 + 1)
        assert w["status"] == "written"
        assert len(svc.recall(decision_id=did)["items"]) == 1
        assert store.count("memory_items") == 1
    finally:
        _cleanup(p)


def test_tb_int2_low_chain_confirm_then_ok():
    """低置信链路 route(needs_confirmation) → confirm → recall → remember 成功；确认前 recall/remember 被拒（§3B.1/1b）。"""
    svc, store, p = _build(judge_pid=0)
    try:
        r = svc.route(_msg("把这个材料归一下类"), now=T0)
        assert r["status"] == "needs_confirmation"
        did = r["decision_id"]
        assert svc.recall(decision_id=did)["status"] == "invalid"       # 确认前拒
        assert svc.remember(did, "确认前回写够长", now=T0)["status"] == "invalid"
        assert svc.confirm(did, 3, actor_id="u1", now=T0 + 1)["status"] == "ok"
        assert svc.recall(decision_id=did)["status"] == "ok"
        assert svc.remember(did, "归类结论：属于AI周报", now=T0 + 2)["status"] == "written"
        assert store.count("memory_items") == 1
    finally:
        _cleanup(p)


def test_tb_int3_correct_retire_no_new_decision():
    """B correct：旧 CORRECTED + 旧 memory retired + 不新建 decision + 不 dispatch + 返回 {corrected,to_pid}（§3B.4）。"""
    svc, store, p = _build(judge_pid=1)
    try:
        r = svc.route(_msg("甲公司的风电订单"), now=T0)
        did = r["decision_id"]
        svc.remember(did, "甲公司的风电订单明细", now=T0 + 1)
        assert store.count("memory_items") == 1
        before = store.count("route_decisions")
        res = svc.correct(did, 2, actor_id="u1", correction_event_id="ce1", now=T0 + 2)
        assert res["status"] == "corrected" and res["to_pid"] == 2 and res["from"] == 1, res
        assert store.get_decision(did)["state"] == CORRECTED
        assert store.count("route_decisions") == before                # 不新建 decision（模式 B 不重投）
        assert store.count("write_jobs") == 0                          # 从不 dispatch/enqueue
        assert svc.recall(project_id=1, allow_project=True)["items"] == []   # 旧记忆 retired
        assert store.count("corrections") == 1
    finally:
        _cleanup(p)


def test_tb_int4_correct_idempotent_event():
    """correct 同 correction_event_id 重放 → duplicate，不重复写 correction（§3B.4）。"""
    svc, store, p = _build(judge_pid=1)
    try:
        r = svc.route(_msg("甲公司的客户集中度"), now=T0)
        did = r["decision_id"]
        a = svc.correct(did, 2, actor_id="u1", correction_event_id="ceX", now=T0 + 1)
        assert a["status"] == "corrected"
        b = svc.correct(did, 2, actor_id="u1", correction_event_id="ceX", now=T0 + 2)
        assert b["status"] == "duplicate", b
        assert store.count("corrections") == 1                         # 不重复写
    finally:
        _cleanup(p)


def test_tb_int5_no_harness_touched():
    """MCP 模式 B 全程 harness 调用数 == 0（§3B.5）：结构上 MemoryService 不接受 harness/channel；
    可观察上全程无写任务、无 decision 进入 sent。"""
    svc, store, p = _build(judge_pid=1)
    try:
        assert not hasattr(svc, "harness") and not hasattr(svc, "channel")
        r = svc.route(_msg("甲公司的财务报表"), now=T0)
        did = r["decision_id"]
        svc.recall(decision_id=did)
        svc.remember(did, "甲公司的财务报表已收齐", now=T0 + 1)
        svc.correct(did, 2, actor_id="u1", correction_event_id="c5", now=T0 + 2)
        assert store.count("write_jobs") == 0
        rows = store.db.execute("SELECT dispatch_state FROM route_decisions").fetchall()
        assert all(row["dispatch_state"] != DISP_SENT for row in rows)
    finally:
        _cleanup(p)


def test_tb_int6_cross_decision_content_duplicate():
    """同内容跨 decision remember → 明确 duplicate(content)，不伪装 written（§3B.3）。"""
    svc, store, p = _build(judge_pid=1)
    try:
        r1 = svc.route(_msg("甲公司营收口径说明"), now=T0)
        r2 = svc.route(_msg("甲公司另一条消息触发同项目"), now=T0)
        d1, d2 = r1["decision_id"], r2["decision_id"]
        content = "甲公司营收按合并口径统计"
        assert svc.remember(d1, content, now=T0 + 1)["status"] == "written"
        b = svc.remember(d2, content, now=T0 + 2)
        assert b["status"] == "duplicate" and b["kind"] == "content", b
        assert store.count("memory_items") == 1
    finally:
        _cleanup(p)


# ============================ 负向层（7） ============================

def test_tb_neg1_confirm_unknown_project():
    """confirm unknown project → unknown_project，状态不变（§3B.1b）。"""
    svc, store, p = _build(judge_pid=0)
    try:
        r = svc.route(_msg("帮我把这个分类一下"), now=T0)
        did = r["decision_id"]
        c = svc.confirm(did, 999, actor_id="u1", now=T0 + 1)
        assert c["status"] == "unknown_project", c
        assert store.get_decision(did)["state"] == WAITING_CONFIRMATION   # 状态不变
    finally:
        _cleanup(p)


def test_tb_neg2_confirm_non_waiting():
    """confirm 非 WAITING_CONFIRMATION → invalid（§3B.1b）。"""
    svc, store, p = _build(judge_pid=1)
    try:
        r = svc.route(_msg("甲公司的毛利率水平"), now=T0)       # 高置信 → COMMITTED
        did = r["decision_id"]
        assert svc.confirm(did, 2, actor_id="u1", now=T0 + 1)["status"] == "invalid"
    finally:
        _cleanup(p)


def test_tb_neg3_recall_invalid_states():
    """recall(decision_id) 对 not_found / WAITING / CORRECTED 全 invalid（§3B.2）。"""
    svc, store, p = _build(judge_pid=0)
    try:
        assert svc.recall(decision_id="nope")["status"] == "invalid"          # not_found
        r = svc.route(_msg("把这个东西归类下"), now=T0)
        assert svc.recall(decision_id=r["decision_id"])["status"] == "invalid"  # WAITING + project_id None
    finally:
        _cleanup(p)
    svc2, store2, p2 = _build(judge_pid=1)
    try:
        r2 = svc2.route(_msg("甲公司数据汇总"), now=T0)
        did2 = r2["decision_id"]
        svc2.correct(did2, 2, actor_id="u1", now=T0 + 1)
        assert svc2.recall(decision_id=did2)["status"] == "invalid"           # CORRECTED
    finally:
        _cleanup(p2)


def test_tb_neg4_project_recall_denied_by_default():
    """project_id recall 默认被拒（防绕过 confirm 直取项目记忆，§3B.2/3B.6⑤）。"""
    svc, store, p = _build(judge_pid=1)
    try:
        r = svc.route(_msg("甲公司的订单情况"), now=T0)
        svc.remember(r["decision_id"], "甲公司的订单很多", now=T0 + 1)
        assert svc.recall(project_id=1)["status"] == "invalid"                # 默认拒
        assert svc.recall(project_id=1, allow_project=True)["status"] == "ok"  # 显式授权才行
    finally:
        _cleanup(p)


def test_tb_neg5_remember_invalid_states():
    """remember 对 not_found / WAITING(+project_id None) 全 invalid（§3B.3）。"""
    svc, store, p = _build(judge_pid=0)
    try:
        assert svc.remember("nope", "随便写点够长的内容", now=T0)["status"] == "invalid"   # not_found
        r = svc.route(_msg("帮我归类这条消息"), now=T0)
        assert svc.remember(r["decision_id"], "试探内容够长能过门", now=T0)["status"] == "invalid"  # WAITING
        assert store.count("memory_items") == 0
    finally:
        _cleanup(p)


def test_tb_neg6_remember_same_decision_dedup_holds():
    """同 decision 多次 remember → 仅一条记忆，余皆 duplicate(decision)（§3B.3 decision_id UNIQUE）。
    注：v0.1 production = 单 worker 串行（单写者模型），故以串行多次验证
    「一 decision 一记忆」在任意序下恒成立；多连接真并发（需 BEGIN IMMEDIATE 序列化）留 serve 阶段。"""
    svc, store, p = _build(judge_pid=1)
    try:
        did = svc.route(_msg("甲公司2024Q1订单"), now=T0)["decision_id"]
        outs = [svc.remember(did, f"内容{i}号够长可过门槛", now=T0 + 1 + i)["status"] for i in range(3)]
        assert outs[0] == "written" and outs[1:] == ["duplicate", "duplicate"], outs
        assert store.count("memory_items") == 1
    finally:
        _cleanup(p)


def test_tb_neg7_correct_remember_both_orderings_safe():
    """correct 与 remember **任意串行顺序**都不出现「CORRECTED decision 仍有 active 记忆」（§3B.6⑦）。
    单 writer 串行模型下（v0.1 contract）以两种顺序验证不变量；store.remember_for_decision 事务内
    re-check 态是顺序②的护栏（防 correct 后 remember 仍写入）。真多连接并发留 serve 准入章。"""
    # 顺序①：remember 先写入 → 后被 correct retire
    svc, store, p = _build(judge_pid=1)
    try:
        did = svc.route(_msg("甲公司应付账款A"), now=T0)["decision_id"]
        assert svc.remember(did, "应付账款明细够长内容A", now=T0 + 1)["status"] == "written"
        svc.correct(did, 2, actor_id="u1", now=T0 + 2)
        assert store.get_decision(did)["state"] == CORRECTED
        assert _active_mem(store, did) == 0
    finally:
        _cleanup(p)
    # 顺序②：correct 先 → remember 见 CORRECTED（事务内 re-check）→ invalid，不写
    svc, store, p = _build(judge_pid=1)
    try:
        did = svc.route(_msg("甲公司应付账款B"), now=T0)["decision_id"]
        svc.correct(did, 2, actor_id="u1", now=T0 + 1)
        assert svc.remember(did, "应付账款明细够长内容B", now=T0 + 2)["status"] == "invalid"
        assert store.get_decision(did)["state"] == CORRECTED
        assert _active_mem(store, did) == 0
    finally:
        _cleanup(p)


def test_tb_route_redelivery_typed_duplicate():
    """加固（外审 P2）：route 重投——同 platform_msg_id、不同 event_id → typed {status:duplicate}，
    不抛 IntegrityError（event 级幂等放行后，msg 级单活跃索引兜底降级；不抛异常表业务失败）。"""
    svc, store, p = _build(judge_pid=1)
    try:
        a = svc.route(_msg("甲公司数据汇总", eid="e1", pmid="pmX"), now=T0)
        assert a["status"] == "ok", a
        b = svc.route(_msg("甲公司数据汇总", eid="e2", pmid="pmX"), now=T0)   # 同 pmid 不同 eid
        assert b["status"] == "duplicate", b
        c = svc.route(_msg("甲公司数据汇总", eid="e1", pmid="pmX"), now=T0)   # 同 eid → event 级幂等
        assert c["status"] == "duplicate", c
        assert store.count("route_decisions") == 1
    finally:
        _cleanup(p)


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
