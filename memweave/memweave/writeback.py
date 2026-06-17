"""写入门策略（项目内沉淀，写入层设计 定的写入层重点）。

v0.1 门链：
  ① 信息量门（本模块 info_gate）：零信号/纯指代/空/极短不沉淀。
  ② 作用域门（store._commit_write_nc：须 COMMITTED + 有归属项目）。
  ③ 去重门（store._write_memory_nc：同项目 content_hash 精确去重）。
v0.2 记忆价值门（本模块 durability）：给沉淀内容打持久性标签（stable/tentative），
  区分"值得长期信赖" vs "试探/临时"——不硬删（防漏记），标签为后续检索降权/清理提供依据。
跨项目自动蒸馏降级、审核式跨项目复用（用户确认门 + provenance）留 v0.2。
v0.1 去重仅精确 hash；语义近重复合并留 v0.2。
"""
from __future__ import annotations
import re
from .router import is_low_signal
from .models import DUR_STABLE, DUR_TENTATIVE


def info_gate(text: str) -> bool:
    """信息量门：有实质内容才值得沉淀进项目记忆。
    依据 exp06——零信号消息（谢谢/继续/好的）归错无害且无沉淀价值。"""
    t = (text or "").strip()
    if not t:
        return False
    if is_low_signal(t):       # 复用判定层的零信号/指代识别（router）覆盖"谢谢/好的/继续"等
        return False
    # 不按长度挡：短但高价值指令（"禁GPU"/"别联网"）必须通过；
    # 极短无意义内容由去重门 + 记忆价值门兜底。
    return True


# 试探/假设/临时/否定结论标记——高精度词表（宁可漏标也不误降，见 durability docstring）
_TENTATIVE_RE = re.compile(
    r"可能|也许|大概|或许|估计|好像|似乎|不确定|不一定|说不定|我猜|"          # 不确定
    r"试试|试一下|试一试|看看能不能|要不要|能不能|可不可以|"                  # 试探
    r"假设|假如|"                                                          # 假设
    r"暂时|临时|先这样|回头再|稍后再|等下再|待会|"                          # 临时/过程
    r"行不通|没成功|失败了|算了")                                          # 否定结论


def durability(text: str) -> str:
    """记忆价值门（v0.2 第一版）：给沉淀内容打持久性标签。
    stable=可长期信赖（决策/事实/约束/确认偏好/可复用方法）；
    tentative=试探/假设/未定/临时/否定结论。

    保守策略：默认 stable，仅命中明确的试探/假设/临时/否定标记才降为 tentative。
    宁可漏标（把临时内容留作 stable，至多多记一条）也不误降（把稳定内容误标 tentative，
    将被后续检索降权/清理 = 漏记关键，代价更大）——与 info_gate 去 len<6 同一哲学。
    注：纯规则第一版，只识别显式标记；LLM 语义判定 + 基于标签的检索降权/清理留后续。"""
    t = (text or "").strip()
    if _TENTATIVE_RE.search(t):
        return DUR_TENTATIVE
    return DUR_STABLE


# 高确定性密钥格式（大小写敏感：sk- 必小写、AKIA 必大写）
_SECRET_FMT_RE = re.compile(
    r"sk-[A-Za-z0-9_-]{16,}"            # OpenAI / Anthropic 风格 key
    r"|gh[ps]_[A-Za-z0-9]{20,}"         # GitHub token
    r"|AKIA[0-9A-Z]{12,}"               # AWS access key id
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"    # Slack token
    r"|-----BEGIN[A-Z ]+PRIVATE KEY")   # PEM 私钥块
# 键值式凭据 + 敏感路径/文件（大小写不敏感）
_SECRET_KV_RE = re.compile(
    r"(?:api[_-]?key|secret|passw(?:or)?d|access[_-]?token|auth[_-]?token)\s*[:=]\s*\S{4,}"
    r"|bearer\s+[A-Za-z0-9._-]{16,}"
    r"|/\.ssh/|/\.aws/credentials|\.env\b",
    re.IGNORECASE)


def sensitive_gate(text: str) -> bool:
    """敏感信息门：含疑似凭据/密钥/私钥/敏感路径 → True（拦截，不沉淀）。
    方向与 info_gate/durability 相反——把密钥写进长期记忆的风险 >> 漏记一条，
    故宁可多拦（高 recall，可接受误杀真含凭据的消息）；只识别"像真凭据"的模式
    （赋值+长随机串/已知 key 前缀/私钥块），不拦仅提及"token/api key"等词的正常讨论。
    v0.1：命中即整条不沉淀；脱敏保留（redact 后写）留后续。"""
    t = text or ""
    return bool(_SECRET_FMT_RE.search(t) or _SECRET_KV_RE.search(t))
