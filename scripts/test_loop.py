#!/usr/bin/env python3
"""loop 的行为测试：回复率推导与判定边界（< vs ≥、样本不足）、次数上限防死循环、
build_kb 开关与 subprocess 顺序、失败中止、state 读写与 --reset、报告缺失/损坏。
全部注入式：runner/report_loader/state store/now 均为替身或临时文件，
不打真网真 LLM、不跑真子进程。"""

import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("loop", Path(__file__).resolve().parent / "loop.py")
assert _spec is not None and _spec.loader is not None, "importlib spec 加载失败（模块文件缺失？）"
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

NOW = datetime(2026, 9, 7, 12, 0, 0)
NOW_S = "2026-09-07 12:00:00"


# --- 替身工具 ---


def make_runner(fail=None):
    """runner 替身：fail 为 {argv 片段: 退出码}；记录每次调用的 argv。"""
    calls = []

    def run(argv):
        calls.append(list(argv))
        joined = " ".join(argv)
        for frag, code in (fail or {}).items():
            if frag in joined:
                return code
        return 0

    return run, calls


def make_report(applied_replied=0, applied_awaiting=0, other_convs=0):
    """followup_report.json 替身（schema 对齐 platforms/chat_first.py build_report）。"""
    convs = []
    for i in range(applied_replied):
        convs.append(
            {
                "name": f"张{i}",
                "company": f"回复公司{i}",
                "summary": "你好，看下简历",
                "time": "09-01",
                "status": "hr_replied",
                "appliedJob": f"岗位{i}",
            }
        )
    for i in range(applied_awaiting):
        convs.append(
            {
                "name": f"李{i}",
                "company": f"沉默公司{i}",
                "summary": "[送达] 您好",
                "time": "09-02",
                "status": "awaiting",
                "appliedJob": f"岗位B{i}",
            }
        )
    for i in range(other_convs):
        convs.append(
            {
                "name": f"王{i}",
                "company": f"陌拜公司{i}",
                "summary": "我们直招",
                "time": "09-03",
                "status": "hr_replied",
                "appliedJob": None,
            }
        )
    return {
        "generatedAt": NOW_S,
        "totalConversations": len(convs),
        "hrReplied": sum(1 for c in convs if c["status"] == "hr_replied"),
        "awaiting": sum(1 for c in convs if c["status"] == "awaiting"),
        "replyRate": "50%",
        "linkedToApplied": applied_replied + applied_awaiting,
        "linkedReplied": applied_replied,
        "conversations": convs,
    }


def drive(tmp_path, report=..., state=None, fail=None, report_text=None, **kw):
    """驱动 loop：报告/状态写进 tmp 文件（走默认 loader/saver），注入 runner/now_fn。
    report=...（Ellipsis）表示不写报告文件（测缺失场景）；report_text 直接给损坏内容。"""
    runner, calls = make_runner(fail)
    report_path = tmp_path / "followup_report.json"
    if report is not ...:
        report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    elif report_text is not None:
        report_path.write_text(report_text, encoding="utf-8")
    state_path = tmp_path / "loop_state.json"
    if state is not None:
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    code = mod.loop(report_path=report_path, state_path=state_path, runner=runner, now_fn=lambda: NOW, **kw)
    return code, calls, state_path


def read_state(state_path):
    return json.loads(Path(state_path).read_text(encoding="utf-8"))


def script_names(calls):
    return [Path(a[1]).name for a in calls]


# --- 回复率推导与判定 ---


def test_rate_derived_from_applied_linked_only(tmp_path, capsys):
    """回复率只看已投递关联对话（appliedJob 非空），HR 主动开聊的对话不算分母；
    报告自带的 replyRate 是全收件箱口径，不用它。"""
    # 已投递 10 条回复 1 条 = 10%；另有 90 条 HR 陌拜对话，若误用全收件箱口径会高估
    report = make_report(applied_replied=1, applied_awaiting=9, other_convs=90)
    code, calls, _ = drive(tmp_path, report=report, threshold=0.1, min_applied=10)
    assert code == 0, "回复率恰等于阈值（0.1）不触发，严格小于才触发"
    assert calls == [], "达标不 loop"
    out = capsys.readouterr().out
    assert "样本 10" in out and "回复 1" in out, "打印已投递关联口径的样本与回复数"


