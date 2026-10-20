#!/usr/bin/env python3
"""`_wedge_ledger.py` 的模块契约测试（#57 验收 1）。

`test_live_apply.py` 里那 36 条台账用例钉的是**走 live_apply 别名的调用点行为**（开跑呈报、
三处消费点跳过、report 读数）。本文件钉只能在归属模块上成立的那一层：台账本体的纯函数性质、
坏数据的自愈方向，以及「patch 别名拦不到模块内部互调」这条接线形状本身——最后这条是 #57
拆分的固有代价，不钉住就会在几个月后变成「用例绿着但什么都没测」。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _wedge_ledger as wl  # noqa: E402
import live_apply as la  # noqa: E402  # 别名面：与 wl 必须是同一批对象

# --- 开关：默认开，关只认 0/off（方向论证在函数 docstring，这里钉形状） ---


@pytest.mark.parametrize("raw", ["", "1", "on", "yes", "ofs", "false", "no", "OFF-ish"])
def test_skip_switch_defaults_to_on_for_anything_but_the_off_words(raw):
    assert wl._wedge_skip_enabled({"LIVE_WEDGE_SKIP": raw}) is True


@pytest.mark.parametrize("raw", ["0", "off", " OFF ", "0 "])
def test_skip_switch_only_recognises_zero_and_off_as_off(raw):
    assert wl._wedge_skip_enabled({"LIVE_WEDGE_SKIP": raw}) is False


def test_skip_switch_unrecognised_value_shows_up_in_the_log(monkeypatch, capsys):
    """写错值不许当哑事：按默认「开」走，但必须在开跑第一行露出来（#54「呈报事实」）。"""
    assert wl._wedge_skip_enabled({"LIVE_WEDGE_SKIP": "diabled"}) is True
    assert "LIVE_WEDGE_SKIP='diabled'" in capsys.readouterr().out


def test_skip_switch_absent_key_never_prints():
    """变量压根没设 ＝ 静默按默认开（与「设了但写错」是两件事，只有后者该响）。"""
    assert wl._wedge_skip_enabled({}) is True


# --- 读写：坏形态自愈，不静默 ---


def test_load_falls_back_to_empty_list_on_any_bad_shape(tmp_path, capsys):
    """文件缺失 / 空 / 坏 JSON / 顶层不是列表 ⇒ 一律空台账 + 可看见的 warn（#51 reported 同款自愈）。
    静默回落会让「一场都没楔过」和「台账被写坏」读起来一样。"""
    missing = tmp_path / "missing.json"
    assert wl._load_wedge_skipped(missing) == []

    for idx, content in enumerate(("{ not json", '{"not":"a list"}', "null", "[]"), start=1):
        p = tmp_path / f"bad{idx}.json"
        p.write_text(content, encoding="utf-8")
        out = capsys.readouterr()  # 清掉上一轮
        assert wl._load_wedge_skipped(p) == [], p
        del out


def test_save_then_load_roundtrip_is_atomic(tmp_path):
    entries = [{"encryptJobId": "e1", "jobName": "岗一", "wedge_count": 2, "sessions": ["20260929-201325"]}]
    path = tmp_path / "wedge_skipped.json"
    wl._save_wedge_skipped(path, entries)
    assert wl._load_wedge_skipped(path) == entries
    assert not (tmp_path / f"{path.name}.tmp").exists(), "原子写的临时文件没被换掉"


# --- 归因门槛与合并（纯函数） ---


def _marker(**over):
    data = {
        "last_label": "click:mouse-click:pre",
        "last_eid": "e1",
        "last_job": "岗一",
        "last_brand": "厂一",
        "in_job": True,
    }
    data.update(over)
    return data


def test_entry_gate_is_narrow_over_every_bad_shape():
    """宁窄勿宽：`in_job` 必须**恰好是 True**、eid 必须是非空白字符串。两岗之间的楔死（会话级
    节拍）不得被记到上一岗头上——那是 B1 唯一会造成「误跳一个正常岗」的入口。"""
    assert wl._wedge_entry_from_marker("20260929-201325", _marker()) is not None
    for over in (
        {"in_job": False},
        {"in_job": None},
        {"in_job": "true"},  # 字符串真值不放行
        {"in_job": 1},
        {"last_eid": ""},
        {"last_eid": "   "},
        {"last_eid": None},
    ):
        assert wl._wedge_entry_from_marker("s", _marker(**over)) is None, over


def test_split_already_applied_keeps_the_ledger_off_successfully_applied_jobs():
    """已投出的岗不入台账（#58 T2）：击杀点在 `wait_sent()` 时心跳仍带刚投出那个岗的 eid，
    照单全收就是把成功的人销档。eid 两侧都归一成去空白字符串。"""

    def mk(eid):
        return {"encryptJobId": eid, "jobName": "岗", "session": "s1", "wedge_count": 1}

    items = [mk(" e1 "), mk("e2"), mk("E3 ")]
    # 条目侧带空白也必须被拦（两侧都归一，守卫不依赖上游卫生）；applied_ids 里的非字符串/空值不参与匹配
    bookable, blocked = wl._split_already_applied_wedge_items(items, {"e1", None, "", 0, "e3"})
    assert [i["encryptJobId"] for i in blocked] == [" e1 "], blocked
    assert [i["encryptJobId"] for i in bookable] == ["e2", "E3 "], bookable
    # 大小写敏感是刻意的：eid 是混合字母数字串，忽略大小写会**多拦**（把一个没投过的岗当成
    # 已投出的跳掉），比少拦更糟——这条方向要钉住，别被"顺手 lower()"改掉。
    assert [i["encryptJobId"] for i in wl._split_already_applied_wedge_items([mk("E3 ")], {"e3"})[1]] == []


