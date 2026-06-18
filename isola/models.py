"""数据模型（SDD v2 §4 接口契约）。纯 dataclass，无外部依赖。"""
from __future__ import annotations
from dataclasses import dataclass, field

# 状态常量（SDD §5；DB 字段状态机，非对象状态模式）
WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
TENTATIVE = "TENTATIVE"
COMMITTED = "COMMITTED"
CORRECTED = "CORRECTED"
ACTIVE_STATES = (WAITING_CONFIRMATION, TENTATIVE, COMMITTED)  # 主路径单活跃不变量用

# dispatch_state
DISP_PENDING = "pending"   # 尚未 dispatch（如等确认）
DISP_SENT = "sent"         # 已 dispatch
DISP_FAILED = "failed"

# write_job status
JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_CANCELLED = "cancelled"
JOB_FAILED = "failed"

# memory durability（v0.2 记忆价值门标签；区分"值得长期信赖" vs "试探/临时"）
DUR_STABLE = "stable"        # 决策/事实/约束/确认偏好/可复用方法
DUR_TENTATIVE = "tentative"  # 试探/假设/未定/临时/否定结论


@dataclass
class InboundMessage:
    msg_id: str
    event_id: str
    platform_msg_id: str
    user_id: str
    text: str
    chat_id: str
    platform_ts: int
    reply_to_msg_id: str | None = None
    thread_id: str | None = None
    mentions: list = field(default_factory=list)
    attachments: list = field(default_factory=list)


@dataclass
class JudgeResult:
    project_id: int | None          # None = 无法判断
    is_new_project: bool            # 与"无法判断"显式区分（SDD §4 / T-UNIT-4）
    confidence: float
    raw: str
    latency_ms: int
    error: str | None = None


@dataclass
class RouteDecision:
    decision_id: str
    msg_id: str
    route_type: str                 # cross_ref|inertia|judge|new_project|unknown
    project_id: int | None
    referenced_id: int | None = None
    confidence_score: float = 1.0
    needs_confirmation: bool = False
    judge_raw: str = ""
    latency_ms: int = 0
    state: str = TENTATIVE
    dispatch_state: str = DISP_PENDING
    card_msg_id: str | None = None
    parent_decision_id: str | None = None


@dataclass
class MemoryItem:
    scope_level: str                # project|global（global 只读约束，不自动写）
    project_id: int | None
    content: str
    source_msg_id: str
    decision_id: str
    content_hash: str = ""
    state: str = "active"           # active|retired；recall 仅取 active
    version: int = 1
    durability: str = "stable"      # stable|tentative（记忆价值门，默认 stable 防漏记）
