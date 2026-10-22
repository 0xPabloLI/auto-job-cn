#!/usr/bin/env python3
"""live_apply 的行为测试：文件队列协议、动作截图、卡片导航、CLOSE 哨兵、查询解析、
发送前机械 gate、节奏吞吐模拟、关键词轮导航稳定性（#35）。"""

import importlib.util
import itertools
import json
import os
import random
import re
import subprocess
import sys
import time
import zlib
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest


_spec = importlib.util.spec_from_file_location("live_apply", Path(__file__).resolve().parent / "live_apply.py")
assert _spec is not None and _spec.loader is not None, "importlib spec 加载失败（模块文件缺失？）"
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

_sg_spec = importlib.util.spec_from_file_location("_selfguard", Path(__file__).resolve().parent / "_selfguard.py")
assert _sg_spec is not None and _sg_spec.loader is not None, "importlib spec 加载失败（模块文件缺失？）"
sg = importlib.util.module_from_spec(_sg_spec)
_sg_spec.loader.exec_module(sg)

# #57 第二轮：心跳/取栈取证段拆到 `_forensics.py`，模块级单例（`_HEARTBEAT_DIR` / `_HEARTBEAT_JOB`
# / `_WEDGE_FH`）与「被函数体读、被用例 monkeypatch 的数值」（`WEDGE_STACK_SECONDS`）都归它所有，
# 用例的 setattr 目标必须跟着打在它上面。
# **这里刻意不照 `_sg_spec` / `_load_limits()` 的 spec-from-file 形态加载**：那样会得到**第二个
# 模块对象**，而 `live_apply._touch_heartbeat` 那批别名函数读的是 `sys.modules["_forensics"]`
# 里的那一份——patch 第二个对象同样是静默 no-op（隔离失效却照常绿，正是本票要防的形态）。
# `mod` 已经 exec 过，scripts/ 已挂上 sys.path，故直接 import 拿到同一个对象，并当场自证。
import _forensics as fx  # noqa: E402

# #62：聊天页 URL 锚的**真值来源**是平台层的 `CHAT_URL`（platforms/chat_first.py:29），不在 live_apply
# 里另立一份常量——用例直接把两边对起来钉（`CHAT_URL_MARK in chat_mod.CHAT_URL`），
# 生产端改了 URL 而锚没跟上就当场红，而不是等真机跑一场才发现守卫恒不命中。
from platforms import chat_first as chat_mod  # noqa: E402

# #75 条目 B：收尾采集这条路径也要落水位线两键——用例按真实写入口造「上次基准」，不手搓形态。
import _inbound_watermark as wm  # noqa: E402

# 最直接的自证：别名函数的 `__globals__` 必须就是 `fx` 的命名空间——patch 打在 `fx` 上，写者才读得到。
# 将来若有人改用 spec-from-file 另加载一份 `_forensics`，这里当场红，而不是留一堆静默 no-op 的隔离。
assert mod._touch_heartbeat.__globals__ is fx.__dict__, "取证模块必须是同一个对象"


# --- 时钟钉（全模块 autouse）---


@pytest.fixture(autouse=True)
def _pin_working_hour(monkeypatch):
    """把「当前小时」钉成 10（工作时段）。`_pre_job_stop` 与 `_init_live_session` 的静默时段判定
    读的是真实时钟（默认 0-7 点停批），不钉的话每天 0-7 点之间会有 6 条补投/停批类用例集体红
    （2026-09-29 00:14 实测：`test_retry_pending_jobs_*` ×4 + `test_retry_session_jobs_*` ×2，
    断言拿到的是「[停] 进入静默时段 0-7 点」而不是被测行为）。正解是把时钟依赖消掉，
    不是把它记进「已知环境性失败」清单——否则夜里跑的每一次门禁都在自欺。
    真正要静默态的用例各自 monkeypatch `is_quiet_hour`（`test_pre_job_stop_on_quiet_hour_notifies`
    等），与本夹具正交、不受影响。"""
    monkeypatch.setattr(mod, "current_hour", lambda: 10)


# --- 查询解析 ---


def test_parse_queries_env_comma_split():
    qs = mod.parse_queries({"LIVE_QUERIES": " Solidity , 区块链 ,,"})
    assert qs == ["Solidity", "区块链"]


def _write_profile(profiles_dir, name, data):
    profiles_dir.mkdir(parents=True, exist_ok=True)
    (profiles_dir / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")


def test_parse_queries_profile_queries_and_weights(tmp_path):
    """#29 P0：搜索词与权重全部来自 profile JSON（代码内 DEFAULT_QUERIES/QUERY_WEIGHTS
    硬编码已删，grep 零命中是验收标准 4）。"""
    _write_profile(
        tmp_path, "ai", {"industry": "ai", "queries": ["AI工程", "LLM", "Web3"], "query_weights": {"Web3": 2}}
    )
    qs = mod.parse_queries({}, profile="ai", profiles_dir=tmp_path)
    assert qs[:3] == ["AI工程", "LLM", "Web3"]
    assert qs.count("Web3") == 2, "profile 的 query_weights 高权加轮"
    assert qs.count("LLM") == 1


def test_parse_queries_query_weights_optional(tmp_path):
    _write_profile(tmp_path, "ai", {"queries": ["大模型", " RAG "]})
    assert mod.parse_queries({}, profile="ai", profiles_dir=tmp_path) == ["大模型", "RAG"], (
        "缺 query_weights 视为无加权；词原样去空白"
    )


def test_parse_queries_missing_queries_fail_loud(tmp_path):
    """#29 P0：profile 缺有效 queries 且未设 env → 修复指引 + 非零退出，不静默回落。"""
    _write_profile(tmp_path, "noq", {"industry": "x"})
    with pytest.raises(SystemExit) as ei:
        mod.parse_queries({}, profile="noq", profiles_dir=tmp_path)
    assert "queries" in str(ei.value) and "noq" in str(ei.value), "指引含字段名与 profile 名"


def test_parse_queries_profile_file_missing_fail_loud(tmp_path):
    with pytest.raises(SystemExit):
        mod.parse_queries({}, profile="不存在", profiles_dir=tmp_path)


def test_parse_queries_invalid_queries_fail_loud(tmp_path):
    _write_profile(tmp_path, "bad1", {"queries": []})
    with pytest.raises(SystemExit):
        mod.parse_queries({}, profile="bad1", profiles_dir=tmp_path)
    _write_profile(tmp_path, "bad2", {"queries": ["ok", 3]})
    with pytest.raises(SystemExit):  # 非字符串元素也算非法
        mod.parse_queries({}, profile="bad2", profiles_dir=tmp_path)


def test_parse_queries_env_beats_missing_profile(tmp_path):
    assert mod.parse_queries({"LIVE_QUERIES": "Go"}, profile="x", profiles_dir=tmp_path) == ["Go"]


def test_parse_queries_env_beats_profile(tmp_path):
    _write_profile(tmp_path, "ai", {"industry": "ai", "queries": ["AI工程"]})
    qs = mod.parse_queries({"LIVE_QUERIES": "Go"}, profile="ai", profiles_dir=tmp_path)
    assert qs == ["Go"], "env 显式设置最优先，按原样不带权重"


# --- 文件队列协议（agent 起草点） ---


def test_request_greeting_writes_job_context(tmp_path):
    job = {
        "jobName": "智能合约工程师",
        "brandName": "某Web3公司",
        "salaryDesc": "30-50K",
        "encryptJobId": "abc",
        "bossName": "汀先生",
        "bossTitle": "CTO",
    }
    path = mod.request_greeting(tmp_path, 3, job, "JD 全文：负责合约开发")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert path.name == "request_3.json"
    assert data["jobName"] == "智能合约工程师" and data["brandName"] == "某Web3公司"
    assert data["salaryDesc"] == "30-50K" and data["jdText"].startswith("JD 全文")
    assert data["bossName"] == "汀先生"


def test_wait_reply_picks_up_reply(tmp_path):
    reply = tmp_path / "reply_0.json"
    sleeps = []

    def agent_writes_later(_):
        sleeps.append(1)
        if len(sleeps) == 2:  # 第 2 次轮询前 agent 写入
            reply.write_text(json.dumps({"send_text": "您好，7 年 Solidity"}, ensure_ascii=False), encoding="utf-8")

    text = mod.wait_reply(tmp_path, 0, timeout=60, poll=1, sleep_fn=agent_writes_later, now_fn=lambda: 0)
    assert text == "您好，7 年 Solidity"


def test_wait_reply_timeout_returns_none(tmp_path):
    clock = {"t": 0}
    text = mod.wait_reply(
        tmp_path,
        0,
        timeout=10,
        poll=3,
        sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s),
        now_fn=lambda: clock["t"],
    )
    assert text is None, "超时未收到 agent 回复应返回 None（岗位记 agent_timeout）"


def test_wait_reply_ignores_corrupted_file_until_valid(tmp_path):
    reply = tmp_path / "reply_1.json"
    reply.write_text("{写一半", encoding="utf-8")  # agent 写一半被读到
    sleeps = []

    def fix_later(_):
        sleeps.append(1)
        if len(sleeps) == 2:
            reply.write_text(json.dumps({"send_text": "您好"}, ensure_ascii=False), encoding="utf-8")

    text = mod.wait_reply(tmp_path, 1, timeout=60, poll=1, sleep_fn=fix_later, now_fn=lambda: 0)
    assert text == "您好", "损坏的 reply 文件继续轮询，不立刻判超时"


def test_wait_reply_blank_text_not_accepted(tmp_path):
    reply = tmp_path / "reply_2.json"
    reply.write_text(json.dumps({"send_text": "  "}, ensure_ascii=False), encoding="utf-8")
    clock = {"t": 0}
    text = mod.wait_reply(
        tmp_path,
        2,
        timeout=5,
        poll=1,
        sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s),
        now_fn=lambda: clock["t"],
    )
    assert text is None, "空白 send_text 视为未回复"


# --- #55 部分一：宽限复查——超时后不立即翻篇，迟到草稿在窗口内送达即消费 ---


def test_wait_reply_grace_rescues_late_reply(tmp_path, capsys):
    """验收 1：reply 在超时后 ~30s（宽限窗口内）出现 → 被消费，岗位正常处理（返回 text）。"""
    reply = tmp_path / "reply_0.json"
    clock = {"t": 0}
    calls = {"n": 0}

    def late_agent(s):
        calls["n"] += 1
        clock["t"] += s
        if calls["n"] == 6:  # 主超时耗尽后（第 5 次起进宽限轮询）、宽限期内送达
            reply.write_text(json.dumps({"send_text": "您好，7 年 Solidity"}, ensure_ascii=False), encoding="utf-8")

    text = mod.wait_reply(
        tmp_path, 0, timeout=10, poll=3, sleep_fn=late_agent, now_fn=lambda: clock["t"], grace=60, grace_poll=5
    )
    assert text == "您好，7 年 Solidity", "宽限窗口内送达的迟到草稿被正常消费"
    assert "救回" in capsys.readouterr().out


def test_wait_reply_grace_exhausted_marks_voided(tmp_path, capsys):
    """宽限窗口过仍无回复 → 返回 None；日志明示「已作废」——迟到草稿不静默丢。"""
    clock = {"t": 0}
    text = mod.wait_reply(
        tmp_path,
        0,
        timeout=10,
        poll=3,
        sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s),
        now_fn=lambda: clock["t"],
        grace=15,
        grace_poll=5,
    )
    assert text is None
    out = capsys.readouterr().out
    assert "已作废" in out and "宽限" in out


def test_wait_reply_grace_zero_keeps_legacy_semantics(tmp_path):
    """grace=0 → 不进入宽限轮询（旧超时语义），守护「正常路径零变化」。"""
    clock = {"t": 0}
    sleeps = []

    def count(s):
        sleeps.append(s)
        clock["t"] += s

    text = mod.wait_reply(tmp_path, 0, timeout=9, poll=3, sleep_fn=count, now_fn=lambda: clock["t"], grace=0)
    assert text is None
    assert all(s == 3 for s in sleeps), "grace=0 时只有主轮询的 poll 间隔"


def test_wait_reply_skip_flag(tmp_path):
    reply = tmp_path / "reply_3.json"
    reply.write_text(json.dumps({"skip": True}), encoding="utf-8")
    assert mod.wait_reply(tmp_path, 3, timeout=10, poll=1, sleep_fn=lambda s: None, now_fn=lambda: 0) == mod.SKIP, (
        "agent 判定不匹配（skip:true）立即返回 SKIP，不等超时"
    )


# --- 动作截图 ---


class _ShotPage:
    def __init__(self, raises=False):
        self.calls = []
        self._raises = raises

    def screenshot(self, **kw):
        self.calls.append(kw)
        if self._raises:
            raise RuntimeError("screenshot timeout")


def test_shot_saves_timestamped_png(tmp_path):
    page = _ShotPage()
    mod.shot(page, tmp_path, "scroll")
    assert len(page.calls) == 1
    kw = page.calls[0]
    assert kw["path"].startswith(str(tmp_path)) and kw["path"].endswith(".png")
    assert "scroll" in kw["path"]


def test_shot_swallows_errors(tmp_path):
    page = _ShotPage(raises=True)
    mod.shot(page, tmp_path, "scroll")  # 不抛出，截图失败不影响投递


# --- 列表卡片导航（替代直跳 URL） ---


class _NavPage:
    def __init__(self, card=None):
        self._card = card
        self.gotos = []
        self.clicked = []

    def query_selector(self, sel):
        return self._card

    def goto(self, url, **kw):
        self.gotos.append(url)


class _Card:
    def click(self, **kw):
        pass


def test_enter_detail_clicks_card_without_goto(monkeypatch):
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    page = _NavPage(card=_Card())
    mode = mod.enter_detail(page, "abc123", lambda url, **kw: page.goto(url, **kw))
    assert mode == "card"
    assert page.gotos == [], "从列表点卡片进详情，不再直跳 URL"


def test_enter_detail_falls_back_to_direct_goto(monkeypatch):
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    page = _NavPage(card=None)
    mode = mod.enter_detail(page, "abc123", lambda url, **kw: page.goto(url, **kw))
    assert mode == "direct_no_card", "#50：卡片缺失回退直跳返回 direct_no_card"
    assert len(page.gotos) == 1 and "abc123" in page.gotos[0], "卡片缺失回退直跳"


def test_enter_detail_falls_back_on_click_failure(monkeypatch):
    """#50：卡片存在但点击失败或抛异常时，回退直跳且返回 direct_click_failed。"""
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(mod, "click_with_retry", lambda *a, **k: False)
    page = _NavPage(card=_Card())
    mode = mod.enter_detail(page, "abc123", lambda url, **kw: page.goto(url, **kw))
    assert mode == "direct_click_failed"
    assert len(page.gotos) == 1 and "abc123" in page.gotos[0], "点击失败回退直跳"

    # 抛异常同样返回 direct_click_failed
    page2 = _NavPage(card=_Card())

    def _boom(*a, **k):
        raise RuntimeError("click timeout")

    monkeypatch.setattr(mod, "click_with_retry", _boom)
    mode2 = mod.enter_detail(page2, "abc123", lambda url, **kw: page2.goto(url, **kw))
    assert mode2 == "direct_click_failed"
    assert len(page2.gotos) == 1


# --- CLOSE 哨兵（结束不关窗） ---


def test_wait_close_sentinel_exits_on_file(tmp_path):
    sentinel = tmp_path / "CLOSE"
    polls = []

    def user_creates_later(_):
        polls.append(1)
        if len(polls) == 3:
            sentinel.write_text("", encoding="utf-8")

    mod.wait_close_sentinel(sentinel, poll=1, sleep_fn=user_creates_later)  # 正常返回


# --- 已投递历史加载 ---


def test_load_applied_ids(tmp_path):
    hist = tmp_path / "applied_history.json"
    hist.write_text(json.dumps({"applied": [{"encryptJobId": "a"}, {"encryptJobId": "b"}]}), encoding="utf-8")
    assert mod.load_applied_ids(hist) == {"a", "b"}


def test_load_applied_ids_missing_file(tmp_path):
    assert mod.load_applied_ids(tmp_path / "none.json") == set()


# --- 招呼语台账回写（#23：live_apply 与投递动作 1:1，#19 eval 闭环前置） ---

JOB = {
    "encryptJobId": "j1",
    "jobName": "Solidity 工程师",
    "brandName": "某基金",
    "bossName": "宓乐言",
    "bossTitle": "招聘者",
}


def test_record_greeting_appends_entry_with_job_id(tmp_path):
    p = tmp_path / "greetings.json"
    ok = mod.record_greeting(JOB, "我有7年Solidity经验", "2026-09-10T12:00:00", greetings_path=p)
    assert ok == "added"
    entries = json.loads(p.read_text(encoding="utf-8"))
    assert len(entries) == 1
    e = entries[0]
    assert e["encryptJobId"] == "j1" and e["brandName"] == "某基金"
    assert e["greeting"] == "我有7年Solidity经验"
    assert e["bossName"] == "宓乐言" and e["bossTitle"] == "招聘者"
    assert e["source"] == "live_apply" and e["date"] == "2026-09-10T12:00:00"


def test_record_greeting_overwrites_same_job_id(tmp_path, capsys):
    """#64 验收 1：同 eid 二次真投出 ⇒ 条目字段级更新为第二稿（库里旧条可能只是从未投出的
    草稿，「写过草稿」不得吞掉本次文案），且覆盖要可见——台账日志出现「覆盖」字样。"""
    p = tmp_path / "greetings.json"
    assert mod.record_greeting(JOB, "第一稿", "t1", greetings_path=p) == "added"
    assert mod.record_greeting(JOB, "第二稿", "t2", greetings_path=p) == "overwritten"
    entries = json.loads(p.read_text(encoding="utf-8"))
    assert len(entries) == 1 and entries[0]["greeting"] == "第二稿"
    out = capsys.readouterr().out
    assert "覆盖" in out and "旧 date=t1" in out and "旧 source=live_apply" in out


def test_record_greeting_overwrites_legacy_draft_entry(tmp_path):
    """#64 验收 2：旧链路遗留的 date=None / source=None 起草条目不阻断本次真投出——
    整条以本次值为准（date/source 被填上），不许静默 return。"""
    p = tmp_path / "greetings.json"
    legacy = [{"encryptJobId": "j1", "greeting": "旧稿", "date": None, "source": None}]
    p.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    assert mod.record_greeting(JOB, "真投出的稿", "2026-10-10T09:00:00", greetings_path=p) == "overwritten"
    entries = json.loads(p.read_text(encoding="utf-8"))
    assert len(entries) == 1
    e = entries[0]
    assert e["greeting"] == "真投出的稿"
    assert e["date"] == "2026-10-10T09:00:00" and e["source"] == "live_apply"


def test_record_greeting_overwrite_preserves_fields_absent_this_time(tmp_path):
    """#64 P5：覆盖是字段级更新，不是整条替换——本次没带值（None）的字段
    （bossActiveDesc/bossOnline/level）留在原条目上，别静默丢掉 #32/#27 的活跃度分组数据。"""
    p = tmp_path / "greetings.json"
    old = [
        {
            "encryptJobId": "j1",
            "greeting": "旧稿",
            "level": "高",
            "bossActiveDesc": "刚刚活跃",
            "bossOnline": True,
            "date": "t0",
            "source": "live_apply",
        }
    ]
    p.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    assert mod.record_greeting(JOB, "实发稿", "t1", greetings_path=p) == "overwritten"
    e = json.loads(p.read_text(encoding="utf-8"))[0]
    assert e["bossActiveDesc"] == "刚刚活跃" and e["bossOnline"] is True
    assert e["level"] == "高", "本次无 reply meta（level=None）不盖旧值"
    assert e["greeting"] == "实发稿" and e["date"] == "t1" and e["source"] == "live_apply"


def test_record_greeting_overwrite_lifts_legacy_source(tmp_path):
    """#64 P5：legacy_draft_only 旧条被真投出覆盖后脱离历史遗留族——date/source 恒非 None，
    必被本次真实值刷掉，EDD 按文案分组重新收录它。"""
    p = tmp_path / "greetings.json"
    old = [{"encryptJobId": "j1", "greeting": "从未发出的旧稿", "date": None, "source": "legacy_draft_only"}]
    p.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    assert mod.record_greeting(JOB, "实发稿", "2026-10-10T09:00:00", greetings_path=p) == "overwritten"
    e = json.loads(p.read_text(encoding="utf-8"))[0]
    assert e["source"] == "live_apply" and e["date"] == "2026-10-10T09:00:00"
    assert mod.g.is_legacy_greeting(e) is False


def test_record_greeting_overwrite_log_carries_old_draft_prefix(tmp_path, capsys):
    """#64 S3：覆盖是整条数据变更里唯一不可逆的一处——日志带旧稿前 40 字留痕（截断钉住）。"""
    p = tmp_path / "greetings.json"
    old_draft = "旧" * 90
    old = [{"encryptJobId": "j1", "greeting": old_draft, "date": "t0", "source": "live_apply"}]
    p.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    assert mod.record_greeting(JOB, "新稿", "t1", greetings_path=p) == "overwritten"
    out = capsys.readouterr().out
    assert f"旧稿「{old_draft[:40]}」" in out
    assert old_draft not in out, "只留前 40 字，不整篇刷屏"


def test_record_greeting_tolerates_non_dict_entries(tmp_path):
    """#64「台账层绝不反噬投递」的另一种坏法：库里混进非 dict 条目（手工编辑留下的 null）——
    扫描与合并都只认 dict，坏条目原位留着不丢，本次文案照常落库，异常绝不冒到发送链路。"""
    p = tmp_path / "greetings.json"
    p.write_text(
        json.dumps([None, {"encryptJobId": "j1", "greeting": "旧稿", "source": "live_apply"}], ensure_ascii=False),
        encoding="utf-8",
    )
    assert mod.record_greeting(JOB, "实发稿", "t1", greetings_path=p) == "overwritten"
    entries = json.loads(p.read_text(encoding="utf-8"))
    assert entries[0] is None, "坏条目原位保留，不静默删除"
    assert entries[1]["greeting"] == "实发稿" and entries[1]["source"] == "live_apply"


def test_record_greeting_write_failure_returns_skipped(tmp_path, monkeypatch, capsys):
    """台账层绝不反噬投递：写盘失败不冒泡，warn 一行后返回 "skipped"（#64）。"""

    def boom(path, data):
        raise OSError("disk on fire")

    monkeypatch.setattr(mod, "write_json_atomic", boom)
    assert mod.record_greeting(JOB, "稿", "t1", greetings_path=tmp_path / "greetings.json") == "skipped"
    out = capsys.readouterr().out
    assert "warn" in out and "disk on fire" in out


def test_record_greeting_reads_level_from_reply_meta(tmp_path):
    """level/addressee 从 agent 的 reply_N.json 附带读取（eval 按 level 分组消费）。"""
    session = tmp_path / "sess"
    session.mkdir()
    (session / "reply_3.json").write_text(
        json.dumps({"send_text": "稿", "level": "高", "addressee": "leader"}, ensure_ascii=False), encoding="utf-8"
    )
    p = tmp_path / "greetings.json"
    mod.record_greeting(JOB, "稿", "t1", session_dir=session, idx=3, greetings_path=p)
    e = json.loads(p.read_text(encoding="utf-8"))[0]
    assert e["level"] == "高" and e["addressee"] == "leader"


def test_record_greeting_missing_reply_meta_defaults(tmp_path):
    """reply_N.json 缺失/损坏（如 friend/add 未捕获路径）不阻塞回写，level 留空。"""
    p = tmp_path / "greetings.json"
    mod.record_greeting(JOB, "稿", "t1", session_dir=tmp_path, idx=9, greetings_path=p)
    e = json.loads(p.read_text(encoding="utf-8"))[0]
    assert e["level"] is None and e["addressee"] == "hr"


def test_record_greeting_corrupt_file_starts_fresh(tmp_path):
    p = tmp_path / "greetings.json"
    p.write_text("not-json{", encoding="utf-8")
    assert mod.record_greeting(JOB, "稿", "t1", greetings_path=p) == "added"
    entries = json.loads(p.read_text(encoding="utf-8"))
    assert len(entries) == 1, "损坏文件告警后从 [] 起步，不丢本条台账"


def test_greeting_ledger_stats_keys_always_present():
    """#64 验收 3：report 读数三键恒在——空 state / 缺字典给零形态，有数照抄（口径同
    `_no_send_reason_stats` / `no_jd_dom_shapes` 的「新键恒在」）。"""
    zero = {"added": 0, "overwritten": 0, "skipped": 0}
    assert mod._greeting_ledger_stats({}) == zero
    assert mod._greeting_ledger_stats({"applied": 1, "direct_fallback": 0}) == zero
    assert mod._greeting_ledger_stats({"greeting_ledger": {"added": 2, "overwritten": 1, "skipped": 3}}) == {
        "added": 2,
        "overwritten": 1,
        "skipped": 3,
    }


def test_record_applied_accumulates_greeting_ledger_outcomes(tmp_path, monkeypatch):
    """#64 验收 3 接线：三条分支的结局字符串各自把 state["greeting_ledger"] 对应计数 +1；
    写盘失败的岗位照常走完 _record_applied（台账层不反噬投递）。"""
    monkeypatch.setattr(mod._logging, "log_event", lambda event, **fields: None)
    session, _ = _fake_session(
        tmp_path,
        cfg={
            "apply_log_path": str(tmp_path / "apply_log.jsonl"),
            "history_path": str(tmp_path / "history.json"),
            "greetings_path": str(tmp_path / "greetings.json"),
        },
        state={
            "applied": 0,
            "direct_fallback": 0,
            "keyword_skips": [],
            "greeting_ledger": {"added": 0, "overwritten": 0, "skipped": 0},
        },
    )
    real_write = mod.write_json_atomic

    def write_flaky(path, data):
        # 只让 greetings 台账写失败，另两件台账照常落盘，否则走不到 record_greeting
        if str(path).endswith("greetings.json"):
            raise OSError("disk on fire")
        return real_write(path, data)

    for i, text in enumerate(("第一稿", "第二稿")):
        mod._record_applied(mod._JobFlow(page="P", job=JOB, idx=i, ctx=session, greeting=text), code=0)
    assert session.state["greeting_ledger"] == {"added": 1, "overwritten": 1, "skipped": 0}
    assert sum(session.state["greeting_ledger"].values()) == session.state["applied"], "每个投出的岗恰好落一个桶"
    monkeypatch.setattr(mod, "write_json_atomic", write_flaky)
    mod._record_applied(mod._JobFlow(page="P", job=JOB, idx=2, ctx=session, greeting="第三稿"), code=0)
    assert session.state["greeting_ledger"] == {"added": 1, "overwritten": 1, "skipped": 1}
    assert sum(session.state["greeting_ledger"].values()) == session.state["applied"], "写盘失败也占一个桶（skipped）"


# --- #64 追加验收 5/6：招呼语台账历史条目迁移打标（由代码做，不手改数据文件） ---

_MIGRATION_ZERO = {"legacy_undated": 0, "legacy_draft_only": 0, "applied_without_greeting": 0, "tagged": 0}


def _migration_fixture(tmp_path, greetings, applied_eids=()):
    gp, hp = tmp_path / "greetings.json", tmp_path / "applied_history.json"
    gp.write_text(json.dumps(greetings, ensure_ascii=False), encoding="utf-8")
    hp.write_text(json.dumps({"applied": [{"encryptJobId": e} for e in applied_eids]}), encoding="utf-8")
    return gp, hp


def test_migrate_greeting_ledger_tags_by_date_and_history(tmp_path, capsys):
    """验收 5 判据：date 空 + eid 在投递台账 ⇒ legacy_undated；date 空 + 不在 ⇒
    legacy_draft_only；已有真实 date 的条目一律不动（补日期＝伪造 EDD 输入，票面理由 1）。"""
    gp, hp = _migration_fixture(
        tmp_path,
        [
            {"encryptJobId": "a", "greeting": "稿a", "date": None, "source": None},
            {"encryptJobId": "b", "greeting": "稿b"},  # date 键整个缺失也算空
            {"encryptJobId": "c", "greeting": "实发稿c", "date": "2026-09-15T10:00:00", "source": None},
        ],
        applied_eids=["a", "c"],
    )
    stats = mod.migrate_greeting_ledger(gp, hp)
    assert stats == {"legacy_undated": 1, "legacy_draft_only": 1, "applied_without_greeting": 0, "tagged": 2}
    entries = {e["encryptJobId"]: e for e in json.loads(gp.read_text(encoding="utf-8"))}
    assert entries["a"]["source"] == "legacy_undated"
    assert entries["b"]["source"] == "legacy_draft_only"
    assert entries["c"]["source"] is None and entries["c"]["date"] == "2026-09-15T10:00:00"
    assert "打标 legacy_undated 1 条 / legacy_draft_only 1 条" in capsys.readouterr().out


def test_migrate_greeting_ledger_idempotent(tmp_path, capsys):
    """连跑两次：第二次不再打标（legacy 标本身就是「已处置」记号），文件字节不再变化。"""
    gp, hp = _migration_fixture(tmp_path, [{"encryptJobId": "a", "date": "", "source": None}], applied_eids=["a"])
    assert mod.migrate_greeting_ledger(gp, hp)["tagged"] == 1
    after_first = gp.read_bytes()
    capsys.readouterr()
    second = mod.migrate_greeting_ledger(gp, hp)
    assert second == {"legacy_undated": 1, "legacy_draft_only": 0, "applied_without_greeting": 0, "tagged": 0}
    assert gp.read_bytes() == after_first
    assert "[台账]" not in capsys.readouterr().out, "零打标场次不得出声（正常会话零噪音）"


def test_migrate_greeting_ledger_dry_run_counts_without_writing(tmp_path):
    gp, hp = _migration_fixture(tmp_path, [{"encryptJobId": "a", "date": None}], applied_eids=["a"])
    before = gp.read_bytes()
    stats = mod.migrate_greeting_ledger(gp, hp, dry_run=True)
    assert stats["tagged"] == 1 and stats["legacy_undated"] == 1
    assert gp.read_bytes() == before, "dry-run 只算不写（#55 验收 4 同款约束）"


def test_migrate_greeting_ledger_census_counts_gap_without_backfill(tmp_path):
    """验收 6：投出了但台账没文案的 eid 只进普查读数，绝不凭空造条目回填。"""
    gp, hp = _migration_fixture(tmp_path, [{"encryptJobId": "a", "date": None}], applied_eids=["a", "x", "y", "z"])
    stats = mod.migrate_greeting_ledger(gp, hp)
    assert stats["applied_without_greeting"] == 3
    entries = json.loads(gp.read_text(encoding="utf-8"))
    assert {e["encryptJobId"] for e in entries} == {"a"}


def test_migrate_greeting_ledger_missing_file_censuses_gap_only(tmp_path):
    """台账文件缺失 ⇒ 当作空表继续：无条可打标，但缺口普查照算，且不落盘。"""
    gp = tmp_path / "greetings.json"
    hp = tmp_path / "applied_history.json"
    hp.write_text(json.dumps({"applied": [{"encryptJobId": "a"}, {"encryptJobId": "b"}]}), encoding="utf-8")
    assert mod.migrate_greeting_ledger(gp, hp) == dict(_MIGRATION_ZERO, applied_without_greeting=2)
    assert not gp.exists()


def test_migrate_greeting_ledger_corrupt_file_warns_and_preserves(tmp_path, capsys):
    """损坏文件绝不拿 [] 覆盖：warn 一行、原字节保留、四键全 0。"""
    gp, hp = tmp_path / "greetings.json", tmp_path / "applied_history.json"
    gp.write_text("not-json{", encoding="utf-8")
    hp.write_text(json.dumps({"applied": [{"encryptJobId": "a"}]}), encoding="utf-8")
    before = gp.read_bytes()
    assert mod.migrate_greeting_ledger(gp, hp) == _MIGRATION_ZERO
    assert gp.read_bytes() == before
    assert "warn" in capsys.readouterr().out


def test_greeting_migration_stats_keys_always_present():
    """验收 5/6 的 report 读数四键恒在（写法与 `_greeting_ledger_stats` 同构）。"""
    assert mod._greeting_migration_stats({}) == _MIGRATION_ZERO
    assert mod._greeting_migration_stats({"greeting_migration": None}) == _MIGRATION_ZERO
    assert mod._greeting_migration_stats({"greeting_migration": {"legacy_undated": 16, "tagged": 17}}) == {
        "legacy_undated": 16,
        "legacy_draft_only": 0,
        "applied_without_greeting": 0,
        "tagged": 17,
    }


# --- 发送前机械 gate（用户约定 2026-09-10：确定性不靠 agent 自评） ---


def _write_reply(session_dir, idx, payload):
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / f"reply_{idx}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_read_reply_returns_payload(tmp_path):
    _write_reply(tmp_path, 1, {"send_text": "我有7年Solidity经验", "level": "高"})
    data = mod.read_reply(tmp_path, 1)
    assert data == {"send_text": "我有7年Solidity经验", "level": "高"}


def test_read_reply_missing_or_corrupt_returns_none(tmp_path):
    assert mod.read_reply(tmp_path, 1) is None
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "reply_2.json").write_text("{oops", encoding="utf-8")
    assert mod.read_reply(tmp_path, 2) is None


def test_pre_send_gate_clean_draft_passes():
    wl = {"years": {7}, "entities": {"solidity"}}
    problems = mod.pre_send_gate(
        "我有7年Solidity经验，主网部署过3个协议。请问贵司的审计流程？".replace("贵司", "咱们"), "高", wl, notes="§1 §8"
    )
    assert problems == [], problems


def test_pre_send_gate_rejects_invalid_level():
    wl = {"years": {7}, "entities": set()}
    problems = mod.pre_send_gate("正文正常", "紧急", wl, notes="§1")
    assert any("level" in p for p in problems)
    assert mod.pre_send_gate("正文正常", None, wl, notes="§1")


def test_pre_send_gate_rejects_ai_fingerprint():
    wl = {"years": set(), "entities": set()}
    problems = mod.pre_send_gate("我精通Solidity，期待您的回复。", "高", wl, notes="§1")
    assert any("AI痕迹" in p for p in problems)


def test_pre_send_gate_rejects_internal_marker():
    wl = {"years": set(), "entities": set()}
    problems = mod.pre_send_gate("我有7年经验（见§2）。", "高", wl, notes="§1")
    assert any("内部标记" in p for p in problems)


def test_pre_send_gate_rejects_off_whitelist_claim():
    wl = {"years": {7}, "entities": {"solidity"}}
    problems = mod.pre_send_gate("我有9年Solidity经验。", "高", wl, notes="§1")
    assert any("白名单外" in p for p in problems)


def test_pre_send_gate_runs_without_cards_file():
    """fact_cards 缺失（whitelist=None）时白名单项跳过，其余 gate 仍生效。"""
    problems = mod.pre_send_gate("我精通Solidity。", "高", None, notes="§1")
    assert any("AI痕迹" in p for p in problems)
    assert not any("白名单外" in p for p in problems)


# --- 事实来源备注区核验（#27 项 3：notes 必填 + §编号存在性） ---


def test_pre_send_gate_rejects_missing_notes():
    """runbook 要求每条 claim 的来源（§N/repo/链上/日期）记入 reply_N.json 的 notes
    备注区——缺失/空白即拦截（起草规范开场必有年限+成果，claim 恒存在）。"""
    wl = {"years": {7}, "entities": {"solidity"}}
    for notes in (None, "", "  "):
        problems = mod.pre_send_gate("我有7年Solidity经验。", "高", wl, notes=notes, sections={1})
        assert any("备注" in p for p in problems), (notes, problems)


def test_pre_send_gate_rejects_non_string_notes_without_crash():
    """notes 写成非字符串（list/dict/数字）按缺失拦截，不抛 AttributeError——
    机械 gate 对任意坏形态 fail-closed（review 采纳项）。"""
    wl = {"years": {7}, "entities": {"solidity"}}
    for notes in (["§1"], {"src": "§1"}, 1, True):
        problems = mod.pre_send_gate("我有7年Solidity经验。", "高", wl, notes=notes, sections={1})
        assert any("备注" in p for p in problems), (notes, problems)


def test_pre_send_gate_valid_section_refs_pass():
    wl = {"years": {7}, "entities": {"solidity"}}
    problems = mod.pre_send_gate(
        "我有7年Solidity经验。", "高", wl, notes="§1 §14 repo:aaveapy 链上 polygonscan", sections={1, 14}
    )
    assert problems == [], problems


def test_pre_send_gate_non_section_sources_pass():
    """来源不强制 §编号：repo/链上/推文日期形态同样合法（runbook 认可四种来源）。"""
    wl = {"years": {7}, "entities": {"solidity"}}
    problems = mod.pre_send_gate(
        "我有7年Solidity经验。", "高", wl, notes="repo:marshmallow-v1 链上 polygonscan 推文 2026-08-15", sections={1}
    )
    assert problems == [], problems


def test_pre_send_gate_rejects_unknown_section_ref():
    """引用的 §编号须在 profile_background 小节集合内——陈旧引用=来源不可追溯。"""
    wl = {"years": {7}, "entities": {"solidity"}}
    problems = mod.pre_send_gate("我有7年Solidity经验。", "高", wl, notes="§99 链上", sections={1, 14})
    assert any("§99" in p for p in problems), problems


def test_pre_send_gate_sections_none_skips_existence_only():
    """profile_background 缺失（sections=None）时 §存在性跳过，备注非空仍强制——
    与 whitelist 不可用同语义（warn 不静默全跳）。"""
    wl = {"years": {7}, "entities": {"solidity"}}
    assert mod.pre_send_gate("我有7年Solidity经验。", "高", wl, notes="§1 链上", sections=None) == []
    assert mod.pre_send_gate("我有7年Solidity经验。", "高", wl, notes="", sections=None)


# --- 运行时状态词硬门（#34：未实测的现在时状态断言不得发给 HR） ---


def test_pre_send_gate_blocks_unverified_state_word():
    """#34 真实事故回放：2022 年部署快照写成「生产运行至今」，旧门 0 违规放行。"""
    wl = {"years": {7}, "entities": {"solidity"}}
    problems = mod.pre_send_gate("我有7年Solidity经验，主网合约生产运行至今。", "高", wl, notes="§1 链上")
    assert any("未实测状态词" in p for p in problems), problems


def test_pre_send_gate_allows_state_word_with_recent_evidence():
    """有近期实测佐证（state_evidence 含 7 天内日期）才放行现状断言。"""
    wl = {"years": {7}, "entities": {"solidity"}}
    ev = f"链上 polygonscan {date.today().isoformat()} 调用确认"
    problems = mod.pre_send_gate(
        "我有7年Solidity经验，主网合约生产运行至今。", "高", wl, notes="§1 链上", state_evidence=ev
    )
    assert problems == [], problems


def test_pre_send_gate_allows_snapshot_past_tense_without_evidence():
    """快照式过去时是本票规定的正确写法：不要求 state_evidence。"""
    wl = {"years": {7}, "entities": {"solidity"}}
    problems = mod.pre_send_gate("我有7年Solidity经验，2022 年在主网部署并已验证。", "高", wl, notes="§1 链上")
    assert problems == [], problems


# --- 称呼规范软拦截（#27 项 1：确定性子集，warning 不阻断） ---


def test_salutation_english_name_no_ninhao():
    assert mod.salutation_warnings("Peter", "猎头", "Hi Peter，我有7年Solidity经验。") == []
    warns = mod.salutation_warnings("Peter", "技术开发", "您好，我有7年Solidity经验。")
    assert any("您好" in w for w in warns), warns


def test_salutation_no_name_uses_ninhao():
    assert mod.salutation_warnings("", "HR", "您好，我有7年Solidity经验。") == []
    warns = mod.salutation_warnings(None, None, "我有7年Solidity经验。")
    assert any("您好" in w for w in warns), warns


def test_salutation_honorific_kept_as_is():
    assert mod.salutation_warnings("澜女士", "HR", "澜女士您好，我有7年经验。") == []
    warns = mod.salutation_warnings("澜女士", "HR", "您好，我有7年经验。")
    assert any("澜女士" in w for w in warns), warns
    # 平台装饰前缀：A商务-李经理 → 用「李经理」即算原样保留
    assert mod.salutation_warnings("A商务-李经理", "HR", "李经理您好，我有7年经验。") == []


def test_salutation_recruiter_surname_zong():
    assert mod.salutation_warnings("宓乐言", "招聘者", "宓总您好，我有7年经验。") == []
    warns = mod.salutation_warnings("宓乐言", "招聘者", "您好，我有7年经验。")
    assert any("宓总" in w for w in warns), warns
    # 性别推测（周先生）不在确定性规范内：公司招聘者应姓+总
    warns = mod.salutation_warnings("宓乐言", "招聘者", "周先生您好，我有7年经验。")
    assert any("宓总" in w for w in warns), warns


def test_salutation_chinese_full_name_flagged():
    warns = mod.salutation_warnings("陈乐宜", "招聘者", "陈乐宜您好，我有7年经验。")
    assert any("全名" in w for w in warns), warns


def test_salutation_headhunter_fallback_zong_allowed():
    # 猎头「无把握兜底姓+总」合法不告警（性别推测不可代码化，归 runbook 约束）
    assert mod.salutation_warnings("岑建明", "猎头", "刘总您好，我有7年经验。") == []
    warns = mod.salutation_warnings("岑建明", "猎头", "岑建明您好，我有7年经验。")
    assert any("全名" in w for w in warns), warns


def test_salutation_compound_surname_tolerated():
    assert mod.salutation_warnings("欧阳雪", "招聘者", "欧阳总您好，我有7年经验。") == []


def test_salutation_mixed_name_skipped():
    # 规范未定义的混合形态（Peter王）不检查
    assert mod.salutation_warnings("Peter王", "HR", "您好，我有7年经验。") == []


# --- 「结尾只带软问题」软提醒（用户约定 2026-10-16，apply-runbook §2） ---

_K_TEXT = "宓总您好，我有7年Solidity经验，主网部署过合约。请问这岗更偏自研链底层，还是链上服务那一侧？"


def test_question_hits_knowledge_question():
    """正对照：知识型问句命中，且报的是那句本身（不是非空就算过）。"""
    warns = mod.question_warnings(_K_TEXT)
    assert len(warns) == 1, warns
    assert "更偏自研链底层" in warns[0], warns
    assert "知识型提问" in warns[0], warns


def test_soft_question_forms_pass():
    """软问题句式（对方查自己收件箱/日程就能答）一律放行——这些是新规范允许的收尾形态。
    本门只管「问的是不是对方答得上来」，不管材料选得对不对（审计报告该不该发由 runbook 定）。"""
    for text in (
        "宓总您好，主网部署过合约。方便的话我把审计报告发您看？",
        "宓总您好，方便的话我把简历和网站发您看？",
        "宓总您好，这岗还在看人吗？",
        "宓总您好，您这边方便的时间？我随时可以聊。",
        "宓总您好，简历和链上验证链接要不要我发过来？",
    ):
        assert mod.question_warnings(text) == [], text


def test_knowledge_question_not_rescued_by_trailing_soft_question():
    """逐问句切段：知识型问句后面跟一句软问题，不得把前一句一起放过去
    （2026-10-16 历史回放实测形态：「…重心在哪块？方便的话我发 repo 给您看。」）。"""
    warns = mod.question_warnings("宓总您好，请问这岗合约方向重心在哪块？方便的话我发 repo 给您看。")
    assert any("重心在哪块" in w for w in warns), warns


def test_statement_ending_never_hits():
    """没有问句就没有命中；空正文不崩。"""
    assert mod.question_warnings("宓总您好，主网部署过合约，审计报告和链上地址都在 repo。") == []
    assert mod.question_warnings("") == []
    assert mod.question_warnings(None) == []


def test_hits_capped_at_two():
    warns = mod.question_warnings("这岗偏底层吗？还是偏应用？链路谁负责？对账归谁？")
    assert len(warns) == 2, warns


# --- 一小时吞吐量模拟（README 默认节奏节的事实来源，守护节奏参数不被无意改掉） ---


def test_pacer_hourly_throughput_bounds():
    limits_spec = importlib.util.spec_from_file_location("_limits", Path(__file__).resolve().parent / "_limits.py")
    batch = importlib.util.module_from_spec(limits_spec)
    limits_spec.loader.exec_module(batch)

    clock = {"t": 0.0}

    def fake_sleep(lo, hi, rng=None):
        clock["t"] += (rng or random).uniform(lo, hi)

    totals = []
    for seed in range(300):
        rng = random.Random(seed)
        clock["t"] = 0.0
        pacer = batch.SessionPacer(
            {}, sleep_fn=lambda lo, hi, r=rng: fake_sleep(lo, hi, r), hour_fn=lambda: 10, rng=rng
        )
        sent = 0
        while clock["t"] < 3600:
            pacer.wait_sent()
            sent += 1
        totals.append(sent)
    mean = sum(totals) / len(totals)
    assert 6 <= mean <= 11, f"纯节奏期望 {mean:.1f} 封/时，偏离 README 承诺区间"
    assert min(totals) >= 3 and max(totals) <= 20, f"单次一小时分布异常: min={min(totals)} max={max(totals)}"


# --- 关键词轮导航稳定性（#35：goto 超时重试 + 监听器注销 + 关键词级隔离） ---

_JOBLIST = [
    {"encryptJobId": "j1", "jobName": "智能合约工程师", "brandName": "某基金"},
    {"encryptJobId": "j2", "jobName": "Solidity 工程师", "brandName": "某公链"},
]


class _XhrResp:
    def __init__(self, payload, url="https://www.zhipin.com/wapi/zpgeek/search/joblist.json"):
        self.url = url
        self._payload = payload

    def json(self):
        return self._payload


class _ListPage:
    """列表页替身（搜索 / 推荐共用）：`goto` 投第一批，此后**每次 `evaluate`（真身是
    human_scroll 的 scrollBy）再投下一批**，批次用尽即不再有新响应——用来验证 #33 第七轮
    「滚到底持续累积」与「零新增即到底」两条路径。

    `has_more` 默认跟随「后面还有没有批次」（与真实端点 `zpData.hasMore` 同义），
    显式传 False 可模拟页面自己报到底。"""

    def __init__(
        self, fail_times=0, batches=None, url="https://www.zhipin.com/wapi/zpgeek/search/joblist.json", has_more=None
    ):
        self.listeners = []
        self.gotos = []
        self.scrolls = 0
        self._fail_times = fail_times
        self._batches = list(batches if batches is not None else [_JOBLIST])
        self._url = url
        self._has_more = has_more

    def on(self, event, handler):
        self.listeners.append(handler)

    def remove_listener(self, event, handler):
        self.listeners.remove(handler)

    def _emit(self):
        if not self._batches:
            return
        batch = self._batches.pop(0)
        zd = {"jobList": batch, "hasMore": bool(self._batches) if self._has_more is None else self._has_more}
        for h in list(self.listeners):
            h(_XhrResp({"code": 0, "zpData": zd}, url=self._url))

    def goto(self, url, **kw):
        self.gotos.append(url)
        if self._fail_times > 0:
            self._fail_times -= 1
            raise RuntimeError("Timeout 30000ms exceeded.")
        self._emit()

    def evaluate(self, _script):
        """human_scroll 的落点：滚一次 = 页面请求下一批。"""
        self.scrolls += 1
        self._emit()

    def wait_for_timeout(self, _ms):
        pass


class _SearchPage(_ListPage):
    """搜索页替身：前 fail_times 次 goto 抛超时；成功时向已注册监听器投递 joblist XHR。
    joblist 为单批快捷写法（等价 batches=[joblist]）。"""

    def __init__(self, fail_times=0, joblist=None, batches=None, has_more=None):
        super().__init__(
            fail_times=fail_times,
            batches=[joblist] if joblist is not None else batches,
            has_more=has_more,
        )


def _quiet_scroll(monkeypatch, patch_sleep=True):
    """滚动采集的测试夹具（#33 第七轮）：把单轮等待与轮间停顿压到 0——真实值是
    SCROLL_WAIT_SEC=8s + SCROLL_PAUSE=1.5~3s（外加每轮 2~3 次 0.3~0.8s 的滚间停顿），
    不压掉每个用例要白等好几秒。已经自己接管 time.sleep 的夹具传 patch_sleep=False。

    另把 human_scroll 换成单步版（#44 反侦察把一段滚动拆成 2~4 次 scrollBy 后，
    _ListPage 夹具「每次 evaluate 投一批」的绑定会把一批瞬间耗光、提前 hasMore=false
    ——夹具关心的是「一轮滚动」语义，不关心滚动内部拆几步）。"""
    monkeypatch.setattr(mod, "SCROLL_WAIT_SEC", 0)
    monkeypatch.setattr(mod, "SCROLL_PAUSE", (0, 0))
    monkeypatch.setattr(mod, "human_scroll", lambda page, px: page.evaluate("window.scrollBy(0, %d)" % px))
    if patch_sleep:
        monkeypatch.setattr(mod.time, "sleep", lambda *_: None)


def _quiet_search(monkeypatch, sleeps=None):
    def fake_sleep(seconds=0):
        if sleeps is not None:
            sleeps.append(seconds)

    monkeypatch.setattr(mod.time, "sleep", fake_sleep)
    monkeypatch.setattr(mod, "search_via_input", lambda page, query: True)
    _quiet_scroll(monkeypatch, patch_sleep=False)


def test_search_list_retries_goto_after_timeout(monkeypatch):
    """#35：首次 goto 超时不再终结关键词轮——退避后重试一次，成功后正常返回条目。"""
    sleeps = []
    _quiet_search(monkeypatch, sleeps)
    page = _SearchPage(fail_times=1)
    listing = mod.search_list(page, "智能合约")
    assert [j["encryptJobId"] for j in listing] == ["j1", "j2"]
    assert len(page.gotos) == 2, "1 次失败 + 1 次重试"
    assert max(sleeps) >= mod.KEYWORD_GOTO_BACKOFF, "重试前有退避等待"
    assert page.listeners == [], "监听器注销"


def test_search_list_cleans_listener_when_retries_exhausted(monkeypatch):
    """#35 根因之一：goto 抛异常时 remove_listener 永不执行（监听器泄漏）。
    try/finally 保证失败路径同样注销，异常向上抛出交关键词级边界处理。"""
    _quiet_search(monkeypatch)
    page = _SearchPage(fail_times=99)
    with pytest.raises(RuntimeError, match="Timeout"):
        mod.search_list(page, "AI")
    assert len(page.gotos) == 2, "重试上限 1 次（共 2 次尝试）"
    assert page.listeners == [], "失败路径同样注销监听器"


def test_keyword_round_records_skip_and_returns_none(monkeypatch):
    """#35 关键词级边界：导航重试后仍失败 → 记 keyword_skips 并返回 None，
    主循环转下一关键词（不崩整轮）。"""
    _quiet_search(monkeypatch)
    page = _SearchPage(fail_times=99)
    state = {"applied": 0, "direct_fallback": 0, "keyword_skips": []}
    assert mod.keyword_round(page, "Web3", state, set()) is None
    assert [s["query"] for s in state["keyword_skips"]] == ["Web3"]
    assert "Timeout" in state["keyword_skips"][0]["error"], "跳过原因带原始异常摘要"


def test_keyword_round_returns_fresh_jobs(monkeypatch):
    """正常路径不变：搜索成功 → 过滤已投递 → 返回新岗位；不误记 keyword_skips。"""
    _quiet_search(monkeypatch)
    page = _SearchPage()
    state = {"applied": 0, "direct_fallback": 0, "keyword_skips": []}
    fresh = mod.keyword_round(page, "AI", state, {"j1"})
    assert [j["encryptJobId"] for j in fresh] == ["j2"]
    assert state["keyword_skips"] == []


def test_keyword_round_isolates_malformed_listing(monkeypatch):
    """列表形态漂移（条目非 dict）同样不得终结整轮：解析也在关键词级边界内。"""
    _quiet_search(monkeypatch)
    page = _SearchPage(joblist=["not-a-dict"])
    state = {"applied": 0, "direct_fallback": 0, "keyword_skips": []}
    assert mod.keyword_round(page, "AI", state, set()) is None
    assert [s["query"] for s in state["keyword_skips"]] == ["AI"]


def test_build_report_carries_keyword_skips():
    """#35：被跳过的关键词随会话报告落盘（汇报口径），不静默丢失。"""
    state = {
        "applied": 2,
        "direct_fallback": 1,
        "keyword_skips": [{"query": "Web3", "error": "Timeout 30000ms exceeded."}],
    }
    report = mod.build_report(["Web3", "AI"], state, [])
    assert report["keyword_skips"] == state["keyword_skips"]
    assert report["applied"] == 2 and report["direct_fallback"] == 1
    assert report["queries"] == ["Web3", "AI"]
    # #64：state 未接线时 greeting_ledger 给零形态而不是缺键
    assert report["greeting_ledger"] == {"added": 0, "overwritten": 0, "skipped": 0}
    # #64 追加验收 5/6：迁移读数四键同样恒在（零形态）
    assert report["greeting_ledger_migration"] == {
        "legacy_undated": 0,
        "legacy_draft_only": 0,
        "applied_without_greeting": 0,
        "tagged": 0,
    }


# --- #37 会话收尾 EDD 快照（观测层故障绝不阻断投递收尾）---


def _edd_paths(tmp_path, followup_data=None):
    paths = {
        "followup": tmp_path / "followup_report.json",
        "greetings": tmp_path / "greetings.json",
        "annotations": tmp_path / "reply_annotations.json",
        "trend": tmp_path / "eval_trend.jsonl",
    }
    if followup_data is not None:
        paths["followup"].write_text(json.dumps(followup_data), encoding="utf-8")
        paths["greetings"].write_text("[]", encoding="utf-8")
        paths["annotations"].write_text("{}", encoding="utf-8")
    return paths


def test_edd_wrapup_appends_trend_and_marks_report(tmp_path, capsys):
    """有 followup 数据 → 追加快照 + 渲染趋势块 + 返回 outcome 供 report.json 记录。"""
    followup = {"conversations": [{"company": "甲公司", "summary": "hi", "status": "awaiting", "appliedJob": "J1"}]}
    outcome = mod.edd_wrapup(_edd_paths(tmp_path, followup), profile="p", now="2026-09-17T10:00:00")
    assert outcome is not None and outcome["trend_appended"] is True
    assert "## EDD 趋势跟踪" in capsys.readouterr().out


def test_edd_wrapup_failure_swallowed_returns_none(tmp_path, capsys):
    """followup 坏 JSON → warn + None：观测层故障不影响投递收尾与退出码。"""
    paths = _edd_paths(tmp_path)
    paths["followup"].write_text("not json", encoding="utf-8")
    paths["greetings"].write_text("[]", encoding="utf-8")
    outcome = mod.edd_wrapup(paths, profile="p", now="2026-09-17T10:00:00")
    assert outcome is None
    out = capsys.readouterr().out
    assert "[warn] EDD 收尾快照失败" in out


def test_edd_wrapup_no_followup_returns_skip_outcome(tmp_path):
    """无 followup → outcome 带 reason（trend_appended=False），不是故障。"""
    outcome = mod.edd_wrapup(_edd_paths(tmp_path), profile="p", now="2026-09-17T10:00:00")
    assert outcome == {"trend_appended": False, "reason": "no followup data"}


def test_edd_wrapup_zero_sample_after_nonzero_history_skips_snapshot(tmp_path, capsys):
    """#59 因果链的收尾侧：残缺采集算出的 0 样本快照不追加趋势——走既有 trend_appended=False
    分支打跳过行（不渲染趋势块），outcome 原样进 report.json 的 edd 字段供事后核对。"""
    paths = _edd_paths(
        tmp_path, {"conversations": [{"company": "没发过招呼的公司", "summary": "[送达]", "status": "awaiting"}]}
    )
    paths["trend"].write_text(
        json.dumps({"ts": "2026-09-29T23:00:00", "profile": "p", "sampleSize": 50, "repliedCount": 2}) + "\n",
        encoding="utf-8",
    )
    outcome = mod.edd_wrapup(paths, profile="p", now="2026-09-30T00:10:19")
    assert outcome["trend_appended"] is False and "0 样本" in outcome["reason"]
    term = capsys.readouterr().out
    assert "[EDD] 跳过趋势快照" in term and "## EDD 趋势跟踪" not in term, term
    assert len(paths["trend"].read_text(encoding="utf-8").splitlines()) == 1, "假零点进了趋势历史"


# --- #45 密度阈值提示制：会话收尾通道（达标 → 提示块 + 候选清单 + 系统通知；低密度静默）---


def _density_outcome(due):
    return {
        "trend_appended": False,
        "snapshot": {},
        "review_due": {"due": False, "reasons": []},
        "density_due": {"due": due, "threshold": 5, "groups": ["by_addressee:hr（回复 5/23）"]},
    }


def test_edd_wrapup_density_due_notifies_and_prints_candidates(tmp_path, capsys, monkeypatch):
    """密度达标 → 控制台提示块 + 候选清单 + macOS 通知（提示制，不自动晋级）。"""
    monkeypatch.setattr(mod.g, "record_session_snapshot", lambda p, profile, now=None: _density_outcome(True))
    monkeypatch.setattr(
        mod._edd,
        "load_pending_candidates",
        lambda path: [{"key": "by_addressee:hr", "kind": "winning", "sample": 23, "replyRate": 0.217}],
    )
    notified = []
    monkeypatch.setattr(mod, "notify", lambda t, m: notified.append((t, m)))
    outcome = mod.edd_wrapup(_edd_paths(tmp_path), profile="p", now="2026-09-17T10:00:00")
    assert outcome["density_due"]["due"] is True
    out = capsys.readouterr().out
    assert "💡 [范式审阅]" in out and "是不是要更新招呼语范式" in out
    assert "by_addressee:hr" in out and "promote-winners" in out
    assert len(notified) == 1 and "范式" in notified[0][0]


def test_edd_wrapup_density_below_threshold_silent(tmp_path, capsys, monkeypatch):
    """低密度静默（拍板 3）：无提示块、无通知——09-24 那种 2 条回复不弹窗。"""
    monkeypatch.setattr(mod.g, "record_session_snapshot", lambda p, profile, now=None: _density_outcome(False))
    notified = []
    monkeypatch.setattr(mod, "notify", lambda t, m: notified.append((t, m)))
    mod.edd_wrapup(_edd_paths(tmp_path), profile="p", now="2026-09-17T10:00:00")
    out = capsys.readouterr().out
    assert "范式审阅" not in out
    assert notified == []


def test_edd_wrapup_density_paths_without_pending_key(tmp_path, capsys, monkeypatch):
    """paths 无 pending 键（旧调用方/降级场景）→ 候选按暂无处理，不 KeyError。"""
    monkeypatch.setattr(mod.g, "record_session_snapshot", lambda p, profile, now=None: _density_outcome(True))
    notified = []
    monkeypatch.setattr(mod, "notify", lambda t, m: notified.append((t, m)))
    paths = _edd_paths(tmp_path)
    outcome = mod.edd_wrapup(paths, profile="p", now="2026-09-17T10:00:00")
    assert outcome["density_due"]["due"] is True
    assert "暂无" in capsys.readouterr().out


# --- #40 冒烟 dry-run（LIVE_DRY_RUN=1：到发送前 gate 截停，不点沟通/不发送/不写台账）---


def _boom(what):
    """越界哨兵：dry-run 下这些动作绝不允许发生，发生即测试失败。"""

    def f(*a, **k):
        raise AssertionError(f"dry-run 越界: {what}")

    return f


class _DryRunPage:
    """最小 page 替身：apply_live 从进入详情走到 dry-run 截停所需的方法面。"""

    def on(self, event, handler):
        pass

    def query_selector(self, sel):
        return None


def _dry_run_mocks(monkeypatch, send_text, reply):
    """apply_live 的外部动作替换为受控桩；点沟通/发送/台账三类越界动作挂哨兵。
    enter_detail / grab_detail 的替身带 `**kw`：#56 A3 给它们加了 beat_fn 形参，
    桩只关心返回值，不关心拍点。"""
    monkeypatch.setattr(mod, "enter_detail", lambda page, eid, goto_fn, **kw: "card")
    monkeypatch.setattr(mod, "human_read", lambda *a, **k: None)
    monkeypatch.setattr(mod, "shot", lambda *a, **k: None)
    monkeypatch.setattr(mod, "detect_risk_signal", lambda page: None)
    monkeypatch.setattr(mod, "find_continue_button", lambda page: None)
    monkeypatch.setattr(mod, "grab_detail", lambda page, d, **kw: "JD 全文：负责测试冒烟")
    monkeypatch.setattr(mod, "wait_reply", lambda *a, **k: send_text)
    monkeypatch.setattr(mod, "read_reply", lambda *a, **k: reply)
    monkeypatch.setattr(mod, "safe_go_back", lambda *a, **k: None)
    # 白名单不可用（None）→ 白名单 gate 跳过，与真实冒烟缺卡片时的 warn 语义一致
    monkeypatch.setattr(mod.g, "load_whitelist_file", lambda path: None)
    monkeypatch.setattr(mod, "find_comm_button", _boom("find_comm_button"))
    monkeypatch.setattr(mod, "click_with_retry", _boom("click_with_retry"))
    monkeypatch.setattr(mod, "log_apply", _boom("log_apply"))
    monkeypatch.setattr(mod, "record_history", _boom("record_history"))
    monkeypatch.setattr(mod, "record_greeting", _boom("record_greeting"))


_DRY_RUN_CFG = {"dry_run": True}
_DRY_RUN_JOB = {"jobName": "测试岗", "brandName": "测试公司", "encryptJobId": "e1"}


def _run_apply_dry_run(tmp_path, monkeypatch, send_text, reply, job=None):
    _dry_run_mocks(monkeypatch, send_text, reply)
    pacer = SimpleNamespace(wait_skip=lambda: None, wait_sent=lambda: None)
    platform = SimpleNamespace(login_dialog_selector="#login-dialog")
    results, state = [], {"applied": 0, "direct_fallback": 0, "keyword_skips": []}
    outcome = mod.apply_live(
        _DryRunPage(), job or _DRY_RUN_JOB, tmp_path, 1, tmp_path, dict(_DRY_RUN_CFG), pacer, platform, results, state
    )
    return outcome, results, state


def test_dry_run_stop_records_and_stops(capsys):
    """dry-run 截停记录：dry_run_ok 条目带草稿与两道软提醒 warning，返回 stop，打印截停说明。"""
    results = []
    outcome = mod.dry_run_stop(results, 2, _DRY_RUN_JOB, "您好，草稿", ["warn 称呼"], ["warn 提问"])
    assert outcome == "stop"
    assert results[0] == {
        "idx": 2,
        "job": "测试岗",
        "brand": "测试公司",
        "status": "dry_run_ok",
        "send_text": "您好，草稿",
        "salutation_warns": ["warn 称呼"],
        "question_warns": ["warn 提问"],
    }
    assert "[dry-run]" in capsys.readouterr().out


def test_apply_live_dry_run_stops_before_comm_click(tmp_path, monkeypatch):
    """核心安全属性：gate 放行稿在点「立即沟通」**之前**截停——查找/点击/发送/台账零发生。"""
    outcome, results, state = _run_apply_dry_run(
        tmp_path, monkeypatch, "您好，测试冒烟草稿", {"level": "中", "notes": "来源 repo 2026-01"}
    )
    assert outcome == "stop"
    assert [r["status"] for r in results] == ["dry_run_ok"]
    assert state["applied"] == 0, "dry-run 不占投递名额"


def test_dry_run_wires_question_warn_into_report(tmp_path, monkeypatch, capsys):
    """接线证据（不只是纯函数）：知识型提问的过门草稿走完整 apply_live 路径后，report 条目带
    question_warns 且终端打出 `[warn 提问]`；同一形态换成软问题收尾则为空。
    软提醒不阻断——两种形态都仍走到 dry_run_ok（未被 gate 拦）。"""
    reply = {"level": "中", "notes": "来源 repo 2026-01"}
    hard = "宓总您好，我有7年Solidity经验，主网部署过合约。请问这岗更偏自研链底层，还是链上服务那一侧？"
    soft = "宓总您好，我有7年Solidity经验，主网部署过合约。方便的话我把审计报告发您看？"
    job = {
        "jobName": "测试岗",
        "brandName": "测试公司",
        "encryptJobId": "e1",
        "bossName": "宓乐言",
        "bossTitle": "招聘者",
    }
    outcome, results, _ = _run_apply_dry_run(tmp_path, monkeypatch, hard, reply, job=job)
    out = capsys.readouterr().out
    assert outcome == "stop" and results[0]["status"] == "dry_run_ok", "软提醒不得阻断发送路径"
    assert any("更偏自研链底层" in w for w in results[0]["question_warns"]), results[0]
    assert "[warn 提问]" in out, out
    assert results[0]["salutation_warns"] == [], "称呼合规的草稿不该顺带报称呼"
    _, r2, _ = _run_apply_dry_run(tmp_path, monkeypatch, soft, reply, job=job)
    assert r2[0]["question_warns"] == [], "软问题收尾不报（负对照）"


def test_apply_live_dry_run_still_respects_gate(tmp_path, monkeypatch):
    """gate 拦截优先于截停：违规稿走既有 gate_blocked 路径（不记 dry_run_ok 不发送）。"""
    outcome, results, state = _run_apply_dry_run(tmp_path, monkeypatch, "您好，草稿", {"level": None, "notes": ""})
    assert outcome == "next", "拦截后继续下一岗位，与真实会话语义一致"
    assert results[0]["status"] == "gate_blocked"
    assert state["applied"] == 0


def test_build_report_carries_dry_run_flag():
    """#40 机械核验字段：dry_run 进 report.json（冒烟证据）；旧 state 缺键回落 False。"""
    state = {"applied": 0, "direct_fallback": 0, "keyword_skips": [], "dry_run": True}
    assert mod.build_report(["q"], state, [])["dry_run"] is True
    legacy = {"applied": 0, "direct_fallback": 0, "keyword_skips": []}
    assert mod.build_report(["q"], legacy, [])["dry_run"] is False


# --- #41：详情页滞留被弹回列表 → 重进详情再找沟通按钮（慢起草岗漏投修复） ---

_LIST_URL = "https://www.zhipin.com/web/geek/job?query=Solidity&city=101020100"
_DETAIL_URL = "https://www.zhipin.com/job_detail/e1.html?lid=abc"


class _FakeKeyboard:
    """键盘替身：记下 press 过的键名（#68）。
    为什么要新增这个类——本文件此前**任何**替身页都没有 `keyboard`，因为 Enter 兜底那一支
    在生产里恒不触发（判据查字符串 `"contenteditable" in "DIV|true"` 永假），也就从没人真调过
    `page.keyboard.press`。替身缺这块，正是那条分支被测试永久绕过的物证。"""

    def __init__(self):
        self.pressed = []

    def press(self, key):
        self.pressed.append(key)


class _SendPage:
    """发送段替身：url 可配（详情判定锚点）；query_selector 恒 None（登录框不命中）；
    panel_title 可配（#33 第八轮：分栏视图判据走 evaluate 取右栏标题，默认空 = 非分栏）。
    `keyboard` 记录 Enter 兜底那一下（#68 新用例的取证点）。"""

    def __init__(self, url, panel_title=""):
        self.url = url
        self.panel_title = panel_title
        self.keyboard = _FakeKeyboard()

    def evaluate(self, js, *args):
        return self.panel_title

    def query_selector(self, sel):
        return None

    def on(self, event, handler):
        pass

    def remove_listener(self, event, handler):
        pass


class _FakeInputEl:
    """输入框替身：代表真实聊天弹窗那个 `textarea`（**非** contenteditable），fill 记录内容。
    #68：`evaluate` 按脚本**分派**应答——问 `isContentEditable` 回 False（它今天回字符串
    `"TEXTAREA|"`，被 `bool()` 一取就成了真值，那正是本票要的负对照「靠巧合绿」）；
    问老的 tagName 探针仍回 `"TEXTAREA|"`，两条各自诚实。
    #62 补的一条应答：反馈浮层祖先链探针回 0（＝不在浮层里）。本替身代表的是**真实聊天弹窗**
    那个 `textarea`，它按定义就不在 `.c-satisfaction-feedback` 容器内——补这条是把替身本来就
    声称代表的 DOM 形态交代给新探针，不是放宽被测行为（对照 `_FeedbackBoxEl`：同一个探针它回 1，
    那条才是「反馈框」形态）。"""

    def __init__(self):
        self.filled = None

    def evaluate(self, js, *args):
        if "AUTOJOB_FEEDBACK_PROBE" in js:
            return 0
        if "isContentEditable" in js:
            return False
        return "TEXTAREA|"

    def click(self, **kw):
        pass

    def fill(self, text):
        self.filled = text


def test_on_job_detail_matches_eid():
    """#41 判定锚点（形态①：独立详情页）：URL 含 job_detail 且含本岗 eid（防误点别岗按钮）。"""
    assert mod.on_job_detail(_SendPage(_DETAIL_URL), "e1") is True
    assert mod.on_job_detail(_SendPage(_LIST_URL), "e1") is False, "弹回搜索列表：URL 无 job_detail"
    assert mod.on_job_detail(_SendPage(_DETAIL_URL), "e2") is False, "别人的详情页不算在详情"


def test_on_job_detail_recognises_split_panel_by_title():
    """#33 第八轮（形态②：分栏视图）：URL 不含 job_detail，但右栏承载本岗 ⇒ 算在详情。
    该判据若缺失，起草期间会被误判「已弹回」→ 主动重进毁掉右栏（09-23 三例 no_btn 根因之一）。"""
    page = _SendPage(_LIST_URL, panel_title="区块链开发工程师 30-50K 上海 经验不限 本科")
    assert mod.on_job_detail(page, "e1", "区块链开发工程师") is True


def test_on_job_detail_rejects_split_panel_of_other_job():
    """分栏右栏承载的是别岗 ⇒ 不算在详情（防误判后点到别岗按钮）。"""
    page = _SendPage(_LIST_URL, panel_title="高级Java开发-远程 30-40K 上海")
    assert mod.on_job_detail(page, "e1", "区块链开发工程师") is False


def test_comm_button_selectors_cover_split_view_button():
    """#33 第八轮：两套详情形态的按钮 class 都要在候选里。
    探针实测分栏视图按钮 = `a.op-btn.op-btn-chat`（旧组 `.job-op`/`.btn-startchat` 命中数为 0）。"""
    from platforms.chat_first import COMM_BTN_SELECTORS

    joined = " ".join(COMM_BTN_SELECTORS)
    assert "btn-startchat" in joined, "独立详情页（旧版组件）选择器不得丢"
    assert "op-btn-chat" in joined, "分栏视图右栏按钮选择器缺失 ⇒ no_btn 回归"


def test_recovery_falls_back_to_standalone_detail_once(monkeypatch):
    """#33 第八轮：首查落空 → **直跳独立详情页**兜底一次（取代「回列表点卡片重进」：
    卡片点击会被吸顶筛选栏拦截，且点中后渲染回分栏视图）。"""
    calls = []
    monkeypatch.setattr(
        mod, "find_comm_button", lambda page, **kw: calls.append("find") or (None if len(calls) == 1 else "BTN")
    )
    monkeypatch.setattr(mod, "open_standalone_detail", lambda page, eid: calls.append(f"goto:{eid}"))
    page = _SendPage(_LIST_URL, panel_title="测试岗 20-30K")
    assert mod.find_comm_button_with_recovery(page, "e1", "测试岗") == "BTN"
    assert calls == ["find", "goto:e1", "find"], "兜底只一次，且用直跳而非卡片重进"


def test_recovery_returns_none_after_one_fallback(monkeypatch):
    """兜底后仍落空 ⇒ 返回 None 交调用方 no_btn 存证，不循环重试。"""
    calls = []
    monkeypatch.setattr(mod, "find_comm_button", lambda page, **kw: calls.append("find") or None)
    monkeypatch.setattr(mod, "open_standalone_detail", lambda page, eid: calls.append(f"goto:{eid}"))
    page = _SendPage(_LIST_URL, panel_title="测试岗 20-30K")
    assert mod.find_comm_button_with_recovery(page, "e1", "测试岗") is None
    assert calls == ["find", "goto:e1", "find"]


def test_recovery_switches_away_when_panel_hosts_other_job(monkeypatch):
    """#33 第八轮误投守卫：进入职位页时右栏会自动渲染第一张卡片——若它不是本岗，
    全局查找命中的就是**别岗按钮**。必须在查找前先换到本岗独立详情页。"""
    calls = []
    monkeypatch.setattr(mod, "find_comm_button", lambda page, **kw: calls.append("find") or "BTN")
    monkeypatch.setattr(mod, "open_standalone_detail", lambda page, eid: calls.append(f"goto:{eid}"))
    page = _SendPage(_LIST_URL, panel_title="高级Java开发-远程 30-40K 上海")
    assert mod.find_comm_button_with_recovery(page, "e1", "测试岗") == "BTN"
    assert calls == ["goto:e1", "find"], "别岗右栏 ⇒ 先换页再查找（否则会点到别岗按钮）"


def test_recovery_keeps_panel_when_it_hosts_target_job(monkeypatch):
    """右栏就是本岗 ⇒ 不换页，直接查找（避免无谓导航）。"""
    calls = []
    monkeypatch.setattr(mod, "find_comm_button", lambda page, **kw: calls.append("find") or "BTN")
    monkeypatch.setattr(mod, "open_standalone_detail", lambda page, eid: calls.append(f"goto:{eid}"))
    page = _SendPage(_LIST_URL, panel_title="测试岗 20-30K")
    assert mod.find_comm_button_with_recovery(page, "e1", "测试岗") == "BTN"
    assert calls == ["find"]


def _send_mocks(monkeypatch, btn_seq, input_seq, continue_btn=None):
    """发送段桩：find_comm_button / find_input 按序列返回并计数；其余外部动作全替换。
    序列耗尽后返回 None（模拟持续落空），不做长度对齐假设。
    enter_detail / click_with_retry / grab_detail 的替身显式收下 #56 A3 新增的 beat_fn
    形参（本段桩不注入拍点，拍点由 A3 那几个专用用例单独锁）。"""
    calls = {"enter": 0, "btn": 0, "find_input": 0, "evidence": [], "clicks": [], "standalone": []}
    btns = iter(btn_seq)
    inputs = iter(input_seq)

    def fake_enter(page, eid, goto_fn, beat_fn=None):
        calls["enter"] += 1
        return "card"

    def fake_standalone(page, eid):
        calls["standalone"].append(eid)
        # #62 补齐的一条副作用：真的 `open_standalone_detail` 是 `page.goto(JOB_DETAIL_URL)`，
        # 替身过去只记「被调过」不挪 URL，于是本用例的 page 在点击段之后仍停在搜索列表 URL 上——
        # 那不是被测行为，是替身少建模了一次导航。#62 的打字前守卫按 URL 判形态，替身不挪 URL
        # 就等于让用例声称「已经在本岗独立详情页上」而页面事实仍在列表，二者必须一致。
        page.url = mod.JOB_DETAIL_URL.format(eid=eid)

    def fake_btn(page, **kw):
        calls["btn"] += 1
        return next(btns, None)

    def fake_input(page, **kw):
        calls["find_input"] += 1
        return next(inputs, None)

    def fake_click(page, btn, action="", beat_fn=None):
        calls["clicks"].append(action)
        return True

    monkeypatch.setattr(mod, "enter_detail", fake_enter)
    monkeypatch.setattr(mod, "open_standalone_detail", fake_standalone)
    monkeypatch.setattr(mod, "find_comm_button", fake_btn)
    monkeypatch.setattr(mod, "find_input", fake_input)
    monkeypatch.setattr(mod, "click_with_retry", fake_click)
    monkeypatch.setattr(mod, "human_read", lambda *a, **k: None)
    monkeypatch.setattr(mod, "shot", lambda *a, **k: None)
    monkeypatch.setattr(mod, "detect_risk_signal", lambda page: None)

    def _cont(page):
        # 可传常值或 callable(page)——起草前的已沟通检测（L936）与本分支的失败复查
        # 共用这个 seam，序列场景用 callable 区分先后。
        return continue_btn(page) if callable(continue_btn) else continue_btn

    monkeypatch.setattr(mod, "find_continue_button", _cont)
    monkeypatch.setattr(mod, "grab_detail", lambda page, d, **kw: "JD 全文：负责测试")
    monkeypatch.setattr(mod, "wait_reply", lambda *a, **k: "您好，我有7年Solidity经验。")
    monkeypatch.setattr(mod, "read_reply", lambda *a, **k: {"level": "中", "notes": "§1 链上"})
    monkeypatch.setattr(mod, "safe_go_back", lambda *a, **k: None)
    monkeypatch.setattr(mod, "find_send_button", lambda page, **kw: SimpleNamespace())
    monkeypatch.setattr(mod, "log_apply", lambda *a, **k: None)
    monkeypatch.setattr(mod, "record_history", lambda *a, **k: None)
    monkeypatch.setattr(mod, "record_greeting", lambda *a, **k: "added")  # #64 起返回结局字符串
    monkeypatch.setattr(mod.g, "load_whitelist_file", lambda path: None)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    return calls


def _run_send(tmp_path, monkeypatch, page, btn_seq, input_seq, continue_btn=None):
    calls = _send_mocks(monkeypatch, btn_seq, input_seq, continue_btn=continue_btn)
    platform = SimpleNamespace(
        login_dialog_selector="#login-dialog",
        save_evidence=lambda p, tag, job=None: calls["evidence"].append(tag),
    )
    pacer = SimpleNamespace(wait_skip=lambda: None, wait_sent=lambda: None)
    results, state = [], {"applied": 0, "direct_fallback": 0, "keyword_skips": []}
    job = {"jobName": "测试岗", "brandName": "测试公司", "encryptJobId": "e1"}
    outcome = mod.apply_live(page, job, tmp_path, 1, tmp_path, {"paste_prob": 1.0}, pacer, platform, results, state)
    return outcome, results, state, calls


def test_apply_live_falls_back_to_standalone_detail_when_btn_misses(tmp_path, monkeypatch):
    """#41→#33 第八轮核心场景（真实冒烟 12/14/15 号岗）：首查落空 → 直跳独立详情页兜底一次 →
    命中则继续原 click 链路完成投递，不再直接 no_btn。"""
    page = _SendPage(_LIST_URL)
    outcome, results, state, calls = _run_send(tmp_path, monkeypatch, page, [None, SimpleNamespace()], [_FakeInputEl()])
    assert calls["enter"] == 1, "step1 首次进详情（卡片优先）"
    assert calls["standalone"] == ["e1"], "首查落空 → 直跳本岗独立详情页兜底一次"
    assert calls["clicks"] == ["立即沟通按钮", "发送招呼按钮"], "兜底命中 → 走原 click 链路（沟通+发送两跳）"
    assert [r["status"] for r in results] == ["applied"]
    assert calls["evidence"] == [], "兜底命中不产生存证"


def test_apply_live_no_btn_when_fallback_still_misses(tmp_path, monkeypatch):
    """兜底后仍落空 → 才走 no_btn 存证跳过（#43 修复后存证 URL 如实反映最终位置）。"""
    page = _SendPage(_LIST_URL)
    outcome, results, state, calls = _run_send(tmp_path, monkeypatch, page, [None, None], [])
    assert calls["enter"] == 1, "step1 首次进详情"
    assert calls["standalone"] == ["e1"], "兜底恰好一次，不循环"
    assert [r["status"] for r in results] == ["no_btn"]
    assert calls["evidence"] == ["live-no-comm-1"]


def test_apply_live_click_timeout_but_button_flipped_records_applied(tmp_path, monkeypatch):
    """#44 台账防漏记（09-23 实证 2/5 漏记）：actionability 报超时但点击事件已派发，
    按钮随即变「继续沟通」⇒ 按已发送补记台账（code=None → -1），不再记 no_btn、
    不进连续失败计数；存证照留（诊断需要）。"""
    page = _SendPage(_LIST_URL)
    seen = {"n": 0}

    def continue_after_click(page):
        # 第 1 次调用 = 起草前的已沟通检测（须为 None，否则本岗到不了点击段）；
        # 第 2 次 = 点击失败后的复查（命中 ⇒ 补记）。
        seen["n"] += 1
        return SimpleNamespace() if seen["n"] >= 2 else None

    outcome, results, state, calls = _run_send(
        tmp_path, monkeypatch, page, [None, None], [], continue_btn=continue_after_click
    )
    assert seen["n"] == 2, "复查发生在点击失败之后"
    assert [r["status"] for r in results] == ["applied"], "补记为 applied 而非 no_btn"
    assert results[0].get("note"), "补记须带 note 说明误判原因（可审计）"
    assert results[0]["code"] is None and state["applied"] == 1
    assert state.get("consec_no_btn", 0) == 0, "动作实际生效，连续失败计数不得累加"
    assert calls["evidence"] == ["live-no-comm-1"], "失败存证仍要留（取证不因补记省略）"


def test_apply_live_no_reentry_when_already_on_detail(tmp_path, monkeypatch):
    """#41 反向守卫：仍在详情页时按钮落空是 DOM 漂移，不重进（防循环/误点别岗），直接 no_btn。"""
    page = _SendPage(_DETAIL_URL)
    outcome, results, state, calls = _run_send(tmp_path, monkeypatch, page, [None], [])
    assert calls["enter"] == 1, "只有 step1 首次进详情，详情页内落空不触发重进"
    assert [r["status"] for r in results] == ["no_btn"]


# --- #42：聊天页输入框加载态轮询重试（点击沟通成功但输入框延后就绪，不再一查落空弃草稿） ---


def test_apply_live_find_input_retries_until_ready(tmp_path, monkeypatch):
    """#42 核心场景（真实冒烟 18 号岗）：前两次落空（聊天页加载态）第三次命中 →
    继续走发送，已过 gate 的草稿不再沉没。"""
    page = _SendPage(_DETAIL_URL)
    outcome, results, state, calls = _run_send(
        tmp_path, monkeypatch, page, [SimpleNamespace()], [None, None, _FakeInputEl()]
    )
    assert calls["find_input"] == 3, "首查 + 2 次重试"
    assert [r["status"] for r in results] == ["applied"], "输入框就绪后走完发送"
    assert state["applied"] == 1


def test_apply_live_find_input_retries_exhausted_no_input(tmp_path, monkeypatch):
    """#42：重试耗尽仍落空 → 既有 no_input 存证兜底不变，不崩整轮。"""
    page = _SendPage(_DETAIL_URL)
    outcome, results, state, calls = _run_send(tmp_path, monkeypatch, page, [SimpleNamespace()], [None])
    assert calls["find_input"] == mod.FIND_INPUT_ATTEMPTS, "重试上限由常量约束"
    assert [r["status"] for r in results] == ["no_input"]
    assert calls["evidence"] == ["live-no-input-1"]


def test_apply_live_find_input_immediate_hit_no_retry(tmp_path, monkeypatch):
    """#42 反向守卫：输入框立即可见时首查命中，行为与原先一致。"""
    page = _SendPage(_DETAIL_URL)
    outcome, results, state, calls = _run_send(tmp_path, monkeypatch, page, [SimpleNamespace()], [_FakeInputEl()])
    assert calls["find_input"] == 1
    assert [r["status"] for r in results] == ["applied"]


# --- #39 拆分接缝契约（LiveSessionContext / _JobFlow / 轮次段边界） ---
#
# 这些行为原先埋在 apply_live（257 行）与 run_live（188 行）内部，只能靠读完全文保证
# 一致；拆成接缝后逐个钉住——重点不是「函数被调用过」，而是那些漏掉就静默出错的
# 不变式：跳过分支的三件套、停批信号的传递、关键词失败的转轮。


def _fake_session(tmp_path, cfg=None, state=None):
    """会话上下文替身：pacer / platform 用可计数的桩，results 每次新建。"""
    calls = {"skip": 0, "sent": 0, "evidence": []}
    pacer = SimpleNamespace(
        wait_skip=lambda: calls.__setitem__("skip", calls["skip"] + 1),
        wait_sent=lambda: calls.__setitem__("sent", calls["sent"] + 1),
    )
    platform = SimpleNamespace(save_evidence=lambda p, tag, job=None: calls["evidence"].append(tag))
    session = mod.LiveSessionContext(
        session_dir=tmp_path,
        shots_dir=tmp_path,
        cfg={"quiet_hours": (0, 7)} if cfg is None else cfg,
        pacer=pacer,
        platform=platform,
        results=[],
        state={"applied": 0, "direct_fallback": 0, "keyword_skips": []} if state is None else state,
    )
    return session, calls


def _job_flow(session, idx=3, job=None):
    return mod._JobFlow(page="P", job={"jobName": "岗位"} if job is None else job, idx=idx, ctx=session)


def test_skip_job_records_paces_and_goes_back(tmp_path, monkeypatch):
    """#39 接缝：8 个跳过分支共用「记账 + 节奏跳过 + 回列表」三件套。
    漏掉 safe_go_back 会让下一岗从错误页面开始（旧实现里同样三行散落在 8 处，
    只能靠人眼保证一致）——这条测试把三件套钉成契约。"""
    session, calls = _fake_session(tmp_path)
    backs = []
    monkeypatch.setattr(mod, "safe_go_back", lambda page: backs.append(page))
    assert mod._skip_job(_job_flow(session), "no_jd") == "next"
    assert session.results == [{"idx": 3, "job": "岗位", "status": "no_jd"}]
    assert calls["skip"] == 1, "跳过岗走 wait_skip（不计入发送节奏）"
    assert calls["sent"] == 0
    assert backs == ["P"], "回列表由接缝统一负责"


def test_skip_job_extra_fields_appended_after_status(tmp_path, monkeypatch):
    """额外字段（reason/detail）追加在 status 之后——report.json 的键序与旧实现逐行可比。"""
    session, _ = _fake_session(tmp_path)
    monkeypatch.setattr(mod, "safe_go_back", lambda page: None)
    mod._skip_job(_job_flow(session, idx=1), "agent_skip", reason="hard_lang")
    assert session.results[0] == {"idx": 1, "job": "岗位", "status": "agent_skip", "reason": "hard_lang"}


def test_safe_go_back_on_split_view_list_does_not_pop_history():
    """#44：分栏视图卡片点击不跳页，页面已经在搜索结果页时绝对不能 go_back。
    调用 go_back 会把搜索结果弹出历史记录、退回到自检页或空白页，导致后续全部直跳。"""
    actions = []

    class DummyPage:
        url = "https://www.zhipin.com/web/geek/jobs?query=Solidity&page=1"
        _autojob_list_url = "https://www.zhipin.com/web/geek/jobs?query=Solidity&page=1"

        def go_back(self, **kw):
            actions.append("go_back")

        def goto(self, url, **kw):
            actions.append(f"goto:{url}")

    page = DummyPage()
    mod.safe_go_back(page)
    assert actions == [], "已经在分栏列表页，不得触发任何导航"


def test_safe_go_back_from_detail_or_chat_restores_list():
    """#44：偏离列表页（直跳详情 / 聊天页）后，优先 goto 回记录的列表页。"""
    actions = []

    class DummyPage:
        def __init__(self, url):
            self.url = url
            self._autojob_list_url = "https://www.zhipin.com/web/geek/jobs?query=Solidity&page=1"

        def go_back(self, **kw):
            actions.append("go_back")

        def goto(self, url, **kw):
            actions.append(f"goto:{url}")

    # 1. 从独立详情页返回
    page = DummyPage("https://www.zhipin.com/job_detail/xyz.html")
    mod.safe_go_back(page)
    assert actions == ["goto:https://www.zhipin.com/web/geek/jobs?query=Solidity&page=1"]

    # 2. 从聊天页返回
    actions.clear()
    page = DummyPage("https://www.zhipin.com/web/geek/chat")
    mod.safe_go_back(page)
    assert actions == ["goto:https://www.zhipin.com/web/geek/jobs?query=Solidity&page=1"]


def test_safe_go_back_fallback_when_no_list_url():
    """#44：无 list_url 且不在列表页时，兜底执行 go_back。"""
    actions = []

    class DummyPage:
        url = "https://example.com/other"

        def go_back(self, **kw):
            actions.append("go_back")

        def goto(self, url, **kw):
            actions.append(f"goto:{url}")

    page = DummyPage()
    mod.safe_go_back(page)
    assert actions == ["go_back"]


def test_pre_job_stop_on_daily_limit_is_silent(tmp_path, monkeypatch):
    """日上限是常规停止：停整轮但不发通知（通知是给「需要人介入」的事件用的）。"""
    session, _ = _fake_session(tmp_path)
    notes = []
    monkeypatch.setattr(mod, "notify", lambda title, detail: notes.append(title))
    session.state["applied"] = 20
    assert mod._pre_job_stop(session, SimpleNamespace(limit=20)) is True
    assert notes == []


def test_pre_job_stop_on_quiet_hour_notifies(tmp_path, monkeypatch, capsys):
    """静默时段跨午夜进入时也要停（2026-09-09 23:57 实测缺口），且必须通知用户。"""
    session, _ = _fake_session(tmp_path)
    notes = []
    monkeypatch.setattr(mod, "notify", lambda title, detail: notes.append((title, detail)))
    monkeypatch.setattr(mod, "is_quiet_hour", lambda hour, quiet: True)
    assert mod._pre_job_stop(session, SimpleNamespace(limit=20)) is True
    assert notes and "静默时段" in notes[0][1]
    assert "[停] 进入静默时段" in capsys.readouterr().out


def test_pre_job_stop_on_close_sentinel(tmp_path, monkeypatch):
    """CLOSE 哨兵（用户写的优雅停止信号）优先于继续投递。"""
    session, _ = _fake_session(tmp_path)
    (tmp_path / mod.CLOSE_SENTINEL).write_text("", encoding="utf-8")
    monkeypatch.setattr(mod, "is_quiet_hour", lambda hour, quiet: False)
    assert mod._pre_job_stop(session, SimpleNamespace(limit=20)) is True


def test_pre_job_stop_continues_when_idle(tmp_path, monkeypatch):
    session, _ = _fake_session(tmp_path)
    monkeypatch.setattr(mod, "is_quiet_hour", lambda hour, quiet: False)
    assert mod._pre_job_stop(session, SimpleNamespace(limit=20)) is False


def _rounds_stubs(monkeypatch, fresh_by_query, outcome):
    """轮次段桩：keyword_round 按关键词返回岗位；_apply_one_job 返回固定 outcome；
    其余外部动作（滚动/截图/节奏/存证）替换为静默桩，只留可断言的调用记录。"""
    calls = {"queries": [], "applied": []}
    monkeypatch.setattr(mod, "is_quiet_hour", lambda hour, quiet: False)
    monkeypatch.setattr(mod, "rnd_sleep", lambda *a, **k: None)
    monkeypatch.setattr(mod, "human_scroll", lambda *a, **k: None)
    monkeypatch.setattr(mod, "shot", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_browse_list", lambda *a, **k: None)  # 外部动作，轮次组不覆盖
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(mod, "is_excluded_company", lambda brand, excluded: brand == "某基金")
    monkeypatch.setattr(
        mod,
        "keyword_round",
        lambda page, q, state, _ids, _collected=None, target=0: calls["queries"].append(q) or fresh_by_query.get(q),
    )
    monkeypatch.setattr(
        mod, "_apply_one_job", lambda page, job, idx, session: calls["applied"].append(job["brandName"]) or outcome
    )
    return calls


def _run_rounds(session, monkeypatch, queries, fresh_by_query, outcome="next", limit=20):
    calls = _rounds_stubs(monkeypatch, fresh_by_query, outcome)
    session.cfg["live_recommend"] = False  # #33：本组只覆盖搜索流轮，推荐流轮有独立用例
    run = SimpleNamespace(
        queries=queries,
        limit=limit,
        applied_ids=set(),
        excluded={"某基金"},
        collected=[],  # #33 会话级已收集列表（跨关键词轮 / 跨来源统一去重）
        session=session,
    )
    mod._execute_query_rounds("P", run)
    return calls


def test_query_rounds_stop_signal_ends_session(tmp_path, monkeypatch):
    """#39 接缝：单岗返回 "stop"（风控/封禁/每日上限）必须终结后续关键词轮——
    否则封禁后还会接着搜下一个词，风控面越滚越大。"""
    session, _ = _fake_session(tmp_path)
    calls = _run_rounds(session, monkeypatch, ["a", "b"], {"a": [{"jobName": "岗A", "brandName": "某公司"}]}, "stop")
    assert calls["queries"] == ["a"], "stop 之后不得再开新关键词轮"


def test_query_rounds_relogin_notifies_and_stops(tmp_path, monkeypatch):
    """登录态过期：提示重登并停整轮（登录态没了，继续搜只是无效请求）。"""
    session, _ = _fake_session(tmp_path)
    notes = []
    monkeypatch.setattr(mod, "notify", lambda title, detail: notes.append(title))
    calls = _run_rounds(session, monkeypatch, ["a", "b"], {"a": [{"jobName": "岗A", "brandName": "某公司"}]}, "relogin")
    assert calls["queries"] == ["a"]
    assert "登录态过期" in notes


def test_query_rounds_keyword_failure_continues(tmp_path, monkeypatch):
    """关键词级边界：keyword_round 返回 None（已在内部记账）→ 留证后转下一关键词，不停整轮。"""
    session, scalls = _fake_session(tmp_path)
    applied = []

    def fake_keyword_round(page, q, state, _ids, _collected=None, target=0):
        if q == "a":
            state["keyword_skips"].append({"query": q, "error": "Timeout 30000ms exceeded."})
            return None
        return [{"jobName": "岗B", "brandName": "某公司"}]

    _rounds_stubs(monkeypatch, {}, "next")
    monkeypatch.setattr(mod, "keyword_round", fake_keyword_round)
    monkeypatch.setattr(mod, "_apply_one_job", lambda page, job, idx, session: applied.append("B") or "next")
    session.cfg["live_recommend"] = False  # #33：本用例只覆盖搜索流轮
    run = SimpleNamespace(
        queries=["a", "b"], limit=20, applied_ids=set(), excluded=set(), collected=[], session=session
    )
    mod._execute_query_rounds("P", run)
    assert applied == ["B"], "失败关键词不阻断后续关键词轮"
    assert [t for t in scalls["evidence"] if t.startswith("live-keyword-")], "关键词失败留现场"


def test_query_rounds_skips_excluded_company(tmp_path, monkeypatch):
    """简历出现过的公司不投递（用户约定 2026-09-10）：命中即跳过，不进 apply_live。"""
    session, calls = _fake_session(tmp_path)
    fresh = [{"encryptJobId": "e1", "jobName": "岗", "brandName": "某基金"}]
    run_calls = _run_rounds(session, monkeypatch, ["a"], {"a": fresh}, "next")
    assert run_calls["applied"] == [], "被排除公司不得进投递"
    assert [r["status"] for r in session.results] == ["excluded_company"] and calls["skip"] == 1


class _NoiseCard:
    """浏览噪声用假卡片：get_attribute/bounding_box 纯读取，无 actionability。"""

    def __init__(self, href):
        self.href = href

    def get_attribute(self, name):
        return self.href if name == "href" else None

    def bounding_box(self):
        return {"x": 100, "y": 200, "width": 300, "height": 60}


class _NoisePage:
    def __init__(self, cards):
        self.url = "https://www.zhipin.com/web/geek/jobs?query=x"
        self._cards = cards
        self.moves = []
        self.clicks = []
        self.gotos = []
        self.mouse = self._Mouse(self)

    def query_selector_all(self, sel):
        return self._cards

    def evaluate(self, js, *args):
        if "innerWidth" in js:
            return {"w": 1200, "h": 800}
        return None

    def goto(self, url, **kw):
        self.gotos.append(url)

    class _Mouse:
        def __init__(self, owner):
            self._owner = owner

        def move(self, x, y):
            self._owner.moves.append((x, y))

        def click(self, x, y):
            self._owner.clicks.append((x, y))


def test_browse_list_noise_excludes_candidate_pool(monkeypatch):
    """#44 复盘（用户质询）：浏览噪声点开的卡片必须排除本轮候选池——
    原实现 random.choice 全池随机，可能点开马上要投的岗位污染其状态；
    且点击走 coordinate_click（handle.click 会挂起）、回列表走 goto 复位。"""
    monkeypatch.setattr(mod, "human_scroll", lambda *a, **k: None)
    monkeypatch.setattr(mod, "shot", lambda *a, **k: None)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(mod.random, "random", lambda: 0.0)  # 必触发噪声（<0.25）

    cand = _NoiseCard("/job_detail/e1.html")
    noise = _NoiseCard("/job_detail/e9.html")
    page = _NoisePage([cand, noise])
    mod._browse_list(page, SimpleNamespace(shots_dir="."), "q", exclude_ids={"e1"})
    assert page.clicks, "噪声卡应被坐标点击"
    assert page.gotos == [page.url], "点开必须 goto 复位回列表（不依赖 go_back）"

    # 全部卡片都在候选池 → 无卡可挑，宁可不拟人也不污染
    page2 = _NoisePage([cand])
    mod._browse_list(page2, SimpleNamespace(shots_dir="."), "q", exclude_ids={"e1"})
    assert not page2.clicks and not page2.gotos


class _FillBoom:
    """粘贴失败替身：fill 抛异常（个别输入框不兼容 fill）。"""

    def fill(self, text):
        raise RuntimeError("fill not supported")


def test_type_greeting_typed_branch_when_paste_prob_zero(monkeypatch):
    """#39 接缝：paste_prob=0 走逐字键入（行为混合分支），不调 fill。"""
    typed = []
    monkeypatch.setattr(mod, "human_type", lambda page, text, **kw: typed.append((text, kw)))
    monkeypatch.setattr(mod.random, "random", lambda: 0.99)
    el = _FakeInputEl()
    mod._type_greeting(None, el, "草稿", {"paste_prob": 0.0, "typo_prob": 0.05})
    assert el.filled is None and typed == [("草稿", {"delay_lo": 40, "delay_hi": 120, "typo_prob": 0.05})]


def test_type_greeting_falls_back_to_typing_when_fill_raises(monkeypatch, capsys):
    """#39 接缝：fill 不兼容时回退逐字键入——草稿不因输入框差异而丢。"""
    typed = []
    monkeypatch.setattr(mod, "human_type", lambda page, text, **kw: typed.append(text))
    monkeypatch.setattr(mod.random, "random", lambda: 0.0)
    mod._type_greeting(None, _FillBoom(), "草稿", {"paste_prob": 1.0})
    assert typed == ["草稿"]
    assert "回退逐字键入" in capsys.readouterr().out


def test_write_session_report_persists_report_json(tmp_path, monkeypatch):
    """#39 接缝：report.json 落盘含 dry_run / keyword_skips / results，EDD 快照挂 edd 字段——
    原先埋在 run_live 尾部，没有直接测试。"""
    state = {"applied": 2, "direct_fallback": 1, "keyword_skips": [{"query": "q", "error": "e"}], "dry_run": True}
    session, _ = _fake_session(tmp_path, state=state)
    session.results.append({"idx": 1, "job": "岗", "status": "applied"})
    monkeypatch.setattr(mod, "edd_wrapup", lambda paths, profile, now=None: {"trend_appended": False, "reason": "none"})
    monkeypatch.setattr(mod.g, "resolve_paths", lambda env=None: {"name": "p"})
    run = SimpleNamespace(queries=["q"], env={}, session=session)
    mod._write_session_report(run)
    data = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert data["applied"] == 2 and data["dry_run"] is True
    assert data["keyword_skips"] == state["keyword_skips"] and data["results"] == session.results
    assert data["edd"] == {"trend_appended": False, "reason": "none"}


def test_write_session_report_done_line_buckets_skip_and_fail(tmp_path, monkeypatch, capsys):
    """#47：收尾 [done] 行分桶播报——「未投（跳过/拦停）」与「失败」分开报，
    不得再混成一批让用户读成「好多投递失败」。"""
    state = {"applied": 1, "direct_fallback": 0, "keyword_skips": []}
    session, _ = _fake_session(tmp_path, state=state)
    session.results.extend(
        [
            {"idx": 1, "job": "岗", "status": "applied"},
            {"idx": 2, "job": "岗2", "status": "agent_skip", "reason": "noise"},
            {"idx": 3, "job": "岗3", "status": "no_btn"},
        ]
    )
    monkeypatch.setattr(mod, "edd_wrapup", lambda paths, profile, now=None: {"trend_appended": False, "reason": "none"})
    monkeypatch.setattr(mod.g, "resolve_paths", lambda env=None: {"name": "p"})
    run = SimpleNamespace(queries=["q"], env={}, session=session)
    mod._write_session_report(run)
    out = capsys.readouterr().out
    assert "未投 1（跳过/拦停）" in out and "失败 1" in out
    data = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert data["outcome_summary"] == {"applied": 1, "skipped": 1, "failed": 1}


# --- #33：推荐列表作为搜索流之外的第二岗位来源（页面 = 顶栏「职位」无 query 模式） ---


# 两批滚动加载的响应，第二批与第一批有重复（r2）——实测形态 15+15 → 28 条唯一
_RECO_BATCHES = [
    [{"encryptJobId": "r1", "jobName": "岗1"}, {"encryptJobId": "r2", "jobName": "岗2"}],
    [{"encryptJobId": "r2", "jobName": "岗2"}, {"encryptJobId": "r3", "jobName": "岗3"}],
]


class _RecoPage(_ListPage):
    """推荐列表页替身：与搜索页同型，只是端点换成 `pc/recommend/job/list.json`。"""

    def __init__(self, batches=None, has_more=None):
        super().__init__(
            batches=_RECO_BATCHES if batches is None else batches,
            url="https://www.zhipin.com/wapi/zpgeek/pc/recommend/job/list.json?page=1&pageSize=15",
            has_more=has_more,
        )


def _quiet_reco(monkeypatch):
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    _quiet_scroll(monkeypatch, patch_sleep=False)


def test_recommend_list_accumulates_scroll_batches_and_dedupes(monkeypatch):
    """#33 最关键实现点：**每一批滚动响应都要留下**并按 eid 去重。
    覆盖式 capture_xhr 在后一批响应时会冲掉前一批（15 条白拿），故必须累积式；
    同时要真的滚动——只吃第一批正是此前「池子只有 28 条」假象的来源。"""
    _quiet_reco(monkeypatch)
    page = _RecoPage()
    listing = mod.recommend_list(page)
    assert [j["encryptJobId"] for j in listing] == ["r1", "r2", "r3"], "跨批合并去重且保序"
    assert page.gotos == [mod.LIVE_RECOMMEND_URL]
    assert page.scrolls >= 2, "第二批是靠滚动拿到的（不是只取第一批）"
    assert page.listeners == [], "监听器注销"


def test_recommend_list_honours_cap(monkeypatch):
    """LIVE_COLLECT_CAP 是单来源采集上限：超出的截断（票面验收 4）。"""
    _quiet_reco(monkeypatch)
    assert [j["encryptJobId"] for j in mod.recommend_list(_RecoPage(), limit=2)] == ["r1", "r2"]


def test_recommend_list_cleans_listener_on_navigation_failure(monkeypatch):
    """导航失败时同样注销监听器（#35 搜索流的教训：失败路径漏注销会泄漏到下一轮）。"""
    _quiet_reco(monkeypatch)

    def _boom(*_a, **_k):
        raise RuntimeError("Timeout 30000ms exceeded.")

    page = _RecoPage()
    monkeypatch.setattr(mod, "goto_with_retry", _boom)
    with pytest.raises(RuntimeError, match="Timeout"):
        mod.recommend_list(page)
    assert page.listeners == []


def test_recommend_list_empty_when_no_xhr(monkeypatch):
    """零命中返回空列表而非异常：页面改版/端点消失时退化为「本轮无推荐岗位」，不崩会话。"""
    _quiet_reco(monkeypatch)
    ticks = itertools.count(0, 30)
    monkeypatch.setattr(mod.time, "time", lambda: next(ticks))
    assert mod.recommend_list(_RecoPage(batches=[])) == []


def test_recommend_list_stops_at_has_more_false(monkeypatch):
    """到底哨兵优先：响应带 hasMore=false 时一次都不滚（页面自己说没了，不必滚满轮次）。"""
    _quiet_reco(monkeypatch)
    page = _RecoPage(has_more=False)
    mod.recommend_list(page)
    assert page.scrolls == 0, "hasMore=false 时不该再滚"


def test_dedupe_jobs_drops_entries_without_id_and_non_dict():
    """去重原语：无 id 项与非 dict 条目（列表形态漂移）都丢弃，不污染后续投递。"""
    out = mod.dedupe_jobs(
        [
            {"encryptJobId": "a"},
            "not-a-dict",
            {"jobName": "无 id"},
            {"encryptJobId": "a"},
            {"encryptJobId": "b"},
            {"encryptJobId": None},
        ]
    )
    assert [j["encryptJobId"] for j in out] == ["a", "b"]


def test_keyword_round_shares_collected_across_rounds(monkeypatch):
    """票面验收 6（来源无关去重）：会话级 collected 跨关键词轮共用——第二轮拿到的
    同一批岗位不再重复返回。推荐流与搜索流之间同样由这一个列表兜住，而非各自去重。"""
    _quiet_search(monkeypatch)
    collected = []
    state = {"applied": 0, "direct_fallback": 0, "keyword_skips": []}
    first = mod.keyword_round(_SearchPage(), "AI", state, set(), collected)
    second = mod.keyword_round(_SearchPage(), "AI", state, set(), collected)
    assert [j["encryptJobId"] for j in first] == ["j1", "j2"]
    assert second == [], "同一批岗位第二轮不重复返回"
    assert [j["encryptJobId"] for j in collected] == ["j1", "j2"]
    assert state["keyword_skips"] == []


def test_keyword_round_without_collected_keeps_legacy_behavior(monkeypatch):
    """不传 collected 时退化为单轮内去重（#33 之前的旧行为），旧调用点不受影响。"""
    _quiet_search(monkeypatch)
    state = {"applied": 0, "direct_fallback": 0, "keyword_skips": []}
    assert len(mod.keyword_round(_SearchPage(), "AI", state, set())) == 2


def test_apply_collect_cfg_defaults_on():
    """默认 LIVE_RECOMMEND=on + 采集上限 120（票面 v2 口径：搜索与推荐并用，非二选一）。"""
    cfg = {}
    mod._apply_collect_cfg(cfg, {})
    assert cfg["live_recommend"] is True
    assert cfg["collect_cap"] == mod.COLLECT_CAP_DEFAULT


def test_apply_collect_cfg_off_and_custom_cap():
    cfg = {}
    mod._apply_collect_cfg(cfg, {"LIVE_RECOMMEND": "off", "LIVE_COLLECT_CAP": "12"})
    assert cfg["live_recommend"] is False
    assert cfg["collect_cap"] == 12


def test_apply_collect_cfg_zero_means_unlimited():
    """0 = 不限（**不是**「不采集」）——0 档只受滚动轮次与到底判据约束，横幅文案要说清，
    否则读报告的人会把策略读反。"""
    cfg = {}
    mod._apply_collect_cfg(cfg, {"LIVE_COLLECT_CAP": "0"})
    assert cfg["collect_cap"] == 0
    assert "不限量" in mod._collect_banner_desc(cfg)


def test_apply_collect_cfg_accepts_legacy_env_with_notice(capsys):
    """旧名 LIVE_RECOMMEND_PER_ROUND 继续生效并提示——既有环境变量不静默失效。"""
    cfg = {}
    mod._apply_collect_cfg(cfg, {"LIVE_RECOMMEND_PER_ROUND": "45"})
    assert cfg["collect_cap"] == 45
    assert "已并入 LIVE_COLLECT_CAP" in capsys.readouterr().out


def test_apply_collect_cfg_invalid_cap_falls_back(capsys):
    """非法上限值回退默认并提示——写错的配置不中断会话（同 load_config 既有风格）。"""
    cfg = {}
    mod._apply_collect_cfg(cfg, {"LIVE_COLLECT_CAP": "abc"})
    assert cfg["collect_cap"] == mod.COLLECT_CAP_DEFAULT
    assert "不是整数" in capsys.readouterr().out


# --- #65 推荐轮消费配额（管「吃多少个」，与上面的采集上限分家）---


def test_apply_recommend_cap_cfg_defaults():
    """默认消费配额取 `RECOMMEND_CAP_DEFAULT`（票面定 10）——这里断言指针而不是字面量，
    数值真源只有一份（同 `test_apply_collect_cfg_defaults_on` 的口径）。"""
    cfg = {}
    mod._apply_recommend_cap_cfg(cfg, {})
    assert cfg["recommend_cap"] == mod.RECOMMEND_CAP_DEFAULT


def test_apply_recommend_cap_cfg_custom_and_zero():
    """自定义值直取；0 是**合法值**（= 不限，沿用 LIVE_COLLECT_CAP 的 0 语义），不是回退。"""
    cfg = {}
    mod._apply_recommend_cap_cfg(cfg, {"LIVE_RECOMMEND_CAP": "4"})
    assert cfg["recommend_cap"] == 4
    cfg = {}
    mod._apply_recommend_cap_cfg(cfg, {"LIVE_RECOMMEND_CAP": "0"})
    assert cfg["recommend_cap"] == 0, "0 被回退成默认就等于把「放开」读成「更狠地截」"


@pytest.mark.parametrize("raw,keyword", [("abc", "不是整数"), ("-3", "为负")])
def test_apply_recommend_cap_cfg_invalid_falls_back(capsys, raw, keyword):
    """#65 验收 2：非整数 / 负数各回退默认并打印一行，口径逐项照 `_apply_collect_cfg`。"""
    cfg = {}
    mod._apply_recommend_cap_cfg(cfg, {"LIVE_RECOMMEND_CAP": raw})
    assert cfg["recommend_cap"] == mod.RECOMMEND_CAP_DEFAULT
    out = capsys.readouterr().out
    assert keyword in out and str(mod.RECOMMEND_CAP_DEFAULT) in out, "回退了却不吭声 = 静默改配置"


def test_collect_banner_desc_marks_off_and_cap():
    """横幅口径：off 时明说「推荐流 off（纯搜索）」，看报告的人不必反推开关状态。"""
    assert "推荐流 off" in mod._collect_banner_desc({"live_recommend": False})
    assert "采集上限 20" in mod._collect_banner_desc({"live_recommend": True, "collect_cap": 20})


def test_collect_target_scales_with_remaining_and_cap():
    """采集目标 = 剩余额度 × COLLECT_SLACK（留余量给 gate/去重/公司过滤），受上限封顶；
    额度已满返回 0（调用方据此不再采集）。"""
    run = SimpleNamespace(session=SimpleNamespace(state={"applied": 10}), limit=20)
    assert mod._collect_target(run, 0) == 10 * mod.COLLECT_SLACK
    assert mod._collect_target(run, 8) == 8, "上限更小时以上限为准"
    run.session.state["applied"] = 20
    assert mod._collect_target(run, 0) == 0, "额度已满不再采集"


def test_scroll_collect_keeps_scrolling_until_target(monkeypatch):
    """够量即停：target 达标后不再多滚（每多滚一轮就多一次页面加载与风控暴露面）。"""
    _quiet_scroll(monkeypatch)
    page = _SearchPage(batches=[[{"encryptJobId": f"p{i}"}] for i in range(5)])
    buf, flags = [], {}
    mod.capture_xhr_accum(page, mod.SEARCH_XHR_FRAG, buf, mod.JOB_LIST_KEY, flags)
    page.goto("about:blank")
    mod.scroll_collect(page, buf, target=3, flags=flags)
    assert len(buf) >= 3
    assert flags["rounds"] >= 1
    assert flags["at_bottom"] is False, "是够量停的，不是到底"


def test_scroll_collect_marks_bottom_after_idle_rounds(monkeypatch):
    """批次用尽后连续 N 轮零新增即判定到底——页面没有「没有更多」文案时靠静默兜底，
    但不许无限滚（SCROLL_MAX_ROUNDS 是最后一道闸）。"""
    _quiet_scroll(monkeypatch)
    page = _SearchPage(has_more=True)  # 单批且页面不报到底 → 只能靠静默判停
    buf, flags = [], {}
    mod.capture_xhr_accum(page, mod.SEARCH_XHR_FRAG, buf, mod.JOB_LIST_KEY, flags)
    page.goto("about:blank")
    mod.scroll_collect(page, buf, flags=flags)
    assert flags["rounds"] == mod.SCROLL_IDLE_STOP
    assert flags["at_bottom"] is True


def test_scroll_collect_caps_rounds_without_sentinel(monkeypatch):
    """页面既不报到底、每轮又都有新增时，以 SCROLL_MAX_ROUNDS 收口——防跑飞。"""
    _quiet_scroll(monkeypatch)
    monkeypatch.setattr(mod, "SCROLL_MAX_ROUNDS", 2)
    page = _SearchPage(batches=[[{"encryptJobId": f"p{i}"}] for i in range(9)])
    buf, flags = [], {}
    mod.capture_xhr_accum(page, mod.SEARCH_XHR_FRAG, buf, mod.JOB_LIST_KEY, flags)
    page.goto("about:blank")
    mod.scroll_collect(page, buf, flags=flags)
    assert flags["rounds"] == 2


def test_scroll_collect_target_counts_unique_not_raw_len(monkeypatch):
    """#44 回归：采集目标按**唯一岗位数**比对，不是原始 buf 长度。

    列表页初始化会并发拉两个频道、返回的是**同一批** 15 条 ⇒ 原始 buf=30、唯一仅 15。
    旧口径 `len(buf) >= target` 会在首批即退（滚 0 轮）——两次验收实跑日志
    「拿到 15 条（滚 **0** 轮，到底=False）」即此 bug，滚动采集等于从未启动。

    构造判据：取 target 落在「唯一数 < target ≤ 原始数」区间内，旧口径必然判「已够」
    而滚 0 轮，新口径必然继续滚。"""
    _quiet_scroll(monkeypatch)
    page = _SearchPage(batches=[[{"encryptJobId": f"n{i}"}] for i in range(8)])
    buf, flags = [], {}
    mod.capture_xhr_accum(page, mod.SEARCH_XHR_FRAG, buf, mod.JOB_LIST_KEY, flags)
    page.goto("about:blank")
    dup = [{"encryptJobId": f"d{i}"} for i in range(15)]
    buf.extend(dup)
    buf.extend(dup)  # 两个频道返回同一批 → 原始翻倍、唯一不变
    raw, uniq = len(buf), mod._unique_count(buf)
    assert uniq < raw, "夹具前提：存在跨频道重复"
    target = uniq + 3
    assert target <= raw, "旧口径（len(buf)>=target）在此判『已够』→ 滚 0 轮"
    mod.scroll_collect(page, buf, target=target, flags=flags)
    assert flags["rounds"] >= 1, "唯一数没够就该继续滚（旧口径此处 rounds=0）"


def _stop_session(monkeypatch, tmp_path, **state):
    """#44 熔断用例夹具：复用既有 `_fake_session`（带 `session_dir`，末尾 CLOSE 哨兵检查要用），
    并屏蔽静默时段——用例跑在凌晨会被静默时段恒真干扰。"""
    monkeypatch.setattr(mod, "is_quiet_hour", lambda *a, **k: False)
    # 隔离桩（2026-09-24 22:38 实证泄漏）：夹具触达阈值即真熔断——未打桩时 pytest 会
    # 真弹系统通知、并把 fixture 事件写进真实 private/data/app.log（当晚主目录冒烟
    # 即发生，用户收到「熔断」通知险些误判为真实会话）。日志重定向 tmp + notify 吞掉。
    monkeypatch.setenv("AUTOJOB_LOG_PATH", str(tmp_path / "app.log"))
    monkeypatch.setattr(mod, "notify", lambda *a, **k: None)
    session, _ = _fake_session(tmp_path)
    session.state.update(state)
    return session, SimpleNamespace(limit=40, session=session)


def test_pre_job_stop_halts_on_consecutive_click_failures(monkeypatch, tmp_path):
    """#44：连续 N 次「立即沟通」点击失败即停批——页面正常、按钮在场，但就是点不动。

    2026-09-23 第二次验收实跑 16 岗 applied=0、卡片点击 0/16，全程无风控信号，
    只有跑完看 report 才发现；这道闸在阈值处就停，不白耗额度与风控暴露面。"""
    session, run = _stop_session(monkeypatch, tmp_path, consec_no_btn=mod.CLICK_FAIL_STOP - 1, detail_entered=9)
    assert mod._pre_job_stop(session, run) is False, "未达阈值不熔断"
    session, run = _stop_session(monkeypatch, tmp_path, consec_no_btn=mod.CLICK_FAIL_STOP, detail_entered=9)
    assert mod._pre_job_stop(session, run) is True, "达阈值即停批"


def test_pre_job_stop_circuit_break_logs_event(monkeypatch, tmp_path):
    """#44 熔断必须落机读日志（用户 2026-09-24 要求）：通知会过、控制台会滚屏，
    事件日志是唯一可回溯「当时计数与原因」的地方。两道动作熔断各自落一条。
    #48：level 须为 error——默认 ERROR 阈值下也要落盘（warning 会被静默丢弃，
    2026-09-24 80% 熔断告警零持久化的根因）。"""
    events = []
    monkeypatch.setattr(mod._logging, "log_event", lambda event, **fields: events.append((event, fields)))
    monkeypatch.setattr(mod, "notify", lambda *a, **k: None)  # 测试不发系统通知（#48 顺带补桩）

    session, run = _stop_session(monkeypatch, tmp_path, direct_fallback=8, detail_entered=10)
    assert mod._pre_job_stop(session, run) is True
    assert events and events[0][0] == "circuit_break"
    fields = events[0][1]
    assert fields["breaker"] == "direct_fallback"
    assert fields["rate"] == 0.8 and fields["detail_entered"] == 10
    assert fields["level"] == "error", "熔断事件须 error 级（默认 ERROR 阈值下也落盘，#48）"

    events.clear()
    session, run = _stop_session(monkeypatch, tmp_path, consec_no_btn=mod.CLICK_FAIL_STOP, detail_entered=9)
    assert mod._pre_job_stop(session, run) is True
    assert events and events[0][0] == "circuit_break"
    fields = events[0][1]
    assert fields["breaker"] == "consec_no_btn" and fields["count"] == mod.CLICK_FAIL_STOP
    assert fields["level"] == "error"


def test_circuit_break_event_persists_by_default(tmp_path, monkeypatch):
    """#48：默认 env（无 STRUCTURED_LOG/LOG_LEVEL）下熔断事件即落 app.log。
    2026-09-24 实证：warning 级被默认 ERROR 阈值丢弃——80% 熔断告警弹了通知、
    滚了屏，事后却没有任何日志可查（app.log 未创建、report.json 未写）。"""
    p = tmp_path / "app.log"
    monkeypatch.setenv("AUTOJOB_LOG_PATH", str(p))
    monkeypatch.delenv("AUTOJOB_STRUCTURED_LOG", raising=False)
    monkeypatch.delenv("AUTOJOB_LOG_LEVEL", raising=False)
    monkeypatch.setattr(mod, "notify", lambda *a, **k: None)
    session, run = _stop_session(monkeypatch, tmp_path, direct_fallback=8, detail_entered=10)
    assert mod._pre_job_stop(session, run) is True
    lines = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert [r["event"] for r in lines] == ["circuit_break"]
    assert lines[0]["level"] == "ERROR"
    assert lines[0]["breaker"] == "direct_fallback" and lines[0]["rate"] == 0.8


def test_safety_events_pinned_to_error_level():
    """#48 源码钉：risk_blocked/apply_paused 事件点必须 level="error"——行为测试只
    覆盖 circuit_break 两道闸，这里钉住其余安全事件点不被改回 warning 静默丢弃。
    非安全事件（gate_blocked/greeting_applied 等）不在本钉范围内。"""
    src = (Path(__file__).resolve().parent / "live_apply.py").read_text(encoding="utf-8")
    for event in ("risk_blocked", "circuit_break", "apply_paused"):
        blocks = re.findall(rf'_logging\.log_event\(\s*"{event}",(.*?)\)', src, re.S)
        assert blocks, f"{event} 事件点缺失"
        for b in blocks:
            assert 'level="error"' in b, f'{event} 须 level="error"（#48：默认 ERROR 阈值下也要落盘）'


def test_pre_job_stop_halts_on_high_direct_fallback_rate(monkeypatch, tmp_path):
    """#44：卡片点击大面积失败（直跳回退占比过高）即停——入口动作全线失效的信号。
    样本量不足 DIRECT_FALLBACK_MIN 时不许熔断，防前两岗偶发就误杀整场会话。"""
    session, run = _stop_session(monkeypatch, tmp_path, direct_fallback=3, detail_entered=3)
    assert mod._pre_job_stop(session, run) is False, "样本量不足不熔断"
    session, run = _stop_session(monkeypatch, tmp_path, direct_fallback=8, detail_entered=10)
    assert mod._pre_job_stop(session, run) is True, "10 次进详情 8 次靠直跳 → 熔断"


def test_pre_job_stop_ignores_direct_no_card_false_positive(monkeypatch, tmp_path):
    """#50：无卡片直跳（direct_no_card）不计入点击退化率分子与分母，消除假阳性熔断。
    10 次进详情全因推荐流/DOM 缺失无卡片直跳，真实卡片尝试为 0，不得熔断。"""
    session, run = _stop_session(
        monkeypatch,
        tmp_path,
        direct_no_card=10,
        direct_fallback=10,
        detail_entered=10,
        card_clicks=0,
        card_click_fails=0,
    )
    assert mod._pre_job_stop(session, run) is False, "无卡片直跳绝不误触熔断"


def test_pre_job_stop_mixed_card_attempts_calculates_actual_failure_rate(monkeypatch, tmp_path):
    """#50：混合场景下仅按真实卡片点击尝试计算退化率。
    - 5 次无卡直跳 + 5 次卡片点击成功：退化率 0/5 = 0%，不熔断；
    - 5 次无卡直跳 + 5 次卡片尝试（4 败 1 成）：退化率 4/5 = 80%，熔断并落盘。"""
    events = []
    monkeypatch.setattr(mod._logging, "log_event", lambda event, **fields: events.append((event, fields)))

    # 场景 1：无卡 5 次 + 成功 5 次 → 不熔断
    session1, run1 = _stop_session(
        monkeypatch,
        tmp_path,
        direct_no_card=5,
        direct_fallback=5,
        detail_entered=10,
        card_clicks=5,
        card_click_fails=0,
    )
    assert mod._pre_job_stop(session1, run1) is False, "卡片点击全部成功，不熔断"

    # 场景 2：无卡 5 次 + 4 败 1 成 → 触发熔断
    session2, run2 = _stop_session(
        monkeypatch,
        tmp_path,
        direct_no_card=5,
        direct_fallback=9,
        detail_entered=10,
        card_clicks=1,
        card_click_fails=4,
    )
    assert mod._pre_job_stop(session2, run2) is True, "卡片尝试 5 次中 4 次失败（80%）触发熔断"
    assert events and events[-1][0] == "circuit_break"
    cb_fields = events[-1][1]
    assert cb_fields["rate"] == 0.8
    assert cb_fields["card_click_fails"] == 4
    assert cb_fields["card_attempts"] == 5


def test_enter_and_inspect_job_records_fine_grained_modes(monkeypatch, tmp_path):
    """#50：_enter_and_inspect_job 细分记账契约：
    - card: card_clicks+1, detail_entered+1
    - direct_click_failed: card_click_fails+1, direct_fallback+1, detail_entered+1
    - direct_no_card: direct_no_card+1, direct_fallback+1, detail_entered+1"""
    session, _ = _fake_session(tmp_path)
    monkeypatch.setattr(mod, "shot", lambda *a, **k: None)
    monkeypatch.setattr(mod, "capture_xhr", lambda *a, **k: None)

    # 1. card 成功
    monkeypatch.setattr(mod, "enter_detail", lambda *a, **k: "card")
    flow1 = _job_flow(session, idx=1)
    mod._enter_and_inspect_job(flow1)
    st = session.state
    assert st.get("card_clicks") == 1
    assert st.get("card_click_fails", 0) == 0
    assert st.get("direct_no_card", 0) == 0
    assert st.get("direct_fallback") == 0
    assert st.get("detail_entered") == 1

    # 2. direct_click_failed
    monkeypatch.setattr(mod, "enter_detail", lambda *a, **k: "direct_click_failed")
    flow2 = _job_flow(session, idx=2)
    mod._enter_and_inspect_job(flow2)
    assert st.get("card_clicks") == 1
    assert st.get("card_click_fails") == 1
    assert st.get("direct_no_card", 0) == 0
    assert st.get("direct_fallback") == 1
    assert st.get("detail_entered") == 2

    # 3. direct_no_card
    monkeypatch.setattr(mod, "enter_detail", lambda *a, **k: "direct_no_card")
    flow3 = _job_flow(session, idx=3)
    mod._enter_and_inspect_job(flow3)
    assert st.get("card_clicks") == 1
    assert st.get("card_click_fails") == 1
    assert st.get("direct_no_card") == 1
    assert st.get("direct_fallback") == 2
    assert st.get("detail_entered") == 3


def test_breaker_notify_copy_disambiguates_click_channel(monkeypatch, tmp_path, capsys):
    """#47：熔断通知与同族终端 print 不得把「点击通道问题」播成「投递失败」——
    2026-09-24 通知「卡片点击失败率 80%」被用户读成投递失败，一天两次。
    两条文案经用户批准（issue #47 范围 3）。注意：_stop_session 夹具会把 notify 桩成吞掉，
    捕获桩必须在 _stop_session 之后 setattr（后写者覆盖）才能拿到通知内容。"""
    session, run = _stop_session(monkeypatch, tmp_path, direct_fallback=8, detail_entered=10)
    notified = []
    monkeypatch.setattr(mod, "notify", lambda t, m: notified.append((t, m)))
    assert mod._pre_job_stop(session, run) is True
    assert "卡片点击通道退化 80%（8/10）" in notified[-1][1]
    assert "非投递结果失败" in notified[-1][1]
    assert "失败率" not in notified[-1][1]
    out = capsys.readouterr().out
    assert "卡片点击通道退化 80%（8/10）" in out, "终端 print 用同口径"
    assert "失败率" not in out

    session, run = _stop_session(monkeypatch, tmp_path, consec_no_btn=mod.CLICK_FAIL_STOP, detail_entered=9)
    notified.clear()
    monkeypatch.setattr(mod, "notify", lambda t, m: notified.append((t, m)))
    assert mod._pre_job_stop(session, run) is True
    assert f"连续 {mod.CLICK_FAIL_STOP} 次「立即沟通」点击未成功" in notified[-1][1]
    assert "非投递失败" in notified[-1][1]
    assert "点击失败" not in notified[-1][1]
    out = capsys.readouterr().out
    assert "点击未成功" in out and "点击失败" not in out, "终端 print 同口径（#47）"


# --- #51：熔断/停批事件自动建 issue（收尾接线） ---


def test_breaker_events_recorded_into_state(monkeypatch, tmp_path):
    """#51：两道动作熔断触发时同步记入 state["breakers"]——收尾建票的数据源。
    事件记录与通知/机读日志解耦：建票只消费机械字段（计数/阈值/阶段）。"""
    session, run = _stop_session(monkeypatch, tmp_path, consec_no_btn=mod.CLICK_FAIL_STOP, detail_entered=9)
    assert mod._pre_job_stop(session, run) is True
    evs = session.state["breakers"]
    assert len(evs) == 1
    assert evs[0]["breaker"] == "consec_no_btn"
    assert evs[0]["count"] == mod.CLICK_FAIL_STOP and evs[0]["threshold"] == mod.CLICK_FAIL_STOP
    assert evs[0]["ts"], "事件时刻须随记录落 state"

    session, run = _stop_session(monkeypatch, tmp_path, direct_fallback=8, detail_entered=10)
    assert mod._pre_job_stop(session, run) is True
    evs = session.state["breakers"]
    assert evs[0]["breaker"] == "direct_fallback"
    assert evs[0]["fails"] == 8 and evs[0]["attempts"] == 10
    assert evs[0]["threshold"] == mod.DIRECT_FALLBACK_STOP


def test_record_breaker_appends_to_state():
    """#51 记录 helper：任意事件点统一入口——白名单外键也先记录（脱敏在渲染层兜）。"""
    session = SimpleNamespace(state={})
    mod._record_breaker(session, breaker="risk_blocked", stage="pre_click", detail="风控弹窗")
    ev = session.state["breakers"][0]
    assert ev["breaker"] == "risk_blocked" and ev["stage"] == "pre_click" and ev["detail"] == "风控弹窗"
    assert ev["ts"]


def _report_run(tmp_path, state=None, env=None, ts="20260926-000000", cfg=None):
    """#51 收尾夹具：_write_session_report 的 run 替身（#47 用例形态 + ts/reported_path）。"""
    state = dict(state or {"applied": 0, "direct_fallback": 0, "keyword_skips": []})
    session, _ = _fake_session(tmp_path, state=state)
    if cfg:
        session.cfg.update(cfg)
    return SimpleNamespace(
        queries=["q"], env=env or {}, session=session, ts=ts, reported_path=tmp_path / "reported.json"
    )


_EDD_STUB = lambda paths, profile, now=None: {"trend_appended": False, "reason": "none"}  # noqa: E731


def test_write_session_report_stores_drafts_and_prompts(tmp_path, monkeypatch, capsys):
    """#51 默认 HITL：熔断事件 → 草稿随 report.json 落盘 + 💡 提示（含确认命令），零远端动作。"""
    monkeypatch.setattr(mod, "edd_wrapup", _EDD_STUB)
    monkeypatch.setattr(mod.g, "resolve_paths", lambda env=None: {"name": "p"})
    state = {
        "applied": 0,
        "direct_fallback": 0,
        "keyword_skips": [],
        "breakers": [
            {"breaker": "direct_fallback", "rate": 0.8, "fails": 8, "attempts": 10, "threshold": 0.6, "ts": "t"}
        ],
    }
    run = _report_run(tmp_path, state=state)
    mod._write_session_report(run)
    data = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert data["issue_drafts"][0]["breaker"] == "direct_fallback", "草稿即所建（CLI 确认时所见即所投）"
    out = capsys.readouterr().out
    assert "💡 [开票提示]" in out
    assert "_issue_reporter.py --report" in out and str(tmp_path / "report.json") in out
    assert not (tmp_path / "reported.json").exists(), "默认模式不发起远端写、不记账"


def test_write_session_report_no_breakers_unchanged(tmp_path, monkeypatch, capsys):
    """无熔断事件：零提示、零 issue_drafts 键——收尾行为与 #47 基线一致。"""
    monkeypatch.setattr(mod, "edd_wrapup", _EDD_STUB)
    monkeypatch.setattr(mod.g, "resolve_paths", lambda env=None: {"name": "p"})
    run = _report_run(tmp_path)
    mod._write_session_report(run)
    data = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert "issue_drafts" not in data
    assert "开票" not in capsys.readouterr().out


def test_write_session_report_auto_mode_files(tmp_path, monkeypatch, capsys):
    """AUTOJOB_AUTO_ISSUE=1：收尾直接建票 + 记账（file_issue 桩在 reporter 层，防真远端写）。"""
    monkeypatch.setattr(mod, "edd_wrapup", _EDD_STUB)
    monkeypatch.setattr(mod.g, "resolve_paths", lambda env=None: {"name": "p"})
    monkeypatch.setattr(mod._issue_reporter, "file_issue", lambda d, **kw: "https://github.com/r/issues/99")
    state = {
        "applied": 0,
        "direct_fallback": 0,
        "keyword_skips": [],
        "breakers": [{"breaker": "consec_no_btn", "count": 3, "threshold": 3, "ts": "t"}],
    }
    run = _report_run(tmp_path, state=state, env={"AUTOJOB_AUTO_ISSUE": "1"})
    mod._write_session_report(run)
    out = capsys.readouterr().out
    assert "[开票] consec_no_btn → https://github.com/r/issues/99" in out
    reported = json.loads((tmp_path / "reported.json").read_text(encoding="utf-8"))
    assert reported["20260926-000000"]["consec_no_btn"]["url"] == "https://github.com/r/issues/99"


def test_write_session_report_filing_failure_degrades(tmp_path, monkeypatch, capsys):
    """gh 失败：[warn] 可见、report.json 照常落盘——收尾与退出码不受影响（票面验收 5）。"""
    monkeypatch.setattr(mod, "edd_wrapup", _EDD_STUB)
    monkeypatch.setattr(mod.g, "resolve_paths", lambda env=None: {"name": "p"})

    def boom(d, **kw):
        raise RuntimeError("gh not found")

    monkeypatch.setattr(mod._issue_reporter, "file_issue", boom)
    state = {
        "applied": 0,
        "direct_fallback": 0,
        "keyword_skips": [],
        "breakers": [{"breaker": "consec_no_btn", "count": 3, "threshold": 3, "ts": "t"}],
    }
    run = _report_run(tmp_path, state=state, env={"AUTOJOB_AUTO_ISSUE": "1"})
    mod._write_session_report(run)
    out = capsys.readouterr().out
    assert "[warn]" in out and "gh not found" in out
    assert (tmp_path / "report.json").exists(), "报告落盘优先于建票失败"


def test_write_session_report_dry_run_skips_filing(tmp_path, monkeypatch, capsys):
    """dry-run 会话不建票（#49 同口径）：草稿照落 report.json 供检查，不发提示命令。"""
    monkeypatch.setattr(mod, "edd_wrapup", _EDD_STUB)
    monkeypatch.setattr(mod.g, "resolve_paths", lambda env=None: {"name": "p"})
    state = {
        "applied": 0,
        "direct_fallback": 0,
        "keyword_skips": [],
        "breakers": [{"breaker": "consec_no_btn", "count": 3, "threshold": 3, "ts": "t"}],
    }
    run = _report_run(tmp_path, state=state, cfg={"dry_run": True})  # 实现读 session.cfg 的 dry_run
    mod._write_session_report(run)
    data = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert data["issue_drafts"], "草稿仍随报告落盘可检查"
    out = capsys.readouterr().out
    assert "dry-run 会话不建票" in out
    assert "💡 [开票提示]" not in out


class _PreflightCard:
    """自检卡片的元素替身，两个旋钮对应新选取逻辑读的两格：`is_visible()` 决定**选中哪张**，
    `click()` 决定**判定结果**；`clicked` 记下它是否真的被点过（本次修复的核心断言靠它）。"""

    def __init__(self, visible=True, exc=None):
        self.visible = visible
        self.exc = exc
        self.clicked = False

    def is_visible(self):
        return self.visible

    def click(self, **kw):
        self.clicked = True
        if self.exc:
            raise self.exc
        return None


class _PreflightPage:
    """自检用的列表页替身：`query_selector_all` 只认 job_detail 那一组（其余两组回空，形态同
    真页面上「首个选择器就命中一堆」），`screenshot` 只记账不落图 ⇒ 「失败有没有留现场」读得出来。
    旧替身实现的是 `query_selector`（单命中、不看可见性），与 `_preflight_visible_card` 现在读的
    API 不再是同一个，故这一族整体换掉。"""

    def __init__(self, cards, goto_exc=None):
        self.cards = list(cards)
        self.goto_exc = goto_exc
        self.shots = []

    def goto(self, *a, **k):
        if self.goto_exc:
            raise self.goto_exc

    def query_selector_all(self, sel):
        return list(self.cards) if "/job_detail/" in sel else []

    def screenshot(self, **kw):
        self.shots.append(kw.get("path"))


def test_preflight_click_check_blocks_when_card_click_times_out(monkeypatch, tmp_path, capsys):
    """#44：起飞前点不动卡片 → 不开跑。点的是无害目标（只进详情，不碰「立即沟通」）。
    失败必须同时留现场（截图）与结果条目——2026-10-09 09:28 那场 shots/ 全空，事后无法定性。"""
    card = _PreflightCard(exc=RuntimeError("Timeout 8000ms exceeded"))
    page = _PreflightPage([card])
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    session = SimpleNamespace(results=[], shots_dir=tmp_path)
    assert mod._preflight_click_check(page, session) is False
    assert session.results[-1]["status"] == "preflight_click_failed"
    assert page.shots, "自检失败要落一张存证图"


def test_preflight_click_check_passes_when_card_clickable(monkeypatch, tmp_path):
    """#44 反向：卡片点得动 → 正常开跑，且不往 results 里塞任何东西、也不留存证图。"""
    page = _PreflightPage([_PreflightCard()])
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    session = SimpleNamespace(results=[], shots_dir=tmp_path)
    assert mod._preflight_click_check(page, session) is True
    assert session.results == [], "自检通过不该留痕"
    assert page.shots == [], "通过的一路不截图（存证只属于失败现场）"


def test_preflight_click_check_does_not_block_when_self_unavailable(monkeypatch, tmp_path):
    """#44：自检自己没做成（无卡片元素）不该误杀正常会话——交给中途熔断兜底。
    「候选 0」与「候选 N 全不可见」是两种读数，前者走这条宽松分支（判定后果不变）。"""
    page = _PreflightPage([])
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    session = SimpleNamespace(results=[], shots_dir=tmp_path)
    assert mod._preflight_click_check(page, session) is True
    assert session.results == [] and page.shots == []


def test_preflight_click_check_uses_the_first_visible_card_not_the_first_card(monkeypatch, tmp_path, capsys):
    """本次修复的核心断言（2026-10-09 09:28 会话 `20261009-092824` 实测）：第一张候选是隐藏节点时
    旧写法照样选中它 → `click_with_retry` 四步全抛 `Element is not visible` → 整场不开跑、零投递。
    现在必须跳过隐藏的、用第二张可见的去点，自检正常放行。"""
    hidden, shown = _PreflightCard(visible=False), _PreflightCard()
    page = _PreflightPage([hidden, shown])
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    session = SimpleNamespace(results=[], shots_dir=tmp_path)
    assert mod._preflight_click_check(page, session) is True
    assert (hidden.clicked, shown.clicked) == (False, True), "点的是第二张，隐藏那张一次都不碰"
    assert session.results == []
    out = capsys.readouterr().out
    assert "候选 2 个 / 可见 1 个" in out, out


def test_preflight_click_check_blocks_when_every_candidate_is_invisible(monkeypatch, tmp_path, capsys):
    """全不可见 ⇒ 判定后果**不变**（返回 False + 落 `preflight_click_failed`，不是「跳过自检继续跑」），
    但日志同时给出候选数与可见数，存证图也落——「找到了但一张都不可见」从此与「一个都没找到」
    分得开，这正是 09:28 那场缺的读数。"""
    page = _PreflightPage([_PreflightCard(visible=False) for _ in range(3)])
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    session = SimpleNamespace(results=[], shots_dir=tmp_path)
    assert mod._preflight_click_check(page, session) is False
    assert session.results[-1]["status"] == "preflight_click_failed"
    assert "3" in session.results[-1]["detail"], session.results[-1]
    out = capsys.readouterr().out
    assert "候选 3 个 / 可见 0 个" in out and "全部不可见" in out, out
    assert page.shots, "全不可见同样是失败现场，必须留图"


def test_preflight_failure_screenshot_error_does_not_change_the_verdict(monkeypatch, tmp_path, capsys):
    """观测层不新增失败面（#38/#54 口径）：存证截图自身抛异常时只打一行 warn，
    判定结果与结果条目一字不变——不许「留现场失败」反过来变成「不拦」。"""

    def boom(*a, **k):
        raise RuntimeError("shots_dir 不可写")

    monkeypatch.setattr(mod, "shot", boom)
    page = _PreflightPage([_PreflightCard(exc=RuntimeError("Timeout 8000ms exceeded"))])
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    session = SimpleNamespace(results=[], shots_dir=tmp_path)
    assert mod._preflight_click_check(page, session) is False
    assert session.results[-1]["status"] == "preflight_click_failed"
    out = capsys.readouterr().out
    assert "[warn] 自检失败存证截图没落" in out, out


def test_preflight_visible_card_counts_across_all_three_selector_groups():
    """候选数取**全量**（三组选择器）：首组没命中就继续往下找，一旦某组出现可见卡就停手不查
    后面的组——健康那一路省掉多余协议往返，全不可见那一路则把三组扫完给出诚实的两个数。
    选择器顺序与旧实现一致（`a[href*='/job_detail/']` 优先），本用例只验跨组计数与挑选。"""
    hidden = _PreflightCard(visible=False)
    shown = _PreflightCard()

    class _TwoGroupPage:
        def query_selector_all(self, sel):
            if "job_detail" in sel:
                return [hidden]
            if "job-card" in sel:
                return [hidden, shown]
            return [hidden, hidden, hidden]

    card, total, visible = mod._preflight_visible_card(_TwoGroupPage())
    assert card is shown
    # 第二组里出现可见卡 ⇒ 第三组不再查：总数=1+2，可见=1
    assert (total, visible) == (3, 1), (total, visible)


def test_search_list_accumulates_scrolled_batches(monkeypatch):
    """#33 第七轮核心：搜索结果是**滚动加载**的（页码在 POST body，每批 15 条）。
    旧实现只读第一条响应 = 每个关键词永远只吃 15 条；现在滚到底累积，跨批重复由 dedupe 收口。"""
    _quiet_search(monkeypatch)
    page = _SearchPage(
        batches=[
            [{"encryptJobId": "j1"}, {"encryptJobId": "j2"}],
            [{"encryptJobId": "j2"}, {"encryptJobId": "j3"}],
        ]
    )
    listing = mod.search_list(page, "AI")
    assert [j["encryptJobId"] for j in listing] == ["j1", "j2", "j2", "j3"], "原始累积（跨批重复不在此处吞）"
    assert [j["encryptJobId"] for j in mod.collect_new_jds(listing, [], set())] == ["j1", "j2", "j3"], "去重归消费端"
    assert page.scrolls >= 1, "后续批次靠滚动拿到，不是只取第一批"
    assert page.listeners == [], "监听器注销"


def test_init_live_session_wires_collect_cfg(monkeypatch, tmp_path):
    """接线回归（#33 第七轮）：`_apply_collect_cfg` 必须在会话初始化里被调用。

    实测教训：`_apply_collect_cfg`（原 `_apply_recommend_cfg`）此前只在测试里被直接调用，
    生产路径从不接线，而 `load_config` 也不认识 `live_recommend` 键——于是
    `_recommend_turn_open` 恒为 False，**第二岗位来源在生产里从未跑过**，测试却全绿。
    本用例从会话初始化出口断言这两个键确实落进 cfg。"""
    monkeypatch.setattr(mod, "resolve_active_profile", lambda env=None: ("blockchain", ""))
    monkeypatch.setattr(mod, "profile_line", lambda name, prefix: "[profile] blockchain")
    monkeypatch.setattr(
        mod,
        "load_config",
        lambda env=None: {"quiet_hours": (0, 0), "daily_limit_fixed": True, "daily_limit": 20, "cap_jitter": 0},
    )
    monkeypatch.setattr(mod, "effective_daily_limit", lambda cfg, state_path=None: 20)
    monkeypatch.setattr(mod, "profile_sections", lambda path: None)
    monkeypatch.setattr(mod, "parse_queries", lambda env: ["AI"])
    monkeypatch.setattr(mod, "is_quiet_hour", lambda hour, quiet: False)
    monkeypatch.setattr(mod, "startup_jitter", lambda cfg: None)
    monkeypatch.setattr(mod, "SessionPacer", lambda cfg, **kw: SimpleNamespace())  # #58 T1：桩签名放开 kw
    monkeypatch.setattr(mod, "get_platform", lambda: SimpleNamespace())
    monkeypatch.setattr(mod, "load_applied_ids", lambda path: set())
    monkeypatch.setattr(mod, "load_excluded_companies", lambda path: set())
    monkeypatch.setattr(
        mod,
        "_open_session_dirs",
        lambda base, vbase, record, ts: (tmp_path / "s", tmp_path / "shots", {}),
    )
    # #56 B1 起 `_init_live_session` 会读写楔死跳岗台账（private/data/wedge_skipped.json）：
    # 本用例与那条接线用例都必须把 private_path 钉到 tmp_path，否则测试会往真实 private/ 落文件
    # （今天的旧格式取证不入账只是巧合，不是保证）。
    monkeypatch.setattr(
        mod,
        "private_path",
        lambda rel, prefix="": tmp_path / "private" / Path(rel).parent / f"{prefix}{Path(rel).name}",
    )
    shown = {}
    monkeypatch.setattr(
        mod,
        "session_banner",
        lambda queries, limit, limit_desc, applied_ids, excluded, session_dir, dry_run, recommend_desc="": shown.update(
            {"recommend_desc": recommend_desc}
        ),
    )

    run = mod._init_live_session({})
    assert run.session.cfg["live_recommend"] is True, "开关不落 cfg → 推荐来源恒不开（本次修掉的 bug）"
    assert run.session.cfg["collect_cap"] == mod.COLLECT_CAP_DEFAULT
    assert run.session.cfg["recommend_cap"] == mod.RECOMMEND_CAP_DEFAULT, "#65 配额不落 cfg → 生产恒不截断"
    assert run.session.cfg["reply_timeout"] == mod.REPLY_TIMEOUT_DEFAULT, (
        "起草窗口不落 cfg → 消费点回落到另一份数（两处默认值漂开的老毛病）"
    )
    assert "采集上限" in shown["recommend_desc"], "横幅口径与 cfg 同源"


def test_reply_timeout_default_has_one_source(monkeypatch, tmp_path):
    """起草窗口（2026-10-20 由 1200 收到 420）：默认值只许活在常量一处。

    修掉的老毛病：解析处写 1200、消费处 `cfg.get("reply_timeout", 600)` 又写一份 600——
    改默认时漏掉后者，生产按前者的数、一旦 cfg 少落键就静默换成另一个数。"""
    for name, fn in [
        ("resolve_active_profile", lambda env=None: ("blockchain", "")),
        ("profile_line", lambda name, prefix: "[profile] blockchain"),
        (
            "load_config",
            lambda env=None: {"quiet_hours": (0, 0), "daily_limit_fixed": True, "daily_limit": 20, "cap_jitter": 0},
        ),
        ("effective_daily_limit", lambda cfg, state_path=None: 20),
        ("profile_sections", lambda path: None),
        ("parse_queries", lambda env: ["AI"]),
        ("is_quiet_hour", lambda hour, quiet: False),
        ("startup_jitter", lambda cfg: None),
        ("SessionPacer", lambda cfg, **kw: SimpleNamespace()),
        ("get_platform", lambda: SimpleNamespace()),
        ("load_applied_ids", lambda path: set()),
        ("load_excluded_companies", lambda path: set()),
        ("_open_session_dirs", lambda base, vbase, record, ts: (tmp_path / "s", tmp_path / "shots", {})),
        (
            "session_banner",
            lambda queries, limit, limit_desc, applied_ids, excluded, session_dir, dry_run, recommend_desc="": None,
        ),
    ]:
        monkeypatch.setattr(mod, name, fn)
    monkeypatch.setattr(
        mod,
        "private_path",
        lambda rel, prefix="": tmp_path / "private" / Path(rel).parent / f"{prefix}{Path(rel).name}",
    )

    assert mod._init_live_session({"LIVE_REPLY_TIMEOUT": "120"}).session.cfg["reply_timeout"] == 120
    assert (
        mod._init_live_session({"LIVE_REPLY_TIMEOUT": ""}).session.cfg["reply_timeout"] == mod.REPLY_TIMEOUT_DEFAULT
    ), "空串必须回落到常量而不是 0（0 = 立刻超时，等于把每个岗判死）"

    src = Path(mod.__file__).resolve().read_text(encoding="utf-8")
    assert 'cfg.get("reply_timeout", REPLY_TIMEOUT_DEFAULT)' in src, "消费点还带着第二份字面量默认值"
    assert 'env.get("LIVE_REPLY_TIMEOUT", str(REPLY_TIMEOUT_DEFAULT))' in src, "解析点不引用常量"


def test_init_live_session_recommend_cap_wires_env_to_truncation(monkeypatch, tmp_path, capsys):
    """接线回归（#65，教训同 #33）：`LIVE_RECOMMEND_CAP` 必须从 env 一路走到
    `_recommend_round` 的**真截断**。只测 `_apply_recommend_cap_cfg` 是测不到接线的——
    cfg 键名拼错、解析函数没被调用，生产里都会「恒不截断」而测试全绿。"""
    _init_stubs(monkeypatch, tmp_path)
    run = mod._init_live_session({"LIVE_RECOMMEND_CAP": "3"})
    assert run.session.cfg["recommend_cap"] == 3
    jobs = _rec_jobs(15)
    monkeypatch.setattr(mod, "recommend_list", lambda page, limit=None, target=0, flags=None: jobs)
    monkeypatch.setattr(mod, "collect_new_jds", lambda listing, collected, applied_ids: list(listing))
    monkeypatch.setattr(mod, "_remember_list_url", lambda page: None)
    monkeypatch.setattr(mod, "_browse_list", lambda *a, **k: None)
    seen = {}
    monkeypatch.setattr(
        mod,
        "_apply_fresh_jobs",
        lambda page, r, fresh, idx: seen.update(ids=[j["encryptJobId"] for j in fresh]) or (False, idx),
    )
    mod._recommend_round("P", run, 0)
    assert seen["ids"] == ["r0", "r1", "r2"], "env 里的 3 必须真的变成消费口收到的前 3 个"
    assert "候选 15 个 / 消费 3 个 / 未消费 12 个" in capsys.readouterr().out


def test_recommend_turn_open_conditions():
    """开轮条件两连（#33 / #61 方案 A）：开关 on + 有剩余额度，缺一不跑推荐流。

    「已停批」不再是开轮条件，本用例也不再断言它——那是 #61 刻意移除的行为（停批信号
    把整个推荐轮吃掉正是票面报的 bug），留着断言等于把回归钉成契约。停批语义改由逐岗
    预检 `_pre_job_stop` 承担，理由与核实过的调用链见 `mod._recommend_turn_open` docstring。"""
    run = SimpleNamespace(session=SimpleNamespace(cfg={"live_recommend": True}, state={"applied": 0}), limit=20)
    assert mod._recommend_turn_open(run) is True
    run.session.cfg["live_recommend"] = False
    assert mod._recommend_turn_open(run) is False, "开关 off 不跑"
    run.session.cfg["live_recommend"] = True
    run.session.state["applied"] = 20
    assert mod._recommend_turn_open(run) is False, "额度已满不跑"


def test_build_report_carries_recommend_fields():
    """#33 可观测口径进 report.json：listing=None 表示该轮没跑（开关 off / 额度已满；
    #61 方案 A 起「已停批」不再是理由），与 0（跑了但没拿到）区分；轮级错误列表一并带出。"""
    state = {"applied": 0, "direct_fallback": 0, "keyword_skips": []}
    rep = mod.build_report(["q"], state, [])
    assert rep["recommend_listing"] is None and rep["recommend_errors"] == []
    state["recommend_listing"] = 0
    state["recommend_errors"] = ["boom"]
    rep = mod.build_report(["q"], state, [])
    assert rep["recommend_listing"] == 0 and rep["recommend_errors"] == ["boom"]


def _rec_jobs(n):
    """推荐轮候选桩（#65）：n 个带稳定 eids 的岗位，顺序即消费顺序。"""
    return [{"encryptJobId": f"r{i}", "jobName": f"岗{i}", "brandName": "别家"} for i in range(n)]


def _recommend_run(tmp_path, monkeypatch, jobs, cap, new_slice=None):
    """推荐轮的桩位（#65）：采集（`recommend_list`）与拟人浏览全替，只留
    `_recommend_round` 里「落账 → 配额截断 → 交给消费口」那一段。
    返回 (run, seen)——`seen["fresh"]` 是 `_apply_fresh_jobs` **实际收到**的列表。
    `new_slice` 让用例把「候选数」与「listing 条数」分开造（实测是 49 条 → 9 个候选）。"""
    session, _ = _fake_session(tmp_path, cfg={"quiet_hours": (0, 0), "collect_cap": 0, "recommend_cap": cap})

    def fake_recommend_list(page, limit=None, target=0, flags=None):
        if flags is not None:
            flags.update(rounds=4, at_bottom=False)  # 采集读数按真机场径（20261009-104050：滚 4 轮）
        return jobs

    monkeypatch.setattr(mod, "recommend_list", fake_recommend_list)
    monkeypatch.setattr(
        mod,
        "collect_new_jds",
        lambda listing, collected, applied_ids: list(listing) if new_slice is None else listing[new_slice],
    )
    monkeypatch.setattr(mod, "_remember_list_url", lambda page: None)
    monkeypatch.setattr(mod, "_browse_list", lambda *a, **k: None)
    seen = {}
    monkeypatch.setattr(
        mod,
        "_apply_fresh_jobs",
        lambda page, run, fresh, idx: seen.update(fresh=list(fresh)) or (False, idx),
    )
    run = SimpleNamespace(session=session, collected=[], applied_ids=set(), limit=20)
    return run, seen


def test_recommend_round_cap_truncates_consumption(tmp_path, monkeypatch, capsys):
    """#65 验收 1：候选 15 个、配额 10 ⇒ 只有**前 10 个**进 `_apply_fresh_jobs`，
    且日志里三个数齐（候选 / 消费 / 未消费）并点名代价——静默截断正是本票要防的病。"""
    run, seen = _recommend_run(tmp_path, monkeypatch, _rec_jobs(15), 10)
    stop, idx = mod._recommend_round("P", run, 0)
    assert [j["encryptJobId"] for j in seen["fresh"]] == [f"r{i}" for i in range(10)], (
        "配额后的是「没轮到」，不是「换序消费」"
    )
    assert stop is False and idx == 0
    out = capsys.readouterr().out
    assert "候选 15 个" in out and "消费 10 个" in out and "未消费 5 个" in out
    assert "不投也不进台账" in out and "#65 登记的代价" in out, "代价不写在行里，读日志的人不知道少了 5 个岗"
    assert run.session.state["recommend_candidates"] == 15
    assert run.session.state["recommend_consumed"] == 10


def test_recommend_round_cap_zero_consumes_all(tmp_path, monkeypatch, capsys):
    """#65 验收 1b：配额 0 = 不限 ⇒ 全量消费，日志明写「不限」（放开了也要看得见）。"""
    run, seen = _recommend_run(tmp_path, monkeypatch, _rec_jobs(15), 0)
    mod._recommend_round("P", run, 0)
    assert len(seen["fresh"]) == 15
    out = capsys.readouterr().out
    assert "不限" in out and "候选 15 个" in out and "消费 15 个" in out
    assert run.session.state["recommend_consumed"] == 15


def test_recommend_round_below_cap_logs_no_truncation_line(tmp_path, monkeypatch, capsys):
    """低噪音：候选没超配额时不打截断行（每轮都打就没人读了），但收尾读数仍看得出消费数。"""
    run, seen = _recommend_run(tmp_path, monkeypatch, _rec_jobs(8), 10)
    mod._recommend_round("P", run, 0)
    assert len(seen["fresh"]) == 8
    assert "[配额]" not in capsys.readouterr().out
    assert (run.session.state["recommend_candidates"], run.session.state["recommend_consumed"]) == (8, 8)


def test_recommend_round_cap_leaves_collect_readings_untouched(tmp_path, monkeypatch):
    """#65 验收 3（本票与「改采集上限」的分界）：同一份采集在截断场与不限场里，
    `recommend_listing` / `recommend_scrolls` / 候选数必须逐字相同——配额只管吃几个。
    读数按真机场径造（会话 20261009-104050：49 条、滚 4 轮、去重后 9 个候选）。"""
    capped, seen_a = _recommend_run(tmp_path, monkeypatch, _rec_jobs(49), 3, new_slice=slice(9))
    mod._recommend_round("P", capped, 0)
    open_ = _recommend_run(tmp_path, monkeypatch, _rec_jobs(49), 0, new_slice=slice(9))[0]
    mod._recommend_round("P", open_, 0)
    for key in ("recommend_listing", "recommend_scrolls", "recommend_candidates"):
        assert capped.session.state[key] == open_.session.state[key], f"{key} 是采集侧读数，配额不得动它"
    assert (capped.session.state["recommend_listing"], capped.session.state["recommend_scrolls"]) == (49, 4)
    assert capped.session.state["recommend_candidates"] == 9
    assert (len(seen_a["fresh"]), capped.session.state["recommend_consumed"]) == (3, 3)
    assert open_.session.state["recommend_consumed"] == 9, "不限场照常全量消费"


def test_build_report_carries_recommend_cap_readings():
    """#65 机读面：`recommend_candidates` / `recommend_consumed` 两键恒在。
    推荐轮没跑落 **null**（与 `recommend_listing` 的 null≠0 口径同源，见 apply-forensics），
    跑了但候选 0 是个真读数，落 0。"""
    state = {"applied": 0, "direct_fallback": 0, "keyword_skips": []}
    rep = mod.build_report(["q"], state, [])
    assert rep["recommend_candidates"] is None and rep["recommend_consumed"] is None
    state.update({"recommend_candidates": 9, "recommend_consumed": 3})
    rep = mod.build_report(["q"], state, [])
    assert (rep["recommend_candidates"], rep["recommend_consumed"]) == (9, 3), "差值 6 = 本场没轮到的岗数"
    state.update({"recommend_candidates": 0, "recommend_consumed": 0})
    rep = mod.build_report(["q"], state, [])
    assert rep["recommend_candidates"] == 0 and rep["recommend_consumed"] == 0


def test_build_report_outcome_summary_buckets_skip_vs_fail():
    """#47：report 层把「主动跳过/规矩拦停」与「真失败」分桶——agent_skip 不得再被
    读成投递失败（2026-09-24 实际误读）。映射：跳过=agent_skip/no_jd/skipped/
    excluded_company/gate_blocked/paused/skip/dry_run_ok/wedge_skipped（#56 B1：楔死台账
    按设计不再投＝跳过，不是失败）；失败=risk_blocked/no_send/agent_timeout/login_expired/
    no_btn/no_input/page_error/preflight_click_failed。"""
    results = [
        {"status": "applied"},
        {"status": "agent_skip", "reason": "hard_lang"},
        {"status": "no_jd"},
        {"status": "excluded_company"},
        {"status": "dry_run_ok"},
        {"status": "gate_blocked", "detail": "x"},
        {"status": "wedge_skipped"},
        {"status": "risk_blocked", "detail": "x"},
        {"status": "no_btn"},
        {"status": "no_send"},
    ]
    rep = mod.build_report(["q"], {"applied": 1, "direct_fallback": 0}, results)
    assert rep["outcome_summary"] == {"applied": 1, "skipped": 6, "failed": 3}


def test_build_report_outcome_summary_unknown_status_is_failed():
    """未知新状态保守归 failed——宁可显眼不可静默（不得把未来状态悄悄算成跳过）。"""
    rep = mod.build_report(["q"], {"applied": 0, "direct_fallback": 0}, [{"status": "some_future_status"}])
    assert rep["outcome_summary"] == {"applied": 0, "skipped": 0, "failed": 1}


def _fake_run(tmp_path, cfg=None, queries=("AI",), limit=20):
    """_LiveRun 替身（#33）：最小可跑字段集；session 复用 _fake_session。"""
    session, _ = _fake_session(tmp_path, cfg={"quiet_hours": (0, 7), **(cfg or {})})
    return mod._LiveRun(
        env={},
        ts="t",
        reported_path=tmp_path / "reported.json",  # #51：防重台账路径（这批用例不触达建票分支）
        browser_dir=tmp_path,
        video_base=tmp_path,
        video_kwargs={},
        queries=list(queries),
        record=False,
        limit=limit,
        applied_ids=set(),
        excluded=set(),
        session=session,
    )


def _spy_recommend_round(calls):
    """推荐流轮替身：按调用顺序记 ("recommend", 开轮时的 idx)——#61 的顺序断言取证点。"""

    def f(page, run, idx):
        calls.append(("recommend", idx))
        return False, idx

    return f


def _spy_keyword_round(calls, fresh_by_query=None):
    """关键词轮替身：记 ("keyword", q)，并按词返回候选（默认空 → 不进投递内层循环）。"""

    def f(page, q, state, applied_ids, collected=None, target=0):
        calls.append(("keyword", q))
        return list((fresh_by_query or {}).get(q, []))

    return f


def test_execute_query_rounds_skips_recommend_turn_when_off(tmp_path, monkeypatch):
    """LIVE_RECOMMEND=off：不接推荐流轮（行为同 #33 之前；#61 前置后仍是唯一退路）。"""
    calls = []
    monkeypatch.setattr(mod, "keyword_round", _spy_keyword_round(calls))
    monkeypatch.setattr(mod, "rnd_sleep", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_recommend_round", _spy_recommend_round(calls))
    mod._execute_query_rounds(SimpleNamespace(), _fake_run(tmp_path, {"live_recommend": False}))
    assert [c[0] for c in calls] == ["keyword"], "off 时只剩搜索流"


def test_execute_query_rounds_runs_recommend_turn_when_on(tmp_path, monkeypatch):
    """默认 on：推荐流轮开轮，且开轮时 idx 仍是 0——它跑在任何关键词轮之前（#61 方案 A）。"""
    calls = []
    monkeypatch.setattr(mod, "keyword_round", _spy_keyword_round(calls))
    monkeypatch.setattr(mod, "rnd_sleep", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_recommend_round", _spy_recommend_round(calls))
    mod._execute_query_rounds(SimpleNamespace(), _fake_run(tmp_path, {"live_recommend": True}))
    assert calls == [("recommend", 0), ("keyword", "AI")], "推荐轮先于关键词轮（idx 从 0 起）"


def test_execute_query_rounds_skips_recommend_turn_when_limit_reached(tmp_path, monkeypatch):
    """额度已满时不跑推荐流轮：不因多一个来源突破日上限（票面风险项）。"""
    calls = []
    monkeypatch.setattr(mod, "keyword_round", _spy_keyword_round(calls))
    monkeypatch.setattr(mod, "rnd_sleep", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_recommend_round", _spy_recommend_round(calls))
    run = _fake_run(tmp_path, {"live_recommend": True}, limit=20)
    run.session.state["applied"] = 20
    mod._execute_query_rounds(SimpleNamespace(), run)
    assert calls == [], "满额既不开推荐轮，也不开关键词轮"


def test_execute_query_rounds_recommend_turn_precedes_every_keyword_round(tmp_path, monkeypatch):
    """#61 方案 A 的实质——顺序回归锁这里：推荐轮必须在**任何**关键词轮之前被调用，
    且关键词轮从推荐轮的 idx 续号（推荐轮先吃额度，编号也先占）。

    单独钉顺序是因为 `..._when_on` 里关键词轮返回空候选、不进投递内层循环，
    两种顺序下 idx 读数一样——那条用例分不清先后，本用例给非空候选把它分清。"""
    calls = []
    fresh = [{"encryptJobId": "e1", "jobName": "岗1", "brandName": "某公司"}]

    def spy_apply_fresh_jobs(page, run, fresh_jobs, idx):
        calls.append(("apply", idx + 1))  # 本轮发出去的第一个编号
        return False, idx + len(fresh_jobs)

    monkeypatch.setattr(mod, "rnd_sleep", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_browse_list", lambda *a, **k: None)
    monkeypatch.setattr(mod, "keyword_round", _spy_keyword_round(calls, {"a": fresh, "b": fresh}))
    monkeypatch.setattr(mod, "_apply_fresh_jobs", spy_apply_fresh_jobs)
    monkeypatch.setattr(mod, "_recommend_round", _spy_recommend_round(calls))
    mod._execute_query_rounds(SimpleNamespace(), _fake_run(tmp_path, {"live_recommend": True}, queries=("a", "b")))
    assert [c[0] for c in calls] == ["recommend", "keyword", "apply", "keyword", "apply"]
    assert calls[0][1] == 0, "推荐轮在搜索流之前开轮（idx=0）"
    assert [c[1] for c in calls if c[0] == "apply"] == [1, 2], "关键词轮从推荐轮的 idx 续号"


def test_execute_query_rounds_recommend_turn_runs_after_search_round_stops(tmp_path, monkeypatch):
    """#61 票面形态回归：搜索轮**中途**停批（风控/熔断/静默/CLOSE 任一），推荐流仍要跑过。

    旧代码在这种形态下整段跳过推荐轮（`not stop` + 推荐轮排在全部关键词轮之后），
    report.json 里 recommend_listing/scrolls 全 null、recommend_errors 为空——
    即会话 20261005-215607 观测到的「推荐流整段没执行」。
    停批仍要拦住**后续搜索轮**（推荐轮返回的 stop 喂给循环开头的既有守卫），
    但不再吃掉推荐轮本身。"""
    calls = []

    def spy_apply_fresh_jobs(page, run, fresh_jobs, idx):
        calls.append(("apply", len(fresh_jobs)))
        run.session.state["applied"] = 1  # 投出 1 个后收到停批信号
        return True, idx + len(fresh_jobs)

    def spy_recommend_round(page, run, idx):
        calls.append(("recommend", idx))
        run.session.state["applied"] = 2
        return False, idx

    monkeypatch.setattr(mod, "rnd_sleep", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_browse_list", lambda *a, **k: None)
    monkeypatch.setattr(
        mod, "keyword_round", _spy_keyword_round(calls, {"a": [{"jobName": "岗1"}], "b": [{"jobName": "岗2"}]})
    )
    monkeypatch.setattr(mod, "_apply_fresh_jobs", spy_apply_fresh_jobs)
    monkeypatch.setattr(mod, "_recommend_round", spy_recommend_round)
    mod._execute_query_rounds(SimpleNamespace(), _fake_run(tmp_path, {"live_recommend": True}, queries=("a", "b")))
    assert [c[0] for c in calls] == ["recommend", "keyword", "apply"], "停批后不得再开第二个关键词轮"
    assert ("recommend", 0) in calls, "搜索轮中途停批仍要跑推荐流（#61 票面形态）"


# --- #61 验收 2：来源归因（轮次跨度的写入点与 build_report 的读出点）---


def _record_applied_job(page, job, idx, session):
    """投递内层循环替身：按 apply_live 的记账形状往 results 追加一条带 idx 的条目。
    brand 命中 excluded 时由真实的 `_apply_fresh_jobs` 先跳过，走不到这里。"""
    session.state["applied"] += 1
    session.results.append({"idx": idx, "job": job.get("jobName"), "status": "applied"})
    return None


def test_span_records_round_numbering_and_results_slice(tmp_path):
    """跨度记账单元（#61 验收 2）：区间 = 该轮发出的编号 [idx_from, idx_to] 与该轮追加的
    results 切片 [res_from, res_to)；本轮没发过号（停批命中首岗 / 候选全被跳过 / 推荐流取列表
    失败直接 return）就**不落账**——空跨度是给 build_report 造的假区间。"""
    session, _ = _fake_session(tmp_path)
    start = mod._span_start(session, 2)  # 本轮要发的第一个编号是 3
    session.results.append({"idx": 3, "status": "applied"})
    mod._span_end(session, "search", "AI", start, 3)
    assert session.state["source_spans"] == [
        {"source": "search", "query": "AI", "idx_from": 3, "idx_to": 3, "res_from": 0, "res_to": 1}
    ]
    empty = mod._span_start(session, 3)
    mod._span_end(session, "recommend", None, empty, 3)
    assert len(session.state["source_spans"]) == 1, "idx 未推进的轮次不落跨度"


def test_execute_query_rounds_records_recommend_then_search_spans(tmp_path, monkeypatch):
    """写入点接线（#61 验收 2）：推荐轮一条、**每个**关键词轮各一条，编号与 results 切片连续。
    必须由生产函数跑出来——只手工喂 spans 给 build_report 测不到接线（#33 那次「生产恒不执行、
    测试全绿」的教训就出在这）。推荐轮这里用真实的 `_apply_fresh_jobs`，编号推进口径与生产一致。"""
    run = _fake_run(tmp_path, {"live_recommend": True}, queries=("a", "b"))
    fresh = {
        "a": [{"encryptJobId": "e1", "jobName": "岗1", "brandName": "某司"}],
        "b": [{"encryptJobId": "e2", "jobName": "岗2", "brandName": "某司"}],
    }

    def spy_recommend_round(page, run, idx):
        return mod._apply_fresh_jobs(page, run, [{"encryptJobId": "r1", "jobName": "推1", "brandName": "某司"}], idx)

    monkeypatch.setattr(mod, "rnd_sleep", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_browse_list", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_pre_job_stop", lambda s, r: False)
    monkeypatch.setattr(mod, "keyword_round", _spy_keyword_round([], fresh))
    monkeypatch.setattr(mod, "_recommend_round", spy_recommend_round)
    monkeypatch.setattr(mod, "_apply_one_job", _record_applied_job)
    mod._execute_query_rounds(SimpleNamespace(), run)
    assert run.session.state["source_spans"] == [
        {"source": "recommend", "query": None, "idx_from": 1, "idx_to": 1, "res_from": 0, "res_to": 1},
        {"source": "search", "query": "a", "idx_from": 2, "idx_to": 2, "res_from": 1, "res_to": 2},
        {"source": "search", "query": "b", "idx_from": 3, "idx_to": 3, "res_from": 2, "res_to": 3},
    ], "推荐轮在前占号，关键词轮从其续号（#61 方案 A 的编号形态）"


def test_retry_session_jobs_records_retry_span(tmp_path, monkeypatch):
    """写入点接线（#61 验收 2）：本会话补投轮的跨度算 "retry"，idx 从 last_idx 续号（#55 语义）。"""
    session, run = _retry_run(tmp_path)
    session.state["session_retries"] = [{"job": _timeout_job(brand="某司"), "request_idx": 4}]
    session.state["last_idx"] = 4
    monkeypatch.setattr(mod, "_pre_job_stop", lambda s, r: False)
    monkeypatch.setattr(mod, "_apply_one_job", _record_applied_job)
    mod._retry_session_jobs("P", run)
    assert session.state["source_spans"] == [
        {"source": "retry", "query": None, "idx_from": 5, "idx_to": 5, "res_from": 0, "res_to": 1}
    ]


def test_retry_pending_jobs_records_retry_span(tmp_path, monkeypatch):
    """写入点接线（#61 验收 2）：跨会话补投轮的跨度同样算 "retry"（两种补投同一种来源）。"""
    session, run = _retry_run(tmp_path)
    path = Path(session.cfg["retry_pending_path"])
    mod._save_retry_pending(
        path,
        [
            mod._retry_snapshot(_timeout_job("job1", brand="某司"), jd_text=""),
            mod._retry_snapshot(_timeout_job("job2", name="岗2", brand="某司"), jd_text=""),
        ],
    )
    monkeypatch.setattr(mod, "_pre_job_stop", lambda s, r: False)
    monkeypatch.setattr(mod, "_apply_one_job", _record_applied_job)
    assert mod._retry_pending_jobs("P", run) is False
    assert session.state["source_spans"] == [
        {"source": "retry", "query": None, "idx_from": 1, "idx_to": 2, "res_from": 0, "res_to": 2}
    ], "台账轮跑完整批只落一条跨度（这一轮的区间就是它发过的号）"


def test_build_report_attributes_each_source():
    """读出点（#61 验收 2）：每条 result 按 idx + results 切片归到所属轮次，
    报告同时给出 applied_by_source 的 applied 小计。"""
    spans = [
        {"source": "recommend", "query": None, "idx_from": 1, "idx_to": 1, "res_from": 0, "res_to": 1},
        {"source": "search", "query": "AI", "idx_from": 2, "idx_to": 3, "res_from": 1, "res_to": 3},
        {"source": "retry", "query": None, "idx_from": 4, "idx_to": 4, "res_from": 3, "res_to": 4},
    ]
    results = [
        {"idx": 1, "status": "applied"},
        {"idx": 2, "status": "applied"},
        {"idx": 3, "status": "agent_skip", "reason": "hard_lang"},
        {"idx": 4, "status": "applied"},
    ]
    rep = mod.build_report(["AI"], {"applied": 3, "direct_fallback": 0, "source_spans": spans}, results)
    assert [r["source"] for r in rep["results"]] == ["recommend", "search", "search", "retry"]
    assert rep["applied_by_source"] == {"search": 1, "recommend": 1, "retry": 1, "unknown": 0}


def test_build_report_source_is_null_when_unattributable():
    """归不上就 null，**不猜**（null 不是「来自搜索」）：
    · 起飞前自检的条目落在所有跨度之前，且没有编号；
    · excluded_company / wedge_skipped 这类条目没有编号——哪怕位置就在该轮切片内也不归因；
    · 编号与切片都在跨度外。
    归不上的 applied 进 applied_by_source 的 unknown 桶，不静默吞（同 #47 未知状态归 failed）。"""
    spans = [{"source": "search", "query": "AI", "idx_from": 1, "idx_to": 2, "res_from": 1, "res_to": 3}]
    results = [
        {"status": "preflight_click_failed"},
        {"idx": 1, "status": "applied"},
        {"status": "excluded_company"},
        {"idx": 9, "status": "applied"},
    ]
    rep = mod.build_report(["AI"], {"applied": 2, "direct_fallback": 0, "source_spans": spans}, results)
    assert [r["source"] for r in rep["results"]] == [None, "search", None, None]
    assert rep["applied_by_source"] == {"search": 1, "recommend": 0, "retry": 0, "unknown": 1}


def test_build_report_separates_overlapping_retry_and_search_numbering():
    """#61 验收 2 的实测坑：跨会话补投轮从 state["last_idx"] 起号、搜索流的 idx 从 0 起
    （既有语义，本次不动），于是两段的编号区间**都是 1..N、会重叠**——只看 idx 会把台账里
    投出的那封读成搜索流投出的。results 切片在同一场会话里严格递增，只有它能分开两者。"""
    spans = [
        {"source": "retry", "query": None, "idx_from": 1, "idx_to": 2, "res_from": 0, "res_to": 2},
        {"source": "search", "query": "AI", "idx_from": 1, "idx_to": 2, "res_from": 2, "res_to": 4},
    ]
    results = [
        {"idx": 1, "status": "applied"},
        {"idx": 2, "status": "applied"},
        {"idx": 1, "status": "applied"},  # 搜索流与台账撞号
        {"idx": 2, "status": "agent_skip", "reason": "hard_lang"},
    ]
    rep = mod.build_report(["AI"], {"applied": 3, "direct_fallback": 0, "source_spans": spans}, results)
    assert [r["source"] for r in rep["results"]] == ["retry", "retry", "search", "search"]
    assert rep["applied_by_source"] == {"search": 1, "recommend": 0, "retry": 2, "unknown": 0}


def test_applied_by_source_is_self_consistent_with_per_entry_source(tmp_path, monkeypatch):
    """自洽（#61 验收 2）：聚合读数与逐条 source 同源，两边必须对得上——
    用真实写入点（_execute_query_rounds + _retry_session_jobs）造出 spans 与 results，
    再按逐条 (status, source) 手工数一遍，与 applied_by_source 比；
    且三桶 + unknown 的总和 == outcome_summary.applied（一封都不许丢）。
    含一条 excluded_company（无编号 → source 为 null），验证它不进任何来源桶。"""
    run = _fake_run(tmp_path, {"live_recommend": True}, queries=("a",))
    run.excluded = {"某基金"}
    run.session.state["source_spans"] = []
    fresh = [
        {"encryptJobId": "e1", "jobName": "岗1", "brandName": "某司"},
        {"encryptJobId": "e2", "jobName": "岗2", "brandName": "某基金"},
    ]

    def spy_recommend_round(page, run, idx):
        return mod._apply_fresh_jobs(page, run, [{"encryptJobId": "r1", "jobName": "推1", "brandName": "某司"}], idx)

    monkeypatch.setattr(mod, "rnd_sleep", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_browse_list", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_pre_job_stop", lambda s, r: False)
    monkeypatch.setattr(mod, "keyword_round", _spy_keyword_round([], {"a": fresh}))
    monkeypatch.setattr(mod, "_recommend_round", spy_recommend_round)
    monkeypatch.setattr(mod, "_apply_one_job", _record_applied_job)
    mod._execute_query_rounds(SimpleNamespace(), run)
    run.session.state["session_retries"] = [{"job": _timeout_job("job9", name="补1", brand="某司"), "request_idx": 1}]
    mod._retry_session_jobs("P", run)

    rep = mod.build_report(run.queries, run.session.state, run.session.results)
    manual = {"search": 0, "recommend": 0, "retry": 0, "unknown": 0}
    for r in rep["results"]:
        if r["status"] == "applied":
            manual[r["source"] if r.get("source") in manual else "unknown"] += 1
    assert rep["applied_by_source"] == manual == {"search": 1, "recommend": 1, "retry": 1, "unknown": 0}
    assert sum(rep["applied_by_source"].values()) == rep["outcome_summary"]["applied"] == rep["applied"]
    assert [r["source"] for r in rep["results"]] == ["recommend", "search", None, "retry"]


# --- #32 段 2：招聘者活跃度字段旁听提取与透传 ---


def test_extract_recruiter_active_pulls_fields():
    container = {"data": {"code": 0, "zpData": {"bossInfo": {"activeTimeDesc": "刚刚活跃", "bossOnline": True}}}}
    assert mod._extract_recruiter_active(container) == {"bossActiveDesc": "刚刚活跃", "bossOnline": True}


def test_extract_recruiter_active_missing_returns_empty():
    assert mod._extract_recruiter_active({}) == {}
    assert mod._extract_recruiter_active({"data": None}) == {}
    assert mod._extract_recruiter_active({"data": {"code": 0, "zpData": {}}}) == {}


def test_request_greeting_includes_recruiter_active(tmp_path):
    job = {"jobName": "区块链工程师", "brandName": "某公司", "encryptJobId": "eid1"}
    path = mod.request_greeting(
        tmp_path, 0, job, "JD", recruiter_active={"bossActiveDesc": "今日活跃", "bossOnline": False}
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["bossActiveDesc"] == "今日活跃" and data["bossOnline"] is False


def test_request_greeting_without_recruiter_active_omits_fields(tmp_path):
    job = {"jobName": "区块链工程师", "brandName": "某公司", "encryptJobId": "eid1"}
    data = json.loads(mod.request_greeting(tmp_path, 0, job, "JD").read_text(encoding="utf-8"))
    assert "bossActiveDesc" not in data and "bossOnline" not in data


def test_record_greeting_carries_recruiter_active(tmp_path):
    job = {"encryptJobId": "eid1", "jobName": "岗", "brandName": "司", "bossName": "汀先生", "bossTitle": "CTO"}
    path = tmp_path / "greetings.json"
    assert (
        mod.record_greeting(
            job,
            "您好",
            "2026-09-24T10:00:00",
            greetings_path=str(path),
            recruiter_active={"bossActiveDesc": "刚刚活跃", "bossOnline": True},
        )
        == "added"
    )
    entry = json.loads(path.read_text(encoding="utf-8"))[0]
    assert entry["bossActiveDesc"] == "刚刚活跃" and entry["bossOnline"] is True


def test_record_greeting_without_recruiter_active_omits_fields(tmp_path):
    job = {"encryptJobId": "eid2", "jobName": "岗", "brandName": "司"}
    path = tmp_path / "greetings.json"
    mod.record_greeting(job, "您好", "2026-09-24T10:00:00", greetings_path=str(path))
    entry = json.loads(path.read_text(encoding="utf-8"))[0]
    assert "bossActiveDesc" not in entry and "bossOnline" not in entry


# --- #49 收尾现场采集 followup：收尾 eval 吃最新会话数据（连同历史标注全量重算）---


def _fresh_run(tmp_path, applied=None):
    """#49 夹具：cfg 带 history_path（收尾采集读 applied 清单用）。"""
    run = _fake_run(tmp_path, cfg={})
    hist = tmp_path / "history.json"
    hist.write_text(json.dumps({"applied": applied or []}), encoding="utf-8")
    run.session.cfg["history_path"] = str(hist)
    return run


def test_collect_fresh_followup_writes_latest_report(tmp_path, capsys, monkeypatch):
    """采集成功：followup_report.json 落最新数据 + followup_fresh=True + 采集统计行。"""
    run = _fresh_run(tmp_path, applied=[{"encryptJobId": "e1"}])
    report = {
        "totalConversations": 2,
        "conversations": [{"company": "甲", "status": "hr_replied"}, {"company": "乙", "status": "awaiting"}],
    }
    monkeypatch.setattr(run.session.platform, "followup", lambda page, applied, **kw: report, raising=False)
    out = tmp_path / "followup_report.json"
    monkeypatch.setattr(mod.g, "resolve_paths", lambda env=None: {"followup": out})
    mod._collect_fresh_followup(run, page=None)
    assert json.loads(out.read_text(encoding="utf-8")) == report
    assert run.session.state["followup_fresh"] is True
    assert "现场采集 2 条对话（HR 回复 1）" in capsys.readouterr().out


def test_collect_fresh_followup_report_carries_watermark_readings(tmp_path, monkeypatch):
    """#75 review 条目 B：`newSinceWatermark` / `watermark` 原来只在 `followup_tracker.run()` 注入，
    而这条路径**整份重写同一个 followup_report.json** 且不带那两键——投递场次收尾正是用户读这份
    报告的时刻（runbook 的「先看哪几条」在那里拿）。两键必须由同一个函数落，两个消费者才不会一个有一个没有。
    同时钉住：这条路径**只读**，一个字都不写基准。"""
    run = _fresh_run(tmp_path, applied=[{"encryptJobId": "e1"}])
    convs = [
        {"encryptJobId": "e1", "status": "hr_replied", "lastTS": 1792000000000},
        {"encryptJobId": "e2", "status": "hr_replied", "lastTS": 1792999999000},
    ]
    report = {"totalConversations": 2, "myUserId": 780045835, "conversations": convs}
    monkeypatch.setattr(run.session.platform, "followup", lambda page, applied, **kw: report, raising=False)
    out = tmp_path / "followup_report.json"
    monkeypatch.setattr(mod.g, "resolve_paths", lambda env=None: {"followup": out})
    monkeypatch.setattr(mod, "resolve_active_profile", lambda **k: ("default", ""))
    monkeypatch.setattr(
        mod, "private_path", lambda relative, prefix="": tmp_path / "private" / f"{prefix}{Path(relative).name}"
    )
    base_path = tmp_path / "private" / "inbound_watermark.json"
    mod._collect_fresh_followup(run, page=None)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["newSinceWatermark"] is None and data["watermark"]["state"] == "first_run", data
    assert not base_path.exists(), "收尾采集只报不写——推进是 --mark-read 的事"
    # 已有基准时同一场读数念得出差值（判据与 followup_tracker 完全同源）
    wm.advance_watermark(
        base_path, [convs[0]], wm.empty_base("default"), profile="default", snapshot_at="2026-10-19 09:00:00"
    )
    mod._collect_fresh_followup(run, page=None)
    again = json.loads(out.read_text(encoding="utf-8"))
    assert again["newSinceWatermark"] == 1 and again["watermark"]["state"] == "ok", again
    assert again["watermark"]["snapshotAt"] == "2026-10-19 09:00:00"


def test_collect_fresh_followup_failure_swallowed(tmp_path, capsys, monkeypatch):
    """采集失败：warn + followup_fresh=False，不影响收尾（观测层语义，同 edd_wrapup）。"""

    def boom(page, applied, **kw):
        raise RuntimeError("nav timeout")

    run = _fresh_run(tmp_path)
    monkeypatch.setattr(run.session.platform, "followup", boom, raising=False)
    monkeypatch.setattr(mod.g, "resolve_paths", lambda env=None: {"followup": tmp_path / "f.json"})
    mod._collect_fresh_followup(run, page=None)  # 不抛
    assert run.session.state["followup_fresh"] is False
    assert "[warn] 收尾 followup 采集失败" in capsys.readouterr().out


def test_collect_fresh_followup_none_result_falls_back(tmp_path, capsys, monkeypatch):
    """followup 返回 None（登录态失效等平台内兜底）→ 按采集失败口径收尾。"""
    run = _fresh_run(tmp_path)
    monkeypatch.setattr(run.session.platform, "followup", lambda page, applied, **kw: None, raising=False)
    monkeypatch.setattr(mod.g, "resolve_paths", lambda env=None: {"followup": tmp_path / "f.json"})
    mod._collect_fresh_followup(run, page=None)
    assert run.session.state["followup_fresh"] is False
    assert "沿用上次复盘数据" in capsys.readouterr().out


def test_collect_fresh_followup_dry_run_skips(tmp_path, capsys, monkeypatch):
    """dry-run 不采集（零副作用语义）；followup_fresh=None = 没跑（区别于失败 False）。"""
    run = _fresh_run(tmp_path)
    run.session.state["dry_run"] = True
    called = []
    monkeypatch.setattr(run.session.platform, "followup", lambda page, applied, **kw: called.append(1), raising=False)
    mod._collect_fresh_followup(run, page=None)
    assert called == []
    assert run.session.state["followup_fresh"] is None
    assert capsys.readouterr().out == ""


def test_collect_fresh_followup_injects_heartbeat_beat(tmp_path, monkeypatch):
    """#59 修复轮 R4-1 的**接线证明**：`_collect_fresh_followup` 必须把 `_heartbeat_beat` 注成
    `beat_fn`——平台层、不 import 取证模块，拍点只有从编排层注入才存在；没接上时
    「聊天页 goto 上界 + 固定停顿」这段合法静默照样顶穿取栈阈值（健康会话给自己落一份非空
    wedge-stack.txt，正是 #58 那批假栈的形态）。`test_platforms.py` 那两条只证明「函数收到
    beat_fn 就会拍」，这条证明的是调用点真的把本体传下去了。"""
    run = _fresh_run(tmp_path)
    seen = {}

    def fake_followup(page, applied, beat_fn=None):
        seen["beat_fn"] = beat_fn
        return {"totalConversations": 1, "conversations": [{"company": "甲"}]}

    monkeypatch.setattr(run.session.platform, "followup", fake_followup, raising=False)
    monkeypatch.setattr(mod.g, "resolve_paths", lambda env=None: {"followup": tmp_path / "f.json"})
    mod._collect_fresh_followup(run, page=None)
    assert seen["beat_fn"] is mod._heartbeat_beat, "收尾采集段必须注入 A3 同一条吞异常口径的拍点"


def test_build_report_carries_followup_fresh():
    """#49 口径进 report.json：None = 没采集（dry-run），False = 失败，True = 已用最新数据。"""
    state = {"applied": 0, "direct_fallback": 0, "keyword_skips": []}
    assert mod.build_report(["q"], state, [])["followup_fresh"] is None
    state["followup_fresh"] = True
    assert mod.build_report(["q"], state, [])["followup_fresh"] is True
    state["followup_fresh"] = False
    assert mod.build_report(["q"], state, [])["followup_fresh"] is False


def test_run_live_collects_fresh_followup_before_report():
    """#49 接线钉：收尾采集必须在报告写盘之前（edd eval 才吃得到新数据）、保持浏览器之前。
    #33 教训：接线漏了测试全绿但生产不跑——从 run_live 源码钉调用顺序。"""
    src = (Path(__file__).resolve().parent / "live_apply.py").read_text(encoding="utf-8")
    body = src.split("def run_live(")[1]
    collect = body.index("_collect_fresh_followup(run, page)")
    report = body.index("_write_session_report(run)")
    hold = body.index("_hold_browser_until_closed(run.session)")
    assert collect < report < hold, "顺序须为 采集 followup → 写报告（含 eval）→ 保持浏览器"


def test_collect_fresh_followup_feeds_edd_snapshot(tmp_path, capsys, monkeypatch):
    """#49 验收①后半句：fresh 采集的数据确实流入收尾 eval——同一 paths 下
    record_session_snapshot 的快照 sampleSize/replied 来自刚落盘的报告，而非上次
    复盘的陈旧数据（review 指出的因果链缺口补直接断言）。"""
    run = _fresh_run(tmp_path, applied=[{"encryptJobId": "e1"}])
    report = {
        "totalConversations": 2,
        "conversations": [
            {"encryptJobId": "e1", "company": "测试公司1", "status": "hr_replied"},
            {"encryptJobId": "e2", "company": "测试公司2", "status": "awaiting"},
        ],
    }
    monkeypatch.setattr(run.session.platform, "followup", lambda page, applied, **kw: report, raising=False)
    paths = {
        "followup": tmp_path / "followup_report.json",
        "greetings": tmp_path / "greetings.json",
        "annotations": tmp_path / "annotations.json",
        "trend": tmp_path / "trend.json",
    }
    paths["greetings"].write_text(
        json.dumps(
            [
                {"encryptJobId": "e1", "brandName": "测试公司1", "greeting": "您好，看到岗位与经历很匹配，期待沟通"},
                {"encryptJobId": "e2", "brandName": "测试公司2", "greeting": "您好，看到岗位与经历很匹配，期待沟通"},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mod.g, "resolve_paths", lambda env=None: paths)
    mod._collect_fresh_followup(run, page=None)
    out = mod.g.record_session_snapshot(paths, profile="test")
    assert out["snapshot"]["sampleSize"] == 2
    assert out["snapshot"]["repliedCount"] == 1
    trend_last = paths["trend"].read_text(encoding="utf-8").strip().splitlines()[-1]  # append_trend = JSONL 追加
    assert json.loads(trend_last)["sampleSize"] == 2


# --- #59 残缺采集护栏：0 条才不覆盖 ⇒ 与上一份条数对比（首页截断是断崖，真衰减是渐进）---


def _prev_followup_report(path, n):
    """上一份 followup 报告（残缺判定的基准）：n 条会话。"""
    path.write_text(
        json.dumps({"totalConversations": n, "conversations": [{"company": f"上次公司{i}"} for i in range(n)]}),
        encoding="utf-8",
    )


def _collect_with(monkeypatch, run, n_conv, out):
    """让 platform.followup 返回 n_conv 条会话并按既有接线跑一次收尾采集。"""
    report = {
        "totalConversations": n_conv,
        "conversations": [{"company": f"本场公司{i}"} for i in range(n_conv)],
    }
    monkeypatch.setattr(run.session.platform, "followup", lambda page, applied, **kw: report, raising=False)
    monkeypatch.setattr(mod.g, "resolve_paths", lambda env=None: {"followup": out})
    mod._collect_fresh_followup(run, page=None)
    return report


def test_collect_fresh_followup_truncated_collection_keeps_previous(tmp_path, capsys, monkeypatch):
    """#59 验收 1：上一份 100 条、本场采到 3 条（首页截断，20260929-202514 实测形态）
    ⇒ 判残缺不覆盖 + followup_fresh=False + warn 把两个数字都打出来。"""
    run = _fresh_run(tmp_path, applied=[{"encryptJobId": "e1"}])
    out = tmp_path / "followup_report.json"
    _prev_followup_report(out, 100)
    before = out.read_text(encoding="utf-8")
    _collect_with(monkeypatch, run, 3, out)
    assert out.read_text(encoding="utf-8") == before, "残缺采集覆盖了全量报告（#59 主症状）"
    assert run.session.state["followup_fresh"] is False
    term = capsys.readouterr().out
    assert "[warn]" in term and "3 条" in term and "100 条" in term, term


def test_collect_fresh_followup_gradual_decay_still_overwrites(tmp_path, capsys, monkeypatch):
    """上一份 100、本场 60（≥一半）⇒ 照常覆盖：会话自然过期造成的真实衰减不能被误判成残缺，
    否则新回复永远进不了 eval（正文否决 max(0.5×prev, prev−10) 的理由）。"""
    run = _fresh_run(tmp_path, applied=[{"encryptJobId": "e1"}])
    out = tmp_path / "followup_report.json"
    _prev_followup_report(out, 100)
    report = _collect_with(monkeypatch, run, 60, out)
    assert json.loads(out.read_text(encoding="utf-8")) == report
    assert run.session.state["followup_fresh"] is True
    assert "[warn]" not in capsys.readouterr().out


def test_collect_fresh_followup_overwrites_when_previous_unreadable(tmp_path, capsys, monkeypatch):
    """上一份读不到 / 坏 JSON / conversations 非列表 ⇒ prev 视作 0，首次采集照常落盘。"""
    for label, prep in (
        ("missing", lambda p: None),
        ("bad json", lambda p: p.write_text("not json", encoding="utf-8")),
        ("not a list", lambda p: p.write_text(json.dumps({"conversations": {"a": 1}}), encoding="utf-8")),
    ):
        out = tmp_path / f"followup_report_{label.replace(' ', '_')}.json"
        prep(out)
        run = _fresh_run(tmp_path, applied=[{"encryptJobId": "e1"}])
        report = _collect_with(monkeypatch, run, 3, out)
        assert json.loads(out.read_text(encoding="utf-8")) == report, f"prev={label} 时应覆盖"
        assert run.session.state["followup_fresh"] is True, label
        assert "[warn]" not in capsys.readouterr().out, label


def test_collect_fresh_followup_zero_conversations_unchanged(tmp_path, capsys, monkeypatch):
    """0 条 ⇒ 保持既有语义（沿用上次报告 + 原文案），不因新增对比判据而改写。"""
    run = _fresh_run(tmp_path, applied=[{"encryptJobId": "e1"}])
    out = tmp_path / "followup_report.json"
    _prev_followup_report(out, 100)
    before = out.read_text(encoding="utf-8")
    _collect_with(monkeypatch, run, 0, out)
    assert out.read_text(encoding="utf-8") == before
    assert run.session.state["followup_fresh"] is False
    term = capsys.readouterr().out
    assert "采集到 0 条对话（疑似登录态过期/掉线）——沿用上次报告，不覆盖" in term, term


# --- 起飞前自检看门狗（20260925-215522 会话楔死实测：进程内 signal 不可靠，
# --- 唯一可靠看门狗在进程外——子进程查哨兵/父进程存活，超时 SIGKILL 整树） ---


def test_parse_preflight_watchdog_default_off_and_invalid():
    assert mod._parse_preflight_watchdog(None) == 180  # 未设置 → 默认 180s
    assert mod._parse_preflight_watchdog("") == 180
    assert mod._parse_preflight_watchdog("0") == 0  # 显式关闭
    assert mod._parse_preflight_watchdog("60") == 60
    assert mod._parse_preflight_watchdog("abc") == 180  # 非法回退默认
    assert mod._parse_preflight_watchdog("-5") == 180  # 负数非法回退默认


def test_watchdog_main_returns_zero_when_sentinel_appears():
    def no_marker(path, payload):
        pytest.fail("哨兵出现=自检正常完成，没击杀就不许留击杀取证")

    def no_probe(eid=None):
        pytest.fail("没击杀就不许做出网探测")

    assert (
        sg._watchdog_main(
            1,
            10,
            "/s",
            "/s/heartbeat.json",
            "/s/WATCHDOG_KILLED.json",
            exists_fn=lambda p: True,
            probe_fn=lambda pid: True,
            sleep_fn=lambda s: None,
            now_fn=lambda: 0.0,
            marker_fn=no_marker,
            net_probe_fn=no_probe,
        )
        == 0
    )


def test_watchdog_main_returns_zero_when_parent_already_dead():
    probes = []

    def probe(pid):
        probes.append(pid)
        return False  # 父进程已死，无需处置

    def no_marker(path, payload):
        pytest.fail("父进程自行退出＝不是击杀，marker 里那条记录会变成假的死因")

    def no_probe(eid=None):
        pytest.fail("没击杀就不许做出网探测")

    assert (
        sg._watchdog_main(
            99,
            10,
            "/s",
            "/s/heartbeat.json",
            "/s/WATCHDOG_KILLED.json",
            exists_fn=lambda p: False,
            probe_fn=probe,
            sleep_fn=lambda s: None,
            now_fn=lambda: 0.0,
            marker_fn=no_marker,
            net_probe_fn=no_probe,
        )
        == 0
    )
    assert probes == [99]  # 死亡探测只针对父进程，不碰进程树


def test_watchdog_main_kills_tree_leaf_first_on_timeout():
    killed = []
    clock = iter(float(x) for x in itertools.count())

    def now_fn():
        return next(clock)

    assert (
        sg._watchdog_main(
            100,
            5,
            "/s",
            "/s/heartbeat.json",
            "/s/WATCHDOG_KILLED.json",
            exists_fn=lambda p: False,  # 哨兵始终不出现
            probe_fn=lambda pid: True,  # 父进程始终存活
            kill_fn=killed.append,
            sleep_fn=lambda s: None,
            now_fn=now_fn,
            pgrep_fn=lambda pid: [200] if pid == 100 else [],  # 100 → 200 → 无子
            # 击杀取证在这条路径上真的会跑：注入件把它按在记账列表里，不碰磁盘也不碰网络
            # （mtime_fn 同——默认实现会去 stat 一个不存在的路径）。
            mtime_fn=lambda p: 0.0,
            marker_fn=lambda path, payload: None,
            net_probe_fn=lambda eid=None: None,
        )
        == 3
    )
    assert killed == [200, 100]  # 叶子在前，父进程最后


def test_watchdog_main_tolerates_kill_race():
    def kill_fn(pid):
        raise OSError("already gone")

    clock = iter(float(x) for x in itertools.count(99))
    assert (
        sg._watchdog_main(
            100,
            5,
            "/s",
            "/s/heartbeat.json",
            "/s/WATCHDOG_KILLED.json",
            exists_fn=lambda p: False,
            probe_fn=lambda pid: True,
            kill_fn=kill_fn,
            sleep_fn=lambda s: None,
            now_fn=lambda: next(clock),
            pgrep_fn=lambda pid: [],
            mtime_fn=lambda p: 0.0,
            marker_fn=lambda path, payload: None,
            net_probe_fn=lambda eid=None: None,
        )
        == 3
    )


def test_spawn_preflight_watchdog_skips_when_disabled():
    assert mod._spawn_preflight_watchdog(Path("/tmp/s"), 0, Path("/tmp/s/hb"), Path("/tmp/s/m")) is None


def test_spawn_preflight_watchdog_builds_child_command(monkeypatch):
    captured = {}

    def fake_popen(argv, cwd=None):
        captured["argv"] = argv
        captured["cwd"] = cwd
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    sdir = Path("/tmp/sessions/x")
    watcher = mod._spawn_preflight_watchdog(
        sdir / "preflight_ok", 45, sdir / fx.SESSION_HEARTBEAT, sdir / fx.WATCHDOG_KILLED
    )
    assert watcher.pid == 4242
    argv = captured["argv"]
    src = argv[2]
    assert "import _selfguard" in src and "_watchdog_main" in src  # 单一实现：子进程 import 同一函数
    # 子进程侧五个位置**全都读**：漏读 argv[4]/[5] 时子进程会当场 IndexError——父进程不 join，
    # 症状就是「静默失去看门狗」（R4-6 那批真子进程用例治的同一种坑，①这一侧也得钉）。
    assert "sys.argv[5]" in src and "sys.argv[4]" in src, src
    # 父pid/超时/哨兵路径 + 心跳与击杀取证两个路径（后两个是本次补的口：①到点也留证）
    assert argv[3:] == [
        str(os.getpid()),
        "45",
        str(sdir / "preflight_ok"),
        str(sdir / fx.SESSION_HEARTBEAT),
        str(sdir / fx.WATCHDOG_KILLED),
    ]


def test_preflight_guarded_spawns_watchdog_and_touches_sentinel(tmp_path, monkeypatch):
    session = SimpleNamespace(session_dir=tmp_path)
    spawned = []
    monkeypatch.setenv("LIVE_PREFLIGHT_WATCHDOG", "30")
    monkeypatch.setattr(
        mod,
        "_spawn_preflight_watchdog",
        lambda sentinel, timeout, heartbeat, marker: (
            spawned.append((sentinel, timeout, heartbeat, marker)) or "WATCHER"
        ),
    )
    monkeypatch.setattr(mod, "_spawn_session_watchdog", lambda *a, **k: "W")  # 隔离心跳版 spawn
    monkeypatch.setattr(mod, "_preflight_click_check", lambda page, s: True)
    assert mod._preflight_guarded(object(), session) is True
    # 取证两个路径必须来自 `_forensics` 的文件名常量（呈报侧与回灌侧读的就是这两个名字）
    assert spawned == [
        (
            tmp_path / "preflight_ok",
            30,
            tmp_path / mod.SESSION_HEARTBEAT,
            tmp_path / mod.WATCHDOG_KILLED,
        )
    ]
    assert (tmp_path / "preflight_ok").exists()  # finally 落哨兵，看门狗自行退场


def test_preflight_guarded_touches_sentinel_even_on_exception(tmp_path, monkeypatch):
    session = SimpleNamespace(session_dir=tmp_path)

    def boom(page, s):
        raise RuntimeError("protocol wedged")

    monkeypatch.setenv("LIVE_PREFLIGHT_WATCHDOG", "0")  # 关闭时不拉看门狗
    monkeypatch.setattr(mod, "_spawn_preflight_watchdog", lambda *a, **k: pytest.fail("disabled 时不得拉起"))
    monkeypatch.setattr(mod, "_spawn_session_watchdog", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_preflight_click_check", boom)
    with pytest.raises(RuntimeError):
        mod._preflight_guarded(object(), session)
    assert (tmp_path / "preflight_ok").exists()


def test_preflight_guarded_warns_when_sentinel_touch_fails(tmp_path, monkeypatch, capsys):
    # session_dir 指向只读父目录下的路径 → 心跳 write_json_atomic 与 preflight_ok touch 均
    # 失败（write_json_atomic 会 mkdir -p，故用不存在 + 只读父目录保证两层写入都炸）
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    session = SimpleNamespace(session_dir=ro / "missing" / "leaf")
    monkeypatch.setenv("LIVE_PREFLIGHT_WATCHDOG", "0")
    monkeypatch.setattr(mod, "_spawn_preflight_watchdog", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_preflight_click_check", lambda page, s: True)
    assert mod._preflight_guarded(object(), session) is True
    out = capsys.readouterr().out
    assert "哨兵写入失败" in out and "强杀" in out  # 可观测性：失败不得静默（review c-1）
    assert "心跳写入失败" in out  # 心跳侧同样必须可观测（20260928 修复点）


# --- 起飞前看门狗的击杀取证（2026-10-07 补的口）---------------------------------------------
#
# 那一场卡死发生在起飞前自检的鼠标点击上：180s 的①先到期把整树 SIGKILL，240s 的②根本没机会动手，
# 于是会话目录里只有 `wedge-stack.txt`、没有 marker、没有网络读数——「那一刻这台机器到底通不通」
# 这一格永远空着。两代看门狗在 preflight 段**本来就并存**（`_preflight_guarded` 里
# `_arm_session_watchdog(rebind=True)`），问题不是没装，是赛跑中快的那个不留证据。
# 下面这批用例钉的就是「先到期者也留证」，形态与心跳版那批一致：依赖注入、进程内直调、零 spawn。


def _run_preflight_watchdog(
    tmp_path,
    heartbeat_content,
    markers,
    killed=None,
    order=None,
    timeout=180,
    mtime=0.0,
    mtime_raises=False,
    **injections,
):
    """跑一次「起飞前看门狗到点击杀」：哨兵始终不出现、父进程始终存活、一次睡眠跨过 deadline。
    返回 rc（击杀路径恒 3），payload 落进 `markers`。`injections` 覆盖同名默认件——
    写盘件/探测件/mtime 件都要能被用例换掉，否则「取证失败照样杀」就只能靠读代码相信。
    `mtime_raises=True` 造的是「心跳文件还没被拍出来」那一态（生产里 preflight 首拍之前就是这个形态）。
    默认件全部是记账假件：这批用例既不碰磁盘也不碰网络（`LIVE_NET_PROBE` 由 conftest 默认关，
    但注入件更直接——它同时把探测的**返回值**变成可断言的东西）。"""
    hb = tmp_path / "heartbeat.json"
    if heartbeat_content is not None:
        hb.write_text(heartbeat_content, encoding="utf-8")
    clock = {"t": 0.0}

    def now_fn():
        return clock["t"]

    def sleep_fn(_seconds):
        clock["t"] += timeout + 1  # 睡过 deadline ⇒ 下一轮 while 判定即刻到期

    def mtime_fn(_path):
        if mtime_raises:
            raise OSError("heartbeat not written yet")
        return mtime

    def marker_fn(path, payload):
        if order is not None:
            order.append("marker")
        markers.append(payload)

    def kill_fn(pid):
        if killed is not None:
            killed.append(pid)
        if order is not None:
            order.append("kill")

    args = {
        "exists_fn": lambda p: False,
        "probe_fn": lambda pid: True,
        "pgrep_fn": lambda pid: [],
        "sleep_fn": sleep_fn,
        "now_fn": now_fn,
        "mtime_fn": mtime_fn,
        "kill_fn": kill_fn,
        "marker_fn": marker_fn,
        "net_probe_fn": lambda eid=None: {"injected": True},
    }
    args.update(injections)
    return sg._watchdog_main(
        100,
        timeout,
        str(tmp_path / "preflight_ok"),
        str(hb),
        str(tmp_path / fx.WATCHDOG_KILLED),
        **args,
    )


_PREFLIGHT_HB = json.dumps(
    {
        "ts": "2026-10-07T20:39:50",
        "label": "click:mouse-click:pre",
        "eid": "e1276",
        "job": "solidity 开发",
        "brand": "企贤",
        "idx": 3,
        "in_job": True,
        "handlers_live": 2,
        "handlers_inflight": 1,
        "handler_last_frag": "/wapi/zpgeek/job/detail.json",
    }
)


def test_preflight_watchdog_kill_lands_marker_with_its_own_phase(tmp_path):
    """①到点击杀现在必须落 marker，且带它这一代的身份：相位=preflight、时限=传进来的 timeout。
    `stall_seconds` 恒 None——停摆不是它的判据，把 180 塞进那个键，判读就会把它误读成
    「心跳版认为停摆了 180s」，两代的读数从此互相污染。"""
    markers = []
    probe = {"dns_ok": True, "tcp_ok": False, "error": "ConnectionRefusedError"}
    rc = _run_preflight_watchdog(tmp_path, _PREFLIGHT_HB, markers, net_probe_fn=lambda eid=None: probe)
    assert rc == 3
    assert len(markers) == 1, "一次击杀只落一份 marker"
    data = markers[0]
    assert data[fx.MARKER_PHASE_KEY] == fx.MARKER_PHASE_PREFLIGHT
    assert data[fx.MARKER_TIMEOUT_KEY] == 180
    assert data["stall_seconds"] is None
    assert data["stale_seconds"] == 181, "实测停摆按 mtime 算出（mtime=0、睡眠后 now=181），不是猜的阈值"
    assert data["last_label"] == "click:mouse-click:pre"
    assert (data["last_eid"], data["last_job"], data["last_brand"], data["in_job"]) == (
        "e1276",
        "solidity 开发",
        "企贤",
        True,
    ), "身份五键取自注入的心跳"
    assert (data["handlers_live"], data["handlers_inflight"], data["handler_last_frag"]) == (
        2,
        1,
        "/wapi/zpgeek/job/detail.json",
    ), "镜像三键同理（取证 A 的读数是定性唯一的正面依据）"
    assert data[fx.NET_PROBE_MARKER_KEY] is probe, "marker 里就是注入探测件的返回值"


def test_preflight_watchdog_writes_marker_before_killing(tmp_path):
    """顺序：写 marker 必须发生在 SIGKILL **之前**——这顺序就是本次改动的全部价值。
    杀完之后再写是不可能的：整棵树（含被楔住的父进程与其浏览器）已经没了，能问出「此刻通不通」
    的窗口只有判定完成后、动手前那几秒。"""
    markers, order = [], []
    _run_preflight_watchdog(tmp_path, _PREFLIGHT_HB, markers, order=order)
    assert order == ["marker", "kill"]


def test_preflight_watchdog_marker_failure_still_kills(tmp_path):
    """取证写盘失败（目录不存在/磁盘满）不许把止损一起带走：仍然击杀、仍然返回 3。
    降级铁律在两代上是同一条——心跳版早已有同款用例，①接了取证就得一起接这条。"""
    killed = []

    def boom(path, payload):
        raise OSError("disk full")

    rc = _run_preflight_watchdog(tmp_path, _PREFLIGHT_HB, [], killed=killed, marker_fn=boom)
    assert rc == 3 and killed == [100]


def test_preflight_watchdog_probe_failure_does_not_change_the_kill(tmp_path):
    """探测件抛异常 → `net_probe` 落 None，marker 其余字段照写、击杀照做（读的人分不清
    「没探」与「探炸」时，至少身份与相位还在）。"""

    def boom(eid=None):
        raise RuntimeError("probe exploded")

    markers = []
    rc = _run_preflight_watchdog(tmp_path, _PREFLIGHT_HB, markers, net_probe_fn=boom)
    assert rc == 3
    assert markers[0][fx.NET_PROBE_MARKER_KEY] is None
    assert markers[0]["last_eid"] == "e1276" and markers[0]["last_label"] == "click:mouse-click:pre"


def test_preflight_watchdog_without_readable_heartbeat_still_writes_marker(tmp_path):
    """心跳读不出（①的常见形态：还没拍到那一拍、或文件消失）与坏 JSON 两种，marker 都照写：
    身份五键与镜像三键全 None，击杀照做。区别只在 `stale_seconds`——文件读不到＝没有实测停摆
    （落 -1），坏 JSON 但文件在＝停摆秒数照常按 mtime 算。前者是②的老口径，一字未改。"""
    for content, mtime_raises, expected_stale in ((None, True, -1), ("{not json", False, 181)):
        markers = []
        rc = _run_preflight_watchdog(tmp_path, content, markers, mtime_raises=mtime_raises)
        assert rc == 3, content
        data = markers[0]
        assert data["stale_seconds"] == expected_stale, (content, data)
        assert all(data[k] is None for k in fx.MARKER_IDENTITY_KEYS), content
        assert all(data[k] is None for k in fx.MARKER_HB_MIRROR), content
        assert data[fx.MARKER_PHASE_KEY] == fx.MARKER_PHASE_PREFLIGHT, "读不出心跳也不能丢相位"


def test_both_watchdog_generations_write_the_same_marker_keys(tmp_path):
    """形状对称（读侧安全的**根**）：`live_apply._surface_prior_wedge` 与
    `_wedge_ledger._wedge_entry_from_marker` 都按固定键读、不做形态猜测——一代少写一个键，
    下一场的读法就静默缺一格（B1 整场不入账、呈报少报一项），不报错。
    这里比的是两代各自跑完真实 payload 构造后的**键集合**，而不是两份手抄的期望清单：
    清单抄两份，漂移就被写进了测试自己肚子里。"""
    pre, sess = [], []
    _run_preflight_watchdog(tmp_path, _PREFLIGHT_HB, pre, net_probe_fn=lambda eid=None: None)
    _run_stale_watchdog(sg, tmp_path, _PREFLIGHT_HB, sess, net_probe_fn=lambda eid=None: None)
    assert set(pre[0]) == set(sess[0]), set(pre[0]) ^ set(sess[0])
    # 两格取值按各自的判据分道（这是唯一允许不同的地方），其余键的口径两代同源
    assert (pre[0][fx.MARKER_PHASE_KEY], sess[0][fx.MARKER_PHASE_KEY]) == (
        fx.MARKER_PHASE_PREFLIGHT,
        fx.MARKER_PHASE_SESSION,
    )
    assert pre[0]["stall_seconds"] is None and sess[0]["stall_seconds"] == 5
    assert pre[0][fx.MARKER_TIMEOUT_KEY] == 180 and sess[0][fx.MARKER_TIMEOUT_KEY] is None
    assert pre[0]["last_label"] == sess[0]["last_label"] == "click:mouse-click:pre"


def test_preflight_watchdog_net_probe_switch_off_touches_no_socket(tmp_path, monkeypatch):
    """①现在也探测 ⇒ 它同样归 `LIVE_NET_PROBE` 管：这里**不注入**探测件，走 `_default_net_probe`
    真身，把 `sg.socket` 换成会喊的假件——关掉时连这个名字都不许被碰，marker 里那个键恒在但为 None。
    （conftest 的 autouse 只保证默认关；这条钉的是「①真的听了这个开关」。）"""
    monkeypatch.setenv(fx.NET_PROBE_ENV, "off")
    monkeypatch.setattr(
        sg,
        "socket",
        SimpleNamespace(
            getaddrinfo=lambda *a: pytest.fail("关掉时不许碰 socket"),
            create_connection=lambda *a: pytest.fail("关掉时不许碰 socket"),
        ),
    )
    markers = []
    rc = _run_preflight_watchdog(tmp_path, _PREFLIGHT_HB, markers, net_probe_fn=None)
    assert rc == 3
    assert markers[0][fx.NET_PROBE_MARKER_KEY] is None


def test_preflight_kill_marker_books_no_job_into_the_skip_ledger(tmp_path, monkeypatch):
    """①落的 marker 从此会被下一场的读盘看见，所以必须验一遍归因门槛：起飞前自检点的是列表首张
    卡片、根本不在任何岗位里，真心跳就是 `in_job=False` / `eid=None`（2026-10-07 实测那一场正是这副
    形状）⇒ B1 跳岗台账**不许**入账。把「起飞前那一次点击」记成某个岗的罪，代价是永久跳掉一个
    无辜的达标岗——比少跳一个岗重得多。这里走真写盘器 + 真心跳，中间没有测试自己拼的字段。"""
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    monkeypatch.setattr(fx, "_HEARTBEAT_JOB", None)
    assert fx._touch_heartbeat("click:mouse-click:pre") is True
    hb_content = (tmp_path / fx.SESSION_HEARTBEAT).read_text(encoding="utf-8")

    rc = _run_preflight_watchdog(tmp_path, hb_content, [], marker_fn=sg._write_marker)
    assert rc == 3
    # 默认注入件必须**真的被消费到**：它一旦被 arity 漂移打成哑件，异常会被 `_write_kill_evidence` 的
    # 降级分支吞掉、marker 落 `net_probe=None`，而那条分支既有的读法是「开关关过 / 早于取证 A/B」——
    # 死掉的假件就此冒充成一种合法现场，全绿（#47 宁可显眼不可静默）。
    assert json.loads((tmp_path / fx.WATCHDOG_KILLED).read_text(encoding="utf-8"))[fx.NET_PROBE_MARKER_KEY] == {
        "injected": True
    }, "默认注入件失效＝假件静默变哑，读数会冒充「没探测」"
    data = json.loads((tmp_path / fx.WATCHDOG_KILLED).read_text(encoding="utf-8"))
    assert data[fx.MARKER_PHASE_KEY] == fx.MARKER_PHASE_PREFLIGHT
    assert data[fx.MARKER_IN_JOB] is False and data[fx.MARKER_LAST_EID] is None
    assert mod._wedge_entry_from_marker("20261007-203217", data) is None, "起飞前击杀不该背任何岗"


# --- 数据保留清理（2026-09-26 用户约定：启动时清理超期视频与截图，默认 7 天） ---


def test_sweep_removes_expired_video_and_shots_keeps_reports(tmp_path):
    video_base = tmp_path / "videos"
    session_base = tmp_path / "live_session"
    old_ts, new_ts = "20260901-000000", "20260925-225531"
    (video_base / old_ts).mkdir(parents=True)
    (video_base / new_ts).mkdir(parents=True)
    (session_base / old_ts / "shots").mkdir(parents=True)
    (session_base / old_ts / "report.json").write_text("{}", encoding="utf-8")
    (session_base / new_ts / "shots").mkdir(parents=True)
    now = datetime(2026, 9, 26, 12, 0, 0)
    removed = mod.sweep_expired_data(video_base, session_base, 7, now)
    assert not (video_base / old_ts).exists()  # 超期视频整目录删除
    assert (video_base / new_ts).exists()  # 7 天内保留
    assert not (session_base / old_ts / "shots").exists()  # 超期截图删除
    assert (session_base / old_ts / "report.json").exists()  # 会话其余产物（报告/队列）保留
    assert (session_base / new_ts / "shots").exists()
    assert len(removed) == 2 and all(old_ts in p for p in removed)


def test_sweep_boundary_exactly_seven_days_is_kept(tmp_path):
    video_base = tmp_path / "videos"
    (video_base / "20260919-120000").mkdir(parents=True)
    mod.sweep_expired_data(video_base, tmp_path / "s", 7, datetime(2026, 9, 26, 12, 0, 0))
    assert (video_base / "20260919-120000").exists()  # 「超过 7 天」严格大于才删


def test_sweep_ignores_non_ts_names_and_zero_disables(tmp_path):
    video_base = tmp_path / "videos"
    (video_base / "notats").mkdir(parents=True)
    (video_base / "20260901-000000").mkdir(parents=True)
    removed = mod.sweep_expired_data(video_base, tmp_path / "s", 0, datetime(2026, 9, 26))
    assert (video_base / "notats").exists()  # 非 ts 名一律不碰
    assert (video_base / "20260901-000000").exists()  # 0 = off
    assert removed == []
    assert mod._parse_retention_days(None) == 7  # 未设置 → 默认 7
    assert mod._parse_retention_days("abc") == 7  # 非法回退默认
    assert mod._parse_retention_days("14") == 14


# --- #54 浏览器退出原因分类（启动期 pid 采集 + os.kill 探活 + DiagnosticReports 检索） ---
# 验收 5：分类判定函数 TDD 覆盖——进程存活/崩溃报告匹配用替身桩，不发真进程。


def _dead_kill(pid, sig):
    raise ProcessLookupError()


def test_browser_closed_message_regex():
    """触发面：Playwright 关页/关浏览器族消息命中，无关异常（超时/网络）不命中。"""
    assert mod.BROWSER_CLOSED_RE.search("Page.goto: Target page, context or browser has been closed")
    assert mod.BROWSER_CLOSED_RE.search("Browser has been closed")
    assert mod.BROWSER_CLOSED_RE.search("context has been closed")
    assert not mod.BROWSER_CLOSED_RE.search("Timeout 30000ms exceeded")
    assert not mod.BROWSER_CLOSED_RE.search("net::ERR_CONNECTION_CLOSED at localhost")


def test_note_browser_exit_first_wins_unrelated_ignored(tmp_path, monkeypatch):
    """首次 browser-closed 异常才分类（首见为准不覆盖）；无关异常不写。"""
    monkeypatch.setattr(mod, "classify_browser_exit", lambda pids: ("external_kill", {"pids": pids}))
    state = {"browser_pids": [4242]}
    now = datetime(2026, 9, 27, 10, 0, 0)
    assert mod.note_browser_exit(state, "Timeout 30000ms exceeded", now=now) is False
    assert "browser_exit" not in state, "无关异常不触发分类"
    assert mod.note_browser_exit(state, "Page.goto: Target page, context or browser has been closed", now=now) is True
    first = state["browser_exit"]
    assert first["classification"] == "external_kill"
    assert first["detected_at"] == "2026-09-27T10:00:00"
    assert mod.note_browser_exit(state, "browser has been closed", now=datetime(2026, 9, 27, 10, 5)) is False
    assert state["browser_exit"] is first, "只记首次，后续同类异常不覆盖"


def test_classify_browser_exit_alive_when_probe_succeeds():
    cls, ev = mod.classify_browser_exit([4242, 4243], kill_fn=lambda pid, sig: None, crash_fn=lambda: None)
    assert cls == "browser_alive", "进程仍活 → 页面级异常（页面/context 被关，浏览器还在）"
    assert ev["alive"] == [4242, 4243]


def test_classify_browser_exit_crash_suspected_with_report_path():
    report = Path("/Library/Logs/DiagnosticReports/camoufox-2026-09-27-095500.ips")
    cls, ev = mod.classify_browser_exit([4242], kill_fn=_dead_kill, crash_fn=lambda: report)
    assert cls == "crash_suspected"
    assert ev["crash_report"] == str(report), "证据须指向 DiagnosticReports 文件路径"


def test_classify_browser_exit_external_kill_without_report():
    cls, ev = mod.classify_browser_exit([4242], kill_fn=_dead_kill, crash_fn=lambda: None)
    assert cls == "external_kill", "进程已死且无崩溃报告 → 外部终结（待确认）"
    assert ev["pids"] == [4242] and ev["crash_report"] is None


def test_classify_browser_exit_unknown_without_pids():
    cls, ev = mod.classify_browser_exit([], kill_fn=_dead_kill, crash_fn=lambda: None)
    assert cls == "unknown", "启动期未采集到 pid 时如实报不可分类，不猜"
    assert ev["pids"] == []


def test_probe_alive_eperm_counts_as_alive():
    """EPERM = 进程存在但属主不同（macOS 常见），视为存活而非已死。"""

    def denied(pid, sig):
        raise PermissionError()

    assert mod._probe_alive(4242, kill_fn=denied) is True
    assert mod._probe_alive(4242, kill_fn=_dead_kill) is False


def test_find_crash_report_recent_matching_ips_only(tmp_path):
    """DiagnosticReports 检索：只认近 N 分钟内、文件名含浏览器二进制、.ips 后缀；取最新。"""
    d = tmp_path / "DiagnosticReports"
    d.mkdir()
    now = datetime(2026, 9, 27, 10, 0, 0)

    def touch(name, age_min):
        f = d / name
        f.write_text("{}", encoding="utf-8")
        stamp = (now.timestamp() - age_min * 60, now.timestamp() - age_min * 60)
        os.utime(f, stamp)
        return f

    touch("camoufox-2026-09-27-090000.ips", 30)  # 超窗 → 不算
    touch("python-2026-09-27-095800.ips", 2)  # 非浏览器二进制 → 不算
    touch("camoufox-2026-09-27-095800.log", 2)  # 非 .ips → 不算
    older = touch("camoufox-2026-09-27-095000.ips", 10)
    newest = touch("camoufox-2026-09-27-095500.ips", 5)
    assert mod.find_crash_report(now=now, report_dir=d) == newest
    assert mod.find_crash_report(now=now, report_dir=d, window_min=3) is None, "窗口收窄后超窗不报"
    assert mod.find_crash_report(now=now, report_dir=tmp_path / "missing") is None, "目录缺失（非 macOS/CI）优雅降级"
    assert older  # noqa: F841 — 占位防 lint 报未用（older 仅为可读性保留）


def test_discover_browser_pids_walks_tree_two_hops():
    """进程树两跳：python(self) → node driver → camoufox/firefox。"""
    calls = []

    def pgrep(parent, pattern):
        calls.append((parent, pattern))
        return {1: [100], 100: [4242]}.get(parent, [])

    assert mod.discover_browser_pids(self_pid=1, pgrep_fn=pgrep) == [4242]
    assert (100, "camoufox|firefox") in calls, "第二跳按浏览器二进制名过滤 node 的子进程"


def test_record_browser_pids_stores_state_and_degrades(tmp_path, capsys):
    """启动期采集：成功存 state；pgrep 不可用/零命中降级为空表 + warn，绝不阻断投递。"""
    session, _ = _fake_session(tmp_path)
    mod._record_browser_pids(session, discover_fn=lambda: [4242])
    assert session.state["browser_pids"] == [4242]
    assert "4242" in capsys.readouterr().out
    session2, _ = _fake_session(tmp_path)

    def boom():
        raise RuntimeError("pgrep missing")

    mod._record_browser_pids(session2, discover_fn=boom)
    assert session2.state["browser_pids"] == []
    assert "[warn]" in capsys.readouterr().out


def test_build_report_browser_exit_passthrough_and_none_default():
    entry = {"classification": "external_kill", "detected_at": "t", "evidence": {"pids": [1]}}
    state = {"applied": 0, "direct_fallback": 0, "keyword_skips": [], "browser_exit": entry}
    assert mod.build_report(["q"], state, [])["browser_exit"] == entry
    base = {"applied": 0, "direct_fallback": 0, "keyword_skips": []}
    assert mod.build_report(["q"], base, [])["browser_exit"] is None, "None = 会话内未发生浏览器关闭类异常"


def test_write_session_report_done_line_carries_classification(tmp_path, monkeypatch, capsys):
    """验收 1：report.json 顶层 browser_exit + 收尾 [done] 行带分类描述，读报告者不必再猜。"""
    monkeypatch.setattr(mod, "edd_wrapup", _EDD_STUB)
    monkeypatch.setattr(mod.g, "resolve_paths", lambda env=None: {"name": "p"})
    state = {
        "applied": 0,
        "direct_fallback": 0,
        "keyword_skips": [],
        "browser_exit": {"classification": "external_kill", "detected_at": "t", "evidence": {"pids": [4242]}},
    }
    run = _report_run(tmp_path, state=state)
    mod._write_session_report(run)
    out = capsys.readouterr().out
    assert "浏览器退出" in out and "外部终结" in out
    data = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert data["browser_exit"]["classification"] == "external_kill"


def test_apply_one_job_browser_closed_notes_exit(tmp_path, monkeypatch):
    """接线：单岗级异常边界（09-26 事故现场）遇 browser-closed 即分类。"""
    monkeypatch.setattr(mod, "classify_browser_exit", lambda pids: ("external_kill", {"pids": pids}))
    monkeypatch.setattr(mod, "safe_go_back", lambda page: None)

    def boom(*a, **k):
        raise RuntimeError("Page.goto: Target page, context or browser has been closed")

    monkeypatch.setattr(mod, "apply_live", boom)
    session, _ = _fake_session(
        tmp_path, state={"applied": 0, "direct_fallback": 0, "keyword_skips": [], "browser_pids": [4242]}
    )
    assert mod._apply_one_job("P", {"jobName": "岗"}, 1, session) is None
    assert [r["status"] for r in session.results] == ["page_error"], "既有记账不回退"
    assert session.state["browser_exit"]["classification"] == "external_kill"


def test_keyword_round_browser_closed_notes_exit(tmp_path, monkeypatch):
    """接线：关键词级异常边界同样触发分类。"""

    def boom(page, query, **kw):
        raise RuntimeError("Target page, context or browser has been closed")

    monkeypatch.setattr(mod, "search_list", boom)
    monkeypatch.setattr(mod, "classify_browser_exit", lambda pids: ("browser_alive", {"pids": pids}))
    state = {"applied": 0, "direct_fallback": 0, "keyword_skips": [], "browser_pids": [4242]}
    assert mod.keyword_round("P", "区块链", state, set()) is None
    assert state["keyword_skips"], "既有记账不回退"
    assert state["browser_exit"]["classification"] == "browser_alive"


def test_recommend_round_browser_closed_notes_exit(tmp_path, monkeypatch):
    """接线：推荐流轮异常边界同样触发分类。"""

    def boom(page, **kw):
        raise RuntimeError("browser has been closed")

    monkeypatch.setattr(mod, "recommend_list", boom)
    monkeypatch.setattr(mod, "classify_browser_exit", lambda pids: ("external_kill", {"pids": pids}))
    session, _ = _fake_session(
        tmp_path, state={"applied": 0, "direct_fallback": 0, "keyword_skips": [], "browser_pids": [4242]}
    )
    run = SimpleNamespace(session=session, collected=set(), applied_ids=set(), limit=10)
    stop, idx = mod._recommend_round("P", run, 0)
    assert stop is False and idx == 0
    assert session.state["recommend_errors"], "既有记账不回退"
    assert session.state["browser_exit"]["classification"] == "external_kill"


def test_collect_fresh_followup_browser_closed_notes_exit(tmp_path, monkeypatch):
    """接线：收尾现场采集失败边界同样触发分类（浏览器死在收尾前）。"""
    session, _ = _fake_session(
        tmp_path, state={"applied": 0, "direct_fallback": 0, "keyword_skips": [], "browser_pids": [4242]}
    )
    session.cfg["history_path"] = str(tmp_path / "h.json")
    (tmp_path / "h.json").write_text(json.dumps({"applied": []}), encoding="utf-8")

    def boom(page, applied, **kw):
        raise RuntimeError("browser has been closed")

    session.platform = SimpleNamespace(save_evidence=lambda *a, **k: None, followup=boom)
    monkeypatch.setattr(mod, "classify_browser_exit", lambda pids: ("external_kill", {"pids": pids}))
    run = SimpleNamespace(env={}, session=session)
    mod._collect_fresh_followup(run, "P")
    assert session.state["followup_fresh"] is False, "既有降级口径不回退"
    assert session.state["browser_exit"]["classification"] == "external_kill"


# --- #55 部分二/三：断点台账 retry_pending.json 与两层补投 ---
#
# 同会话补投（队列耗尽后直跳重试 1 次）+ 跨会话台账（attempts 累计封顶 3）。
# 验收 4：补投不绕过 excluded/日上限/风控停批；dry-run 不写台账不补投。


def _timeout_job(eid="job1", name="区块链开发", brand="某基金"):
    return {
        "encryptJobId": eid,
        "jobName": name,
        "brandName": brand,
        "salaryDesc": "20-40K",
        "cityName": "上海",
        "bossName": "周某",
        "bossTitle": "招聘者",
    }


def _retry_run(tmp_path, state=None, excluded=()):
    base_state = {"applied": 0, "direct_fallback": 0, "keyword_skips": []}
    base_state.update(state or {})
    session, calls = _fake_session(tmp_path, state=base_state)
    session.cfg.update({"retry_pending_path": str(tmp_path / "retry_pending.json")})
    run = SimpleNamespace(session=session, excluded=list(excluded), limit=10)
    return session, run


def test_retry_snapshot_self_contained():
    """台账条目自包含：岗位字段 + jdText 快照 + timeout_at + attempts，不依赖会话目录存活。"""
    snap = mod._retry_snapshot(_timeout_job(), attempts=2, jd_text="JD 全文")
    assert snap["encryptJobId"] == "job1" and snap["jdText"] == "JD 全文"
    assert snap["attempts"] == 2 and snap["timeout_at"]
    for k in ("jobName", "brandName", "salaryDesc", "cityName", "bossName", "bossTitle"):
        assert snap[k], f"快照字段 {k} 须从岗位字典带出"


def test_request_jd_text_reads_request_file(tmp_path):
    mod.request_greeting(tmp_path, 7, _timeout_job(), "JD 全文X")
    assert mod._request_jd_text(tmp_path, 7) == "JD 全文X"
    assert mod._request_jd_text(tmp_path, 99) == "", "request 文件缺失返回空串不炸"


def test_apply_retry_outcome_four_paths():
    """验收 3：投出/agent 跳过/已沟通过 → 移出；再超时 attempts+1 保留；封顶移出。
    #56 A5③ 变更：旧断言 `apply_retry_outcome(entry, "no_jd") == (False, "no_jd")`（注释写着
    「详情不可达 = 岗位已失效」）撤销——那是给静默吞异常扣的定性帽子，实际把站点抖动造成的
    no_jd 从补投队列里静默移除。no_jd 现在按归因类分流（见下一条用例），本用例只锁其余路径，
    故把 no_jd 移出原位、改用独立 entry，不让它扰动既有的 attempts 递增算式。"""
    entry = {"attempts": 0}
    assert mod.apply_retry_outcome(entry, "applied") == (False, "applied")
    assert mod.apply_retry_outcome(entry, "agent_skip") == (False, "agent_skip")
    assert mod.apply_retry_outcome(entry, "skipped") == (False, "skipped"), "已沟通过 → 移出"
    assert mod.apply_retry_outcome(entry, "agent_timeout") == (True, "kept"), "再超时 → attempts+1 保留"
    assert entry["attempts"] == 1
    mod.apply_retry_outcome(entry, "no_btn")
    assert entry["attempts"] == 2, "其余失败态同样消耗 attempts（封顶兜底防无限重试）"
    assert mod.apply_retry_outcome(entry, "agent_timeout") == (False, "attempts_cap")
    assert entry["attempts"] == mod.RETRY_ATTEMPTS_CAP
    # 归因类读不到（桩结果条目 / 旧台账 / 容器形态诡异）时的保守方向：保留而不是丢岗
    no_class = {"attempts": 0}
    assert mod.apply_retry_outcome(no_class, "no_jd") == (True, "kept"), "无归因类 → 保守保留"
    assert no_class["attempts"] == 1, "保守保留同样走既有封顶路径，不会无限重试"
    # #62 依赖但本票未改动的一条契约（钉住而非新行为，故基线上也绿）：off_chat 岗入台账后，
    # 下一场若仍落 `no_send`（守卫又拦下／发送段又落空），走「其余失败态」分支——attempts+1
    # 保留、封顶才移出；台账不会把它静默丢掉，否则 #62 的「下一场捞回」承诺是空的。
    off_chat = {"attempts": 0}
    assert mod.apply_retry_outcome(off_chat, "no_send") == (True, "kept"), "no_send 保留（off_chat 岗靠它捞回）"
    assert off_chat["attempts"] == 1


def test_load_save_retry_pending_roundtrip(tmp_path, capsys):
    path = tmp_path / "retry_pending.json"
    entries = [mod._retry_snapshot(_timeout_job(), attempts=1, jd_text="JD")]
    mod._save_retry_pending(path, entries)
    assert mod._load_retry_pending(path) == entries
    assert mod._load_retry_pending(tmp_path / "missing.json") == []
    path.write_text("{bad", encoding="utf-8")
    assert mod._load_retry_pending(path) == [], "坏形态回落空列表 + warn（#51 reported 同款自愈）"
    assert "[warn]" in capsys.readouterr().out


def test_acquire_timeout_records_session_retry(tmp_path, monkeypatch, capsys):
    """正常轮次 agent_timeout → 会话内补投列表（含 request_idx）；结果条目补 encryptJobId（验收 5）。"""
    session, run = _retry_run(tmp_path)
    monkeypatch.setattr(mod, "grab_detail", lambda page, detail, **kw: "JD 全文")
    monkeypatch.setattr(mod, "wait_reply", lambda *a, **k: None)
    monkeypatch.setattr(mod, "shot", lambda *a, **k: None)
    monkeypatch.setattr(mod, "safe_go_back", lambda page: None)
    flow = mod._JobFlow(page="P", job=_timeout_job(), idx=4, ctx=session)
    assert mod._acquire_and_draft_greeting(flow) == "next"
    assert [r["status"] for r in session.results] == ["agent_timeout"]
    assert session.results[0].get("encryptJobId") == "job1", "验收 5：报告条目补 encryptJobId"
    entry = session.state["session_retries"][0]
    assert entry["job"]["encryptJobId"] == "job1" and entry["request_idx"] == 4
    assert "补投" in capsys.readouterr().out


def test_retry_pass_timeout_goes_to_pending(tmp_path):
    """补投轮（retry_pass）再超时 → 落台账暂存（attempts=0），不回会话补投列表防循环。"""
    session, run = _retry_run(tmp_path)
    session.state["retry_pass"] = True
    mod.request_greeting(session.session_dir, 4, _timeout_job(), "JD 快照")
    mod._record_agent_timeout(SimpleNamespace(ctx=session, job=_timeout_job(), idx=4))
    assert session.state.get("session_retries", []) == [], "补投轮超时不得再次进入会话补投列表"
    entry = session.state["retry_pending"][0]
    assert entry["attempts"] == 0 and entry["jdText"] == "JD 快照"
    assert session.state["retry_touched"] is True


def test_retry_session_jobs_runs_and_requeues(tmp_path, monkeypatch):
    """验收 2：队列耗尽后超时岗位被直跳补投一次（idx 续号）；再超时 → 进台账。"""
    session, run = _retry_run(tmp_path)
    session.state["session_retries"] = [{"job": _timeout_job(), "request_idx": 4}]
    session.state["last_idx"] = 4

    def fake_apply(page, job, idx, session):
        mod._record_agent_timeout(SimpleNamespace(ctx=session, job=job, idx=idx))  # 复现真实超时记账
        session.results.append({"idx": idx, "job": job.get("jobName"), "status": "agent_timeout"})
        return None

    monkeypatch.setattr(mod, "_apply_one_job", fake_apply)
    mod._retry_session_jobs("P", run)
    entry = session.state["retry_pending"][0]
    assert entry["attempts"] == 0 and entry["encryptJobId"] == "job1"
    assert session.state["retry_pass"] is False, "补投轮结束必须复位标记"
    assert session.state["last_idx"] == 5


def test_retry_session_jobs_honors_stop(tmp_path, monkeypatch):
    """验收 4：停批信号（日限/熔断/CLOSE）下剩余岗位不重试，直接落台账兜底——不绕过门禁。"""
    session, run = _retry_run(tmp_path)
    session.state["session_retries"] = [
        {"job": _timeout_job("job1"), "request_idx": 1},
        {"job": _timeout_job("job2"), "request_idx": 2},
    ]
    monkeypatch.setattr(mod, "_pre_job_stop", lambda s, r: True)
    mod._retry_session_jobs("P", run)
    assert [e["encryptJobId"] for e in session.state["retry_pending"]] == ["job1", "job2"]
    assert session.state["retry_touched"] is True


def test_retry_session_jobs_skips_excluded(tmp_path, monkeypatch):
    """验收 4：品牌已进 excluded_companies 的超时岗位不补投也不进台账（用户决定不投的不再骚扰）。"""
    session, run = _retry_run(tmp_path, excluded=("某基金",))
    session.state["session_retries"] = [{"job": _timeout_job(brand="某基金"), "request_idx": 1}]
    monkeypatch.setattr(mod, "_apply_one_job", lambda *a, **k: pytest.fail("excluded 品牌不得补投"))
    mod._retry_session_jobs("P", run)
    assert session.state.get("retry_pending", []) == []


def test_retry_session_jobs_dry_run_noop(tmp_path, capsys):
    """验收 4：dry-run 会话不补投（记账仅存内存，不进台账不发起重试）。"""
    session, run = _retry_run(tmp_path, state={"dry_run": True})
    session.state["session_retries"] = [{"job": _timeout_job(), "request_idx": 1}]
    mod._retry_session_jobs("P", run)
    assert session.state.get("retry_pending", []) == []
    assert not session.state.get("retry_pass"), "dry-run 不进入补投轮标记"
    assert "dry-run 会话不补投" in capsys.readouterr().out


def test_retry_pending_jobs_cross_session_paths(tmp_path, monkeypatch):
    """验收 3 跨会话：投出移出落盘、再超时 attempts+1 保留落盘、reprocessed 计数。"""
    session, run = _retry_run(tmp_path)
    path = Path(session.cfg["retry_pending_path"])
    mod._save_retry_pending(
        path,
        [
            mod._retry_snapshot(_timeout_job("job1"), jd_text=""),
            mod._retry_snapshot(_timeout_job("job2", name="岗2"), jd_text=""),
        ],
    )

    def fake_apply(page, job, idx, session):
        status = "applied" if job["encryptJobId"] == "job1" else "agent_timeout"
        session.results.append({"idx": idx, "job": job.get("jobName"), "status": status})
        return None

    monkeypatch.setattr(mod, "_apply_one_job", fake_apply)
    assert mod._retry_pending_jobs("P", run) is False
    on_disk = mod._load_retry_pending(path)
    assert [e["encryptJobId"] for e in on_disk] == ["job2"], "job1 投出移出，job2 再超时保留"
    assert on_disk[0]["attempts"] == 1
    assert session.state["retry_reprocessed"] == 2
    assert session.state["retry_pass"] is False


def test_retry_pending_jobs_cap_removes_with_warn(tmp_path, monkeypatch, capsys):
    """验收 3：attempts 达封顶 → 移出并告警，不再保留。"""
    session, run = _retry_run(tmp_path)
    path = Path(session.cfg["retry_pending_path"])
    mod._save_retry_pending(path, [mod._retry_snapshot(_timeout_job(), attempts=2, jd_text="")])
    monkeypatch.setattr(
        mod,
        "_apply_one_job",
        lambda p, j, i, s: (
            mod._record_agent_timeout(SimpleNamespace(ctx=s, job=j, idx=i)),
            s.results.append({"idx": i, "status": "agent_timeout"}),
        ),
    )
    mod._retry_pending_jobs("P", run)
    assert mod._load_retry_pending(path) == []
    out = capsys.readouterr().out
    assert "封顶" in out and "[warn" in out


def test_retry_pending_jobs_excluded_brand_removed(tmp_path, monkeypatch):
    """验收 3：品牌已进排除清单 → 直接移出（不补投）。"""
    session, run = _retry_run(tmp_path, excluded=("某基金",))
    path = Path(session.cfg["retry_pending_path"])
    mod._save_retry_pending(path, [mod._retry_snapshot(_timeout_job(brand="某基金"), jd_text="")])
    monkeypatch.setattr(mod, "_apply_one_job", lambda *a, **k: pytest.fail("excluded 品牌不得补投"))
    mod._retry_pending_jobs("P", run)
    assert mod._load_retry_pending(path) == []


def test_retry_pending_jobs_dry_run_skipped(tmp_path):
    """验收 4：dry-run 会话不消费台账（磁盘原样）、不补投，返回 False 放行后续搜索。"""
    session, run = _retry_run(tmp_path, state={"dry_run": True})
    path = Path(session.cfg["retry_pending_path"])
    entries = [mod._retry_snapshot(_timeout_job(), jd_text="")]
    mod._save_retry_pending(path, entries)
    assert mod._retry_pending_jobs("P", run) is False
    assert mod._load_retry_pending(path) == entries, "台账未被消费"
    assert session.results == []


def test_retry_pending_jobs_stop_signal_keeps_rest(tmp_path, monkeypatch):
    """停批信号 → 剩余台账条目原样保留（attempts 不动），返回 True 让主流程跳过新搜索。"""
    session, run = _retry_run(tmp_path)
    path = Path(session.cfg["retry_pending_path"])
    entries = [
        mod._retry_snapshot(_timeout_job("job1"), jd_text=""),
        mod._retry_snapshot(_timeout_job("job2"), jd_text=""),
    ]
    mod._save_retry_pending(path, entries)

    calls = {"n": 0}

    def stop_after_first(s, r):
        calls["n"] += 1
        return calls["n"] > 1  # 第一个岗位处理完，第二个开工前停批

    monkeypatch.setattr(mod, "_pre_job_stop", stop_after_first)

    def fake_apply(page, job, idx, session):
        session.results.append({"idx": idx, "status": "applied"})
        return None

    monkeypatch.setattr(mod, "_apply_one_job", fake_apply)
    assert mod._retry_pending_jobs("P", run) is True
    on_disk = mod._load_retry_pending(path)
    assert [e["encryptJobId"] for e in on_disk] == ["job2"], "job1 已投出移出；job2 停批未获补投，原样保留"
    assert on_disk[0]["attempts"] == 0


def test_run_live_retry_round_wiring(tmp_path, monkeypatch):
    """接线：跨会话补投先于新岗位搜索；同会话补投在队列耗尽后、收尾采集前（#55 验收 2）。"""
    order = []
    state = {"applied": 0, "direct_fallback": 0, "keyword_skips": []}
    session = SimpleNamespace(session_dir=tmp_path, shots_dir=tmp_path, cfg={}, state=state)
    run = SimpleNamespace(
        queries=[],
        env={},
        ts="t",
        video_base=tmp_path,
        video_kwargs={},
        record=False,
        session=session,
        applied_ids=set(),
        excluded=set(),
        limit=10,
        browser_dir=str(tmp_path),
    )
    monkeypatch.setattr(mod, "_init_live_session", lambda env: run)

    class _Ctx:
        def __enter__(self):
            return SimpleNamespace(new_page=lambda: "P")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mod, "Camoufox", lambda **kw: _Ctx())
    for name in (
        "_retry_pending_jobs",
        "_execute_query_rounds",
        "_retry_session_jobs",
        "_collect_fresh_followup",
        "_write_session_report",
        "_hold_browser_until_closed",
        "_finalize_recording",
    ):
        monkeypatch.setattr(mod, name, (lambda n: lambda *a, **k: order.append(n) or False)(name))
    assert mod.run_live({}) == 0
    assert order == [
        "_retry_pending_jobs",
        "_execute_query_rounds",
        "_retry_session_jobs",
        "_collect_fresh_followup",
        "_write_session_report",
        "_hold_browser_until_closed",
        "_finalize_recording",
    ]


def test_report_and_done_line_retry_stats(tmp_path, monkeypatch, capsys):
    """验收 5：收尾 [done] 行带「历史超时补投 N 个 / 本场新增超时 M 个」；报告带台账明细。"""
    monkeypatch.setattr(mod, "edd_wrapup", _EDD_STUB)
    monkeypatch.setattr(mod.g, "resolve_paths", lambda env=None: {"name": "p"})
    entry = mod._retry_snapshot(_timeout_job(), attempts=1, jd_text="")
    state = {
        "applied": 0,
        "direct_fallback": 0,
        "keyword_skips": [],
        "retry_reprocessed": 2,
        "retry_new_count": 1,
        "retry_touched": True,
        "retry_pending": [entry],
    }
    run = _report_run(tmp_path, state=state, cfg={"retry_pending_path": str(tmp_path / "retry_pending.json")})
    mod._write_session_report(run)
    out = capsys.readouterr().out
    assert "历史超时补投 2 个" in out and "本场新增超时 1 个" in out
    data = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert data["retry_pending"] == [entry]


def test_session_report_skips_untouched_ledger(tmp_path, monkeypatch, capsys):
    """正常会话（无补投参与）：retry_pending.json 不被凭空创建（零行为变化）。"""
    monkeypatch.setattr(mod, "edd_wrapup", _EDD_STUB)
    monkeypatch.setattr(mod.g, "resolve_paths", lambda env=None: {"name": "p"})
    run = _report_run(tmp_path, cfg={"retry_pending_path": str(tmp_path / "retry_pending.json")})
    mod._write_session_report(run)
    assert not (tmp_path / "retry_pending.json").exists()


# --- 会话中途看门狗（20260927 三场楔死实测：楔点在 enter_detail 协议链路，preflight
# --- 看门狗只护起飞前；中途靠「心跳文件停摆」检测，进程外 SIGKILL 整树止损） ---


@pytest.fixture(autouse=True)
def _reset_heartbeat_dir(monkeypatch):
    """_HEARTBEAT_DIR / _HEARTBEAT_JOB 是模块级单例：不 reset 会跨测试残留——目录残留在
    已删 tmp 上（后续 _arm_session_watchdog 因非 None 跳过重绑 → 写进幽灵目录报
    FileNotFound），岗位残留在上一岗上（#56 A2 的 in_job 相位与 eid 全部失真）。"""
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", None)
    monkeypatch.setattr(fx, "_HEARTBEAT_JOB", None)


def test_parse_session_watchdog_default_off_and_invalid():
    assert mod._parse_session_watchdog(None) == 240  # 未设置 → 默认 240s
    assert mod._parse_session_watchdog("") == 240
    assert mod._parse_session_watchdog("0") == 0  # 显式关闭
    assert mod._parse_session_watchdog("120") == 120
    assert mod._parse_session_watchdog("abc") == 240  # 非法回退默认
    assert mod._parse_session_watchdog("-5") == 240  # 负数非法回退默认


def test_heartbeat_watchdog_zero_when_done_sentinel_appears():
    """report 写盘后主流程进入用户节奏的 CLOSE 等待，心跳自然停止——done 哨兵必须先于
    停摆判定，否则「等用户」会被误杀。"""
    killed = []

    def no_kill(path, payload):
        pytest.fail("done 在场不得击杀")

    assert (
        sg._heartbeat_watchdog_main(
            100,
            5,
            "/s/heartbeat.json",
            "/s/session_done",
            "/s/WATCHDOG_KILLED.json",
            exists_fn=lambda p: True,  # done 哨兵在场
            probe_fn=lambda pid: True,
            mtime_fn=lambda p: -1e9,  # 心跳早已停摆也无所谓——done 优先
            kill_fn=killed.append,
            sleep_fn=lambda s: None,
            now_fn=lambda: 0.0,
            pgrep_fn=lambda pid: [],
            marker_fn=no_kill,
        )
        == 0
    )
    assert killed == []


def test_heartbeat_watchdog_zero_when_parent_dead():
    probes = []

    def probe(pid):
        probes.append(pid)
        return False

    assert (
        sg._heartbeat_watchdog_main(
            99,
            5,
            "/s/heartbeat.json",
            "/s/session_done",
            "/s/WATCHDOG_KILLED.json",
            exists_fn=lambda p: False,
            probe_fn=probe,
            sleep_fn=lambda s: None,
            now_fn=lambda: 0.0,
        )
        == 0
    )
    assert probes == [99]


def test_heartbeat_watchdog_survives_fresh_heartbeat_then_parent_dies():
    """心跳新鲜时只循环不打扰；父进程自行死亡后安静退场（rc 0、零击杀）。"""
    killed = []
    calls = {"probe": 0, "now": 0}

    def probe(pid):
        calls["probe"] += 1
        return calls["probe"] < 2  # 第一轮活着，第二轮死亡

    def now_fn():
        calls["now"] += 1
        return 10.0 * calls["now"]  # 心跳 mtime=0 → 每轮 age≈10-20s

    assert (
        sg._heartbeat_watchdog_main(
            100,
            30,  # stall=30s：age 未超 → 继续循环
            "/s/heartbeat.json",
            "/s/session_done",
            "/s/WATCHDOG_KILLED.json",
            exists_fn=lambda p: False,
            probe_fn=probe,
            mtime_fn=lambda p: 0.0,
            kill_fn=killed.append,
            sleep_fn=lambda s: None,
            now_fn=now_fn,
            pgrep_fn=lambda pid: [],
        )
        == 0
    )
    assert killed == []


def test_heartbeat_watchdog_kills_tree_on_stale_heartbeat(tmp_path):
    """心跳停摆超阈值且父进程仍活：叶子在前整树击杀 + 落 WATCHDOG_KILLED.json 取证。"""
    killed = []
    markers = []

    def marker_fn(path, payload):
        markers.append((str(path), payload))

    rc = sg._heartbeat_watchdog_main(
        100,
        5,
        "/s/heartbeat.json",
        "/s/session_done",
        "/s/WATCHDOG_KILLED.json",
        exists_fn=lambda p: False,
        probe_fn=lambda pid: True,
        mtime_fn=lambda p: 0.0,  # now=99 → age=99s > stall=5s
        kill_fn=killed.append,
        sleep_fn=lambda s: None,
        now_fn=lambda: 99.0,
        pgrep_fn=lambda pid: [200] if pid == 100 else [],
        marker_fn=marker_fn,
    )
    assert rc == 3
    assert killed == [200, 100]  # 叶子在前
    path, payload = markers[0]
    assert path == "/s/WATCHDOG_KILLED.json"
    assert payload["stall_seconds"] == 5
    assert payload["stale_seconds"] == 99
    assert "killed_at" in payload


def test_heartbeat_watchdog_missing_heartbeat_counts_as_stale():
    """心跳文件消失（异常态）：按永久停摆处置——宁可强杀止损，不可放任楔死。"""
    killed = []

    def mtime_fn(path):
        raise OSError("file gone")

    assert (
        sg._heartbeat_watchdog_main(
            100,
            5,
            "/s/heartbeat.json",
            "/s/session_done",
            "/s/WATCHDOG_KILLED.json",
            exists_fn=lambda p: False,
            probe_fn=lambda pid: True,
            mtime_fn=mtime_fn,
            kill_fn=killed.append,
            sleep_fn=lambda s: None,
            now_fn=lambda: 0.0,
            pgrep_fn=lambda pid: [],
        )
        == 3
    )
    assert killed[-1] == 100


def test_heartbeat_watchdog_marker_failure_still_kills():
    """取证写盘失败不阻止止损（止损优先于取证）。"""
    killed = []

    def marker_fn(path, payload):
        raise OSError("disk full")

    assert (
        sg._heartbeat_watchdog_main(
            100,
            5,
            "/s/heartbeat.json",
            "/s/session_done",
            "/s/WATCHDOG_KILLED.json",
            exists_fn=lambda p: False,
            probe_fn=lambda pid: True,
            mtime_fn=lambda p: -1e9,
            kill_fn=killed.append,
            sleep_fn=lambda s: None,
            now_fn=lambda: 0.0,
            pgrep_fn=lambda pid: [],
            marker_fn=marker_fn,
        )
        == 3
    )
    assert killed == [100]


def test_heartbeat_watchdog_carries_last_label_into_marker(tmp_path):
    """心跳文件里的 label 进取证——事后回答「楔死时在干什么」。"""
    (tmp_path / "heartbeat.json").write_text(json.dumps({"ts": "x", "label": "job:11"}), encoding="utf-8")
    markers = []
    sg._heartbeat_watchdog_main(
        100,
        5,
        str(tmp_path / "heartbeat.json"),
        str(tmp_path / "session_done"),
        str(tmp_path / "WATCHDOG_KILLED.json"),
        exists_fn=lambda p: False,
        probe_fn=lambda pid: True,
        mtime_fn=lambda p: 0.0,
        kill_fn=lambda pid: None,
        sleep_fn=lambda s: None,
        now_fn=lambda: 99.0,
        pgrep_fn=lambda pid: [],
        marker_fn=lambda path, payload: markers.append(payload),
    )
    assert markers[0]["last_label"] == "job:11"


def test_spawn_session_watchdog_skips_when_disabled():
    assert mod._spawn_session_watchdog(SimpleNamespace(session_dir=Path("/tmp/s")), 0) is None


def test_spawn_session_watchdog_builds_child_command(monkeypatch):
    captured = {}

    def fake_popen(argv, cwd=None):
        captured["argv"] = argv
        captured["cwd"] = cwd
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    session = SimpleNamespace(session_dir=Path("/tmp/sessions/x"))
    watcher = mod._spawn_session_watchdog(session, 240)
    assert watcher.pid == 4242
    argv = captured["argv"]
    src = argv[2]
    assert "import _selfguard" in src and "_heartbeat_watchdog_main" in src  # 单一实现
    assert argv[3] == str(os.getpid())  # 父 pid
    assert argv[4] == "240"  # 停摆阈值
    assert argv[5] == "/tmp/sessions/x/heartbeat.json"
    assert argv[6] == "/tmp/sessions/x/session_done"
    assert argv[7] == "/tmp/sessions/x/WATCHDOG_KILLED.json"


# --- #59 修复轮 R4-6：进程外看门狗契约的**真子进程**护栏 -------------------------------
#
# 上面整段看门狗用例都是**进程内**直接调 `_heartbeat_watchdog_main`（依赖注入、零 spawn）：
# 它们证明不了子进程那一侧。生产形态是 `_spawn_session_watchdog` 拉起
# `python -c "…import _selfguard…"`，而 `_selfguard` 从 #57 第二轮起还要 `import _forensics`
# （marker 字段名的真源）。子进程里解析不到这两个模块时**没有任何声音**——Popen 照常返回、
# 主会话照常跑，只是看门狗从来没起来过：楔死时既没击杀也没栈，等于静默失去看门狗。
#
# **为什么放在本文件**（与被测的 spawn 函数同侧）：这条用例的源串是从 `_spawn_session_watchdog`
# 现场**捕获**的、不是手抄的，所以「spawn 形态被改」与「契约被改」都红在同一个文件、同一次改动里；
# 放进 `_selfguard` / `_forensics` 侧就得在这里重造一份 argv 构造，那正是本票一直在防的
# 「第二处各写一份」。它不起浏览器、不联网、限时（带 deadline 的轮询），可进 CI。
#
# **安全边界**：victim 是**一条 `sleep` 的 dummy 子进程**，只有它的 pid 被交给看门狗；
# 绝不传 `os.getpid()`——那是 pytest 自己，会被 SIGKILL 掉整条测试进程树。


def _wait_until(fn, deadline_seconds=30.0, interval=0.05):
    """带 deadline 的轮询：条件成立即返回 True，超时返回 False（由调用方写成断言，
    所以卡住的用例会**红**而不是永远挂着）。本仓没装 pytest-timeout，不为此引依赖；
    也不依赖真实时钟的精确性——只要求「在 deadline 之内发生」。"""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if fn():
            return True
        time.sleep(interval)
    return False


def _assert_real_watchdog_marker(data, stall_seconds):
    """真子进程 marker 的**形状与取值**（拆出来只为守住 ruff 的 50 句门槛：本用例的主体是
    「spawn → 击杀 → 落盘」这条链路，取证判据是另一件事，分开读也更清楚）。
    键集合 == `_forensics.MARKER_IDENTITY_KEYS` 的**键** + 镜像三键（`MARKER_HB_MIRROR`）
    + `NET_PROBE_MARKER_KEY` + 三个时间/阈值字段 + 相位与时限两键（两代看门狗共用同一份形状）。"""
    expected = (
        set(fx.MARKER_IDENTITY_KEYS)
        | set(fx.MARKER_HB_MIRROR)
        | {
            fx.NET_PROBE_MARKER_KEY,
            "killed_at",
            "stall_seconds",
            "stale_seconds",
            fx.MARKER_PHASE_KEY,
            fx.MARKER_TIMEOUT_KEY,
        }
    )
    assert set(data) == expected, set(data) ^ expected
    assert data["stall_seconds"] == pytest.approx(stall_seconds)
    assert data["stale_seconds"] >= 1, data
    # 这一场是心跳版杀的（相位由 spawn 侧的那一代决定，读侧只照抄）
    assert data[fx.MARKER_PHASE_KEY] == fx.MARKER_PHASE_SESSION, data
    assert data[fx.MARKER_LAST_EID] == "e-victim", data
    assert data[fx.MARKER_LAST_LABEL] == "jd:dom-poll:3:post", data
    assert data[fx.MARKER_IN_JOB] is True, data
    # 镜像三键（marker 侧与心跳侧**同名**）取自心跳的同名键——这条同时证明「子进程里
    # `MARKER_HB_MIRROR` 解析得到、且映射的键名没有各写一份」
    assert data[fx.MARKER_HB_MIRROR["handlers_live"]] == 2, data
    assert data[fx.MARKER_HB_MIRROR["handlers_inflight"]] == 1, data
    assert data[fx.MARKER_HB_MIRROR["handler_last_frag"]] == "/wapi/zpgeek/job/detail.json", data
    # 出网探针在子进程里落 None：conftest 的 `_isolate_handler_forensics` 把 `LIVE_NET_PROBE`
    # 默认关掉（本用例零网络是这条链路能进 CI 的前提），
    # 「开关关掉」与「探了但失败」的区分口径见 `_selfguard._default_net_probe`。
    assert data[fx.NET_PROBE_MARKER_KEY] is None, data


def test_real_subprocess_watchdog_kills_a_dummy_victim_and_writes_marker(tmp_path, monkeypatch):
    """R4-6：真 spawn 一次进程外看门狗，把「源串 → 子进程 import `_selfguard` → import
    `_forensics` → `_heartbeat_watchdog_main` → 心跳停摆 → 击杀 + 落 marker」整条链路走通。
    任何一环在子进程里解析不到（模块改名 / `sys.path` 失效 / 字段名 import 报错），
    marker 就不会出现、victim 就不会死 ⇒ 这条红；而进程内那批注入式用例全都会照常绿。
    #56 取证 A/B 之后这条顺带覆盖子进程侧的新符号：`MARKER_HB_MIRROR` 三键与
    `NET_PROBE_MARKER_KEY`（以及 `_probe_egress` 的 `socket` import）——少一个就是 marker 静默缺字段。"""
    stall_seconds = 1.0
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    heartbeat = session_dir / mod.SESSION_HEARTBEAT
    done = session_dir / mod.SESSION_DONE
    marker = session_dir / mod.WATCHDOG_KILLED
    assert not done.exists(), "done 哨兵在场会让看门狗退场（不杀「等用户 CLOSE」）"
    # 心跳由**真写者**产出（`fx._touch_heartbeat`）——这样这条进程外用例钉的是「写侧形状 ↔
    # 读侧字段名」两侧同时成立，而不是测试自己手抄一份 payload 再指望它对得上：
    # 身份五键有非 None 取值可读（缺键是合法降级、由进程内用例钉），取证 A 的镜像三键同理。
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", session_dir)
    monkeypatch.setattr(
        fx, "_HEARTBEAT_JOB", {"eid": "e-victim", "job": "会楔死的岗", "brand": "楔死科技公司", "idx": 11}
    )
    monkeypatch.setattr(fx, "_HANDLER_REGISTRY", {"h1", "h2"})
    monkeypatch.setattr(fx, "_HANDLER_INFLIGHT", 1)
    monkeypatch.setattr(fx, "_HANDLER_LAST_ENTER", "/wapi/zpgeek/job/detail.json")
    assert fx._touch_heartbeat("jd:dom-poll:3:post") is True, "心跳写不出来 ⇒ 看门狗读到的是空目录，本用例失去意义"
    assert heartbeat.exists(), "写侧落的文件名必须就是 spawn 传给子进程的那个路径（两侧读同一份形状）"

    # 源串从生产函数捕获：用注入的 popen_fn 拿到 argv，不动真格 spawn pytest
    captured = {}

    def capture_popen(argv, cwd=None):
        captured["argv"] = argv
        captured["cwd"] = cwd
        return SimpleNamespace(pid=-1)

    mod._spawn_session_watchdog(SimpleNamespace(session_dir=session_dir), stall_seconds, popen_fn=capture_popen)
    produced = captured["argv"]
    assert produced[0] == sys.executable and produced[1] == "-c"
    assert produced[3] == str(os.getpid()), "生产形态：被守护的父进程就是自己——本用例必须把它换掉"

    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    # 只替换「被击杀对象」那一个位置（父 pid → dummy victim 的 pid），stall 与三个文件路径原样照抄
    argv = produced[:3] + [str(victim.pid)] + produced[4:]
    watchdog = None
    try:
        watchdog = subprocess.Popen(argv, cwd=captured["cwd"])
        assert _wait_until(lambda: watchdog.poll() is not None, 30), (
            "真子进程看门狗 30s 内没退出——链路某处解析不到模块（静默失去看门狗的形态）"
        )
        assert watchdog.returncode == 3, f"退出码应为 3（心跳停摆击杀），实际 {watchdog.returncode}"
        assert marker.exists(), "marker 没落盘：子进程侧的取证链路断了"
        data = json.loads(marker.read_text(encoding="utf-8"))
        _assert_real_watchdog_marker(data, stall_seconds)
        # victim 真的被击杀（`sleep 300` 不可能自行结束），且是 SIGKILL
        assert _wait_until(lambda: victim.poll() is not None, 15), "victim 还活着——没杀成"
        assert victim.returncode == -9, victim.returncode
    finally:
        # 清理：断言失败时不能留一只还在轮询的看门狗 / 一条睡 300s 的 dummy 进程
        for proc in (watchdog, victim):
            if proc is not None and proc.poll() is None:
                proc.kill()
        for proc in (watchdog, victim):
            if proc is None:
                continue
            try:
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001  # 收尾兜底：清理路径不该把用例改成另一种失败
                pass


def test_arm_session_watchdog_binds_beats_and_spawns(tmp_path, monkeypatch):
    spawned = []
    monkeypatch.setenv("LIVE_WATCHDOG", "120")
    monkeypatch.setattr(mod, "_spawn_session_watchdog", lambda session, stall: spawned.append(stall) or "W")
    session = SimpleNamespace(session_dir=tmp_path)
    mod._arm_session_watchdog(session)
    assert spawned == [120]
    data = json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))
    assert data["label"] == "browser-up"  # 首拍在拉起前——看门狗起来就有心跳可读


def test_arm_session_watchdog_disabled_skips_spawn(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_WATCHDOG", "0")
    monkeypatch.setattr(mod, "_spawn_session_watchdog", lambda *a, **k: pytest.fail("off 时不得拉起"))
    mod._arm_session_watchdog(SimpleNamespace(session_dir=tmp_path))
    assert not (tmp_path / "heartbeat.json").exists()


def test_touch_heartbeat_writes_labeled_json(tmp_path, monkeypatch):
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    assert mod._touch_heartbeat("job:3") is True
    data = json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))
    assert data["label"] == "job:3"
    assert "ts" in data


def test_touch_heartbeat_unbound_is_noop(monkeypatch):
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", None)
    assert mod._touch_heartbeat("x") is False  # 未绑定（单测/无会话）静默跳过


def test_touch_heartbeat_write_failure_disarms_via_done_sentinel(tmp_path, monkeypatch, capsys):
    """心跳写失败若不处置，看门狗会把健康会话误杀——降级为「无看门狗继续跑」：
    落 done 哨兵让看门狗退场 + 大声警告（可观测，de98212/c-1 同款要求）。"""
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)

    def boom(path, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(fx, "write_json_atomic", boom)
    assert mod._touch_heartbeat("x") is False
    assert (tmp_path / mod.SESSION_DONE).exists()
    assert "心跳写入失败" in capsys.readouterr().out
    assert mod._touch_heartbeat("y") is False  # 解绑后继续 no-op，不再反复报


def test_shot_touches_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    mod.shot(_ShotPage(), tmp_path, "scroll")
    data = json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))
    assert "shot:" in data["label"]


def test_scroll_collect_touches_heartbeat_each_round(tmp_path, monkeypatch):
    """采集滚动轮是单场最长的无截图区段（120 条滚 9 轮实测 5 分+）——逐轮必须有心跳。"""
    _quiet_scroll(monkeypatch)
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    page = _SearchPage(batches=[[{"encryptJobId": f"p{i}"}] for i in range(5)])
    buf, flags = [], {}
    mod.capture_xhr_accum(page, mod.SEARCH_XHR_FRAG, buf, mod.JOB_LIST_KEY, flags)
    page.goto("about:blank")
    mod.scroll_collect(page, buf, target=2, flags=flags)
    data = json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))
    assert data["label"].startswith("scroll:")


def test_wait_reply_touches_heartbeat_while_polling(tmp_path, monkeypatch):
    """agent 起草等待是合法长停（窗口见 `REPLY_TIMEOUT_DEFAULT`）——轮询必须逐拍心跳，否则起草期必被误杀。"""
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    (tmp_path / "reply_9.json").write_text(json.dumps({"send_text": "你好"}), encoding="utf-8")
    mod.wait_reply(tmp_path, 9, timeout=10, poll=0, sleep_fn=lambda s: None, now_fn=lambda: 0.0, grace=0)
    data = json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))
    assert data["label"] == "wait_reply:9"


def test_run_live_watchdog_wiring_order():
    """接线（源断言，同「单一实现」断言先例）：看门狗在浏览器 pid 采集后拉起（覆盖补投轮全程）；
    session_done 哨兵在报告写盘后落（早于 CLOSE 等待——用户节奏的等待不得在看门狗眼皮下停拍）。"""
    src = Path(mod.__file__).resolve().read_text(encoding="utf-8")
    assert src.index("_record_browser_pids(run.session)") < src.index("_arm_session_watchdog(run.session)")
    assert src.index("_write_session_report(run)") < src.index("_mark_session_done(run.session)")
    assert src.index("_mark_session_done(run.session)") < src.index("_hold_browser_until_closed(run.session)")


def test_surface_prior_wedge_warns_with_marker(tmp_path, capsys):
    old = tmp_path / "20260920-000000"
    old.mkdir()
    (old / "WATCHDOG_KILLED.json").write_text(
        json.dumps({"stale_seconds": 321, "last_label": "job:11"}), encoding="utf-8"
    )
    mod._surface_prior_wedge(tmp_path, "20260927-000000")
    out = capsys.readouterr().out
    assert "看门狗" in out and "20260920-000000" in out and "321" in out and "job:11" in out


def test_surface_prior_wedge_silent_without_marker(tmp_path, capsys):
    (tmp_path / "20260920-000000").mkdir()
    mod._surface_prior_wedge(tmp_path, "20260927-000000")
    assert "看门狗" not in capsys.readouterr().out


def test_surface_prior_wedge_tolerates_bad_marker(tmp_path, capsys):
    old = tmp_path / "20260920-000000"
    old.mkdir()
    (old / "WATCHDOG_KILLED.json").write_text("not json", encoding="utf-8")
    mod._surface_prior_wedge(tmp_path, "20260927-000000")
    out = capsys.readouterr().out
    assert "20260920-000000" in out  # 坏取证也要报场名，解析不出细节不阻断


@pytest.mark.parametrize(
    "phase,expected",
    [(fx.MARKER_PHASE_PREFLIGHT, "起飞前自检段击杀"), (fx.MARKER_PHASE_SESSION, "会话中途击杀")],
)
def test_surface_prior_wedge_names_the_generation(tmp_path, capsys, phase, expected):
    """呈报要点明死在哪一段：起飞前自检那一次点击卡住、与会话中途的详情往返卡住，
    是两条不同的追查路线（前者查浏览器/驱动起得来吗，后者查具体岗位与端点）。
    相位取值来自 `_forensics` 的两个常量，呈报侧不造第二份字面量。"""
    old = tmp_path / "20261007-203600"
    old.mkdir()
    (old / mod.WATCHDOG_KILLED).write_text(
        json.dumps({"stale_seconds": 181, "last_label": "click:mouse-click:pre", fx.MARKER_PHASE_KEY: phase}),
        encoding="utf-8",
    )
    mod._surface_prior_wedge(tmp_path, "20261008-090000")
    assert expected in capsys.readouterr().out


def test_surface_prior_wedge_omits_phase_for_legacy_marker(tmp_path, capsys):
    """历史会话目录里的 marker 没有 `watchdog_phase` 键（那些文件改不了），呈报就不显示相位——
    缺项是它的正常读法，不是「猜一个相位」的兼容分支：猜错等于把一场没留证的击杀说成留了证的。"""
    old = tmp_path / "20260920-000000"
    old.mkdir()
    (old / mod.WATCHDOG_KILLED).write_text(json.dumps({"stale_seconds": 321, "last_label": "job:11"}), encoding="utf-8")
    mod._surface_prior_wedge(tmp_path, "20260927-000000")
    out = capsys.readouterr().out
    assert "job:11" in out and "321" in out, out
    assert "起飞前自检段击杀" not in out and "会话中途击杀" not in out, out


# --- preflight 楔死取证 + 登录过期空覆盖防护（20260928-015515 / 192616 两场实测） ---


def test_preflight_wedge_marker_written_by_watchdog(tmp_path):
    """preflight 楔死(自检点卡片协议挂起)：预检看门狗击杀时也落 WATCHDOG_KILLED.json——
    此前只落 preflight_ok 哨兵,零取证,下一场无从呈报死因。"""
    killed = []
    markers = []
    rc = sg._heartbeat_watchdog_main(
        100,
        5,
        str(tmp_path / "heartbeat.json"),
        str(tmp_path / "session_done"),
        str(tmp_path / "WATCHDOG_KILLED.json"),
        exists_fn=lambda p: False,
        probe_fn=lambda pid: True,
        mtime_fn=lambda p: 0.0,
        kill_fn=killed.append,
        sleep_fn=lambda s: None,
        now_fn=lambda: 99.0,
        pgrep_fn=lambda pid: [],
        marker_fn=lambda path, payload: markers.append(payload),
    )
    assert rc == 3
    assert killed[-1] == 100
    assert markers[0]["last_label"] is None  # 未武装心跳时 label 缺失,不阻断取证


def test_preflight_guarded_arms_heartbeat_watchdog(tmp_path, monkeypatch):
    """preflight 段也用心跳看门狗兜底(此前只有哨兵式 preflight 看门狗,楔死时无取证):
    _preflight_guarded 须武装心跳看门狗(rebind=True 重绑本会话),首拍心跳落盘。
    注:自检快路径上无段内心跳(shot 不在其中),最坏 ~70s << 240s 停摆阈值,无误杀面。"""
    session = SimpleNamespace(session_dir=tmp_path)
    monkeypatch.setenv("LIVE_PREFLIGHT_WATCHDOG", "0")  # 哨兵式看门狗关掉,隔离验证心跳版
    monkeypatch.setattr(mod, "_preflight_click_check", lambda page, s: True)
    armed = []
    monkeypatch.setattr(mod, "_spawn_session_watchdog", lambda *a, **k: armed.append("spawned") or "W")
    assert mod._preflight_guarded(object(), session) is True
    # 真的跑了 _arm_session_watchdog(只 mock 掉子进程拉起):首拍 heartbeat 落盘 + spawn 被调
    assert json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))["label"] == "browser-up"
    assert armed == ["spawned"]
    mod._heartbeat_bind(str(tmp_path))  # 复位绑定,避免污染同文件后续测试


def test_collect_fresh_followup_empty_result_keeps_previous(tmp_path, capsys, monkeypatch):
    """登录过期等场景采集到 0 对话(192616 实测)：不得覆盖已有 followup 报告——
    空报告会静默抹掉历史 94 条对话并污染 eval 快照。降级为沿用上次数据。"""
    run = _fresh_run(tmp_path)
    prev = tmp_path / "followup_report.json"
    prev.write_text(json.dumps({"totalConversations": 94, "conversations": [{"company": "甲"}] * 94}), encoding="utf-8")
    empty = {"totalConversations": 0, "conversations": []}
    monkeypatch.setattr(run.session.platform, "followup", lambda page, applied, **kw: empty, raising=False)
    monkeypatch.setattr(mod.g, "resolve_paths", lambda env=None: {"followup": prev})
    mod._collect_fresh_followup(run, page=None)
    assert json.loads(prev.read_text(encoding="utf-8"))["totalConversations"] == 94  # 未被空报告覆盖
    assert run.session.state["followup_fresh"] is False
    assert "沿用" in capsys.readouterr().out


def test_edd_snapshot_skips_empty_sample(tmp_path, monkeypatch, capsys):
    """失败会话(登录过期/熔断/投 0)不追加 EDD 趋势快照：0 样本快照会把趋势稀释成
    「样本 +0」假象。edd_wrapup 在 sampleSize<=0 时跳过 append 并明示原因。"""
    paths = {
        "followup": tmp_path / "f.json",
        "greetings": tmp_path / "g.json",
        "annotations": tmp_path / "a.json",
        "trend": tmp_path / "trend.jsonl",
        "evaluation": tmp_path / "e.json",
        "pending": tmp_path / "p.json",
    }
    (tmp_path / "f.json").write_text(json.dumps({"conversations": []}), encoding="utf-8")
    (tmp_path / "g.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(mod.g, "resolve_paths", lambda env=None: paths)
    outcome = mod.edd_wrapup(paths, profile="p")
    assert mod._edd.load_trend(paths["trend"]) == []  # 趋势无任何快照(空快照不入列)
    assert outcome is not None and not outcome["trend_appended"]  # 降级结果显式可见
    term = capsys.readouterr().out
    # 降级原因可观测、不静默（#59 二轮：措辞不再一律说成「失败会话」——残缺采集那类会话本身
    # 跑完了，所以这里钉的是「跳过 + reason 原文」，而不是某一句固定尾巴）
    assert "跳过趋势快照" in term and "empty followup report" in term, term


# --- #56 A1：per-job detail 监听器随单岗生命周期注销（楔死头号候选＝监听器累积 × handler 内无超时往返）---


class _ListenerPage(_SendPage):
    """记录 on / remove_listener 的页面替身：本票的取证口径就是「跑 N 岗后 page 上的
    response 监听器数不随 N 增长」（#35 修的是 search_list 的同类泄漏）。"""

    def __init__(self, url, panel_title=""):
        super().__init__(url, panel_title)
        self.listeners = []

    def on(self, event, handler):
        self.listeners.append(handler)

    def remove_listener(self, event, handler):
        if handler in self.listeners:
            self.listeners.remove(handler)


def _run_apply_on_page(tmp_path, monkeypatch, page, times=3, grab=None):
    """同一 page 上连跑 N 岗（A1 的「不随 N 增长」需要多岗同页才有意义）。
    grab 可注入 grab_detail 替身，用于观察「本岗进行中监听器仍在位」；
    #56 A3 后 grab_detail 多了 beat_fn 形参，替身一律以 **kw 收下。"""
    calls = _send_mocks(
        monkeypatch,
        [SimpleNamespace() for _ in range(times)],
        [_FakeInputEl() for _ in range(times)],
    )
    if grab is not None:
        monkeypatch.setattr(mod, "grab_detail", grab)
    platform = SimpleNamespace(login_dialog_selector="#login-dialog", save_evidence=lambda p, tag, job=None: None)
    pacer = SimpleNamespace(wait_skip=lambda: None, wait_sent=lambda: None)
    results, state = [], {"applied": 0, "direct_fallback": 0, "keyword_skips": []}
    job = {"jobName": "测试岗", "brandName": "测试公司", "encryptJobId": "e1"}
    for i in range(times):
        mod.apply_live(page, job, tmp_path, i + 1, tmp_path, {"paste_prob": 1.0}, pacer, platform, results, state)
    return results, calls


def test_detail_listener_count_does_not_grow_with_jobs(tmp_path, monkeypatch):
    """#56 验收 1：跑 3 岗后 page 上 response 监听器数为 0——旧实现每岗挂一个 detail.json
    处理器且从不注销（O(N²) 协议往返累积，5/5 场楔死收敛在同一区间）。"""
    page = _ListenerPage(_DETAIL_URL)
    _run_apply_on_page(tmp_path, monkeypatch, page, times=3)
    assert page.listeners == [], f"监听器泄漏：3 岗后仍挂着 {len(page.listeners)} 个 response 处理器"


def test_detail_listener_stays_attached_while_job_runs(tmp_path, monkeypatch):
    """A1 的反向护栏：注销只能发生在**单岗末尾**。若提前到 grab_detail 之前，详情响应就
    再也捕不到——那会把「楔死修复」做成「no_jd 回归」，本用例把这条边界钉住。"""
    page = _ListenerPage(_DETAIL_URL)
    seen = {}

    def spy_grab(_page, _detail, **kw):
        seen["during_job"] = len(page.listeners)
        return "JD 全文：负责测试"

    _run_apply_on_page(tmp_path, monkeypatch, page, times=1, grab=spy_grab)
    assert seen["during_job"] == 1, "本岗进行中 detail 监听器必须在位（否则抓不到详情响应）"


def test_detail_listener_detached_on_early_skip(tmp_path, monkeypatch):
    """提前出口也要注销：grab_detail 落空 → no_jd 跳过（_skip_job 路径），
    监听器同样不得残留。"""
    page = _ListenerPage(_DETAIL_URL)
    results, _calls = _run_apply_on_page(tmp_path, monkeypatch, page, times=1, grab=lambda _p, _d, **kw: "")
    assert [r["status"] for r in results] == ["no_jd"]
    assert page.listeners == [], "跳过路径同样必须注销（楔死不只发生在正常投递路径上）"


def test_detail_listener_detach_failure_does_not_kill_job(tmp_path, monkeypatch, capsys):
    """观测层不得反噬业务：remove_listener 自身抛异常（协议链路已经在抖）时，
    本岗照常收尾，而不是把异常冒出去变成 page_error；降级必须打一行可见警告。

    区分口径：替身只对**先注册的那个 handler** 抛错——注册顺序是「阶段 1 的 detail 监听器
    → 阶段 5b 的 friend/add 监听器」，先注册的就是 #56 A1 新增注销出口要处理的那个；
    friend/add 的注销（`_type_and_send_greeting` 既有 finally）保持正常工作，否则本用例会
    打在既有代码的位置、锁不到 A1 的新增出口。"""

    class _BadDetachPage(_ListenerPage):
        def __init__(self, url, panel_title=""):
            super().__init__(url, panel_title)
            self.first_handler = None  # = 本岗的 detail.json 监听器

        def on(self, event, handler):
            if self.first_handler is None:
                self.first_handler = handler
            super().on(event, handler)

        def remove_listener(self, event, handler):
            if handler is self.first_handler:
                raise RuntimeError("Connection closed while reading from the driver")
            super().remove_listener(event, handler)  # friend/add 的注销照常成功

    page = _BadDetachPage(_DETAIL_URL)
    results, _calls = _run_apply_on_page(tmp_path, monkeypatch, page, times=1)
    assert [r["status"] for r in results] == ["applied"], "注销失败不改变投递结果"
    assert "detail 监听器注销失败" in capsys.readouterr().out, "吞掉异常也必须可见（不开静默失败面）"


# --- #56 取证 A（接线侧）：四处注销点与注册表读数同进同退 ---


def test_handler_registry_returns_to_zero_after_the_whole_flow(tmp_path, monkeypatch):
    """A1 的「跑 N 岗后 page 上监听器数为 0」与取证 A 的「`handlers_live` 归零」必须是**同一件事**
    的两面：只在 `page.listeners` 上归零、注册表还在涨，说明某个注销点没走 `_drop_response_listener`
    （读数是给楔死现场用的，不是给测试用的装饰）。
    读数打在 `_forensics` 本体上——状态归它所有，`live_apply` 顶部的四个函数别名只是同一个函数对象。"""
    fx._reset_handler_forensics()
    page = _ListenerPage(_DETAIL_URL)
    seen = {}

    def spy_grab(_page, _detail, **kw):
        seen["during_job"] = fx.handler_forensics()["handlers_live"]
        return "JD 全文：负责测试"

    _run_apply_on_page(tmp_path, monkeypatch, page, times=3, grab=spy_grab)
    assert page.listeners == []
    assert fx.handler_forensics() == {"handlers_live": 0, "handlers_inflight": 0, "handler_last_frag": None}
    assert seen["during_job"] == 1, "本岗进行中 detail 监听器在计数里（早摘等于把泄漏读成健康）"


def test_drop_response_listener_keeps_the_count_when_remove_raises():
    """诚实读数的顺序证明：`remove_listener` 抛异常（协议链路正在抖，正是 A1 论证过的那种）时
    计数**保持「仍挂着」**——先摘计数再 remove 会把一次真泄漏读成健康，那是取证最坏的失效形态。
    顺带钉住「remove 的异常照原样外抛」：三个既有 finally 调用点的传播语义一字未改。"""
    fx._reset_handler_forensics()
    page = _SendPage(_DETAIL_URL)
    h = mod.capture_xhr(page, _DETAIL_FRAG, {"data": None})
    assert fx.handler_forensics()["handlers_live"] == 1

    class _BadPage:
        def remove_listener(self, event, handler):
            raise RuntimeError("Connection closed while reading from the driver")

    with pytest.raises(RuntimeError):
        mod._drop_response_listener(_BadPage(), h)
    assert fx.handler_forensics()["handlers_live"] == 1, "remove 失败 ⇒ 计数仍挂着"
    mod._drop_response_listener(page, h)
    assert fx.handler_forensics()["handlers_live"] == 0


# --- #56 A2：心跳带岗位身份（击杀取证 → B1 归因的前置）---
#
# 楔死现场只剩最后那一拍 heartbeat.json；5/5 场的最后一拍都是岗头 `job:{idx}`。
# 原 payload 只有 {ts, label}，看门狗取证因此答不出「死在哪个岗上」——A2 把岗位身份
# 一路送到 WATCHDOG_KILLED.json，B1（后续 agent）才有跳岗台账的写入源。


def _hb_flow(idx=2, job=None):
    """_heartbeat_job_start 只读 flow.job / flow.idx，不必造完整会话上下文。"""
    return mod._JobFlow(
        page=None,
        job={"jobName": "区块链岗", "brandName": "测试公司", "encryptJobId": "eid-2"} if job is None else job,
        idx=idx,
        ctx=None,
    )


def _heartbeat_payload(tmp_path):
    return json.loads((tmp_path / "heartbeat.json").read_text(encoding="utf-8"))


def test_touch_heartbeat_carries_job_context(tmp_path, monkeypatch):
    """#56 验收 1（A2）：岗位内每一拍带 eid / 岗位名 / 公司 / 序号 + in_job=True。"""
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    mod._heartbeat_job_start(_hb_flow(idx=2))
    assert mod._touch_heartbeat("job:2") is True
    data = _heartbeat_payload(tmp_path)
    assert data["label"] == "job:2"
    assert data["eid"] == "eid-2"
    assert data["job"] == "区块链岗"
    assert data["brand"] == "测试公司"
    assert data["idx"] == 2
    assert data["in_job"] is True, "岗位内节拍必须可判别（B1 只在 in_job 时归因）"


def test_touch_heartbeat_outside_job_carries_no_eid(tmp_path, monkeypatch):
    """不在岗位内的节拍（滚动轮 / 会话级节点）eid=None、in_job=False——两岗之间的楔死
    不得被错记到上一岗头上，这是 B1 归因的边界条件。"""
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    assert mod._touch_heartbeat("scroll:3") is True
    data = _heartbeat_payload(tmp_path)
    assert data["in_job"] is False
    assert data["eid"] is None and data["idx"] is None


def test_heartbeat_job_end_clears_job_context(tmp_path, monkeypatch):
    """单岗末尾清除岗位上下文：之后任何一拍都不该再带着已作废岗位的 eid。"""
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    mod._heartbeat_job_start(_hb_flow())
    mod._heartbeat_job_end()
    assert mod._touch_heartbeat("shot:after-read-2") is True
    data = _heartbeat_payload(tmp_path)
    assert data["in_job"] is False and data["eid"] is None


def test_apply_live_head_beat_carries_eid(tmp_path, monkeypatch):
    """岗头那一拍是 5/5 场楔死里的**最后一拍**（楔死位点在岗头→detail-open 之间）——
    岗位上下文必须在拍之前设置，否则击杀取证拿到的 eid 恒为 None，A2 等于没做。"""
    page = _ListenerPage(_DETAIL_URL)
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    _run_apply_on_page(tmp_path, monkeypatch, page, times=1)
    data = _heartbeat_payload(tmp_path)
    assert data["label"] == "job:1"
    assert data["eid"] == "e1" and data["job"] == "测试岗" and data["in_job"] is True


def test_apply_live_clears_job_context_at_job_end(tmp_path, monkeypatch):
    """正常投递出口走 `_apply_job` 的 finally（与 A1 注销同一出口，不开第二个 finally）：
    本岗结束后再拍的会话级节拍 in_job=False。"""
    page = _ListenerPage(_DETAIL_URL)
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    _run_apply_on_page(tmp_path, monkeypatch, page, times=2)
    assert fx._HEARTBEAT_JOB is None, "单岗末尾必须清掉岗位上下文"
    mod._touch_heartbeat("scroll:1")
    data = _heartbeat_payload(tmp_path)
    assert data["in_job"] is False and data["eid"] is None


def test_apply_live_clears_job_context_when_phase_raises(tmp_path, monkeypatch):
    """异常出口同样清理：`_apply_one_job` 把单岗异常收成 page_error，但残留的岗位上下文
    会让下一拍继续归因给这个作废岗；监听器同样不得残留（A1/A2 共用同一个 finally）。"""
    page = _ListenerPage(_DETAIL_URL)
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    _send_mocks(monkeypatch, [SimpleNamespace()], [_FakeInputEl()])

    def boom(_flow):
        raise RuntimeError("协议链路抖了")

    monkeypatch.setattr(mod, "_human_read_and_precheck", boom)
    job = {"jobName": "测试岗", "brandName": "测试公司", "encryptJobId": "e1"}
    pacer = SimpleNamespace(wait_skip=lambda: None, wait_sent=lambda: None)
    platform = SimpleNamespace(login_dialog_selector="#login-dialog", save_evidence=lambda p, tag, job=None: None)
    state = {"applied": 0, "direct_fallback": 0, "keyword_skips": []}
    with pytest.raises(RuntimeError):
        mod.apply_live(page, job, tmp_path, 1, tmp_path, {"paste_prob": 1.0}, pacer, platform, [], state)
    assert fx._HEARTBEAT_JOB is None
    assert page.listeners == [], "异常出口同样要注销 detail 监听器"


# --- #57 第二轮：跨进程取证字段名的单一真源（心跳七键 ↔ marker 身份五键 ↔ 台账读键）---


def test_heartbeat_and_marker_field_names_are_the_single_source(tmp_path, monkeypatch):
    """三处同构的字段名——`_heartbeat_payload` 的十键、`_selfguard` 写 marker 的身份五键 +
    镜像三键（取证 A）、`_wedge_ledger._wedge_entry_from_marker` 的读键——现在都从 `_forensics`
    那几份常量派生。
    用例把**两个方向**都钉住（票面「同批登记」第 2 条）：
      · heartbeat 侧：十键**齐全且顺序**与 `HEARTBEAT_FIELDS` 一致——payload 的键序是进程外契约
        （#56 验收 4 明令不许改前七键的字段名与顺序，取证 A 只许往**尾部追加**），故比的是
        `tuple(payload)` 而不是集合；
      · marker 侧：五个身份键名齐全、且映射的**值**必须是心跳键的子集（否则看门狗按不存在的键
        取，写出去的身份恒为 None，B1 整场不入账）；镜像三键同理，但它与身份五键的**键集互不相交**
        ——合并成一张表等于把往返计数喂进 `_wedge_entry_from_marker` 的入账判据，那是行为变更。
    `MARKER_*` 那五个名字按 dict 声明序解包而来，故一并钉「解包序 == 映射键序」：错位等于台账
    读一个不存在的键，代价同样是静默不入账，而不是报错。
    末三键的取值直接 patch `_forensics` 的**状态**（不是别名）——顺带证明「计数不活在别名上」
    这条归属，以及 payload 里那三个数是现场读数而不是第二份真相。"""
    assert fx.HEARTBEAT_FIELDS == (
        "ts",
        "label",
        "eid",
        "job",
        "brand",
        "idx",
        "in_job",
        "handlers_live",
        "handlers_inflight",
        "handler_last_frag",
    )
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    monkeypatch.setattr(fx, "_HEARTBEAT_JOB", {"eid": "e9527", "job": "solidity 开发", "brand": "企贤", "idx": 3})
    monkeypatch.setattr(fx, "_HANDLER_REGISTRY", {"h1", "h2", "h3"})
    monkeypatch.setattr(fx, "_HANDLER_INFLIGHT", 2)
    monkeypatch.setattr(fx, "_HANDLER_LAST_ENTER", "/wapi/zpgeek/job/detail.json")
    payload = mod._heartbeat_payload("job:3")  # 经 live_apply 的别名层取——别名层也是对外契约
    assert tuple(payload) == fx.HEARTBEAT_FIELDS, "十键齐全且顺序一字不改（看门狗与 B1 按固定键读）"
    assert payload["eid"] == "e9527" and payload["in_job"] is True, "岗位上下文按同名键取（eid/job/brand/idx）"
    assert (payload["handlers_live"], payload["handlers_inflight"], payload["handler_last_frag"]) == (
        3,
        2,
        "/wapi/zpgeek/job/detail.json",
    ), "取证 A 的三键取自 `_forensics` 的那三份状态"

    assert list(fx.MARKER_IDENTITY_KEYS) == [
        fx.MARKER_LAST_EID,
        fx.MARKER_LAST_JOB,
        fx.MARKER_LAST_BRAND,
        fx.MARKER_IN_JOB,
        fx.MARKER_LAST_LABEL,
    ], "MARKER_* 解包序必须等于映射声明序（错位=台账读空键=静默不入账）"
    assert set(fx.MARKER_IDENTITY_KEYS) == {"last_eid", "last_job", "last_brand", "in_job", "last_label"}
    assert set(fx.MARKER_IDENTITY_KEYS.values()) <= set(fx.HEARTBEAT_FIELDS), "marker 的取值来源必须是心跳键"
    # 取证 A 的镜像三键：与心跳侧同名（不改名），取值来源同样必须是心跳键
    assert set(fx.MARKER_HB_MIRROR) == {"handlers_live", "handlers_inflight", "handler_last_frag"}
    assert set(fx.MARKER_HB_MIRROR.values()) <= set(fx.HEARTBEAT_FIELDS)
    assert not (set(fx.MARKER_IDENTITY_KEYS) & set(fx.MARKER_HB_MIRROR)), "两份映射的键集不许相交"


def test_marker_mirror_keys_do_not_leak_into_the_ledger_gate(monkeypatch):
    """「改一处不误伤另一处」的那一半：镜像三键进的是 marker，**不是** B1 的入账判据。
    只给镜像三键（没有身份五键）的取证必须照旧不入账——台账判据仍然只认 `in_job` + `last_eid`；
    哪天有人把两份映射并成一张表，这条会红，而不是悄悄把跳岗判据改成「看往返计数」。"""
    only_mirror = {"handlers_live": 4, "handlers_inflight": 1, "handler_last_frag": "/x/detail.json"}
    assert mod._wedge_entry_from_marker("20260930-000000", dict(only_mirror)) is None
    monkeypatch.setattr(fx, "MARKER_HB_MIRROR", {})  # 镜像表清空也不影响台账（它从来不吃它）
    assert mod._wedge_entry_from_marker("20260930-000001", dict(only_mirror)) is None


def test_marker_roundtrip_from_forensics_mapping_books_the_wedge(tmp_path, monkeypatch):
    """往返用例：真写一遍心跳 → 让**进程外**看门狗（`sg._heartbeat_watchdog_main` 默认写盘器）
    落 WATCHDOG_KILLED.json → 下一场读盘喂给 `_wedge_entry_from_marker` ⇒ 归因合格、字段逐个对上。
    三处代码只经由 `_forensics` 的两份常量相遇，中间没有任何一处测试自己拼字段名。
    并配三条**漂移/缺字段**的对抗输入，证明「合格」不是自证：
      · `in_job` 缺失 / 非 True → 不入账（两岗之间被杀不许记给上一岗）；
      · `last_eid` 缺失或空白 → 不入账；
      · 心跳把 `eid` 改名（模拟字段漂移）→ 看门狗照映射取不到值 → marker 身份全 None → 不入账，
        正是票面说的「静默少字段 = 整场不入账」形态（#56 真实数据里旧格式全 rejected 的同款）。"""
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    monkeypatch.setattr(fx, "_HEARTBEAT_JOB", {"eid": "e9527", "job": "solidity 开发", "brand": "企贤", "idx": 11})
    assert fx._touch_heartbeat("job:11") is True
    marker = tmp_path / fx.WATCHDOG_KILLED

    def run_watchdog(heartbeat_path, marker_path):
        return sg._heartbeat_watchdog_main(
            100,
            5,
            str(heartbeat_path),
            str(tmp_path / "session_done"),
            str(marker_path),
            exists_fn=lambda p: False,
            probe_fn=lambda pid: True,
            mtime_fn=lambda p: 0.0,  # now=99 → age=99s > stall=5s ⇒ 击杀并落取证
            kill_fn=lambda pid: None,
            sleep_fn=lambda s: None,
            now_fn=lambda: 99.0,
            pgrep_fn=lambda pid: [],
        )

    assert run_watchdog(tmp_path / fx.SESSION_HEARTBEAT, marker) == 3
    data = json.loads(marker.read_text(encoding="utf-8"))
    entry = mod._wedge_entry_from_marker("20260930-000000", data)
    assert entry is not None, "岗位内被杀的取证必须归因合格（B1 的写入源就靠这一步）"
    assert entry["encryptJobId"] == "e9527"
    assert entry["jobName"] == "solidity 开发" and entry["brandName"] == "企贤"
    assert entry["last_label"] == "job:11" and entry["session"] == "20260930-000000"
    assert entry["stale_seconds"] == 99 and entry["wedged_at"] == data["killed_at"]

    # 对抗输入 1/2：归因门槛的两侧各缺一维都不许入账
    for drop, value in (("in_job", None), ("last_eid", None), ("last_eid", "  ")):
        drifted = dict(data)
        if value is None:
            drifted.pop(drop, None)
        else:
            drifted[drop] = value
        assert mod._wedge_entry_from_marker("20260930-000001", drifted) is None, f"缺 {drop} 不该入账"
    assert mod._wedge_entry_from_marker("20260930-000002", {**data, "in_job": False}) is None

    # 对抗输入 3：字段漂移（心跳侧把 eid 改名）→ 看门狗按映射取不到 → marker 身份为空 → 整场不入账
    renamed = tmp_path / "renamed"
    renamed.mkdir()
    drifted_hb = json.loads((tmp_path / fx.SESSION_HEARTBEAT).read_text(encoding="utf-8"))
    drifted_hb["job_id"] = drifted_hb.pop("eid")
    drifted_hb_path = renamed / fx.SESSION_HEARTBEAT
    drifted_hb_path.write_text(json.dumps(drifted_hb, ensure_ascii=False), encoding="utf-8")
    drifted_marker = renamed / fx.WATCHDOG_KILLED
    assert run_watchdog(drifted_hb_path, drifted_marker) == 3
    drifted_data = json.loads(drifted_marker.read_text(encoding="utf-8"))
    assert drifted_data[fx.MARKER_LAST_EID] is None, "漂移后 marker 的 last_eid 取不到值（正是静默少字段）"
    assert mod._wedge_entry_from_marker("20260930-000003", drifted_data) is None


def test_read_heartbeat_returns_whole_payload(tmp_path):
    """#56 A2：`_read_heartbeat_label` 只取 label → 改名 `_read_heartbeat` 并返回整个 payload，
    看门狗才可能把岗位身份写进击杀取证。"""
    hb = tmp_path / "heartbeat.json"
    payload = {
        "ts": "2026-09-28T01:47:49",
        "label": "job:2",
        "eid": "e9527",
        "job": "solidity 开发",
        "brand": "企贤",
        "idx": 2,
        "in_job": True,
    }
    hb.write_text(json.dumps(payload), encoding="utf-8")
    assert sg._read_heartbeat(hb) == payload


def test_read_heartbeat_degrades_to_empty_dict(tmp_path):
    """降级铁律：文件缺失 / 坏 JSON / 非字典形态一律 {}——marker 照写（缺的字段落 None），
    取证失败绝不阻止击杀。"""
    assert sg._read_heartbeat(tmp_path / "absent.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert sg._read_heartbeat(bad) == {}
    not_a_dict = tmp_path / "list.json"
    not_a_dict.write_text("[1, 2]", encoding="utf-8")
    assert sg._read_heartbeat(not_a_dict) == {}


def _run_stale_watchdog(sg_module, tmp_path, heartbeat_content, markers, **injections):
    """跑一次「心跳已停摆」的看门狗循环，回收 marker payload（击杀必然发生）。
    `**injections`（#56 取证 B）把探测侧的注入位透进去（`net_probe_fn` / `socket` 相关的 env 由
    用例自己管）——共用同一个 helper 是刻意的：「探测不影响止损」这条判据必须建立在
    与既有那批 marker 用例**完全相同**的停摆形态上，否则就是自证。"""
    hb = tmp_path / "heartbeat.json"
    if heartbeat_content is not None:
        hb.write_text(heartbeat_content, encoding="utf-8")
    rc = sg_module._heartbeat_watchdog_main(
        100,
        5,
        str(hb),
        str(tmp_path / "session_done"),
        str(tmp_path / "WATCHDOG_KILLED.json"),
        exists_fn=lambda p: False,
        probe_fn=lambda pid: True,
        mtime_fn=lambda p: 0.0,
        kill_fn=lambda pid: None,
        sleep_fn=lambda s: None,
        now_fn=lambda: 99.0,
        pgrep_fn=lambda pid: [],
        marker_fn=lambda path, payload: markers.append(payload),
        **injections,
    )
    assert rc == 3, "停摆超阈值必须击杀（取证只是附带）"
    return markers[0]


def test_heartbeat_watchdog_marker_carries_job_identity(tmp_path):
    """击杀取证随之落 eid/岗位名/公司/in_job——B1 跳岗台账的写入源。"""
    hb_json = json.dumps(
        {
            "ts": "x",
            "label": "job:2",
            "eid": "e9527",
            "job": "solidity 开发",
            "brand": "企贤",
            "idx": 2,
            "in_job": True,
        }
    )
    marker = _run_stale_watchdog(sg, tmp_path, hb_json, [])
    assert marker["last_label"] == "job:2"
    assert marker["last_eid"] == "e9527"
    assert marker["last_job"] == "solidity 开发"
    assert marker["last_brand"] == "企贤"
    assert marker["in_job"] is True


def test_heartbeat_watchdog_marker_degrades_without_job_keys(tmp_path):
    """旧格式心跳（只有 ts/label）：marker 照写、新字段为 None、击杀不受影响。"""
    marker = _run_stale_watchdog(sg, tmp_path, json.dumps({"ts": "x", "label": "job:11"}), [])
    assert marker["last_label"] == "job:11"
    assert marker["last_eid"] is None and marker["last_job"] is None
    assert marker["in_job"] is None


# --- #56 取证 B：击杀**之前**的有界出网探针（嫌疑③「这台机器此刻不通」的正面取证）-----------
#
# 判读口径（`_selfguard._probe_egress` 的 docstring 是同一条，别在这里加第二份说法）：看门狗是
# **另一个进程**，探到的是它自己视角的网络；浏览器（Camoufox）可能走不同的代理路径——
# 探针失败 = 嫌疑③ 的正面证据，探针成功 **不能**排除嫌疑③。
# 下面全部零网络：三个 IO 注入点（`getaddrinfo` / `create_connection` / `http_fetch_fn`）按本仓
# 既有的依赖注入口径换成假件，`sg.socket` 这个名字整体换成 SimpleNamespace（真 socket 模块一个
# 字节都不碰）。走默认 `_default_http_fetch` 的用例一律另打 `sg._default_http_fetch` 的假件。

# 键集合按**字面量**钉死，不引 `_forensics` 的 `NET_PROBE_*` 常量：这份清单是**读侧契约**
# （人工判读 + runbook 按固定键取值），用常量引写侧等于「写侧改名、用例跟着一起改」——
# 漂移永远绿。2026-10-09 那次纠正正是这种改名（那个带「代理黑洞」比喻的旧键名换成了
# fake-IP 归因键，判读方向同时反转），加上本轮新增的 HTTP 三键。
_NET_PROBE_SHAPE = {
    "host",
    "port",
    "timeout_seconds",
    "dns_ok",
    "tcp_ok",
    "http_ok",
    "http_status",
    "http_total_ms",
    "addresses",
    "resolved_via_proxy_fake_ip",
    "elapsed_ms",
    "error",
    "probe_path",
    "blocked_shape",
    "http_location",
    "http_content_encoding",
}


def _addr_infos(*ips):
    """`getaddrinfo` 的返回形态：[(family, type, proto, canonname, (ip, port))]。
    探测只读 `info[4][0]`，故每条只需把 ip 放在第 5 位——照真 API 的形状造，
    免得实现哪天改读 `info[4]` 整体时假件还在配合错形状。"""
    return [(2, 1, 6, "", (ip, fx.NET_PROBE_PORT)) for ip in ips]


def _ok_conn(closed=None):
    """TCP 段的假连接：探到的连接必须被关掉（探一次留一个 fd 会耗尽进程）。
    `closed` 传列表就把「关了几次」记进去——判空要用 `is not None`，空列表本身是 falsy。"""
    log = closed if closed is not None else []
    return SimpleNamespace(close=lambda: log.append(True))


def _http_stub(log=None, status=200, body_error=None, raises=None, body=b"", location=None, encoding=None):
    """`http_fetch_fn` 的假件，契约 = `_default_http_fetch` 返回的那份小 dict（status / error / body /
    location / content_encoding）。`log` 记「这一段有没有真跑、拿到的超时与 path 是什么」——HTTP 段只在
    TCP 通了之后才跑，而 path 又由击杀那一刻心跳里的 eid 决定，这两条接线判据都必须能读出来。"""

    def fetch(host, timeout, path="/"):
        if log is not None:
            log.append((host, timeout, path))
        if raises is not None:
            raise raises
        return {
            "status": status,
            "error": body_error,
            "body": body,
            "location": location,
            "content_encoding": encoding,
        }

    return fetch


# 时钟四读数（起始 / HTTP 起始 / HTTP 结束 / 结束）：刻意全取二进制可精确表示的秒值，
# 毫秒断言才不会被浮点尾差咬到（0.75-0.25=0.5s ⇒ 500ms，1.0-0.0 ⇒ 1000ms）。
# 量级对齐真机那次往返（2026-10-09 09:41 复测 630ms），断言要的是确定性而不是复现小数。
_NET_PROBE_CLOCK = [0.0, 0.25, 0.75, 1.0]


def test_net_probe_success_records_addresses_timing_and_closed_socket():
    """成功形态：解析到的地址、TCP 成败、**HTTP 往返**与两段各自耗时都进读数，
    主机/端口/超时取自 `_forensics` 那份常量（跨进程单一真源）。
    时钟喂四个读数（起始 / HTTP 起始 / HTTP 结束 / 结束），于是 `elapsed_ms`（整段）与
    `http_total_ms`（HTTP 段自己）分得开——旧形状只有前一个数，答不出「那一次往返等了多久」
    （真机复测的那一次是 630ms，取值口径见 `_NET_PROBE_CLOCK`）。"""
    closed, calls, http_calls = [], [], []

    def getaddrinfo(host, port):
        calls.append(("dns", host, port))
        # 真 API 会把同一个 IP 按 family/type 重复返回（实测一次站点域名解析回 5 条、
        # 只有 2 个不同地址）⇒ 落盘必须去重保序，否则 5 条上限会被重复项吃光
        return _addr_infos("10.0.0.1", "10.0.0.2", "10.0.0.1")

    def create_connection(addr, timeout):
        calls.append(("tcp", addr, timeout))
        return _ok_conn(closed)

    out = sg._probe_egress(
        getaddrinfo_fn=getaddrinfo,
        create_connection_fn=create_connection,
        monotonic_fn=iter(_NET_PROBE_CLOCK).__next__,
        http_fetch_fn=_http_stub(log=http_calls),
    )
    assert out == {
        "host": fx.NET_PROBE_HOST,
        "port": fx.NET_PROBE_PORT,
        "timeout_seconds": fx.NET_PROBE_TIMEOUT_SECONDS,
        "dns_ok": True,
        "tcp_ok": True,
        "http_ok": True,
        "http_status": 200,
        "http_total_ms": 500,
        "addresses": ["10.0.0.1", "10.0.0.2"],
        "resolved_via_proxy_fake_ip": False,
        "elapsed_ms": 1000,
        "error": None,
        # 没传 eid ⇒ 回落首页：2026-10-11 那场探的正是这个形状，它对「取到的是不是内容」一字不答
        "probe_path": "/",
        "blocked_shape": False,
        "http_location": None,
        "http_content_encoding": None,
    }
    assert calls == [
        ("dns", fx.NET_PROBE_HOST, fx.NET_PROBE_PORT),
        ("tcp", (fx.NET_PROBE_HOST, fx.NET_PROBE_PORT), fx.NET_PROBE_TIMEOUT_SECONDS),
    ]
    assert http_calls == [(fx.NET_PROBE_HOST, fx.NET_PROBE_HTTP_TIMEOUT_SECONDS, "/")], (
        "HTTP 段吃自己的 5s 上界与首页 path"
    )
    assert closed == [True], "探到的连接必须关掉"


def test_net_probe_dns_failure_records_only_the_exception_type():
    """DNS 失败是嫌疑③ 的头号形态。只记 `type(e).__name__`：异常文本里常带代理 URL（含凭据）
    与完整主机名，而取证文件是给旁人读的（`_probe_egress` 的口径，也是 #38 落 JSONL 那套的延伸）。"""

    def boom(host, port):
        raise OSError("[Errno -2] Name or service not known via http://user:secret@127.0.0.1:7890")

    out = sg._probe_egress(
        getaddrinfo_fn=boom,
        create_connection_fn=lambda *a: pytest.fail("DNS 都没通就不该再去连（多一次无界等待）"),
        http_fetch_fn=lambda *a: pytest.fail("TCP 都没通就不该再发请求（把「连不上」再测一遍）"),
    )
    assert out["dns_ok"] is False and out["tcp_ok"] is False
    assert out["error"] == "OSError", out
    assert set(out) == _NET_PROBE_SHAPE, "形状恒定要稳：读侧按固定键取，不做形态猜测"
    assert "secret" not in json.dumps(out, ensure_ascii=False), "异常文本一律不落盘"


def test_net_probe_tcp_refusal_keeps_the_dns_reading():
    """解析成功但连不上（拒连/超时）：`dns_ok=True` 与 `error` 并存——两个读数各说各的一段，
    合成一个布尔就答不出「是 DNS 坏了还是 TCP 断了」。此时 HTTP 段**根本不跑**（对端都没连上，
    发请求只是白等一轮），那三键落在 `False`/`None`/`0` 而不是缺键。"""
    out = sg._probe_egress(
        getaddrinfo_fn=lambda h, p: _addr_infos("10.0.0.1"),
        create_connection_fn=lambda *a: (_ for _ in ()).throw(ConnectionRefusedError("Connection refused")),
        http_fetch_fn=lambda *a: pytest.fail("TCP 失败时不许再发 HTTPS 请求"),
    )
    assert out["dns_ok"] is True and out["tcp_ok"] is False
    assert out["error"] == "ConnectionRefusedError", out
    assert (out["http_ok"], out["http_status"], out["http_total_ms"]) == (False, None, 0), out
    assert set(out) == _NET_PROBE_SHAPE


def test_net_probe_flags_the_proxy_fake_ip_range():
    """本机特有读数（**归因**，不是故障证据）：这台机器的系统代理是 127.0.0.1:7890 的 fake-IP
    模式——代理给每个域名分配一个 198.18.0.0/15 里的假地址、真正的转发在 **TCP 层**由代理劫持。
    ⇒ 命中这一段＝「代理正在管这条路」（正常工作态），**不命中才是直连公网**；它回答的是
    「这条读数是走哪条路拿到的」，本身不指向故障。旧注释（2026-10-05，那时还没有真机对照）把它
    判成「代理黑洞＝出网断」，方向是反的：2026-10-09 09:41 那场解析到 198.18.0.116、
    同一刻同机 `urlopen` 拿到 **HTTP 200 / 538056 字节 / 630ms**（出处
    `_forensics.NET_PROBE_FAKE_IP_PREFIXES`）。这是**环境事实**不是通用事实，换机器要重新一一对照。"""
    connect = lambda *a: _ok_conn()  # noqa: E731

    for ips, expected in ((("198.18.0.1", "10.0.0.1"), True), (("198.19.255.7",), True), (("10.0.0.1",), False)):
        out = sg._probe_egress(
            getaddrinfo_fn=lambda h, p, _ips=ips: _addr_infos(*_ips),
            create_connection_fn=connect,
            http_fetch_fn=_http_stub(),
        )
        assert out["resolved_via_proxy_fake_ip"] is expected, (ips, out)
    # 地址落盘有条数上限（取证文件不是 DNS 转储），但布尔读数判的是**全量**地址：
    # 第 6 条才命中 fake-IP 段时，截断后的 addresses 里没有它，布尔仍得是 True。
    tail_case = _addr_infos(*(["10.0.0.%d" % i for i in range(1, 6)] + ["198.18.9.9"]))
    out = sg._probe_egress(
        getaddrinfo_fn=lambda h, p: tail_case, create_connection_fn=connect, http_fetch_fn=_http_stub()
    )
    assert len(out["addresses"]) == fx.NET_PROBE_MAX_ADDRESSES
    assert out["resolved_via_proxy_fake_ip"] is True, "截断只影响落盘条数，不该吃掉布尔读数"


def test_net_probe_never_raises_and_always_returns_the_same_shape():
    """降级铁律的形状那一半：注入件抛**任何**东西（含一条形态怪异的地址、close 炸掉、
    时钟本身坏掉、HTTP 段返回的形状不对）都必须返回同一份键集——`_heartbeat_watchdog_main`
    里那层兜底会把字段落成 None，读的人就分不清「开关关掉了」还是「探测跑到一半坏了」。"""
    cases = [
        {"getaddrinfo_fn": lambda h, p: (_ for _ in ()).throw(ZeroDivisionError("x"))},
        # 下面每条 `tcp_ok` 都会升到 True（连接成败在 close 之前就已记下）⇒ **必须**注入 HTTP 段假件，
        # 否则走默认 `_default_http_fetch` 就是真打网络，本段用例的零网络承诺当场失效。
        {
            "getaddrinfo_fn": lambda h, p: [("这条压根不是地址",)],
            "create_connection_fn": lambda *a: None,
            "http_fetch_fn": _http_stub(),
        },
        {
            "getaddrinfo_fn": lambda h, p: _addr_infos("10.0.0.1"),
            "create_connection_fn": lambda *a: SimpleNamespace(
                close=lambda: (_ for _ in ()).throw(RuntimeError("close boom"))
            ),
            "http_fetch_fn": _http_stub(),
        },
        {
            "getaddrinfo_fn": lambda h, p: None,
            "create_connection_fn": lambda *a: SimpleNamespace(close=lambda: None),
            "monotonic_fn": lambda: (_ for _ in ()).throw(RuntimeError("clock boom")),
            "http_fetch_fn": _http_stub(),
        },
        # arity 对、回来的却不是那份 dict（`info["status"]` 当场炸）：同样只进 `error`、键一个不少。
        # 这条**故意**留在形状用例里：元组契约靠解包失败触发、dict 契约靠取值失败触发，
        # 两种坏法都由同一条断言兜住——形状怎么坏，键集合都不许跟着少。
        {
            "getaddrinfo_fn": lambda h, p: _addr_infos("10.0.0.1"),
            "create_connection_fn": lambda *a: SimpleNamespace(close=lambda: None),
            "http_fetch_fn": lambda h, t, p: None,
        },
    ]
    for kwargs in cases:
        out = sg._probe_egress(**kwargs)
        assert set(out) == _NET_PROBE_SHAPE, kwargs
    # getaddrinfo 返回 None（假件形态）⇒ 解析到 0 条但 `dns_ok` 仍 True（这一步没报错）；
    # close 抛异常也不改 tcp_ok=True（连接成败在 close 之前就已经记下了）。
    ok = sg._probe_egress(
        getaddrinfo_fn=lambda h, p: _addr_infos("10.0.0.1"),
        create_connection_fn=lambda *a: SimpleNamespace(close=lambda: (_ for _ in ()).throw(RuntimeError("boom"))),
        http_fetch_fn=_http_stub(),
    )
    assert ok["tcp_ok"] is True and ok["error"] is None and ok["addresses"] == ["10.0.0.1"]


class _TlsBoomError(Exception):
    """TLS 握手失败的替身（真件是 `ssl.SSLCertVerificationError` 一族）：探针只记
    `type(e).__name__`，用一个自定义类名正好证明记的是类名而不是文本。"""


class _BodyCutError(Exception):
    """读体阶段的截断（真件是 `http.client.IncompleteRead`）：只在
    `test_default_http_fetch_does_not_raise_when_the_body_read_breaks` 里当标记用。"""


def test_net_probe_http_leg_any_status_code_means_the_route_is_up():
    """`http_ok` 答的是「路通不通」，不是「页面好不好」：**任何**已收到的状态码都算往返成功
    （403/404/500 都是对面真的把响应发回来了）。把 4xx/5xx 读成「不通」会把风控插页、
    站点报错这类「路是通的」形态误写进嫌疑③。"""
    for status in (200, 302, 403, 404, 500, 503):
        out = sg._probe_egress(
            getaddrinfo_fn=lambda h, p: _addr_infos("10.0.0.1"),
            create_connection_fn=lambda *a: _ok_conn(),
            monotonic_fn=iter(_NET_PROBE_CLOCK).__next__,
            http_fetch_fn=_http_stub(status=status),
        )
        assert out["http_ok"] is True and out["http_status"] == status, out
        assert out["error"] is None and out["http_total_ms"] == 500, out


@pytest.mark.parametrize(
    "exc,expected_name",
    [
        (ConnectionRefusedError("Connection refused"), "ConnectionRefusedError"),
        (TimeoutError("timed out"), "TimeoutError"),  # socket.timeout 在 3.10+ 就是这个别名
        (_TlsBoomError("SSL record exceeds length"), "_TlsBoomError"),  # TLS 握手失败一族按类名记
    ],
)
def test_net_probe_http_exception_keeps_tcp_ok_and_fails_http(exc, expected_name):
    """「tcp_ok=True 而 http_ok=False」＝连上了对端（本机就是那个本地代理）却取不回内容——
    2026-10-09 那场缺的正是这一格，它是嫌疑③ 的正面证据。异常只记**类名**（文本可能带凭据），
    且只进 `error`：`dns_ok`/`tcp_ok` 的前两段读数一个字不能被覆盖。"""
    out = sg._probe_egress(
        getaddrinfo_fn=lambda h, p: _addr_infos("10.0.0.1"),
        create_connection_fn=lambda *a: _ok_conn(),
        monotonic_fn=iter(_NET_PROBE_CLOCK).__next__,
        http_fetch_fn=_http_stub(raises=exc),
    )
    assert out["dns_ok"] is True and out["tcp_ok"] is True, out
    assert out["http_ok"] is False and out["http_status"] is None, out
    assert out["error"] == expected_name, out
    # 往返失败也留着耗时读数（它是「等了多久才死」的证据，与成败读数互不覆盖）
    assert out["http_total_ms"] == 500, out
    assert set(out) == _NET_PROBE_SHAPE


def test_net_probe_truncated_body_keeps_both_readings():
    """状态码已到手、读体被截断（`_default_http_fetch` 把它回传给调用方而不是外抛）：
    `http_ok is True`（路通了）与 `error="IncompleteRead"`（那一次截断本身是信号）**并存**。
    这是本次设计的要点，别让其中一个吃掉另一个——半路截断反写成「没通」会把嫌疑③ 的读数造假。"""
    out = sg._probe_egress(
        getaddrinfo_fn=lambda h, p: _addr_infos("10.0.0.1"),
        create_connection_fn=lambda *a: _ok_conn(),
        http_fetch_fn=_http_stub(status=200, body_error="IncompleteRead"),
    )
    assert out["http_ok"] is True and out["http_status"] == 200, out
    assert out["error"] == "IncompleteRead", out


def test_net_probe_key_set_is_identical_from_dns_failure_to_full_round_trip():
    """读侧契约（缺键＝静默缺字段，不报错）：最惨分支（DNS 就抛）与最好的分支（三段全通）
    给出的键集合**同一个**，且等于这份显式清单——含新增的 HTTP 三键与 fake-IP 归因键。
    期望清单在这里逐字抄一遍是刻意的：`_NET_PROBE_SHAPE` 被一堆用例共用，这份独立清单
    保证「有人顺手往共用集合里加键」时仍有一条用例红。"""
    expected = {
        "host",
        "port",
        "timeout_seconds",
        "dns_ok",
        "tcp_ok",
        "http_ok",
        "http_status",
        "http_total_ms",
        "addresses",
        "resolved_via_proxy_fake_ip",
        "elapsed_ms",
        "error",
        "probe_path",
        "blocked_shape",
        "http_location",
        "http_content_encoding",
    }
    worst = sg._probe_egress(getaddrinfo_fn=lambda h, p: (_ for _ in ()).throw(OSError("no dns")))
    best = sg._probe_egress(
        getaddrinfo_fn=lambda h, p: _addr_infos("198.18.0.116"),
        create_connection_fn=lambda *a: _ok_conn(),
        http_fetch_fn=_http_stub(status=200),
    )
    assert set(worst) == set(best) == expected, (set(worst) ^ expected, set(best) ^ expected)
    assert worst["http_ok"] is False and best["http_ok"] is True, "同一个形状，不同的读数"


def test_default_http_fetch_returns_status_and_reads_a_bounded_prefix(monkeypatch):
    """`_default_http_fetch` 本体（生产默认那段）：返回那份小 dict（状态码 / body / 两个响应头），
    并且**只读 `_NET_PROBE_BODY_BYTES` 个字节**——实测首页 538056 字节（2026-10-09 09:41 复测），
    全灌进内存只是让击杀前多等几秒。零网络：`http.client` 的连接对象换成替身。"""
    seen = {}

    class _Resp:
        status = 200
        headers = {"Location": "/web/passport/zp/security.html?seed=x", "Content-Encoding": None}

        def getheader(self, name, default=None):
            seen.setdefault("headers", []).append(name)
            return self.headers.get(name, default)

        def read(self, n):
            seen["read_n"] = n
            return b"x" * n

    class _Conn:
        def __init__(self, host, timeout=None):
            seen["host"], seen["timeout"] = host, timeout

        def request(self, method, path, headers=None):
            seen["request"] = (method, path)

        def getresponse(self):
            return _Resp()

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(sg.http.client, "HTTPSConnection", _Conn)
    got = sg._default_http_fetch(fx.NET_PROBE_HOST, fx.NET_PROBE_HTTP_TIMEOUT_SECONDS)
    assert got["status"] == 200 and got["error"] is None, got
    assert got["body"] == b"x" * sg._NET_PROBE_BODY_BYTES, got  # body 不再丢掉：它就是判形态的原料
    # 两个响应头必须由**生产那段**自己读出来：插页的主通道写在头里，只在替身层测不到这一句
    assert got["location"] == "/web/passport/zp/security.html?seed=x" and got["content_encoding"] is None, got
    assert seen["headers"] == ["Location", "Content-Encoding"], seen
    assert seen["host"] == fx.NET_PROBE_HOST and seen["timeout"] == fx.NET_PROBE_HTTP_TIMEOUT_SECONDS
    assert seen["request"] == ("GET", "/")
    assert seen["read_n"] == sg._NET_PROBE_BODY_BYTES and seen["closed"] is True, seen


def test_default_http_fetch_feeds_the_redirect_channel_end_to_end(monkeypatch):
    """通道②必须在**生产实现**上成立，不能只在替身层自证：这里只把 `http.client` 的连接对象换成
    替身，`_default_http_fetch` 与 `_probe_egress` 都走真代码。夹具照 2026-10-15 那次实测造
    （302 + `Location: /web/passport/zp/security.html…` + body 为空）。缺这条，「读头认插页」整件事
    就只由一个自己造返回值的假件证明过——本仓 #33 与「验证门禁的测试自己也会空过」点名的正是这种。"""

    class _Resp:
        status = 302

        def getheader(self, name, default=None):
            return {"Location": "/web/passport/zp/security.html?seed=x&name=y"}.get(name, default)

        def read(self, n):
            return b""

    class _Conn:
        paths = []

        def __init__(self, host, timeout=None):
            return None

        def request(self, method, path, headers=None):
            _Conn.paths.append(path)

        def getresponse(self):
            return _Resp()

        def close(self):
            return None

    monkeypatch.setattr(sg.http.client, "HTTPSConnection", _Conn)
    out = sg._probe_egress(
        getaddrinfo_fn=lambda h, p: _addr_infos("10.0.0.1"),
        create_connection_fn=lambda *a: _ok_conn(),
        eid="85dcd5da960fe18203152d-1FVBW",
    )
    want = "/job_detail/85dcd5da960fe18203152d-1FVBW.html"
    assert _Conn.paths == [want], _Conn.paths  # 真发出去的就是详情页形状
    assert (out["http_ok"], out["http_status"], out["blocked_shape"]) == (True, 302, True), out
    assert out["probe_path"] == want, out
    assert out["http_location"] == "/web/passport/zp/security.html?seed=x&name=y", out


def test_default_http_fetch_does_not_raise_when_the_body_read_breaks(monkeypatch):
    """契约的另一半：状态码已到手之后的读体异常**不外抛**，而是把类名回给调用方
    （路已经证明通了，半路截断不能反写成「没通」，但那次截断本身是信号，也不能整个吞掉）；
    连接都没建立时照旧**外抛**（那种情况没有状态码可记，由 `_probe_egress` 兜进 `error`）。
    `close()` 抛异常只是漏一个 fd，读数已经到手，不许把已成立的往返作废。"""

    class _Resp:
        status = 200

        def getheader(self, name, default=None):
            return default  # 读体断了也不影响这两个头早已在手

        def read(self, n):
            raise _BodyCutError("connection interrupted mid-body")

    class _Conn:
        def __init__(self, host, timeout=None):
            return None

        def request(self, method, path, headers=None):
            return None

        def getresponse(self):
            return _Resp()

        def close(self):
            raise RuntimeError("close boom")  # 关不掉也不许盖掉已经到手的读数

    monkeypatch.setattr(sg.http.client, "HTTPSConnection", _Conn)
    got2 = sg._default_http_fetch(fx.NET_PROBE_HOST, 1)
    assert (got2["status"], got2["error"], got2["body"]) == (200, "_BodyCutError", None), got2
    assert (got2["location"], got2["content_encoding"]) == (None, None), got2  # 截断不牵连已经读到的头

    class _Conn2:
        def __init__(self, host, timeout=None):
            return None

        def request(self, method, path, headers=None):
            raise ConnectionRefusedError("connect failed")

    monkeypatch.setattr(sg.http.client, "HTTPSConnection", _Conn2)
    with pytest.raises(ConnectionRefusedError):
        sg._default_http_fetch(fx.NET_PROBE_HOST, 1)


def _egress_with_body(body, status=200, eid=None):
    """跑一次「三段全通、HTTP 段回给定 body」的探测——插页判据的用例族共用这份形状。"""
    return sg._probe_egress(
        getaddrinfo_fn=lambda h, p: _addr_infos("10.0.0.1"),
        create_connection_fn=lambda *a: _ok_conn(),
        http_fetch_fn=_http_stub(status=status, body=body),
        eid=eid,
    )


def test_net_probe_points_at_the_detail_shape_of_the_stalled_job():
    """探测目标跟着 eid 走（本次纠正的核心）：会话正卡在**某个岗的详情往返**上，而首页常年回
    302——探它问不出「这一类端点此刻给不给内容」，2026-10-11 那场就是把 `http_ok=true`
    误读成了「排除嫌疑③」。有 eid 就必须把 path 拼成 `/job_detail/{eid}.html`。"""
    calls = []
    eid = "85dcd5da960fe18203152d-1FVBW"  # 2026-10-11 那场死在 pointer:mouse-move:1:pre 的岗
    out = sg._probe_egress(
        getaddrinfo_fn=lambda h, p: _addr_infos("10.0.0.1"),
        create_connection_fn=lambda *a: _ok_conn(),
        http_fetch_fn=_http_stub(log=calls, status=200, body=b"<title>x</title>"),
        eid=eid,
    )
    want = f"/job_detail/{eid}.html"
    assert calls == [(fx.NET_PROBE_HOST, fx.NET_PROBE_HTTP_TIMEOUT_SECONDS, want)], calls
    assert out["probe_path"] == want, out


@pytest.mark.parametrize(
    "body,why",
    [
        (
            b"<title>\xe5\xae\x89\xe5\x85\xa8\xe9\xaa\x8c\xe8\xaf\x81 - BOSS</title>",
            "安全验证页（2026-10-11 实测四条详情页全落这张）",
        ),
        (
            b"location = 'https://www.zhipin.com/web/passport/zp/verify.html?callbackUrl=x'",
            "verify.html 跳转壳（同一场的落地 URL）",
        ),
        (
            "<title>请稍候 - BOSS直聘</title>".encode("utf-8"),
            "「请稍候」插页（2026-10-15 复测：不再跳 verify、status 仍 200）",
        ),
    ],
)
def test_net_probe_blocked_shape_names_the_two_measured_interstitials(body, why):
    """路通 + 状态码正常 + 取回的是拦截/挑战页 ＝ 嫌疑③ 的**另一种**正面证据（取到响应≠取到内容）。
    两格是并存读数：`blocked_shape=true` 不许把 `http_ok` 吃掉，反向也一样。"""
    out = _egress_with_body(body, eid="85dcd5da960fe18203152d-1FVBW")
    assert out["http_ok"] is True and out["http_status"] == 200, (why, out)
    assert out["blocked_shape"] is True, why


def test_net_probe_redirect_channel_is_the_primary_signal_on_the_measured_scene():
    """通道②（3xx + Location）不是补充，是主通道。2026-10-15 用**生产探针自己的形状**
    （`http.client`、UA=`auto-job-cn-selfguard-net-probe/1.0`、无 cookie、不跟跳转）实测那条
    详情页：`status=302`、`Location=/web/passport/zp/security.html?seed=…`、`Content-Length: 0`、
    **body 是空的**。只读 body 的判据在这个现场（也就是本次改动自己的 motivating 案例）上答
    False——那就是抓不到目标的证据，所以这条通道单独钉住，不靠文案通道顺带覆盖。"""
    loc = "/web/passport/zp/security.html?seed=vDWL4%2BBOyHWiI6NBIcnUcrnj65DLwrp4Ul1I1953hmo%3D&name="
    out = sg._probe_egress(
        getaddrinfo_fn=lambda h, p: _addr_infos("10.0.0.1"),
        create_connection_fn=lambda *a: _ok_conn(),
        http_fetch_fn=_http_stub(status=302, body=b"", location=loc),
        eid="85dcd5da960fe18203152d-1FVBW",
    )
    assert out["http_ok"] is True and out["http_status"] == 302, "路通与「取到的是不是内容」两格并存"
    assert out["http_location"] == loc, out
    assert out["blocked_shape"] is True, "302 进认证/安全流程＝插页，body 为空也要认出来"


@pytest.mark.parametrize(
    "status,location,why",
    [
        (302, "/web/geek/jobs?query=Web3", "3xx 但指向普通引导页＝不是插页"),
        (200, "/web/passport/zp/verify.html", "200 却带着 Location 头＝没在跳转，不算命中"),
        (301, None, "3xx 但没有 Location＝这趟没被引导，别替它编"),
        (200, None, "普通 200"),
    ],
)
def test_net_probe_redirect_channel_also_only_accepts_a_hit(status, location, why):
    """通道②同通道①：**只认命中**。假阳性会把一次正常的引导读成「站点在拦我们」，
    那比漏判更坏——它会让人去换出口，而真正要查的是别的东西。"""
    out = sg._probe_egress(
        getaddrinfo_fn=lambda h, p: _addr_infos("10.0.0.1"),
        create_connection_fn=lambda *a: _ok_conn(),
        http_fetch_fn=_http_stub(status=status, location=location, body=b"<html>ok</html>"),
    )
    assert out["blocked_shape"] is False, (why, out)


def test_net_probe_keeps_content_encoding_so_the_body_channel_can_be_disqualified():
    """`http_content_encoding` 是那条**已知假阴性**的出口：`http.client` 不解压，压缩载荷里
    中文特征串必然匹配不上。落这一格不是为了判插页，而是为了让读的人看得出「body 判据这一趟
    没有发言权」——否则 `blocked_shape=false` 会被读成「取到了内容」。"""
    co = zlib.compressobj(9, zlib.DEFLATED, 31)
    gz = co.compress("安全验证".encode("utf-8")) + co.flush()
    out = sg._probe_egress(
        getaddrinfo_fn=lambda h, p: _addr_infos("10.0.0.1"),
        create_connection_fn=lambda *a: _ok_conn(),
        http_fetch_fn=_http_stub(status=200, body=gz, encoding="gzip"),
    )
    assert out["http_content_encoding"] == "gzip" and out["blocked_shape"] is False, out


def test_net_probe_blocked_shape_is_only_a_hit_test_not_a_proof_of_content():
    """判据**只认命中**：真岗位页 False，带 gzip 头的压缩字节也 False——`http.client` 不解压，
    中文特征串必然匹配不上。这条**已知假阴性**写死在用例里，防的是把 `blocked_shape=false`
    反着读成「取到了内容」——那正是本次要修的误读的镜像版本。非 bytes 形态入参同样 False。"""
    co = zlib.compressobj(9, zlib.DEFLATED, 31)  # wbits=31 ⇒ 真 gzip 容器：deflate 之后的载荷里没有可读中文
    gzipish = co.compress("安全验证".encode("utf-8")) + co.flush()
    for body, why in (
        ("<title>web3资深产品经理（区块链）_BOSS直聘</title>".encode("utf-8"), "真内容"),
        (gzipish, "压缩字节（已知假阴性）"),
        (None, "注入件给了非字节形态"),
    ):
        out = _egress_with_body(body)
        assert out["blocked_shape"] is False, (why, out)
        assert out["http_ok"] is True, why


def test_probe_path_quotes_a_hostile_eid():
    """eid 是从心跳文件里读出来的字符串：带 CR/LF 的坏值裸拼进请求行＝请求头注入。
    探测自己绝不许成为新的失败面（降级铁律的另一半），所以 path 必须过 `quote`。"""
    path = sg._probe_path("a\r\nX-Injected: 1")
    assert "\r" not in path and "\n" not in path, path
    assert path.startswith("/job_detail/a"), path
    assert sg._probe_path(None) == "/" and sg._probe_path("") == "/", "缺 eid 回落首页（旧调用点语义不变）"


def test_kill_evidence_points_the_probe_at_the_heartbeat_eid(tmp_path, monkeypatch):
    """接线（#33 教训：只在函数里实现、生产路径不接＝白做，单元层还全绿）：击杀取证必须把
    **心跳里那个岗**的 eid 交给探测。这条不走注入的 `net_probe_fn`，走的是生产默认探测 +
    假 socket/假 HTTP 段，所以「marker 里的 path 含该 eid」只能由真链路给出。"""
    monkeypatch.setenv(fx.NET_PROBE_ENV, "1")
    seen = []

    def fetch(host, timeout, path="/"):
        seen.append(path)
        # 通道①：跟完跳转、200 + 带文案的 body
        return {
            "status": 200,
            "error": None,
            "body": "<title>安全验证</title>".encode("utf-8"),
            "location": None,
            "content_encoding": None,
        }

    monkeypatch.setattr(
        sg,
        "socket",
        SimpleNamespace(getaddrinfo=lambda *a: _addr_infos("10.0.0.1"), create_connection=lambda *a: _ok_conn()),
    )
    monkeypatch.setattr(sg, "_default_http_fetch", fetch)
    marker = _run_stale_watchdog(
        sg, tmp_path, json.dumps({"ts": "x", "label": "jd:dom-probe:post", "eid": "e42", "in_job": True}), []
    )
    assert seen == ["/job_detail/e42.html"], seen
    probe = marker[fx.NET_PROBE_MARKER_KEY]
    assert probe["probe_path"] == "/job_detail/e42.html" and probe["blocked_shape"] is True, probe


def test_kill_evidence_falls_back_to_homepage_when_no_job_is_open(tmp_path, monkeypatch):
    """起飞前自检段没有「正在哪个岗上」（心跳缺 eid，2026-10-07 实测那一场正是这副形状）：
    探测回落首页，但两格新键**恒在**——path 落 "/"、blocked_shape 落判出来的值。
    读侧不该为旧形状写分支，也不该因为缺岗就把这一格省掉。"""
    monkeypatch.setenv(fx.NET_PROBE_ENV, "1")
    monkeypatch.setattr(
        sg,
        "socket",
        SimpleNamespace(getaddrinfo=lambda *a: _addr_infos("10.0.0.1"), create_connection=lambda *a: _ok_conn()),
    )
    monkeypatch.setattr(
        sg,
        "_default_http_fetch",
        lambda host, timeout, path="/": {
            "status": 200,
            "error": None,
            "body": b"<title>ok</title>",
            "location": None,
            "content_encoding": None,
        },
    )
    marker = _run_stale_watchdog(
        sg, tmp_path, json.dumps({"ts": "x", "label": "pointer:mouse-click:pre", "in_job": False}), []
    )
    probe = marker[fx.NET_PROBE_MARKER_KEY]
    assert probe["probe_path"] == "/" and probe["blocked_shape"] is False, probe


def test_surface_prior_wedge_names_an_interstitial_reading(tmp_path, capsys):
    """插页命中要点名写进开跑呈报：它与「路不通」是两种处置（前者换出口/等风控冷却，后者先查
    本机网络），只躺在 JSON 里等于没告诉操作者。未命中与旧 marker 缺键都**不许**出声。"""
    old = tmp_path / "20261011-091037"
    old.mkdir()
    marker = {
        "stale_seconds": 240,
        "last_label": "pointer:mouse-move:1:pre",
        "last_job": "web3资深产品经理（区块链）-SH",
        "last_eid": "85dcd5da960fe18203152d-1FVBW",
        fx.NET_PROBE_MARKER_KEY: {
            "http_ok": True,
            "http_status": 200,
            fx.NET_PROBE_PATH_KEY: "/job_detail/85dcd5da960fe18203152d-1FVBW.html",
            fx.NET_PROBE_BLOCKED_KEY: True,
        },
    }
    (old / "WATCHDOG_KILLED.json").write_text(json.dumps(marker, ensure_ascii=False), encoding="utf-8")
    mod._surface_prior_wedge(tmp_path, "20261015-090000")
    out = capsys.readouterr().out
    assert "站点插页" in out and "job_detail" in out, out

    marker[fx.NET_PROBE_MARKER_KEY][fx.NET_PROBE_BLOCKED_KEY] = False
    (old / "WATCHDOG_KILLED.json").write_text(json.dumps(marker, ensure_ascii=False), encoding="utf-8")
    mod._surface_prior_wedge(tmp_path, "20261015-090000")
    assert "插页" not in capsys.readouterr().out, "未命中不许制造噪音"

    legacy = {"stale_seconds": 241, "last_label": "click:mouse-click:pre"}
    (old / "WATCHDOG_KILLED.json").write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    mod._surface_prior_wedge(tmp_path, "20261015-090000")
    out = capsys.readouterr().out
    assert "插页" not in out and "click:mouse-click:pre" in out, "旧 marker 缺键＝正常读法，不是缺陷"


def test_net_probe_default_entry_skips_everything_when_switch_off(monkeypatch):
    """开关 off ⇒ `_default_net_probe` **一次都不探测**（连 `socket` 与 `http.client` 这两个名字
    都被换成会记账的假件，也没被调用）并返回 None；开关 on ⇒ 走真 `_probe_egress` + 真默认
    HTTP 段。这就是「关掉了」与「探了但失败」在读数上的区分口径（后者一定是字典）。"""
    called = []

    def fake_getaddrinfo(*a):
        called.append("dns")
        return _addr_infos("10.0.0.1")

    def fake_connect(*a):
        called.append("tcp")
        return _ok_conn()

    def fake_http(host, timeout, path="/"):
        called.append("http")
        return {"status": 200, "error": None, "body": b"", "location": None, "content_encoding": None}

    monkeypatch.setattr(sg, "socket", SimpleNamespace(getaddrinfo=fake_getaddrinfo, create_connection=fake_connect))
    # 默认件本体也在 `_probe_egress` 里按模块名取用：不打这里就会真打 HTTPS（本用例必须零网络）
    monkeypatch.setattr(sg, "_default_http_fetch", fake_http)
    monkeypatch.setenv(fx.NET_PROBE_ENV, "off")
    assert sg._default_net_probe() is None
    assert called == [], "关掉时不许留任何 socket/HTTP 调用"
    monkeypatch.setenv(fx.NET_PROBE_ENV, "on")
    out = sg._default_net_probe()
    assert isinstance(out, dict) and called == ["dns", "tcp", "http"], out
    assert set(out) == _NET_PROBE_SHAPE and out["dns_ok"] is True and out["http_ok"] is True, out


def test_heartbeat_watchdog_probe_field_none_when_switch_off(tmp_path, monkeypatch):
    """击杀路径上开关关掉的形态：marker 里那个键**恒在**、值为 None，其余字段一字不少
    （读的人不用先猜这一场到底开没开过探测）。"""
    monkeypatch.setenv(fx.NET_PROBE_ENV, "off")
    monkeypatch.setattr(
        sg,
        "socket",
        SimpleNamespace(
            getaddrinfo=lambda *a: pytest.fail("关掉时不许碰 socket"),
            create_connection=lambda *a: pytest.fail("关掉时不许碰 socket"),
        ),
    )
    marker = _run_stale_watchdog(sg, tmp_path, json.dumps({"ts": "x", "label": "job:11"}), [])
    assert marker[fx.NET_PROBE_MARKER_KEY] is None
    assert marker["last_label"] == "job:11"


def test_heartbeat_watchdog_asks_for_egress_before_killing(tmp_path, monkeypatch):
    """开关 on 时的接线证明：marker 里的 `net_probe` 是**探测结果字典**（不是 None），
    且用的是 `_forensics` 那份常量；退出码仍是 3、击杀照常。
    这一族形态是「解析被代理接管（fake-IP 段）+ TCP 连不上」＝嫌疑③ 的正面证据；
    HTTP 段此时**不该跑**，故默认件这里换成会喊的假件（本用例仍然零网络）。"""
    monkeypatch.setenv(fx.NET_PROBE_ENV, "1")
    monkeypatch.setattr(
        sg,
        "socket",
        SimpleNamespace(
            getaddrinfo=lambda *a: _addr_infos("198.18.0.7"),
            create_connection=lambda *a: (_ for _ in ()).throw(OSError("timed out")),
        ),
    )
    monkeypatch.setattr(sg, "_default_http_fetch", lambda *a: pytest.fail("TCP 都没连上就不该发 HTTPS 请求"))
    marker = _run_stale_watchdog(sg, tmp_path, json.dumps({"ts": "x", "label": "job:11", "in_job": True}), [])
    probe = marker[fx.NET_PROBE_MARKER_KEY]
    assert set(probe) == _NET_PROBE_SHAPE, probe
    assert probe["host"] == fx.NET_PROBE_HOST and probe["timeout_seconds"] == fx.NET_PROBE_TIMEOUT_SECONDS
    assert probe["error"] == "OSError" and probe["resolved_via_proxy_fake_ip"] is True, probe
    assert (probe["http_ok"], probe["http_status"], probe["http_total_ms"]) == (False, None, 0), probe


def test_heartbeat_watchdog_mirror_keys_come_from_the_heartbeat(tmp_path):
    """取证 A 的三键经由 marker 落盘的往返：心跳里有它们就带上、没有就落 None（旧格式心跳
    由上面那条 `..._degrades_without_job_keys` 钉）。"""
    hb = json.dumps(
        {
            "ts": "x",
            "label": "handler:detail.json:enter",
            "eid": "e9527",
            "job": "solidity 开发",
            "brand": "企贤",
            "idx": 11,
            "in_job": True,
            "handlers_live": 4,
            "handlers_inflight": 1,
            "handler_last_frag": "/wapi/zpgeek/job/detail.json",
        }
    )
    marker = _run_stale_watchdog(sg, tmp_path, hb, [])
    assert (marker["handlers_live"], marker["handlers_inflight"]) == (4, 1), marker
    assert marker["handler_last_frag"] == "/wapi/zpgeek/job/detail.json"
    # 击杀判据不看这三键：它们只是并排写出来的取证字段
    assert marker["last_eid"] == "e9527"


def test_heartbeat_watchdog_probe_exception_still_kills(tmp_path):
    """探测件抛异常 ⇒ 击杀仍然发生（`_run_stale_watchdog` 里已断言 rc==3）、marker 其余字段照写、
    那个字段落 None。止损永远优先于取证，这是 #38/#54 那条降级铁律在击杀路径上的形态。"""

    def boom(eid=None):
        raise RuntimeError("probe exploded")

    markers = []
    marker = _run_stale_watchdog(
        sg, tmp_path, json.dumps({"ts": "x", "label": "job:9", "eid": "e9"}), markers, net_probe_fn=boom
    )
    assert marker[fx.NET_PROBE_MARKER_KEY] is None
    assert marker["last_label"] == "job:9" and marker["last_eid"] == "e9", "探测坏了不许连累身份字段"
    assert len(markers) == 1, "一次击杀只落一份 marker"


def test_surface_prior_wedge_reports_killed_job(tmp_path, capsys):
    """开跑呈报把岗位身份一并报出来（拿到什么报什么）——重启前就知道「这场死在哪个岗上」。
    旧格式取证（无 eid 键）仍只报 label，由 test_surface_prior_wedge_warns_with_marker 锁住。"""
    old = tmp_path / "20260928-012512"
    old.mkdir()
    (old / "WATCHDOG_KILLED.json").write_text(
        json.dumps(
            {
                "stale_seconds": 240,
                "last_label": "job:2",
                "last_eid": "e9527",
                "last_job": "solidity开发 50-80K",
                "in_job": True,
            }
        ),
        encoding="utf-8",
    )
    mod._surface_prior_wedge(tmp_path, "20260929-000000")
    out = capsys.readouterr().out
    assert "job:2" in out and "e9527" in out and "solidity开发" in out


# --- #56 A4：楔死自动落全栈（faulthandler 随每个心跳节拍武装；无 root、不等 py-spy）---
#
# 楔死时主线程停在无超时的协议往返上，进程内不可能再执行任何 Python 代码（票面机制口径 §2）
# ——「事后取栈」的口子是关的，只能提前把解释器级定时器武装好，到点由解释器自己写全栈。
# 本段用例零网络、不起真实浏览器：被楔住的只是「这一拍之后再没有下一拍」这件事。


@pytest.fixture(autouse=True)
def _isolate_wedge_dump_state():
    """live_apply 的取栈状态（文件句柄 + 武装失败标记）是模块级单例，用例间必须复位：
    残留的 fd 指向已销毁的 tmp 目录，残留的失败标记会让后续用例根本不走武装分支。
    （env 默认关与进程级定时器的兜底 cancel 在 scripts/conftest.py 的 `_isolate_wedge_dump`。）"""
    yield
    mod._reset_wedge_dump_state()


class _FakeFaulthandler:
    """faulthandler 替身：只记调用序列。结构性口径（先 cancel 再 arm、开关关时零调用、
    收尾的取消顺序）只能靠替身看得见；真落盘由下面的 0.3s 用例单独验。
    `raises` / `cancel_raises` 用来走「武装失败」与「取消失败」两条降级分支。"""

    def __init__(self, raises=False, cancel_raises=False):
        self.calls = []
        self.raises = raises
        self.cancel_raises = cancel_raises

    def dump_traceback_later(self, seconds, file=None, exit=False):
        self.calls.append(("arm", seconds, getattr(file, "name", None)))
        if self.raises:
            raise RuntimeError("faulthandler unavailable")

    def cancel_dump_traceback_later(self):
        self.calls.append(("cancel",))
        if self.cancel_raises:
            raise RuntimeError("cancel refused")


def _stall_like_a_wedge(seconds):
    """假装主线程楔死：睡在**这一帧**里不回来（与停在 `send(..., timeout=None)` 上等价）。
    断言靠的就是栈里出现这个函数名——它证明落下来的确实是「被楔住的那个调用点」的栈。"""
    time.sleep(seconds)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, True),
        ("1", True),
        ("on", True),
        ("", True),
        ("ofs", True),
        ("false", True),
        ("no", True),
        ("0", False),
        ("off", False),
        ("OFF", False),
        (" off ", False),
    ],
)
def test_wedge_dump_switch_parsing(monkeypatch, raw, expected):
    """LIVE_WEDGE_DUMP 默认开；只有显式 0/off（大小写与首尾空白不敏感）才关。
    写错值（"ofs"）与空串一律按「开」——关掉取证的代价是下一次楔死又只剩心跳一条线索，
    而留着取证没有任何代价（本票 5 场楔死就是这么过来的）。
    `false` / `no` 也在「按开」这一侧：这是**本开关自己的**取值表，与仓内另两处布尔 env
    都不同（对照 `test_three_bool_env_switches_have_three_distinct_value_tables`）——
    日后若要对齐，改这条用例就是显式决策点，不许顺手漂。"""
    if raw is None:
        monkeypatch.delenv(mod.WEDGE_DUMP_ENV, raising=False)
    else:
        monkeypatch.setenv(mod.WEDGE_DUMP_ENV, raw)
    assert mod._wedge_dump_enabled() is expected


def test_three_bool_env_switches_have_three_distinct_value_tables(monkeypatch, tmp_path, capsys):
    """#56 修复轮 F2（事实核对，`docs/conventions/fact-verification.md`）：A4 开关的旧 docstring
    自称「口径照仓内 LIVE_RECORD / LIVE_PREFLIGHT」，逐字去读那两处后**其实是三套不同的表**。

    **取值表的逐行对照只有一份真源**，在 `_limits.py` 的「布尔 env 开关」小节（#59 修复轮 R4-4：
    原先这张表在本模块、`_forensics`、`_wedge_ledger`、`live_apply` 两处与这条用例里各抄了一份，
    六处维护必然漂）——本用例不再复述它，只按**行为**钉：同一批取值喂给四个开关各自的真实调用点
    （`_init_live_session` 出口的 `run.record` / `_preflight_click_check` 的自检是否跳过 /
    `_wedge_dump_enabled()` / `_wedge_skip_enabled()`），逐行钉死期望方向，再钉「三条方向向量
    两两不同」。方向向量的每一格就是那张表的一行 × 一列，表漂了这里立刻红。
    强度只升不降的三条依据：① 旧用例对 LIVE_RECORD **只有**源码断言、没有行为断言，现在两边都有；
    ② 源码断言只能证明「那行字还在」，证明不了方向（把 `!= "off"` 改成 `== "off"` 它照样绿），
    现在逐行方向都钉住了；③ 三条向量互不相同是显式断言，任何一张表漂到另一张表上立刻红，
    而 strip/lower 与「false/no 归哪一侧」这些差异点都在批内取过值。"""
    value_batch = ("0", "off", "OFF", " off ", "false", "no", "ofs", "", "0ff")

    class _BoomPage:
        def goto(self, *a, **kw):
            raise RuntimeError("不该被调用（本用例只探开关认哪些值）")

    def record_direction(raw):
        """LIVE_RECORD 走 `_init_live_session` 的出口（`run.record` 就是 `_open_session_dirs`
        收到的第三个实参）——这是它在生产里的唯一读点，钉它才算钉住调用点。"""
        _init_stubs(monkeypatch, tmp_path)
        env = {} if raw is None else {"LIVE_RECORD": raw}
        return mod._init_live_session(env).record

    def preflight_runs_check(raw):
        monkeypatch.setenv("LIVE_PREFLIGHT", raw)
        capsys.readouterr()
        mod._preflight_click_check(_BoomPage(), None)  # 自检体被替身炸掉=照跑（关才打印那行）
        return "起飞前自检已关闭" not in capsys.readouterr().out

    def dump_direction(raw):
        monkeypatch.setenv(mod.WEDGE_DUMP_ENV, raw)
        return mod._wedge_dump_enabled()

    def skip_direction(raw):
        monkeypatch.setenv(mod.WEDGE_LEDGER_ENV, raw)
        return mod._wedge_skip_enabled()

    record_vec = [record_direction(raw) for raw in value_batch]
    preflight_vec = [preflight_runs_check(raw) for raw in value_batch]
    dump_vec = [dump_direction(raw) for raw in value_batch]
    skip_vec = [skip_direction(raw) for raw in value_batch]

    # 逐行方向（True=开）。三张表的分界点全在批内：精确串族只有 "off"；四值族连 "OFF"/" off "
    # 都关；两值族关得掉大小写与空白，但 "false"/"no" 关不掉。
    assert record_vec == [True, False, True, True, True, True, True, True, True], dict(zip(value_batch, record_vec))
    assert preflight_vec == [False, False, False, False, False, False, True, True, True], dict(
        zip(value_batch, preflight_vec)
    )
    assert dump_vec == [False, False, False, False, True, True, True, True, True], dict(zip(value_batch, dump_vec))
    assert skip_vec == dump_vec, "A4 与 B1 共用同一张两值表（刻意如此，见 `_wedge_skip_enabled` docstring）"

    # 三条方向向量两两不同 = 「三套表」这件事本身。任何一张漂向另一张，这里立刻红。
    assert record_vec != preflight_vec, "LIVE_RECORD 不是四值表：false/no/0/OFF 关不掉录屏"
    assert record_vec != dump_vec, "LIVE_RECORD 不是两值表：0/OFF/blank 关不掉录屏"
    assert preflight_vec != dump_vec, "四值表与两值表差在 false/no（别照抄）"

    # 未设置＝默认开：三个开关同侧（这一侧旧用例只有 dump/skip 两行，record 是新增覆盖）。
    assert record_direction(None) is True
    monkeypatch.delenv("LIVE_PREFLIGHT", raising=False)
    capsys.readouterr()
    mod._preflight_click_check(_BoomPage(), None)
    assert "起飞前自检已关闭" not in capsys.readouterr().out, "LIVE_PREFLIGHT 未设置＝默认开"
    monkeypatch.delenv(mod.WEDGE_DUMP_ENV, raising=False)
    monkeypatch.delenv(mod.WEDGE_LEDGER_ENV, raising=False)
    assert mod._wedge_dump_enabled() is True and mod._wedge_skip_enabled() is True, "未设置＝默认开（三个开关同侧）"


def test_wedge_dump_off_makes_zero_faulthandler_calls(tmp_path, monkeypatch):
    """开关关闭时**一个 faulthandler 调用都不发**（不是「调了但没效果」）——连 cancel 也算
    触碰全进程单例，关掉后与真实会话的差别必须能在调用序列上切成两半。"""
    fake = _FakeFaulthandler()
    monkeypatch.setattr(fx, "faulthandler", fake)
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    assert mod._touch_heartbeat("job:1") is True  # conftest 已把开关置为 off
    mod._mark_session_done(SimpleNamespace(session_dir=tmp_path))
    assert fake.calls == []
    assert not (tmp_path / mod.WEDGE_STACK).exists()


def test_wedge_dump_arms_after_cancel_on_every_beat(tmp_path, monkeypatch):
    """A4 的机制本体：节拍点=武装点，每一拍都「先 cancel 旧定时器、再武装新的」。
    不先 cancel 的话，旧定时器的那 90s 会在当前拍之后不足 90s 的某一刻到期，
    给一场健康会话落一份假栈。"""
    monkeypatch.setenv(mod.WEDGE_DUMP_ENV, "1")
    fake = _FakeFaulthandler()
    monkeypatch.setattr(fx, "faulthandler", fake)
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    mod._touch_heartbeat("job:1")
    mod._touch_heartbeat("detail:query-card:pre")
    assert [c[0] for c in fake.calls] == ["cancel", "arm", "cancel", "arm"]
    assert {c[1] for c in fake.calls if c[0] == "arm"} == {mod.WEDGE_STACK_SECONDS}
    assert len({c[2] for c in fake.calls if c[0] == "arm"}) == 1, "同一会话目录内复用同一个 fd"


def test_wedge_dump_handle_follows_session_dir_rebind(tmp_path, monkeypatch):
    """会话目录会被重绑（preflight rebind / 同进程跑下一场）——句柄不跟着换目录的话，
    栈就落进上一场的目录里（`_arm_session_watchdog` 的 rebind 注释记过同款的跨会话污染）。"""
    monkeypatch.setenv(mod.WEDGE_DUMP_ENV, "1")
    fake = _FakeFaulthandler()
    monkeypatch.setattr(fx, "faulthandler", fake)
    first = tmp_path / "session-a"
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", first)
    mod._touch_heartbeat("job:1")
    second = tmp_path / "session-b"
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", second)
    mod._touch_heartbeat("job:1")
    assert [c[2] for c in fake.calls if c[0] == "arm"] == [
        str(first / mod.WEDGE_STACK),
        str(second / mod.WEDGE_STACK),
    ]


def test_wedge_dump_without_session_dir_cancels_but_never_arms(monkeypatch):
    """没有会话目录 = 栈无处落盘 → 不武装；但旧定时器照样先取消（不留悬着的定时器）。"""
    monkeypatch.setenv(mod.WEDGE_DUMP_ENV, "1")
    fake = _FakeFaulthandler()
    monkeypatch.setattr(fx, "faulthandler", fake)
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", None)
    mod._arm_wedge_dump()
    assert [c[0] for c in fake.calls] == ["cancel"]


def test_wedge_dump_arm_failure_degrades_to_no_arming(tmp_path, monkeypatch, capsys):
    """取证层绝不新增失败面（#38/#54 口径）：武装失败只打一行 warn 并本会话不再重试，
    心跳照写（看门狗照常工作），异常绝不冒出去打断投递。"""
    monkeypatch.setenv(mod.WEDGE_DUMP_ENV, "1")
    fake = _FakeFaulthandler(raises=True)
    monkeypatch.setattr(fx, "faulthandler", fake)
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    assert mod._touch_heartbeat("job:1") is True, "心跳不得因武装失败而失败"
    assert "楔死取栈武装失败" in capsys.readouterr().out, "降级必须可见（不开静默失败面）"
    seen = len(fake.calls)
    assert mod._touch_heartbeat("job:2") is True
    assert len(fake.calls) == seen, "武装失败后不得逐拍重试（不刷屏）"


def test_wedge_dump_cancel_failure_still_releases_handle(tmp_path, monkeypatch, capsys):
    """取消本身失败（定时器还挂着）：打一行 warn、句柄照收、异常绝不冒进收尾流程——
    `_mark_session_done` 之后就是「等用户 CLOSE」，取证层没权利在那里把会话搞崩。
    残余风险如实登记：这一份假栈可能会落，由 warn 行交代。"""
    monkeypatch.setenv(mod.WEDGE_DUMP_ENV, "1")
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    ok = _FakeFaulthandler()
    monkeypatch.setattr(fx, "faulthandler", ok)
    assert mod._touch_heartbeat("job:1") is True
    assert fx._WEDGE_FH is not None, "武装成功后句柄必须留在模块上（fd 不能交给 GC）"

    monkeypatch.setattr(fx, "faulthandler", _FakeFaulthandler(cancel_raises=True))
    mod._cancel_wedge_dump()  # 不抛出即通过
    out = capsys.readouterr().out
    assert "楔死取栈取消失败" in out, "取消失败也要可见（假栈会落进这一场，读现场的人得知道）"
    assert fx._WEDGE_FH is None, "取消失败也要释放句柄（收尾不留悬着的 fd）"


def test_mark_session_done_cancels_after_the_final_beat(tmp_path, monkeypatch):
    """收尾的取消顺序是本用例的全部：done 那一拍先写（武装）→ 紧随其后取消。
    早一步取消，用户 CLOSE 长等待（合法无心跳）就会落一份「正常收尾」的假栈，
    把 C 段的栈形判据污染成噪音。"""
    monkeypatch.setenv(mod.WEDGE_DUMP_ENV, "1")
    fake = _FakeFaulthandler()
    monkeypatch.setattr(fx, "faulthandler", fake)
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    mod._mark_session_done(SimpleNamespace(session_dir=tmp_path))
    assert [c[0] for c in fake.calls] == ["cancel", "arm", "cancel"]


def test_wedge_dump_cancel_stops_the_dump(tmp_path, monkeypatch):
    """取消之后主线程照样长睡也不落栈（= done 之后的 CLOSE 等待不落假栈的机制证明）。
    文件本身在武装时就建出来了（faulthandler 需要一个有效的 fd，只能先开），故断言为空。
    阈值取 0.5s 而非 0.3s：武装与取消之间只隔着一次函数调用，余量是给 CI 并行调度留的。"""
    monkeypatch.setenv(mod.WEDGE_DUMP_ENV, "1")
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    monkeypatch.setattr(fx, "WEDGE_STACK_SECONDS", 0.5)
    assert mod._touch_heartbeat("job:1") is True
    mod._cancel_wedge_dump()
    time.sleep(0.9)
    stack = tmp_path / mod.WEDGE_STACK
    assert not stack.exists() or stack.read_text(encoding="utf-8") == "", "取消后不得落栈"


def test_wedge_dump_lands_full_stack_when_main_thread_stalls(tmp_path, monkeypatch):
    """#56 验收 2：人为制造「节拍之后主线程再不回来」→ wedge-stack.txt 自动出现，
    栈里认得出被楔住的那个调用（真 faulthandler，秒数 monkeypatch 到 0.3，不真等 90s）。"""
    monkeypatch.setenv(mod.WEDGE_DUMP_ENV, "1")
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    monkeypatch.setattr(fx, "WEDGE_STACK_SECONDS", 0.3)
    assert mod._touch_heartbeat("job:1") is True
    _stall_like_a_wedge(0.7)
    stack = (tmp_path / mod.WEDGE_STACK).read_text(encoding="utf-8")
    assert "_stall_like_a_wedge" in stack, "栈顶必须是被楔住的那个调用帧"
    assert "test_wedge_dump_lands_full_stack_when_main_thread_stalls" in stack


def test_wedge_dump_two_beats_land_exactly_one_dump(tmp_path, monkeypatch):
    """连拍两次不重复落：每拍先 cancel 再 arm，于是只有**最后一拍**的定时器会到期，
    且默认 repeat=False 只落一次——健康会话里每一拍都在取消前一拍，文件永不出现。"""
    monkeypatch.setenv(mod.WEDGE_DUMP_ENV, "1")
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    monkeypatch.setattr(fx, "WEDGE_STACK_SECONDS", 0.5)
    mod._touch_heartbeat("job:1")
    time.sleep(0.1)  # 远在 0.5s 之内：第一拍那台定时器应当被第二拍取消掉
    mod._touch_heartbeat("detail:query-card:pre")
    time.sleep(0.9)
    assert (tmp_path / mod.WEDGE_STACK).read_text(encoding="utf-8").count("Timeout") == 1


# --- #56 A3：细颗粒节拍（把心跳铺到每个无超时协议调用前后）---
#
# 5 场楔死的现场只剩一条 `job:{idx}` 心跳：它说得出「死在这个岗上」，说不出「停在哪个调用上」。
# A3 把拍点铺进 enter_detail / click_with_retry / grab_detail 里那些 timeout=None 的裸奔调用
# （票面机制口径 §1），label 形态固定为 `段:调用[:轮次]:pre|post`——只有 pre 没 post =
# 进了这个调用没出来；pre+post = 出来了、死在下一步。C 段据此判栈形，不必先有栈。


def test_enter_detail_beats_label_the_bare_calls_in_order(monkeypatch):
    """#56 验收 1：enter_detail 段节拍可见。`page.query_selector`（card 分支第一个裸奔调用）
    与卡片点击前后各一拍，并把同一个 beat_fn 透传给 平台侧，于是下层的 bounding_box /
    mouse.move / mouse.click 也各有其名。整段顺序就是现场可读的顺序。"""
    beats = []
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)

    def fake_click(page, btn, action="", beat_fn=None):
        beat_fn("click:bounding-box:pre")  # 下层（platforms/chat_first.py）的拍由透传产生
        beat_fn("click:bounding-box:post")
        return True

    monkeypatch.setattr(mod, "click_with_retry", fake_click)
    page = _NavPage(card=_Card())
    mode = mod.enter_detail(page, "abc123", lambda url, **kw: page.goto(url, **kw), beat_fn=beats.append)
    assert mode == "card"
    assert beats == [
        "detail:query-card:pre",
        "detail:query-card:post",
        "detail:card-click:pre",
        "click:bounding-box:pre",
        "click:bounding-box:post",
        "detail:card-click:post",
    ], "label 命名是本用例的锁定对象：稳定、可 grep、pre/post 显式写在里面"


def test_enter_detail_beats_the_direct_goto_branches(monkeypatch):
    """直跳（卡片缺失）分支同样有拍：goto 虽有 30s 超时，但它是这段里唯一的长停顿窗口，
    标出来才能把「死在 goto」与「死在 goto 之后的随机停留」分开。"""
    beats = []
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    page = _NavPage(card=None)
    mode = mod.enter_detail(page, "abc123", lambda url, **kw: page.goto(url, **kw), beat_fn=beats.append)
    assert mode == "direct_no_card"
    assert beats == [
        "detail:query-card:pre",
        "detail:query-card:post",
        "detail:direct-goto:pre",
        "detail:direct-goto:post",
    ]


def test_enter_detail_click_exception_leaves_no_post_beat(monkeypatch):
    """异常出口不伪装成「这个调用出来了」：click_with_retry 抛异常时不拍 card-click:post，
    但仍要拍到回退直跳——最后一条 label 与实际控制流严格一致（回退成功也算走完这一岗）。"""
    beats = []
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)

    def _boom(*a, **k):
        raise RuntimeError("Timeout 8000ms exceeded.")

    monkeypatch.setattr(mod, "click_with_retry", _boom)
    page = _NavPage(card=_Card())
    mode = mod.enter_detail(page, "abc123", lambda url, **kw: page.goto(url, **kw), beat_fn=beats.append)
    assert mode == "direct_click_failed"
    assert beats == [
        "detail:query-card:pre",
        "detail:query-card:post",
        "detail:card-click:pre",
        "detail:direct-goto:pre",
        "detail:direct-goto:post",
    ]
    assert "detail:card-click:post" not in beats


def test_enter_detail_without_beat_fn_is_unchanged(monkeypatch):
    """beat_fn 默认 None = 零行为变更（A3 的另一半保证）：老签名调用照旧三态返回。"""
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    page = _NavPage(card=_Card())
    assert mod.enter_detail(page, "abc123", lambda url, **kw: page.goto(url, **kw)) == "card"


def _a3_ctx(tmp_path):
    """节拍接线用例的会话上下文：只用到 session_dir / shots_dir / state / results / platform。"""
    return mod.LiveSessionContext(
        session_dir=tmp_path,
        shots_dir=tmp_path / "shots",
        cfg={"paste_prob": 1.0},
        pacer=SimpleNamespace(wait_skip=lambda: None, wait_sent=lambda: None),
        platform=SimpleNamespace(login_dialog_selector="#login-dialog", save_evidence=lambda p, tag, job=None: None),
        results=[],
        state={"applied": 0, "direct_fallback": 0, "keyword_skips": []},
    )


def _a3_flow(tmp_path, idx=1):
    job = {"jobName": "测试岗", "brandName": "测试公司", "encryptJobId": "e1"}
    return mod._JobFlow(page=_ListenerPage(_DETAIL_URL), job=job, idx=idx, ctx=_a3_ctx(tmp_path))


def test_heartbeat_beat_is_the_defensive_injector(monkeypatch):
    """注入给平台层的拍点必须是「吞掉一切异常的薄封装」：`human_pointer_walk` 整段在
    try/except 里、`coordinate_click` 的异常等价于「不点」——拍点若抛出去，指针轨迹会静默
    消失、坐标点击退回 handle 点击，取证就成了行为变更（A 段口径：除 A1 外零行为变更）。"""
    seen = []
    # `_heartbeat_beat` 调 `_touch_heartbeat` 是**模块内**调用（读 `_forensics` 的全局名），
    # 所以桩必须打在 `fx` 上：打 `mod._touch_heartbeat` 只改到别名，调用点看不见=静默 no-op。
    monkeypatch.setattr(fx, "_touch_heartbeat", lambda label: seen.append(label) or True)
    mod._heartbeat_beat("jd:dom-poll:1:pre")
    assert seen == ["jd:dom-poll:1:pre"], "label 原样送进心跳（细颗粒节拍即心跳本体，不是另一套通道）"

    def boom(label):
        raise RuntimeError("心跳通道坏了")

    monkeypatch.setattr(fx, "_touch_heartbeat", boom)
    mod._heartbeat_beat("click:mouse-click:pre")  # 不抛出即通过：观测层绝不新增失败面


def test_enter_and_inspect_job_beats_land_in_heartbeat_with_eid(tmp_path, monkeypatch):
    """接线（阶段 1）+ A2×A3 的交点：`_enter_and_inspect_job` 把心跳薄封装注入 enter_detail，
    于是楔死时那条细颗粒 label 同时带着本岗 eid——「停在哪个调用 + 停在哪个岗」一次答完。"""
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    monkeypatch.setattr(mod, "capture_xhr", lambda page, frag, container: lambda resp: None)
    monkeypatch.setattr(mod, "shot", lambda *a, **k: None)
    seen = {}

    def fake_enter(page, eid, goto_fn, beat_fn=None):
        seen["beat_fn"] = beat_fn
        if beat_fn:
            beat_fn("detail:query-card:pre")
        return "card"

    monkeypatch.setattr(mod, "enter_detail", fake_enter)
    flow = _a3_flow(tmp_path)
    mod._heartbeat_job_start(flow)  # 真实链路上由 apply_live 岗头设置（A2）
    mod._enter_and_inspect_job(flow)
    assert seen["beat_fn"] is not None, "阶段 1 必须把拍点注进 enter_detail"
    data = _heartbeat_payload(tmp_path)
    assert data["label"] == "detail:query-card:pre"
    assert data["eid"] == "e1" and data["in_job"] is True, "楔死区间内的拍点也要带岗位身份"


def test_grab_detail_stage_beats_land_in_heartbeat(tmp_path, monkeypatch):
    """接线（阶段 3）：grab_detail 的 DOM 轮询 / XHR 空等是票面点名的同族无超时站点，
    那一段楔进去时最后一条心跳落在 `jd:*` 上，而不是停在岗头。"""
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    monkeypatch.setattr(mod, "request_greeting", lambda *a, **k: None)
    monkeypatch.setattr(mod, "shot", lambda *a, **k: None)
    monkeypatch.setattr(mod, "wait_reply", lambda *a, **k: "您好，我有 7 年 Solidity 经验。")
    seen = {}

    def fake_grab(page, detail, beat_fn=None):
        seen["beat_fn"] = beat_fn
        if beat_fn:
            beat_fn("jd:dom-poll:2:pre")
        return "JD 全文：负责测试"

    monkeypatch.setattr(mod, "grab_detail", fake_grab)
    flow = _a3_flow(tmp_path)
    mod._heartbeat_job_start(flow)
    assert mod._acquire_and_draft_greeting(flow) is None
    assert seen["beat_fn"] is not None, "阶段 3 必须把拍点注进 grab_detail"
    assert _heartbeat_payload(tmp_path)["label"] == "jd:dom-poll:2:pre"


def test_open_chat_stage_beats_land_in_heartbeat(tmp_path, monkeypatch):
    """接线（阶段 5a）：「立即沟通」那次点击用的是同一个 click_with_retry——
    楔死位点里紧邻的下一个站点，同理必须接上心跳。"""
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(mod, "shot", lambda *a, **k: None)
    monkeypatch.setattr(mod, "find_comm_button_with_recovery", lambda page, eid, name, **kw: "BTN")
    monkeypatch.setattr(mod, "detect_risk_signal", lambda page: None)
    monkeypatch.setattr(mod, "find_input_with_retry", lambda page, **kw: "INPUT")

    def fake_click(page, btn, action="", beat_fn=None):
        assert action == "立即沟通按钮"
        if beat_fn:
            beat_fn("click:mouse-click:pre")
        return True

    monkeypatch.setattr(mod, "click_with_retry", fake_click)
    flow = _a3_flow(tmp_path)
    mod._heartbeat_job_start(flow)
    assert mod._open_chat_and_find_input(flow) is None
    assert flow.input_el == "INPUT"
    assert _heartbeat_payload(tmp_path)["label"] == "click:mouse-click:pre"


def test_preflight_click_check_beats_into_heartbeat(tmp_path, monkeypatch):
    """起飞前自检那场楔死（015515）走的是同一个 click_with_retry：接上心跳后，
    preflight 段的停摆也答得出停在哪个调用上（票面 A3「同族无超时站点一并覆盖」）。"""
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    seen = {}

    def fake_click(page, btn, action="", beat_fn=None):
        seen["beat_fn"] = beat_fn
        if beat_fn:
            beat_fn("click:bounding-box:pre")
        return True

    monkeypatch.setattr(mod, "click_with_retry", fake_click)

    # page 替身用本文件共享的那一族（`query_selector_all` + `is_visible`）：自检现在挑的是
    # **第一张可见**卡片，旧 `query_selector` 单命中替身已经喂不进新的选取逻辑。
    assert mod._preflight_click_check(_PreflightPage([_PreflightCard()]), SimpleNamespace(results=[])) is True
    assert seen["beat_fn"] is not None, "自检点击同样要接上拍点"
    assert _heartbeat_payload(tmp_path)["label"] == "click:bounding-box:pre"


def test_browse_list_noise_sites_beat_into_heartbeat(tmp_path, monkeypatch):
    """#56 修复轮 F4②（A3 漏接的第一处）：`_browse_list` 的噪声点是**真的 coordinate_click
    进详情页**（票面 B1/A3 都关心这段——5/5 场楔死就落在「岗头→详情」那一段），
    过去整段一个拍点都没有：楔死在这里，最后一条心跳还停在上一岗。
    接线只加旁路：默认语义、排除逻辑、导航复位一字不动。"""
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    monkeypatch.setattr(mod, "human_scroll", lambda *a, **k: None)
    monkeypatch.setattr(mod, "shot", lambda *a, **k: None)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(mod.random, "random", lambda: 0.0)  # 必触发噪声（<0.25）
    seen = {}

    def silent_click(page, el, beat_fn=None):
        seen["beat_fn"] = beat_fn
        return True

    monkeypatch.setattr(mod, "coordinate_click", silent_click)
    page = _NoisePage([_NoiseCard("/job_detail/e9.html")])
    mod._browse_list(page, SimpleNamespace(shots_dir="."), "q", exclude_ids={"e1"})
    assert seen["beat_fn"] is mod._heartbeat_beat, "噪声点击必须把拍点注进 coordinate_click"
    assert page.gotos == [page.url], "既有语义（点开仍要 goto 复位回列表）不变"
    assert _heartbeat_payload(tmp_path)["label"] == "browse:noisy-cards:post", (
        "找卡片那一次裸奔 query_selector_all 也接了拍（不点也读得出走到哪一步）"
    )

    def beating_click(page, el, beat_fn=None):
        beat_fn("click:mouse-click:pre")
        return True

    monkeypatch.setattr(mod, "coordinate_click", beating_click)
    mod._browse_list(_NoisePage([_NoiseCard("/job_detail/e9.html")]), SimpleNamespace(shots_dir="."), "q")
    assert _heartbeat_payload(tmp_path)["label"] == "click:mouse-click:pre", (
        "楔死在坐标点击里时，最后一条心跳要指到那一个无超时调用"
    )


def test_browse_list_beat_marks_swallowed_hover_exception(tmp_path, monkeypatch):
    """修复轮 4（A3）：hover 段的 `except: pass` 出口补 `browse:hover-cards:error`——
    补点前异常被吞时最后一条心跳只到 `:post`（或卡住前的 pre），与「这段没跑了」同形，二义。"""
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    monkeypatch.setattr(mod, "human_scroll", lambda *a, **k: None)
    monkeypatch.setattr(mod, "shot", lambda *a, **k: None)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(mod.random, "randint", lambda a, b: 2)  # 恰好两轮：i=1 走 hover 决策
    rolls = iter([0.0, 1.0])  # 第一掷进 hover；第二掷不进噪声点击
    monkeypatch.setattr(mod.random, "random", lambda: next(rolls))

    class _BoomCard(_NoiseCard):
        def bounding_box(self):
            raise RuntimeError("boundingBox 挂了")

    page = _NoisePage([_BoomCard("/job_detail/e9.html")])
    mod._browse_list(page, SimpleNamespace(shots_dir="."), "q")
    assert _heartbeat_payload(tmp_path)["label"] == "browse:hover-cards:error"


def test_browse_list_beat_marks_swallowed_noise_exception(tmp_path, monkeypatch):
    """修复轮 4（A4）：噪声点击段的 `except: pass` 出口补 `browse:noisy-cards:error`。
    这段是**真的进详情页**的路径（5/5 场楔死区间），出口形态过去与「走到 post 后卡住」同形。"""
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    monkeypatch.setattr(mod, "human_scroll", lambda *a, **k: None)
    monkeypatch.setattr(mod, "shot", lambda *a, **k: None)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(mod.random, "randint", lambda a, b: 2)
    rolls = iter([1.0, 0.0])  # 第一掷不进 hover；第二掷进噪声点击
    monkeypatch.setattr(mod.random, "random", lambda: next(rolls))

    def _boom_click(page, el, **kw):
        raise RuntimeError("坐标点击挂了")

    monkeypatch.setattr(mod, "coordinate_click", _boom_click)
    page = _NoisePage([_NoiseCard("/job_detail/e9.html")])
    mod._browse_list(page, SimpleNamespace(shots_dir="."), "q")
    assert _heartbeat_payload(tmp_path)["label"] == "browse:noisy-cards:error"


def test_find_input_beats_its_query_selector_family(tmp_path, monkeypatch):
    """#56 修复轮 F4②（第二处）：`find_input` / `find_send_button` / `find_comm_button` 一族的
    `query_selector(_all)` 同属票面 §1 点名的 timeout=None 裸奔调用，A3 段漏接。
    这里锁编排层那两只：接上 beat_fn 后「死在找输入框第几个选择器」读得出来，
    且**不新增重试、不改查找顺序**（`find_input_with_retry` 的重试次数仍由常量约束）。"""
    asked, labels = [], []

    class _QPage:
        def query_selector(self, sel):
            asked.append(sel)
            return None

    assert mod.find_input(_QPage(), beat_fn=labels.append) is None
    assert asked == [
        "textarea[placeholder*='简短']",
        "textarea[placeholder*='招呼']",
        "textarea[placeholder*='描述']",
        ".chat-input textarea",
        ".chat-input [contenteditable='true']",
        "[contenteditable='true']",
        "textarea",
    ], "查找顺序一字未动"
    assert mod.find_send_button(_QPage(), beat_fn=labels.append) is None
    assert len(asked) == 11, "7 个输入框选择器 + 4 个发送按钮选择器，各查一次（不新增重试）"
    pres = [lbl for lbl in labels if lbl.endswith(":pre")]
    posts = [lbl for lbl in labels if lbl.endswith(":post")]
    assert len(pres) == len(posts) == 11, "每个选择器一次前后拍"
    assert pres[0] == "find:input:query:1:pre" and pres[-4] == "find:send:query:1:pre"
    labels.clear()
    assert mod.find_input(_QPage()) is None and mod.find_send_button(_QPage()) is None
    assert labels == [], "默认 None = 零行为变更、一个拍都不发"


def test_find_input_and_send_button_beat_their_swallow_exits():
    """修复轮 4（A1/A2）：两只查找函数的 `except: continue/pass` 出口补 `*:error`——
    异常被吞时过去只留一条 pre，与「卡在该调用里」同形，C 段判栈分不开两种形态。
    补点后：每个选择器 pre + error，两拍一套；返回值与控制流一字未动。"""

    class _BoomQ:
        def query_selector(self, sel):
            raise RuntimeError("protocol 链路挂了")

    labels = []
    assert mod.find_input(_BoomQ(), beat_fn=labels.append) is None, "异常照旧吞掉：返回值不变"
    assert labels == [
        lbl for n in range(1, 8) for lbl in (f"find:input:query:{n}:pre", f"find:input:query:{n}:error")
    ], "7 个选择器各自 pre+error（抛出后该调用没有 post）"
    labels.clear()
    assert mod.find_send_button(_BoomQ(), beat_fn=labels.append) is None
    assert labels == [lbl for n in range(1, 5) for lbl in (f"find:send:query:{n}:pre", f"find:send:query:{n}:error")], (
        "4 个发送按钮选择器同理"
    )


def test_find_input_with_retry_passes_the_beat_down(tmp_path, monkeypatch):
    """`find_input_with_retry` 是带重试的那一层：拍点要透到每一次查找里（它自己不改重试次数），
    「死在第几次重试的第几个选择器」才读得出来。"""
    seen = []

    def fake_find(page, beat_fn=None):
        seen.append(beat_fn)
        return None

    monkeypatch.setattr(mod, "find_input", fake_find)
    monkeypatch.setattr(mod, "FIND_INPUT_ATTEMPTS", 2)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    assert mod.find_input_with_retry(object(), beat_fn="BEAT") is None
    assert seen == ["BEAT", "BEAT"], "每一次重试都带上同一个 beat_fn"
    assert mod.find_input_with_retry(object()) is None and set(seen) == {"BEAT", None}


def test_open_chat_stage_wires_beats_into_the_finders(tmp_path, monkeypatch):
    """接线（真实阶段函数）：`_open_chat_and_find_input` 经由
    `find_comm_button_with_recovery` 与 `find_input_with_retry`——这两只不接心跳就等于漏接
    （`query_selector` 全族都是票面 §1 点名的无超时调用）。"""
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(mod, "shot", lambda *a, **k: None)
    monkeypatch.setattr(mod, "detect_risk_signal", lambda page: None)
    seen = {}

    def fake_recovery(page, eid, job_name=None, beat_fn=None):
        seen["recovery"] = beat_fn
        if beat_fn:
            beat_fn("find:comm-btn:query:1:pre")
        return "BTN"

    def fake_input(page, beat_fn=None):
        seen["input"] = beat_fn
        return "INPUT"

    monkeypatch.setattr(mod, "find_comm_button_with_recovery", fake_recovery)
    monkeypatch.setattr(mod, "find_input_with_retry", fake_input)
    monkeypatch.setattr(mod, "click_with_retry", lambda page, btn, action="", beat_fn=None: True)
    flow = _a3_flow(tmp_path)
    mod._heartbeat_job_start(flow)
    assert mod._open_chat_and_find_input(flow) is None
    assert seen["recovery"] is mod._heartbeat_beat, "沟通按钮查找必须接上拍点"
    assert seen["input"] is mod._heartbeat_beat, "输入框查找（含每一次重试）必须接上拍点"
    assert _heartbeat_payload(tmp_path)["label"] == "find:comm-btn:query:1:pre"


def test_find_comm_button_with_recovery_passes_the_beat_down(monkeypatch):
    """`find_comm_button_with_recovery` 自己不懂心跳，把同一个 beat_fn 透给 平台层的
    `find_comm_button`（两次查找都要透，否则兜底那次仍然无名）。"""
    seen = []

    monkeypatch.setattr(mod, "job_panel_title", lambda page: "本岗")
    monkeypatch.setattr(mod, "on_job_detail", lambda page, eid, name=None: True)
    monkeypatch.setattr(mod, "open_standalone_detail", lambda page, eid: None)

    def fake_find(page, beat_fn=None):
        seen.append(beat_fn)
        return None

    monkeypatch.setattr(mod, "find_comm_button", fake_find)
    assert mod.find_comm_button_with_recovery(object(), "e1", "本岗", beat_fn="BEAT") is None
    assert seen == ["BEAT", "BEAT"], "首查与兜底重查都带上同一个 beat_fn"
    # 不注入时传下去的是本模块的 no-op 替身（`_noop_beat`）——语义与 None 完全相同，一个拍都不发
    assert mod.find_comm_button_with_recovery(object(), "e1", "本岗") is None
    assert seen[-1] is mod._noop_beat


def test_type_and_send_stage_passes_beat_to_send_button_click(tmp_path, monkeypatch):
    """接线（阶段 5b）：发送按钮那次点击也是 click_with_retry——发送段楔死时
    最后一条心跳得能说「在点发送」，而不是停在 typed 截图那一拍。"""
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(mod, "shot", lambda *a, **k: None)
    seen = {}

    def fake_send_finder(page, beat_fn=None):
        seen["finder"] = beat_fn
        return SimpleNamespace()

    monkeypatch.setattr(mod, "find_send_button", fake_send_finder)
    monkeypatch.setattr(mod, "_type_greeting", lambda *a, **k: None)

    def fake_click(page, btn, action="", beat_fn=None):
        seen["action"], seen["beat_fn"] = action, beat_fn
        if beat_fn:
            beat_fn("click:handle:pre")
        return True

    monkeypatch.setattr(mod, "click_with_retry", fake_click)
    flow = _a3_flow(tmp_path)
    flow.greeting = "您好"
    flow.input_el = _FakeInputEl()
    mod._heartbeat_job_start(flow)
    assert mod._type_and_send_greeting(flow) is None
    assert seen["action"] == "发送招呼按钮" and seen["beat_fn"] is not None
    assert seen["finder"] is mod._heartbeat_beat, "修复轮 F4②：找发送按钮那族 query_selector 也接上拍点"
    assert _heartbeat_payload(tmp_path)["label"] == "click:handle:pre"


def test_real_enter_detail_beats_reach_heartbeat_end_to_end(tmp_path, monkeypatch):
    """验收 1 的强证据：不替换 enter_detail 与平台侧点击实现，只替 page——
    真实代码路径上「阶段 1 → 卡片点击 → 指针轨迹 → 坐标点击」的拍点逐条落进 heartbeat.json，
    且带着本岗 eid（A2×A3 的交点）。替身自证只能证明「我调了 beat_fn」，这条能证明接线真的通。"""
    labels = []
    real_write = fx.write_json_atomic

    def spy(path, data):
        real_write(path, data)
        if str(path).endswith(mod.SESSION_HEARTBEAT):
            labels.append(data)

    class _RealCard:
        def bounding_box(self):
            return {"x": 300, "y": 200, "width": 100, "height": 40}

        def click(self, **kw):
            return None

    class _RealNavPage(_SendPage):
        """card 分支的整条真实通道：query_selector 给卡片、evaluate 给视口尺寸、mouse 记录落点。"""

        def __init__(self, url):
            super().__init__(url)
            self.mouse = SimpleNamespace(move=lambda *a: None, click=lambda *a: None)

        def query_selector(self, sel):
            return _RealCard() if "e1" in sel else None

        def evaluate(self, js, *args):
            return {"w": 1280, "h": 800}

    page = _RealNavPage(_DETAIL_URL)
    monkeypatch.setattr(fx, "write_json_atomic", spy)
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)  # 轨迹/进场的随机停留不真等
    monkeypatch.setattr(mod, "capture_xhr", lambda page, frag, container: lambda resp: None)
    monkeypatch.setattr(mod, "shot", lambda *a, **k: None)

    job = {"jobName": "测试岗", "brandName": "测试公司", "encryptJobId": "e1"}
    flow = mod._JobFlow(page=page, job=job, idx=1, ctx=_a3_ctx(tmp_path))
    mod._heartbeat_job_start(flow)  # 真实链路由 apply_live 岗头设置（A2）
    mod._enter_and_inspect_job(flow)

    assert [d["label"] for d in labels[:3]] == [
        "detail:query-card:pre",
        "detail:query-card:post",
        "detail:card-click:pre",
    ]
    assert [d["label"] for d in labels[-4:]] == [
        "click:evaluate-viewport:post",
        "click:mouse-click:pre",
        "click:mouse-click:post",
        "detail:card-click:post",
    ]
    assert "pointer:mouse-move:aim:post" in [d["label"] for d in labels], "下层轨迹段的拍也在同一条心跳里"
    assert all(d["eid"] == "e1" and d["in_job"] is True for d in labels), (
        "楔死区间内每一拍都带岗位身份——B1 跳岗台账与 C 段栈形判据都靠这一条"
    )


# --- #56 A5：静默失败变可观测 + no_jd 按证据三类归因 ---
#
# 失败基线（改前实测，`.venv/bin/python` 直接调现版函数）：
#   capture_xhr：把「`.json()` 抛异常」的假响应喂进去 → 容器原样 `{'data': None}`、
#                app.log 根本没被创建（`except Exception: pass` 吞干净）；
#   capture_xhr_accum：`buf=[]`、`flags={}` 同样零痕迹；
#   grab_detail：返回 '' / 真实墙钟 20.05s（5s DOM 轮询 + 15s XHR 空等）/ 容器原样
#                `{'data': None}`——等了多久、DOM 命中了几次，一个字都没留；
#   apply_retry_outcome(entry, "no_jd") → (False, "no_jd")，台账口径写
#                「详情不可达（岗位疑已失效）」（未经证据的定性，且把可补投岗静默移除）；
#   build_report(...) → 没有 no_jd 分类读数；模块里没有 classify_no_jd。
# 下面每条用例各自钉住上述一处：断言的都是「今天什么都没落、改后必须落」的行为。

_DETAIL_FRAG = "/wapi/zpgeek/job/detail.json"
_LIST_FRAG = "/wapi/zpgeek/joblist/search/job/list.json"


class _BoomResp:
    """`resp.json()` 抛异常的响应替身——票面点名的那次无超时协议往返的**失败**形态
    （`Response.body()` → `send("body", None)`；不返回的那一半是楔死，属 A1/C 段）。"""

    def __init__(self, url="https://www.zhipin.com/wapi/zpgeek/job/detail.json?eid=e1"):
        self.url = url

    def json(self):
        raise RuntimeError("Timeout 30000ms exceeded while reading response body")


class _NoUrlResp:
    """连 `resp.url` 都取不到的形态（协议链路已断）：取证自身不得再抛。
    故意**不实现 `json()`**——handler 在第一行 `url_frag in resp.url` 就炸，永远走不到 json；
    真走到了说明控制流变了，那时 AttributeError 会让用例直接红。"""

    @property
    def url(self):
        raise RuntimeError("Connection closed while reading from the driver")


class _OkResp:
    def __init__(self, url, payload):
        self.url = url
        self._payload = payload

    def json(self):
        return self._payload


def _log_events(monkeypatch):
    """机读事件捕获桩（双轨制：print 层不动，本段只断言旁路）。"""
    events = []
    monkeypatch.setattr(mod._logging, "log_event", lambda event, **fields: events.append((event, fields)))
    return events


def test_capture_xhr_failure_lands_error_record_and_event(monkeypatch):
    """#56 验收 1（A5①本体）：detail 监听器里被吞掉的异常必须留下两处证据——
    容器记录（供归因）+ error 级机读事件（供回溯「哪条 URL、什么异常」，#48 口径）。
    同时锁「handler 不外抛」：它跑在 dispatcher 的 EventGreenlet 里，往外抛会改变协议链路
    行为（#56 A 段口径：除 A1 外零行为变更）——本用例不包 try，抛出即红。"""
    events = _log_events(monkeypatch)
    container = {"data": None}
    h = mod.capture_xhr(_SendPage(_DETAIL_URL), _DETAIL_FRAG, container)
    h(_BoomResp())
    rec = container["errors"][0]
    assert rec["url"].endswith("detail.json?eid=e1") and rec["at"]
    assert "Timeout 30000ms" in rec["error"], "异常原文要可读，不能只说「失败」"
    assert container["data"] is None, "取证不改控制流：这一次确实还是没拿到数据"
    name, fields = events[0]
    assert name == "xhr_capture_failed"
    assert fields["level"] == "error", "#48：非 error 会被默认阈值吞掉，等于没落"
    assert fields["url"] == rec["url"] and fields["error"] == rec["error"] and fields["frag"] == _DETAIL_FRAG


def test_capture_xhr_unreadable_url_still_logs(monkeypatch):
    """取证层的取证也会坏：`resp.url` 本身抛异常时事件照落（url 空着），
    而不是让取证的缺口变成第三次静默。"""
    events = _log_events(monkeypatch)
    container = {"data": None}
    mod.capture_xhr(_SendPage(_DETAIL_URL), _DETAIL_FRAG, container)(_NoUrlResp())
    assert container["errors"][0]["url"] == ""
    assert events[0][0] == "xhr_capture_failed" and events[0][1]["url"] == ""
    assert "Connection closed" in events[0][1]["error"]


def test_capture_xhr_success_and_miss_record_nothing(monkeypatch):
    """护栏（防过度取证）：命中且 code=0 → 只写 data；URL 不含 frag 的响应完全不写。
    A5 要的是「静默失败变可观测」，不是把成功路径也变成写盘点。"""
    events = _log_events(monkeypatch)
    page = _SendPage(_DETAIL_URL)
    container = {"data": None}
    h = mod.capture_xhr(page, _DETAIL_FRAG, container)
    h(_OkResp(f"https://www.zhipin.com{_DETAIL_FRAG}", {"code": 0, "zpData": {}}))
    h(_OkResp("https://www.zhipin.com/wapi/other.json", {"code": 0}))
    assert container == {"data": {"code": 0, "zpData": {}}}, "成功路径不得多出取证键"
    assert events == []


def test_capture_xhr_nonzero_code_lands_code_record_and_event(monkeypatch):
    """修复轮 F1（`capture_xhr` 的 code!=0 折叠）：旧实现只在 `code == 0` 时写 `container["data"]`，
    非 0 响应（风控 / 岗位失效的返回码）**既不留痕、又被归成「超时未回」**——于是「接口自己
    报了错」与「站点压根没回」读起来是同一个数。现在它落 `container["nonzero"]` + 一条独立事件。
    两条硬约束照旧：① 类别不变（非 0 不是「往返抛异常」，仍保留补投机会）；
    ② handler 不外抛（它跑在 dispatcher 的 EventGreenlet 里，抛出去就改变协议链路行为）。"""
    events = _log_events(monkeypatch)
    container = {"data": None}
    mod.capture_xhr(_SendPage(_DETAIL_URL), _DETAIL_FRAG, container)(
        _OkResp(f"https://www.zhipin.com{_DETAIL_FRAG}", {"code": 37, "message": "风控" * 60})
    )
    assert "errors" not in container, "非 0 返回码不得写进往返异常记录（那是另一条证据链）"
    assert container["data"] is None, "取证不改控制流：这一次确实还是没拿到正文"
    rec = container["nonzero"]
    assert rec["code"] == 37 and len(rec["message"]) == mod.DETAIL_CODE_MESSAGE_CAP, "返回码 + message 截断留痕"
    assert rec["url"].endswith("detail.json") and rec["at"]
    name, f = events[0]
    assert name == "xhr_capture_nonzero_code" and f["level"] == "error", "#48：非 error 会被默认阈值吞掉"
    assert f["code"] == 37 and f["frag"] == _DETAIL_FRAG
    assert mod.classify_no_jd(container) == mod.NO_JD_NO_RESPONSE, "类别仍是超时未回（补投机会不变）"
    assert mod.no_jd_detail_code(container) == {"code": 37, "message": rec["message"]}


def test_capture_xhr_zero_code_records_nothing_extra(monkeypatch):
    """护栏：code=0 的正常响应只写 data，不产生 `nonzero` 痕迹——A5 要的是「静默失败变可观测」，
    不是把成功路径也变成写盘点。"""
    events = _log_events(monkeypatch)
    container = {"data": None}
    mod.capture_xhr(_SendPage(_DETAIL_URL), _DETAIL_FRAG, container)(
        _OkResp(f"https://www.zhipin.com{_DETAIL_FRAG}", {"code": 0, "zpData": {}})
    )
    assert container == {"data": {"code": 0, "zpData": {}}}
    assert events == []


def test_detail_roundtrip_failure_and_nonzero_code_stay_separate_signals(monkeypatch):
    """事件名分开、读数也分开加和（本条是「并入 `xhr_capture_failed`」这一路的否决）：
    `xhr_capture_failed` 的计数就是票面假说一（无超时往返失败）的直接证据量，把非 0 返回码
    并进去会让「往返失败了几次」与「接口自己报了几次错」混成一桶——正是本次要拆开的折叠。
    同一岗两种证据都在时，类别仍按「往返失败优先」（异常是这一处往返失败的直接证据）。"""
    events = _log_events(monkeypatch)
    container = {"data": None}
    h = mod.capture_xhr(_SendPage(_DETAIL_URL), _DETAIL_FRAG, container)
    h(_OkResp(f"https://www.zhipin.com{_DETAIL_FRAG}", {"code": 5, "message": "岗位已失效"}))
    h(_BoomResp())
    assert [e[0] for e in events] == ["xhr_capture_nonzero_code", "xhr_capture_failed"]
    assert mod.classify_no_jd(container) == mod.NO_JD_ROUNDTRIP_FAILED
    assert mod.no_jd_detail_code(container) == {"code": 5, "message": "岗位已失效"}
    assert mod.no_jd_dom_shape(container) == mod.DOM_SHAPE_UNKNOWN, "没探测过就是没探测过"


# --- #56 修复轮 3 R3：一次往返只求值一次、只落一个事件名 ---
#
# `capture_xhr` 的 handler 跑在 dispatcher 的 EventGreenlet 里，里面每次 `resp.json()` 都是
# 一次 `Response.body()` → `send("body", None)` 的**无超时协议往返**（本票的失败点）。
# 落盘路径若在同一次往返里再取一次响应体（helper 自己 `resp.json()`），读数就被扭曲成两种
# 下场之一（都用「只求值一次」排除，实测：把 helper 改成自己取体后跑本段用例）：
#   · 第二次抛、被 helper 的 `except BaseException` 吞掉 → 非 0 留痕与事件**一起丢**（记轻）；
#   · 第二次抛、外逸给 handler 的 `except Exception` → 一次往返落 `xhr_capture_nonzero_code`
#     与 `xhr_capture_failed` **两个事件名**（假说一的证据量被记重）。
# 所以：payload 由调用方求值**一次**后传入，helper 不碰响应体；helper 的
# `except BaseException` 只兜「取证自身失败」（宁缺毋造，绝不改 handler 的控制流）。


class _SecondCallBoomsResp:
    """第一次 `json()` 给出非 0 码、第二次抛的响应替身——「同一次往返取两次」的判别夹具：
    第二次取体正是本票点名的无超时往返，它抛（或不返回）时事件名会落双。"""

    def __init__(self, url, payload):
        self.url = url
        self._payload = payload
        self.calls = 0

    def json(self):
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("Timeout 30000ms exceeded while reading response body（第二次求值）")
        return self._payload


def test_capture_xhr_reads_the_body_once_and_logs_a_single_event_name(monkeypatch):
    """R3 本体：非 0 码这一次往返**只落一个事件名**，且响应体只被求值一次。"""
    events = _log_events(monkeypatch)
    container = {"data": None}
    resp = _SecondCallBoomsResp(f"https://www.zhipin.com{_DETAIL_FRAG}", {"code": 5001, "message": "风控"})
    mod.capture_xhr(_SendPage(_DETAIL_URL), _DETAIL_FRAG, container)(resp)
    assert resp.calls == 1, "响应体只求值一次（多一次就多一次楔死机会）"
    assert [e[0] for e in events] == ["xhr_capture_nonzero_code"], "同一次往返不得再添一个事件名"
    assert "errors" not in container, "非 0 返回码不是往返失败，不得灌进假说一的读数"


def test_capture_xhr_non_dict_payload_falls_to_a_single_failure_event(monkeypatch):
    """既有防护不许在复用 payload 的重构里丢掉：`j` 可能是 None / 非 dict（`j.get` 会抛）。
    这种形态只落**一个** `xhr_capture_failed`，容器不留 `nonzero` 痕迹。"""
    events = _log_events(monkeypatch)
    container = {"data": None}
    mod.capture_xhr(_SendPage(_DETAIL_URL), _DETAIL_FRAG, container)(
        _OkResp(f"https://www.zhipin.com{_DETAIL_FRAG}", None)
    )
    assert [e[0] for e in events] == ["xhr_capture_failed"]
    assert "nonzero" not in container and container["data"] is None
    assert "AttributeError" in events[0][1]["error"]


def test_nonzero_observer_never_touches_the_response_body(monkeypatch):
    """helper 侧的同一条约束：它只吃传进来的 payload，形态再诡异（None / 非字典）也不外抛、
    不二次取体；连 `resp.json` 都没有的替身也照样落痕（取体不在本函数职责内）。"""
    events = _log_events(monkeypatch)
    container = {"data": None}
    mod._observe_xhr_nonzero_code(container, _NoUrlResp(), _DETAIL_FRAG, None)
    assert [e[0] for e in events] == ["xhr_capture_nonzero_code"]
    assert container["nonzero"]["code"] is None and container["nonzero"]["url"] == ""
    for odd in ("不是字典", 123, ["zpData"]):
        mod._observe_xhr_nonzero_code(container, _NoUrlResp(), _DETAIL_FRAG, odd)
    assert len(events) == 4, "每种形态都只落一条自己的事件名，不派生第二个"


def test_capture_xhr_error_records_capped_events_not(monkeypatch):
    """抖动站点会让同一岗的多个响应接连失败：容器记录有上限（取证不留无界缓冲），
    事件不设上限（JSONL 旁路要留全量线索——砍掉事件就是把读数砍回零可观测）。"""
    events = _log_events(monkeypatch)
    container = {"data": None}
    h = mod.capture_xhr(_SendPage(_DETAIL_URL), _DETAIL_FRAG, container)
    for _ in range(mod.XHR_ERROR_RECORD_CAP + 3):
        h(_BoomResp())
    assert len(container["errors"]) == mod.XHR_ERROR_RECORD_CAP
    assert len(events) == mod.XHR_ERROR_RECORD_CAP + 3


def test_capture_xhr_accum_uses_its_own_event_and_flags(monkeypatch):
    """#33 的列表采集监听器同款吞异常一并可观测，但**事件名与容器都不同**：
    no_jd 归因只吃详情容器，混成一个事件名会让「详情往返失败了几次」被列表噪声污染。"""
    events = _log_events(monkeypatch)
    buf, flags = [], {}
    detail_container = {"data": None}
    h = mod.capture_xhr_accum(_SendPage(_LIST_URL), _LIST_FRAG, buf, "jobList", flags)
    h(_BoomResp(url=f"https://www.zhipin.com{_LIST_FRAG}"))
    assert buf == [], "列表容器（list）不写 errors——取证落 flags"
    assert flags["errors"][0]["frag"] == _LIST_FRAG
    assert [e[0] for e in events] == ["xhr_list_capture_failed"]
    assert events[0][1]["level"] == "error"
    assert "errors" not in detail_container, "两条证据链互不串门"
    h2 = mod.capture_xhr(_SendPage(_DETAIL_URL), _DETAIL_FRAG, detail_container)
    h2(_BoomResp())
    assert [name for name, _ in events] == ["xhr_list_capture_failed", "xhr_capture_failed"]


def test_capture_xhr_accum_without_flags_still_logs(monkeypatch):
    """flags 缺省（调用方没给可写字典）时事件照落，且不炸——归因侧自然也不会去猜。"""
    events = _log_events(monkeypatch)
    buf = []
    mod.capture_xhr_accum(_SendPage(_LIST_URL), _LIST_FRAG, buf, "jobList")(
        _BoomResp(url=f"https://www.zhipin.com{_LIST_FRAG}")
    )
    assert [e[0] for e in events] == ["xhr_list_capture_failed"]
    assert buf == []


# --- #56 取证 A：两个采集 handler 的「一次往返一进一出」---


def test_capture_xhr_pairs_one_enter_with_one_exit_per_body_read(monkeypatch):
    """包一层 enter/exit **不许**改变取体次数与控制流：
    · `resp.json()` 仍只求值一次（R3 那条铁律——第二次取体就是第二次无超时往返）；
    · 命中端点才进计数（不命中的响应一次往返都没有，读数才数得出「这个端点回来过几次」）；
    · 失败形态（`json()` 抛）照样平衡，异常照旧落进容器 + 事件、绝不外抛。"""
    fx._reset_handler_forensics()
    events = _log_events(monkeypatch)
    page = _SendPage(_DETAIL_URL)
    container = {"data": None}
    h = mod.capture_xhr(page, _DETAIL_FRAG, container)
    assert fx.handler_forensics()["handlers_live"] == 1, "`page.on` 成功后就该登记"

    resp = _SecondCallBoomsResp(f"https://www.zhipin.com{_DETAIL_FRAG}", {"code": 0, "zpData": {"x": 1}})
    h(resp)
    assert resp.calls == 1, "响应体只求值一次（enter/exit 包的是同一条语句）"
    assert container["data"] == {"code": 0, "zpData": {"x": 1}}
    assert fx.handler_forensics()["handlers_inflight"] == 0, "成功出口必须平衡"

    h(_BoomResp())  # 取体抛异常：finally 照样 exit，容器 + 事件之外不多做事
    assert fx.handler_forensics()["handlers_inflight"] == 0
    assert [e[0] for e in events] == ["xhr_capture_failed"]

    h(_BoomResp(url="https://www.zhipin.com/别的端点.json"))  # 未命中 frag ⇒ 不进入往返
    assert fx.handler_forensics()["handler_last_frag"] == _DETAIL_FRAG, "最后一次进入的仍是 detail"


def test_capture_xhr_accum_counts_each_list_roundtrip_separately(monkeypatch):
    """列表侧同款：两个频道交错并发时「在途」可以是 2，这正是取证 A 要读的数——
    所以每个 handler 的每次取体各包一对，而不是全局一刀切。"""
    fx._reset_handler_forensics()
    events = _log_events(monkeypatch)
    buf, flags = [], {}
    h = mod.capture_xhr_accum(_SendPage(_LIST_URL), _LIST_FRAG, buf, "jobList", flags)
    h(_OkResp(f"https://www.zhipin.com{_LIST_FRAG}", {"code": 0, "zpData": {"jobList": [{"encryptJobId": "j1"}]}}))
    assert fx.handler_forensics() == {"handlers_live": 1, "handlers_inflight": 0, "handler_last_frag": _LIST_FRAG}
    assert len(buf) == 1, "取证接线不改累积语义"

    h(_BoomResp(url=f"https://www.zhipin.com{_LIST_FRAG}"))
    assert fx.handler_forensics()["handlers_inflight"] == 0
    assert [e[0] for e in events] == ["xhr_list_capture_failed"]


def test_classify_no_jd_three_classes_from_container_evidence():
    """#56 A5② + 修复轮 F1：三类判据（往返失败 / 超时未回 / DOM 与 XHR 双空）只读容器证据，
    顺序敏感——有异常就归「往返失败」，哪怕 data 也缺失（那是它的下游结果）。
    类别只管「给不给补投机会」；「DOM 漂没漂」「接口报没报错」由 `no_jd_dom_shape` /
    `no_jd_detail_code` 两条独立读数回答（下面四条用例），类别集合与台账政策都不因它们变化。"""
    at = "2026-09-28T00:00:00"
    assert (
        mod.classify_no_jd({"data": None, "errors": [{"url": "u", "error": "boom", "at": at}]})
        == mod.NO_JD_ROUNDTRIP_FAILED
    )
    # 修复轮 3（R1）把「双空」收窄成**两侧都为空**：XHR 侧无正文之外还要求 DOM 侧的探测证据，
    # 故这里必须一并给出 hit_total=0 的探测结果（裸容器不再凭空造出丢岗的正向证据）
    assert (
        mod.classify_no_jd(
            {
                "data": {"code": 0, "zpData": {"jobInfo": {"postDescription": ""}}},
                "empty_outcome": {"hit_total": 0, "nonempty_total": 0},
            }
        )
        == mod.NO_JD_BOTH_EMPTY
    )
    assert mod.classify_no_jd({"data": None}) == mod.NO_JD_NO_RESPONSE, (
        "无 data 无 error＝没拿到可用响应（非 0 返回码与完全没回两种形态，靠 detail_code 读数分开）"
    )
    assert mod.classify_no_jd({"data": None, "errors": []}) == mod.NO_JD_NO_RESPONSE, "空列表不算失败证据"
    # 往返失败优先：异常 + 无 data 是同一根因的两种表征，归错类会把站点抖动说成岗位失效
    both = {"data": None, "errors": [{"url": "u", "error": "boom", "at": at}], "empty_outcome": {"hit_total": 0}}
    assert mod.classify_no_jd(both) == mod.NO_JD_ROUNDTRIP_FAILED
    # DOM 零命中是「双空」的**必要**条件之一（修复轮 3 R1），但不是充分条件：没有 XHR 侧那份
    # 「code=0 的响应确实回来了」的正向证据时，仍然只是「没拿到可用响应」＝保留补投机会
    assert mod.classify_no_jd({"data": None, "empty_outcome": {"hit_total": 0}}) == mod.NO_JD_NO_RESPONSE


# --- #56 修复轮 3 R1（阻塞项）：「双空」的触发集必须落在「JD 正文为空」上 ---
#
# 旧实现 `if container.get("data"): return NO_JD_BOTH_EMPTY` 只看「容器里有 data」，不看那份
# 响应里到底有没有正文，也不看 DOM 侧有没有证据。后果有两层（`apply_retry_outcome` 里
# **只有双空会移出台账**）：
#   ① grab_detail 已经拿到正文、探测又报 hit_total=0（＝票面「详情页形态漂移」那一侧）的岗，
#      被判「岗位无内容的正向证据」并**永久丢岗**；
#   ② report 的 `no_jd_classes[双空]` 被形态漂移的岗污染，验收 4 的分流被推向票面另一侧。
# 新判据：XHR 侧按 `grab_detail` 实际取值的那条链（`detail_post_description`，单一真源）判
# 「这份响应确实没有 JD 正文」，DOM 侧按 `no_jd_dom_shape` 判「探测有证据且无正文」。


def test_classify_no_jd_both_empty_requires_both_sides_empty():
    """复审给的反例：带正文的 code=0 响应 + 探测零命中 → 不是双空，而是 `detail_no_response`
    （保留补投机会），形态漂移那一侧仍由 `dom_shape=zero_hits` 如实报出来。"""
    with_body = {
        "data": {"code": 0, "zpData": {"jobInfo": {"postDescription": "负责智能合约开发与审计"}}},
        "empty_outcome": {"hit_total": 0, "nonempty_total": 0},
    }
    assert mod.classify_no_jd(with_body) == mod.NO_JD_NO_RESPONSE, "XHR 侧有正文＝不配「双空」这句定性"
    assert mod.no_jd_dom_shape(with_body) == mod.DOM_SHAPE_ZERO_HITS, "形态漂移的证据不因为改了判据而消失"
    keep, reason = mod.apply_retry_outcome({"attempts": 0}, "no_jd", jd_class=mod.classify_no_jd(with_body))
    assert (keep, reason) == (True, "kept"), "只有双空丢岗：这类岗必须留在补投台账里"
    # DOM 侧无证据（探测缺失/异常）时也不许凭空造出双空——`_record_empty_detail` 的宁缺毋造口径
    assert mod.classify_no_jd({"data": {"code": 0, "zpData": {"jobInfo": {"postDescription": ""}}}}) == (
        mod.NO_JD_NO_RESPONSE
    ), "落空时刻没有 DOM 探测证据＝取证缺口，不是「岗位无内容」"
    # DOM 探测反而读出正文（落空时刻与探测时刻不一致）时同样不双空：两侧都没有的证据不成立
    assert (
        mod.classify_no_jd(
            {
                "data": {"code": 0, "zpData": {"jobInfo": {"postDescription": ""}}},
                "empty_outcome": {"hit_total": 2, "nonempty_total": 1},
            }
        )
        == mod.NO_JD_NO_RESPONSE
    )


def test_classify_no_jd_both_empty_when_both_sides_truly_empty():
    """允许移出台账的那一种形态：code=0 的响应确实回来了、按 `grab_detail` 的同一条取值链
    拿不到 JD 正文，且 DOM 探测有证据、也没给出正文（零命中＝选择器不存在，命中皆空＝元素在但没字）。"""
    for dom in ({"hit_total": 0, "nonempty_total": 0}, {"hit_total": 3, "nonempty_total": 0}):
        container = {"data": {"code": 0, "zpData": {"jobInfo": {"postDescription": ""}}}, "empty_outcome": dom}
        assert mod.classify_no_jd(container) == mod.NO_JD_BOTH_EMPTY, f"DOM 形态 {dom} 下双空成立"
        assert mod.apply_retry_outcome({"attempts": 1}, "no_jd", jd_class=mod.NO_JD_BOTH_EMPTY)[0] is False


def test_both_empty_and_grab_detail_share_one_post_description_chain():
    """R2 的单一真源：编排层判「XHR 侧没有 JD 正文」用的就是 `grab_detail` 取值的那条链，
    不是自己在编排层重猜一遍 key（否则 `zpData: null` 这类形态会在两处得到两个答案）。"""
    from platforms.chat_first import detail_post_description as chain_fn

    assert mod.detail_post_description is chain_fn
    bodyless = {"code": 0, "zpData": None, "message": "岗位已下线"}
    assert chain_fn(bodyless) == ""
    assert mod.classify_no_jd({"data": bodyless, "empty_outcome": {"hit_total": 0, "nonempty_total": 0}}) == (
        mod.NO_JD_BOTH_EMPTY
    )


def test_no_jd_with_body_and_zero_dom_hits_keeps_the_job(tmp_path, monkeypatch, capsys):
    """R1 的端到端版（真实单岗链路 + report 读数）：容器里是一份带正文的 code=0 响应而探测零命中时
    ——① 结果条目类为「超时未回」、`dom_shape=zero_hits`；② 台账保留（不移出）；
    ③ report 的 `no_jd_classes[双空]` 为 0、`no_jd_dom_shapes[zero_hits]` 为 1（分流不被污染）。
    这份「返回值与容器分叉」的形态由 grab 替身构造（真 grab_detail 走同一条链后不会再出现），
    留它是把「判据不看外层 dict 真值」这条钉在链路上，而不是只钉在纯函数上。"""
    events = _log_events(monkeypatch)

    def grab(_page, container, **kw):
        container["data"] = {"code": 0, "zpData": {"jobInfo": {"postDescription": "JD 正文：负责合约审计"}}}
        container["empty_outcome"] = {"seconds": 5.3, "hit_total": 0, "nonempty_total": 0, "selectors": []}
        return ""

    results, _ = _run_apply_on_page(tmp_path, monkeypatch, _ListenerPage(_DETAIL_URL), times=1, grab=grab)
    assert results[0]["status"] == "no_jd"
    assert results[0]["jd_class"] == mod.NO_JD_NO_RESPONSE
    assert results[0]["dom_shape"] == mod.DOM_SHAPE_ZERO_HITS
    entry = {"attempts": 0}
    assert mod.apply_retry_outcome(entry, "no_jd", jd_class=results[0]["jd_class"])[0] is True, "不许永久丢岗"
    rep = mod.build_report(["q"], {"applied": 0, "direct_fallback": 0}, results)
    assert rep["no_jd_classes"][mod.NO_JD_BOTH_EMPTY] == 0, "双空桶不被形态漂移的岗污染"
    assert rep["no_jd_dom_shapes"][mod.DOM_SHAPE_ZERO_HITS] == 1
    out = capsys.readouterr().out
    assert "形态漂移" in out and "弹回列表" in out, "修复轮 4（E）：零命中当场两假说并列（旧断言式措辞已删）"
    assert events[0][1]["jd_class"] == mod.NO_JD_NO_RESPONSE


def test_no_jd_produces_no_request_file_and_request_always_carries_a_body(tmp_path, monkeypatch):
    """R2 的语义钉：`_acquire_and_draft_greeting` **只在拿到非空字符串正文时**才产
    request_N.json，且产出的 jdText 恒非空——起草侧收到一份空/畸形 JD 的请求，等于让 agent
    对着空白写招呼语，而 report 里它读起来像一次正常起草。
    三个方向各跑一遍（同一处判据的两侧都要有门）：落空串 / 越契约的返回值（非字符串、纯空白）
    都走 no_jd 且不产文件；有正文才产文件。"""

    def no_body(_page, container, **kw):
        container["data"] = {"code": 0, "zpData": None}
        return ""

    results, _ = _run_apply_on_page(tmp_path, monkeypatch, _ListenerPage(_DETAIL_URL), times=1, grab=no_body)
    assert [r["status"] for r in results] == ["no_jd"]
    assert not (tmp_path / "request_1.json").exists(), "拿不到正文就不产 request，直接走 no_jd"

    # 平台层返回值越了契约（非字符串正文 / 纯空白）也不许当成「拿到了」——R2 之前的旧写法
    # `if not jd_text:` 会让 123 一路走到 request_greeting，产出一份 jdText=123 的请求。
    for odd in (123, "   ", "\n\t"):

        def odd_body(_page, _container, _odd=odd, **kw):
            return _odd

        results, _ = _run_apply_on_page(tmp_path, monkeypatch, _ListenerPage(_DETAIL_URL), times=1, grab=odd_body)
        assert [r["status"] for r in results] == ["no_jd"], f"返回值 {odd!r} 不是 JD 正文，不得产 request"
        assert not (tmp_path / "request_1.json").exists()

    ok, _ = _run_apply_on_page(tmp_path, monkeypatch, _ListenerPage(_DETAIL_URL), times=1)
    req = json.loads((tmp_path / "request_1.json").read_text(encoding="utf-8"))
    assert ok[0]["status"] == "applied"
    assert req["jdText"] == "JD 全文：负责测试", "request_N.json 的 jdText 恒非空"


def test_no_jd_dom_shape_reads_the_dom_side_evidence():
    """修复轮 F1 的判别力本体：`grab_detail` 的选择器命中探测结果过去**只进事件、从不参与归因**，
    于是「往返没回 + DOM 选择器全落空」（票面的头号形态漂移嫌疑）被折进「超时未回＝站点抖动」，
    读 report 分不出形态漂移这一侧。`hit_total` 为 0 就是漂移嫌疑，必须成为一条独立读数。"""
    assert mod.no_jd_dom_shape({"data": None, "empty_outcome": {"hit_total": 0, "nonempty_total": 0}}) == (
        mod.DOM_SHAPE_ZERO_HITS
    )
    assert mod.no_jd_dom_shape({"empty_outcome": {"hit_total": 3, "nonempty_total": 0}}) == mod.DOM_SHAPE_EMPTY_HITS
    assert mod.no_jd_dom_shape({"empty_outcome": {"hit_total": 3, "nonempty_total": 1}}) == mod.DOM_SHAPE_TEXT_HITS


def test_no_jd_dom_shape_never_mistakes_forensic_gap_for_zero_hits():
    """`hit_total: 0` 会被下游读成「选择器全落空」这一形态漂移的正向证据，所以
    **探测缺失/异常一律落 unknown**（chat_first.py `_record_empty_detail` 已立的诚实性口径：
    「探测返回形态异常时不写命中数——宁缺毋造」，#43/#53 同族）。"""
    assert mod.no_jd_dom_shape({"data": None}) == mod.DOM_SHAPE_UNKNOWN, "桩页面/被替换的 grab_detail"
    assert mod.no_jd_dom_shape({"empty_outcome": {"probe_error": "TypeError: 探测返回形态异常"}}) == (
        mod.DOM_SHAPE_UNKNOWN
    )
    assert mod.no_jd_dom_shape({"empty_outcome": {}}) == mod.DOM_SHAPE_UNKNOWN
    assert mod.no_jd_dom_shape({"empty_outcome": {"hit_total": None}}) == mod.DOM_SHAPE_UNKNOWN
    assert mod.no_jd_dom_shape({"empty_outcome": {"hit_total": "0"}}) == mod.DOM_SHAPE_UNKNOWN, "字符串 0 不是计数证据"
    assert mod.no_jd_dom_shape({"empty_outcome": "不是字典"}) == mod.DOM_SHAPE_UNKNOWN
    for bad in (None, {}, "不是容器"):
        assert mod.no_jd_dom_shape(bad) == mod.DOM_SHAPE_UNKNOWN


def test_no_jd_detail_code_reads_only_the_nonzero_form():
    """`detail_code` 读数只回答一件事：这次 no_jd 有没有「接口自己回了个错」的留痕。
    code=0 与完全没回都不写它——那两种才归「超时未回」的另一半。"""
    assert mod.no_jd_detail_code({"data": None, "nonzero": {"code": 0, "message": "x"}}) == {"code": 0, "message": "x"}
    assert mod.no_jd_detail_code({"data": None}) is None
    assert mod.no_jd_detail_code({"nonzero": "坏形态"}) is None
    assert mod.no_jd_detail_code({"nonzero": {"message": "没有码"}}) is None, "无 code 键不算留痕"
    for bad in (None, {}, "不是容器"):
        assert mod.no_jd_detail_code(bad) is None


def test_no_jd_class_wording_matches_the_new_criteria():
    """修复轮 F1 + 修复轮 3 R1：措辞要跟判据一字不差——旧口径让「DOM 与 XHR 双空」听着像
    「响应没回也算双空」，而它专指「code=0 的响应已到、按 grab_detail 的同一条取值链拿不到正文，
    **且** DOM 侧探测也没有正文」；「超时未回」则要如实说出它涵盖两种形态、靠哪个读数分开
    （`classify_no_jd` docstring 同一条论证）。"""
    both = mod.NO_JD_CLASS_DESC[mod.NO_JD_BOTH_EMPTY]
    assert "响应" in both
    assert "code" in both.lower() or "code=0" in both
    assert "正文" in both, "判据看的是 JD 正文，不是「容器里有 data」"
    assert "探测" in both and "DOM" in both, "R1：DOM 侧那一半也要写进口径，否则措辞又跑在判据前面"
    no_resp = mod.NO_JD_CLASS_DESC[mod.NO_JD_NO_RESPONSE]
    assert "非 0" in no_resp and "没回" in no_resp, "两类同归一处的理由要写在口径里，不能只留一个「超时未回」"
    assert "detail_code" in no_resp, "要说清靠哪条读数分开"
    assert "双空" not in mod.NO_JD_CLASS_DESC[mod.NO_JD_ROUNDTRIP_FAILED]
    assert "双空" not in no_resp
    # 三个短名保持票面原话（改的是判据，不是类别名）
    assert mod.NO_JD_CLASS_SHORT[mod.NO_JD_BOTH_EMPTY] == "DOM 与 XHR 双空"
    assert mod.NO_JD_CLASS_SHORT[mod.NO_JD_NO_RESPONSE] == "详情超时未回"
    assert mod.NO_JD_CLASS_SHORT[mod.NO_JD_ROUNDTRIP_FAILED] == "详情往返失败"


def test_classify_no_jd_boundary_forms_are_conservative():
    """边界形态（桩容器 / 缺键 / 非字典 / errors 形态诡异）一律保守归「超时未回」——
    三类里只有「双空」会丢岗，而没有证据时说「岗位无内容」就是票面点名的静默误杀。
    （#41/#42 裁决：漏投比错投代价高。）"""
    assert mod.classify_no_jd({}) == mod.NO_JD_NO_RESPONSE
    assert mod.classify_no_jd(None) == mod.NO_JD_NO_RESPONSE
    assert mod.classify_no_jd("not-a-container") == mod.NO_JD_NO_RESPONSE
    assert mod.classify_no_jd({"data": None, "errors": "怎么是字符串"}) == mod.NO_JD_NO_RESPONSE
    assert mod.classify_no_jd({"errors": [None]}) == mod.NO_JD_ROUNDTRIP_FAILED, (
        "列表非空即有往返失败迹象，条目形态诡异也不作为丢证据的理由"
    )
    assert mod.classify_no_jd({"data": {}, "errors": []}) == mod.NO_JD_NO_RESPONSE, "空 dict 不算「响应到了」"


def test_log_detail_empty_event_answers_the_four_questions(monkeypatch, tmp_path):
    """#56 A5①：grab_detail 落空的那一次必须一条事件答完票面四问——
    哪条 URL / 什么异常（来自容器）/ 等了多久 / DOM 选择器命中数，并带岗位身份。
    事件落编排层（平台层不 import 日志侧车，同 #56 A3 的分层口径）。"""
    events = _log_events(monkeypatch)
    flow = mod._JobFlow(
        page=_SendPage(_DETAIL_URL),
        job={"jobName": "区块链开发工程师", "encryptJobId": "e1"},
        idx=4,
        ctx=_a3_ctx(tmp_path),
    )
    flow.detail = {
        "data": None,
        "errors": [{"url": "u2", "error": "RuntimeError: Timeout reading body", "at": "2026-09-28T00:00:00"}],
        "empty_outcome": {
            "url": _DETAIL_URL,
            "seconds": 20.2,
            "dom_seconds": 5.0,
            "xhr_seconds": 15.2,
            "dom_rounds": 10,
            "xhr_rounds": 50,
            "hit_total": 1,
            "nonempty_total": 0,
            "hit_selectors": [".job-sec-text"],
            "selectors": [{"selector": ".job-sec-text", "hits": 1, "nonempty": 0}],
        },
    }
    mod._log_detail_empty(flow.page, flow, mod.NO_JD_ROUNDTRIP_FAILED)
    name, f = events[0]
    assert name == "detail_empty_outcome" and f["level"] == "error"
    assert f["url"] == _DETAIL_URL, "落空时刻的详情页 URL"
    assert f["error"] == "RuntimeError: Timeout reading body" and f["error_count"] == 1, "什么异常"
    assert f["waited_seconds"] == 20.2 and f["dom_rounds"] == 10 and f["xhr_rounds"] == 50, "等了多久"
    assert f["dom_hits"] == 1 and f["dom_nonempty"] == 0 and f["hit_selectors"] == [".job-sec-text"], "命中数"
    assert (f["job"], f["eid"], f["idx"]) == ("区块链开发工程师", "e1", 4), "取证要能指到具体哪个岗"
    assert f["jd_class"] == mod.NO_JD_ROUNDTRIP_FAILED and f["probe_present"] is True


def test_log_detail_empty_is_honest_without_probe_evidence(monkeypatch, tmp_path):
    """取证缺口本身也是证据：grab_detail 被替换（桩）或旧形态容器没有 empty_outcome 时，
    事件照落、字段留 None、`probe_present=False`——不能因为缺口就退回零可观测，
    也不能把「没探到」谎报成「命中数为 0」（后者是双空的正向证据）。"""
    events = _log_events(monkeypatch)
    flow = mod._JobFlow(
        page=_SendPage(_DETAIL_URL), job={"jobName": "岗", "encryptJobId": "e9"}, idx=1, ctx=_a3_ctx(tmp_path)
    )
    mod._log_detail_empty(flow.page, flow, mod.NO_JD_NO_RESPONSE)
    _, f = events[0]
    assert f["probe_present"] is False and f["waited_seconds"] is None and f["dom_hits"] is None
    assert f["hit_selectors"] == [] and f["error"] == "" and f["error_count"] == 0
    assert f["url"] == _DETAIL_URL, "URL 由编排层从 page 取，不依赖平台层探测"


def test_log_detail_empty_carries_the_two_split_readings(monkeypatch, tmp_path):
    """修复轮 F1：`detail_empty_outcome` 事件也要带得上两条分流读数（DOM 形态 + 非 0 返回码），
    与结果条目、report 同源同判据——事后翻 JSONL 与读 report.json 必须得到同一个答案。"""
    events = _log_events(monkeypatch)
    flow = mod._JobFlow(
        page=_SendPage(_DETAIL_URL), job={"jobName": "岗", "encryptJobId": "e7"}, idx=7, ctx=_a3_ctx(tmp_path)
    )
    flow.detail = {
        "data": None,
        "nonzero": {"code": 37, "message": "风控", "url": _DETAIL_URL, "at": "2026-09-28T00:00:00"},
        "empty_outcome": {"hit_total": 0, "nonempty_total": 0, "seconds": 20.1},
    }
    mod._log_detail_empty(flow.page, flow, mod.NO_JD_NO_RESPONSE)
    _, f = events[0]
    assert f["dom_shape"] == mod.DOM_SHAPE_ZERO_HITS and f["dom_hits"] == 0
    assert f["detail_code"] == {"code": 37, "message": "风控"} and f["detail_code_nonzero"] is True
    assert f["probe_present"] is True


def test_log_detail_empty_readings_stay_unknown_without_forensics(monkeypatch, tmp_path):
    """同一事件里的缺口侧：没有探测证据时 `dom_shape` 落 unknown、`detail_code_nonzero` 落 False
    （不是 0、不是「没报错」）——取证缺口不得伪装成任何一种证据形态。"""
    events = _log_events(monkeypatch)
    flow = mod._JobFlow(
        page=_SendPage(_DETAIL_URL), job={"jobName": "岗", "encryptJobId": "e8"}, idx=8, ctx=_a3_ctx(tmp_path)
    )
    flow.detail = {"data": None}
    mod._log_detail_empty(flow.page, flow, mod.NO_JD_NO_RESPONSE)
    _, f = events[0]
    assert f["dom_shape"] == mod.DOM_SHAPE_UNKNOWN and f["dom_hits"] is None
    assert f["detail_code"] is None and f["detail_code_nonzero"] is False


def test_no_jd_result_and_print_carry_split_readings_end_to_end(tmp_path, monkeypatch, capsys):
    """#56 验收 4 的判别力（真实单岗链路，修复轮 F1 的关键形态）：**往返没回 + DOM 选择器
    全落空**——类别仍是「详情超时未回」（保留补投机会，不改台账政策），但结果条目带
    `dom_shape=zero_hits`、终端与 report 都读得出这一侧。
    旧实现里这条形态与「站点抖动」是同一个数，读报告的人分不出该补开票还是该修网络。
    修复轮 4（E）：零命中的措辞降到两假说并列（形态漂移 / 已离岗弹回列表），不再是断言。"""
    events = _log_events(monkeypatch)

    def grab(_page, container, **kw):
        container["empty_outcome"] = {
            "seconds": 20.1,
            "hit_total": 0,
            "nonempty_total": 0,
            "hit_selectors": [],
            "selectors": [],
        }
        return ""

    results, _ = _run_apply_on_page(tmp_path, monkeypatch, _ListenerPage(_DETAIL_URL), times=1, grab=grab)
    assert results[0]["jd_class"] == mod.NO_JD_NO_RESPONSE, "类别不变：仍保留补投机会"
    assert results[0]["dom_shape"] == mod.DOM_SHAPE_ZERO_HITS and results[0]["detail_code"] is None
    out = capsys.readouterr().out
    assert "形态漂移" in out and "弹回列表" in out, "修复轮 4（E）：零命中当场念出两种可能（并列）"
    assert events[0][1]["dom_shape"] == mod.DOM_SHAPE_ZERO_HITS
    rep = mod.build_report(["q"], {"applied": 0, "direct_fallback": 0}, results)
    assert rep["no_jd_dom_shapes"][mod.DOM_SHAPE_ZERO_HITS] == 1
    assert rep["no_jd_code_nonzero_by_class"] == dict.fromkeys(mod.NO_JD_CLASSES, 0), "这条没有非 0 返回码"


def test_no_jd_zero_hits_speaks_two_hypotheses_and_lands_the_url(tmp_path, monkeypatch, capsys):
    """修复轮 4（E）：「hit_total=0」不是「形态漂移」的单义证据——#33 第八轮既定事实：
    分栏视图 `/web/geek/jobs?query=…` 的 URL 不含 job_detail，**人已离岗被弹回列表也是这个
    URL**（要当场分开就得再取右栏标题＝又一次无超时 evaluate，本票正在治这类站点、禁止新增）。
    按「如实降精度」处置：播报两假说并列；落空时刻 url 由 `_record_empty_detail` 落进容器后，
    随结果条目/事件/report 读数一起落盘，人工判读能看出落空时刻在哪（配合 C 段栈分辨）。"""
    events = _log_events(monkeypatch)
    bounced_url = "https://www.zhipin.com/web/geek/jobs?query=Solidity&city=101020100"

    def grab(_page, container, **kw):
        container["empty_outcome"] = {
            "seconds": 20.1,
            "hit_total": 0,
            "nonempty_total": 0,
            "hit_selectors": [],
            "selectors": [],
            "url": bounced_url,
        }
        return ""

    results, _ = _run_apply_on_page(tmp_path, monkeypatch, _ListenerPage(_DETAIL_URL), times=1, grab=grab)
    r = results[0]
    assert r["dom_shape"] == mod.DOM_SHAPE_ZERO_HITS
    assert r["empty_url"] == bounced_url, "落空时刻 URL 随结果条目落盘（人工判读用）"
    out = capsys.readouterr().out
    assert "形态漂移" in out and "弹回列表" in out, "终端两假说并列"
    assert "＝形态漂移嫌疑" not in out, "断言式旧措辞必须消失"
    assert events[0][1]["empty_url"] == bounced_url, "事件同样带落空时刻 URL"
    rep = mod.build_report(["q"], {"applied": 0, "direct_fallback": 0}, results)
    assert rep["no_jd_empty_urls"] == [
        {"idx": r["idx"], "job": r["job"], "dom_shape": mod.DOM_SHAPE_ZERO_HITS, "url": bounced_url}
    ], "report 读数带 url：要不要判漂移靠它（配合 C 段栈），不再单指一假说"
    label = mod._dom_shape_label(mod.DOM_SHAPE_ZERO_HITS)
    assert "形态漂移" in label and "弹回" in label, "读数措辞（[done] 行渲染的就是它）两假说并列"


def test_no_jd_drift_and_gap_readings_do_not_collapse(tmp_path, monkeypatch):
    """漂移嫌疑与取证缺口在两处落点上都不得混成同一个数（桩 grab_detail 拿不到探测证据）。"""

    def probe_zero(_page, container, **kw):
        container["empty_outcome"] = {"hit_total": 0, "nonempty_total": 0}
        return ""

    drift, _ = _run_apply_on_page(tmp_path, monkeypatch, _ListenerPage(_DETAIL_URL), times=1, grab=probe_zero)
    gap, _ = _run_apply_on_page(tmp_path, monkeypatch, _ListenerPage(_DETAIL_URL), times=1, grab=lambda *_a, **k: "")
    assert drift[0]["dom_shape"] == mod.DOM_SHAPE_ZERO_HITS and gap[0]["dom_shape"] == mod.DOM_SHAPE_UNKNOWN
    rep = mod.build_report(["q"], {"applied": 0, "direct_fallback": 0}, drift + gap)
    assert rep["no_jd_dom_shapes"][mod.DOM_SHAPE_ZERO_HITS] == 1
    assert rep["no_jd_dom_shapes"][mod.DOM_SHAPE_UNKNOWN] == 1


def test_log_detail_empty_never_raises(monkeypatch, tmp_path):
    """观测层绝不反噬投递（#38/#54 同口径）：容器形态诡异 + page.url 属性抛异常时静默返回。"""
    _log_events(monkeypatch)

    class _BadPage:
        @property
        def url(self):
            raise RuntimeError("browser has been closed")

    flow = mod._JobFlow(page=_BadPage(), job={"jobName": "岗"}, idx=1, ctx=_a3_ctx(tmp_path))
    flow.detail = "不是字典"
    mod._log_detail_empty(_BadPage(), flow, mod.NO_JD_BOTH_EMPTY)  # 不抛出即通过


def test_no_jd_result_and_print_carry_class_end_to_end(tmp_path, monkeypatch, capsys):
    """#56 验收 1 + 4 的交点（真实单岗链路）：grab_detail 落空 → 结果条目带 `jd_class`、
    终端打印按类说话（人读层前缀措辞不变）、事件与台账分流吃同一个类。"""
    events = _log_events(monkeypatch)

    def grab(_page, container, **kw):
        container["errors"] = [{"url": "u", "error": "RuntimeError: Timeout reading body", "at": "x"}]
        container["empty_outcome"] = {"seconds": 20.2, "hit_total": 0, "hit_selectors": [], "selectors": []}
        return ""

    results, _ = _run_apply_on_page(tmp_path, monkeypatch, _ListenerPage(_DETAIL_URL), times=1, grab=grab)
    assert results[0]["status"] == "no_jd" and results[0]["jd_class"] == mod.NO_JD_ROUNDTRIP_FAILED
    out = capsys.readouterr().out
    assert "没拿到 JD 全文（详情往返失败" in out, "人读层要带类别，但不改既有前缀措辞"
    assert "形态漂移" in out and "弹回列表" in out, "修复轮 4（E）：零命中两侧并列念出（不再是断言）"
    assert [e[0] for e in events] == ["detail_empty_outcome"]
    assert events[0][1]["jd_class"] == mod.NO_JD_ROUNDTRIP_FAILED


def test_no_jd_class_both_empty_when_response_arrives_empty(tmp_path, monkeypatch):
    """同一链路的第二类：详情响应到了但正文为空（且 DOM 探测零命中）→ dom_xhr_both_empty，
    这是唯一允许把岗位移出补投台账的证据形态。"""

    def grab(_page, container, **kw):
        container["data"] = {"code": 0, "zpData": {"jobInfo": {"postDescription": ""}}}
        container["empty_outcome"] = {"seconds": 5.3, "hit_total": 0, "hit_selectors": [], "selectors": []}
        return ""

    results, _ = _run_apply_on_page(tmp_path, monkeypatch, _ListenerPage(_DETAIL_URL), times=1, grab=grab)
    assert results[0]["jd_class"] == mod.NO_JD_BOTH_EMPTY


def test_no_jd_class_timeout_when_container_stays_empty(tmp_path, monkeypatch):
    """第三类：容器什么都没有（等满 XHR 窗口既无响应也无异常）→ 超时未回，留在台账。"""
    results, _ = _run_apply_on_page(
        tmp_path, monkeypatch, _ListenerPage(_DETAIL_URL), times=1, grab=lambda *_a, **k: ""
    )
    assert results[0]["jd_class"] == mod.NO_JD_NO_RESPONSE


def test_apply_retry_outcome_no_jd_splits_by_evidence_class():
    """#56 验收 1（A5③）：站点抖动两类（往返失败 / 超时未回）留在补投台账并 attempts+1；
    只有「DOM 与 XHR 双空」才移出。旧实现把 no_jd 整体移出＝票面点名的「反向作用」。"""
    for cls in (mod.NO_JD_ROUNDTRIP_FAILED, mod.NO_JD_NO_RESPONSE, None, "", "future_class"):
        entry = {"attempts": 0}
        assert mod.apply_retry_outcome(entry, "no_jd", jd_class=cls) == (True, "kept"), f"{cls} 不得丢岗"
        assert entry["attempts"] == 1, "保留即消耗一次补投机会（封顶兜底仍在）"
    entry = {"attempts": 1}
    assert mod.apply_retry_outcome(entry, "no_jd", jd_class=mod.NO_JD_BOTH_EMPTY) == (
        False,
        "no_jd_dom_xhr_both_empty",
    ), "只有双空有「岗位无内容」的正向证据"
    assert entry["attempts"] == 1, "移出不改 attempts"
    cap = {"attempts": mod.RETRY_ATTEMPTS_CAP - 1}
    assert mod.apply_retry_outcome(cap, "no_jd", jd_class=mod.NO_JD_ROUNDTRIP_FAILED) == (False, "attempts_cap")


def test_retry_remove_desc_speaks_by_evidence():
    """#56 A5②纠偏：台账口径里不再有「岗位疑已失效」这种未经证据的定性，
    no_jd 的移出理由只可能是「DOM 与 XHR 双空」。"""
    assert "no_jd" not in mod._RETRY_REMOVE_DESC, "no_jd 不再是无条件移出理由"
    assert mod._RETRY_REMOVE_DESC["no_jd_dom_xhr_both_empty"] == mod.NO_JD_CLASS_DESC[mod.NO_JD_BOTH_EMPTY]
    assert not any("疑已失效" in d or "详情不可达" in d for d in mod._RETRY_REMOVE_DESC.values())


def test_retry_pending_jobs_keeps_flaky_no_jd_and_drops_both_empty(tmp_path, monkeypatch, capsys):
    """#56 验收 1（A5③端到端）：同一轮台账里抖动型 no_jd 岗保留（attempts+1、落盘、按类播报），
    双空型移出——补投队列不再被静默误杀。"""
    session, run = _retry_run(tmp_path)
    path = Path(session.cfg["retry_pending_path"])
    mod._save_retry_pending(
        path,
        [
            mod._retry_snapshot(_timeout_job("job1"), jd_text=""),
            mod._retry_snapshot(_timeout_job("job2", name="岗2"), jd_text=""),
        ],
    )

    def fake_apply(page, job, idx, sess):
        cls = mod.NO_JD_ROUNDTRIP_FAILED if job["encryptJobId"] == "job1" else mod.NO_JD_BOTH_EMPTY
        sess.results.append({"idx": idx, "job": job.get("jobName"), "status": "no_jd", "jd_class": cls})
        return None

    monkeypatch.setattr(mod, "_apply_one_job", fake_apply)
    assert mod._retry_pending_jobs("P", run) is False
    on_disk = mod._load_retry_pending(path)
    assert [e["encryptJobId"] for e in on_disk] == ["job1"], "抖动型保留、双空型移出"
    assert on_disk[0]["attempts"] == 1
    out = capsys.readouterr().out
    assert "保留（详情往返失败＝站点抖动、岗位仍有效" in out
    assert "移出（DOM 与 XHR 双空" in out, "移出理由按证据说话"
    assert "疑已失效" not in out


def test_retry_pending_jobs_unknown_class_keeps_job(tmp_path, monkeypatch, capsys):
    """归因类别读不到（桩结果条目 / 旧台账）时保守保留并在终端照实说「归因未知」——
    既不谎报站点抖动，也不谎报岗位无内容。"""
    session, run = _retry_run(tmp_path)
    path = Path(session.cfg["retry_pending_path"])
    mod._save_retry_pending(path, [mod._retry_snapshot(_timeout_job(), jd_text="")])
    monkeypatch.setattr(
        mod,
        "_apply_one_job",
        lambda p, j, i, s: s.results.append({"idx": i, "job": j.get("jobName"), "status": "no_jd"}),
    )
    mod._retry_pending_jobs("P", run)
    assert [e["encryptJobId"] for e in mod._load_retry_pending(path)] == ["job1"]
    out = capsys.readouterr().out
    assert "保留（归因未知＝证据不足，保守保留" in out and "attempts=1" in out
    assert "站点抖动" not in out, "读不出类别时不许把保留理由说成「已知站点在抖」（拿猜测冒充证据）"


def test_no_jd_keep_phrase_only_claims_what_it_saw():
    """`_no_jd_keep_phrase` 的诚实边界：两类站点证据才配「站点抖动、岗位仍有效」这句话，
    双空与读不出类别都不得复用该措辞（前者压根不会走到保留，后者是证据不足）。"""
    assert mod._no_jd_keep_phrase(mod.NO_JD_ROUNDTRIP_FAILED) == "详情往返失败＝站点抖动、岗位仍有效"
    assert mod._no_jd_keep_phrase(mod.NO_JD_NO_RESPONSE) == "详情超时未回＝站点抖动、岗位仍有效"
    for cls in (mod.NO_JD_BOTH_EMPTY, None, "", "future_class"):
        assert "站点抖动" not in mod._no_jd_keep_phrase(cls)
        assert "保守保留" in mod._no_jd_keep_phrase(cls)


def test_build_report_carries_no_jd_class_distribution():
    """#56 验收 4：分流读数进 report.json——三类恒在（0 也写出来），
    「双空 0 个」与「没这个读数」含义不同，首轮真实投递后读报告即可分流不必翻日志。"""
    results = [
        {"status": "no_jd", "jd_class": mod.NO_JD_ROUNDTRIP_FAILED},
        {"status": "no_jd", "jd_class": mod.NO_JD_ROUNDTRIP_FAILED},
        {"status": "no_jd", "jd_class": mod.NO_JD_NO_RESPONSE},
        {"status": "applied"},
    ]
    rep = mod.build_report(["q"], {"applied": 1, "direct_fallback": 0}, results)
    assert rep["no_jd_classes"] == {
        mod.NO_JD_ROUNDTRIP_FAILED: 2,
        mod.NO_JD_NO_RESPONSE: 1,
        mod.NO_JD_BOTH_EMPTY: 0,
    }
    empty = mod.build_report(["q"], {"applied": 0, "direct_fallback": 0}, [])["no_jd_classes"]
    assert set(empty) == set(mod.NO_JD_CLASSES) and sum(empty.values()) == 0, "没跑过也要能分辨"
    odd = mod.build_report(["q"], {"applied": 0, "direct_fallback": 0}, [{"status": "no_jd"}])["no_jd_classes"]
    assert odd["unknown"] == 1, "无类别的 no_jd 显式进 unknown 桶，不静默丢"


def test_build_report_carries_no_jd_split_readings():
    """#56 验收 4（修复轮 F1）：类别分布旁边要有两条能分流的读数，让读报告的人一眼看出
    「往返失败/挂起」还是「形态漂移」——只有类别时，「超时未回」里混着 DOM 零命中
    （漂移嫌疑）与纯取证缺口两种完全不同的下场。"""
    results = [
        {"status": "no_jd", "jd_class": mod.NO_JD_NO_RESPONSE, "dom_shape": mod.DOM_SHAPE_ZERO_HITS},
        {
            "status": "no_jd",
            "jd_class": mod.NO_JD_NO_RESPONSE,
            "dom_shape": mod.DOM_SHAPE_UNKNOWN,
            "detail_code": {"code": 37, "message": "风控"},
        },
        {"status": "no_jd", "jd_class": mod.NO_JD_BOTH_EMPTY, "dom_shape": mod.DOM_SHAPE_EMPTY_HITS},
        {"status": "applied"},
    ]
    rep = mod.build_report(["q"], {"applied": 1, "direct_fallback": 0}, results)
    assert rep["no_jd_dom_shapes"] == {
        mod.DOM_SHAPE_ZERO_HITS: 1,
        mod.DOM_SHAPE_EMPTY_HITS: 1,
        mod.DOM_SHAPE_TEXT_HITS: 0,
        mod.DOM_SHAPE_UNKNOWN: 1,
    }, "四类恒在（0 也写出来）：与 no_jd_classes 同一口径，「没这个读数」≠「读到 0」"
    assert rep["no_jd_code_nonzero_by_class"] == {
        mod.NO_JD_ROUNDTRIP_FAILED: 0,
        mod.NO_JD_NO_RESPONSE: 1,
        mod.NO_JD_BOTH_EMPTY: 0,
    }, "非 0 读数按类交叉：这一条落在「详情超时未回」桶里（不只是个扁平总数）"
    assert rep["no_jd_classes"][mod.NO_JD_NO_RESPONSE] == 2, "类别分布照旧（读数不参与类别）"

    empty = mod.build_report(["q"], {"applied": 0, "direct_fallback": 0}, [])
    assert set(empty["no_jd_dom_shapes"]) == set(mod.NO_JD_DOM_SHAPES) and sum(empty["no_jd_dom_shapes"].values()) == 0
    assert empty["no_jd_code_nonzero_by_class"] == dict.fromkeys(mod.NO_JD_CLASSES, 0), "没跑过也要能分辨"

    legacy = mod.build_report(["q"], {"applied": 0, "direct_fallback": 0}, [{"status": "no_jd"}])
    assert legacy["no_jd_dom_shapes"][mod.DOM_SHAPE_UNKNOWN] == 1, "旧结果条目读不出形态时进 unknown，不静默丢"
    assert legacy["no_jd_code_nonzero_by_class"] == dict.fromkeys(mod.NO_JD_CLASSES, 0)

    odd = mod.build_report(
        ["q"], {"applied": 0, "direct_fallback": 0}, [{"status": "no_jd", "detail_code": {"code": 1}}]
    )
    assert odd["no_jd_code_nonzero_by_class"] == {**dict.fromkeys(mod.NO_JD_CLASSES, 0), "unknown": 1}, (
        "类别缺失的 no_jd 进 unknown 桶（只在真出现时存在），不静默丢——同 no_jd_classes 口径"
    )

    non_jd = mod.build_report(
        ["q"],
        {"applied": 0, "direct_fallback": 0},
        [{"status": "applied", "dom_shape": mod.DOM_SHAPE_ZERO_HITS, "detail_code": {"code": 37}}],
    )
    assert sum(non_jd["no_jd_dom_shapes"].values()) == 0 and sum(non_jd["no_jd_code_nonzero_by_class"].values()) == 0, (
        "读数只统计 no_jd 条目——把投成功的岗算进漂移嫌疑是造假证据"
    )


def test_no_jd_code_nonzero_readings_cross_tab_by_class(tmp_path, monkeypatch):
    """修复轮 4（B）：非 0 读数与类别的交叉——三种真实容器形态各落哪个桶（只用已落容器，
    不新增协议往返）：① 先 code=0（响应带正文、晚到）后非 0 → 类＝详情超时未回（XHR 侧有正文，
    不许当双空），非 0 记该类的桶；② 只非 0 → 同类同桶（旧扁平总数读不出它与 ① 同类）；
    ③ 只 code=0 空正文 + DOM 探测无正文 → 类＝双空，非 0 桶为 0（这条 no_jd 没有非 0）。"""

    def form1(_page, container, **kw):
        container["data"] = {"code": 0, "zpData": {"jobInfo": {"postDescription": "JD 正文"}}}
        container["nonzero"] = {"code": 37, "message": "风控"}
        return ""

    def form2(_page, container, **kw):
        container["nonzero"] = {"code": 37, "message": "风控"}
        return ""

    def form3(_page, container, **kw):
        container["data"] = {"code": 0, "zpData": {"jobInfo": {"postDescription": ""}}}
        container["empty_outcome"] = {"hit_total": 0, "nonempty_total": 0}
        return ""

    results = []
    for grab in (form1, form2, form3):
        r, _ = _run_apply_on_page(tmp_path, monkeypatch, _ListenerPage(_DETAIL_URL), times=1, grab=grab)
        results.append(r[0])
    assert [r["jd_class"] for r in results] == [
        mod.NO_JD_NO_RESPONSE,
        mod.NO_JD_NO_RESPONSE,
        mod.NO_JD_BOTH_EMPTY,
    ], "①② 有正文/只有非 0 ⇒ 超时未回；③ 空正文双空"
    assert results[0]["detail_code"] == {"code": 37, "message": "风控"} and results[2]["detail_code"] is None
    rep = mod.build_report(["q"], {"applied": 0, "direct_fallback": 0}, results)
    assert rep["no_jd_code_nonzero_by_class"] == {
        mod.NO_JD_ROUNDTRIP_FAILED: 0,
        mod.NO_JD_NO_RESPONSE: 2,
        mod.NO_JD_BOTH_EMPTY: 0,
    }, "①② 的非 0 都落在「详情超时未回」桶；③ 所属的「双空」桶为 0"


def test_done_line_carries_no_jd_classes(tmp_path, monkeypatch, capsys):
    """#56 验收 4（人读层）：收尾 [done] 行带分类分布，口径同 #47 的分桶播报——
    只读数、不发明新展示层；无 no_jd 时该行完全不变（正常会话零噪音）。"""
    monkeypatch.setattr(mod, "edd_wrapup", _EDD_STUB)
    monkeypatch.setattr(mod.g, "resolve_paths", lambda env=None: {"name": "p"})
    run = _report_run(tmp_path)
    run.session.results.extend(
        [
            {"status": "no_jd", "jd_class": mod.NO_JD_ROUNDTRIP_FAILED},
            {"status": "no_jd", "jd_class": mod.NO_JD_ROUNDTRIP_FAILED},
            {"status": "no_jd", "jd_class": mod.NO_JD_NO_RESPONSE, "dom_shape": mod.DOM_SHAPE_ZERO_HITS},
            {
                "status": "no_jd",
                "jd_class": mod.NO_JD_NO_RESPONSE,
                "dom_shape": mod.DOM_SHAPE_UNKNOWN,
                "detail_code": {"code": 37, "message": "风控"},
            },
        ]
    )
    mod._write_session_report(run)
    out = capsys.readouterr().out
    assert "no_jd 归因 详情往返失败×2 / 详情超时未回×2" in out
    assert "DOM 与 XHR 双空" not in out, "0 的类不进终端行（报告里恒在）"
    assert (
        "no_jd 分流" in out and "DOM 零命中（形态漂移或已离岗弹回）×1" in out and "非 0 返回码（详情超时未回×1）" in out
    ), (
        "修复轮 F1：分流读数也要进收尾行（无探测证据的那 3 条如实落 DOM 探测无证据，不猜成零命中）；"
        "修复轮 4（B/E）：非 0 读数按类读出，零命中读数的措辞两假说并列（不再单指漂移）"
    )
    data = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert data["no_jd_classes"][mod.NO_JD_BOTH_EMPTY] == 0
    assert data["no_jd_dom_shapes"][mod.DOM_SHAPE_ZERO_HITS] == 1
    assert data["no_jd_code_nonzero_by_class"][mod.NO_JD_NO_RESPONSE] == 1

    quiet = _report_run(tmp_path, state={"applied": 3, "direct_fallback": 0, "keyword_skips": []})
    quiet.session.results.append({"status": "applied"})
    mod._write_session_report(quiet)
    out = capsys.readouterr().out
    assert "no_jd 归因" not in out and "no_jd 分流" not in out, "零噪音"

    only_gap = _report_run(tmp_path, state={"applied": 3, "direct_fallback": 0, "keyword_skips": []})
    only_gap.session.results.append({"status": "no_jd", "jd_class": mod.NO_JD_NO_RESPONSE})
    mod._write_session_report(only_gap)
    out = capsys.readouterr().out
    assert "no_jd 归因 详情超时未回×1" in out, (
        "没有探测证据时如实念「DOM 探测无证据×1」——不猜成零命中，也不静默省略这一侧"
    )
    assert "DOM 探测无证据×1" in out and "非 0 返回码" not in out, "0 的那条不进这一行（报告里恒在）"


def test_capture_xhr_forensics_failure_never_reaches_dispatcher(monkeypatch):
    """取证通道的通道也可能会坏（app.log 写入侧抛异常 / 容器不可写）：
    `_observe_xhr_failure` 外层必须把它吞干净——handler 跑在 dispatcher 的 EventGreenlet 里，
    任何冒泡都会改变协议链路行为（本票 A 段口径）。这条兜底没测过就等于没写。"""
    container = {"data": None}

    def boom(event, **fields):
        raise RuntimeError("log handler exploded")

    monkeypatch.setattr(mod._logging, "log_event", boom)
    h = mod.capture_xhr(_SendPage(_DETAIL_URL), _DETAIL_FRAG, container)
    h(_BoomResp())  # 不抛出即通过
    assert container["errors"], "容器记录先落，事件写失败不牵连它"


def test_log_detail_empty_swallow_weird_flow(monkeypatch, tmp_path):
    """同上，`_log_detail_empty` 的外层兜底：岗位字段形态诡异（job 不是字典）时静默返回，
    不得把 no_jd 的取证变成新的 page_error。"""
    _log_events(monkeypatch)
    flow = mod._JobFlow(page=_SendPage(_DETAIL_URL), job={"jobName", "岗位"}, idx=1, ctx=_a3_ctx(tmp_path))
    mod._log_detail_empty(flow.page, flow, mod.NO_JD_NO_RESPONSE)  # set 没有 .get → 走外层吞掉


# --- #56 B1：楔死岗跳岗台账（wedge_skipped.json，默认开）---
#
# 票面 B 段现状：楔死 → 整树杀 → 直接重启，而**没有任何东西记住这场死在哪个岗**——同一个
# 「企贤 solidity开发 50-80K」在 5 场会话里分别以第 11/11/3/1/2 岗位点楔死，每场白交 240s
# + 一次浏览器重启。B1 的链条＝「下一场开跑回灌 A2 的击杀取证 → 呈报 → 三个消费点跳过」，
# 验收 3 要求全链有测试。
#
# 夹具为什么从「一份 tmp 会话目录里的 WATCHDOG_KILLED.json」起步：楔死的进程被进程外看门狗
# SIGKILL，进程内跑不了任何 Python（票面机制口径 §2），**写不了台账的是它自己**——台账只能由
# 下一场从上一场的击杀取证回灌。所以每条入账用例都先造取证、再跑开跑函数。

_WEDGE_EID = "e9527c6f0"
_WEDGE_PRIOR = "20260928-012512"
# #58 T2 给回灌加了「已投台账」入参：#56 既有用例的前提都是「这台机器还没有已投台账」，
# 显式写出来（而不是偷偷给个默认值），守卫不参与那些用例要钉的归因门槛。
_NO_APPLIED: set = set()


def _wedge_marker(base, name=_WEDGE_PRIOR, **over):
    """在 `base/<name>/` 造一份 A2 形态的击杀取证（默认＝岗位内楔死、带 eid）。"""
    d = Path(base) / name
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "killed_at": "2026-09-28T01:51:49",
        "stall_seconds": 240.0,
        "stale_seconds": 240,
        "last_label": "job:2",
        "last_eid": _WEDGE_EID,
        "last_job": "solidity开发 50-80K",
        "last_brand": "企贤",
        "in_job": True,
    }
    payload.update(over)
    (d / "WATCHDOG_KILLED.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return d


def _wedge_entry(eid=_WEDGE_EID, count=1, session=_WEDGE_PRIOR):
    """台账条目（消费侧用例直接构造，不经回灌，免得每条用例都要重铺取证）。"""
    return {
        "encryptJobId": eid,
        "jobName": "solidity开发 50-80K",
        "brandName": "企贤",
        "session": session,
        "sessions": [session] * count,
        "wedge_count": count,
        "wedged_at": "2026-09-28T01:51:49",
        "last_label": "job:2",
        "stale_seconds": 240,
    }


def _wedge_job(eid=_WEDGE_EID, name="solidity开发 50-80K", brand="企贤"):
    return {"encryptJobId": eid, "jobName": name, "brandName": brand}


def _wedge_session(tmp_path, entries=(), enabled=True, state_extra=None):
    """带台账工作副本的会话替身：cfg 两个键与 `_init_live_session` 的接法同形。
    走 `_pre_job_stop` 的用例另需 `_wedge_no_quiet` 桩掉静默时段，免得结果随时钟漂移。"""
    state = {"applied": 0, "direct_fallback": 0, "keyword_skips": [], "dry_run": False}
    state.update(state_extra or {})
    session, calls = _fake_session(tmp_path, state=state)
    session.cfg.update(
        {
            "wedge_skipped_path": str(tmp_path / "wedge_skipped.json"),
            "wedge_skip_enabled": enabled,
            "retry_pending_path": str(tmp_path / "retry_pending.json"),  # #55 断点台账（消费点 3 用）
        }
    )
    session.state["wedge_eids"] = {e["encryptJobId"]: e for e in entries}
    session.state["wedge_ledger"] = {"enabled": enabled, "total": len(entries), "backfilled": 0, "rejected": 0}
    return session, calls


def _wedge_no_quiet(monkeypatch):
    monkeypatch.setattr(mod, "is_quiet_hour", lambda hour, quiet: False)


def _wedge_run(session, excluded=()):
    return SimpleNamespace(session=session, excluded=list(excluded), limit=10, collected=[], applied_ids=set())


# ① 回灌写入


def test_backfill_books_attributed_marker(tmp_path):
    """#56 验收 3（B1① 第一环）：带 `last_eid` + `in_job` 的取证 → 台账里出现这个岗，
    条目带票面点名的可追溯字段（eid / 岗位名 / 品牌 / 来源场 / wedged_at / last_label / stale）。
    `last_label` 是 C 段判栈要的「死在哪个节拍上」，不可省。"""
    base = tmp_path / "live_session"
    _wedge_marker(base)
    ledger = tmp_path / "wedge_skipped.json"
    stats = mod._backfill_wedge_ledger(base, "20260929-090000", ledger, _NO_APPLIED)
    entries = mod._load_wedge_skipped(ledger)
    assert stats["booked"] == 1 and stats["rejected"] == 0 and stats["written"] is True
    assert len(entries) == 1
    e = entries[0]
    assert e["encryptJobId"] == _WEDGE_EID and e["jobName"] == "solidity开发 50-80K" and e["brandName"] == "企贤"
    assert e["session"] == _WEDGE_PRIOR and e["wedged_at"] == "2026-09-28T01:51:49"
    assert e["last_label"] == "job:2" and e["stale_seconds"] == 240
    assert e["wedge_count"] == 1, "一次楔死即「第 1 次」"


@pytest.mark.parametrize(
    "over",
    [
        {"in_job": False},
        {"in_job": None},
        {"in_job": "true"},
        {"last_eid": ""},
        {"last_eid": "   "},
        {"last_eid": None},
    ],
)
def test_backfill_rejects_everything_that_is_not_attributable(tmp_path, over):
    """B1① 归因门槛（严格，`in_job is True` **且** `last_eid` 非空）：两岗之间/岗位外被杀、
    eid 缺失或空白一律不入账。为什么宁窄勿宽——错归因的代价是**把一个正常岗位永久跳掉**
    （漏投一个潜在达标岗），比「少跳一个岗」重得多；真值判断用 `is True`，字符串 "true"
    也不算（取证字段是布尔，读不出布尔就是读不出）。"""
    base = tmp_path / "live_session"
    _wedge_marker(base, **over)
    ledger = tmp_path / "wedge_skipped.json"
    stats = mod._backfill_wedge_ledger(base, "20260929-090000", ledger, _NO_APPLIED)
    assert stats["rejected"] == 1 and stats["booked"] == 0
    assert mod._load_wedge_skipped(ledger) == [] and stats["written"] is False


def test_backfill_ignores_pre_a2_markers(tmp_path):
    """A2 之前的旧格式取证（只有 killed_at / stale / last_label）不入账：它答不出死在哪个岗，
    而 5 场楔死里 3 场是这种形态——宁可少跳，不可猜岗。"""
    base = tmp_path / "live_session"
    d = base / "20260927-213017"
    d.mkdir(parents=True)
    (d / "WATCHDOG_KILLED.json").write_text(
        json.dumps({"killed_at": "2026-09-27T21:35:32", "stale_seconds": 240, "last_label": "job:1"}), encoding="utf-8"
    )
    ledger = tmp_path / "wedge_skipped.json"
    stats = mod._backfill_wedge_ledger(base, "20260929-090000", ledger, _NO_APPLIED)
    assert stats["scanned"] == 1 and stats["rejected"] == 1 and stats["booked"] == 0
    assert not ledger.exists()


def test_unattributed_markers_still_get_surfaced(tmp_path, capsys):
    """本切片不许削弱 A2 的呈报面：不入账的那几场照样被 `_surface_prior_wedge` 念出来
    （「刚死了一场」与「从此不碰哪些岗」是两条信息，后者筛掉前者也还在）。"""
    base = tmp_path / "live_session"
    _wedge_marker(base, in_job=False)
    assert (
        mod._backfill_wedge_ledger(base, "20260929-090000", tmp_path / "wedge_skipped.json", _NO_APPLIED)["booked"] == 0
    )
    mod._surface_prior_wedge(base, "20260929-090000")
    out = capsys.readouterr().out
    assert "被看门狗强杀" in out and _WEDGE_PRIOR in out and "job:2" in out


def test_backfill_is_idempotent_when_rescanning_the_same_session(tmp_path):
    """B1① 幂等：取证目录每场都会被重扫（`sweep_expired_data` 只删 shots/，会话目录长期留着），
    同一场 marker 第二次扫到**不得重复入账**——幂等键就是来源场名。"""
    base = tmp_path / "live_session"
    _wedge_marker(base)
    ledger = tmp_path / "wedge_skipped.json"
    mod._backfill_wedge_ledger(base, "20260929-090000", ledger, _NO_APPLIED)
    again = mod._backfill_wedge_ledger(base, "20260930-090000", ledger, _NO_APPLIED)
    assert again["booked"] == 0 and again["duplicates"] == 1 and again["written"] is False
    entries = mod._load_wedge_skipped(ledger)
    assert len(entries) == 1 and entries[0]["wedge_count"] == 1 and entries[0]["sessions"] == [_WEDGE_PRIOR]


def test_backfill_merges_same_job_across_sessions(tmp_path):
    """B1① 合并口径：同一 eid 多场楔死合成**一条**并累计次数（票面实证＝同一岗连续 4-5 场）。
    合并后必须能回答两件事：「这个岗第几次楔的」= wedge_count，「最近一场是哪场」= session。"""
    base = tmp_path / "live_session"
    _wedge_marker(base, "20260927-213017", last_label="job:1", killed_at="2026-09-27T21:35:32")
    _wedge_marker(base, _WEDGE_PRIOR, last_label="job:2", killed_at="2026-09-28T01:51:49")
    _wedge_marker(base, "20260928-090000", last_label="job:3", killed_at="2026-09-28T09:30:00")
    ledger = tmp_path / "wedge_skipped.json"
    stats = mod._backfill_wedge_ledger(base, "20260929-000000", ledger, _NO_APPLIED)
    entries = mod._load_wedge_skipped(ledger)
    assert stats["added"] == 1 and stats["merged"] == 2 and len(entries) == 1
    e = entries[0]
    assert e["wedge_count"] == 3 and e["session"] == "20260928-090000", "最近一场取场名最大者（场名按时间升序）"
    assert e["last_label"] == "job:3" and e["wedged_at"] == "2026-09-28T09:30:00"
    assert e["sessions"] == ["20260927-213017", _WEDGE_PRIOR, "20260928-090000"]


def test_backfill_only_scans_sessions_earlier_than_this_one(tmp_path):
    """B1① 扫描边界：只认**早于本场**的会话目录（本场目录里的取证=本进程还没死；
    晚于本场的场名要么不存在要么是历史残留，都不入账）；非 ts 形态的目录一律不碰。"""
    base = tmp_path / "live_session"
    _wedge_marker(base, "20260929-090000")  # = 本场
    _wedge_marker(base, "20260930-090000", last_eid="e-future")  # 晚于本场
    junk = base / "not-a-session"
    junk.mkdir()
    (junk / "WATCHDOG_KILLED.json").write_text("{}", encoding="utf-8")
    stats = mod._backfill_wedge_ledger(base, "20260929-090000", tmp_path / "wedge_skipped.json", _NO_APPLIED)
    assert stats["scanned"] == 0 and stats["booked"] == 0


def test_backfill_survives_corrupt_marker_and_corrupt_ledger(tmp_path, capsys):
    """B1① 自愈口径（照 `_load_retry_pending`：坏形态按空台账起步并 warn，不裸抛）：
    坏取证计数后跳过，坏台账不阻断开跑，且下一次写入把台账修回来。"""
    base = tmp_path / "live_session"
    bad = _wedge_marker(base, "20260926-100000")
    (bad / "WATCHDOG_KILLED.json").write_text("{not json", encoding="utf-8")
    _wedge_marker(base, "20260926-110000", last_label="job:5")
    ledger = tmp_path / "wedge_skipped.json"
    ledger.write_text('{"不是列表": 1}', encoding="utf-8")
    stats = mod._backfill_wedge_ledger(base, "20260929-090000", ledger, _NO_APPLIED)
    assert stats["unreadable"] == 1 and stats["scanned"] == 2 and stats["booked"] == 1
    out = capsys.readouterr().out
    assert "[warn]" in out, "坏台账要念出来，自愈不静默"
    assert [e["encryptJobId"] for e in mod._load_wedge_skipped(ledger)] == [_WEDGE_EID]


def test_backfill_tolerates_missing_session_base(tmp_path, capsys):
    """B1① 首跑（尚无任何会话目录）与目录不可读都只是「没有可入账岗位」，绝不抛。"""
    stats = mod._backfill_wedge_ledger(
        tmp_path / "nope", "20260929-090000", tmp_path / "wedge_skipped.json", _NO_APPLIED
    )
    assert stats["scanned"] == 0 and stats["booked"] == 0
    assert not (tmp_path / "wedge_skipped.json").exists()


def test_backfill_writes_atomically_and_keeps_unknown_entries(tmp_path):
    """B1① 落盘形态：`.tmp` + os.replace（`write_json_atomic`）不留半文件；
    台账里来源不明的既有条目**照原样保留**（本切片无权判定并删除别人的条目）。"""
    base = tmp_path / "live_session"
    _wedge_marker(base)
    ledger = tmp_path / "wedge_skipped.json"
    legacy = {"encryptJobId": "e-legacy", "jobName": "历史条目", "session": "20260101-000000"}
    mod._save_wedge_skipped(ledger, [legacy])
    mod._backfill_wedge_ledger(base, "20260929-090000", ledger, _NO_APPLIED)
    entries = mod._load_wedge_skipped(ledger)
    assert {e["encryptJobId"] for e in entries} == {"e-legacy", _WEDGE_EID}
    assert [e for e in entries if e["encryptJobId"] == "e-legacy"][0] == legacy
    assert not Path(str(ledger) + ".tmp").exists()


# ④ 开关


@pytest.mark.parametrize(
    "raw, expected",
    [(None, True), ("", True), ("on", True), ("1", True), ("YES", True), ("ofs", True), ("false", True)],
)
def test_wedge_skip_enabled_default_on(monkeypatch, raw, expected):
    """B1④ 默认开（用户 2026-09-28 拍板）：未设置 / 空串 / 不认识的值都按开。
    方向论证见 `_wedge_skip_enabled` docstring——写错值这一侧的代价**当场可见**
    （开与关都呈报台账，不认识的原始值还额外打一行提示），而按关读错则回到本票要治的现状。"""
    if raw is None:
        monkeypatch.delenv("LIVE_WEDGE_SKIP", raising=False)
    else:
        monkeypatch.setenv("LIVE_WEDGE_SKIP", raw)
    assert mod._wedge_skip_enabled() is expected


@pytest.mark.parametrize("raw", ["0", "off", "OFF", "  off  "])
def test_wedge_skip_enabled_only_recognised_values_close(monkeypatch, raw):
    """关只认 0 / off（去空白、大小写不敏感），口径照仓内既有开关（LIVE_RECORD / A4 的 _wedge_dump_enabled）。"""
    monkeypatch.setenv("LIVE_WEDGE_SKIP", raw)
    assert mod._wedge_skip_enabled() is False


def test_wedge_skip_enabled_announces_unrecognised_value(monkeypatch, capsys):
    """typo 不当哑事：读到不认识的原始值时明示「按默认开处理，关闭只认 0/off」。"""
    monkeypatch.setenv("LIVE_WEDGE_SKIP", "false")
    assert mod._wedge_skip_enabled() is True
    out = capsys.readouterr().out
    assert "LIVE_WEDGE_SKIP" in out and "按默认「开」" in out and "0/off" in out
    capsys.readouterr()
    monkeypatch.setenv("LIVE_WEDGE_SKIP", "on")
    assert mod._wedge_skip_enabled() is True
    assert "按默认「开」" not in capsys.readouterr().out, "可识别值不该提示"


# ② 呈报 + 载入工作副本（开跑三步的接线面）


def test_prepare_wedge_ledger_backfills_then_loads_then_reports(tmp_path, capsys):
    """#56 验收 3（B1②）：开跑把台账如实念出来——岗位名 + eid + 第几次楔 + 最近哪场 + 死在哪个节拍，
    并念清**代价**（可能漏投一个达标岗）与**放行办法**（LIVE_WEDGE_SKIP=off）。"""
    session, _ = _wedge_session(tmp_path)
    base = tmp_path / "live_session"
    _wedge_marker(base)
    mod._prepare_wedge_ledger(session, base, "20260929-090000", _NO_APPLIED)
    out = capsys.readouterr().out
    assert _WEDGE_EID in out and "solidity开发 50-80K" in out and _WEDGE_PRIOR in out
    assert "第 1 次" in out and "job:2" in out
    assert "漏投" in out and "LIVE_WEDGE_SKIP=off" in out, "代价与放行办法必须一起念"
    assert session.state["wedge_eids"][_WEDGE_EID]["wedge_count"] == 1, "消费点查的是工作副本，不是磁盘"
    ledger_stats = session.state["wedge_ledger"]
    assert ledger_stats["enabled"] is True and ledger_stats["total"] == 1 and ledger_stats["backfilled"] == 1


def test_prepare_wedge_ledger_dry_run_reports_but_never_writes(tmp_path, capsys):
    """B1① #55 验收 4 同款约束：dry-run 会话**只呈报、明示「演练会话不动台账」**——
    既不回填历史取证也不写文件（演练场不该产生永久性的「不投」决定）。"""
    session, _ = _wedge_session(tmp_path, state_extra={"dry_run": True})
    base = tmp_path / "live_session"
    _wedge_marker(base)
    mod._prepare_wedge_ledger(session, base, "20260929-090000", _NO_APPLIED)
    out = capsys.readouterr().out
    assert "演练会话" in out and "不动台账" in out
    assert not Path(session.cfg["wedge_skipped_path"]).exists(), "dry-run 不得凭空造台账文件"
    assert session.state["wedge_ledger"]["backfilled"] == 0


def test_prepare_wedge_ledger_off_reports_but_skips_nothing_and_writes_nothing(tmp_path, capsys):
    """B1④ 关闭侧：仍呈报（明示「本场不跳过」），但**不回灌写入**——关掉止损机制不该还偷偷写台账。
    预先写一条台账（sessions 已含 _WEDGE_PRIOR）：若 off 状态仍在回灌，另一场取证会把
    wedge_count 并进 2；断言它仍是 1，就同时钉住了「呈报读的是既有台账」与「不写入」。"""
    session, _ = _wedge_session(tmp_path, enabled=False)
    ledger = Path(session.cfg["wedge_skipped_path"])
    mod._save_wedge_skipped(ledger, [_wedge_entry()])
    base = tmp_path / "live_session"
    _wedge_marker(base, "20260927-213017", last_label="job:1")
    mod._prepare_wedge_ledger(session, base, "20260929-090000", _NO_APPLIED)
    out = capsys.readouterr().out
    assert "本场不跳过" in out and _WEDGE_EID in out
    assert mod._load_wedge_skipped(ledger)[0]["wedge_count"] == 1, "off 时不得回灌"
    assert session.state["wedge_ledger"]["enabled"] is False


def test_prepare_wedge_ledger_survives_unwritable_ledger(tmp_path, capsys):
    """台账写不进去（盘满/权限/路径被占）不该终结开跑：warn 一行后继续，呈报与消费按读得到的
    形态走（这里父路径是一个普通文件 → mkdir 必炸）。"""
    session, _ = _wedge_session(tmp_path)
    base = tmp_path / "live_session"
    _wedge_marker(base)
    blocker = tmp_path / "blocked"
    blocker.write_text("我不是目录", encoding="utf-8")
    session.cfg["wedge_skipped_path"] = str(blocker / "wedge_skipped.json")
    mod._prepare_wedge_ledger(session, base, "20260929-090000", _NO_APPLIED)
    out = capsys.readouterr().out
    assert "[warn]" in out, "写失败必须可见（不然台账「没长」会被读成「没有楔死岗」）"
    assert session.state["wedge_ledger"]["written"] is False
    assert session.state["wedge_eids"] == {}, "写没进去就不该假装台账里有它"


# ③ 三个消费点


def test_apply_fresh_jobs_skips_wedged_job(tmp_path, monkeypatch, capsys):
    """B1③ 消费点 1（搜索流 + 推荐流共用的内层循环）：台账内的 eid 不进 apply、不占 idx、
    记 `wedge_skipped` 结果条目（带第几次楔，可追溯）、走 `pacer.wait_skip()`；
    形状与导航语义照 `is_excluded_company` 分支——**不做 safe_go_back**（岗位还没进详情，
    页面仍在列表上，回退等于把列表翻页）。"""
    session, calls = _wedge_session(tmp_path, entries=[_wedge_entry(count=4)])
    _wedge_no_quiet(monkeypatch)
    backs = []
    monkeypatch.setattr(mod, "safe_go_back", lambda page: backs.append(page))
    applied = []
    monkeypatch.setattr(mod, "_apply_one_job", lambda page, job, idx, s: applied.append(idx) or None)
    stop, idx = mod._apply_fresh_jobs(
        "P", _wedge_run(session), [_wedge_job(), _wedge_job(eid="e-normal", name="正常岗", brand="别家")], 0
    )
    assert applied == [1], "楔死岗不得进投递，同轮下一个岗照常"
    assert idx == 1 and stop is False
    assert [r["status"] for r in session.results] == ["wedge_skipped"]
    row = session.results[0]
    assert row["encryptJobId"] == _WEDGE_EID and row["wedge_count"] == 4 and row["job"] == "solidity开发 50-80K"
    assert calls["skip"] == 1, "跳过也走节奏（照 excluded 分支）"
    assert backs == [], "不得回退列表（照 excluded 分支的导航语义）"
    assert "楔死台账" in capsys.readouterr().out, "终端一行讲清为什么跳"
    assert session.state["wedge_skips"][0]["stage"] == "搜索/推荐流"


def test_query_rounds_and_recommend_round_both_skip_wedged_job(tmp_path, monkeypatch):
    """漏一处消费点就是「本该跳过的岗又被投了一次」——正是本票要治的重复消耗。
    本用例从**两条入口**（关键词轮 / 推荐轮）各跑一次真接线，而不是只测 `_apply_fresh_jobs`。"""
    session, _ = _wedge_session(tmp_path, entries=[_wedge_entry()])
    calls = _run_rounds(session, monkeypatch, ["a"], {"a": [_wedge_job()]}, "next")
    assert calls["applied"] == [] and [r["status"] for r in session.results] == ["wedge_skipped"]

    session2, _ = _wedge_session(tmp_path, entries=[_wedge_entry()])
    _wedge_no_quiet(monkeypatch)
    monkeypatch.setattr(mod, "recommend_list", lambda page, limit=None, target=0, flags=None: [_wedge_job()])
    monkeypatch.setattr(mod, "collect_new_jds", lambda listing, collected, _ids: list(listing))
    monkeypatch.setattr(mod, "_browse_list", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_remember_list_url", lambda page: None)
    applied = []
    monkeypatch.setattr(mod, "_apply_one_job", lambda page, job, idx, s: applied.append(job["encryptJobId"]) or None)
    stop, idx = mod._recommend_round("P", _wedge_run(session2), 0)
    assert applied == [] and idx == 0 and [r["status"] for r in session2.results] == ["wedge_skipped"]
    assert stop is False


def test_browse_noise_excludes_wedged_job_so_it_cannot_be_clicked_open(tmp_path, monkeypatch):
    """B1 消费面的补漏（票面没点名，但同一张票的逻辑要求它）：`_browse_list` 的噪声点是
    **真的 coordinate_click 进详情页**，而 5/5 场楔死就落在「岗头→详情」那一段——投递路径
    跳过的岗若还被噪声点开，止损就被绕过了，同一场会话照样白交 240s。
    从推荐轮真接线跑（关键词轮共用同一个 `_noise_exclusions`，由下一条用例锁接线）。"""
    session, _ = _wedge_session(tmp_path, entries=[_wedge_entry()])
    _wedge_no_quiet(monkeypatch)
    seen = {}
    monkeypatch.setattr(
        mod, "recommend_list", lambda page, limit=None, target=0, flags=None: [_wedge_job(eid="e-x", name="别的岗")]
    )
    monkeypatch.setattr(mod, "collect_new_jds", lambda listing, collected, _ids: list(listing))
    monkeypatch.setattr(mod, "_remember_list_url", lambda page: None)
    monkeypatch.setattr(
        mod,
        "_browse_list",
        lambda page, s, label, exclude_ids=frozenset(): seen.update({"exclude": set(exclude_ids)}),
    )
    monkeypatch.setattr(mod, "_apply_one_job", lambda *a, **k: None)
    mod._recommend_round("P", _wedge_run(session), 0)
    assert _WEDGE_EID in seen["exclude"], "台账内的岗不得被噪声点开"
    assert "e-x" in seen["exclude"], "本轮候选池的排除语义不得丢（#44 既有约束）"


def test_noise_exclusions_do_not_exclude_when_switch_off(tmp_path):
    """off 时噪声排除也不额外加东西——放行办法既放行投递也放行噪声，不留半个「跳过」状态。"""
    session, _ = _wedge_session(tmp_path, entries=[_wedge_entry()], enabled=False)
    session.state["wedge_eids"] = {_WEDGE_EID: _wedge_entry()}
    assert mod._noise_exclusions(session, [_wedge_job(eid="e-other")]) == {"e-other"}
    on = _wedge_session(tmp_path, entries=[_wedge_entry()])[0]
    assert mod._noise_exclusions(on, [_wedge_job(eid="e-other")]) == {"e-other", _WEDGE_EID}


def test_wedge_entry_for_and_noise_exclusions_agree_on_nonstring_eid(tmp_path):
    """#56 修复轮 F3：台账命中判定过去有**两份不等价**的实现——`_wedge_entry_for` 用
    `eids.get(str(eid))`（按字符串键查），`_noise_exclusions` 用 `ids |= set(eids)`
    （按 dict 的**原始键**取并集）。同一个岗于是会在投递路径被跳过、在噪声路径照样被点开
    （止损被绕过）；且 `_browse_list` 的读者拿集合元素去做 `e in href` 子串判断，
    混进非字符串直接 TypeError、被那层的 try/except 吞成「这条列表没点噪声」。
    现在两处都直调 `_wedge_ledger_map` 这一条共享口径（键恒按 str 归一）。
    修复轮 4（C）删掉 `_wedge_ledger_hit` 这个生产零调用的中间层（它只回 bool，
    `_wedge_entry_for` 要返回条目——注释却称「两处都走这条链」，是 F2 那类同型错）。"""
    numeric = 9007199254740993  # 非字符串 eid（JSON 里岗位 id 是数字时的真实形态）
    session, _ = _wedge_session(tmp_path, entries=[_wedge_entry(eid=numeric)])
    entry = mod._wedge_entry_for(session, numeric)
    assert entry is not None, "投递路径：台账命中 ⇒ 跳过"
    assert mod._wedge_entry_for(session, str(numeric)) is not None, "字符串同值也算命中（同一口径）"
    excluded = mod._noise_exclusions(session, [_wedge_job(eid="e-other")])
    assert str(numeric) in excluded, "噪声路径：同一个岗也必须被排除（两份判定不得分叉）"
    assert all(isinstance(i, str) for i in excluded), "排除集恒为字符串——`e in href` 才不会炸"
    assert mod._noise_exclusions(session, [_wedge_job(eid=None)]) == {str(numeric)}, "空 eid 不进集合"
    assert mod._noise_exclusions(session, [_wedge_job(eid="")]) == {str(numeric)}

    off, _ = _wedge_session(tmp_path, entries=[_wedge_entry(eid=numeric)], enabled=False)
    assert mod._wedge_entry_for(off, numeric) is None
    assert mod._noise_exclusions(off, [_wedge_job(eid="e-other")]) == {"e-other"}, "off 时两处一起放行"
    broken = _wedge_session(tmp_path)[0]
    broken.state["wedge_eids"] = "不是字典"
    assert mod._wedge_entry_for(broken, numeric) is None
    assert mod._noise_exclusions(broken, [_wedge_job(eid="e-other")]) == {"e-other"}


def test_wedge_ledger_chain_has_no_dead_intermediate_helper():
    """修复轮 4（C）：`_wedge_ledger_hit` 只被用例调用、docstring 却称「`_wedge_entry_for`
    与 `_noise_exclusions` 都走这条链」——生产不走的函数 + 声称在走的注释，正是 F2 那类
    「注释大于代码」。删除是改动更小的选项（另一选项要求返回条目的 `_wedge_entry_for`
    经只回 bool 的它绕一圈，等于对同一 dict 查两遍、只有新增失败面）；本用例锁「不得复活」。"""
    src = Path(mod.__file__).resolve().read_text(encoding="utf-8")
    assert "_wedge_ledger_hit" not in src, "生产零调用的中间层不得复活——两处消费点直调 `_wedge_ledger_map`（唯一口径）"


def test_both_browse_sites_route_through_noise_exclusions():
    """接线（源断言，同 #55/#56 既有的「单一实现」断言先例）：两条列表浏览路径
    （关键词轮 / 推荐轮）都必须经 `_noise_exclusions`——只改一处就是「一边跳了另一边还点」。"""
    src = Path(mod.__file__).resolve().read_text(encoding="utf-8")
    assert src.count("exclude_ids=_noise_exclusions(session, fresh)") == 2
    assert 'exclude_ids={j.get("encryptJobId", "") for j in fresh}' not in src


def test_retry_session_jobs_skips_wedged_job_without_duplicate_row(tmp_path, monkeypatch):
    """B1③ 消费点 2（会话内补投轮）：超时岗若已在台账里就不再直跳（那正是白交 240s 的动作）。
    与 excluded 分支同款：**不记第二条结果条目、不 wait_skip**——同一岗本场已有一条
    agent_timeout 结果条目，再记一条会让 #47 分桶把同一个岗算两次，而这里连导航都没发生。"""
    session, calls = _wedge_session(tmp_path, entries=[_wedge_entry()])
    _wedge_no_quiet(monkeypatch)
    session.state["session_retries"] = [{"job": _wedge_job(), "request_idx": 3}]
    session.state["last_idx"] = 3
    monkeypatch.setattr(mod, "_apply_one_job", lambda *a, **k: pytest.fail("楔死台账内的岗不得补投"))
    mod._retry_session_jobs("P", _wedge_run(session))
    assert session.results == [] and session.state.get("retry_pending", []) == []
    assert calls["skip"] == 0 and len(session.state["wedge_skips"]) == 1
    assert session.state["last_idx"] == 3, "跳过的岗不占补投序号（不进 apply 就不该烧 idx）"


def test_retry_pending_jobs_skips_wedged_entry_and_evicts_it_loudly(tmp_path, monkeypatch, capsys):
    """B1③ 消费点 3（跨会话台账轮）：楔死台账命中 → 从 retry_pending.json **有证据、有播报**地移出
    （票面 :496「静默移除」批判的反面）+ 记结果条目。"""
    session, calls = _wedge_session(tmp_path, entries=[_wedge_entry()])
    _wedge_no_quiet(monkeypatch)
    path = Path(session.cfg["retry_pending_path"])
    mod._save_retry_pending(
        path,
        [
            mod._retry_snapshot(_wedge_job(), jd_text=""),
            mod._retry_snapshot(_wedge_job(eid="e-normal", name="正常岗", brand="别家"), jd_text=""),
        ],
    )

    applied = []

    def fake_apply(page, job, idx, s):
        applied.append(job["encryptJobId"])
        s.results.append({"idx": idx, "job": job.get("jobName"), "status": "applied"})
        return None

    monkeypatch.setattr(mod, "_apply_one_job", fake_apply)
    assert mod._retry_pending_jobs("P", _wedge_run(session)) is False
    assert applied == ["e-normal"], "楔死岗不进补投链路，同轮下一个条目照常"
    assert mod._load_retry_pending(path) == [], "两个条目都移出（一个楔死、一个已投出）"
    out = capsys.readouterr().out
    assert "移出" in out and "楔死台账" in out and "solidity开发 50-80K" in out
    assert "疑已失效" not in out, "别把 A5 的口径改回去"
    assert [r["status"] for r in session.results] == ["wedge_skipped", "applied"]
    row = session.results[0]
    assert row["encryptJobId"] == _WEDGE_EID and row["wedge_count"] == 1 and row["stage"] == "跨会话台账轮"
    assert calls["skip"] == 0, "台账轮只是删条目，没有导航——不白等节奏"


@pytest.mark.parametrize("stage", ["fresh", "session_retry", "pending"])
def test_wedge_skip_off_leaves_every_consumer_applying(tmp_path, monkeypatch, stage):
    """B1④ 放行办法真能放行：开关为 off 时**三个消费点都照常投**，
    即使工作副本里已经躺着这个岗（呈报仍在，动作没有）。"""
    session, _ = _wedge_session(tmp_path, entries=[_wedge_entry()], enabled=False)
    _wedge_no_quiet(monkeypatch)
    applied = []

    def fake_apply(page, job, idx, s):
        applied.append(job.get("encryptJobId"))
        s.results.append({"idx": idx, "job": job.get("jobName"), "status": "applied"})
        return None

    monkeypatch.setattr(mod, "_apply_one_job", fake_apply)
    run = _wedge_run(session)
    if stage == "fresh":
        mod._apply_fresh_jobs("P", run, [_wedge_job()], 0)
    elif stage == "session_retry":
        session.state["session_retries"] = [{"job": _wedge_job(), "request_idx": 1}]
        mod._retry_session_jobs("P", run)
    else:
        path = Path(session.cfg["retry_pending_path"])
        mod._save_retry_pending(path, [mod._retry_snapshot(_wedge_job(), jd_text="")])
        mod._retry_pending_jobs("P", run)
    assert applied == [_WEDGE_EID], f"{stage} 处 off 时不得跳过"
    assert session.state.get("wedge_skips", []) == []
    if stage == "pending":
        # off 时这条目走的是**正常补投**出口（本次桩成 applied → 按「已投出」移出），
        # 不是因为楔死台账被移出——两条移出路径不许混。
        assert [e["encryptJobId"] for e in mod._load_retry_pending(path)] == []
        assert [r["status"] for r in session.results] == ["applied"], "不得出现 wedge_skipped 结果条目"


# 台账分流 / 三桶 / 读数


def test_apply_retry_outcome_wedge_skipped_evicts_without_attempt_cost():
    """B1③ `apply_retry_outcome` 认识 wedge_skipped → 移出（一个从此不投的岗没理由留在补投队列里），
    attempts 不动。同时锁住 A5 刚立的 no_jd 分流：站点抖动型仍保留，别改回去。"""
    entry = {"attempts": 1}
    assert mod.apply_retry_outcome(entry, "wedge_skipped") == (False, "wedge_skipped")
    assert entry["attempts"] == 1
    flaky = {"attempts": 0}
    assert mod.apply_retry_outcome(flaky, "no_jd", jd_class=mod.NO_JD_ROUNDTRIP_FAILED) == (True, "kept")
    assert mod.apply_retry_outcome(flaky, "agent_timeout") == (True, "kept")


def test_retry_remove_desc_explains_wedge_skipped():
    """移出口径要说清「凭什么不再投」并指得出台账文件——不是又一条「岗位疑已失效」式的定性。"""
    desc = mod._RETRY_REMOVE_DESC["wedge_skipped"]
    assert "楔死" in desc and "wedge_skipped.json" in desc
    assert "疑已失效" not in desc and "详情不可达" not in desc
    assert "no_jd" not in mod._RETRY_REMOVE_DESC, "A5 的 no_jd 分流不受本次改动影响"


def test_outcome_summary_buckets_wedge_skipped_as_skipped():
    """#47 三桶：`wedge_skipped` **显式**归 skipped——它不是失败，是按设计不再投；
    留在未收录集合里会被保守归 failed，把「止损」读成「链路坏了」。"""
    assert "wedge_skipped" in mod._OUTCOME_SKIPPED_STATUSES
    rep = mod.build_report(["q"], {"applied": 0, "direct_fallback": 0}, [{"status": "wedge_skipped"}])
    assert rep["outcome_summary"] == {"applied": 0, "skipped": 1, "failed": 0}


def test_build_report_carries_wedge_ledger_counts():
    """B1⑤：report 答两件事——台账总条数、本场因台账跳过了几岗（附可追溯明细）。
    未接线（桩 state / 老报告读者）时给零形态而不是缺键，读报告的人不必猜「是没跑还是没跳」。"""
    state = {
        "applied": 0,
        "direct_fallback": 0,
        "wedge_ledger": {"enabled": True, "total": 5, "backfilled": 2, "rejected": 3},
        "wedge_skips": [{"encryptJobId": _WEDGE_EID, "job": "solidity开发 50-80K", "stage": "搜索/推荐流"}],
    }
    wl = mod.build_report(["q"], state, [])["wedge_ledger"]
    assert wl["total"] == 5 and wl["session_skipped"] == 1 and wl["enabled"] is True
    assert wl["backfilled"] == 2 and wl["rejected"] == 3, "被门槛筛掉的场数也进读数（不然读不出「为什么台账是空的」）"
    assert wl["skipped_jobs"][0]["encryptJobId"] == _WEDGE_EID
    bare = mod.build_report(["q"], {"applied": 0, "direct_fallback": 0}, [])["wedge_ledger"]
    assert bare["total"] == 0 and bare["session_skipped"] == 0 and bare["enabled"] is False


def test_done_line_carries_wedge_skip_counts(tmp_path, monkeypatch, capsys):
    """B1⑤ 人读层：[done] 行按 #47 分桶口径带一句；本场没跳过时零噪音（别刷屏）。"""
    monkeypatch.setattr(mod, "edd_wrapup", _EDD_STUB)
    monkeypatch.setattr(mod.g, "resolve_paths", lambda env=None: {"name": "p"})
    state = {
        "applied": 0,
        "direct_fallback": 0,
        "keyword_skips": [],
        "wedge_ledger": {"enabled": True, "total": 5, "backfilled": 0, "rejected": 0},
        "wedge_skips": [{"encryptJobId": _WEDGE_EID}, {"encryptJobId": "e2"}],
    }
    mod._write_session_report(_report_run(tmp_path, state=state))
    out = capsys.readouterr().out
    assert "楔死跳岗 2" in out and "台账共 5" in out and "开" in out

    quiet = _report_run(tmp_path, state={"applied": 0, "direct_fallback": 0, "keyword_skips": []})
    mod._write_session_report(quiet)
    assert "楔死" not in capsys.readouterr().out, "0 时不刷屏"


def test_done_line_carries_greeting_ledger_counts(tmp_path, monkeypatch, capsys):
    """#64 P4 人读层：[done] 行带招呼语台账计数一句；三个计数全为 0 时整段不出现（零噪音）。"""
    monkeypatch.setattr(mod, "edd_wrapup", _EDD_STUB)
    monkeypatch.setattr(mod.g, "resolve_paths", lambda env=None: {"name": "p"})
    state = {
        "applied": 2,
        "direct_fallback": 0,
        "keyword_skips": [],
        "greeting_ledger": {"added": 1, "overwritten": 1, "skipped": 0},
    }
    mod._write_session_report(_report_run(tmp_path, state=state))
    out = capsys.readouterr().out
    assert "；招呼语台账 入库 1 / 覆盖 1" in out
    assert "跳过 0" not in out, "0 计数的桶不出现在片段里"

    quiet = _report_run(tmp_path, state={"applied": 0, "direct_fallback": 0, "keyword_skips": []})
    mod._write_session_report(quiet)
    assert "招呼语台账" not in capsys.readouterr().out, "全零时整段不出现"


def test_init_live_session_wires_wedge_ledger(monkeypatch, tmp_path):
    """接线回归（#33 教训：只在函数里实现、生产路径不接=白做，测试还全绿）：
    `_init_live_session` 必须 ① 经 `private_path` + profile 前缀落台账路径、② 解析开关入 cfg、
    ③ 真的跑一遍回灌+载入+呈报。路径必须是 private_path 的结果（#28：路径知识单一出口）。"""
    base = _init_stubs(monkeypatch, tmp_path)
    _wedge_marker(base / "live_session", "20200101-000000")
    run = mod._init_live_session({})
    cfg = run.session.cfg
    assert Path(cfg["wedge_skipped_path"]) == tmp_path / "private" / "data" / "wedge_skipped.json"
    assert cfg["wedge_skip_enabled"] is True, "默认开（用户 2026-09-28 拍板）"
    booked = mod._load_wedge_skipped(Path(cfg["wedge_skipped_path"]))
    assert [e["encryptJobId"] for e in booked] == [_WEDGE_EID], "开跑没接回灌 = 下场照样从同一个岗开始"
    assert run.session.state["wedge_eids"][_WEDGE_EID]["wedge_count"] == 1

    off = mod._init_live_session({"LIVE_WEDGE_SKIP": "off"})
    assert off.session.cfg["wedge_skip_enabled"] is False, "off 要落进 cfg，消费点才看得见"


def test_init_live_session_wedge_ledger_path_is_profile_scoped(monkeypatch, tmp_path):
    """#28 口径：台账路径由 `private_path` 加 profile 前缀，非在位 profile 不得共用同一文件
    （A 的楔死岗不该让 B 的会话跳过）。"""
    _init_stubs(monkeypatch, tmp_path, profile=("ai", "ai."))
    run = mod._init_live_session({})
    assert Path(run.session.cfg["wedge_skipped_path"]).name == "ai.wedge_skipped.json"


def test_init_live_session_wires_greeting_migration(monkeypatch, tmp_path):
    """接线回归（#33 教训：只在函数里实现、生产路径不接=白做）：#64 追加验收 5 的迁移
    必须在开跑时真的跑一遍，读数落 `state["greeting_migration"]`（report.json 同源）。"""
    base = _init_stubs(monkeypatch, tmp_path)
    base.mkdir(parents=True, exist_ok=True)
    (base / "greetings.json").write_text(
        json.dumps([{"encryptJobId": "a", "date": None, "source": None}]), encoding="utf-8"
    )
    run = mod._init_live_session({})
    assert run.session.state["greeting_migration"]["tagged"] == 1
    entries = json.loads((base / "greetings.json").read_text(encoding="utf-8"))
    # _init_stubs 把 load_applied_ids 桩成空集 ⇒ eid 找不到投递记录，归 draft_only 桶
    assert entries[0]["source"] == "legacy_draft_only"


def _init_stubs(monkeypatch, tmp_path, profile=("blockchain", "")):
    """`_init_live_session` 的最小可跑桩：外部依赖全替，`private_path` 指向 tmp_path
    （真实 private/ 一个字节都不写）。返回 tmp 下的 private 根，供用例铺取证。"""
    monkeypatch.setattr(mod, "resolve_active_profile", lambda env=None: profile)
    monkeypatch.setattr(mod, "profile_line", lambda name, prefix: f"[profile] {name}")
    monkeypatch.setattr(
        mod,
        "load_config",
        lambda env=None: {"quiet_hours": (0, 0), "daily_limit_fixed": True, "daily_limit": 20, "cap_jitter": 0},
    )
    monkeypatch.setattr(mod, "effective_daily_limit", lambda cfg, state_path=None: 20)
    monkeypatch.setattr(mod, "profile_sections", lambda path: None)
    monkeypatch.setattr(mod, "parse_queries", lambda env: ["AI"])
    monkeypatch.setattr(mod, "is_quiet_hour", lambda hour, quiet: False)
    monkeypatch.setattr(mod, "startup_jitter", lambda cfg: None)
    monkeypatch.setattr(
        mod,
        "SessionPacer",
        # #58 T1 起 `_init_live_session` 会注入 `beat_fn=_heartbeat_beat`，桩签名跟着放开
        lambda cfg, **kw: SimpleNamespace(wait_skip=lambda: None, wait_sent=lambda: None),
    )
    monkeypatch.setattr(mod, "get_platform", lambda: SimpleNamespace())
    monkeypatch.setattr(mod, "load_applied_ids", lambda path: set())
    monkeypatch.setattr(mod, "load_excluded_companies", lambda path: set())
    monkeypatch.setattr(mod, "_open_session_dirs", lambda b, v, r, ts: (tmp_path / "s", tmp_path / "shots", {}))

    def fake_private_path(rel, prefix=""):
        p = Path(rel)
        return tmp_path / "private" / p.parent / f"{prefix}{p.name}"

    monkeypatch.setattr(mod, "private_path", fake_private_path)
    return tmp_path / "private" / "data"


# --- #58 T1：合法长等待持续报平安（分段节拍铺到 SessionPacer 与「等首批 XHR」轮询）---
#
# 票面根因：#56 A4 的取栈阈值（90s）与看门狗停摆阈值（240s）都以「每个机械节点都会拍」为前提，
# 而会话里最长的合法空隙恰恰没有拍点——一场零楔死的健康会话（20260929-145750）三次落栈，
# 栈顶分别是 `rnd_sleep`（intra 限速）、`search_list` 的等首批 XHR 轮询、再一次 `rnd_sleep`。
# T1 只补拍点（分段睡），节奏参数的语义与取值一字不动。
#
# 本段用例的观测口子：`SessionPacer(nap_fn=...)` 是**秒级**睡眠钩子（记账代替真睡），
# `beat_fn` 直接注 `mod._heartbeat_beat` 本体——于是「等待期间心跳文件被刷了几次」是真实写盘次数，
# 不是桩的调用数。_MidRng 取中值，好让「等待总时长」写成可断言的整数。


def _load_limits():
    """同文件加载 `_limits`（照 `test_pacer_hourly_throughput_bounds` 的先例）：#58 的阈值护栏
    要同时读 `_limits.PACE_BEAT_INTERVAL` 与 live_apply 的两级阈值——两处都抄常数的话，
    改了常数测试还是绿的（那正是本票要治的「阈值前提没人复核」）。"""
    spec = importlib.util.spec_from_file_location("_limits", Path(__file__).resolve().parent / "_limits.py")
    assert spec is not None and spec.loader is not None, "importlib spec 加载失败（模块文件缺失？）"
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class _MidRng:
    """中值随机源（口径照 test_limits 的 `_Rng`）：uniform/randint 都取区间中值。"""

    def uniform(self, lo, hi):
        return (lo + hi) / 2

    def randint(self, lo, hi):
        return (lo + hi) // 2

    def random(self):
        return 0.5


def test_long_break_keeps_refreshing_the_heartbeat_file(tmp_path, monkeypatch):
    """#58 的失败基线（R3）：长休息 480-1500s > 停摆阈值 240s，改前**一次心跳都不写** ⇒
    看门狗把一场健康会话整树 SIGKILL（票面后果 2），并且 90s 就给自己落一份假栈（后果 1）。
    现在每段之前刷一次 heartbeat.json，label 是 `pace:break`（`in_job=false` 也照 A2 口径带着，
    读的人由此分辨得出「这是合法等待的拍点」而不是「死在哪个协议调用上」）。"""
    writes = []
    real_write = fx.write_json_atomic

    def spy(path, payload):
        writes.append(payload["label"])
        return real_write(path, payload)

    monkeypatch.setattr(fx, "write_json_atomic", spy)
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    lim = _load_limits()
    segs = []
    pacer = lim.SessionPacer(
        {"session_min": 2, "session_max": 2, "intra_min": 60, "intra_max": 150, "break_min": 480, "break_max": 1500},
        hour_fn=lambda: 10,
        rng=_MidRng(),
        beat_fn=mod._heartbeat_beat,
        nap_fn=segs.append,
    )
    pacer.wait_sent()
    pacer.wait_sent()  # 凑满 session 2/2 → 长休息（中值 990s）
    assert writes.count("pace:break") == 33, "长休息 990s 每 30s 一拍；改前这一等是 0 拍"
    assert writes.count("pace:intra") == 8, "两次会话内短等各 4 拍（105s 中值）"
    assert max(segs) <= lim.PACE_BEAT_INTERVAL
    assert sum(segs) == pytest.approx(105.0 * 2 + 990.0), "等待总时长 == 分段前的单次取值之和（不缩短）"
    hb = json.loads((tmp_path / mod.SESSION_HEARTBEAT).read_text(encoding="utf-8"))
    assert hb["label"] == "pace:break" and hb["in_job"] is False


def test_init_live_session_injects_the_heartbeat_into_the_pacer(monkeypatch, tmp_path):
    """接线回归（#33 的教训：实现了不接=白做，测试还全绿）：`_init_live_session` 构造 pacer
    时必须把 `_heartbeat_beat` 注成 `beat_fn`——分段逻辑在生产路径上拿不到拍点就是零效果。"""
    _init_stubs(monkeypatch, tmp_path)
    seen = {}

    def fake_pacer(cfg, **kw):
        seen.update(kw)
        return SimpleNamespace(wait_skip=lambda: None, wait_sent=lambda: None)

    monkeypatch.setattr(mod, "SessionPacer", fake_pacer)
    mod._init_live_session({})
    assert seen.get("beat_fn") is mod._heartbeat_beat, "拍点注入器 = A3 同一条吞异常口径"


class _LateXhrPage(_ListPage):
    """搜索页替身：`goto` **不**投首批，首批延后到第 `arrive_after` 次 `wait_for_timeout`
    才到——专门造出 `search_list` 里那段「等首批」的纯轮询窗口（票面假栈的 dump 2）。"""

    def __init__(self, arrive_after=None, url=None):
        # url 可指到推荐流端点（recommend_list 的 frag 与搜索流不同，不指过去首批永远不命中）
        extra = {"url": url} if url else {}
        super().__init__(batches=[_JOBLIST] if arrive_after else [], **extra)
        self._arrive_after = arrive_after
        self._waits = 0

    def goto(self, url, **kw):
        self.gotos.append(url)  # 关键：不 _emit

    def wait_for_timeout(self, ms):
        self._waits += 1
        if self._arrive_after and self._waits >= self._arrive_after:
            self._emit()


def test_search_list_beats_every_round_of_the_first_batch_wait(monkeypatch):
    """#58 验收 2：等首批 XHR 的 60s 轮询逐轮拍点，label 带轮次（与 `scroll:{n}` 同族），
    首批到货后照常返回——循环退出条件没动。"""
    beats = []
    monkeypatch.setattr(mod, "_heartbeat_beat", lambda label: beats.append(label))
    _quiet_search(monkeypatch)
    page = _LateXhrPage(arrive_after=3)
    listing = mod.search_list(page, "智能合约")
    assert [j["encryptJobId"] for j in listing] == ["j1", "j2"]
    assert beats == ["search:wait-xhr:1", "search:wait-xhr:2", "search:wait-xhr:3"]


def test_search_list_xhr_wait_still_gives_up_at_60s(monkeypatch):
    """同一循环的 60s 上界与「零命中返回 []」出口一字不动（T1 只加拍点，不改等待语义）：
    时钟按 20s 一跳 → 第 1 轮 20s、第 2 轮 40s 都继续等，第 3 次判定 60s 到界即止。"""
    beats = []
    monkeypatch.setattr(mod, "_heartbeat_beat", lambda label: beats.append(label))
    _quiet_search(monkeypatch)
    ticks = itertools.count(0, 20)
    monkeypatch.setattr(mod.time, "time", lambda: next(ticks))
    page = _LateXhrPage()
    assert mod.search_list(page, "智能合约") == []
    assert beats == ["search:wait-xhr:1", "search:wait-xhr:2"]
    assert page.listeners == [], "监听器注销不受本票影响"


def test_recommend_list_beats_every_round_of_the_first_batch_wait(monkeypatch):
    """#58 T1 对称补漏：`recommend_list` 里有与 `search_list` 同型的 60s 等首批 XHR 轮询，
    票面只点名了搜索流——但推荐流这一处前面同样叠着 `goto_with_retry`（30s goto + 5-8s 退避），
    最坏可逼近 90s 取栈阈值。合法等待必须持续报平安，label 前缀按来源区分（`recommend:`）。"""
    beats = []
    monkeypatch.setattr(mod, "_heartbeat_beat", lambda label: beats.append(label))
    _quiet_scroll(monkeypatch)
    page = _LateXhrPage(arrive_after=3, url=mod.RECOMMEND_XHR_FRAG)
    mod.recommend_list(page, limit=10)
    assert beats == ["recommend:wait-xhr:1", "recommend:wait-xhr:2", "recommend:wait-xhr:3"]
    assert page.listeners == [], "监听器注销不受本票影响"


def test_recommend_list_xhr_wait_still_gives_up_at_60s(monkeypatch):
    """上界与「零命中返回 []」出口一字不动（只加拍点，不改等待语义）：时钟按 20s 一跳
    → 两轮后到界即止，与 search_list 那条同形。"""
    beats = []
    monkeypatch.setattr(mod, "_heartbeat_beat", lambda label: beats.append(label))
    _quiet_scroll(monkeypatch)
    ticks = itertools.count(0, 20)
    monkeypatch.setattr(mod.time, "time", lambda: next(ticks))
    page = _LateXhrPage()
    assert mod.recommend_list(page, limit=10) == []
    assert beats == ["recommend:wait-xhr:1", "recommend:wait-xhr:2"]


@pytest.mark.parametrize(
    "case,hour,cfg,want_label,want_total",
    [
        (
            "intra 工作时段 ×1.0",
            10,
            {"session_min": 9, "session_max": 9, "intra_min": 60, "intra_max": 150},
            "pace:intra",
            105.0,
        ),
        (
            "intra 晚间 ×2.0（票面最长合法等待 300s 的来源）",
            22,
            {"session_min": 9, "session_max": 9, "intra_min": 60, "intra_max": 150},
            "pace:intra",
            210.0,
        ),
        (
            # intra 设成 0/0 只为把观测面隔离到长休息本身（0 秒等待一段都不切）；生产取值不动。
            "break 连投满后的长休息（480-1500s，不乘系数）",
            22,
            {"session_min": 1, "session_max": 1, "intra_min": 0, "intra_max": 0, "break_min": 480, "break_max": 1500},
            "pace:break",
            990.0,
        ),
        (
            "dismiss 跳过岗短等 ×2.0",
            22,
            {"session_min": 9, "session_max": 9, "dismiss_min": 15, "dismiss_max": 40},
            "pace:dismiss",
            55.0,
        ),
    ],
)
def test_every_kind_of_long_pacer_wait_stays_inside_the_beat_ceiling(case, hour, cfg, want_label, want_total):
    """#58 验收 4（护栏·覆盖）：票面对照表里的每一类合法等待都逐条走一遍——
    ① 总时长 == 分段前的单次取值（不缩短），② 任何一段 ≤ PACE_BEAT_INTERVAL，
    ③ 拍点数 == 段数（每段之前一拍，等待期间没有落空的空隙）。
    为什么把「类别」列成表：将来有人调大 `intra_max` / `break_max`，段长是常数、覆盖不破；
    真正的破法是有人把 `PACE_BEAT_INTERVAL` 调到超过阈值——那由
    `test_legitimate_silence_ceiling_sits_below_both_thresholds` 先红。"""
    lim = _load_limits()
    beats, segs = [], []
    pacer = lim.SessionPacer(cfg, hour_fn=lambda: hour, rng=_MidRng(), beat_fn=beats.append, nap_fn=segs.append)
    if want_label == "pace:dismiss":
        pacer.wait_skip()
    else:
        pacer.wait_sent()
    assert sum(segs) == pytest.approx(want_total), f"{case}：等待总时长被分段改掉了"
    assert segs and max(segs) <= lim.PACE_BEAT_INTERVAL, f"{case}：出现超过节拍上界的整段"
    assert beats == [want_label] * len(segs), f"{case}：每一段之前必须有一拍"
    if want_total > mod.WEDGE_STACK_SECONDS:
        assert len(beats) > 1, f"{case}：{want_total}s 已超过取栈阈值，必须被分段覆盖"


def test_legitimate_silence_ceiling_sits_below_both_thresholds():
    """#58 验收 4（护栏·不等式）：锁住 `PACE_BEAT_INTERVAL < WEDGE_STACK_SECONDS <
    SESSION_WATCHDOG_DEFAULT`，并把票面那张对照表的关键数字写成断言——
    这两级阈值本来就是「合法无心跳上界」的函数，动任何一个常数都必须先在这里红一次。"""
    lim = _load_limits()
    cfg = lim.load_config(env={})
    # 前提链：分段节拍是合法的**最长**无心跳间隔，两级阈值都在它之上（3 倍 / 2.7 倍余量）
    assert lim.PACE_BEAT_INTERVAL < mod.WEDGE_STACK_SECONDS < mod.SESSION_WATCHDOG_DEFAULT
    # 票面对照表：不分段时这两类等待本身就 > 两级阈值（旧估算「~90s」错在没把它们算进去）
    worst_intra = cfg["intra_max"] * lim.DEFAULT_HOUR_MULTIPLIER  # 150×2.0 = 300s（9-17 点外的时段）
    assert worst_intra == 300 and cfg["break_min"] == 480 and cfg["break_max"] == 1500
    assert worst_intra > mod.SESSION_WATCHDOG_DEFAULT, "晚间 intra 300s > 停摆阈值 240s：不分段必被击杀"
    assert cfg["break_min"] > mod.SESSION_WATCHDOG_DEFAULT, "长休息 480s > 240s：连投满 2 个即触发"
    assert worst_intra > mod.WEDGE_STACK_SECONDS and cfg["break_min"] > mod.WEDGE_STACK_SECONDS
    # 旧估算里的「~90s」正是被这些合法等待击穿的量级；现在的上界由分段节拍决定
    assert mod.SESSION_WATCHDOG_DEFAULT - lim.PACE_BEAT_INTERVAL >= lim.PACE_BEAT_INTERVAL


def test_followup_chat_page_silence_ceiling_sits_below_both_thresholds():
    """#59 修复轮 R4-1（护栏·不等式，紧挨上一条同族）：`platforms/chat_first.followup()` 的收尾采集段
    是**没进过对照表**的那一条合法静默——旧注释按「sleep 6 + 轮询 15 ≈ 21s」自证「低于两级阈值」，
    漏算了 goto 自身的超时上界：改前真实最坏 90 + 6 + 15 = **111s**，顶穿取栈阈值 90s ⇒ 一场
    **健康但慢**的聊天页会给自己落一份非空 `wedge-stack.txt`，正面推翻 runbook「栈文件为空＝本场
    没楔过」的判读（#58 那批假栈的复发形态）。
    数字全部**从常量取**而不是从注释抄：有人改任一处常数，这里当场红，逼他重新对表。"""
    import platforms.chat_first as chat_mod

    lim = _load_limits()
    # 两次拍点之间的最长合法静默 = goto 上界（goto 前后各一拍，中间夹固定停顿）+ 固定停顿
    goto_then_sleep_seconds = chat_mod.CHAT_GOTO_TIMEOUT_MS / 1000 + chat_mod.FOLLOWUP_POST_GOTO_SLEEP_SECONDS
    poll_window_ms = chat_mod.FRIEND_POLL_ROUNDS * chat_mod.FRIEND_POLL_INTERVAL_MS
    # 本段的既有事实（改任一常数先进这条，再去复核上面那条不等式）
    assert goto_then_sleep_seconds == 66.0, (chat_mod.CHAT_GOTO_TIMEOUT_MS, chat_mod.FOLLOWUP_POST_GOTO_SLEEP_SECONDS)
    assert poll_window_ms == 15000, (chat_mod.FRIEND_POLL_ROUNDS, chat_mod.FRIEND_POLL_INTERVAL_MS)
    # 对表线：聊天页静默上界 ≤ 取栈阈值 < 停摆阈值（轮询段本身逐轮拍点，故窗口总长不计入静默）
    assert goto_then_sleep_seconds <= mod.WEDGE_STACK_SECONDS < mod.SESSION_WATCHDOG_DEFAULT
    # 轮询段的无心跳上界 = 单轮间隔（0.3s），必须远低于分段节拍与两级阈值
    assert chat_mod.FRIEND_POLL_INTERVAL_MS / 1000 < lim.PACE_BEAT_INTERVAL < mod.WEDGE_STACK_SECONDS


# --- #58 T2：B1 归因的正向守卫（已投出的岗不得进跳岗台账）---
#
# 票面后果 3（本票最重的一条）：击杀点在 `_record_apply_outcome` 调 `pacer.wait_sent()` 时，
# 而 `_apply_job` 的 `finally`（清岗位上下文）排在它**之后** ⇒ 被杀瞬间心跳仍是
# `in_job=true` + eid=刚成功投出的那个岗。B1 拿这个 eid 回灌入账，就是把一个已经投出的人
# 当成嫌疑人永久拉黑，且全程无声——与 #56 A5 同一条纪律：丢岗必须是**正向证据**，不能是推断。
# 本段刻意**不动** `_apply_job` 的 finally 顺序（A2 的「死在哪个岗上」归因能力靠它），
# 守卫长在回灌这一侧。


def test_split_already_applied_wedge_items_is_a_pure_positive_guard():
    """T2 守卫的纯函数形态（照 `_wedge_entry_from_marker` / `_merge_wedge_ledger` 的风格）：
    eid 命中已投台账的条目被单独分出来，不改入参；两侧都归一成字符串（#56 修复轮 F3 同口径），
    非字符串/空白条目不得因为「长得像」就被判成已投。"""
    items = [{"encryptJobId": _WEDGE_EID}, {"encryptJobId": "e-fresh"}]
    keep, applied = mod._split_already_applied_wedge_items(items, {_WEDGE_EID})
    assert [i["encryptJobId"] for i in keep] == ["e-fresh"]
    assert [i["encryptJobId"] for i in applied] == [_WEDGE_EID]
    assert [i["encryptJobId"] for i in items] == [_WEDGE_EID, "e-fresh"], "纯函数：入参一字不动"
    keep2, applied2 = mod._split_already_applied_wedge_items(items, {f" {_WEDGE_EID} ", 12345, None, ""})
    assert [i["encryptJobId"] for i in applied2] == [_WEDGE_EID] and len(keep2) == 1
    keep3, applied3 = mod._split_already_applied_wedge_items(items, set())
    assert len(keep3) == 2 and applied3 == []


def test_backfill_skips_a_killed_job_that_was_actually_applied(tmp_path, capsys):
    """#58 验收 3：伪造「击杀取证的 eid 已在 applied_history」的会话目录 → 回灌**不入账**，
    呈报明说这条取证归因可疑、提示核查；台账文件不凭空造（#55 `retry_touched` 同口径）。"""
    base = tmp_path / "live_session"
    _wedge_marker(base)
    ledger = tmp_path / "wedge_skipped.json"
    stats = mod._backfill_wedge_ledger(base, "20260929-090000", ledger, {_WEDGE_EID})
    assert stats["already_applied"] == 1 and stats["booked"] == 0 and stats["written"] is False
    assert stats["rejected"] == 0, "「岗已投出」不是「取证形态不合格」，两件事分开计数"
    assert not ledger.exists()
    out = capsys.readouterr().out
    assert "已投出" in out and "归因可疑" in out and "核查" in out and _WEDGE_EID in out


def test_backfill_still_books_jobs_that_were_not_applied(tmp_path, capsys):
    """T2 不得把 B1 的止损削弱：不在已投台账里的 eid 照常入账（原有用例的默认已投集=空集，
    这条把「混合场景」钉死——两份取证，一份已投一份没投 → 入账 1、拦下 1）。"""
    base = tmp_path / "live_session"
    _wedge_marker(base)
    _wedge_marker(base, "20260928-090000", last_eid="e-wedged-for-real", last_job="真楔死岗", last_brand="别家")
    ledger = tmp_path / "wedge_skipped.json"
    stats = mod._backfill_wedge_ledger(base, "20260929-090000", ledger, {_WEDGE_EID})
    booked = mod._load_wedge_skipped(ledger)
    assert [e["encryptJobId"] for e in booked] == ["e-wedged-for-real"]
    assert stats["booked"] == 1 and stats["added"] == 1 and stats["already_applied"] == 1
    guard_line = "".join(ln for ln in capsys.readouterr().out.splitlines() if "归因可疑" in ln)
    assert _WEDGE_EID in guard_line and "e-wedged-for-real" not in guard_line, "守卫只点名被拦下的那个岗"


def test_backfill_keeps_the_two_rejection_readings_apart(tmp_path):
    """两份取证各自的原因各自计数：`in_job=false`（形态不合格，走 rejected）与
    「岗已投出」（走 already_applied）是两件事——读报告的人要能分辨「取证不可信」与
    「归因链条出了问题（T1 之后这条本该恒为 0，见呈报里的核查提示）」。"""
    base = tmp_path / "live_session"
    _wedge_marker(base)
    _wedge_marker(base, "20260928-090000", in_job=False, last_eid="e-innocent")
    stats = mod._backfill_wedge_ledger(base, "20260929-090000", tmp_path / "wedge_skipped.json", {_WEDGE_EID})
    assert stats["rejected"] == 1 and stats["already_applied"] == 1 and stats["booked"] == 0
    assert stats["scanned"] == 2, "两份取证都被扫到（计数分流 ≠ 少扫）"


def test_new_wedge_stats_zero_form_carries_the_guard_counter():
    """零形态键集合固定（`_new_wedge_stats` 的既有契约）：新增守卫键，report 的读者
    才能分辨「没跑回灌」与「跑了但全被守卫拦下」。"""
    zero = mod._new_wedge_stats()
    assert zero["already_applied"] == 0 and zero["rejected"] == 0 and zero["written"] is False
    bare = mod.build_report(["q"], {"applied": 0, "direct_fallback": 0}, [])["wedge_ledger"]
    assert bare["already_applied"] == 0, "未接线时给零形态而不是缺键（口径同 #56 B1⑤）"


def test_prepare_wedge_ledger_reports_the_guard_reading(tmp_path, capsys):
    """开跑三步（回灌→载入→呈报）读数同步：`state["wedge_ledger"]["already_applied"]`
    进 report.json，呈报里那句核查提示在开跑第一行就露出来（不当场念出来=没人会去查）。"""
    session, _ = _wedge_session(tmp_path)
    base = tmp_path / "live_session"
    _wedge_marker(base)
    mod._prepare_wedge_ledger(session, base, "20260929-090000", {_WEDGE_EID})
    out = capsys.readouterr().out
    assert "归因可疑" in out and _WEDGE_EID in out
    assert session.state["wedge_ledger"]["already_applied"] == 1
    assert session.state["wedge_ledger"]["backfilled"] == 0
    assert session.state["wedge_eids"] == {}, "被守卫拦下的岗不得进本场工作副本（不然照样被跳过）"


def test_init_live_session_wires_applied_history_into_the_wedge_guard(monkeypatch, tmp_path):
    """接线回归（#33 教训同款）：`_init_live_session` 必须把 `load_applied_ids` 的**同一份**
    已投集合交给开跑回灌——守卫长在纯函数里但没人接线=白长，那场「已成功投出的岗被永久
    跳过」的事故照样发生。"""
    base = _init_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(mod, "load_applied_ids", lambda path: {_WEDGE_EID})
    _wedge_marker(base / "live_session", "20200101-000000")
    run = mod._init_live_session({})
    assert not Path(run.session.cfg["wedge_skipped_path"]).exists(), "已投出的岗不该被写进跳岗台账"
    assert run.session.state["wedge_ledger"]["already_applied"] == 1
    assert run.session.state["wedge_eids"] == {}
    assert mod._wedge_entry_for(run.session, _WEDGE_EID) is None, "消费点查表也查不到它（同一个岗不投两次）"


# --- #58 附带修：重定向下的行缓冲（09-29 实测 25s 零日志，agent 只能盲等或重启）---


def test_enable_line_buffering_turns_it_on_and_never_raises(monkeypatch):
    """成功路径真的把流切成行缓冲；不可 reconfigure 的流（capsys 换掉的、已关闭的）
    返回 False 而不抛——观测性开关绝不成为投递启动失败的原因。"""
    calls = {}

    class _Out:
        def reconfigure(self, **kw):
            calls.update(kw)

    monkeypatch.setattr(mod.sys, "stdout", _Out())
    assert mod._enable_line_buffering() is True
    assert calls == {"line_buffering": True}

    class _Broken:
        def reconfigure(self, **kw):
            raise ValueError("I/O operation on closed file")  # 真实形态：流已关闭

    monkeypatch.setattr(mod.sys, "stdout", _Broken())
    assert mod._enable_line_buffering() is False


def test_run_live_enables_line_buffering_before_any_output(monkeypatch):
    """接线（#33「实现了但生产不接」的同型防回归）：`run_live` 第一件事就是切行缓冲，
    晚一步就落在 `[profile]` 那行 print 之后，重定向下依旧块缓冲。"""
    order = []
    monkeypatch.setattr(mod, "_enable_line_buffering", lambda: order.append("buffering"))
    monkeypatch.setattr(mod, "_init_live_session", lambda env=None: order.append("init") or None)
    assert mod.run_live({}) == 0  # 预检返回 None（静默时段形态）→ 正常早退
    assert order == ["buffering", "init"]


# --- #62：招呼语输入框的「还在不在本岗沟通形态」打字前守卫 ---------------------------------
#
# 现场（private/data/crash/20261009-115327-昱昉网络科技-live-no-send-4.html + 同批 .url.txt，
# 2026-10-09 11:53）：点「立即沟通」后页面**已弹回搜索结果列表**
# `/web/geek/jobs?...&query=Web3`（右栏写「没有找到相关职位，打开 APP，查看全部职位库」），
# 而 `find_input` 的裸 `textarea` 兜底命中了页面底部「对搜索结果是否满意？」反馈浮层里的
# textarea——那份 HTML 里**全页只有一个 textarea、零 contenteditable** ⇒ 199 字招呼语被打进了
# 站点的意见反馈框；随后 `find_send_button` 认不出 `satisfaction-submit-btn`，且输入框不是
# contenteditable（走不了 Enter 发送兜底）⇒ 落 `no_send`，而这个岗没进任何台账（下轮不补投）。
#
# 三件事分开修，本段逐个钉：
#   ① 打字**之前**的形态守卫（本票核心）：`off_chat` 归因 + 进 #55 跨会话补投台账；
#   ② `find_input` 命中反馈浮层视同未命中（纵深防御，见 test_feedback_overlay_is_not_an_input）；
#   ③ `find_send_button` 不认满意度提交按钮（防站点把文案改成「发送」，见 …_is_not_a_send_button）。

# 落空 URL：照存证 .url.txt 原样抄（已弹回列表，不含 job_detail、不含 /web/geek/chat）
_BOUNCED_URL = "https://www.zhipin.com/web/geek/jobs?city=101020100&multiBusinessDistrict=310120&query=Web3"
# 真实聊天页形态（#42 关票记录与 chat_first.py `CHAT_URL` 同口径：`/web/geek/chat?id=<招聘者 id>`）：
# URL 里**没有本岗 eid、也没有 jobId**——守卫认的只有这条 path，别把夹具写成「像校验了岗位」的样子。
# id 是虚构的招聘者标识（真值是加密串，不入仓）。
_CHAT_URL = "https://www.zhipin.com/web/geek/chat?id=b7f2c1a0"


class _GreetEl:
    """招呼语输入框替身：把三类「打字动作」都计数——守卫的验收口径是「确定一个字都没打进页面」，
    只盯 `fill` 会漏掉逐字键入分支（`_type_greeting` 按 paste_prob 随机二选一）和聚焦点击。
    `evaluate` 的应答与既有 `_FakeInputEl` 同形，另外能回答 #62 的反馈浮层祖先链探测（0 = 不在浮层内）。
    #68：按**脚本**分派——`editable=True` 建模 `<div contenteditable="true">`（问 `isContentEditable`
    回 True、问老的 tagName 探针回 `"DIV|true"`），默认 False 建模沟通输入框那个 `textarea`。
    分派是本票的要点：负对照（textarea + 无发送按钮 ⇒ `send_btn_missing`）必须由「替身对
    isContentEditable 那问老实回 False」撑起，不能再靠 `bool("TEXTAREA|")` 这种字符串巧合。"""

    def __init__(self, tag="TEXTAREA|", editable=False):
        self.tag = tag
        self.editable = editable
        self.fills = []
        self.clicks = 0
        self.overlay_probes = 0

    def is_visible(self):
        return True

    def evaluate(self, js, *args):
        if "AUTOJOB_FEEDBACK_PROBE" in js:
            self.overlay_probes += 1
            return 0
        return self._answer(js)

    def _answer(self, js):
        """非浮层探针的其余脚本按形态如实作答（子类只需改探针那一条）。"""
        if "isContentEditable" in js:
            return self.editable
        return self.tag

    def click(self, **kw):
        self.clicks += 1

    def fill(self, text):
        self.fills.append(text)


class _FeedbackBoxEl(_GreetEl):
    """满意度反馈浮层里那个 textarea 的替身（实测祖先链
    `textarea.input` → `.input-wrap.input-wrap-textarea.input-wrap-resize-none`
    → `.nput-wrap-textarea`（站点自己拼错的类名）→ `.c-satisfaction-feedback…`）：
    祖先链探测回 1 = 在浮层内。"""

    def evaluate(self, js, *args):
        if "AUTOJOB_FEEDBACK_PROBE" in js:
            self.overlay_probes += 1
            return 1
        return self._answer(js)


def _run_guard_case(
    tmp_path,
    monkeypatch,
    page,
    el,
    btn_seq=None,
    input_seq=None,
    send_btn="KEEP",
    send_click="OK",
    real_find_input=False,
):
    """发送段替身（复用 `_send_mocks`）+ 打字动作计数 + 台账落盘路径。
    `send_btn`="NONE" 时把 `find_send_button` 打成恒落空，用于「字已打进聊天页却找不到发送
    按钮」这条旧路径；`send_click`="FAIL" 时只让**发送**那一下点击失败（「立即沟通」仍成功），
    用于 `send_click_failed` 归因；`real_find_input=True` 时把 `_send_mocks` 换掉的
    `find_input` 还原成**生产实现**——形态保真用例必须走真的选择器一族，否则「反馈框会不会
    被裸 textarea 兜底命中」这条正是本票现场的问题被替身答掉了。
    其余外部动作一律沿用 `_send_mocks` 的替换。"""
    prod_find_input = mod.find_input  # 在 _send_mocks 覆盖它之前抓住生产实现
    calls = _send_mocks(
        monkeypatch,
        btn_seq if btn_seq is not None else [SimpleNamespace()],
        input_seq if input_seq is not None else [el],
    )
    typed = {"human_type": 0}

    def fake_human_type(page, text, **kw):
        typed["human_type"] += 1

    monkeypatch.setattr(mod, "human_type", fake_human_type)
    if send_btn == "NONE":
        monkeypatch.setattr(mod, "find_send_button", lambda page, **kw: None)
    if send_click == "FAIL":
        ok_click = mod.click_with_retry  # _send_mocks 的替身：恒 True

        def click_fails_only_on_send(page, btn, action="", beat_fn=None):
            return False if "发送" in action else ok_click(page, btn, action=action, beat_fn=beat_fn)

        monkeypatch.setattr(mod, "click_with_retry", click_fails_only_on_send)
    if real_find_input:
        monkeypatch.setattr(mod, "find_input", prod_find_input)
    platform = SimpleNamespace(
        login_dialog_selector="#login-dialog",
        save_evidence=lambda p, tag, job=None: calls["evidence"].append(tag),
    )
    pacer = SimpleNamespace(wait_skip=lambda: None, wait_sent=lambda: None)
    results, state = [], {"applied": 0, "direct_fallback": 0, "keyword_skips": []}
    job = {"jobName": "测试岗", "brandName": "测试公司", "encryptJobId": "e1"}
    outcome = mod.apply_live(page, job, tmp_path, 1, tmp_path, {"paste_prob": 1.0}, pacer, platform, results, state)
    return outcome, results, state, calls, typed


def test_preinput_guard_blocks_typing_when_bounced_to_feedback_box(tmp_path, monkeypatch):
    """#62 形态对照的**坏形态**一侧（本票 red 基线）：点沟通返回成功、输入框也找到了，
    但页面已弹回搜索列表且那个「输入框」是满意度反馈浮层的 textarea ⇒ 守卫必须在
    `_type_and_send_greeting` 之前把它拦下，一个字都不许进页面。
    `send_btn="NONE"` 复刻存证的最后一跳：那页的按钮是 `satisfaction-submit-btn`，
    `find_send_button` 四个选择器一个都不认 ⇒ 未加守卫时的实际行为＝先 `fill` 199 字、
    再落无归因的 `no_send`、且这个岗不进任何台账（与 20261009-115327 现场一致）。"""
    page = _SendPage(_BOUNCED_URL)  # panel_title 默认 ""＝右栏没有本岗（实测那句「没有找到相关职位」）
    el = _FeedbackBoxEl()
    outcome, results, state, calls, typed = _run_guard_case(tmp_path, monkeypatch, page, el, send_btn="NONE")

    assert [r["status"] for r in results] == ["no_send"], "状态串沿用既有 no_send，不新造（#47 三桶）"
    assert results[0].get("reason") == "off_chat", "两种 no_send 成因要靠 reason 分辨：本条是打字前拦下"
    assert el.fills == [] and typed["human_type"] == 0 and el.clicks == 0, "守卫必须发生在任何打字动作之前"
    assert calls["evidence"] == ["live-off-chat-1"], "存证换新标签，别再和真正的发送失败混在一个文件名上"
    # 守卫在**找输入框之前**：形态不对就不必再问 DOM 要输入框（省 7 次 query_selector +
    # 一次祖先链探测），更要紧的是别让这一岗落进 `no_input`——那是 #42「聊天页加载态未就绪」
    # 的语义，把「人已离岗」混进去就同时丢掉归因和补投台账。
    assert calls["find_input"] == 0, "形态守卫必须排在 find_input 之前"
    assert outcome == "next"


def test_preinput_guard_passes_real_chat_url_form(tmp_path, monkeypatch):
    """形态对照的**好形态**之一，也是 brief 点名必须钉住的那条：真实聊天页 URL 是
    `/web/geek/chat?id=<招聘者用户 id>`，**不含本岗 encryptJobId** ⇒ 只认 `on_job_detail` 的守卫
    会把所有「点沟通直接进聊天页」的岗全挡死（比原 bug 更严重的回归）。并集里这条必须放行。"""
    page = _SendPage(_CHAT_URL)
    el = _GreetEl()
    outcome, results, state, calls, typed = _run_guard_case(tmp_path, monkeypatch, page, el)

    assert [r["status"] for r in results] == ["applied"], "聊天页形态必须放行，不得误挡"
    assert len(el.fills) == 1 and state["applied"] == 1
    assert calls["evidence"] == []


def test_preinput_guard_passes_standalone_detail_and_split_panel_forms(tmp_path, monkeypatch):
    """形态对照的**好形态**之二/之三（`on_job_detail` 的两种形态，#41/#33 第八轮既有判据）：
    ① 独立详情页 `/job_detail/{eid}.html`；② 新版分栏视图——URL 与出事那张存证**同一个列表页**，
    区别只在右栏承载的是不是本岗。守卫复用同一条形态无关判据 ⇒ ② 放行、③ 右栏是别岗则挡
    （误投别岗比漏投代价高，与 `find_comm_button_with_recovery` 的既有裁决同源）。"""
    el = _GreetEl()
    _, results, state, _, _ = _run_guard_case(tmp_path, monkeypatch, _SendPage(_DETAIL_URL), el)
    assert [r["status"] for r in results] == ["applied"], "独立详情页形态放行"

    _, results, state, _, _ = _run_guard_case(
        tmp_path, monkeypatch, _SendPage(_BOUNCED_URL, panel_title="测试岗 20-30K 上海"), _GreetEl()
    )
    assert [r["status"] for r in results] == ["applied"], "分栏右栏是本岗 ⇒ 同 URL 也算在沟通形态"


def test_in_chat_form_is_a_union_and_fails_closed():
    """`_in_chat_form` 的取值表——「并集」与「保守」两条只能在这一层钉：集成用例里
    `find_comm_button_with_recovery` 会先把「右栏是别岗」的分栏视图直跳成本岗独立详情页，
    形态在守卫之前就已修好，所以别岗那条根本到不了守卫（这正说明换形态是上游的职责、
    守卫只负责在换不动时拦住）。
    放行侧缺任何一条都会把一类正常形态全挡死——尤其聊天页：真实 URL 是
    `/web/geek/chat?id=<招聘者用户 id>`，**不含本岗 encryptJobId** ⇒ 只认 `on_job_detail` 恒 False。"""
    for url, title, why in [
        (_DETAIL_URL, "", "独立详情页：URL 含 job_detail+eid（#41）"),
        (_CHAT_URL, "", "聊天页：只有 /web/geek/chat 这条认得（#4 峰云岗形态）"),
        (_BOUNCED_URL, "测试岗 20-30K", "分栏视图右栏＝本岗（#33 第八轮）"),
    ]:
        assert mod._in_chat_form(_SendPage(url, panel_title=title), "e1", "测试岗") is True, why
    for url, title, why in [
        (_BOUNCED_URL, "", "弹回列表且右栏没有本岗＝本票现场"),
        (_BOUNCED_URL, "高级Java开发-远程 30-40K", "分栏右栏是别岗 ⇒ 不是本岗的沟通形态"),
        (_DETAIL_URL.replace("e1.html", "e2.html"), "", "别人的详情页不算（#41 既有裁决）"),
    ]:
        assert mod._in_chat_form(_SendPage(url, panel_title=title), "e1", "测试岗") is False, why
    # 取不到判定信息 ⇒ 保守判「不在沟通形态」（与 `on_job_detail` 取不到右栏标题返回 False 同向）
    assert mod._in_chat_form(object(), "e1", "测试岗") is False, "非 DOM 句柄：保守挡"

    class _NoUrlPage:
        @property
        def url(self):
            raise RuntimeError("page 已关闭")

        def evaluate(self, js, *args):
            return "测试岗 20-30K"  # 右栏标题就算拿得到，URL 读不到也不许放行

    assert mod._in_chat_form(_NoUrlPage(), "e1", "测试岗") is False, "URL 取不到：保守挡"


def test_feedback_overlay_probe_fails_closed_on_a_real_handle():
    """元素级第二层：真句柄**探不出**祖先链（节点已随导航被摘掉／执行上下文销毁＝页面正在跳走）
    ⇒ 保守判「在反馈浮层里」，不许打字。判「探不出＝可用」等于把本票的现场当放行条件——
    打进反馈框的那 199 字捞不回，少投一次的岗能被 #55 台账捞回。
    对照另一半：没有 `evaluate` 的调用方（单测替身）不判在浮层里，那条路径上根本没有 DOM 可查。"""
    assert mod._in_feedback_overlay(_GreetEl()) is False, "探针回 0＝正向证明不在浮层内"
    assert mod._in_feedback_overlay(_FeedbackBoxEl()) is True, "探针回 1＝命中浮层"

    class _HandleThatCannotAnswer:
        def evaluate(self, js, *args):
            raise RuntimeError("Execution context was destroyed")

    assert mod._in_feedback_overlay(_HandleThatCannotAnswer()) is True, "真句柄探不出：保守判不可用"
    assert mod._in_feedback_overlay(SimpleNamespace()) is False, "非 DOM 句柄（桩）：无 DOM 可查，不臆断"


def test_feedback_overlay_markers_are_the_saved_evidence_dom():
    """把判据钉在存证 HTML 的真实字符串上（`private/data/crash/20261009-115327-昱昉网络科技-
    live-no-send-4.html`，gitignored 故不在用例里读它，只抄结论）：容器类名、站点拼错的
    `.nput-wrap-textarea`、提交按钮类名、以及 `ka=` 锚的**两种拼法**（提交用下划线、关闭用连字符）
    ——这些是选择器唯一的真值来源，改坏了就等于把反馈框重新放进来，而单测替身是察觉不到的。"""
    assert ".c-satisfaction-feedback" in mod.FEEDBACK_OVERLAY_SELECTORS
    assert ".card-feedback" in mod.FEEDBACK_OVERLAY_SELECTORS
    assert ".satisfaction-submit-btn" in mod.FEEDBACK_OVERLAY_SELECTORS
    assert "satisfaction_feedback" in mod.FEEDBACK_ANCHOR_PREFIXES, "ka=satisfaction_feedback_submit_click"
    assert "satisfaction-feedback" in mod.FEEDBACK_ANCHOR_PREFIXES, "ka=satisfaction-feedback_close"
    # 聊天页形态的锚：与 platforms/chat_first.py:29 CHAT_URL 同源，别写成只匹配带 eid 的形态
    assert mod.CHAT_URL_MARK == "/web/geek/chat"
    assert mod.CHAT_URL_MARK in chat_mod.CHAT_URL, "锚必须是平台层 CHAT_URL 的子串，否则生产恒不命中"


class _OverlayOnlyPage:
    """按选择器发牌的极简页面替身：`deals` 里点到的选择器返回对应元素，其余返回 None。
    只建模 `query_selector`——`find_input` / `find_send_button` 一族用的就是它（timeout=None 裸调）。"""

    def __init__(self, **deals):
        self.deals = deals

    def query_selector(self, sel):
        return self.deals.get(sel)


def test_feedback_overlay_is_not_an_input():
    """验收 2 的元素层：反馈浮层里的 textarea **命中了选择器**却一律视同未命中——出事那页全页
    只有一个 textarea，就是它，裸 `textarea` 兜底把 199 字招呼语送进了站点意见反馈渠道。
    正对照一并钉：真聊天 textarea 与反馈框同页并存时返回前者——排除的是浮层里的控件，
    不是把整族选择器打哑（打哑就等于所有岗都 `no_input`，比原 bug 更糟）。"""
    box = _FeedbackBoxEl()
    assert mod.find_input(_OverlayOnlyPage(**{"textarea": box})) is None, "浮层里的 textarea 不算输入框"
    assert box.overlay_probes >= 1, "落空的原因是排除，不是没查到"

    real = _GreetEl()
    got = mod.find_input(_OverlayOnlyPage(**{".chat-input textarea": real, "textarea": _FeedbackBoxEl()}))
    assert got is real, "同页并存时仍要拿到真的沟通输入框"


def test_feedback_overlay_is_not_a_send_button():
    """验收 2 的另一半（纵深防御）：站点的满意度「提交」按钮今天只靠**文案**侥幸躲过那四条
    选择器（「提交」不在其中）。构造那个改版形态——类名与文案双双命中 `button:has-text('发送')`
    / `.btn-send`，只因它落在浮层里 ⇒ 不认作发送按钮，否则求职信会被提交进满意度渠道。"""
    for sel in ("button:has-text('发送招呼')", "button:has-text('发送')", ".btn-send", "[class*='send']"):
        submit = _FeedbackBoxEl()
        assert mod.find_send_button(_OverlayOnlyPage(**{sel: submit})) is None, f"{sel} 命中浮层提交按钮也不认"
        assert submit.overlay_probes >= 1

    real = _GreetEl()  # 这里只当「浮层外、可见、探针回 0」的通用元素替身用（发送按钮的判据就这三条）
    assert mod.find_send_button(_OverlayOnlyPage(**{"button:has-text('发送招呼')": real})) is real, "真发送按钮照旧认"


class _BouncedFeedbackPage(_SendPage):
    """存证现场的 DOM 保真替身（`20261009-115327-昱昉网络科技-live-no-send-4.html`）：已弹回
    搜索列表（URL 不含 job_detail、不含 /web/geek/chat，右栏标题空＝那句「没有找到相关职位」），
    而全页**只有一个** textarea——满意度反馈浮层里那个 `placeholder="请填写更多反馈意见…"`
    （实测零 contenteditable）。`find_input` 一族里会命中它的只有最后那条裸 `textarea` 兜底，
    故只对它应答；登录框等其余选择器照 `_SendPage` 返回 None。"""

    def __init__(self, url=_BOUNCED_URL):
        super().__init__(url)
        self.feedback_box = _FeedbackBoxEl()

    def query_selector(self, sel):
        return self.feedback_box if sel == "textarea" else None


def test_bounced_page_yields_off_chat_not_no_input_under_the_real_find_input(tmp_path, monkeypatch):
    """**形态保真**：验收 1 的坏形态一侧走生产 `find_input`（不是替身），因为本票的现场问题
    恰恰是「生产的选择器一族会不会命中反馈框」——替身答不了这条。

    两层各证一半：① 元素层——生产 `find_input` 面对存证那页的 DOM 一条都命中不了（裸 textarea
    兜底命中的正是反馈框，被浮层排除摘掉），没有这层，199 字就打进站点的意见反馈渠道；
    ② 形态层——整条流程落 `no_send` + `off_chat` 并进补投台账，而**不是** `no_input`：
    守卫若排在 find_input 之后，这一岗会掉进 #42「聊天页加载态未就绪」的语义里，
    归因与台账双双丢失（这也是把守卫挪到查找之前的唯一理由）。"""
    page = _BouncedFeedbackPage()
    # ① 元素层：生产实现 + 存证形态 ⇒ 反馈框被排除，全族落空
    assert mod.find_input(page) is None, "反馈浮层里的 textarea 不许被当成沟通输入框"
    assert page.feedback_box.overlay_probes >= 1, "它确实被祖先链探针查过（落空的原因是排除，不是没找到）"

    # ② 形态层：走完整发送段（find_input 用生产实现）
    el = _GreetEl()  # 不会被消费：守卫在找输入框之前就拦下了
    outcome, results, state, _, typed = _run_guard_case(
        tmp_path, monkeypatch, page, el, send_btn="NONE", real_find_input=True
    )
    assert [r["status"] for r in results] == ["no_send"], "不得落 no_input（那是 #42 的加载态语义）"
    assert results[0].get("reason") == "off_chat"
    assert el.fills == [] and page.feedback_box.fills == [] and typed["human_type"] == 0, "一个字都不许进页面"
    assert state.get("retry_pending"), "打字前拦下的岗要能被下一场捞回"
    assert outcome == "next"


def test_off_chat_guard_books_the_job_into_the_cross_session_ledger(tmp_path, monkeypatch):
    """验收 3 拍板口径「只捞确定没打一个字的」：守卫触发 ⇒ 进 #55 跨会话补投台账。
    条目形态照 `_retry_snapshot` 的既有契约（`_RETRY_SNAPSHOT_FIELDS` + jdText/timeout_at/attempts），
    并置 `retry_touched`（落盘开关）；落盘仍由既有 `_save_retry_pending_state` 负责
    （dry-run 不写，属 #55 领地，本票不重复钉），封顶仍由 `apply_retry_outcome` 管。"""
    page = _SendPage(_BOUNCED_URL)
    _, results, state, _, _ = _run_guard_case(tmp_path, monkeypatch, page, _FeedbackBoxEl(), send_btn="NONE")
    entries = state.get("retry_pending") or []
    assert len(entries) == 1, "本岗恰好入台账一条"
    assert entries[0]["attempts"] == 0, "首次入台账不烧 attempts"
    assert entries[0]["jobName"] == "测试岗" and entries[0]["encryptJobId"] == "e1"
    assert "jdText" in entries[0] and "timeout_at" in entries[0], "自包含字段集照 #55 契约"
    assert state["retry_touched"] is True, "台账被写过 ⇒ 落盘开关必须置位，否则收尾不落盘"
    # `retry_new_count` 是 #55 收尾行「本场新增**超时** M 个」的取数口（runbook 步骤 6 ⑤），
    # off_chat 不是超时 ⇒ 不许掺进去，否则终端读数会把「打字前被形态守卫拦下」说成「agent 超时」。
    assert "retry_new_count" not in state, "off_chat 不得计入超时口径（收尾行会误报成因）"
    assert results[0]["status"] == "no_send"


def test_post_typing_no_send_stays_out_of_the_retry_ledger(tmp_path, monkeypatch):
    """拍板口径的另一半：字**已经打进**沟通输入框之后才落空的 no_send 一律**不**自动补投
    （可能已实际发出，自动重投有双发风险），只靠 report 的 `no_send_reasons` 读数看见它。
    两种成因都钉：找不到发送按钮 / 发送那一下点击失败。"""
    for reason, kwargs in [
        ("send_btn_missing", {"send_btn": "NONE"}),
        ("send_click_failed", {"send_click": "FAIL"}),
    ]:
        page = _SendPage(_CHAT_URL)  # 形态合法 ⇒ 守卫放行，一路走到打字与发送
        el = _GreetEl()
        # 本条负对照**成立的理由**要钉死：替身代表 textarea ⇒ 问 `isContentEditable` 老实回 False。
        # 若只靠 `bool("TEXTAREA|")` 这类字符串巧合撑起，判据一换（#68）这条就会以错的姿态绿。
        assert el.evaluate("el => !!el.isContentEditable") is False, f"{reason} 的前提：它不是 contenteditable"
        _, results, state, calls, typed = _run_guard_case(tmp_path, monkeypatch, page, el, **kwargs)
        assert [r["status"] for r in results] == ["no_send"], reason
        assert results[0].get("reason") == reason, "两种成因要能分辨（读数按它分桶）"
        assert len(el.fills) + typed["human_type"] == 1, f"{reason} 的前提是字已经打进去了"
        assert not state.get("retry_pending"), f"{reason} 不进补投台账（双发风险）"
        assert "retry_touched" not in state, f"{reason} 不该动台账落盘开关"
        assert calls["evidence"] == ["live-no-send-1"], "存证仍用发送段旧标签，别和 off_chat 混"


def test_contenteditable_chat_input_falls_back_to_enter_not_no_send(tmp_path, monkeypatch):
    """#68 那条恒不触发分支的**第一条**用例证据：聊天页输入框是 `<div contenteditable="true">`
    且找不到独立发送按钮 ⇒ 走 Enter 把招呼发出去，**不记** `no_send` / `send_btn_missing`。
    修之前判据是 `"contenteditable" in (el.tagName + '|' + 属性值)`——`<div contenteditable="true">`
    实得 `"DIV|true"`，串里永不含那个词 ⇒ `elif is_contenteditable:` 永远不进，直接落 else。
    本票的诞生原因正是「分支被测试永久绕过而全绿」，所以这条用例是这支代码在本仓第一次被执行；
    负对照见上一条（textarea ⇒ 照旧落 `send_btn_missing`，#62 口径不动）。"""
    page = _SendPage(_CHAT_URL)
    el = _GreetEl(tag="DIV|true", editable=True)  # 真机聊天页形态：contenteditable 容器，无独立按钮
    outcome, results, state, calls, _typed = _run_guard_case(tmp_path, monkeypatch, page, el, send_btn="NONE")
    assert page.keyboard.pressed == ["Enter"], "Enter 兜底必须真的按下——本票修的就是这一支恒不触发"
    assert [r["status"] for r in results] == ["applied"], f"聊天页不该记 no_send：{results}"
    assert len(el.fills) == 1, "先打字再 Enter（Enter 是发送动作，不是输入动作）"
    assert state["applied"] == 1, "Enter 发出后照常计成品"
    assert not state.get("retry_pending"), "已发出的岗不进补投台账"
    assert calls["evidence"] == [], "走通兜底就不该再存 no-send 现场存证"
    assert outcome == "next"


def test_no_send_reason_stats_keeps_four_keys_and_buckets_by_cause():
    """#62 机读面：四键**恒在**（0 也写出来，照 #65 `recommend_*` / #56 `no_jd_classes` 的口径）；
    非 `no_send` 的条目一律不计；缺 reason 的历史条目与未知成因都落 `other`，不静默吞。"""
    assert mod._no_send_reason_stats([]) == {
        "off_chat": 0,
        "send_btn_missing": 0,
        "send_click_failed": 0,
        "other": 0,
    }, "零形态四键恒在"
    rows = [
        {"status": "no_send", "reason": "off_chat"},
        {"status": "no_send", "reason": "off_chat"},
        {"status": "no_send", "reason": "send_btn_missing"},
        {"status": "no_send"},  # 历史条目/桩：无归因
        {"status": "no_send", "reason": "某个没人登记过的成因"},
        {"status": "applied", "reason": "off_chat"},  # 非 no_send：不计
        {"status": "no_input"},
    ]
    assert mod._no_send_reason_stats(rows) == {
        "off_chat": 2,
        "send_btn_missing": 1,
        "send_click_failed": 0,
        "other": 2,
    }


def test_build_report_carries_no_send_reasons():
    """report.json 的新读数键恒在（本场没有 no_send 也写零形态）——收尾汇报与「这批岗能不能
    自动补投」都读它；off_chat 那批已进台账会被下一场捞回，另两桶只能靠这个读数看见。"""
    state = {"applied": 0, "direct_fallback": 0, "keyword_skips": []}
    assert mod.build_report(["q"], state, [])["no_send_reasons"] == {
        "off_chat": 0,
        "send_btn_missing": 0,
        "send_click_failed": 0,
        "other": 0,
    }
    rows = [{"status": "no_send", "reason": "off_chat"}, {"status": "no_send", "reason": "send_click_failed"}]
    got = mod.build_report(["q"], state, rows)["no_send_reasons"]
    assert got["off_chat"] == 1 and got["send_click_failed"] == 1 and got["send_btn_missing"] == 0
