#!/usr/bin/env python3
"""#51 熔断/停批事件自动建 issue：`_issue_reporter` 单元测试。

四条验收主轴（票面验收 2-6）：默认关闭零远端写 / 显式开启正确建票 /
本地防重不重复 / 子进程异常全降级。脱敏纪律（白名单字段 + scrub 兜底）单列。
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _issue_reporter as ir  # noqa: E402


@pytest.fixture(autouse=True)
def _issue_repo(monkeypatch):
    """开票目标仓由 env 给定：测试不依赖运行机器上的 git remote。"""
    monkeypatch.setenv("AUTOJOB_ISSUE_REPO", "0xExample/auto-job-cn")


# --- 草稿渲染与脱敏纪律（票面：正文只含 breaker 类型、计数、阈值、session ID） ---


def test_draft_issue_keeps_whitelist_fields_masks_credentials():
    """白名单机械字段进正文；公司/岗位/招呼语等白名单外字段严禁出现；
    字段值过 scrub_text 兜底凭证类 PII。"""
    event = {
        "breaker": "risk_blocked",
        "stage": "pre_click",
        "detail": "安全验证提示 token=测试假值零零七",  # 低熵占位——高熵假串会被 gitleaks generic-api-key 误拦
        "jobName": "SRE工程师",
        "brandName": "某公司",
        "greeting": "您好我是三年代码 QA",
    }
    d = ir.draft_issue(event, "20260926-000000")
    assert d["breaker"] == "risk_blocked"
    assert "risk_blocked" in d["title"] and "20260926-000000" in d["title"]
    assert "token=******" in d["body"], "凭证类字段值须被 scrub 掩码"
    assert "安全验证提示" in d["body"] and "pre_click" in d["body"]
    assert "SRE工程师" not in d["body"], "岗位名严禁上票"
    assert "某公司" not in d["body"], "公司名严禁上票"
    assert "您好我是" not in d["body"], "招呼语严禁上票"


def test_draft_issue_unknown_breaker_renders_minimal():
    """未登记 breaker 走最小集：标题照出、白名单外字段（含未知键）一律不进正文。"""
    d = ir.draft_issue({"breaker": "new_breaker", "mystery": "SHOULD_NOT_LEAK"}, "s1")
    assert "new_breaker" in d["title"]
    assert "SHOULD_NOT_LEAK" not in d["body"]


def test_draft_issue_detail_truncated():
    """detail 服务端消息可能很长：截断到 _DETAIL_MAX，正文不被刷屏。"""
    d = ir.draft_issue({"breaker": "apply_paused", "code": 37, "detail": "x" * 500}, "s1")
    assert len(d["body"]) < 900, "detail 须截断（500 字符原样上票会刷屏）"
    assert "x" * 500 not in d["body"]


def test_drafts_for_maps_all_events():
    events = [{"breaker": "consec_no_btn", "count": 3}, {"breaker": "apply_paused", "code": 38}]
    drafts = ir.drafts_for(events, "s1")
    assert [d["breaker"] for d in drafts] == ["consec_no_btn", "apply_paused"]
    assert ir.drafts_for(None, "s1") == []


# --- 防重台账（票面验收 4：同 session 同 breaker 绝不重复开票） ---


def test_load_reported_missing_and_corrupt(tmp_path, capsys):
    """缺失 → 空台账；坏 JSON / 非对象形态 → 空台账 + warn（不代猜不崩溃）。"""
    assert ir.load_reported(tmp_path / "nope.json") == {}
    p = tmp_path / "reported.json"
    p.write_text("not json", encoding="utf-8")
    assert ir.load_reported(p) == {}
    assert "[warn]" in capsys.readouterr().out
    p.write_text("[1, 2]", encoding="utf-8")
    assert ir.load_reported(p) == {}, "非对象形态同样回落空台账"


def test_mark_reported_roundtrip(tmp_path):
    p = tmp_path / "reported.json"
    ir.mark_reported(p, "s1", "direct_fallback", "https://github.com/r/issues/9", now=datetime(2026, 9, 26, 1, 2, 3))
    reported = ir.load_reported(p)
    assert ir.is_reported(reported, "s1", "direct_fallback")
    assert not ir.is_reported(reported, "s1", "consec_no_btn"), "不同 breaker 互不干扰"
    assert not ir.is_reported(reported, "s2", "direct_fallback"), "不同 session 互不干扰"
    assert reported["s1"]["direct_fallback"]["url"] == "https://github.com/r/issues/9"
    assert reported["s1"]["direct_fallback"]["filed_at"] == "2026-09-26T01:02:03"


def test_reported_path_for_matches_default_resolution(tmp_path, monkeypatch):
    """review 采纳项：live_apply 与 default_reported_path 共用 reported_path_for——
    同一 prefix 两种入口必须得到同一路径（路径知识单一出口，防裸字符串漂移）。"""
    monkeypatch.setattr(ir, "private_path", lambda rel, prefix, root=None: tmp_path / f"{prefix}.{rel}")
    assert ir.reported_path_for("ai", root=tmp_path) == tmp_path / "ai.data/reported_issues.json"
    monkeypatch.setattr(ir, "resolve_active_profile", lambda root=None, env=None: ("ai", "ai"))
    assert ir.default_reported_path(root=tmp_path, env={}) == ir.reported_path_for("ai", root=tmp_path)


# --- gh 子进程（票面验收 3/5：正确建票 + 异常降级） ---


def test_file_issue_builds_gh_command():
    captured = {}

    def fake_run(cmd, **kw):
        captured.update(cmd=cmd, kw=kw)
        return SimpleNamespace(returncode=0, stdout="https://github.com/r/issues/7\n", stderr="")

    url = ir.file_issue({"title": "T", "body": "B"}, run_fn=fake_run)
    assert url == "https://github.com/r/issues/7"
    cmd = captured["cmd"]
    assert cmd[:3] == ["gh", "issue", "create"]
    assert cmd[cmd.index("--label") + 1] == "needs-triage"
    assert cmd[cmd.index("--title") + 1] == "T" and cmd[cmd.index("--body") + 1] == "B"
    assert "--repo" in cmd
    assert captured["kw"]["timeout"] == ir.GH_TIMEOUT, "必须带超时——卡死不能拖住收尾"


def test_file_issue_nonzero_exit_raises():
    def fake_run(cmd, **kw):
        return SimpleNamespace(returncode=1, stdout="", stderr="gh: auth error")

    with pytest.raises(RuntimeError, match="auth error"):
        ir.file_issue({"title": "T", "body": "B"}, run_fn=fake_run)


# --- 收尾编排：默认 HITL / opt-in 自动（票面验收 2/3/4/5） ---


def test_wrapup_default_off_prompts_without_gh(tmp_path, capsys):
    """默认（AUTOJOB_AUTO_ISSUE 未设）：只打印 💡 提示与确认命令，绝不发起远端写。"""

    def boom(*a, **k):
        raise AssertionError("默认模式绝不允许调 gh")

    report = tmp_path / "report.json"
    reported = tmp_path / "reported.json"
    d = ir.draft_issue({"breaker": "direct_fallback", "rate": 0.8, "fails": 8, "attempts": 10, "threshold": 0.6}, "s1")
    pending = ir.wrapup_report([d], "s1", reported, report_path=report, env={}, run_fn=boom)
    assert len(pending) == 1
    out = capsys.readouterr().out
    assert "💡 [开票提示]" in out and "direct_fallback" in out
    assert "_issue_reporter.py --report" in out and str(report) in out, "确认命令须带 report 路径"
    assert not reported.exists(), "未提交不记账"


def test_wrapup_auto_files_marks_and_dedups(tmp_path, capsys):
    """AUTOJOB_AUTO_ISSUE=1：自动建票 + 记账；同 session 同 breaker 重放被防重拦截。"""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="https://github.com/r/issues/8\n", stderr="")

    reported = tmp_path / "reported.json"
    d = ir.draft_issue({"breaker": "consec_no_btn", "count": 3, "threshold": 3}, "s1")
    ir.wrapup_report([d], "s1", reported, env={"AUTOJOB_AUTO_ISSUE": "1"}, run_fn=fake_run)
    assert len(calls) == 1
    assert "issues/8" in capsys.readouterr().out
    ir.wrapup_report([d], "s1", reported, env={"AUTOJOB_AUTO_ISSUE": "1"}, run_fn=fake_run)
    assert len(calls) == 1, "防重台账生效：同 session 同 breaker 不二次开票"


def test_wrapup_auto_gh_failure_degrades_without_mark(tmp_path, capsys):
    """gh 失败（未登录/502/非零退出）→ [warn] 降级、不记账（可重试）、绝不抛出。"""

    def fake_run(cmd, **kw):
        return SimpleNamespace(returncode=1, stdout="", stderr="connection reset by peer")

    reported = tmp_path / "reported.json"
    d = ir.draft_issue({"breaker": "consec_no_btn", "count": 3, "threshold": 3}, "s1")
    pending = ir.wrapup_report([d], "s1", reported, env={"AUTOJOB_AUTO_ISSUE": "1"}, run_fn=fake_run)
    out = capsys.readouterr().out
    assert pending and "[warn]" in out and "connection reset" in out
    assert ir.load_reported(reported) == {}, "失败不记账——下次仍可重试"


def test_wrapup_auto_gh_timeout_degrades(tmp_path, capsys):
    def slow_run(cmd, **kw):
        raise TimeoutError("timed out")

    reported = tmp_path / "reported.json"
    d = ir.draft_issue({"breaker": "apply_paused", "code": 37, "detail": "风控码"}, "s1")
    pending = ir.wrapup_report([d], "s1", reported, env={"AUTOJOB_AUTO_ISSUE": "1"}, run_fn=slow_run)
    out = capsys.readouterr().out
    assert pending and "[warn]" in out and "timed out" in out
    assert ir.load_reported(reported) == {}


def test_wrapup_auto_mode_rejects_other_values(tmp_path):
    """AUTOJOB_AUTO_ISSUE 非 1 值一律视为 off——远端写开关必须显式。"""

    def boom(*a, **k):
        raise AssertionError("AUTOJOB_AUTO_ISSUE=0 也绝不允许调 gh")

    d = ir.draft_issue({"breaker": "consec_no_btn", "count": 3}, "s1")
    ir.wrapup_report([d], "s1", tmp_path / "r.json", env={"AUTOJOB_AUTO_ISSUE": "0"}, run_fn=boom)


# --- CLI 确认入口（HITL 的「确认动作」本体） ---


def _report_with_drafts(tmp_path, session_id="20260926-000000"):
    report = tmp_path / session_id / "report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    drafts = [
        ir.draft_issue({"breaker": "direct_fallback", "rate": 0.8}, session_id),
        ir.draft_issue({"breaker": "consec_no_btn", "count": 3}, session_id),
    ]
    report.write_text(json.dumps({"issue_drafts": drafts}), encoding="utf-8")
    return report


def test_cli_main_files_pending_from_report(tmp_path, monkeypatch, capsys):
    """执行 main 即确认动作：读 report.json 草稿逐张建票 + 记账；重跑被防重拦截。"""
    report = _report_with_drafts(tmp_path)
    filed = []
    monkeypatch.setattr(
        ir, "file_issue", lambda draft, **kw: filed.append(draft["breaker"]) or f"https://x/{draft['breaker']}"
    )
    reported = tmp_path / "reported.json"
    rc = ir.main(["--report", str(report)], reported_path=reported)
    assert rc == 0 and filed == ["direct_fallback", "consec_no_btn"]
    out = capsys.readouterr().out  # readouterr 只能读一次（drain 语义），先落变量
    assert "https://x/direct_fallback" in out
    rc2 = ir.main(["--report", str(report)], reported_path=reported)
    assert rc2 == 0 and len(filed) == 2, "防重：重跑不重复开票"


def test_cli_main_failure_exit_code(tmp_path, monkeypatch, capsys):
    """建票失败 → [warn] + 退出码 1（annotate 同款友好拒绝语义，操作者可感知）。"""
    report = _report_with_drafts(tmp_path)

    def boom(draft, **kw):
        raise RuntimeError("gh not found")

    monkeypatch.setattr(ir, "file_issue", boom)
    rc = ir.main(["--report", str(report)], reported_path=tmp_path / "r.json")
    assert rc == 1
    out = capsys.readouterr().out
    assert "[warn]" in out and "gh not found" in out


def test_cli_main_no_drafts(tmp_path, capsys):
    report = tmp_path / "report.json"
    report.write_text("{}", encoding="utf-8")
    assert ir.main(["--report", str(report)], reported_path=tmp_path / "r.json") == 0
    assert "无待开票" in capsys.readouterr().out


def test_cli_main_bad_report(tmp_path, capsys):
    rc = ir.main(["--report", str(tmp_path / "nope.json")], reported_path=tmp_path / "r.json")
    assert rc == 1
    assert "[error]" in capsys.readouterr().out
