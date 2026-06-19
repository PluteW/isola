"""就绪自检（分发层 v1，doctor 命令）。

铁律：默认**只读** —— 绝不写 store、绝不 dispatch、绝不建库。
唯一例外：`--emit-wrapper` 是用户显式请求的写动作（只写一个 OpenClaw wrapper 文件，
见 emit_wrapper_if_requested）；`run` 与各 check 全程只读。
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


# 每个 check：接 (config_path, ctx) → (pass: bool, evidence: str, requires_human: bool)
# ctx 透传 doctor CLI 选项（openclaw_dir / node_path / …）；多数 check 不用，仅 _harness 用。
def _py(_, ctx):
    v = sys.version_info
    return v >= (3, 9), f"python {v.major}.{v.minor}.{v.micro}", False


def _yaml(_, ctx):
    try:
        import yaml  # noqa: F401
        return True, "PyYAML 可用", False
    except ImportError:
        return False, "缺 PyYAML", False


def _config(cfg_path, ctx):
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


def _judge(cfg_path, ctx):
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


# ---- OpenClaw 入口探测（E3）：两轮；探测兜底，不改 adapter 契约 ----
_MJS = "openclaw.mjs"


def _executable(p):
    return bool(p) and os.path.isfile(p) and os.access(p, os.X_OK)


def _find_mjs(root, max_depth=4):
    """在 root 下有限层搜 openclaw.mjs（不排除 node_modules）。返回所有匹配（排序）。"""
    root = pathlib.Path(root).expanduser()
    if not root.is_dir():
        return []
    base = len(root.parts)
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        if len(pathlib.Path(dirpath).parts) - base > max_depth:
            dirnames[:] = []                      # 限深剪枝，防深递归卡顿
            continue
        if _MJS in filenames:
            hits.append(str(pathlib.Path(dirpath) / _MJS))
    return sorted(hits)


def _probe_openclaw(binary, openclaw_dir=None):
    """第一轮找可执行入口（which 已覆盖 PATH 中的全局 npm bin）；
    没有且给了 openclaw_dir → 第二轮在该目录搜 .mjs。
    返回 {kind: 'binary'|'mjs'|'none', path, via?, all?}。"""
    if binary:
        p = shutil.which(binary) or (binary if _executable(binary) else None)
        if p:
            return {"kind": "binary", "path": p, "via": "配置 binary"}
    p = shutil.which("openclaw")
    if p:
        return {"kind": "binary", "path": p, "via": "PATH"}
    nb = os.path.join(os.getcwd(), "node_modules", ".bin", "openclaw")
    if _executable(nb):
        return {"kind": "binary", "path": nb, "via": "node_modules/.bin"}
    if openclaw_dir:
        hits = _find_mjs(openclaw_dir)
        if hits:
            return {"kind": "mjs", "path": hits[0], "all": hits}
    return {"kind": "none"}


def _probe_node(node_path=None):
    if node_path and _executable(node_path):
        return node_path
    return shutil.which("node")


def wrapper_template(mjs, node):
    node = node or "<在此填 node 路径（如 conda 环境的 node）>"
    return f'#!/bin/sh\nexec "{node}" "{mjs}" "$@"\n'


def _harness(cfg_path, ctx):
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
        pr = _probe_openclaw(h.get("binary", "openclaw"), ctx.get("openclaw_dir"))
        if pr["kind"] == "binary":
            tip = "" if pr["via"] == "配置 binary" else "；建议把 harness.binary 配成该路径"
            # 入口在，但 provider/model 注册无非交互命令可确证 → 诚实人工停点
            return False, (f"OpenClaw 入口：{pr['path']}（via {pr['via']}）{tip}"
                           "；provider/model 是否注册需人工确认"), True
        if pr["kind"] == "mjs":
            node = _probe_node(ctx.get("node_path"))
            more = "" if len(pr["all"]) == 1 else f"（共 {len(pr['all'])} 个取第一个；其余：{', '.join(pr['all'][1:])}）"
            node_tip = f"node={node}" if node else "未探到 node，需 --node-path 或手填"
            return False, (f"找到 {pr['path']}{more}；.mjs 不可直接执行，需造 wrapper（{node_tip}）"
                           "并把 harness.binary 指向它（加 --emit-wrapper 可自动生成 scripts/openclaw-bin）"), True
        return False, ("未找到 OpenClaw 入口；装 openclaw，或 "
                       "`isola doctor --openclaw-dir <你的 OpenClaw 目录>` 补路径再探一轮"), False
    return False, f"未知 harness type: {t!r}", False


_STEPS = [
    (Step("python", "Python ≥ 3.9", "python3 --version", "安装/切换到 Python 3.9+"), _py),
    (Step("pyyaml", "PyYAML 已装", "python3 -c 'import yaml'", "pip install -e ."), _yaml),
    (Step("config", "配置存在且合法", "isola doctor", "isola init 后填写 config.yaml"), _config),
    (Step("judge", "判定器端点连通", "", "检查 judge.base_url 与网络；设置 JUDGE_API_KEY 环境变量"), _judge),
    (Step("harness", "执行后端就绪", "", "llm_direct: 检查 base_url；openclaw: 入口探测见 evidence（必要时 --openclaw-dir）"), _harness),
]


def run(config_path, ctx=None):
    """跑所有 Step（**只读**）。ctx 透传 CLI 选项。返回机器可读结果列表。"""
    ctx = ctx or {}
    results = []
    for step, fn in _STEPS:
        try:
            ok, evidence, need_human = fn(config_path, ctx)
        except Exception as e:                       # 自检本身不应炸穿
            ok, evidence, need_human = False, f"检查异常: {type(e).__name__}: {e}", False
        status = "pass" if ok else ("need_human" if need_human else "fail")
        results.append({
            "id": step.id, "title": step.title, "status": status,
            "check_command": step.check_command, "fix_command": step.fix_command,
            "requires_human": need_human, "evidence": evidence,
        })
    return results


def emit_wrapper_if_requested(cfg_path, ctx):
    """`--emit-wrapper`（用户**显式**写动作）：探到 .mjs 则写 ./scripts/openclaw-bin。
    返回 (path|None, msg)。默认（无 --emit-wrapper）命令层不调用此函数 → doctor 保持只读。"""
    from .config import load_config, ConfigError
    try:
        h = load_config(cfg_path).harness
    except ConfigError as e:
        return None, f"config 不可用: {e}"
    if h.get("type") != "openclaw":
        return None, "harness 非 openclaw，无需 wrapper"
    pr = _probe_openclaw(h.get("binary", "openclaw"), ctx.get("openclaw_dir"))
    if pr["kind"] == "binary":
        return None, f"已有可执行入口 {pr['path']}，无需 wrapper"
    if pr["kind"] != "mjs":
        return None, "未找到 .mjs（先用 --openclaw-dir 补 OpenClaw 目录）"
    node = _probe_node(ctx.get("node_path"))
    dst = pathlib.Path(os.getcwd()) / "scripts" / "openclaw-bin"
    if dst.exists() and not ctx.get("force"):
        return None, f"{dst} 已存在；加 --force 覆盖"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(wrapper_template(pr["path"], node), encoding="utf-8")
    os.chmod(dst, 0o755)
    note = "" if node else "（wrapper 里 node 是占位，请手填后再用）"
    return str(dst), f"已生成 {dst} 并 chmod +x{note}；把 harness.binary 指向它"


def render(results):
    sym = {"pass": "✓", "fail": "✗", "need_human": "⚠"}
    for r in results:
        print(f"  {sym.get(r['status'], '?')} [{r['status']:10}] {r['title']}: {r['evidence']}")
        if r["status"] != "pass" and r["fix_command"]:
            print(f"      fix: {r['fix_command']}")
