"""真相源测试——守护 SDD v2 §1 / test-plan-v1 的 T-INV-1~5,7 + T-REC-1~4。
纯 assert 自测（无 pytest），与 test_router.py 一致。"""
import sys
import pathlib
import sqlite3
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from isola.store import Store
from isola.models import (
    InboundMessage, RouteDecision, MemoryItem,
    TENTATIVE, COMMITTED, CORRECTED, WAITING_CONFIRMATION,
    DISP_SENT, DISP_PENDING, JOB_PENDING, JOB_RUNNING, JOB_DONE, JOB_CANCELLED,
)

T0 = 1_000_000.0   # 固定基准时间，测试可控


def _msg(mid="m1", pmid="pm1", text="查一下甲公司的毛利率"):
    return InboundMessage(msg_id=mid, event_id="e_"+mid, platform_msg_id=pmid,
                          user_id="u1", text=text, chat_id="c1", platform_ts=int(T0))


def _decision(did="d1", mid="m1", pid=1, state=TENTATIVE, disp=DISP_SENT):
    return RouteDecision(decision_id=did, msg_id=mid, route_type="judge",
                         project_id=pid, state=state, dispatch_state=disp,
                         confidence_score=0.9)


# ---------- 不变量 ----------
def test_inv1_event_idempotent():
    s = Store()
    assert s.record_event("e1", "pm1", "msg", now=T0) is True
    assert s.record_event("e1", "pm1", "msg", now=T0) is False   # 重复丢弃
    assert s.count("events") == 1


def test_inv2_message_idempotent():
    s = Store()
    id1 = s.upsert_message(_msg("mA", "pmX"), now=T0)
    id2 = s.upsert_message(_msg("mB", "pmX"), now=T0)            # 同 platform_msg_id
    assert id1 == id2 == "mA"                                    # 返回既有，不新建
    assert s.count("messages") == 1


def test_inv3_single_active_decision():
    s = Store()
    s.upsert_message(_msg(), now=T0)
    s.insert_decision(_decision("d1"), now=T0)
    try:
        s.insert_decision(_decision("d2"), now=T0)               # 同 msg_id 第二个 active
        assert False, "应抛 IntegrityError"
    except sqlite3.IntegrityError:
        pass
    # 把 d1 转 CORRECTED 后，可插新 active（纠正重投场景）
    s.set_decision_state("d1", CORRECTED, now=T0)
    s.insert_decision(_decision("d2", state=TENTATIVE), now=T0)  # 不再冲突
    assert s.count("route_decisions") == 2


def test_inv4_writejob_idempotent():
    s = Store()
    s.upsert_message(_msg(), now=T0); s.insert_decision(_decision(), now=T0)
    assert s.enqueue_write("d1", "m1", due_at=T0+300, now=T0) is not None
    assert s.enqueue_write("d1", "m1", due_at=T0+300, now=T0) is None   # 幂等
    assert s.count("write_jobs") == 1


def test_inv5_memory_idempotent():
    s = Store()
    it = MemoryItem(scope_level="project", project_id=1, content="x",
                    source_msg_id="m1", decision_id="d1")
    i1 = s.write_memory(it, now=T0)
    i2 = s.write_memory(it, now=T0)                              # 同 decision_id
    assert i1 == i2
    assert s.count("memory_items") == 1


def test_inv7_retired_not_recalled():
    s = Store()
    s.write_memory(MemoryItem("project", 1, "保密内容", "m1", "d1"), now=T0)
    assert len(s.recall({"level": "project", "project_id": 1})) == 1
    s.retire(decision_id="d1", now=T0)
    assert len(s.recall({"level": "project", "project_id": 1})) == 0   # retired 不召回


# ---------- 崩溃恢复 ----------
def test_rec1_due_tentative_commits_and_writes():
    s = Store()
    s.upsert_message(_msg(), now=T0)
    s.insert_decision(_decision(state=TENTATIVE), now=T0)
    s.enqueue_write("d1", "m1", due_at=T0+300, now=T0)
    cnt = s.recovery_scan(now=T0+301)                            # 隔离期已过
    assert cnt["committed"] == 1
    assert s.get_decision("d1")["state"] == COMMITTED
    assert s.count("memory_items") == 1                          # 写入发生
    assert s.get_job_by_decision("d1")["status"] == JOB_DONE


