#!/usr/bin/env python3
"""
loop 机制（#14 T7）：followup 回复率低于阈值时，自动 loop 回生成环节（可选先补知识库）。
独立脚本、不改 orchestrate.py——orchestrate 是线性阶段编排（已定稿），loop 是效果反馈后的
条件分支，不是线性阶段；本脚本跑在其之后。

用法：
  python3 scripts/loop.py                              # 判定 + 视结果 loop
  python3 scripts/loop.py --threshold 0.15             # 调触发阈值
  python3 scripts/loop.py --with-build-kb              # loop 时先跑 build_kb 补知识库草稿
  python3 scripts/loop.py --reset                      # 清零 loop 计数（history 保留）
  python3 scripts/loop.py --state-file /tmp/ls.json    # 状态文件另指定

与 orchestrate 的组合用法：
  orchestrate 全链路（或单跑 followup + learn）产出 private/data/followup_report.json
  → python3 scripts/loop.py 判定回复率
  → 触发 loop：打印 agent 起草指引，agent 按 apply-runbook「起草规范」重写
    greeting_drafts.json（status=pending_review；本地 LLM drafter 已退役）
  → 人工审核 private/data/greeting_drafts.json（通过的改 status=approved）
  → python3 scripts/orchestrate.py --stages promote,apply（含两个 HITL 卡点：promote 前
    草稿审核、apply 前投递确认）。

回复率口径（关键，不要猜）：
  followup_report.json（followup_tracker.py → platforms/chat_first.py build_report 产物）自带的
  replyRate 是**全收件箱**口径的百分比字符串（含 HR 主动开聊的对话），不是"已投递条目"
  的回复率。已投递条目口径 build_report 已算成 linkedToApplied/linkedReplied 两个 int 字段
  （数值与从 conversations 按 appliedJob 非空取样本、status=hr_replied 占比重推导恒等）；
  本脚本优先直接用这两个字段，缺失/类型不对时才回退从 conversations 重推导（防报告字段
  缺失/口径漂移时误判）。

loop 动作（触发时按序 subprocess 透传输出，任一步失败立即中止并以该码退出）：
  a. build_kb.py（可选，--with-build-kb 默认关）——产出知识库草稿 private/kb/draft/，
     需人工审核合并到正式 KB 后 agent 起草才会用到，故默认不自动跑；
  b. agent 起草环节（不跑子进程）：打印 AGENT_REDRAFT_HINT，由 agent 按
     apply-runbook「起草规范」重写 greeting_drafts.json；greeting_memory.json 由
     gen_greetings learn 阶段回写，agent 起草时读取以避免重复表达。

次数上限防死循环：状态记 private/data/loop_state.json
  {"loop_count": 已 loop 次数, "last_triggered_at": 最近触发时间,
   "history": [{"date", "reply_rate", "threshold"}, ...]}
  达 --max-loops（默认 2）上限 → 打印警示 exit 1，不再跑动作（建议人工审查
  greeting_memory.json / 知识库，机器反复 loop 收益有限；确认修复后 --reset 清零）。
  状态先落盘再跑动作：即使动作失败也计入次数，防失败重试风暴绕过上限。

退出码：0 = 达标不 loop / 样本不足不判定 / loop 动作全部完成 / 仅 --reset；
  1 = 报告读不了、达次数上限；其他 = 失败动作的退出码。

注入式测试：runner（subprocess 替身）/report_loader/state_loader/state_saver/now_fn
  均可注入（见 test_loop.py），测试不打真网真 LLM、不跑真子进程。
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _atomic_io import write_json_atomic

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
REPORT_PATH = ROOT / "private/data/followup_report.json"
STATE_PATH = ROOT / "private/data/loop_state.json"
DRAFTS_PATH = ROOT / "private/data/greeting_drafts.json"

DEFAULT_THRESHOLD = 0.1
DEFAULT_MIN_APPLIED = 10
DEFAULT_MAX_LOOPS = 2


# --- 输入 ---


def load_report(path):
    """读 followup_report.json。不存在 / JSON 损坏 / 缺 conversations 列表抛 ValueError。"""
    try:
        with open(path, encoding="utf-8") as f:
            report = json.load(f)
    except FileNotFoundError:
        raise ValueError(f"报告不存在: {path}（先跑 followup_tracker.py 生成）")
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"报告读取/解析失败: {path}（{e}）")
    if not isinstance(report, dict) or not isinstance(report.get("conversations"), list):
        raise ValueError(f"报告结构不对（缺 conversations 列表）: {path}（重跑 followup_tracker.py 生成）")
    return report


def load_state(path):
    """读 loop 状态；不存在返回全新状态，损坏/结构不对则告警后按全新状态处理。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return fresh_state()
    except (OSError, json.JSONDecodeError):
        print(f"[警告] 状态文件 {path} 读取/解析失败，按全新状态处理（计数从 0 重新计）")
        return fresh_state()
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("loop_count"), int)
        or not isinstance(data.get("history"), list)
    ):
        print(f"[警告] 状态文件 {path} 结构不对，按全新状态处理（计数从 0 重新计）")
        return fresh_state()
    return data


