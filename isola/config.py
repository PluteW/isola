"""配置加载与装配（分发层 v1 CLI 档，内部设计）。

config.yaml → Config（集中校验：段类型 / type 合法 / 密钥安全 / projects id / 漂移）→ 注册表工厂 build_core。
Python idiom：模块级 dict 注册表 + 工厂函数，非类层级。不含 serve/HTTP。
校验全部前置到 load_config（避免 build_core 先建库再失败的持久化副作用）。
"""
from __future__ import annotations
import os
import re
import pathlib
import tempfile
from dataclasses import dataclass, field

import yaml

from .store import Store
from .registry import Registry
from .router import Router
from .core import IsolaCore
from .judge import OpenAICompatJudge
from .adapters.cli import CLIChannel
from .adapters.llm_harness import LLMHarness
from .adapters.openclaw import OpenClawAdapter


class ConfigError(Exception):
    """配置非法：缺段/段类型错/未知 type/明文密钥/projects id 非法/漂移。消息须能定位。"""


@dataclass
class Config:
    judge: dict
    harness: dict
    store: dict
    projects: list
    channel: dict = field(default_factory=lambda: {"type": "cli"})
    isolation_s: int = 300
    path: str = ""


# ---- 注册表：type → 构造（模块级 dict）----
_CHANNELS = {"cli": CLIChannel}
_HARNESSES = {"openclaw": OpenClawAdapter, "llm_direct": LLMHarness}
_JUDGES = {"openai_compat": OpenAICompatJudge}
_KEY_REQUIRED = {"openai_compat", "llm_direct"}        # 这些 type 必须配 api_key_env

# 疑似明文密钥的键名（递归扫描全配置；*_env 键豁免）——：防 token/Authorization/嵌套绕过
_SECRET_KEY_RE = re.compile(r"(api[_-]?key|secret|passwd|password|token|authorization)$", re.I)


