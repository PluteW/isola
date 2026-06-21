"""模式 B 接入轨：MCP server（官方 mcp SDK / FastMCP，stdio）。SDD v3 第③章 §1。

把 core 的模式 B use case（`MemoryService`）暴露成 5 个 MCP 工具，供 Claude Code / Codex
这类「自身即 agent」的系统调用：
  isola_route → isola_recall → [agent 自己执行] → isola_remember；低置信 isola_confirm；纠错 isola_correct。

设计纪律（SDD ③ §1 / §5）：
- **薄接入轨**：工具只做 parse（MCP 入参 → DTO）+ 转发到同一组 core use case + 回传结果，
  **绝不实现判定/编排/记忆/纠错**（业务只在 core 一份，钉①）；`IsolaTools` 即这层映射，可脱离 SDK 测。
- **mcp 依赖懒加载**：`from mcp...` 只在 `build_mcp_server` 内 import——未装 SDK 也能 import 本模块、
  跑 `IsolaTools` 契约测试（mcp 为 optional extra：`pip install isola[mcp]`）。
- **不经执行后端**：模式 B 不 dispatch、不碰 harness/channel（见 `MemoryService`）。
- **record_presentation = unsupported**：MCP 无持久卡，调用方 agent 自行呈现候选/结果（SDD ③ §1）。
- **recall 默认只 decision_id**：`isola_recall` 不暴露 project_id 入参——确认门绕过口在 transport 边界
  即关闭（§3B.6⑤；project_id recall 仅留 core 内 admin 用）。

JSON schema 由 FastMCP 从 typed 函数签名 + docstring 自动生成（满足「完整 schema」）。
"""
from __future__ import annotations
import time
import uuid
from .models import InboundMessage


class IsolaTools:
    """MCP 工具 → core use case 的薄映射层（不依赖 mcp SDK，可独立测试）。
    包一个 `MemoryService`；route 负责把 MCP 入参拼成 `InboundMessage`，其余直接转发。"""

    def __init__(self, svc):
        self.svc = svc

    def route(self, text: str, chat_id: str, user_id: str, event_id: str,
              platform_msg_id: str = "", now: float | None = None) -> dict:
        now = now if now is not None else self.svc.now_fn()
        msg = InboundMessage(
            msg_id=uuid.uuid4().hex, event_id=event_id,
            platform_msg_id=platform_msg_id or event_id,   # 未给则以 event_id 兜底 msg 级幂等键
            user_id=user_id, text=text, chat_id=chat_id, platform_ts=int(now))
        return self.svc.route(msg, now=now)

    def recall(self, decision_id: str, query: str = "", k: int = 5) -> dict:
        return self.svc.recall(decision_id=decision_id or None, query=query, k=k)

    def confirm(self, decision_id: str, project_id: int, actor_id: str = "") -> dict:
        return self.svc.confirm(decision_id, project_id, actor_id)

    def remember(self, decision_id: str, content: str, durability: str = "") -> dict:
        return self.svc.remember(decision_id, content, durability=durability or None)

    def correct(self, decision_id: str, to_pid: int, actor_id: str = "",
                correction_event_id: str = "") -> dict:
        return self.svc.correct(decision_id, to_pid, actor_id,
                                correction_event_id=correction_event_id or None)


def build_mcp_server(tools: IsolaTools, name: str = "isola"):
    """注册 5 个模式 B 工具并返回 FastMCP 实例（懒加载 mcp SDK）。typed 参数 → 自动 JSON schema。"""
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP(name)

    @mcp.tool()
    def isola_route(text: str, chat_id: str, user_id: str, event_id: str,
                    platform_msg_id: str = "") -> dict:
        """判定一条消息归属哪个项目（模式 B，不执行、不投递）。
        高置信 → {status: ok, decision_id, project_id}；低置信 → {status: needs_confirmation,
        decision_id, candidates}（需再调 isola_confirm）。event_id 用于幂等去重。"""
        return tools.route(text, chat_id, user_id, event_id, platform_msg_id)

    @mcp.tool()
    def isola_recall(decision_id: str, query: str = "", k: int = 5) -> dict:
        """取回某条已归属（COMMITTED）decision 所属项目的记忆条目。仅按 decision_id
        （防绕过确认直取项目记忆）。返回 {status: ok, items:[{content,durability,hash}]} 或 invalid。"""
        return tools.recall(decision_id, query, k)

    @mcp.tool()
    def isola_confirm(decision_id: str, project_id: int, actor_id: str = "") -> dict:
        """对低置信 route 的归属做确认：把 decision 确认到 project_id 并坐实（COMMITTED），不投递。
        返回 {status: ok|invalid|unknown_project}。"""
        return tools.confirm(decision_id, project_id, actor_id)

    @mcp.tool()
    def isola_remember(decision_id: str, content: str, durability: str = "") -> dict:
        """把一条结论写回该 decision 所属项目的记忆（过信息量 / 敏感门）。一 decision 一记忆。
        durability 可选 stable|tentative。返回 {status: written|duplicate|skipped|invalid}。"""
        return tools.remember(decision_id, content, durability)

    @mcp.tool()
    def isola_correct(decision_id: str, to_pid: int, actor_id: str = "",
                      correction_event_id: str = "") -> dict:
        """纠正归属：把 decision 标 CORRECTED 并 retire 其记忆（模式 B 不重投——agent 自行重新
        route/remember 到正确项目）。correction_event_id 提供则走幂等。返回 {status: corrected|...}。"""
        return tools.correct(decision_id, to_pid, actor_id, correction_event_id)

    return mcp


def main(argv=None) -> None:
    """入口（console script `isola-mcp`）：装配模式 B MemoryService → 起 stdio MCP server。"""
    import argparse
    from .config import load_config, build_memory_service
    ap = argparse.ArgumentParser(prog="isola-mcp",
                                 description="Isola 模式 B 记忆服务（MCP server, stdio）")
    ap.add_argument("--config", default="config.yaml", help="配置文件路径（先 `isola init`）")
    args = ap.parse_args(argv)
    svc = build_memory_service(load_config(args.config))
    build_mcp_server(IsolaTools(svc)).run(transport="stdio")


if __name__ == "__main__":
    main()