def fresh_state():
    return {"loop_count": 0, "last_triggered_at": None, "history": []}


def save_state(path, state):
    write_json_atomic(path, state)


# --- 判定 ---


def evaluate_report(report, threshold, min_applied):
    """从报告推导「已投递条目中收到回复的占比」，给出 loop 判定。

    返回 (verdict, info)：
      verdict: "insufficient" 样本不足不判定 / "ok" 达标不 loop / "trigger" 低于阈值要 loop
      info: {"sample": 已投递关联对话数, "replied": 其中 HR 已回复数, "rate": 回复率}

    优先用报告自带 linkedToApplied/linkedReplied（build_report 按同一口径算好，数值与从
    conversations 重推导恒等）；缺失/类型不对时回退重推导——防报告字段缺失或口径漂移
    时误判。
    """
    sample = report.get("linkedToApplied")
    replied = report.get("linkedReplied")
    if not (isinstance(sample, int) and isinstance(replied, int)):
        linked = [c for c in report.get("conversations", []) if c.get("appliedJob")]
        replied = len([c for c in linked if c.get("status") == "hr_replied"])
        sample = len(linked)
    rate = replied / sample if sample else 0.0
    info = {"sample": sample, "replied": replied, "rate": rate}
    if sample < min_applied:
        return "insufficient", info
    if rate < threshold:
        return "trigger", info
    return "ok", info


def loop_actions(with_build_kb):
    """触发 loop 后的动作序列 [(名称, argv)]；build_kb 可选放最前（先补知识库再起草）。
    草稿生成不再有 generate 阶段（本地 LLM drafter 已退役）——由 agent 按 apply-runbook
    「起草规范」重写 greeting_drafts.json（见 AGENT_REDRAFT_HINT，主流程触发时打印）。"""
    actions = []
    if with_build_kb:
        actions.append(("build_kb", [sys.executable, str(SCRIPTS / "build_kb.py")]))
    return actions


AGENT_REDRAFT_HINT = (
    "[agent 环节] 请 agent 按 apply-runbook「起草规范」重写 greeting_drafts.json"
    "（status=pending_review）：读 greeting_memory.json 的品牌回复情况（build_kb 草稿若"
    "已审核合并也一并吸收），调整文案后待人工审核"
)


def default_runner(argv):
    """默认 runner：subprocess.run，stdout/stderr 实时透传（不捕获）。"""
    sys.stdout.flush()  # 先冲掉父进程缓冲，防 CI/管道下父输出与子进程输出乱序
    return subprocess.run(argv).returncode


# --- 主流程 ---


