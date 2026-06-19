"""Isola CLI 入口（分发层 v1「CLI 同步档」）。

子命令 init / doctor / chat，argparse subparsers + 函数 map 分派（Python idiom，非命令类）。
不含 serve/HTTP（v0.2 experimental）。用法见 AGENTS.md。
"""
from __future__ import annotations
import argparse
import json
import sys
import time
import uuid
import pathlib

DEFAULT_CONFIG = "config.yaml"


def cmd_init(args):
    src = pathlib.Path(__file__).parent / "config.example.yaml"   # 包内数据（pip 安装后亦可定位）
    dst = pathlib.Path(args.path)
    dst.parent.mkdir(parents=True, exist_ok=True)           # nested 路径父目录不存在则创建
    if dst.exists():
        print(f"已存在 {dst}，未覆盖。"); return 0
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"已生成 {dst}。请填写 base_url/model，并设置 *_api_key_env 指向的环境变量，再运行 `doctor`。")
    return 0


def cmd_doctor(args):
    from .doctor import run, render, emit_wrapper_if_requested
    ctx = {"openclaw_dir": args.openclaw_dir, "node_path": args.node_path,
           "emit_wrapper": args.emit_wrapper, "force": args.force}
    results = run(args.config, ctx)                 # run 与各 check 全程只读
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        render(results)
    if args.emit_wrapper:                           # 唯一写动作：用户显式请求时才生成 wrapper
        _, msg = emit_wrapper_if_requested(args.config, ctx)
        # --json 时走 stderr，保持 stdout 为纯 results JSON（不破坏 agent 解析）
        print(f"  emit-wrapper: {msg}", file=sys.stderr if args.json else sys.stdout)
    if any(r["status"] == "fail" for r in results):
        return 1
    if any(r["status"] == "need_human" for r in results):
        return 2          # 需人工处理（如 OpenClaw 注册）→ 非零，防自动化脚本误判全就绪
    return 0


def cmd_chat(args):
    from .config import load_config, build_core
    from .models import InboundMessage
    core = build_core(load_config(args.config))
    base = time.time()

    def handle(text, i):
        msg = InboundMessage(msg_id=uuid.uuid4().hex, event_id=uuid.uuid4().hex,
                             platform_msg_id=uuid.uuid4().hex, user_id="cli",
                             text=text, chat_id="cli", platform_ts=int(base))
        r = core.handle_message(msg, now=base + i)
        print(f"  → 项目 {r.get('project_id')} [{r['status']}] {r.get('reply', '')}")
        return r

    if args.text is not None:
        if not args.text.strip():                          # 空/纯空白消息不投递
            print("空消息，未处理。", file=sys.stderr); return 2
        handle(args.text, 0)
    else:
        print("Isola chat（输入消息，空行或 Ctrl-D 退出）")
        i = 0
        while True:
            try:
                line = input("> ").strip()
            except EOFError:
                print(); break
            if not line:
                break
            handle(line, i)
            i += 1
    core.tick(now=base + 10 ** 9)        # 触发到期写入（同步档：结束时落定记忆）
    return 0


_COMMANDS = {"init": cmd_init, "doctor": cmd_doctor, "chat": cmd_chat}


def build_parser():
    p = argparse.ArgumentParser(prog="isola",
                                description="单入口多项目作用域记忆路由（v1 CLI 同步档）")
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("init", help="生成 config 模板")
    pi.add_argument("--path", default=DEFAULT_CONFIG, help="目标配置路径")
    pd = sub.add_parser("doctor", help="只读就绪自检（--emit-wrapper 时显式写一个 wrapper）")
    pd.add_argument("--config", default=DEFAULT_CONFIG)
    pd.add_argument("--json", action="store_true", help="机器可读输出")
    pd.add_argument("--openclaw-dir", default=None, help="补 OpenClaw 安装目录，第二轮搜 openclaw.mjs")
    pd.add_argument("--node-path", default=None, help="指定 node 可执行（wrapper 用；conda node 不在 PATH 时）")
    pd.add_argument("--emit-wrapper", action="store_true", help="显式生成 scripts/openclaw-bin（默认只打印模板、只读）")
    pd.add_argument("--force", action="store_true", help="配合 --emit-wrapper：覆盖已存在的 wrapper")
    pc = sub.add_parser("chat", help="同步处理消息（路由→投递→落记忆）")
    pc.add_argument("--config", default=DEFAULT_CONFIG)
    pc.add_argument("--text", default=None, help="单条消息；省略则进入交互循环")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    import sqlite3
    import yaml
    from .config import ConfigError
    try:
        return _COMMANDS[args.cmd](args)
    except KeyboardInterrupt:
        print(); return 130
    except (ConfigError, OSError, sqlite3.Error, yaml.YAMLError) as e:   # 预期错误给一行提示，不露 traceback
        print(f"错误: {e}", file=sys.stderr); return 2


if __name__ == "__main__":
    sys.exit(main())