def _scan_plaintext_secrets(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            ks = str(k)
            if ks.endswith("_env"):                    # 允许 *_api_key_env（只存环境变量名）
                continue
            if _SECRET_KEY_RE.search(ks) and isinstance(v, str) and v.strip():
                raise ConfigError(f"{path}{ks} 疑似明文密钥；请改用 *_api_key_env 指向环境变量名")
            _scan_plaintext_secrets(v, f"{path}{ks}.")
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _scan_plaintext_secrets(item, f"{path}[{i}].")


def _resolve_store_path(raw_store: dict, cfg_dir: pathlib.Path) -> str:
    p = raw_store.get("path", ":memory:")
    if p == ":memory:":
        return ":memory:"
    pp = pathlib.Path(p).expanduser()
    if not pp.is_absolute():                            # 相对路径按 config 文件所在目录解析
        pp = cfg_dir / pp
    return str(pp)


def load_config(path: str) -> Config:
    cfg_path = pathlib.Path(path).expanduser()
    if not cfg_path.exists():
        raise ConfigError(f"配置文件不存在: {path}（先运行 `isola init`）")
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError("配置顶层必须是 mapping（key: value 结构）")
    # 必填段 + 段类型（防 judge: null / projects: {} 抛 TypeError）
    for key, typ, tname in [("judge", dict, "mapping"), ("harness", dict, "mapping"),
                            ("store", dict, "mapping"), ("projects", list, "list")]:
        if key not in raw:
            raise ConfigError(f"配置缺必填段: {key}（参考 config.example.yaml）")
        if not isinstance(raw[key], typ):
            raise ConfigError(f"配置段 {key} 必须是 {tname}")
    channel = raw.get("channel", {"type": "cli"})
    if not isinstance(channel, dict):
        raise ConfigError("配置段 channel 必须是 mapping")
    # 明文密钥递归拦截（安全门 INV-D4）
    _scan_plaintext_secrets(raw)
    # type 合法 + api_key_env 必填
    for section, registry, kind in [(raw["judge"], _JUDGES, "judge"),
                                    (raw["harness"], _HARNESSES, "harness"),
                                    (channel, _CHANNELS, "channel")]:
        t = section.get("type")
        if t not in registry:
            raise ConfigError(f"未知 {kind} type={t!r}；已注册: {sorted(registry)}")
        if t in _KEY_REQUIRED and not section.get("api_key_env"):
            raise ConfigError(f"{kind}.api_key_env 必填（type={t} 需要 api_key，填环境变量名）")
    # projects：id 必须是从 1 连续唯一的 int，name 非空（先校验，不留半 registry）
    projects = raw["projects"]
    ids = [p.get("id") if isinstance(p, dict) else None for p in projects]
    if any(not isinstance(i, int) for i in ids) or ids != list(range(1, len(projects) + 1)):
        raise ConfigError(f"projects 的 id 必须是从 1 连续的整数 [1..{len(projects)}]，实际 {ids}")
    for i, p in enumerate(projects):
        if not str(p.get("name", "")).strip():
            raise ConfigError(f"projects[{i}] 的 name 不能为空")
    store = dict(raw["store"])
    store["path"] = _resolve_store_path(raw["store"], cfg_path.parent)
    return Config(judge=raw["judge"], harness=raw["harness"], store=store,
                  projects=projects, channel=channel,
                  isolation_s=raw.get("isolation_s", 300), path=str(cfg_path))


def _resolve_kwargs(section: dict) -> dict:
    """适配器构造 kwargs：剔除 type 与 *_api_key_env，把 api_key_env 解析成真实 api_key。"""
    out = {k: v for k, v in section.items() if k != "type" and not k.endswith("_env")}
    env_name = section.get("api_key_env")
    if env_name:
        key = os.environ.get(env_name)
        if not key:
            raise ConfigError(f"环境变量 {env_name} 未设置（{section.get('type')} 需要 api_key）")
        out["api_key"] = key
    return out


def _make(registry: dict, section: dict, kind: str):
    t = section.get("type")
    if t not in registry:                              # 双重保险（load_config 已校验）
        raise ConfigError(f"未知 {kind} type={t!r}；已注册: {sorted(registry)}")
    return registry[t](**_resolve_kwargs(section))


def _registry_path(cfg: Config) -> str:
    sp = cfg.store.get("path", ":memory:")
    if sp == ":memory:":                               # 内存库（测试）→ 原子创建临时注册表（mkstemp 无 mktemp 竞态）
        fd, p = tempfile.mkstemp(suffix=".registry.yaml")
        os.close(fd)
        return p
    return str(pathlib.Path(sp).parent / "registry.yaml")


def _seed_projects(reg: Registry, projects: list) -> None:
    """首次播种；已有注册表则以其为权威 + 校验 config 无漂移（ / 设计 R2-7）。"""
    existing = reg.active_projects()
    if existing:
        cfg_map = {p["id"]: p["name"] for p in projects}
        reg_map = {p["id"]: p["name"] for p in existing}
        if cfg_map != reg_map:
            raise ConfigError(
                f"config 的 projects 与已存在注册表不一致（id/name 漂移）：config={cfg_map} "
                f"registry={reg_map}；改回一致，或删除旧注册表后重新播种")
        return
    for p in projects:                                 # id 已校验 [1..n]，Registry.add 自增 id 必匹配
        reg.add(p["name"], p.get("desc", ""))


def build_core(cfg: Config) -> IsolaCore:
    """从 Config 装配 IsolaCore。无副作用的 adapter 先构造（校验 type+env），建库/写 registry 放最后。"""
    judge = _make(_JUDGES, cfg.judge, "judge")
    harness = _make(_HARNESSES, cfg.harness, "harness")
    channel = _make(_CHANNELS, cfg.channel, "channel")
    sp = cfg.store.get("path", ":memory:")
    if sp != ":memory:":                               # fresh install：父目录不存在则创建
        pathlib.Path(sp).parent.mkdir(parents=True, exist_ok=True)
    store = Store(sp)
    reg = Registry(_registry_path(cfg))
    _seed_projects(reg, cfg.projects)
    return IsolaCore(store, reg, Router(judge), channel, harness, isolation_s=cfg.isolation_s)


def build_memory_service(cfg: Config):
    """从 Config 装配模式 B 的 MemoryService（SDD ③ §1）：仅 judge + store + registry，
    **不构造 channel/harness**——模式 B 不 dispatch，故也不需要 harness 的 api_key。"""
    from .mode_b import MemoryService
    judge = _make(_JUDGES, cfg.judge, "judge")
    sp = cfg.store.get("path", ":memory:")
    if sp != ":memory:":
        pathlib.Path(sp).parent.mkdir(parents=True, exist_ok=True)
    store = Store(sp)
    reg = Registry(_registry_path(cfg))
    _seed_projects(reg, cfg.projects)
    return MemoryService(store, reg, Router(judge))
