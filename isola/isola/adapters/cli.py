"""CLIChannel：最小真实 ChannelAdapter（替代飞书，用于本机端到端真跑）。
入站=脚本/文件喂的 InboundMessage；出站=打印到终端。SDD §4 ChannelAdapter 契约。"""
from __future__ import annotations
from isola.models import InboundMessage


class CLIChannel:
    def __init__(self, verbose=True):
        self.verbose = verbose
        self._n = 0

    def parse_inbound(self, raw):
        return raw if isinstance(raw, InboundMessage) else InboundMessage(**raw)

    def send_scoped_card(self, chat_id, text, scope_label, decision_id):
        self._n += 1
        cid = f"card{self._n}"
        if self.verbose:
            print(f"    └─[{scope_label}] {text[:90]}")
        return cid

    def send_confirm_card(self, chat_id, text, projects, decision_id):
        self._n += 1
        cid = f"confirm{self._n}"
        if self.verbose:
            names = " / ".join(f"{p['id']}.{p['name']}" for p in projects)
            print(f"    ❓ 低置信，请确认归属：{names}")
        return cid

    def update_card(self, card_msg_id, new_state):
        if self.verbose:
            print(f"    ✎ 卡片 {card_msg_id} → {new_state}")

    def parse_correction(self, raw):
        return raw
