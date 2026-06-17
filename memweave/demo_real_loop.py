#!/usr/bin/env python3
"""第一条真实端到端：真 judge + 真 harness（都接 DeepSeek）+ CLI 入口，
喂真实交错消息，真跑 ①→⑧，看归属准确率 + 记忆是否按项目隔离。
零飞书、零用户交互。用法: DEEPSEEK_API_KEY=... python3 demo_real_loop.py
"""
import os, sys, time, tempfile, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))

from memweave.store import Store
from memweave.registry import Registry
from memweave.router import Router
from memweave.judge import OpenAICompatJudge
from memweave.core import MemWeaveCore
from memweave.models import InboundMessage
from memweave.adapters.cli import CLIChannel
from memweave.adapters.llm_harness import LLMHarness

KEY = os.environ["DEEPSEEK_API_KEY"]
DS = "https://api.deepseek.com"
T0 = 1_000_000.0

# 真实交错消息序列（gold = 期望归属项目）。1=法律调研 2=股票尽调 3=技术周报
SEQ = [
    (1, "开始合同审查Agent的调研，先列一下国内外主要产品和核心能力"),
    (1, "Harvey和幂律智能的部署方式和数据安全方案对比一下"),
    (2, "另外启动甲公司的尽调，先看近三年营收和毛利率变化"),
    (2, "他们前五大客户有哪些，集中度高不高"),
    (3, "这周的AI技术周报开始整理，先收集本周大模型发布和重要论文"),
    (3, "继续"),                                                    # 指代 → 惯性应=3
    (2, "把甲公司的估值算一下，对标公司选哪几家"),                      # 切回股票
    (2, "参考《法律合同调研》的资料整理框架，把这边尽调材料也归一下"),  # cross_ref → 归2，引用1
    (1, "法律那边，OCR和Word解析有哪些成熟开源方案"),                  # 切回法律
]


def main():
    store = Store()
    reg = Registry(tempfile.mktemp(suffix=".yaml"))
    reg.add("法律合同调研", "调研某类产品调研：竞品、形态、部署与数据安全")
    reg.add("甲公司尽调", "甲公司公司级尽调：财务、客户、订单、产能与估值")
    reg.add("AI技术周报", "每周 AI 技术动态整理：模型发布、论文、开源热榜")
    judge = OpenAICompatJudge(DS, "deepseek-chat", KEY)
    harness = LLMHarness(DS, "deepseek-chat", KEY)
    core = MemWeaveCore(store, reg, Router(judge), CLIChannel(verbose=True),
                        harness, isolation_s=300, now_fn=lambda: T0)

    print("=" * 70)
    print("真实端到端：DeepSeek judge + DeepSeek harness（项目隔离 session）")
    print("=" * 70)
    correct = confirmed = 0
    for i, (gold, text) in enumerate(SEQ):
        ev = InboundMessage(msg_id=f"m{i}", event_id=f"e{i}", platform_msg_id=f"pm{i}",
                            user_id="u1", text=text, chat_id="c1", platform_ts=int(T0))
        print(f"\n[{i+1}] (期望→{gold}) {text}")
        r = core.handle_message(ev, now=T0 + i)
        if r["status"] == "awaiting_confirmation":
            confirmed += 1
            print(f"    → 低置信，用户确认归属 {gold}")
            r = core.handle_confirm(r["decision_id"], confirmed_pid=gold, now=T0 + i)
        pred = r.get("project_id")
        ok = "✓" if pred == gold else "✗"
        ref = f" 引用→{r.get('referenced_id')}" if r.get("referenced_id") else ""
        print(f"    判定→项目{pred} {ok}{ref}  [{r['status']}]")
        if pred == gold:
            correct += 1

    core.tick(now=T0 + 9999)   # 隔离期到期 → 提交写入项目记忆
    n = len(SEQ)
    print("\n" + "=" * 70)
    print(f"归属：{correct}/{n} 符合预期（其中 {confirmed} 条低置信经确认）")
    print("记忆隔离检查（每项目召回，应只含本项目内容）：")
    for pid, name in [(1, "法律调研"), (2, "股票尽调"), (3, "技术周报")]:
        items = store.recall({"level": "project", "project_id": pid}, k=20)
        print(f"  项目{pid} {name}：{len(items)} 条记忆")
        for it in items:
            print(f"      · {it['content'][:46]}")
    print(f"\n真相源：decisions={store.count('route_decisions')} "
          f"memory={store.count('memory_items')} write_jobs={store.count('write_jobs')}")
    print("=" * 70)


if __name__ == "__main__":
    main()
