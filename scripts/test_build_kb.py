#!/usr/bin/env python3
"""build_kb 的行为测试：输入 fail-fast、禁推测 prompt、草稿渲染、LLM 注入主流程、失败隔离。"""

import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("build_kb", Path(__file__).resolve().parent / "build_kb.py")
assert _spec is not None and _spec.loader is not None, "importlib spec 加载失败（模块文件缺失？）"
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

ENTRY_A = {
    "repo": "repo_a",
    "path": "/tmp/repo_a",
    "positioning": "演示前端",
    "tech_stack": "TypeScript + React",
    "architecture_summary": "src 平铺",
    "highlights": ["有 README"],
    "red_flags": [],
    "files_sampled": 1,
    "surveyed_at": "2026-09-07T12:00:00",
}
ENTRY_B = {
    "repo": "repo_b",
    "path": "/tmp/repo_b",
    "positioning": "演示后端",
    "tech_stack": "Node 20",
    "architecture_summary": "monorepo",
    "highlights": ["有 CI"],
    "red_flags": ["策略逻辑未读到"],
    "files_sampled": 2,
    "surveyed_at": "2026-09-07T12:00:01",
}
ENTRY_BAD = {
    "repo": "repo_bad",
    "path": "/tmp/repo_bad",
    "error": "模拟超时",
    "files_sampled": 0,
    "surveyed_at": "2026-09-07T12:00:02",
}

SECTION_A = "- **定位**：演示前端\n- **技术栈**：TypeScript + React\n- **亮点**：\n  - 有 README"
SECTION_B = "- **定位**：演示后端\n- **技术栈**：Node 20"
SUMMARY = "- 旗舰组：repo_a（演示前端）；repo_b（演示后端）"


def make_input(tmp_path, entries, name="repos_detail.json"):
    p = tmp_path / name
    p.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return str(p)


def llm_by_stage(section=SECTION_A, summary=SUMMARY):
    """按 system prompt 区分单仓条目 / 精炼摘要两个阶段。"""
    calls = []

    def llm(messages, cfg):
        calls.append(messages)
        if "精炼层" in messages[0]["content"]:
            return json.dumps({"summary": summary}, ensure_ascii=False)
        return json.dumps({"section": section}, ensure_ascii=False)

    llm.calls = calls
    return llm


# --- LLM 输出 JSON 提取（复用 gen_greetings 思路） ---


def test_extract_json_tolerates_fence():
    assert mod.extract_json('```json\n{"section": "x"}\n```') == {"section": "x"}


def test_extract_json_invalid_raises():
    with pytest.raises(ValueError):
        mod.extract_json("完全没有 JSON")


# --- LLM 配置与调用 ---


def test_llm_config_defaults_and_override():
    cfg = mod.llm_config(env={})
    assert cfg["base_url"] == "http://127.0.0.1:11434/v1"
    cfg2 = mod.llm_config(
        env={
            "GREETING_LLM_BASE_URL": "https://api.x.cn/v1/",
            "GREETING_LLM_MODEL": "glm-4",
            "GREETING_LLM_API_KEY": "sk-1",
        }
    )
    assert cfg2["base_url"] == "https://api.x.cn/v1"
    assert cfg2["model"] == "glm-4" and cfg2["api_key"] == "sk-1"


def test_call_llm_builds_openai_compatible_request():
    captured = {}

    def transport(url, payload, headers):
        captured.update(url=url, payload=payload, headers=headers)
        return {"choices": [{"message": {"content": '{"section": "x"}'}}]}

    out = mod.call_llm(
        [{"role": "user", "content": "hi"}],
        {"base_url": "http://x/v1", "model": "m", "api_key": "k"},
        transport=transport,
    )
    assert out == '{"section": "x"}'
    assert captured["url"] == "http://x/v1/chat/completions"
    assert captured["payload"]["model"] == "m"
    assert captured["headers"]["Authorization"] == "Bearer k"


def test_call_llm_falls_back_to_reasoning_when_content_empty():
    """qwen3 思考模式兜底：兼容端点 content 为空时回退 reasoning 字段。"""

    def transport(url, payload, headers):
        return {"choices": [{"message": {"content": "", "reasoning": '{"summary": "s"}'}}]}

    out = mod.call_llm(
        [{"role": "user", "content": "hi"}], {"base_url": "x", "model": "m", "api_key": ""}, transport=transport
    )
    assert out == '{"summary": "s"}'


