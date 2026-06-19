"""分发层测试（T-D-*，内部设计）。纯 assert，if __name__ 自跑，离线。"""
import sys
import pathlib
import tempfile
import os
import re
import io
import contextlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))   # isola 包
sys.path.insert(0, str(pathlib.Path(__file__).parent))          # fakes

import yaml
from isola import config as cfgmod
from isola.config import load_config, build_core, ConfigError
from isola.models import InboundMessage
from fakes import FakeJudge, FakeHarness, FakeChannel

# 往注册表注入 fake 实现（离线，不联网）——验证装配而非真实 LLM
cfgmod._JUDGES["fake"] = FakeJudge
cfgmod._HARNESSES["fake"] = FakeHarness
cfgmod._CHANNELS["fake"] = FakeChannel


def _write_config(d, **over):
    base = {
        "judge": {"type": "fake"}, "harness": {"type": "fake"}, "channel": {"type": "fake"},
        "store": {"path": ":memory:"}, "isolation_s": 300,
        "projects": [{"id": 1, "name": "项目A", "desc": "A 的描述"},
                     {"id": 2, "name": "项目B", "desc": "B 的描述"}],
    }
    base.update(over)
    p = pathlib.Path(d) / "config.yaml"
    p.write_text(yaml.safe_dump(base, allow_unicode=True), encoding="utf-8")
    return str(p)


def test_unknown_type_raises():
    """未知 harness type → build_core 抛错且消息列出已注册 keys。"""
    with tempfile.TemporaryDirectory() as d:
        cp = _write_config(d, harness={"type": "nope"})
        try:
            build_core(load_config(cp)); assert False, "应抛 ConfigError"
        except ConfigError as e:
            assert "nope" in str(e) and "已注册" in str(e), e


def test_build_core_routes_one():
    """合法 config（fake provider）→ build_core 出可路由一条消息的 Core。"""
    with tempfile.TemporaryDirectory() as d:
        core = build_core(load_config(_write_config(d)))
        msg = InboundMessage(msg_id="m1", event_id="e1", platform_msg_id="pm1",
                             user_id="u", text="查项目A的事", chat_id="c1", platform_ts=0)
        r = core.handle_message(msg, now=1000.0)
        assert r.get("project_id") == 1, r          # FakeJudge 默认 ret_pid=1


def test_example_config_no_plaintext_key():
    """config.example.yaml 不含明文 api_key（只 api_key_env）。"""
    ex = pathlib.Path(__file__).parent.parent / "isola" / "config.example.yaml"   # 包内数据
    text = ex.read_text(encoding="utf-8")
    assert not re.search(r"(?m)^\s*api_key\s*:", text), "含明文 api_key"
    assert "api_key_env" in text


def test_plaintext_key_rejected():
    """配置里写明文 api_key → load_config 拒绝。"""
    with tempfile.TemporaryDirectory() as d:
        cp = _write_config(d, judge={"type": "fake", "api_key": "sk-secret"})
        try:
            load_config(cp); assert False, "应拒绝明文 api_key"
        except ConfigError as e:
            assert "api_key" in str(e)


def test_projects_need_id():
    """projects 缺 id → 拒绝（防 project_id 错绑）。"""
    with tempfile.TemporaryDirectory() as d:
        cp = _write_config(d, projects=[{"name": "无id", "desc": "x"}])
        try:
            load_config(cp); assert False, "应拒绝无 id 项目"
        except ConfigError as e:
            assert "id" in str(e)


def test_doctor_readonly():
    """doctor 只读：不创建/写 store 库文件。"""
    from isola import doctor
    with tempfile.TemporaryDirectory() as d:
        db = str(pathlib.Path(d) / "must_not_exist.db")
        cp = _write_config(d, store={"path": db})
        doctor.run(cp)
        assert not os.path.exists(db), "doctor 不应建库（只读）"


def test_doctor_reports_failures():
    """缺配置 → doctor 的 config 项 fail + 给 fix。"""
    from isola import doctor
    res = doctor.run("/nonexistent/path/config.yaml")
    step = next(r for r in res if r["id"] == "config")
    assert step["status"] == "fail" and step["fix_command"], step


