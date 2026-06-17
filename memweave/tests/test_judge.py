"""判定器解析逻辑测试（T-UNIT-4）：0=新项目 与 None=无法判断 显式区分。
纯函数离线测，不调真模型。"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from memweave.judge import parse_judge_output

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