def test_rec2_corrected_cancels_no_write():
    s = Store()
    s.upsert_message(_msg(), now=T0)
    s.insert_decision(_decision(state=TENTATIVE), now=T0)
    s.enqueue_write("d1", "m1", due_at=T0+300, now=T0)
    s.set_decision_state("d1", CORRECTED, now=T0+10)             # 隔离期内被纠正
    cnt = s.recovery_scan(now=T0+301)
    assert cnt["cancelled"] == 1
    assert s.get_job_by_decision("d1")["status"] == JOB_CANCELLED
    assert s.count("memory_items") == 0                          # 不写入污染


def test_rec3_running_timeout_retries():
    s = Store()
    s.upsert_message(_msg(), now=T0); s.insert_decision(_decision(), now=T0)
    jid = s.enqueue_write("d1", "m1", due_at=T0, now=T0)
    s.mark_job_running(jid, now=T0)                              # 开始执行
    cnt = s.recovery_scan(now=T0 + 301)                         # 超 running_timeout(300)
    assert cnt["retried"] == 1
    assert s.get_job_by_decision("d1")["status"] == JOB_PENDING  # 回 pending 重试


def test_rec4_waiting_confirmation_untouched():
    s = Store()
    s.upsert_message(_msg(), now=T0)
    # 低置信：WAITING_CONFIRMATION，未 dispatch，无 write_job
    s.insert_decision(_decision(state=WAITING_CONFIRMATION, disp=DISP_PENDING), now=T0)
    cnt = s.recovery_scan(now=T0 + 9999)
    assert all(v == 0 for v in cnt.values())                      # 任何动作都没发生
    assert s.get_decision("d1")["state"] == WAITING_CONFIRMATION  # 仍等确认，未误投
    assert s.count("memory_items") == 0


# ---------- 实现加固新增：守护 Top3 修复点 ----------
def test_state_transition_guard():
    """非法状态回退被拒（守护"纠正不污染"）。"""
    s = Store()
    s.upsert_message(_msg(), now=T0)
    s.insert_decision(_decision(state=TENTATIVE), now=T0)
    assert s.set_decision_state("d1", COMMITTED, now=T0) is True        # TENTATIVE→COMMITTED 合法
    assert s.set_decision_state("d1", TENTATIVE, now=T0) is False       # COMMITTED→TENTATIVE 非法
    assert s.get_decision("d1")["state"] == COMMITTED                   # 状态未被改回
    assert s.set_decision_state("d1", CORRECTED, now=T0) is True        # COMMITTED→CORRECTED 合法
    assert s.set_decision_state("d1", COMMITTED, now=T0) is False       # CORRECTED 是终态，不可复活
    assert s.get_decision("d1")["state"] == CORRECTED


def test_job_claimed_once():
    """job 仅 pending→running 一次（防重复领取）。"""
    s = Store()
    s.upsert_message(_msg(), now=T0); s.insert_decision(_decision(), now=T0)
    jid = s.enqueue_write("d1", "m1", due_at=T0, now=T0)
    assert s.mark_job_running(jid, now=T0) is True                      # 首次抢占成功
    assert s.mark_job_running(jid, now=T0) is False                     # 已 running，再领失败
    assert s.get_job_by_decision("d1")["attempts"] == 1                 # attempts 不被重复加


def test_unit3_info_gate_blocks_blank():
    """write_policy 信息量门（T-UNIT-3）：空白 text 即使到期也不写入。"""
    s = Store()
    s.upsert_message(_msg(text="   "), now=T0)
    s.insert_decision(_decision(state=TENTATIVE), now=T0)
    s.enqueue_write("d1", "m1", due_at=T0, now=T0)
    s.recovery_scan(now=T0 + 1)
    assert s.count("memory_items") == 0          # 空白被信息量门拦
    assert s.get_decision("d1")["state"] == COMMITTED   # 状态仍提交（只是不写记忆）


def test_info_gate_blocks_low_signal():
    """信息量门：零信号消息（谢谢/继续）即使 COMMITTED 也不沉淀。"""
    s = Store()
    s.upsert_message(_msg(text="好的，谢谢"), now=T0)
    s.insert_decision(_decision(state=TENTATIVE), now=T0)
    s.enqueue_write("d1", "m1", due_at=T0, now=T0)
    s.recovery_scan(now=T0 + 1)
    assert s.count("memory_items") == 0              # 零信号不沉淀


