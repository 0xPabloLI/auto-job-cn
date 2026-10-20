#!/usr/bin/env python3
"""orchestrate 的行为测试：config 极小 YAML 子集解析（注释/列表/引号/缺文件）、
阶段选择与顺序、参数映射（runner 收到的 argv/env 断言）、失败中止语义
（非 0 退出码中止且不跑后续）、HITL 卡点（回车/y/其他/EOF/--yes）。
全部注入式：runner/input_fn/config_loader 均为替身，不打真网真 LLM、不跑真子进程。"""

import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("orchestrate", Path(__file__).resolve().parent / "orchestrate.py")
assert _spec is not None and _spec.loader is not None, "importlib spec 加载失败（模块文件缺失？）"
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


# --- 替身工具 ---


def make_runner(fail=None):
    """runner 替身：fail 为 {argv 片段: 退出码}；记录每次调用 (argv, env)。"""
    calls = []

    def run(argv, env=None):
        call = {"argv": list(argv), "env": env}
        calls.append(call)
        if "--repos-file" in argv:
            # 临时 repos 文件在编排结束即清理，runner 调用时快照内容供断言
            p = argv[argv.index("--repos-file") + 1]
            call["repos_file_content"] = Path(p).read_text(encoding="utf-8")
        joined = " ".join(argv)
        for frag, code in (fail or {}).items():
            if frag in joined:
                return code
        return 0

    return run, calls


def make_ask(answers, prompts=None):
    """input_fn 替身：按序返回 answers，记录收到的 prompt。"""
    it = iter(answers)

    def ask(prompt=""):
        if prompts is not None:
            prompts.append(prompt)
        return next(it)

    return ask


def drive(
    cfg=None, stages="", skip="", yes=False, fail=None, answers=(), greetings_path=None, now_fn=None, monotonic_fn=None
):
    """驱动 orchestrate：注入 config_loader/runner/input_fn，返回 (退出码, calls, prompts)。"""
    runner, calls = make_runner(fail)
    prompts = []
    ask = make_ask(list(answers), prompts)
    code = mod.orchestrate(
        stages=stages,
        skip=skip,
        yes=yes,
        config_loader=lambda: cfg if cfg is not None else {},
        runner=runner,
        input_fn=ask,
        now_fn=now_fn,
        monotonic_fn=monotonic_fn,
        greetings_path=greetings_path or Path("/nonexistent/greetings.json"),
    )
    return code, calls, prompts


def script_names(calls):
    return [Path(c["argv"][1]).name for c in calls]


def write_greetings(tmp_path, n=2, text_len=60):
    p = tmp_path / "greetings.json"
    jobs = [
        {"encryptJobId": str(i), "jobName": f"岗位{i}", "brandName": f"品牌{i}", "greeting": "好" * text_len}
        for i in range(1, n + 1)
    ]
    p.write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")
    return p


# --- config 极小 YAML 子集解析 ---


def test_parse_mini_yaml_scalar_list_comments_quotes():
    text = """# 整行注释
industry_query: Web3 招聘 # 行内注释
twitter_handles:
  - alice
- bob
apply_limit: 5
quoted: "a: b # 不是注释"
url_no_space: https://x.com/a#frag
"""
    data = mod.parse_mini_yaml(text)
    assert data["industry_query"] == "Web3 招聘"
    assert data["twitter_handles"] == ["alice", "bob"], "列表项缩进不敏感"
    assert data["apply_limit"] == "5", "标量一律按字符串读，使用处再转 int"
    assert data["quoted"] == "a: b # 不是注释", "引号内冒号/# 原样保留"
    assert data["url_no_space"] == "https://x.com/a#frag", "# 前无空白不视为注释"


def test_parse_mini_yaml_quote_variants():
    assert mod.parse_mini_yaml("a: '单引号: 值'")["a"] == "单引号: 值"
    assert mod.parse_mini_yaml('a: ""')["a"] == ""
    assert mod.parse_mini_yaml('a: "x" # 注释')["a"] == "x"


def test_parse_mini_yaml_comment_right_after_colon_is_empty_value():
    data = mod.parse_mini_yaml("k: # 注释\n  - item")
    assert data["k"] == ["item"]


def test_parse_mini_yaml_errors():
    with pytest.raises(ValueError):
        mod.parse_mini_yaml("没有冒号的行")
    with pytest.raises(ValueError):
        mod.parse_mini_yaml("- stray"), "列表项缺所属键"
    with pytest.raises(ValueError):
        mod.parse_mini_yaml('a: "未闭合')
    with pytest.raises(ValueError):
        mod.parse_mini_yaml('a: "x" oops'), "引号闭合后仍有非注释内容"
    with pytest.raises(ValueError):
        mod.parse_mini_yaml("k: v\n- stray"), "标量键下接列表项"
    with pytest.raises(ValueError):
        mod.parse_mini_yaml("my key: v"), "键含空格"


def test_load_config_missing_returns_empty(tmp_path):
    assert mod.load_config(tmp_path / "nope.yaml") == {}


