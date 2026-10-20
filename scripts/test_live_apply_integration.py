#!/usr/bin/env python3
"""#40 集成/E2E 冒烟（integration 标记）：真实登录态走通 live_apply 到发送前 gate。

**CI 默认跳过**（ci.yml `pytest -m "not integration"`）；本测试另有一层 skip 守卫：
未提供 SMOKE_REPLY_TEXT（合规草稿）或本机未 onboarding 时自动 skip——普通 `pytest`
全仓跑不会误开真实浏览器。人工触发（步骤与留证要求见 docs/agents/integration-smoke.md）：

    SMOKE_REPLY_TEXT="您好，……" python3 -m pytest scripts/test_live_apply_integration.py -m integration -s

流程：run_live(LIVE_DRY_RUN=1) 真开浏览器 → 搜索 → 详情 → JD 入队 request_N.json →
本测试的起草线程按文件队列协议回写 reply_N.json（真实起草是 agent 会话/人工职责，
2026-09-09 架构决策——冒烟不引入本地 LLM，dry-run 也不会发送）→ pre_send_gate →
**dry-run 截停**：不点「立即沟通」、不发送、不写台账，零额度消耗。

机械核验（不靠自评）：run_live 退出值 0（不占名额）+ report.json dry_run=true 且存在
dry_run_ok 条目（链路确实走到了 gate）+ greetings.json 条目数前后不变（台账零写入）。
"""

import importlib.util
import json
import os
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_spec = importlib.util.spec_from_file_location("live_apply", Path(__file__).resolve().parent / "live_apply.py")
assert _spec is not None and _spec.loader is not None, "importlib spec 加载失败（模块文件缺失？）"
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

ROOT = Path(__file__).resolve().parent.parent
WORKER_DEADLINE = 900  # 起草/收尾线程总看门狗（秒）：超时强制 CLOSE 并判失败


def _watch_and_draft(session_base, baseline_dirs, reply, log):
    """后台线程：监视新会话目录——request_N.json 出现即按协议回写 reply_N.json；
    report.json 落盘即写 CLOSE 哨兵结束会话。返回（是否见到 request，是否超时）。"""
    t0 = time.time()
    while time.time() - t0 < WORKER_DEADLINE:
        new_dirs = [d for d in session_base.glob("*") if d.is_dir() and d not in baseline_dirs]
        for sess in sorted(new_dirs):
            for req in sorted(sess.glob("request_*.json")):
                idx = req.stem.split("_")[1]
                reply_path = sess / f"reply_{idx}.json"
                if not reply_path.exists():
                    mod.write_json_atomic(reply_path, reply)
                    log.append(f"drafted reply_{idx}.json")
            if (sess / "report.json").exists():
                (sess / "CLOSE").write_text("", encoding="utf-8")
                log.append("CLOSE sent")
                return True, False
        time.sleep(1)
    # 看门狗超时：强制 CLOSE 让浏览器收尾，测试断言按失败处理
    new_dirs = [d for d in session_base.glob("*") if d.is_dir() and d not in baseline_dirs]
    for sess in new_dirs:
        (sess / "CLOSE").write_text("", encoding="utf-8")
    log.append("watchdog timeout")
    return False, True


def _greetings_count(path):
    try:
        return len(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return 0


def test_live_apply_smoke_dry_run_to_gate():
    """真实登录态全链冒烟：搜索→详情→JD→队列→gate，dry-run 截停零额度消耗。"""
    reply_text = os.environ.get("SMOKE_REPLY_TEXT", "").strip()
    if not (ROOT / "private/profiles.json").exists():
        pytest.skip("本机未 onboarding（private/profiles.json 缺失）——见 docs/agents/onboarding.md")
    if not reply_text:
        pytest.skip("SMOKE_REPLY_TEXT 未设置——冒烟需一份合规草稿，跑法见 docs/agents/integration-smoke.md")

    env = dict(os.environ)
    env["LIVE_DRY_RUN"] = "1"
    env["APPLY_QUIET_HOURS"] = "off"  # 冒烟显式豁免静默时段（文档要求非静默时段跑，双保险）
    env["LIVE_REPLY_TIMEOUT"] = "120"
    name, prefix = mod.resolve_active_profile(env=env)
    session_base = mod.private_path("data/live_session", prefix)
    greetings_path = mod.private_path("data/greetings.json", prefix)
    baseline_dirs = {d for d in session_base.glob("*") if d.is_dir()}
    greetings_before = _greetings_count(greetings_path)

    reply = {
        "send_text": reply_text,
        "level": os.environ.get("SMOKE_REPLY_LEVEL", "中"),
        "notes": os.environ.get("SMOKE_REPLY_NOTES", "来源：冒烟草稿（dry-run 不发送）"),
        "addressee": "hr",
    }
    log: list = []
    worker = threading.Thread(target=_watch_and_draft, args=(session_base, baseline_dirs, reply, log), daemon=True)
    worker.start()

    applied = mod.run_live(env)
    worker.join(timeout=WORKER_DEADLINE + 30)

    sessions = sorted(d for d in session_base.glob("*") if d.is_dir() and d not in baseline_dirs)
    assert sessions, f"冒烟未产生会话目录（worker log: {log}）"
    report = json.loads((sessions[-1] / "report.json").read_text(encoding="utf-8"))
    print(f"\n[smoke] worker: {log}\n[smoke] report: {json.dumps(report, ensure_ascii=False, indent=2)}")

    assert applied == 0, "dry-run 冒烟不得占投递名额（台账/名额零消耗）"
    assert report["dry_run"] is True, "report.json 应带 dry_run=true 供机械核验"
    statuses = [r.get("status") for r in report["results"]]
    assert "dry_run_ok" in statuses, (
        f"没有任何岗位走到发送前 gate（statuses={statuses}）——换一个更有新鲜岗位的搜索词（LIVE_QUERIES）后重试"
    )
    assert _greetings_count(greetings_path) == greetings_before, "greetings.json 台账零写入被违反"
