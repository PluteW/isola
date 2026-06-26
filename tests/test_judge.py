"""判定器解析逻辑测试（T-UNIT-4）：0=新项目 与 None=无法判断 显式区分。
纯函数离线测，不调真模型。"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from isola.judge import parse_judge_output, ManualJudge, CLIJudge

P = [{"id": 1, "name": "甲公司尽调", "desc": ""},
     {"id": 2, "name": "乙公司尽调", "desc": ""}]


def test_explicit_project():
    assert parse_judge_output("2", P)[0] == 2


def test_embedded_number():
    assert parse_judge_output("应该是项目1", P)[0] == 1


def test_new_project_zero():
    assert parse_judge_output("0", P)[0] == 0          # 新项目（与无法判断区分）


def test_unparseable_none():
    assert parse_judge_output("我不太确定", P)[0] is None   # 无法判断


def test_invalid_id_none():
    assert parse_judge_output("5", P)[0] is None       # 非法 id（不在 projects 且非 0）


def test_zero_vs_none_distinct():
    """核心：0（新项目）与 None（无法判断）必须不同（SDD §4）。"""
    assert parse_judge_output("0", P)[0] == 0
    assert parse_judge_output("xyz", P)[0] is None
    assert parse_judge_output("0", P)[0] != parse_judge_output("xyz", P)[0]


def test_strips_think():
    assert parse_judge_output("<think>纠结半天</think>1", P)[0] == 1


def test_empty():
    assert parse_judge_output("", P)[0] is None
    assert parse_judge_output(None, P)[0] is None


def test_manual_judge_defers():
    """U1·手动判定器：从不自动判定（None）→ route 转确认；无需 LLM / key。"""
    assert ManualJudge().attribute("随便一句", P, [])[0] is None


def test_cli_judge_parses_answer():
    """U1·CLI 判定器：shell 调命令解析「答案: N」（用 sh -c 假冒 agent，无需真 CLI / key）。"""
    assert CLIJudge(["sh", "-c", "echo 答案: 2"]).attribute("x", P, [])[0] == 2
    assert CLIJudge(["sh", "-c", "echo 答案: 0"]).attribute("x", P, [])[0] == 0    # 0=新项目


def test_cli_judge_degrades_on_error():
    """CLI 非零退出 / 无效输出 → None 降级，不炸穿（route 转确认）。"""
    assert CLIJudge(["sh", "-c", "exit 1"]).attribute("x", P, [])[0] is None
    assert CLIJudge(["sh", "-c", "echo 乱七八糟"]).attribute("x", P, [])[0] is None


def test_cli_judge_command_string_splits():
    assert CLIJudge("claude -p").command == ["claude", "-p"]


def test_config_keyless_judge_starts():
    """U1 端到端：judge type=manual 的 config，load_config + build_memory_service 全程不要求 key。"""
    import tempfile
    import os
    import textwrap
    from isola.config import load_config, build_memory_service
    p = tempfile.mktemp(suffix=".yaml")
    with open(p, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent('''\
            judge: {type: manual}
            harness: {type: openclaw}
            store: {path: ":memory:"}
            projects:
              - {id: 1, name: 甲公司, desc: x}
            '''))
    try:
        svc = build_memory_service(load_config(p))   # 不设任何 env key，不应抛
        assert svc.router.judge is not None
    finally:
        os.path.exists(p) and os.remove(p)


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"PASS {name}")
            except AssertionError as e:
                fails += 1; print(f"FAIL {name}: {e}")
    print(f"\n{'ALL GREEN' if not fails else str(fails)+' FAILED'}")
    sys.exit(1 if fails else 0)
