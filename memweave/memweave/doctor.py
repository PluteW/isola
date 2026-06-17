"""就绪自检（分发层 v1，doctor 命令；内部设计）。

铁律：**只读** —— 绝不写 store、绝不 dispatch、绝不建库。
Step 列表 + runner；`doctor --json` 输出机器可读固定字段，供 agent 自主安装逐步判定。
"""
from __future__ import annotations
import sys
import os
import socket
import shutil
import pathlib
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass
class Step:
    id: str
    title: str
    check_command: str
    fix_command: str


# 每个 check：接 config_path → (pass: bool, evidence: str, requires_human: bool)
def _py(_):
    v = sys.version_info
    return v >= (3, 9), f"python {v.major}.{v.minor}.{v.micro}", False


def _yaml(_):
    try:
        import yaml  # noqa: F401
        return True, "PyYAML 可用", False
    except ImportError:
        return False, "缺 PyYAML", False


def _config(cfg_path):
    from .config import load_config, ConfigError
    if not pathlib.Path(cfg_path).expanduser().exists():
        return False, f"{cfg_path} 不存在", False
    try:
        load_config(cfg_path)
        return True, "config 合法", False
    except ConfigError as e:
        return False, str(e), False


def _tcp(url, timeout=3.0):
    u = urlparse(url)
    if not u.hostname:
        return False, f"无法解析 host: {url!r}"
    port = u.port or (443 if u.scheme == "https" else 80)
    try:
        with socket.create_connection((u.hostname, port), timeout=timeout):
            return True, f"{u.hostname}:{port} 可连通"
    except Exception as e:
        return False, f"{u.hostname}:{port} 连不上（{type(e).__name__}）"


def _judge(cfg_path):
    from .config import load_config, ConfigError
    try:
        cfg = load_config(cfg_path)
    except ConfigError as e:
        return False, f"config 不可用: {e}", False
    env = cfg.judge.get("api_key_env")
    if env and not os.environ.get(env):
        return False, f"环境变量 {env} 未设置（judge 需要 api_key）", False
    ok, ev = _tcp(cfg.judge.get("base_url", ""))
    return ok, ev, False


def _harness(cfg_path):
    from .config import load_config, ConfigError
    try:
        cfg = load_config(cfg_path)
    except ConfigError as e:
        return False, f"config 不可用: {e}", False
    h = cfg.harness
    t = h.get("type")
    if t == "llm_direct":
        env = h.get("api_key_env")
        if env and not os.environ.get(env):
            return False, f"环境变量 {env} 未设置（llm_direct 需要 api_key）", False
        ok, ev = _tcp(h.get("base_url", ""))
        return ok, ev, False
    if t == "openclaw":
        binary = h.get("binary", "openclaw")
        if not shutil.which(binary):
            return False, f"`{binary}` 不在 PATH", False
        # provider/model 注册无非交互命令可确证 → 诚实人工停点（不假装自动）
        return False, f"`{binary}` 在 PATH；provider/model 是否注册需人工确认", True
    return False, f"未知 harness type: {t!r}", False


_STEPS = [
    (Step("python", "Python ≥ 3.9", "python3 --version", "安装/切换到 Python 3.9+"), _py),
    (Step("pyyaml", "PyYAML 已装", "python3 -c 'import yaml'", "pip install -r requirements.txt"), _yaml),
    (Step("config", "配置存在且合法", "python -m memweave doctor", "python -m memweave init 后填写 config.yaml"), _config),
    (Step("judge", "判定器端点连通", "", "检查 judge.base_url 与网络；设置 JUDGE_API_KEY 环境变量"), _judge),
    (Step("harness", "执行后端就绪", "", "llm_direct: 检查 base_url；openclaw: 注册 provider/model（见 AGENTS.md）"), _harness),
]


def run(config_path):
    """跑所有 Step（只读）。返回机器可读结果列表。"""
    results = []
    for step, fn in _STEPS:
        try:
            ok, evidence, need_human = fn(config_path)
        except Exception as e:                       # 自检本身不应炸穿
            ok, evidence, need_human = False, f"检查异常: {type(e).__name__}: {e}", False
        status = "pass" if ok else ("need_human" if need_human else "fail")
        results.append({
            "id": step.id, "title": step.title, "status": status,
            "check_command": step.check_command, "fix_command": step.fix_command,
            "requires_human": need_human, "evidence": evidence,
        })
    return results


def render(results):
    sym = {"pass": "✓", "fail": "✗", "need_human": "⚠"}
    for r in results:
        print(f"  {sym.get(r['status'], '?')} [{r['status']:10}] {r['title']}: {r['evidence']}")
        if r["status"] != "pass" and r["fix_command"]:
            print(f"      fix: {r['fix_command']}")