def test_rate_uses_report_linked_fields_when_present(tmp_path):
    """报告自带 linkedToApplied/linkedReplied（build_report 同口径）时直接采信，
    不再重推导 conversations。"""
    report = make_report(applied_replied=0, applied_awaiting=10)
    report["linkedReplied"] = 5  # 故意与 conversations 不一致：验证字段优先
    code, calls, _ = drive(tmp_path, report=report, threshold=0.1, min_applied=10)
    assert code == 0, "采信报告字段 5/10=50% ≥ 阈值，不 loop"
    assert calls == []


def test_rate_falls_back_when_linked_fields_missing_or_mistyped(tmp_path):
    """报告缺 linked 字段/类型不对时回退从 conversations 重推导，防口径漂移。"""
    report = make_report(applied_replied=1, applied_awaiting=9)
    del report["linkedToApplied"]
    del report["linkedReplied"]
    code, calls, _ = drive(tmp_path, report=report, threshold=0.1, min_applied=10)
    assert code == 0, "字段缺失 → 重推导 1/10=10% 恰达阈值，不 loop"
    assert calls == []
    report = make_report(applied_replied=1, applied_awaiting=9)
    report["linkedToApplied"] = "10"  # 类型不对同样回退
    code, calls, _ = drive(tmp_path, report=report, threshold=0.1, min_applied=10)
    assert code == 0
    assert calls == []


def test_rate_below_threshold_triggers_loop(tmp_path):
    report = make_report(applied_replied=0, applied_awaiting=10)
    code, calls, _ = drive(tmp_path, report=report, threshold=0.1, min_applied=10)
    assert code == 0
    assert calls == [], "草稿生成是 agent 环节，默认动作序列为空（无 generate 子进程）"


def test_insufficient_sample_no_judgment_exit_zero(tmp_path, capsys):
    """样本 9 < min-applied 10：不判定直接退出 0，不跑动作、不写状态。"""
    report = make_report(applied_replied=0, applied_awaiting=9)
    code, calls, state_path = drive(tmp_path, report=report, threshold=0.1, min_applied=10)
    assert code == 0
    assert calls == []
    assert not Path(state_path).exists(), "不判定不应产生状态文件"
    assert "样本 9" in capsys.readouterr().out


def test_zero_sample_no_judgment(tmp_path):
    report = make_report(applied_replied=0, applied_awaiting=0, other_convs=30)
    code, calls, _ = drive(tmp_path, report=report, min_applied=10)
    assert code == 0
    assert calls == []


def test_sample_equal_to_min_applied_is_judged(tmp_path):
    """样本恰为 min-applied 时要判定（>= 而非 >）。"""
    report = make_report(applied_replied=10, applied_awaiting=0)
    code, calls, _ = drive(tmp_path, report=report, threshold=0.1, min_applied=10)
    assert code == 0
    assert calls == [], "10/10 = 100% 达标不 loop"


# --- 次数上限 ---


def test_max_loops_reached_warns_exit_1_no_actions(tmp_path, capsys):
    state = {"loop_count": 2, "last_triggered_at": NOW_S, "history": []}
    report = make_report(applied_replied=0, applied_awaiting=10)
    code, calls, state_path = drive(tmp_path, report=report, state=state, max_loops=2)
    assert code == 1
    assert calls == [], "达上限不再跑任何动作"
    assert read_state(state_path)["loop_count"] == 2, "达上限不改状态"
    out = capsys.readouterr().out
    assert "人工" in out and "--reset" in out, "警示建议人工审查 memory/知识库并提示 --reset"


def test_under_cap_still_loops(tmp_path):
    state = {"loop_count": 1, "last_triggered_at": NOW_S, "history": []}
    report = make_report(applied_replied=0, applied_awaiting=10)
    code, calls, _ = drive(tmp_path, report=report, state=state, max_loops=2)
    assert code == 0
    assert calls == []


# --- loop 动作：build_kb 开关与 subprocess 顺序 ---


