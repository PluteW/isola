"""MCP server（模式 B 接入轨）测试——守护 SDD ③ §1 + §7 契约。
纯 assert 自跑（无 pytest），与 test_mode_b.py 一致。

两类：
- 薄接入轨契约（不依赖 mcp SDK）：`IsolaTools` 把 5 个工具忠实转发到同一组 core use case，
  不加任何判定/编排逻辑——状态枚举与直调 `MemoryService` 一致（钉①「业务只在 core 一份」）；
- SDK 装配（需 mcp，未装则 SKIP）：`build_mcp_server` 注册 5 个 isola_* 工具，schema 完整、
  isola_recall 不暴露 project_id（确认门绕过口在 transport 边界即关闭，§3B.6⑤）。
"""
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
from isola.mcp_server import IsolaTools, build_mcp_server
from isola.models import COMMITTED, WAITING_CONFIRMATION, CORRECTED
from fakes import FakeJudge

T0 = 1_000_000.0


def _tools(judge_pid):
    """装配模式 B 的 IsolaTools（包 MemoryService，无 channel/harness）。"""
    store = Store()
    reg_path = tempfile.mktemp(suffix=".yaml")
    reg = Registry(reg_path)
    reg.add("甲公司尽调", "公司级尽调：财务/客户/订单")   # pid 1
    reg.add("乙公司尽调", "公司级尽调：主营/订单/风电")   # pid 2
    reg.add("AI技术周报", "每周 AI 动态整理")              # pid 3
    svc = MemoryService(store, reg, Router(FakeJudge(judge_pid)), now_fn=lambda: T0)
    return IsolaTools(svc), store, reg_path


_N = [0]


def _ev():
    _N[0] += 1
    return f"e{_N[0]}", f"pm{_N[0]}"


def _cleanup(p):
    os.path.exists(p) and os.remove(p)


# ============== 薄接入轨契约（SDK-free，转发到同一组 core use case） ==============

def test_mcp_route_recall_remember_high():
    """isola_route(高) → isola_recall → isola_remember 全链路，经工具层 → 一条 active memory。"""
    tools, store, p = _tools(judge_pid=1)
    try:
        eid, pmid = _ev()
        r = tools.route("查一下甲公司近三年毛利率", "c1", "u1", eid, pmid)
        assert r["status"] == "ok" and r["project_id"] == 1, r
        did = r["decision_id"]
        assert store.get_decision(did)["state"] == COMMITTED
        assert tools.recall(did)["items"] == []                      # 初始空
        assert tools.remember(did, "甲公司近三年毛利率走低")["status"] == "written"
        assert len(tools.recall(did)["items"]) == 1
        assert store.count("write_jobs") == 0                        # 模式 B 不投递
    finally:
        _cleanup(p)


def test_mcp_low_then_confirm():
    """isola_route(低) → needs_confirmation + 候选；确认前 recall/remember 被拒；isola_confirm 后通。"""
    tools, store, p = _tools(judge_pid=0)
    try:
        eid, pmid = _ev()
        r = tools.route("帮我把这个材料归一下类", "c1", "u1", eid, pmid)
        assert r["status"] == "needs_confirmation", r
        assert r["candidates"] and "project_id" in r["candidates"][0] and "name" in r["candidates"][0]
        did = r["decision_id"]
        assert tools.recall(did)["status"] == "invalid"             # 确认前拒
        assert tools.remember(did, "确认前回写够长")["status"] == "invalid"
        assert tools.confirm(did, 3, "u1")["status"] == "ok"
        assert tools.recall(did)["status"] == "ok"
        assert tools.remember(did, "归类结论：属于AI周报")["status"] == "written"
    finally:
        _cleanup(p)


def test_mcp_correct_flow():
    """isola_correct：旧 decision CORRECTED + 旧记忆 retire；之后 recall(decision) → invalid。"""
    tools, store, p = _tools(judge_pid=1)
    try:
        eid, pmid = _ev()
        did = tools.route("甲公司的风电订单", "c1", "u1", eid, pmid)["decision_id"]
        tools.remember(did, "甲公司的风电订单明细")
        before = store.count("route_decisions")
        res = tools.correct(did, 2, "u1", "ce1")
        assert res["status"] == "corrected" and res["to_pid"] == 2, res
        assert store.get_decision(did)["state"] == CORRECTED
        assert store.count("route_decisions") == before            # 不重投/不新建
        assert tools.recall(did)["status"] == "invalid"            # CORRECTED 不可 recall
    finally:
        _cleanup(p)


def test_mcp_route_event_idempotent():
    """isola_route 忠实转发 event_id：同 event_id 重放 → duplicate（幂等在 core，工具不另做）。"""
    tools, store, p = _tools(judge_pid=1)
    try:
        a = tools.route("甲公司数据汇总", "c1", "u1", "ex", "pmx")
        assert a["status"] == "ok"
        b = tools.route("甲公司数据汇总", "c1", "u1", "ex", "pmx")   # 同 event_id
        assert b["status"] == "duplicate", b
        assert store.count("route_decisions") == 1
    finally:
        _cleanup(p)


def test_mcp_passthrough_no_added_logic():
    """薄转发：错误状态与直调 core 完全一致（工具层不吞错/不另判）。"""
    tools, store, p = _tools(judge_pid=0)
    try:
        assert tools.remember("nope", "随便写点够长的内容")["status"] == "invalid"   # 不存在
        assert tools.recall("nope")["status"] == "invalid"
        assert tools.correct("nope", 2, "u1")["status"] == "not_found"
        eid, pmid = _ev()
        did = tools.route("帮我把这条材料归个类", "c1", "u1", eid, pmid)["decision_id"]   # 低置信→WAITING
        assert tools.confirm(did, 999, "u1")["status"] == "unknown_project"             # WAITING + 未知项目
    finally:
        _cleanup(p)