# --- 禁推测约束进 prompt ---


def test_prompts_disable_thinking():
    assert "/no_think" in mod.SECTION_SYSTEM
    assert "/no_think" in mod.SUMMARY_SYSTEM


def test_prompts_carry_no_speculation_constraint():
    """两段 system prompt 都必须带禁推测约束：只依据输入字段事实、不得编造。"""
    for system in (mod.SECTION_SYSTEM, mod.SUMMARY_SYSTEM):
        assert "不得编造" in system, "缺少禁推测约束"


def test_build_section_messages_contains_entry_facts():
    msgs = mod.build_section_messages(ENTRY_A)
    assert msgs[0]["role"] == "system"
    user = msgs[1]["content"]
    assert "repo_a" in user and "演示前端" in user and "TypeScript + React" in user
    assert "有 README" in user


def test_build_summary_messages_contains_all_repos():
    msgs = mod.build_summary_messages([ENTRY_A, ENTRY_B])
    assert msgs[0]["role"] == "system"
    user = msgs[1]["content"]
    assert "repo_a" in user and "repo_b" in user


# --- 输入读取（fail-fast） ---


def test_load_entries_returns_list(tmp_path):
    assert mod.load_entries(make_input(tmp_path, [ENTRY_A])) == [ENTRY_A]


def test_load_entries_missing_exits(tmp_path):
    with pytest.raises(SystemExit):
        mod.load_entries(str(tmp_path / "nope.json"))


def test_load_entries_corrupt_exits(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit):
        mod.load_entries(str(p))


def test_load_entries_non_list_exits(tmp_path):
    p = tmp_path / "obj.json"
    p.write_text('{"repo": "a"}', encoding="utf-8")
    with pytest.raises(SystemExit):
        mod.load_entries(str(p))


def test_split_entries_separates_error_entries():
    valid, skipped = mod.split_entries([ENTRY_A, ENTRY_BAD, ENTRY_B])
    assert [e["repo"] for e in valid] == ["repo_a", "repo_b"]
    assert [e["repo"] for e in skipped] == ["repo_bad"]


# --- 草稿渲染（纯函数，无 LLM） ---


def _sections():
    return [("repo_a", "2026-09-07T12:00:00", SECTION_A), ("repo_b", "2026-09-07T12:00:01", SECTION_B)]


def test_render_draft_clean_structure():
    text = mod.render_draft(
        _sections(), SUMMARY, [], [], "in.json", now=__import__("datetime").datetime(2026, 9, 7, 12, 0, 0)
    )
    assert "# 知识库草稿" in text
    assert "docs/kb-sop.md" in text, "草稿头部应指向 SOP 文档"
    assert "### 1. repo_a" in text and "### 2. repo_b" in text
    assert "- **定位**：演示前端" in text
    assert SUMMARY in text, "精炼层摘要建议应整段进草稿"
    assert "private/kb/repos_detail.md" in text and "private/profile_background.md" in text
    assert "待重跑" not in text, "无失败时不应出现待重跑节"


def test_render_draft_lists_rerun_entries():
    skipped = [dict(ENTRY_BAD)]
    failed = [("repo_b", "模拟坏输出")]
    text = mod.render_draft(
        [("repo_a", ENTRY_A["surveyed_at"], SECTION_A)], SUMMARY, skipped, failed, "in.json", now=None
    )
    assert "待重跑" in text
    assert "repo_bad" in text and "模拟超时" in text, "调研失败档案应列待重跑"
    assert "repo_b" in text and "模拟坏输出" in text, "综合失败仓库应列待重跑"


def test_render_draft_summary_placeholder_on_failure():
    text = mod.render_draft(_sections(), None, [], [], "in.json", now=None)
    assert "生成失败" in text, "摘要失败时精炼层节应留占位说明"


# --- 默认路径 ---


def test_default_paths():
    assert str(mod.DEFAULT_INPUT).endswith("private/data/repos_detail.json")
    assert str(mod.DEFAULT_OUTPUT).endswith("private/kb/draft/repos_detail.draft.md")


# --- 主流程（LLM 注入） ---


