#!/usr/bin/env python3
"""熔断/停批事件自动建 issue（#51）——独立 reporter 模块，严禁并入 _logging.py。

设计裁决（triage 2026-09-25，正文「修正方案」）：
- **分层解耦**：`_logging.py`/`log_event` 是纯日志基础设施（本地、零业务副作用）；
  `gh` 子进程、issue 渲染、防重台账全在本模块——日志层保持单一职责。
- **远端写是硬门禁动作**（AGENTS.md：remote Issue changes require user authorization）：
  默认 HITL——收尾只打印 💡 提示与确认命令，执行 `python3 scripts/_issue_reporter.py
  --report <report.json>` 即确认动作；`AUTOJOB_AUTO_ISSUE=1` 显式开启才自动建票。
  「投简历」口令不是建票授权（原提议被评审否决，防 Issue Bombing）。
- **防重**：`private/data/[<profile>.]reported_issues.json` 按 (session_id, breaker)
  记账——同会话同熔断器绝不重复开票；文件缺失/坏形态回落空台账（自愈：下次
  mark 原子重写修复）；gh 失败不记账（可重试）。
- **脱敏纪律**：正文只含白名单机械字段（breaker/计数/阈值/stage/detail），公司名/
  岗位名/招呼语等白名单外字段一律不进正文；字段值过 `_logging.scrub_text` 兜底
  凭证类 PII 并截断。detail 仅来自固定文案或服务端状态消息（detect_risk_signal /
  handle_code），不携带用户材料。
- **全降级**：gh 未安装/未登录/超时/非零退出一律 [warn] 输出，绝不阻断投递收尾
  （截图/台账/report.json 落盘优先）。

消费方：`live_apply._write_session_report`（收尾接线，草稿随 report.json 落盘）；
本脚本 CLI 是 HITL 模式的确认入口。
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _atomic_io import write_json_atomic  # noqa: E402
from _logging import scrub_text  # noqa: E402
from _profile_paths import private_path, resolve_active_profile  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPO = ""  # 留空 = 走 resolve_repo()；公开仓默认没有可开票的 tracker
ISSUE_LABEL = "needs-triage"
GH_TIMEOUT = 30  # gh 网络子进程超时（秒）——再慢也不能拖住收尾
AUTO_ISSUE_ENV = "AUTOJOB_AUTO_ISSUE"
REPORTED_REL = "data/reported_issues.json"

# 脱敏白名单：每类事件允许进正文的机械字段（键白名单，值再过 scrub_text + 截断）。
# 未登记的 breaker 走最小集——新增事件类型在此登记后才会带字段上票。
_FIELD_WHITELIST = {
    "consec_no_btn": ("count", "threshold", "stage"),
    "direct_fallback": ("rate", "fails", "attempts", "threshold", "stage"),
    "risk_blocked": ("stage", "detail"),
    "apply_paused": ("code", "detail"),
}
_DETAIL_MAX = 160  # detail 截断上界：服务端消息可能很长，正文只要可定位


def auto_mode(env=None):
    """AUTOJOB_AUTO_ISSUE=1 → 无人值守自动建票；其他任何值（含未设）一律 off。"""
    env = os.environ if env is None else env
    return (env.get(AUTO_ISSUE_ENV) or "").strip() == "1"


def default_reported_path(root=None, env=None):
    """防重台账路径：private/data/[<profile>.]reported_issues.json（#28 扁平命名）。"""
    _name, prefix = resolve_active_profile(env=env, root=root)
    return reported_path_for(prefix, root=root)


def reported_path_for(prefix, root=None):
    """防重台账路径（prefix 已解析形态）：live_apply 与 default_reported_path 共用，
    相对路径知识只落本模块一处（review 采纳项：裸字符串两处写会漂移）。"""
    return private_path(REPORTED_REL, prefix, root=root)


def draft_issue(event, session_id):
    """事件 → 脱敏 issue 草稿。白名单外字段与凭证类 PII 永不进入正文。"""
    breaker = str((event or {}).get("breaker") or "unknown")
    sid = str(session_id)
    rows = [f"- **breaker**: {breaker}", f"- **session**: {sid}"]
    for key in _FIELD_WHITELIST.get(breaker, ()):
        if event.get(key) not in (None, ""):
            rows.append(f"- **{key}**: {scrub_text(str(event[key]))[:_DETAIL_MAX]}")
    title = f"[auto] 投递熔断/停批报告：{breaker}（session {sid}）"
    body = (
        "## 熔断/停批自动报告（#51 `_issue_reporter`）\n\n"
        + "\n".join(rows)
        + f"\n\n**现场材料**：`private/data/live_session/{sid}/report.json`"
        "（results / reason_stats / 截图索引）。\n\n"
        "> 按 #51 脱敏纪律，正文只含机械字段，不含公司/岗位/HR 信息；请结合 report.json 与会话目录截图定位。"
    )
    return {"breaker": breaker, "title": title, "body": body}


def drafts_for(events, session_id):
    """state["breakers"] 事件列表 → 草稿列表（live_apply 收尾接线入口）。"""
    return [draft_issue(e, session_id) for e in (events or [])]


def load_reported(path):
    """读防重台账；缺失 → {}；坏 JSON/非对象 → {} + warn（不代猜不崩溃）。"""
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[warn] 防重台账不可读（{str(e)[:80]}）——本次防重失效，重复开票请人工核对")
        return {}
    if not isinstance(data, dict):
        print("[warn] 防重台账形态异常（非对象）——回落空台账")
        return {}
    return data