def test_load_config_reads_yaml(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("industry_query: Web3\n", encoding="utf-8")
    assert mod.load_config(p) == {"industry_query": "Web3"}


# --- 阶段选择与顺序 ---


def test_default_full_run_sequence(tmp_path, capsys):
    g = write_greetings(tmp_path)
    code, calls, prompts = drive(answers=[""], greetings_path=g)
    assert code == 0
    assert script_names(calls) == [
        "repo_survey.py",
        "build_kb.py",
        "industry_research.py",
        "gen_greetings.py",
        "followup_tracker.py",
        "gen_greetings.py",
    ], "crawl/apply 阶段已随 #44 清理，投递由 agent 会话跑 live_apply"
    gen_cmds = [" ".join(c["argv"][2:]) for c in calls if Path(c["argv"][1]).name == "gen_greetings.py"]
    assert gen_cmds == ["promote", "learn"], "generate 已退役，只剩 promote/learn"
    out = capsys.readouterr().out
    assert "twitter_handles" in out, "未配置 handles → twitter_survey 跳过并说明"
    assert "人工审核合并" in out, "build_kb 后打印人工合并提醒（不阻塞）"
    assert len(prompts) == 1 and prompts[0].startswith("审核完成按回车继续"), "只剩卡点1（投递已移出编排）"
    assert "总计" in out, "结束打印各阶段耗时汇总表"


def test_stages_subset_keeps_canonical_order(tmp_path):
    code, calls, _ = drive(stages="promote,followup,learn", yes=True)
    assert code == 0
    assert script_names(calls) == ["gen_greetings.py", "followup_tracker.py", "gen_greetings.py"]


def test_skip_excludes_stages(tmp_path):
    code, calls, _ = drive(skip="build_kb,industry_research,twitter_survey,promote,learn", yes=True)
    assert code == 0
    assert script_names(calls) == ["repo_survey.py", "followup_tracker.py"]


def test_stages_plus_skip_combined(tmp_path):
    code, calls, _ = drive(stages="promote,followup,learn", skip="followup", yes=True)
    assert code == 0
    assert script_names(calls) == ["gen_greetings.py", "gen_greetings.py"]


def test_unknown_stage_name_returns_2(capsys):
    code, calls, _ = drive(stages="craw,promote")
    assert code == 2 and calls == []
    assert "未知阶段名" in capsys.readouterr().out


def test_unknown_skip_stage_name_returns_2(capsys):
    """--skip 里的未知阶段名同样报错（--stages/--skip 共用校验）。"""
    code, calls, _ = drive(skip="promote,nosuch")
    assert code == 2 and calls == []
    out = capsys.readouterr().out
    assert "未知阶段名" in out and "nosuch" in out


def test_blank_stage_list_errors_not_full_run(capsys):
    """--stages/--skip 原始串非空但只有空白/逗号 → 报错退出 2，不静默回退全跑。"""
    code, calls, _ = drive(stages="  ")
    assert code == 2 and calls == []
    out = capsys.readouterr().out
    assert "--stages" in out and "未解析出任何阶段名" in out
    code, calls, _ = drive(stages=" , ")
    assert code == 2 and calls == []
    capsys.readouterr()
    code, calls, _ = drive(skip="  ")
    assert code == 2 and calls == []
    assert "--skip" in capsys.readouterr().out


def test_empty_selection_returns_2(capsys):
    code, calls, _ = drive(stages="followup", skip="followup")
    assert code == 2 and calls == []
    assert "没有要运行的阶段" in capsys.readouterr().out


# --- 参数映射（runner 收到的 argv/env） ---


def test_repos_config_writes_temp_file_and_cleans_up(capsys):
    code, calls, _ = drive(cfg={"repos": ["/repos/a", "/repos/b"]}, stages="repo_survey")
    assert code == 0
    argv = calls[0]["argv"]
    assert "--repos-file" in argv
    assert calls[0]["repos_file_content"].splitlines() == ["/repos/a", "/repos/b"]
    repos_file = argv[argv.index("--repos-file") + 1]
    assert not Path(repos_file).exists(), "临时 repos 文件用完即删"


def test_industry_query_mapped(capsys):
    code, calls, _ = drive(cfg={"industry_query": "Solidity 招聘"}, stages="industry_research")
    assert code == 0
    argv = calls[0]["argv"]
    assert argv[argv.index("--query") + 1] == "Solidity 招聘"


def test_twitter_handles_loop_one_subprocess_per_handle(capsys):
    code, calls, _ = drive(cfg={"twitter_handles": ["alice", "bob"]}, stages="twitter_survey")
    assert code == 0
    assert [" ".join(c["argv"][2:]) for c in calls] == ["--handle alice", "--handle bob"]
    out = capsys.readouterr().out
    assert out.count("[1/1] 阶段 twitter_survey") == 1, "多 handle 属同一阶段，只有一条开始横幅"


def test_twitter_survey_without_handles_skips(capsys):
    code, calls, _ = drive(stages="twitter_survey")
    assert code == 0 and calls == []
    assert "twitter_handles" in capsys.readouterr().out


def test_apply_limit_key_no_longer_supported(capsys):
    """apply_limit 原映射 batch_apply_v2 的 APPLY_DAILY_LIMIT env——该阶段已随 #44 清理，
    键降级为未知键：告警忽略，不静默生效。"""
    code, calls, _ = drive(cfg={"apply_limit": "5"}, stages="followup", yes=True)
    assert code == 0
    assert calls[0]["env"] is None, "不再有按键注入的环境变量"
    out = capsys.readouterr().out
    assert "apply_limit" in out and "不受支持" in out


def test_repos_scalar_rejected(capsys):
    code, calls, _ = drive(cfg={"repos": "/single"}, stages="repo_survey")
    assert code == 2 and calls == []
    assert "repos" in capsys.readouterr().out


def test_crawl_url_unsupported_warns_not_silent(capsys):
    """crawl_url 无真实映射（爬岗已随 #44 移出编排）→ 明确告警而非静默忽略。"""
    code, calls, _ = drive(cfg={"crawl_url": "https://example.com/list"}, stages="repo_survey", yes=True)
    assert code == 0 and len(calls) == 1
    assert "crawl_url" in capsys.readouterr().out


# --- 失败中止语义 ---


def test_failure_aborts_before_next_stage(capsys):
    code, calls, _ = drive(stages="repo_survey,build_kb", yes=True, fail={"repo_survey": 1})
    assert code == 1
    assert script_names(calls) == ["repo_survey.py"], "失败后不跑后续阶段"
    out = capsys.readouterr().out
    assert "[编排中止] 阶段 repo_survey 失败（退出码 1）" in out
    assert "[建议]" in out


def test_failure_exit_code_passthrough(capsys):
    code, calls, _ = drive(stages="build_kb", fail={"build_kb": 3})
    assert code == 3
    out = capsys.readouterr().out
    assert "退出码 3" in out


# --- 耗时测量（单调钟） ---


def test_durations_use_monotonic_clock(capsys):
    """阶段耗时/汇总表用单调钟差值；横幅时间戳保留墙钟 now_fn。
    墙钟每次调用 +10s、单调钟每次 +1.5s：若误用墙钟测耗时会被 10.0s 断言抓住。"""
    wall = {"t": datetime(2026, 1, 1, 8, 0, 0)}
    mono = {"t": 1000.0}

    def now_fn():
        wall["t"] += timedelta(seconds=10)
        return wall["t"]

    def monotonic_fn():
        mono["t"] += 1.5
        return mono["t"]

    code, calls, _ = drive(stages="build_kb", now_fn=now_fn, monotonic_fn=monotonic_fn)
    assert code == 0 and len(calls) == 1
    out = capsys.readouterr().out
    assert "开始 2026-01-01 08:00:10" in out, "横幅时间戳保留墙钟 datetime"
    assert "10.0s" not in out, "耗时不得混入墙钟差值"
    assert "[阶段完成] build_kb 耗时 1.5s" in out, "阶段耗时 = 单调钟两次调用差值"
    assert f"  {'build_kb':<20s} {1.5:7.1f}s" in out, "汇总表按阶段行"
    assert f"  {'总计':<20s} {1.5:7.1f}s" in out, "汇总表总计行"


# --- HITL 卡点（#44 后只剩卡点1 草稿审核；投递已移出编排） ---


def test_checkpoint1_review_then_promote(tmp_path, capsys):
    g = write_greetings(tmp_path)
    code, calls, prompts = drive(stages="promote,followup", answers=[""], greetings_path=g)
    assert code == 0
    assert script_names(calls) == ["gen_greetings.py", "followup_tracker.py"]
    assert prompts[0].startswith("审核完成按回车继续"), "卡点1：回车继续"
    assert "status 改为 approved" in capsys.readouterr().out


def test_checkpoint_eof_aborts(tmp_path, capsys):
    g = write_greetings(tmp_path)

    def eof(prompt=""):
        raise EOFError

    runner, calls = make_runner()
    code = mod.orchestrate(stages="promote", config_loader=lambda: {}, runner=runner, input_fn=eof, greetings_path=g)
    assert code == 1 and calls == []
    out = capsys.readouterr().out
    assert "非交互" in out and "--yes" in out, "EOF 提示需交互或用 --yes"


def test_yes_skips_all_checkpoints(tmp_path):
    g = write_greetings(tmp_path)

    def boom(prompt=""):
        raise AssertionError("卡点不应被触发")

    runner, calls = make_runner()
    code = mod.orchestrate(
        stages="promote,followup", yes=True, config_loader=lambda: {}, runner=runner, input_fn=boom, greetings_path=g
    )
    assert code == 0
    assert script_names(calls) == ["gen_greetings.py", "followup_tracker.py"]


def test_no_checkpoint_when_promote_not_selected():
    code, calls, prompts = drive(stages="repo_survey,followup")
    assert code == 0 and len(calls) == 2 and prompts == []


# --- main 接线 ---


def test_main_list_stages(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["orchestrate.py", "--list-stages"])
    assert mod.main() == 0
    out = capsys.readouterr().out
    for name in (
        "repo_survey",
        "build_kb",
        "industry_research",
        "twitter_survey",
        "promote",
        "followup",
        "learn",
    ):
        assert name in out
    assert "卡点" in out and "config.yaml" in out
