#!/usr/bin/env python3
"""_profile_paths 的行为测试（#28）：注册表加载、解析优先级、fail-loud、
扁平命名前缀、浏览器目录、简历路径声明、隔离 fail-closed。"""

import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("_profile_paths", Path(__file__).resolve().parent / "_profile_paths.py")
assert _spec is not None and _spec.loader is not None, "importlib spec 加载失败（模块文件缺失？）"
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)


def _registry(tmp_path, default=None, profiles=None, raw=None):
    r = tmp_path / "repo"
    (r / "private").mkdir(parents=True, exist_ok=True)
    p = r / "private" / "profiles.json"
    if raw is not None:
        p.write_text(raw, encoding="utf-8")
    else:
        p.write_text(json.dumps({"default": default, "profiles": profiles or {}}), encoding="utf-8")
    return r


# --- 注册表加载（验收 1：缺失/坏 JSON 可读报错） ---


def test_load_registry_missing_file_is_empty():
    reg = pp.load_registry(root=Path("/nonexistent-root"))
    assert reg == {"default": None, "profiles": {}}


def test_load_registry_bad_json_fail_loud(tmp_path):
    r = _registry(tmp_path, raw="{oops")
    with pytest.raises(SystemExit) as ei:
        pp.load_registry(root=r)
    assert "JSON" in str(ei.value)


def test_load_registry_bad_shapes_fail_loud(tmp_path):
    r = _registry(tmp_path, raw='["list"]')
    with pytest.raises(SystemExit):
        pp.load_registry(root=r)
    r2 = _registry(tmp_path, raw='{"profiles": [1]}')
    with pytest.raises(SystemExit):
        pp.load_registry(root=r2)
    r3 = _registry(tmp_path, raw='{"default": 3, "profiles": {}}')
    with pytest.raises(SystemExit):
        pp.load_registry(root=r3)


# --- 解析优先级（验收 2） ---


def test_resolve_env_beats_registry_default(tmp_path):
    r = _registry(tmp_path, default="web3", profiles={"web3": {}, "ai": {}})
    assert pp.resolve_active_profile(env={}, root=r)[0] == "web3"
    assert pp.resolve_active_profile(env={"AUTOJOB_PROFILE": " ai "}, root=r)[0] == "ai"


def test_resolve_unique_profile_auto_adopted(tmp_path):
    r = _registry(tmp_path, profiles={"blockchain": {}})
    name, prefix = pp.resolve_active_profile(env={}, root=r)
    assert name == "blockchain"
    assert prefix == "", "唯一（在位）profile 零迁移，无前缀"


def test_resolve_multiple_without_default_fail_loud(tmp_path):
    r = _registry(tmp_path, profiles={"web3": {}, "ai": {}})
    with pytest.raises(SystemExit) as ei:
        pp.resolve_active_profile(env={}, root=r)
    assert "fail-loud" in str(ei.value) or "default" in str(ei.value)


def test_resolve_unknown_env_profile_fail_loud(tmp_path):
    r = _registry(tmp_path, default="web3", profiles={"web3": {}})
    with pytest.raises(SystemExit):
        pp.resolve_active_profile(env={"AUTOJOB_PROFILE": "ghost"}, root=r)


def test_resolve_env_without_any_registry_fail_loud(tmp_path):
    r = tmp_path / "repo"
    (r / "private").mkdir(parents=True)
    with pytest.raises(SystemExit):
        pp.resolve_active_profile(env={"AUTOJOB_PROFILE": "ai"}, root=r)


def test_resolve_default_not_in_profiles_fail_loud(tmp_path):
    r = _registry(tmp_path, default="ghost", profiles={"web3": {}})
    with pytest.raises(SystemExit):
        pp.resolve_active_profile(env={}, root=r)


def test_resolve_explicit_flag_top_priority(tmp_path):
    r = _registry(tmp_path, default="web3", profiles={"web3": {}, "ai": {}})
    assert pp.resolve_active_profile(explicit="ai", env={}, root=r)[0] == "ai"


# --- 前缀与路径（验收 6：默认路径零迁移） ---