def test_with_build_kb_runs_kb_then_agent_hint(tmp_path, capsys):
    report = make_report(applied_replied=0, applied_awaiting=10)
    code, calls, _ = drive(tmp_path, report=report, with_build_kb=True)
    assert code == 0
    assert script_names(calls) == ["build_kb.py"], "唯一子进程是 build_kb，起草是 agent 环节"
    out = capsys.readouterr().out
    assert "agent" in out and "greeting_drafts.json" in out, "触发后打印 agent 起草指引"


def test_build_kb_failure_aborts_loop(tmp_path, capsys):
    report = make_report(applied_replied=0, applied_awaiting=10)
    code, calls, _ = drive(tmp_path, report=report, with_build_kb=True, fail={"build_kb": 5})
    assert code == 5, "动作失败以该退出码退出"
    assert script_names(calls) == ["build_kb.py"]
    assert "agent 环节" not in capsys.readouterr().out, "build_kb 失败中止，不再打印起草指引"


def test_state_counted_even_when_action_fails(tmp_path):
    """状态先落盘再跑动作：即使动作失败也计入次数，防失败重试风暴绕过上限。"""
    report = make_report(applied_replied=0, applied_awaiting=10)
    code, calls, state_path = drive(tmp_path, report=report, with_build_kb=True, fail={"build_kb": 3}, max_loops=2)
    assert code == 3
    assert read_state(state_path)["loop_count"] == 1


# --- state 读写 ---


def test_trigger_writes_state_schema(tmp_path):
    report = make_report(applied_replied=0, applied_awaiting=10)
    code, calls, state_path = drive(tmp_path, report=report, threshold=0.1)
    assert code == 0
    st = read_state(state_path)
    assert st["loop_count"] == 1
    assert st["last_triggered_at"] == NOW_S
    assert st["history"] == [{"date": NOW_S, "reply_rate": 0.0, "threshold": 0.1}]


def test_second_loop_increments_and_appends_history(tmp_path):
    state = {
        "loop_count": 1,
        "last_triggered_at": "2026-09-06 09:00:00",
        "history": [{"date": "2026-09-06 09:00:00", "reply_rate": 0.05, "threshold": 0.1}],
    }
    report = make_report(applied_replied=1, applied_awaiting=19)  # 1/20 = 5% < 10%，触发
    code, calls, state_path = drive(tmp_path, report=report, state=state, threshold=0.1)
    assert code == 0
    st = read_state(state_path)
    assert st["loop_count"] == 2
    assert len(st["history"]) == 2
    assert st["history"][1] == {"date": NOW_S, "reply_rate": 0.05, "threshold": 0.1}


def test_ok_verdict_does_not_touch_state(tmp_path):
    report = make_report(applied_replied=10, applied_awaiting=0)
    code, calls, state_path = drive(tmp_path, report=report)
    assert code == 0
    assert not Path(state_path).exists(), "达标不 loop 也不写状态"


def test_corrupt_state_starts_fresh_with_warning(tmp_path, capsys):
    report = make_report(applied_replied=0, applied_awaiting=10)
    runner, calls = make_runner()
    report_path = tmp_path / "followup_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    state_path = tmp_path / "loop_state.json"
    state_path.write_text("不是 JSON{{{", encoding="utf-8")
    code = mod.loop(report_path=report_path, state_path=state_path, runner=runner, now_fn=lambda: NOW)
    assert code == 0
    assert read_state(state_path)["loop_count"] == 1, "损坏状态按全新状态处理"
    assert "警告" in capsys.readouterr().out


# --- reset ---