def test_merge_is_idempotent_per_source_session(tmp_path):
    """来源场名是幂等键：同一场重复回灌不涨计数（否则一次楔死会被念成多次楔）。"""
    entries = []
    item = {"encryptJobId": "e1", "jobName": "岗一", "session": "s1", "wedge_count": 1}
    merged1, stats1 = wl._merge_wedge_ledger(entries, [dict(item)])
    merged2, stats2 = wl._merge_wedge_ledger(merged1, [dict(item)])
    assert (stats1["added"], stats1["duplicates"]) == (1, 0) and len(merged1) == 1
    assert (stats2["added"], stats2["duplicates"]) == (0, 1), "同场重复回灌不该再计一次"
    assert merged2 == merged1, "幂等跳过改写了台账"
    # 另一场再楔一次：合并成一条、累计次数、最近场与来源清单都跟着走
    merged3, stats3 = wl._merge_wedge_ledger(merged2, [dict(item, session="s2", wedge_count=1)])
    assert len(merged3) == 1 and merged3[0]["wedge_count"] == 2, merged3
    assert merged3[0]["session"] == "s2" and merged3[0]["sessions"] == ["s1", "s2"], merged3[0]
    assert (stats3["added"], stats3["merged"], stats3["duplicates"]) == (0, 1, 0), stats3
    assert entries == [], "merge 改写了入参（docstring 声称纯函数）"


def test_backfill_scans_only_timestamped_session_dirs(tmp_path):
    """只认 `<8位>-<6位>` 形态的会话目录：把 videos / shots 之类目录也扫进去就会读别人的文件
    （`sweep_expired_data` 同一条约束）。"""
    base = tmp_path / "live_session"
    (base / "20260929-201325").mkdir(parents=True)
    (base / "not_a_session").mkdir()
    (base / "20260929-201325" / wl.WATCHDOG_KILLED).write_text(json.dumps(_marker()), encoding="utf-8")
    (base / "not_a_session" / wl.WATCHDOG_KILLED).write_text(json.dumps(_marker(last_eid="e-bad")), encoding="utf-8")
    ledger = base.parent / "wedge_skipped.json"
    wl._backfill_wedge_ledger(base, "20260930-000000", ledger, set(), log=lambda *_: None)
    assert [e["encryptJobId"] for e in wl._load_wedge_skipped(ledger)] == ["e1"], "非会话目录被扫进来了"


# --- 接线形状：别名不是复制，也不是第二道拦截点 ---


def test_live_apply_aliases_are_the_same_objects():
    for name in ("_wedge_skip_enabled", "_load_wedge_skipped", "_backfill_wedge_ledger", "WEDGE_LEDGER_FILE"):
        assert getattr(la, name) is getattr(wl, name), f"{name} 的别名不再是 _wedge_ledger 里的同一个对象"


def test_patching_the_live_apply_alias_does_not_reach_internal_calls(tmp_path, monkeypatch):
    """别名接线（#57 的迁移形态）的代价必须有人看着：`_backfill_wedge_ledger` 内部读台账走的是
    `_wedge_ledger` 自己的命名空间，改绑 `live_apply` 上的别名对它**静默无效**。今天没有用例依赖
    它，但「打了桩却没生效、测试照样绿」正是这类接线最坏的失效形态——所以正反各证一次。"""
    ledger = tmp_path / "wedge_skipped.json"
    base = tmp_path / "live_session"
    (base / "20260929-201325").mkdir(parents=True)
    (base / "20260929-201325" / wl.WATCHDOG_KILLED).write_text(json.dumps(_marker()), encoding="utf-8")
    wl._save_wedge_skipped(ledger, [{"encryptJobId": "old", "jobName": "旧岗", "sessions": ["s0"], "wedge_count": 1}])

    seen_on_alias = []
    monkeypatch.setattr(la, "_load_wedge_skipped", lambda path: seen_on_alias.append(path) or [])
    wl._backfill_wedge_ledger(base, "20260930-000000", ledger, set(), log=lambda *_: None)
    assert seen_on_alias == [], "别名桩被模块内部调用了——接线形态与 docstring 声称的不一致"
    assert [e["encryptJobId"] for e in wl._load_wedge_skipped(ledger)] == ["old", "e1"], "真实读盘路径没走通"

    seen_on_owner = []
    monkeypatch.setattr(wl, "_load_wedge_skipped", lambda path: seen_on_owner.append(path) or [])
    wl._backfill_wedge_ledger(base, "20260930-000001", ledger, set(), log=lambda *_: None)
    assert seen_on_owner, "打归属模块也拦不到——本用例的对照组失效"


def test_internal_call_reads_the_owner_namespace_not_the_alias(tmp_path, monkeypatch):
    """同一件事的第二面：模块内部互调解析到 `_wedge_ledger.__dict__`。"""
    assert wl._backfill_wedge_ledger.__globals__ is wl.__dict__
    assert la._backfill_wedge_ledger is wl._backfill_wedge_ledger