def test_doctor_flags_missing_env():
    """doctor 抓出 api_key_env 指向的环境变量未设置（review 补强：防 config pass 但 chat 才炸）。"""
    from isola import doctor
    with tempfile.TemporaryDirectory() as d:
        os.environ.pop("MW_NONEXISTENT_KEY_XYZ", None)
        cp = _write_config(d, judge={"type": "fake", "api_key_env": "MW_NONEXISTENT_KEY_XYZ"})
        res = doctor.run(cp)
        jstep = next(r for r in res if r["id"] == "judge")
        assert jstep["status"] == "fail" and "MW_NONEXISTENT_KEY_XYZ" in jstep["evidence"], jstep


def test_cli_subcommands_exist():
    """init/doctor/chat 子命令存在、--help 可用（退出码 0）。"""
    from isola.__main__ import build_parser
    p = build_parser()
    assert p.parse_args(["init"]).cmd == "init"
    assert p.parse_args(["doctor", "--json"]).cmd == "doctor"
    assert p.parse_args(["chat", "--text", "hi"]).cmd == "chat"
    for cmd in ("init", "doctor", "chat"):
        try:
            with contextlib.redirect_stdout(io.StringIO()):   # 静默 argparse --help 输出
                p.parse_args([cmd, "--help"])
            assert False, "应 SystemExit"
        except SystemExit as e:
            assert e.code == 0


def test_key_env_required_for_real_provider():
    """openai_compat/llm_direct 必须配 api_key_env（缺则 load_config 拒）。"""
    with tempfile.TemporaryDirectory() as d:
        cp = _write_config(d, judge={"type": "openai_compat", "base_url": "https://x", "model": "m"})
        try:
            load_config(cp); assert False, "应拒缺 api_key_env"
        except ConfigError as e:
            assert "api_key_env" in str(e)


def test_nested_plaintext_secret_rejected():
    """嵌套/别名明文密钥也被拦（Authorization / token / 嵌套）。"""
    for bad in [{"type": "fake", "headers": {"Authorization": "Bearer sk-xxx"}},
                {"type": "fake", "token": "ghp_realtoken1234567890"}]:
        with tempfile.TemporaryDirectory() as d:
            cp = _write_config(d, harness=bad)
            try:
                load_config(cp); assert False, bad
            except ConfigError as e:
                assert "明文密钥" in str(e)


def test_projects_id_must_be_contiguous():
    """projects id 必须从 1 连续唯一（跳号/重复/非 1 起 → 拒）。"""
    for bad in [[{"id": 2, "name": "x"}],
                [{"id": 1, "name": "a"}, {"id": 1, "name": "b"}],
                [{"id": 1, "name": "a"}, {"id": 3, "name": "b"}]]:
        with tempfile.TemporaryDirectory() as d:
            cp = _write_config(d, projects=bad)
            try:
                load_config(cp); assert False, bad
            except ConfigError as e:
                assert "id" in str(e)


def test_registry_drift_rejected():
    """已有注册表后 config 改 project name → 拒启（防错绑，R2-7）。"""
    with tempfile.TemporaryDirectory() as d:
        db = str(pathlib.Path(d) / "s.db")
        build_core(load_config(_write_config(d, store={"path": db})))     # 首次播种 项目A/项目B
        cp2 = _write_config(d, store={"path": db},
                            projects=[{"id": 1, "name": "改名了", "desc": "x"},
                                      {"id": 2, "name": "项目B", "desc": "y"}])
        try:
            build_core(load_config(cp2)); assert False, "应拒漂移"
        except ConfigError as e:
            assert "漂移" in str(e)


def test_malformed_section_type_rejected():
    """段类型错（judge: null）→ ConfigError 而非 TypeError。"""
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "config.yaml"
        p.write_text("judge: null\nharness: {type: fake}\nstore: {path: ':memory:'}\nprojects: []\n",
                     encoding="utf-8")
        try:
            load_config(str(p)); assert False, "应拒 judge: null"
        except ConfigError as e:
            assert "judge" in str(e)


def test_no_side_effect_on_invalid_config():
    """非法配置（未知 type）失败前不建库（副作用顺序）。"""
    with tempfile.TemporaryDirectory() as d:
        db = str(pathlib.Path(d) / "nope.db")
        cp = _write_config(d, store={"path": db}, harness={"type": "nope"})
        try:
            build_core(load_config(cp))
        except ConfigError:
            pass
        assert not os.path.exists(db), "非法配置不应建库"


def test_relative_store_path_and_parent_created():
    """相对 store path 按 config 目录解析 + 父目录自动创建（fresh install 不崩）。"""
    with tempfile.TemporaryDirectory() as d:
        core = build_core(load_config(_write_config(d, store={"path": "data/sub/mw.db"})))
        assert os.path.exists(pathlib.Path(d) / "data" / "sub" / "mw.db"), "父目录+库应被创建"
        core.store.count("events")        # 库可用


