#!/usr/bin/env python3
"""repo_survey 的行为测试：采样过滤、prompt 组装、LLM 注入、upsert 落盘、dry-run、失败隔离。"""

import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("repo_survey", Path(__file__).resolve().parent / "repo_survey.py")
assert _spec is not None and _spec.loader is not None, "importlib spec 加载失败（模块文件缺失？）"
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

LLM_OUT = json.dumps(
    {
        "positioning": "演示仓库",
        "tech_stack": "TypeScript + React",
        "architecture_summary": "src 平铺",
        "highlights": ["有 README"],
        "red_flags": [],
    },
    ensure_ascii=False,
)


def make_repo(tmp_path, name="demo"):
    repo = tmp_path / name
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("# demo\n一个演示仓库", encoding="utf-8")
    return repo


# --- 采样规则 ---


def test_sample_files_skips_ignored_dirs_binary_and_large(tmp_path):
    repo = tmp_path / "demo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text("gitconfig")
    (repo / "node_modules" / "x").mkdir(parents=True)
    (repo / "node_modules" / "x" / "a.js").write_text("x")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "m.pyc").write_bytes(b"\x00\x01")
    (repo / "dist").mkdir()
    (repo / "dist" / "bundle.js").write_text("x")
    (repo / "build").mkdir()
    (repo / "build" / "out.js").write_text("x")
    for agent_dir in (".agents", ".cursor", ".codeartsdoer", ".claude", ".opencode", ".amp"):
        (repo / agent_dir).mkdir()
        (repo / agent_dir / "config.json").write_text("{}")
    (repo / "logo.png").write_bytes(b"\x89PNG")
    (repo / "big.txt").write_text("x" * (200 * 1024 + 1))
    (repo / "README.md").write_text("# demo")
    (repo / "main.py").write_text("print(1)")
    names = [str(f) for f in mod.sample_files(repo)]
    assert "README.md" in names and "main.py" in names
    assert all(
        not n.startswith(
            (
                ".git",
                "node_modules",
                "__pycache__",
                "dist",
                "build",
                ".agents",
                ".cursor",
                ".codeartsdoer",
                ".claude",
                ".opencode",
                ".amp",
            )
        )
        for n in names
    )
    assert "logo.png" not in names and "big.txt" not in names


def test_sample_files_skips_git_pointer_file(tmp_path):
    """git worktree 里 .git 是指针文件而非目录，同样要跳过。"""
    repo = tmp_path / "wt"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: /somewhere\n")
    (repo / "main.py").write_text("x")
    assert [str(f) for f in mod.sample_files(repo)] == ["main.py"]


def test_sample_files_caps_at_max_and_keeps_priority_files(tmp_path):
    repo = tmp_path / "demo"
    repo.mkdir()
    (repo / "README.md").write_text("r")
    (repo / "package.json").write_text("{}")
    for i in range(80):
        (repo / f"mod_{i:03}.py").write_text("x")
    files = mod.sample_files(repo, max_files=60)
    assert len(files) == 60
    rels = [str(f) for f in files]
    assert "README.md" in rels and "package.json" in rels, "优先文件不能被普通文件挤掉"
    assert rels.index("README.md") < rels.index("mod_000.py")


def test_sample_files_prioritizes_ci_workflow_files(tmp_path):
    """.github/workflows 下的 workflow 与 .gitlab-ci.yml 等按 CI 配置优先入选。"""
    repo = tmp_path / "demo"
    wf = repo / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text("on: push")
    (repo / ".gitlab-ci.yml").write_text("build: pass")
    (repo / "mod.py").write_text("x")
    rels = [str(f) for f in mod.sample_files(repo)]
    assert rels.index(".github/workflows/ci.yml") < rels.index("mod.py")
    assert rels.index(".gitlab-ci.yml") < rels.index("mod.py")


# --- LLM 输出 JSON 提取（复用 gen_greetings 思路） ---


def test_extract_json_tolerates_fence():
    assert mod.extract_json('```json\n{"a": 1}\n```') == {"a": 1}


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
        return {"choices": [{"message": {"content": '{"a": 1}'}}]}

    out = mod.call_llm(
        [{"role": "user", "content": "hi"}],
        {"base_url": "http://x/v1", "model": "m", "api_key": "k"},
        transport=transport,
    )
    assert out == '{"a": 1}'
    assert captured["url"] == "http://x/v1/chat/completions"
    assert captured["payload"]["model"] == "m"
    assert captured["headers"]["Authorization"] == "Bearer k"


def test_call_llm_falls_back_to_reasoning_when_content_empty():
    """qwen3 思考模式兜底：兼容端点 content 为空时回退 reasoning 字段。"""

    def transport(url, payload, headers):
        return {"choices": [{"message": {"content": "", "reasoning": '{"a": 1}'}}]}

    out = mod.call_llm(
        [{"role": "user", "content": "hi"}], {"base_url": "x", "model": "m", "api_key": ""}, transport=transport
    )
    assert out == '{"a": 1}'