def test_run_writes_draft_with_sections_and_summary(tmp_path):
    out = tmp_path / "draft.md"
    llm = llm_by_stage()
    text = mod.run(
        make_input(tmp_path, [ENTRY_A, ENTRY_BAD, ENTRY_B]),
        str(out),
        llm_call=llm,
        cfg={},
        now=__import__("datetime").datetime(2026, 9, 7, 12, 0, 0),
    )
    assert out.exists(), "草稿必须落盘"
    saved = out.read_text(encoding="utf-8")
    assert saved == text
    assert "### 1. repo_a" in saved and "### 2. repo_b" in saved, "跳过 error 档案后按序编号"
    assert SUMMARY in saved
    assert "repo_bad" in saved and "模拟超时" in saved, "调研失败档案进待重跑"
    assert len(llm.calls) == 3, "两仓各一次条目调用 + 一次摘要调用"


def test_run_summary_call_only_covers_sectioned_repos(tmp_path):
    """综合失败的仓库不进精炼摘要的输入（摘要只覆盖草稿里实际存在的节）。"""
    out = tmp_path / "draft.md"
    calls = []

    def llm_fail_b(messages, cfg):
        calls.append(messages)
        if "精炼层" in messages[0]["content"]:
            return json.dumps({"summary": SUMMARY}, ensure_ascii=False)
        if "repo_b" in messages[1]["content"]:
            raise ValueError("模拟坏输出")
        return json.dumps({"section": SECTION_A}, ensure_ascii=False)

    mod.run(make_input(tmp_path, [ENTRY_A, ENTRY_B]), str(out), llm_call=llm_fail_b, cfg={})
    summary_msgs = next(m for m in calls if "精炼层" in m[0]["content"])
    assert "repo_a" in summary_msgs[1]["content"]
    assert "repo_b" not in summary_msgs[1]["content"]


def test_run_survives_single_section_failure(tmp_path):
    out = tmp_path / "draft.md"
    calls = []

    def llm(messages, cfg):
        calls.append(messages)
        if "精炼层" in messages[0]["content"]:
            return json.dumps({"summary": SUMMARY}, ensure_ascii=False)
        if "repo_b" in messages[1]["content"]:
            raise ValueError("模拟坏输出")
        return json.dumps({"section": SECTION_A}, ensure_ascii=False)

    mod.run(make_input(tmp_path, [ENTRY_A, ENTRY_B]), str(out), llm_call=llm, cfg={})
    saved = out.read_text(encoding="utf-8")
    assert "### 1. repo_a" in saved
    assert "repo_b" in saved and "模拟坏输出" in saved, "综合失败仓库列待重跑但不中断"
    assert SUMMARY in saved, "摘要仍生成"
    assert "### 2." not in saved, "失败的 repo_b 不占 detail 节编号"


def test_run_summary_failure_keeps_detail_sections(tmp_path):
    out = tmp_path / "draft.md"

    def llm(messages, cfg):
        if "精炼层" in messages[0]["content"]:
            raise ValueError("模拟摘要超时")
        return json.dumps({"section": SECTION_A}, ensure_ascii=False)

    mod.run(make_input(tmp_path, [ENTRY_A]), str(out), llm_call=llm, cfg={})
    saved = out.read_text(encoding="utf-8")
    assert "### 1. repo_a" in saved
    assert "生成失败" in saved, "摘要失败时草稿留占位且不丢 detail 节"


def test_run_missing_input_exits_without_draft(tmp_path):
    out = tmp_path / "draft.md"
    with pytest.raises(SystemExit):
        mod.run(str(tmp_path / "nope.json"), str(out), llm_call=lambda m, c: pytest.fail("不应调用 LLM"), cfg={})
    assert not out.exists()


def test_run_all_entries_errored_exits_without_draft(tmp_path):
    out = tmp_path / "draft.md"
    with pytest.raises(SystemExit):
        mod.run(make_input(tmp_path, [ENTRY_BAD]), str(out), llm_call=lambda m, c: pytest.fail("不应调用 LLM"), cfg={})
    assert not out.exists()


def test_run_creates_missing_parent_dirs(tmp_path):
    out = tmp_path / "kb" / "draft" / "draft.md"
    mod.run(make_input(tmp_path, [ENTRY_A]), str(out), llm_call=llm_by_stage(), cfg={})
    assert out.exists()


def test_run_prints_merge_guidance(tmp_path, capsys):
    mod.run(make_input(tmp_path, [ENTRY_A]), str(tmp_path / "draft.md"), llm_call=llm_by_stage(), cfg={})
    captured = capsys.readouterr().out
    assert "合并指引" in captured
    assert "private/kb/repos_detail.md" in captured
    assert "private/profile_background.md" in captured
    assert "docs/kb-sop.md" in captured
