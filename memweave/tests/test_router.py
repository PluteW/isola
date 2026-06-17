"""路由器规则层离线测试——用例取自 exp02 数据集的真实模式。"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from memweave.router import Router, is_low_signal, detect_cross_ref

PROJECTS = [
    {"id": 1, "name": "甲公司尽调", "desc": "公司级尽调"},
    {"id": 2, "name": "乙公司尽调", "desc": "公司级尽调"},
    {"id": 3, "name": "法律合同调研", "desc": "竞品与产品形态"},
]


class StubJudge:
    def __init__(self, ret=2):
        self.ret = ret
        self.called = False

    def attribute(self, text, projects, history):
        self.called = True
        return self.ret, "stub"


def test_zero_signal_goes_inertia():
    r = Router(StubJudge())
    for t in ["好的，谢谢", "收到", "嗯，可以", "辛苦了", "好嘞"]:
        d = r.route(t, PROJECTS, last_project_id=2)
        assert d.route == "inertia" and d.project_id == 2, t


def test_anaphor_goes_inertia():
    r = Router(StubJudge())
    for t in ["继续", "接着昨天的进度往下做", "那个表格补完了吗", "上次说的第三点再展开一下"]:
        d = r.route(t, PROJECTS, last_project_id=1)
        assert d.route == "inertia" and d.project_id == 1, t


def test_cross_ref_detected_and_kept_in_current():
    r = Router(StubJudge())
    t = "参考《乙公司尽调》的目录结构，把这个公司的尽调底稿也整理成同样的章节"
    d = r.route(t, PROJECTS, last_project_id=1)
    assert d.route == "cross_ref" and d.project_id == 1 and d.referenced_id == 2


def test_cross_ref_by_name_plus_verb():
    t = "按乙公司尽调里用过的证据强度分级办法，把这边的关键结论也标一遍"
    assert detect_cross_ref(t, PROJECTS, current_id=1) == 2


def test_mention_without_ref_verb_not_cross_ref():
    # 实质切换回另一项目，不应被引用检测拦截
    t = "乙公司尽调的募投项目进度核一下"
    assert detect_cross_ref(t, PROJECTS, current_id=1) is None


def test_content_goes_judge():
    j = StubJudge(ret=2)
    r = Router(j)
    d = r.route("把估值部分算一下，对标公司选哪几家合适", PROJECTS, last_project_id=1)
    assert d.route == "judge" and d.project_id == 2 and j.called


def test_judge_zero_means_new_project_proposal():
    r = Router(StubJudge(ret=0))
    d = r.route("帮我规划一下下个月的健身计划", PROJECTS, last_project_id=1)
    assert d.project_id is None and d.confidence == "low"


def test_question_mark_not_low_signal():
    assert not is_low_signal("好的吗？")
    assert not is_low_signal("可以查一下他们的毛利率吗？")


def test_empty_registry():
    d = Router(StubJudge()).route("随便说点什么", [], last_project_id=None)
    assert d.confidence == "low"


# ---------- 降级路径（test-plan §4 ④：组件失败/边界时优雅回退，不瞎归属）----------
def test_judge_unconfigured_falls_back_to_inertia():
    """判定器未配置 → 惯性回退 + 低置信（不炸穿）。"""
    d = Router(judge=None).route("把估值模型重算一遍", PROJECTS, last_project_id=3)
    assert d.route == "judge" and d.project_id == 3 and d.confidence == "low"


def test_judge_pid_none_falls_back_to_inertia():
    """判定器输出无法解析（pid=None）→ 惯性回退 + 低置信。"""
    d = Router(StubJudge(ret=None)).route("分析下行业集中度", PROJECTS, last_project_id=2)
    assert d.project_id == 2 and d.confidence == "low"


def test_low_signal_without_inertia_goes_judge_low():
    """低信号 + 无惯性上下文 → 交判定器且低置信（无处延续，不瞎归属）。"""
    d = Router(StubJudge()).route("继续", PROJECTS, last_project_id=None)
    assert d.route == "judge" and d.project_id is None and d.confidence == "low"


def test_cross_ref_without_inertia_not_triggered():
    """cross_ref 命中但无惯性项目 → 不触发 cross_ref（无当前项目可归属）。"""
    t = "参考《乙公司尽调》的目录结构整理"
    d = Router(StubJudge(ret=0)).route(t, PROJECTS, last_project_id=None)
    assert d.route != "cross_ref"


# ---------- 负向/异常输入鲁棒性（test-plan §4 ⑤：畸形输入不炸穿）----------
def test_router_robust_to_none_and_blank():
    """None / 空 / 空白 text 不炸穿（修 is_low_signal(None) AttributeError——纯图片/语音消息场景）。"""
    r = Router(StubJudge())
    for t in [None, "", "   ", "\n\t"]:
        assert r.route(t, PROJECTS, last_project_id=1) is not None
    assert is_low_signal(None) is False
    assert detect_cross_ref(None, PROJECTS, current_id=1) is None


def test_router_robust_to_extreme_input():
    """超长 / emoji / 控制字符不崩。"""
    r = Router(StubJudge())
    for t in ["x" * 100000, "😀" * 500, chr(0) + "null\x01\x02"]:
        assert r.route(t, PROJECTS, last_project_id=1) is not None


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if fails else 0)