def loop(
    report_path=REPORT_PATH,
    state_path=STATE_PATH,
    threshold=DEFAULT_THRESHOLD,
    min_applied=DEFAULT_MIN_APPLIED,
    max_loops=DEFAULT_MAX_LOOPS,
    with_build_kb=False,
    reset=False,
    runner=None,
    now_fn=None,
    report_loader=None,
    state_loader=None,
    state_saver=None,
):
    """loop 主流程。可注入：report_loader(path)→report、state_loader(path)→state、
    state_saver(path, state)、runner(argv)→退出码、now_fn→datetime。返回退出码（见模块
    docstring「退出码」）。"""
    run_cmd = runner or default_runner
    wall = now_fn or datetime.now
    load_rep = report_loader or load_report
    load_st = state_loader or load_state
    save_st = state_saver or save_state

    if reset:
        if not Path(state_path).exists():
            print(f"[重置] 状态文件不存在（计数本为 0），无需重置、不创建文件: {state_path}")
            return 0
        prev = load_st(state_path)
        state = fresh_state()
        state["history"] = prev.get("history", [])
        save_st(state_path, state)
        print(f"[重置] loop 计数已清零（history 保留 {len(state['history'])} 条）: {state_path}")
        return 0

    try:
        report = load_rep(report_path)
    except ValueError as e:
        print(f"[错误] {e}")
        return 1

    verdict, info = evaluate_report(report, threshold, min_applied)
    rate_s = f"{info['rate'] * 100:.1f}%"
    sample_s = f"已投递关联样本 {info['sample']} 条，回复 {info['replied']} 条，回复率 {rate_s}"
    if verdict == "insufficient":
        print(
            f"[不判定] {sample_s}；样本 < --min-applied {min_applied}，"
            "小样本不判定（避免误触发 loop）。多投递、多跑几次 followup 后再试。"
        )
        return 0
    if verdict == "ok":
        print(f"[达标] {sample_s} ≥ 阈值 {threshold}，无需 loop。")
        return 0
    print(f"[触发] {sample_s} < 阈值 {threshold} → loop 回生成环节")

    state = load_st(state_path)
    if state["loop_count"] >= max_loops:
        print(f"[达上限] loop 次数已达上限 {state['loop_count']}/{max_loops}，不再自动 loop。")
        print("建议人工审查 private/data/greeting_memory.json 与知识库（机器反复 loop 收益有限），")
        print("确认问题修复后 python3 scripts/loop.py --reset 清零计数再跑。")
        return 1

    stamp = wall().strftime("%Y-%m-%d %H:%M:%S")
    state["loop_count"] += 1
    state["last_triggered_at"] = stamp
    state.setdefault("history", []).append({"date": stamp, "reply_rate": info["rate"], "threshold": threshold})
    save_st(state_path, state)
    print(f"[loop] 第 {state['loop_count']}/{max_loops} 次（次数上限防死循环），状态已记 {state_path}")

    for name, argv in loop_actions(with_build_kb):
        print(f"\n[运行] {name}: {' '.join(argv)}")
        code = run_cmd(argv)
        if code != 0:
            print(
                f"\n[loop 中止] {name} 失败（退出码 {code}）；"
                f"本次 loop 已计入次数（{state['loop_count']}/{max_loops}），"
                "重试请 --reset 后再跑"
            )
            return code

    print(f"\n{AGENT_REDRAFT_HINT}")
    print("\n[loop 完成] 草稿重写后的人工路径：")
    if with_build_kb:
        print(
            "  0. build_kb 草稿在 private/kb/draft/，需人工审核合并到正式 KB"
            "（private/kb/repos_detail.md、private/profile_background.md）后 agent 起草才会用到"
        )
    print(f"  1. 审核 {DRAFTS_PATH}，通过的草稿 status 改为 approved")
    print(
        "  2. python3 scripts/orchestrate.py --stages promote,followup（投递由 agent 会话跑 live_apply），卡点照常触发"
    )
    return 0


def main(argv=None, **injections):
    """CLI 入口。injections（report_path/runner/now_fn 等）仅供测试透传给 loop()。"""
    from _profile_paths import private_path as _pp, profile_line as _pl, resolve_active_profile as _rap

    _name, _prefix = _rap()
    print(_pl(_name, _prefix))
    globals()["REPORT_PATH"] = _pp("data/followup_report.json", _prefix)
    globals()["STATE_PATH"] = _pp("data/loop_state.json", _prefix)
    globals()["DRAFTS_PATH"] = _pp("data/greeting_drafts.json", _prefix)
    parser = argparse.ArgumentParser(
        description="followup 回复率低于阈值时 loop 回生成环节（#14 T7；独立于 orchestrate.py）"
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help="回复率触发阈值（0-1，默认 0.1）；回复率严格小于该值才触发",
    )
    parser.add_argument(
        "--min-applied",
        type=int,
        default=DEFAULT_MIN_APPLIED,
        help="判定所需最小已投递关联样本数（默认 10）；不足则不判定，退出 0",
    )
    parser.add_argument(
        "--max-loops",
        type=int,
        default=DEFAULT_MAX_LOOPS,
        help="loop 次数上限（防死循环，默认 2）；达上限退出 1，--reset 清零",
    )
    parser.add_argument(
        "--with-build-kb",
        action="store_true",
        help="loop 时先跑 build_kb.py 补知识库草稿（默认关：草稿需人工合并后才生效，自动跑完也不会被 generate 用到）",
    )
    parser.add_argument(
        "--reset", action="store_true", help="清零 loop 计数（history 保留）后退出；状态文件不存在则仅提示，不创建文件"
    )
    parser.add_argument("--state-file", default=str(STATE_PATH), help=f"loop 状态文件路径（默认 {STATE_PATH}）")
    args = parser.parse_args(argv)
    if not 0 <= args.threshold <= 1:
        parser.error("--threshold 应在 0-1 之间")
    if args.min_applied < 1:
        parser.error("--min-applied 应 ≥ 1")
    if args.max_loops < 1:
        parser.error("--max-loops 应 ≥ 1")
    injections.setdefault("state_path", Path(args.state_file))
    return loop(
        threshold=args.threshold,
        min_applied=args.min_applied,
        max_loops=args.max_loops,
        with_build_kb=args.with_build_kb,
        reset=args.reset,
        **injections,
    )


if __name__ == "__main__":
    sys.exit(main())