# ============== SDK 装配（需 mcp，未装则 SKIP） ==============

def test_mcp_server_registers_five_tools_with_schema():
    """build_mcp_server 注册 5 个 isola_* 工具；schema 完整；isola_recall 不暴露 project_id（门槛⑤）。"""
    try:
        import mcp  # noqa: F401
    except Exception:
        print("  (SKIP: mcp SDK 未安装)")
        return
    tools, store, p = _tools(judge_pid=1)
    try:
        server = build_mcp_server(tools)
        listed = server._tool_manager.list_tools()
        names = sorted(t.name for t in listed)
        assert names == ["isola_confirm", "isola_correct", "isola_recall",
                         "isola_remember", "isola_route"], names
        by = {t.name: t for t in listed}
        assert all(t.description for t in listed)                   # 每个工具都有描述
        rp = set(by["isola_route"].parameters["properties"])
        assert {"text", "chat_id", "user_id", "event_id"} <= rp, rp
        rec = by["isola_recall"].parameters["properties"]
        assert "decision_id" in rec and "project_id" not in rec     # 不暴露 project_id（边界关绕过口）
        rem = by["isola_remember"].parameters["properties"]
        assert {"decision_id", "content"} <= set(rem)
    finally:
        _cleanup(p)


def test_mcp_call_tool_roundtrip():
    """经 FastMCP call_tool 真实调用（入参按 schema 校验 + dispatch）：route → remember → recall
    端到端 round-trip（= 真 MCP 服务行为，仅少 stdio 管道本身）。需 mcp，未装则 SKIP。"""
    try:
        import mcp  # noqa: F401
    except Exception:
        print("  (SKIP: mcp SDK 未安装)")
        return
    import asyncio
    tools, store, p = _tools(judge_pid=1)
    try:
        tm = build_mcp_server(tools)._tool_manager

        async def go():
            r = await tm.call_tool("isola_route", {
                "text": "甲公司近三年毛利率", "chat_id": "c1", "user_id": "u1", "event_id": "eRT"})
            did = (r[1] if isinstance(r, tuple) else r)["decision_id"]
            await tm.call_tool("isola_remember", {"decision_id": did, "content": "甲公司毛利率走低"})
            rc = await tm.call_tool("isola_recall", {"decision_id": did})
            return (rc[1] if isinstance(rc, tuple) else rc)

        out = asyncio.run(go())
        assert out["status"] == "ok" and len(out["items"]) == 1, out
        assert store.count("memory_items") == 1
    finally:
        _cleanup(p)


def test_mcp_protocol_end_to_end():
    """真 MCP 协议 round-trip（内存双工流，客户端↔服务端走完整 JSON-RPC：initialize → list_tools
    → call_tool 链 + 结果序列化）——仅少 stdio 字节管道本身。route→remember→recall 全链路。需 mcp，未装则 SKIP。"""
    try:
        from mcp.shared.memory import create_connected_server_and_client_session as connect
    except Exception:
        print("  (SKIP: mcp SDK 未安装)")
        return
    import asyncio
    import json
    tools, store, p = _tools(judge_pid=1)

    def _data(r):
        assert not r.isError, r
        return json.loads(r.content[0].text)

    try:
        server = build_mcp_server(tools)

        async def go():
            async with connect(server._mcp_server) as client:
                listed = await client.list_tools()
                names = sorted(t.name for t in listed.tools)
                assert names == ["isola_confirm", "isola_correct", "isola_recall",
                                 "isola_remember", "isola_route"], names
                did = _data(await client.call_tool("isola_route", {
                    "text": "甲公司近三年毛利率", "chat_id": "c1",
                    "user_id": "u1", "event_id": "eP2P"}))["decision_id"]
                assert _data(await client.call_tool("isola_remember", {
                    "decision_id": did, "content": "甲公司毛利率走低"}))["status"] == "written"
                rc = _data(await client.call_tool("isola_recall", {"decision_id": did}))
                assert rc["status"] == "ok" and len(rc["items"]) == 1, rc
                return did

        did = asyncio.run(go())
        assert store.get_decision(did)["state"] == COMMITTED
        assert store.count("memory_items") == 1
    finally:
        _cleanup(p)


def test_build_memory_service_offline_boot():
    """启动装配（config.build_memory_service）离线可跑：judge 构造不联网，store/registry/projects 就位，
    返回的是模式 B MemoryService（无 channel/harness）。守护真正的服务启动路径，非仅 --help。"""
    from isola.config import Config, build_memory_service
    os.environ["ISOLA_TEST_KEY"] = "dummy"
    try:
        cfg = Config(
            judge={"type": "openai_compat", "base_url": "http://x", "model": "m",
                   "api_key_env": "ISOLA_TEST_KEY"},
            harness={"type": "llm_direct", "base_url": "http://x", "model": "m",
                     "api_key_env": "ISOLA_TEST_KEY"},      # 模式 B 不构造它，仅为 Config 必填
            store={"path": ":memory:"},
            projects=[{"id": 1, "name": "甲公司尽调", "desc": "财务"}])
        svc = build_memory_service(cfg)
        assert svc.registry.get(1)["name"] == "甲公司尽调"
        assert svc.store is not None and svc.router.judge is not None
        assert not hasattr(svc, "harness") and not hasattr(svc, "channel")   # 模式 B：不持后端
    finally:
        os.environ.pop("ISOLA_TEST_KEY", None)


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