def test_prefix_empty_for_incumbent_prefixed_for_others(tmp_path):
    r = _registry(tmp_path, default="web3", profiles={"web3": {}, "ai": {}})
    assert pp.resolve_active_profile(env={}, root=r)[1] == ""
    assert pp.resolve_active_profile(env={"AUTOJOB_PROFILE": "ai"}, root=r)[1] == "ai."


def test_private_path_prefixes_filename_only():
    out = pp.private_path("data/greetings.json", prefix="ai.", root=Path("/repo"))
    assert str(out) == str(Path("/repo/private/data/ai.greetings.json"))
    plain = pp.private_path("data/greetings.json", prefix="", root=Path("/repo"))
    assert str(plain) == str(Path("/repo/private/data/greetings.json")), "默认前缀 = 现有路径逐字节一致"


def test_browser_profile_dir_incumbent_keeps_name(tmp_path):
    r = _registry(tmp_path, default="web3", profiles={"web3": {}, "ai": {}})
    assert pp.browser_profile_dir("web3", root=r).name == ".camoufox_profile"
    assert pp.browser_profile_dir("ai", root=r).name == ".camoufox_profile.ai"
    assert pp.browser_profile_dir(None, root=r).name == ".camoufox_profile"


# --- 简历注册（验收 3：任意命名/任意位置） ---


def test_resume_path_relative_and_absolute(tmp_path):
    r = _registry(tmp_path, default="web3", profiles={"web3": {"resume": "private/Resume_示例用户.pdf"}})
    p = pp.resume_path("web3", root=r)
    assert p == r / "private" / "Resume_示例用户.pdf"
    r2 = _registry(tmp_path, profiles={"ai": {"resume": "/abs/path/resume.md"}})
    assert pp.resume_path("ai", root=r2) == Path("/abs/path/resume.md")


def test_resume_path_unregistered_returns_none(tmp_path):
    r = _registry(tmp_path, profiles={"web3": {}})
    assert pp.resume_path("web3", root=r) is None
    assert pp.resume_path("ghost", root=r) is None


# --- 隔离 fail-closed（验收 5 的解析层面基础） ---


def test_isolation_two_profiles_get_distinct_paths(tmp_path):
    """profile A 的台账/白名单不会被 profile B 消费：解析层保证前缀互斥。"""
    r = _registry(tmp_path, default="web3", profiles={"web3": {}, "ai": {}})
    _, p_web3 = pp.resolve_active_profile(env={}, root=r)
    _, p_ai = pp.resolve_active_profile(env={"AUTOJOB_PROFILE": "ai"}, root=r)
    assert p_web3 != p_ai
    assert pp.private_path("data/greetings.json", p_web3, root=r) != pp.private_path(
        "data/greetings.json", p_ai, root=r
    )
    assert pp.private_path("kb/fact_cards.md", p_ai, root=r).name == "ai.fact_cards.md"
    assert pp.private_path("kb/fact_cards.md", p_web3, root=r).name == "fact_cards.md"
    assert pp.browser_profile_dir("web3", root=r) != pp.browser_profile_dir("ai", root=r)


# --- kb profile 名解析（#29 P1 默认值收敛点） ---


def _mk_kb_profiles(r, names):
    d = r / "private" / "kb" / "profiles"
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / f"{n}.json").write_text("{}", encoding="utf-8")


def test_resolve_kb_profile_registry_wins(tmp_path):
    r = _registry(tmp_path, default="ai", profiles={"ai": {}, "blockchain": {}})
    _mk_kb_profiles(r, ["blockchain"])
    assert pp.resolve_kb_profile(env={}, root=r) == "ai"


def test_resolve_kb_profile_legacy_env_chain(tmp_path):
    r = tmp_path / "repo"
    (r / "private").mkdir(parents=True)
    assert pp.resolve_kb_profile(env={"GREETING_PROFILE": "ai"}, root=r) == "ai"
    assert pp.resolve_kb_profile(env={"AUTOJOB_PROFILE": "x", "GREETING_PROFILE": "ai"}, root=r) == "x", (
        "AUTOJOB_PROFILE 优先于旧 GREETING_PROFILE"
    )