def test_reset_clears_count_keeps_history(tmp_path):
    state_path = tmp_path / "loop_state.json"
    state_path.write_text(
        json.dumps(
            {
                "loop_count": 2,
                "last_triggered_at": NOW_S,
                "history": [{"date": NOW_S, "reply_rate": 0.0, "threshold": 0.1}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    code = mod.loop(state_path=state_path, reset=True, runner=lambda argv: pytest.fail("reset 不应跑动作"))
    assert code == 0
    st = read_state(state_path)
    assert st["loop_count"] == 0
    assert st["last_triggered_at"] is None
    assert len(st["history"]) == 1, "历史保留，只清计数"


def test_reset_missing_state_file_no_create(tmp_path, capsys):
    """--reset 遇不存在的状态文件：计数本为 0，仅提示、不创建文件（reset 是清零不是初始化）。"""
    state_path = tmp_path / "loop_state.json"
    code = mod.loop(state_path=state_path, reset=True, runner=lambda argv: pytest.fail("reset 不应跑动作"))
    assert code == 0
    assert not state_path.exists(), "无需重置不应创建状态文件"
    assert "重置" in capsys.readouterr().out


# --- 报告缺失 / 损坏 ---


def test_missing_report_errors_exit_1(tmp_path, capsys):
    code, calls, state_path = drive(tmp_path, report=...)
    assert code == 1
    assert calls == []
    assert "错误" in capsys.readouterr().out


def test_corrupt_report_errors_exit_1(tmp_path):
    code, calls, _ = drive(tmp_path, report_text="{不是 JSON")
    assert code == 1
    assert calls == []


def test_report_without_conversations_errors_exit_1(tmp_path):
    code, calls, _ = drive(tmp_path, report_text='{"foo": 1}')
    assert code == 1
    assert calls == []


# --- 注入点：report_loader / state store 替身（不打真实文件）---


def test_injected_loaders_and_saver(tmp_path):
    sentinel_report = make_report(applied_replied=0, applied_awaiting=10)
    saved = {}

    def fake_loader(path):
        return sentinel_report

    def fake_state_loader(path):
        return {"loop_count": 0, "last_triggered_at": None, "history": []}

    def fake_saver(path, state):
        saved["path"] = Path(path)
        saved["state"] = state

    code = mod.loop(
        report_path=tmp_path / "no.json",
        state_path=tmp_path / "no-state.json",
        runner=make_runner()[0],
        now_fn=lambda: NOW,
        report_loader=fake_loader,
        state_loader=fake_state_loader,
        state_saver=fake_saver,
    )
    assert code == 0
    assert saved["state"]["loop_count"] == 1
    assert not (tmp_path / "no-state.json").exists(), "走注入 saver，不落真实文件"


# --- main() CLI 接线 ---


def test_main_reset_via_cli(tmp_path):
    state_path = tmp_path / "loop_state.json"
    state_path.write_text(json.dumps({"loop_count": 2, "last_triggered_at": NOW_S, "history": []}), encoding="utf-8")
    code = mod.main(["--reset", "--state-file", str(state_path)])
    assert code == 0
    assert read_state(state_path)["loop_count"] == 0


def test_main_rejects_bad_values(tmp_path, capsys):
    with pytest.raises(SystemExit):
        mod.main(["--threshold", "1.5"])
    with pytest.raises(SystemExit):
        mod.main(["--min-applied", "0"])
    with pytest.raises(SystemExit):
        mod.main(["--max-loops", "-1"])


def test_main_loops_with_cli_flags(tmp_path, capsys):
    report_path = tmp_path / "followup_report.json"
    report_path.write_text(
        json.dumps(make_report(applied_replied=0, applied_awaiting=10), ensure_ascii=False), encoding="utf-8"
    )
    state_path = tmp_path / "loop_state.json"
    calls = []
    code = mod.main(
        ["--state-file", str(state_path)],
        report_path=report_path,
        runner=lambda argv: calls.append(argv) or 0,
        now_fn=lambda: NOW,
    )
    assert code == 0
    assert calls == [], "默认动作序列为空，起草由 agent 完成"
    assert "agent" in capsys.readouterr().out


def test_main_with_build_kb_wires_runner(tmp_path):
    """main() 层 --with-build-kb 接线：CLI 传入后 runner 收到 build_kb 的 argv（起草是 agent 环节）。"""
    report_path = tmp_path / "followup_report.json"
    report_path.write_text(
        json.dumps(make_report(applied_replied=0, applied_awaiting=10), ensure_ascii=False), encoding="utf-8"
    )
    state_path = tmp_path / "loop_state.json"
    calls = []
    code = mod.main(
        ["--state-file", str(state_path), "--with-build-kb"],
        report_path=report_path,
        runner=lambda argv: calls.append(argv) or 0,
        now_fn=lambda: NOW,
    )
    assert code == 0
    assert script_names(calls) == ["build_kb.py"]