def test_survey_prompt_disables_thinking():
    assert "/no_think" in mod.SURVEY_SYSTEM


# --- prompt 组装 ---


def test_build_survey_messages_contains_files_and_snippets():
    msgs = mod.build_survey_messages(
        "demo", "/tmp/demo", [Path("README.md"), Path("src/index.ts")], "## README.md\n# demo"
    )
    assert msgs[0]["role"] == "system"
    user = msgs[1]["content"]
    assert "demo" in user and "/tmp/demo" in user
    assert "README.md" in user and "src/index.ts" in user
    assert "# demo" in user


def test_build_snippets_reads_and_truncates_per_file(tmp_path):
    repo = tmp_path / "demo"
    repo.mkdir()
    (repo / "README.md").write_text("A" * 3000, encoding="utf-8")
    (repo / "package.json").write_text("B" * 3000, encoding="utf-8")
    text = mod.build_snippets(repo, mod.sample_files(repo))
    assert "README.md" in text and "package.json" in text
    assert "A" * 1500 in text and "A" * 1600 not in text, "单文件片段截到 SNIPPET_PER_FILE"
    assert len(text) < mod.MAX_SNIPPET_CHARS


def test_build_snippets_global_cap_truncates_across_files(tmp_path, monkeypatch):
    """多个长优先文件：片段总量到 MAX_SNIPPET_CHARS 后 [:remaining] 截断、remaining<=0 停止追加。"""
    monkeypatch.setattr(mod, "MAX_SNIPPET_CHARS", 5000)
    repo = tmp_path / "demo"
    repo.mkdir()
    names = ["README.md", "package.json", "pyproject.toml", "Makefile", "Dockerfile", "Gemfile"]
    for name in names:
        (repo / name).write_text("x" * 1500, encoding="utf-8")
    text = mod.build_snippets(repo, [Path(n) for n in names])
    contents = [p.split("\n", 1)[1] for p in text.split("\n\n")]  # 去掉 "## 文件名" 头
    assert sum(len(c) for c in contents) <= mod.MAX_SNIPPET_CHARS, "片段总量不超过全局上限"
    assert [len(c) for c in contents] == [1500, 1500, 1500, 500], (
        "前 3 个整段、第 4 个被 [:remaining] 截断、之后因 remaining<=0 丢弃"
    )
    assert "Makefile" in text and "Dockerfile" not in text, "超限后的文件不再进片段"


# --- 单仓调研 ---


def test_survey_repo_success_entry_fields(tmp_path):
    repo = make_repo(tmp_path)

    def llm(messages, cfg):
        return f"```json\n{LLM_OUT}\n```"

    entry = mod.survey_repo(repo, llm, {}, now=datetime(2026, 9, 7, 12, 0, 0))
    assert entry["repo"] == "demo"
    assert entry["positioning"] == "演示仓库"
    assert entry["tech_stack"] == "TypeScript + React"
    assert entry["architecture_summary"] == "src 平铺"
    assert entry["highlights"] == ["有 README"] and entry["red_flags"] == []
    assert entry["files_sampled"] == 1
    assert entry["surveyed_at"] == "2026-09-07T12:00:00"
    assert "error" not in entry


def test_survey_repo_llm_failure_records_error(tmp_path):
    repo = make_repo(tmp_path)

    def llm(messages, cfg):
        raise ValueError("模拟超时")

    entry = mod.survey_repo(repo, llm, {})
    assert entry["error"] == "模拟超时"
    assert entry["repo"] == "demo" and entry["files_sampled"] == 1


def test_survey_repo_bad_json_records_error(tmp_path):
    repo = make_repo(tmp_path)
    entry = mod.survey_repo(repo, lambda m, c: "完全没有 JSON", {})
    assert "error" in entry


def test_survey_repo_empty_repo_errors_without_llm(tmp_path):
    repo = tmp_path / "empty_repo"
    repo.mkdir()

    def llm(messages, cfg):
        raise AssertionError("空仓库不应调用 LLM")

    entry = mod.survey_repo(repo, llm, {})
    assert "没有可采样文件" in entry["error"]
    assert entry["files_sampled"] == 0


# --- upsert 落盘 ---


def test_upsert_replaces_same_repo_appends_new_keeps_order():
    existing = [{"repo": "a", "v": 1}, {"repo": "b", "v": 1}]
    merged = mod.upsert_repos(existing, [{"repo": "b", "v": 2}, {"repo": "c", "v": 1}])
    assert [e["repo"] for e in merged] == ["a", "b", "c"]
    assert merged[1]["v"] == 2