def test_build_core_resolves_api_key_env():
    """带 api_key_env 的真 provider 正确装配：api_key_env 被剔除、api_key 从 env 注入（真跑暴露的 bug）。"""
    os.environ["MW_TEST_KEY"] = "dummy"
    with tempfile.TemporaryDirectory() as d:
        cp = _write_config(d,
            judge={"type": "openai_compat", "base_url": "http://x/v1", "model": "m", "api_key_env": "MW_TEST_KEY"},
            harness={"type": "llm_direct", "base_url": "http://x/v1", "model": "m", "api_key_env": "MW_TEST_KEY"})
        core = build_core(load_config(cp))          # 不应 TypeError（曾因 api_key_env 未剔除而崩）
        assert core is not None


def test_cmd_init_writes_packaged_template():
    """cmd_init 从包内 config.example.yaml 生成 config.yaml（扁平化后 package-data 可定位）。"""
    import types
    from isola.__main__ import cmd_init
    with tempfile.TemporaryDirectory() as d:
        dst = pathlib.Path(d) / "sub" / "config.yaml"          # 含嵌套：顺带验证父目录自动创建
        with contextlib.redirect_stdout(io.StringIO()):
            rc = cmd_init(types.SimpleNamespace(path=str(dst)))
        assert rc == 0 and dst.exists(), "应生成 config.yaml"
        text = dst.read_text(encoding="utf-8")
        assert "api_key_env" in text and "projects" in text, "内容应来自包内模板"


def test_doctor_probe_openclaw_mjs():
    """openclaw：无入口→fail+提示 --openclaw-dir；给目录搜到 .mjs→need_human+wrapper 指引（E3）。"""
    import isola.doctor as doctor
    with tempfile.TemporaryDirectory() as d:
        (pathlib.Path(d) / "oc" / "sub").mkdir(parents=True)
        (pathlib.Path(d) / "oc" / "sub" / "openclaw.mjs").write_text("// fake", encoding="utf-8")
        cp = _write_config(d, harness={"type": "openclaw", "binary": "oc-nope-xyz", "agent": "main"})
        orig_which, cwd0 = doctor.shutil.which, os.getcwd()
        doctor.shutil.which = lambda _name: None         # 隔离：模拟 PATH 里无 openclaw/node
        try:
            os.chdir(d)                                  # 隔离：cwd 无 node_modules
            h = [r for r in doctor.run(cp, {}) if r["id"] == "harness"][0]
            assert h["status"] == "fail" and "--openclaw-dir" in h["evidence"], h
            h2 = [r for r in doctor.run(cp, {"openclaw_dir": str(pathlib.Path(d) / "oc")})
                  if r["id"] == "harness"][0]
            assert h2["status"] == "need_human" and "openclaw.mjs" in h2["evidence"], h2
        finally:
            doctor.shutil.which = orig_which; os.chdir(cwd0)


def test_doctor_emit_wrapper():
    """--emit-wrapper 写 scripts/openclaw-bin（exec node mjs）；已存在不覆盖、--force 覆盖（E3 唯一写动作）。"""
    import isola.doctor as doctor
    with tempfile.TemporaryDirectory() as d:
        (pathlib.Path(d) / "oc").mkdir()
        (pathlib.Path(d) / "oc" / "openclaw.mjs").write_text("// fake", encoding="utf-8")
        cp = _write_config(d, harness={"type": "openclaw", "binary": "oc-nope-xyz", "agent": "main"})
        ctx = {"openclaw_dir": str(pathlib.Path(d) / "oc"), "node_path": sys.executable}
        orig_which, cwd0 = doctor.shutil.which, os.getcwd()
        doctor.shutil.which = lambda _name: None
        try:
            os.chdir(d)                                  # emit 写到 cwd/scripts
            path, msg = doctor.emit_wrapper_if_requested(cp, ctx)
            assert path and pathlib.Path(path).exists(), msg
            body = pathlib.Path(path).read_text(encoding="utf-8")
            assert "openclaw.mjs" in body and "exec" in body, body
            assert doctor.emit_wrapper_if_requested(cp, ctx)[0] is None, "已存在应不覆盖"
            assert doctor.emit_wrapper_if_requested(cp, {**ctx, "force": True})[0], "--force 应覆盖"
        finally:
            doctor.shutil.which = orig_which; os.chdir(cwd0)


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