def is_reported(reported, session_id, breaker):
    return str(breaker) in (reported.get(str(session_id)) or {})


def mark_reported(path, session_id, breaker, url="", now=None):
    """记账一张已开票（原子写）。读-改-写窗口内并发会话理论上可丢一条记账，
    投递会话按 profile 串行运行，风险可接受。"""
    reported = load_reported(path)
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    reported.setdefault(str(session_id), {})[str(breaker)] = {"url": url or "", "filed_at": stamp}
    write_json_atomic(path, reported)


def resolve_repo(env=None):
    """开票目标仓：`AUTOJOB_ISSUE_REPO` 优先，其次 origin 的 owner/name。

    公开版本仓不开 public issue（见 SECURITY.md），因此解析不到目标时返回空串，
    由调用侧按既有的 [warn] 降级路径处理——绝不静默写到别人的仓上。
    """
    env = os.environ if env is None else env
    explicit = (env.get("AUTOJOB_ISSUE_REPO") or "").strip()
    if explicit:
        return explicit
    proc = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )
    match = re.search(r"github\.com[:/]([^/]+/[^/.]+)", proc.stdout or "")
    return match.group(1) if match else ""


def file_issue(draft, run_fn=None, repo=DEFAULT_REPO, timeout=GH_TIMEOUT):
    """`gh issue create` 子进程；返回 issue URL（gh stdout）。非零退出/超时抛异常，
    由调用方降级——本函数不做吞错，失败信息必须能浮到 [warn]。"""
    run_fn = run_fn or subprocess.run
    repo = repo or resolve_repo()
    if not repo:
        raise RuntimeError("未配置开票目标仓：设 AUTOJOB_ISSUE_REPO=<owner>/<repo> 后重试")
    cmd = [
        "gh",
        "issue",
        "create",
        "--repo",
        repo,
        "--title",
        draft["title"],
        "--body",
        draft["body"],
        "--label",
        ISSUE_LABEL,
    ]
    proc = run_fn(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(ROOT))
    if proc.returncode != 0:
        raise RuntimeError(f"gh 退出码 {proc.returncode}: {(proc.stderr or proc.stdout or '').strip()[:120]}")
    return (proc.stdout or "").strip()


def _file_pending(pending, session_id, path, run_fn=None, now=None):
    """逐张建票 + 记账；单张失败降级 [warn] 且不记账（可重试）。返回失败张数。"""
    failed = 0
    for d in pending:
        try:
            url = file_issue(d, run_fn=run_fn)
            mark_reported(path, session_id, d["breaker"], url, now=now)
            print(f"[开票] {d['breaker']} → {url or '（gh 无输出，请到 GitHub 确认）'}")
        except Exception as e:
            failed += 1
            print(f"[warn] 建票失败（{d['breaker']}），已降级不阻断: {str(e)[:100]}")
    return failed


def filter_pending(drafts, session_id, reported):
    """防重过滤（review 采纳项：wrapup_report 与 main 共用同一形状，防口径漂移）。"""
    return [d for d in (drafts or []) if not is_reported(reported, session_id, d["breaker"])]


def wrapup_report(drafts, session_id, reported_path, report_path=None, env=None, run_fn=None, now=None):
    """收尾编排（#51）：防重过滤 → auto 直接建票 / HITL 打印提示与确认命令。
    返回本次实际待处理草稿。gh 失败全降级 [warn]，绝不抛出——投递收尾优先。"""
    env = os.environ if env is None else env
    pending = filter_pending(drafts, session_id, load_reported(reported_path))
    if not pending:
        return []
    if auto_mode(env):
        _file_pending(pending, session_id, reported_path, run_fn=run_fn, now=now)
    else:
        print(f"💡 [开票提示] 本次会话触发熔断/停批事件 {len(pending)} 起，已准备就绪 Issue 草稿：")
        for d in pending:
            print(f"   - {d['title']}")
        if report_path is not None:
            print(f"   确认后执行: python3 scripts/_issue_reporter.py --report {report_path}")
        print("   （远端建票属外部动作，默认不自动提交；AUTOJOB_AUTO_ISSUE=1 开启无人值守自动建票）")
    return pending


def main(argv=None, run_fn=None, reported_path=None):
    """HITL 确认入口：读 report.json 的 issue_drafts，防重后逐张建票。
    执行本命令即确认动作（远端写授权由操作者按 AGENTS.md 把握）。
    退出码：0=全部成功或无待开票；1=报告不可读或有失败。"""
    parser = argparse.ArgumentParser(description="熔断/停批事件建票（#51）——执行即确认")
    parser.add_argument("--report", required=True, help="live 会话 report.json 路径（含 issue_drafts）")
    args = parser.parse_args(argv)
    report = Path(args.report)
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[error] report 不可读: {str(e)[:120]}")
        return 1
    drafts = data.get("issue_drafts") or []
    if not drafts:
        print("无待开票草稿（issue_drafts 缺失或为空）")
        return 0
    # 生产布局 session_dir = live_session/<ts>，目录名即 session id；防重键按此归一
    session_id = report.parent.name
    path = reported_path or default_reported_path()
    pending = filter_pending(drafts, session_id, load_reported(path))
    if not pending:
        print("无待开票草稿（均已开票）")
        return 0
    failed = _file_pending(pending, session_id, path, run_fn=run_fn)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