def test_info_gate_keeps_short_high_value():
    """短但高价值指令不被信息量门误杀。"""
    from isola.writeback import info_gate
    assert info_gate("禁GPU") is True
    assert info_gate("别联网") is True
    assert info_gate("用本地模型") is True
    assert info_gate("好的，谢谢") is False        # 零信号仍挡
    assert info_gate("继续") is False              # 指代仍挡


def test_durability_gate_classifies():
    """记忆价值门：试探/假设/临时/否定→tentative；决策/事实/约束→stable。"""
    from isola.writeback import durability
    for t in ["这个方案可能行，先试试", "也许用本地模型更好", "假设毛利率能到30%",
              "暂时先这样，回头再改", "那个办法行不通"]:
        assert durability(t) == "tentative", t
    for t in ["禁GPU", "决定用 DeepSeek 做 judge", "甲公司某年营收数亿",
              "所有报告统一用三段式结构", "客户要求周五前交付"]:
        assert durability(t) == "stable", t


def test_durability_set_on_commit():
    """端到端：COMMITTED 写入时自动按内容打 durability 标签并落库。"""
    s = Store()
    s.upsert_message(_msg(text="也许这个数据不准，再核对下"), now=T0)
    s.insert_decision(_decision(state=TENTATIVE), now=T0)
    s.enqueue_write("d1", "m1", due_at=T0, now=T0)
    s.recovery_scan(now=T0 + 1)
    row = s.recall({"level": "project", "project_id": 1})[0]
    assert row["durability"] == "tentative"          # "也许"→tentative，标签随写入落库


def test_sensitive_gate_detects():
    """敏感信息门：凭据/密钥/私钥/敏感路径→拦截；仅提及 token/api 等词的正常讨论→放行。"""
    from isola.writeback import sensitive_gate
    for t in ["我的 key 是 sk-abcd1234efgh5678ijkl", "配置 password=hunter2024Z",
              "AKIAIOSF0DNN7EXAMPLE", "-----BEGIN RSA PRIVATE KEY-----",
              "Authorization: Bearer eyJhbGc1234567890abcdef", "看 /Users/x/.ssh/id_rsa"]:
        assert sensitive_gate(t) is True, t
    for t in ["用 token 做路由", "讨论 API key 的设计要点", "甲公司毛利率20%", "禁GPU"]:
        assert sensitive_gate(t) is False, t


def test_sensitive_blocked_on_commit():
    """端到端：含密钥的消息即使 COMMITTED 也不沉淀进记忆。"""
    s = Store()
    s.upsert_message(_msg(text="服务器密钥 password=S3cr3tValue99"), now=T0)
    s.insert_decision(_decision(state=TENTATIVE), now=T0)
    s.enqueue_write("d1", "m1", due_at=T0, now=T0)
    s.recovery_scan(now=T0 + 1)
    assert s.count("memory_items") == 0                 # 敏感内容不沉淀
    assert s.get_decision("d1")["state"] == COMMITTED   # 状态仍提交，只是不写记忆


def test_dedup_same_content_same_project():
    """去重门：同项目同内容（不同 decision）只沉淀一次。"""
    s = Store()
    s.write_memory(MemoryItem("project", 1, "甲公司近三年营收分析", "m1", "d1"), now=T0)
    s.write_memory(MemoryItem("project", 1, "甲公司近三年营收分析", "m2", "d2"), now=T0)
    assert s.count("memory_items") == 1              # 内容去重


def test_dedup_is_per_project():
    """去重是项目内的：不同项目相同内容各自保留（隔离优先于去重）。"""
    s = Store()
    s.write_memory(MemoryItem("project", 1, "通用方法X", "m1", "d1"), now=T0)
    s.write_memory(MemoryItem("project", 2, "通用方法X", "m2", "d2"), now=T0)
    assert s.count("memory_items") == 2              # 跨项目不去重


def test_rec_committed_pending_compensation():
    """崩溃半状态补偿：decision 已 COMMITTED 但 memory 未写、job 仍 pending → 恢复补写。"""
    s = Store()
    s.upsert_message(_msg(), now=T0)
    s.insert_decision(_decision(state=TENTATIVE), now=T0)
    s.enqueue_write("d1", "m1", due_at=T0+300, now=T0)
    s.set_decision_state("d1", COMMITTED, now=T0+301)                   # 模拟崩在提交后、写入前
    assert s.count("memory_items") == 0
    cnt = s.recovery_scan(now=T0+302)
    assert cnt["compensated"] == 1
    assert s.count("memory_items") == 1                                 # 补写成功
    assert s.get_job_by_decision("d1")["status"] == JOB_DONE


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
