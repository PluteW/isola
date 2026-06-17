"""OpenClawAdapter 单元测试（fake runner，离线）——验证命令构造/JSON解析/fallback防分叉/幂等/错误。
守护 形态决策 harness 无关：OpenClawAdapter 满足与 LLMHarness 相同的 HarnessAdapter 契约。"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from memweave.adapters.openclaw import OpenClawAdapter, _last_json


class _R:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode; self.stdout = stdout; self.stderr = stderr


def _fake_runner(captured):
    def run(cmd, timeout, env):
        captured["cmd"] = cmd; captured["timeout"] = timeout
        return captured["ret"]
    return run


def test_command_construction_session_key():
    cap = {"ret": _R(stdout='{"payloads":[{"text":"ok"}]}')}
    a = OpenClawAdapter("openclaw", agent="research", model="qwen", runner=_fake_runner(cap))
    a.dispatch("proj:7", "查甲公司", idempotency_key="d1")
    cmd = cap["cmd"]
    assert "agent" in cmd and "--local" in cmd
    assert cmd[cmd.index("--agent") + 1] == "research"        # 角色=agent
    assert cmd[cmd.index("--session-key") + 1] == "proj:7"    # 项目=session key 后缀
    assert cmd[cmd.index("--message") + 1] == "查甲公司"
    assert "--json" in cmd and cmd[cmd.index("--model") + 1] == "qwen"


def test_parse_payloads_reply():
    cap = {"ret": _R(stdout='{"sessionId":"s9","payloads":[{"text":"第一段"},{"text":"第二段"}]}')}
    a = OpenClawAdapter("openclaw", runner=_fake_runner(cap))
    r = a.dispatch("proj:1", "x", idempotency_key="d1")
    assert r["ok"] and r["reply"] == "第一段\n第二段" and r["turn_id"] == "s9"


def test_diagnostic_lines_then_json():
    """OpenClaw 真实输出：诊断行在前、JSON 在后 → 提取最后一个 JSON。"""
    out = ('[diagnostic] lane task start\n'
           '[model-fallback] decision ...\n'
           '{"payloads":[{"text":"已处理"}],"meta":{}}')
    cap = {"ret": _R(stdout=out)}
    a = OpenClawAdapter("openclaw", runner=_fake_runner(cap))
    r = a.dispatch("proj:1", "x", idempotency_key="d1")
    assert r["ok"] and r["reply"] == "已处理"


def test_fallback_fork_rejected():
    cap = {"ret": _R(stdout='{"payloads":[{"text":"x"}],"meta":{"fallbackFrom":"gateway-fallback-abc"}}')}
    a = OpenClawAdapter("openclaw", runner=_fake_runner(cap))
    r = a.dispatch("proj:1", "x", idempotency_key="d1")
    assert r["ok"] is False and "fallback" in r["error"]      # 分叉不投递


def test_idempotent():
    calls = {"n": 0}
    def run(cmd, timeout, env):
        calls["n"] += 1
        return _R(stdout='{"payloads":[{"text":"ok"}]}')
    a = OpenClawAdapter("openclaw", runner=run)
    a.dispatch("proj:1", "x", idempotency_key="dup")
    a.dispatch("proj:1", "x", idempotency_key="dup")
    assert calls["n"] == 1                                    # 同 key 只真投一次


def test_nonzero_returncode_error():
    cap = {"ret": _R(returncode=1, stderr="boom")}
    a = OpenClawAdapter("openclaw", runner=_fake_runner(cap))
    r = a.dispatch("proj:1", "x", idempotency_key="d1")
    assert r["ok"] is False and "boom" in r["error"]


def test_no_json_error():
    cap = {"ret": _R(stdout="只有诊断没有json")}
    a = OpenClawAdapter("openclaw", runner=_fake_runner(cap))
    r = a.dispatch("proj:1", "x", idempotency_key="d1")
    assert r["ok"] is False and "no parsable JSON" in r["error"]


def test_last_json_helper():
    assert _last_json('{"a":1}')["a"] == 1
    assert _last_json('noise\n{"a":1}\n{"b":2}')["b"] == 2
    assert _last_json("no json here") is None


def test_contract_compatible_with_core():
    """契约一致性：OpenClawAdapter 能直接替换 LLMHarness 喂给 core（鸭子类型）。"""
    cap = {"ret": _R(stdout='{"payloads":[{"text":"reply"}]}')}
    a = OpenClawAdapter("openclaw", runner=_fake_runner(cap))
    assert hasattr(a, "ensure_session") and hasattr(a, "dispatch") and hasattr(a, "reset_session")
    res = a.dispatch("proj:1", "x", idempotency_key="d1")
    assert set(res) >= {"ok", "reply", "turn_id", "meta"}    # 与 LLMHarness 返回结构一致


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"PASS {name}")
            except AssertionError as e:
                fails += 1; print(f"FAIL {name}: {e}")
            except Exception as e:
                fails += 1; print(f"ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{'ALL GREEN' if not fails else str(fails)+' FAILED'}")
    sys.exit(1 if fails else 0)