def test_upsert_error_result_keeps_existing_good_entry():
    """新条目带 error 而旧档案无 error 时保留旧档案；反之旧档案是 error 则被成功结果替换。"""
    good = [{"repo": "a", "positioning": "好档案"}]
    assert mod.upsert_repos(good, [{"repo": "a", "error": "模拟超时"}]) == good, "error 结果不覆盖好档案"
    merged = mod.upsert_repos([{"repo": "b", "error": "旧失败"}], [{"repo": "b", "positioning": "新档案"}])
    assert merged[0]["positioning"] == "新档案", "旧 error 档案应被成功结果替换"


# --- 主流程（LLM 注入） ---


def test_run_surveys_all_and_survives_single_failure(tmp_path):
    ra = make_repo(tmp_path, "repo_a")
    rb = make_repo(tmp_path, "repo_b")
    out = tmp_path / "repos_detail.json"

    def llm(messages, cfg):
        if "repo_b" in messages[1]["content"]:
            raise ValueError("模拟超时")
        return LLM_OUT

    results = mod.run([str(ra), str(rb)], str(out), max_files=60, force=False, dry_run=False, llm_call=llm, cfg={})
    assert [e["repo"] for e in results] == ["repo_a", "repo_b"], "失败的 repo_b 不中断整批"
    assert results[0]["positioning"] == "演示仓库"
    assert "error" in results[1]
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert len(saved) == 2, "成功档案与 error 档案都落盘"


def test_run_upserts_by_repo_name(tmp_path):
    ra = make_repo(tmp_path, "repo_a")
    rb = make_repo(tmp_path, "repo_b")
    out = tmp_path / "repos_detail.json"
    out.write_text(json.dumps([{"repo": "repo_a", "positioning": "旧档案"}], ensure_ascii=False), encoding="utf-8")
    mod.run(
        [str(ra), str(rb)], str(out), max_files=60, force=False, dry_run=False, llm_call=lambda m, c: LLM_OUT, cfg={}
    )
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert [e["repo"] for e in saved] == ["repo_a", "repo_b"]
    assert saved[0]["positioning"] == "演示仓库", "同名 repo 应被本批结果替换"
    assert saved[0]["surveyed_at"]


def test_run_force_overwrites(tmp_path):
    ra = make_repo(tmp_path, "repo_a")
    out = tmp_path / "repos_detail.json"
    out.write_text(json.dumps([{"repo": "old_repo", "positioning": "旧"}], ensure_ascii=False), encoding="utf-8")
    mod.run([str(ra)], str(out), max_files=60, force=True, dry_run=False, llm_call=lambda m, c: LLM_OUT, cfg={})
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert [e["repo"] for e in saved] == ["repo_a"]


def test_dry_run_no_llm_no_write(tmp_path, capsys):
    ra = make_repo(tmp_path, "repo_a")
    out = tmp_path / "repos_detail.json"

    def llm(messages, cfg):
        raise AssertionError("dry-run 不应调用 LLM")

    results = mod.run([str(ra)], str(out), max_files=60, force=False, dry_run=True, llm_call=llm, cfg={})
    assert results == []
    assert not out.exists()
    captured = capsys.readouterr()
    assert "repo_a" in captured.out and "1 个文件" in captured.out


def test_run_resolves_dot_path_for_repo_name(tmp_path):
    ra = make_repo(tmp_path, "repo_a")
    out = tmp_path / "repos_detail.json"
    results = mod.run(
        [str(ra) + "/."], str(out), max_files=60, force=False, dry_run=False, llm_call=lambda m, c: LLM_OUT, cfg={}
    )
    assert results[0]["repo"] == "repo_a", "相对路径要 resolve 出正确的 repo 名"


def test_run_skips_missing_path(tmp_path):
    out = tmp_path / "repos_detail.json"
    results = mod.run(
        [str(tmp_path / "nope")],
        str(out),
        max_files=60,
        force=False,
        dry_run=False,
        llm_call=lambda m, c: LLM_OUT,
        cfg={},
    )
    assert results == []
    assert not out.exists()


# --- 输入解析 ---


def test_load_repo_paths_from_comma_arg():
    assert mod.load_repo_paths("/a/b, /c/d ,", None) == ["/a/b", "/c/d"]


def test_load_repo_paths_from_file_with_comments(tmp_path):
    f = tmp_path / "repos.txt"
    f.write_text("# 注释\n/a/b\n\n  /c/d  \n", encoding="utf-8")
    assert mod.load_repo_paths("", str(f)) == ["/a/b", "/c/d"]


def test_load_repo_paths_default_missing_exits():
    with pytest.raises(SystemExit):
        mod.load_repo_paths("", "/nonexistent/repos_list.txt")


def test_load_repo_paths_repos_arg_wins(tmp_path):
    f = tmp_path / "repos.txt"
    f.write_text("/file/path\n", encoding="utf-8")
    assert mod.load_repo_paths("/arg/path", str(f)) == ["/arg/path"]
