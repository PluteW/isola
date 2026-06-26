"""LLM 判定器后端。prompt 用实验定稿的朴素版（exp03：教规则有害）。

两种后端共用同一 OpenAI 兼容协议：
- 本地：ollama / vllm 的 openai-compat 端点（默认，真实消息不出机器）
- API：DeepSeek 等（手动开启，准确率档 ~92.5%）
"""
from __future__ import annotations
import re
import json
import subprocess
import urllib.request


def build_messages(text: str, projects: list, history: list) -> list:
    n = len(projects)
    ids = "、".join(str(p["id"]) for p in projects)
    sys_p = (f"你是消息归属判定器。一个聊天通道中同时进行着{n}个项目的对话。"
             "根据项目描述和带归属标注的历史消息，判断当前用户消息属于哪个项目。"
             "注意消息可能延续最近的话题，也可能切换到其他项目。"
             f"如果消息明显不属于任何现有项目、像是开启新工作，输出 0。"
             f"只输出项目编号（{ids} 或 0），不要输出其他内容。")
    plist = "\n".join(f"项目{p['id']}: {p['name']}——{p['desc']}" for p in projects)
    lines = []
    for h in history[-6:]:
        who = "用户" if h["role"] == "user" else "助手"
        lines.append(f"[项目{h['project_id']}] {who}: {h['text']}")
    usr_p = (f"项目列表：\n{plist}\n\n历史消息：\n" + ("\n".join(lines) or "（无）") +
             f"\n\n当前用户消息：{text}\n\n属于哪个项目？")
    return [{"role": "system", "content": sys_p}, {"role": "user", "content": usr_p}]


def parse_judge_output(out: str, projects: list) -> tuple[int | None, str]:
    """解析判定器原始输出 →（pid, raw）。纯函数，可离线测（T-UNIT-4）。
    语义：合法项目 id=该项目；0=新项目（显式区分）；None=无法解析/非法 id（无法判断）。"""
    out = re.sub(r"<think>.*?</think>", "", out or "", flags=re.S).strip()
    valid = {str(p["id"]) for p in projects} | {"0"}
    m = re.search(r"\d+", out)
    if m and m.group() in valid:
        return int(m.group()), out[:120]
    return None, out[:120]


class OpenAICompatJudge:
    def __init__(self, base_url: str, model: str, api_key: str = "none", timeout: int = 120):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def attribute(self, text: str, projects: list, history: list):
        """返回 (pid, raw)。超时/网络错 → (None, error)，由 router 降级为低置信确认卡。"""
        payload = json.dumps({
            "model": self.model,
            "messages": build_messages(text, projects, history),
            "temperature": 0,
            "max_tokens": 2048,   # 推理型模型需要思考预算（exp04）
        }).encode()
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=payload,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                out = json.load(resp)["choices"][0]["message"]["content"]
        except Exception as e:                       # 超时/网络/解析错 → 无法判断（降级）
            return None, f"error:{type(e).__name__}"
        return parse_judge_output(out, projects)


class ManualJudge:
    """手动 / 离线判定器：从不自动判定（始终无法判断）→ route 一律低置信、交确认。
    无需 LLM 端点 / api_key——让 isola-mcp 等服务在无 key 环境也能启动（U1）；归属由调用方 agent / 用户确认。"""

    def attribute(self, text, projects, history):
        return None, "manual:待确认"


class CLIJudge:
    """无 key 判定器：把判定 prompt 交给一个已登录的本地 CLI agent（如 `claude -p` / ` exec`）。
    command：命令前缀（prompt 作为最后一个参数追加），字符串或列表——如 "claude -p"、["","exec"]。
    复用同一判定 prompt（含 last-6 历史）；无需 api_key——无 key 环境也能自动归属（U1）。"""

    def __init__(self, command, timeout: int = 120):
        self.command = command.split() if isinstance(command, str) else list(command)
        self.timeout = int(timeout)

    def attribute(self, text, projects, history):
        sys_p, usr_p = (m["content"] for m in build_messages(text, projects, history))
        prompt = sys_p + "\n\n" + usr_p + "\n\n只输出一行 `答案: N`（N=项目编号或 0），别的都不要。"
        try:
            r = subprocess.run(self.command + [prompt], capture_output=True, text=True, timeout=self.timeout)
        except Exception as e:                        # 进程/超时错 → 无法判断（降级，route 转确认）
            return None, f"error:{type(e).__name__}"
        if r.returncode != 0:
            return None, f"error:rc={r.returncode}"
        m = re.search(r"答案\s*[:：]\s*(\d+)", r.stdout or "")
        if m and m.group(1) in ({str(p['id']) for p in projects} | {"0"}):
            return int(m.group(1)), (r.stdout or "").strip()[-80:]
        return parse_judge_output(r.stdout, projects)