def test_resolve_kb_profile_unique_and_default_literal(tmp_path):
    r = tmp_path / "repo"
    (r / "private").mkdir(parents=True)
    assert pp.resolve_kb_profile(env={}, root=r) == "blockchain", "无任何 profile → 唯一默认字面量"
    r2 = tmp_path / "repo2"
    _mk_kb_profiles(r2, ["blockchain"])
    assert pp.resolve_kb_profile(env={}, root=r2) == "blockchain", "唯一 kb profile 自动采用"
    r3 = tmp_path / "repo3"
    _mk_kb_profiles(r3, ["ai"])
    assert pp.resolve_kb_profile(env={}, root=r3) == "ai"


def test_resolve_kb_profile_multiple_without_env_fail_loud(tmp_path):
    r = tmp_path / "repo"
    (r / "private").mkdir(parents=True)
    _mk_kb_profiles(r, ["ai", "blockchain"])
    with pytest.raises(SystemExit):
        pp.resolve_kb_profile(env={}, root=r)


def test_profile_line_format():
    assert pp.profile_line("ai", "ai.") == "[profile] ai（文件前缀 ai.）"
    assert pp.profile_line("blockchain") == "[profile] blockchain"
    assert "未注册" in pp.profile_line(None)


# --- 端到端路径束（gen_greetings.resolve_paths 消费解析层，验收 5/6） ---

import importlib.util as _ilu
import sys as _sys


def _load_pp():
    m = _sys.modules.get("_profile_paths")
    if m is not None:
        return m
    spec = _ilu.spec_from_file_location("_profile_paths", Path(__file__).resolve().parent / "_profile_paths.py")
    m = _ilu.module_from_spec(spec)
    _sys.modules["_profile_paths"] = m  # 先注册再执行：gen_greetings 的 import 复用同一实例
    spec.loader.exec_module(m)
    return m


def _load_gg():
    spec = _ilu.spec_from_file_location("gen_greetings", Path(__file__).resolve().parent / "gen_greetings.py")
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_resolve_paths_isolated_between_profiles(tmp_path, monkeypatch):
    """profile A 的台账/白名单不会被 profile B 消费：注册表解析 → 路径束互斥，
    且默认 profile 路径与 legacy 逐字节一致（fail-closed 的解析层保证）。"""
    pp_mod = _load_pp()
    r = tmp_path / "repo"
    (r / "private").mkdir(parents=True)
    (r / "private" / "profiles.json").write_text(
        json.dumps(
            {"default": "web3", "profiles": {"web3": {"resume": "private/r.pdf"}, "ai": {"resume": "private/ai.md"}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(pp_mod, "ROOT", r)
    mod = _load_gg()
    monkeypatch.setenv("AUTOJOB_PROFILE", "ai")
    paths_ai = mod.resolve_paths()
    monkeypatch.setenv("AUTOJOB_PROFILE", "")
    paths_web3 = mod.resolve_paths()
    for key in (
        "kb",
        "fact_cards",
        "drafts",
        "greetings",
        "memory",
        "followup",
        "history",
        "annotations",
        "evaluation",
        "winning",
    ):
        assert paths_ai[key] != paths_web3[key], f"{key} 必须按 profile 隔离"
        assert paths_ai[key].name.startswith("ai.")
        assert not paths_web3[key].name.startswith("web3."), "默认 profile 零迁移"
    assert paths_web3["fact_cards"] == r / "private" / "kb" / "fact_cards.md"


def test_resolve_paths_multi_profile_no_default_fail_loud(tmp_path, monkeypatch):
    pp_mod = _load_pp()
    r = tmp_path / "repo"
    (r / "private").mkdir(parents=True)
    (r / "private" / "profiles.json").write_text(json.dumps({"profiles": {"web3": {}, "ai": {}}}), encoding="utf-8")
    monkeypatch.setattr(pp_mod, "ROOT", r)
    mod = _load_gg()
    with pytest.raises(SystemExit):
        mod.resolve_paths()
