"""四路由判定器——exp01–04 实验定稿的归属架构。

路由顺序与依据：
1. cross_ref 检测（提及他项目名 + 引用动词共现）→ 按惯性项目投递 + 引用标注
   （exp02：误判 100% 被提及项目吸走；exp04：检测后路由使组合 +4~6pt）
2. 低信号检测（零信号/指代）→ 惯性规则
   （惯性在该类上稳定优于一切模型，含推理型：94%/75% vs 61–72%）
3. 其余 → LLM 判定器（朴素 prompt——规则 prompt 已被 exp03 证伪有害）
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field


# 引用动词/句式：与他项目名共现时判为跨项目引用（exp02 cross_ref 语料归纳，
# 实地检出率是效度威胁 #1，靠归属日志持续校准）
REF_PATTERNS = [
    r"参考", r"按照?.{0,24}(办法|格式|结构|框架|流程|样式|章节)", r"搬过来", r"套用",
    r"借用", r"照着", r"那套", r"同样的", r"也按", r"复用", r"对齐",
]
REF_RE = re.compile("|".join(REF_PATTERNS))

# 零信号：短确认/致谢，无疑问、无实质内容（exp02 zero 类）
ZERO_RE = re.compile(r"谢谢|再见|好的|没有?问题|收到|嗯|可以|辛苦|拜拜|👌|好嘞|ok|OK")
# 指代延续：依赖上文才有所指（exp02 anaphor 类）
ANAPHOR_RE = re.compile(
    r"^(继续|接着|往下|然后呢)|那个.{0,10}(怎么样|进度|补完|好了)|上次(说|提)的|刚才(说|提)的|昨天的进度")


@dataclass
class RouteDecision:
    route: str                     # cross_ref | inertia | judge
    project_id: int | None         # 归属项目（None=交由判定器后仍无法判定）
    referenced_id: int | None = None   # cross_ref 时被引用的项目
    confidence: str = "high"       # high | low
    reason: str = ""
    judge_raw: str = ""


def is_low_signal(text: str) -> bool:
    t = (text or "").strip()
    if ANAPHOR_RE.search(t):
        return True
    return len(t) < 25 and bool(ZERO_RE.search(t)) and "？" not in t and "?" not in t


def detect_cross_ref(text: str, projects: list, current_id: int | None) -> int | None:
    """返回被引用的他项目 id；不构成引用则返回 None。

    两种触发：①《项目名》书名号显式提及 ②项目名明文出现且与引用动词共现。
    仅当被提及项目 != 当前项目时才算跨项目引用。
    """
    text = text or ""                       # 防御：纯图片/语音消息 text 可能为 None
    for p in projects:
        if p["id"] == current_id:
            continue
        name = p["name"]
        if f"《{name}》" in text:
            return p["id"]
        if name in text and REF_RE.search(text):
            return p["id"]
    return None


class Router:
    def __init__(self, judge=None):
        self.judge = judge  # 实现 .attribute(text, projects, history) -> (pid|None|0, raw)

    def route(self, text: str, projects: list, last_project_id: int | None,
              history: list | None = None) -> RouteDecision:
        text = text or ""                   # 防御：text 为 None 时按空消息处理，不炸穿判定层
        if not projects:
            return RouteDecision("judge", None, confidence="low", reason="注册表为空，待建项目")

        ref = detect_cross_ref(text, projects, last_project_id)
        if ref is not None and last_project_id is not None:
            return RouteDecision("cross_ref", last_project_id, referenced_id=ref,
                                 reason=f"引用了项目{ref}的材料，归属当前项目{last_project_id}")

        if is_low_signal(text):
            if last_project_id is not None:
                return RouteDecision("inertia", last_project_id, reason="低信号消息，延续最近项目")
            return RouteDecision("judge", None, confidence="low", reason="低信号且无惯性上下文")

        if self.judge is None:
            return RouteDecision("judge", last_project_id, confidence="low", reason="判定器未配置，暂用惯性")
        pid, raw = self.judge.attribute(text, projects, history or [])
        if pid == 0:   # 判定器认为不属于任何现有项目 → 新项目提议（实验未覆盖，靠日志校准）
            return RouteDecision("judge", None, confidence="low",
                                 reason="疑似新项目，待用户确认", judge_raw=raw)
        if pid is None:
            return RouteDecision("judge", last_project_id, confidence="low",
                                 reason="判定器输出无法解析，暂用惯性", judge_raw=raw)
        return RouteDecision("judge", pid, reason="语义判定", judge_raw=raw)
