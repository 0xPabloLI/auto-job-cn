#!/usr/bin/env python3
"""#75 入站水位线的判据与形态层。

夹具取自真实读数形状：`private/data/followup_report.json`（68 条 wapi 会话）与
`private/data/inbound_session/20261021-191140/report.json`（`lastTS` 是毫秒 int、
`lastFromId` 与 `myUserId` 是同一套 id）；字段名一律按 `platforms/chat_first.py` 的
`parse_friend_entries` 实际产出的键，不臆造。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _inbound_watermark as mod  # noqa: E402

MY_UID = 780045835
HR_UID = 587222408


def _conv(eid="e1", uid=None, ts=1792000000000, from_id=HR_UID, status="hr_replied", **over):
    """一条 wapi 会话的最小真实形状（键名与 `parse_friend_entries` 一致）。"""
    conv = {"encryptJobId": eid, "encryptUid": uid, "lastTS": ts, "lastFromId": from_id, "status": status}
    conv.update(over)
    return conv


def _base(keys=(), max_ts=None, snapshot_at="2026-10-21 09:54:58", profile="blockchain", state=mod.STATE_OK):
    """一个「已有基准」的读侧形状（用例直接构造，绕开写盘）。"""
    return mod.empty_base(profile) | {
        "state": state,
        "snapshotAt": snapshot_at,
        "keys": frozenset(keys),
        "maxLastTS": max_ts,
    }


# --- 会话键：encryptJobId ∪ encryptUid（票面口径） ---


def test_session_key_prefers_job_id_then_uid():
    assert mod.session_key(_conv(eid="e1", uid="u9")) == "e1"
    assert mod.session_key(_conv(eid=None, uid="u9")) == "u9"


def test_session_key_normalizes_int_uid():
    """wapi 的 `uid` 是 int、`encryptJobId` 是串：统一成 str 才比得动（否则 740868320 与
    "740868320" 会被读成两条不同会话，键集合永远对不上）。"""
    assert mod.session_key(_conv(eid="", uid=740868320)) == "740868320"


def test_session_key_missing_both_is_none():
    """DOM 降级卡片两样加密 id 都没有（#30 探测结论）⇒ 无键，由调用方单独计数，不静默丢。"""
    assert mod.session_key({"lastTS": 1}) is None
    assert mod.session_key({"encryptJobId": "", "encryptUid": None}) is None


# --- 一条会话的**全部**可用键（#75 review 条目 I：uid→eid 回填不算新会话） ---


def test_session_keys_collects_both_id_kinds():
    """拍板原文是 `encryptJobId` **∪** `encryptUid`：两个都有时两个都是这条会话的键。
    单键（eid 优先）在 boss 回填 eid 的那一刻会换键 ⇒ 同一条对话被念成两条。"""
    assert mod.session_keys(_conv(eid="e1", uid="u9")) == ("e1", "u9")
    assert mod.session_keys(_conv(eid=None, uid="u9")) == ("u9",)
    assert mod.session_keys(_conv(eid="e1", uid=None)) == ("e1",)
    assert mod.session_keys({"lastTS": 1}) == ()
    assert mod.session_keys(_conv(eid="e1", uid="e1")) == ("e1",), "两类键同值时只记一次"


def test_uid_key_flipping_to_eid_is_not_a_new_conversation(tmp_path):
    """`platforms/chat_first.py:950` 的 `_conv_record` 会给归到投递记录的会话**回填** `encryptJobId`：
    同一条 HR 会话先只有 uid 键、下一场变成 eid+uid 键。判「新会话」要看**任一**已知键——
    否则每回填一次就重复提醒一次同一条对话（review 条目 I 的假阳性）。"""
    path = tmp_path / "wm.json"
    only_uid = {"encryptUid": "u1", "lastTS": 1792000000000, "status": "hr_replied"}
    assert mod.advance_watermark(path, [only_uid], mod.empty_base("blockchain"), profile="blockchain")["written"]
    backfilled = {**only_uid, "encryptJobId": "e1"}
    base = mod.read_watermark(path, "blockchain")
    assert mod.due_reason(backfilled, base, MY_UID) is None, "uid 已知 ⇒ 不是新会话，eid 回填不算"
    assert mod.split_due([backfilled], base, MY_UID)["newCount"] == 0
    # 正对照：uid 也换了（真的没见过的会话）仍要报成新会话，别把判据修成「一律不算」
    assert mod.due_reason({**backfilled, "encryptUid": "u2"}, base, MY_UID) == "new"


def test_baseline_records_both_id_kinds(tmp_path):
    """写基准时两类键都记（与 I 的「任一已知键 ⇒ 不是新的」配套，也界定 A 的「可用键」）。"""
    path = tmp_path / "wm.json"
    res = mod.advance_watermark(
        path, [_conv(eid="e1", uid="u1"), _conv(eid=None, uid="u2")], mod.empty_base("blockchain"), profile="blockchain"
    )
    assert res["written"] and res["keys"] == 3, res
    assert mod.read_watermark(path, "blockchain")["keys"] == frozenset({"e1", "u1", "u2"})


# --- 读侧四种形态（验收④：首跑 / 损坏 / 串档 / 正常都有明确读数） ---


def test_missing_file_is_first_run_and_creates_nothing(tmp_path):
    path = tmp_path / "inbound_watermark.json"
    base = mod.read_watermark(path, "blockchain")
    assert base["state"] == mod.STATE_FIRST_RUN
    assert base["keys"] == frozenset() and base["snapshotAt"] is None
    assert not path.exists(), "读侧一个字都不写——水位线只能由 --mark-read 建立"


def test_ok_round_trip_with_profile_and_timestamps(tmp_path):
    path = tmp_path / "inbound_watermark.json"
    convs = [_conv(eid="e1", uid="u1"), _conv(eid="e2", ts=1792999999000), _conv(eid=None, uid="u3", ts=5)]
    wrote = mod.advance_watermark(path, convs, mod.empty_base("blockchain"), profile="blockchain")
    # 4 个键不是 3 个：条目 I 之后一条会话贡献它**全部**可用键（e1 与 u1 是同一条对话的两个键），
    # 只记 eid 的话，boss 回填 eid 之前那条对话的 uid 键就永远进不了基准（假阳性来源）。
    assert wrote["written"] and wrote["keys"] == 4
    base = mod.read_watermark(path, "blockchain")
    assert base["state"] == mod.STATE_OK
    assert base["keys"] == frozenset({"e1", "u1", "e2", "u3"}), "uid 也收（票面口径 ∪）"
    assert base["maxLastTS"] == 1792999999000
    assert base["snapshotAt"] == wrote["snapshotAt"], "没给快照时刻时回落到写入时刻，读数不能是空的"
    assert json.loads(path.read_text(encoding="utf-8"))["profile"] == "blockchain"


def test_advance_without_profile_label_records_default(tmp_path):
    """legacy 单 profile（注册表没名字）也要有明确的 `profile` 字段——缺了它下次读就是坏形态。"""
    path = tmp_path / "wm.json"
    mod.advance_watermark(path, [_conv()], mod.empty_base(), profile=None)
    assert json.loads(path.read_text(encoding="utf-8"))["profile"] == "default"
    assert mod.read_watermark(path, None)["state"] == mod.STATE_OK


def test_corrupt_json_is_reported_and_file_left_untouched(tmp_path):
    """#64 口径：损坏文件原样保留。这里断言的是**字节没变**，不是「内容还能解析」。"""
    path = tmp_path / "wm.json"
    raw = "{这不是 JSON"
    path.write_text(raw, encoding="utf-8")
    base = mod.read_watermark(path, "blockchain")
    assert base["state"] == mod.STATE_CORRUPT
    assert base["detail"], "得说清为什么读不出，只报「损坏」等于让人无从下手"
    assert path.read_text(encoding="utf-8") == raw
    assert base["keys"] == frozenset(), "读不懂就当没有基准，不拿半截数据去比"


def test_bad_shapes_are_all_corrupt(tmp_path):
    """version 不认识 / keys 不是串数组 / 缺 profile / maxLastTS 混型——都不猜、都算读不出。"""
    cases = {
        "version 是未来值": {"version": 2, "profile": "blockchain", "snapshotAt": "x", "keys": [], "maxLastTS": None},
        "keys 不是数组": {
            "version": 1,
            "profile": "blockchain",
            "snapshotAt": "x",
            "keys": {"a": 1},
            "maxLastTS": None,
        },
        "keys 混型": {"version": 1, "profile": "blockchain", "snapshotAt": "x", "keys": ["ok", 3]},
        "缺 profile": {"version": 1, "snapshotAt": "x", "keys": [], "maxLastTS": None},
        "maxLastTS 是串": {"version": 1, "profile": "blockchain", "snapshotAt": "x", "keys": [], "maxLastTS": "1792"},
        "顶层是数组": [1, 2],
        "snapshotAt 是空串": {"version": 1, "profile": "b", "snapshotAt": "", "keys": []},
    }
    # 文件名用序号，不用 `abs(hash(label))`：内建 `hash` 受 PYTHONHASHSEED 影响，同一份用例两次
    # 运行会落到不同文件名上——失败现场不可复现，`-n auto` 分片与覆盖率也跟着漂。
    for n, (label, payload) in enumerate(cases.items(), 1):
        path = tmp_path / f"wm-{n:02d}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        base = mod.read_watermark(path, "blockchain")
        assert base["state"] == mod.STATE_CORRUPT, f"{label} 必须落 corrupt，实得 {base}"
        assert path.exists(), f"{label}：读不懂也不许把原文件删了"


def test_other_profile_baseline_is_not_cross_read(tmp_path):
    """profile 切换（#28 的扁平命名也可能让两个档共用一个文件名，如注册表 default 换人）：
    各有各的基准，绝不串档——读不懂别人那批键，就当没有基准。"""
    path = tmp_path / "wm.json"
    mod.advance_watermark(path, [_conv(eid="别人的岗")], mod.empty_base(), profile="ai")
    base = mod.read_watermark(path, "blockchain")
    assert base["state"] == mod.STATE_PROFILE_MISMATCH
    assert base["storedProfile"] == "ai"
    due = mod.split_due([_conv(eid="别人的岗")], base, MY_UID)
    assert due["newCount"] is None, "串档时不报条数：拿别人的键算出来的 N 是假读数"
    assert not mod.is_usable(base) and mod.is_usable(_base())


# --- 「新增」判据：键集合差 + 时间兜底 + 末条归属（验收③） ---


def test_unseen_key_is_new_and_seen_key_is_not():
    base = _base(keys=["e1"], max_ts=1792000000000)
    due = mod.split_due([_conv(eid="e1"), _conv(eid="e2", ts=1792000000000)], base, MY_UID)
    assert due["newCount"] == 1 and due["dueKeys"] == frozenset({"e2"})
    assert due["newConversations"] == 1 and due["updatedConversations"] == 0


def test_seen_key_with_later_timestamp_is_updated():
    """老会话里对方新发一条 ⇒ 要算进来（票面第 3 条的第二臂）。"""
    base = _base(keys=["e1"], max_ts=1792000000000)
    due = mod.split_due([_conv(eid="e1", ts=1792999999000)], base, MY_UID)
    assert due["newCount"] == 1 and due["updatedConversations"] == 1
    assert mod.due_reason(_conv(eid="e1", ts=1792999999000), base, MY_UID) == "updated"


def test_same_millisecond_new_conversation_is_caught_by_key_set():
    """同毫秒多条为什么不能只靠 lastTS：新会话与基准同毫秒时时间臂完全不响。"""
    base = _base(keys=["e1"], max_ts=1792000000000)
    same_ms = _conv(eid="e2", ts=1792000000000)
    assert mod.due_reason(same_ms, base, MY_UID) == "new", "键集合差接住了它"
    assert mod.due_reason(_conv(eid="e1", ts=1792000000000), base, MY_UID) is None, "同毫秒的旧会话不误报"


def test_our_own_last_message_is_not_due():
    """验收③前半：自己刚发出去的那条不算「该读」（`fromId == myUserId`，与 boss 判 awaiting 同源）。"""
    base = _base(keys=[], max_ts=1792000000000)
    ours = _conv(eid="e1", from_id=MY_UID, status="awaiting")
    assert mod.due_reason(ours, base, MY_UID) is None
    assert mod.split_due([ours], base, MY_UID)["newCount"] == 0
    assert mod.above_line(ours, base, MY_UID) is False


def test_from_id_compare_is_not_type_sensitive():
    """`userId` 在 wapi 里是 int、在别的读数里可能是串：比 str 才不把「同一个人」读成两个人。"""
    assert mod.last_message_is_ours({"lastFromId": 780045835}, "780045835") is True
    assert mod.last_message_is_ours({"lastFromId": "780045835"}, 780045835) is True
    assert mod.last_message_is_ours({"lastFromId": HR_UID}, MY_UID) is False


def test_status_conclusion_is_primary_and_no_evidence_passes_through():
    """`status` 缺席（老报告回放、DOM 降级没有 fromId）时按已有结论读；两样都没有 ⇒ 按「不是我方」
    放行——宁可多列一条给人看，也不静默丢那条可能真该读的。"""
    assert mod.last_message_is_ours({"status": "awaiting"}, None) is True
    assert mod.last_message_is_ours({"status": "hr_replied"}, None) is False
    assert mod.last_message_is_ours({}, None) is False
    base = _base(keys=[])
    assert mod.due_reason(_conv(eid="e1", from_id=None, status="awaiting"), base, None) is None
    assert mod.due_reason(_conv(eid="e1", from_id=None, status="hr_replied"), base, None) == "new"


# --- 判据同源（#75 条目 C）：取 boss 的结论，取不到才自己比一次 ---


def test_boss_status_conclusion_wins_over_a_re_computed_from_id():
    """本仓的结论已落在 `status` 上（`awaiting` ⇔ 末条是我方、`hr_replied` ⇔ 末条是对方，
    见 `platforms/chat_first.inbound_waiting`）。两边不一致时**以 status 为准**：在水位线里再比一次
    `fromId == my_user_id` 是第二份判据，boss 那边一改（比如 `lastMessageInfo` 缺失时保守归
    awaiting 那条）这里就漂，而且漂了没人红。"""
    assert mod.last_message_is_ours(_conv(eid="e1", from_id=MY_UID, status="awaiting"), MY_UID) is True
    assert mod.last_message_is_ours(_conv(eid="e1", from_id=HR_UID, status="hr_replied"), MY_UID) is False
    # 不一致的那一格：会话行自带结论，调用方另传的身份不得把它翻过来
    assert mod.last_message_is_ours(_conv(eid="e1", from_id=MY_UID, status="hr_replied"), MY_UID) is False
    assert mod.last_message_is_ours(_conv(eid="e1", from_id=HR_UID, status="awaiting"), MY_UID) is True


def test_candidate_dict_without_status_uses_the_from_id_arm():
    """`reply_inbound.classify` 给的候选 dict **没有 `status` 键**（那是呈报形状，不是会话行），
    所以「候选逐列打勾」这条链路走的是退回臂——这个兜底此前对候选这个消费者零覆盖（条目 C）。"""
    cand = {"encryptJobId": "e1", "lastFromId": MY_UID, "lastTS": 1792000000000}
    assert "status" not in cand, "候选 dict 就是没有结论，才需要退回臂"
    assert mod.last_message_is_ours(cand, MY_UID) is True
    assert mod.last_message_is_ours({**cand, "lastFromId": HR_UID}, MY_UID) is False
    base = _base(keys=[])
    assert mod.above_line(cand, base, MY_UID) is False, "我方末条 ⇒ 线上没有它"
    assert mod.above_line({**cand, "lastFromId": HR_UID}, base, MY_UID) is True


def test_unkeyed_conversations_are_counted_not_dropped():
    """键缺失：不进 N（记不进基准，算进去就永远降不下来），但必须单独计数、可念出来。"""
    base = _base(keys=["e1"], max_ts=None)
    due = mod.split_due([_conv(eid="e1"), {"lastTS": 1792999999000, "status": "hr_replied"}], base, MY_UID)
    assert due["newCount"] == 0 and due["unkeyed"] == 1
    assert due["usable"] is True


# --- 接线出口 `attach_readings`（#75 条目 B：三个消费者共用同一个函数） ---


def test_attach_readings_lands_both_keys_and_writes_nothing(tmp_path):
    """两个恒在键**就地**落进读数、返回 `(base, due)` 供打印，且这个函数一个字都不写盘。
    写成返回值让调用方自己塞，就有第二个消费者忘了塞（本条 red 的真身：`live_apply` 收尾那条
    路径整份重写同一个报告、不带这两键）。"""
    report = {
        "myUserId": MY_UID,
        "conversations": [_conv(eid="e1"), _conv(eid="e2", ts=1792999999000)],
    }
    path = tmp_path / "wm.json"
    mod.advance_watermark(path, [_conv(eid="e1")], mod.empty_base("blockchain"), profile="blockchain")
    base, due = mod.attach_readings(report, profile="blockchain", path=path)
    assert report["newSinceWatermark"] == 1 and report["watermark"]["state"] == mod.STATE_OK
    assert report["watermark"]["keys"] == 1 and due["newConversations"] == 1
    assert base["state"] == mod.STATE_OK
    assert json.loads(path.read_text(encoding="utf-8"))["keys"] == ["e1"], "只读消费者不许推进基准"


def test_attach_readings_without_a_wired_path_still_lands_both_keys():
    """调用方没接线（`path=""`，老的用例形状）⇒ 按首跑落键，两个键照样恒在，不抛。"""
    report = {"conversations": [_conv(eid="e1")]}
    base, due = mod.attach_readings(report, profile="blockchain", path="")
    assert report["newSinceWatermark"] is None and report["watermark"]["state"] == mod.STATE_FIRST_RUN
    assert base["state"] == mod.STATE_FIRST_RUN and due["newCount"] is None


# --- 基准不可用时的读数（验收④） ---


def test_first_run_reports_no_baseline_not_everything(tmp_path):
    """首跑最容易撒的谎：没有基准 ⇒ 把 68 条全报成「新增 68 条」。"""
    base = mod.read_watermark(tmp_path / "wm.json", "blockchain")
    convs = [_conv(eid=f"e{n}") for n in range(68)]
    due = mod.split_due(convs, base, MY_UID)
    assert due["newCount"] is None and due["dueKeys"] == frozenset()
    assert mod.due_reason(convs[0], base, MY_UID) is None
    line = mod.headline(base, due)
    assert "无从比较" in line and "68" not in line, line


def test_corrupt_and_mismatch_yield_no_count(tmp_path):
    """两种「读不出」都不报条数——报一个凭半截数据算出来的 N 比不报更糟。"""
    broken = tmp_path / "corrupt.json"
    broken.write_text("{坏文件", encoding="utf-8")
    base = mod.read_watermark(broken, "blockchain")
    assert base["state"] == mod.STATE_CORRUPT
    assert mod.split_due([_conv(eid="e2", ts=1)], base, MY_UID)["newCount"] is None

    shared = tmp_path / "wm.json"
    mod.advance_watermark(shared, [_conv()], mod.empty_base(), profile="ai")
    mismatched = mod.read_watermark(shared, "blockchain")
    assert mismatched["state"] == mod.STATE_PROFILE_MISMATCH
    assert mod.split_due([_conv(eid="e2", ts=1)], mismatched, MY_UID)["newCount"] is None


# --- 报告对象与措辞（恒在键 / 两份报告同源） ---


def test_watermark_reading_is_json_safe_and_zero_filled(tmp_path):
    """报告里要的是**数量**不是 frozenset；0 也写出来（口径同 #62 `no_send_reasons`）。"""
    base = mod.read_watermark(tmp_path / "wm.json", "blockchain")
    due = mod.split_due([_conv()], base, MY_UID)
    reading = mod.watermark_reading(base, due)
    json.dumps(reading)  # 抛异常就是形态不对
    assert reading["state"] == mod.STATE_FIRST_RUN and reading["snapshotAt"] is None
    assert reading["keys"] == 0 and reading["unkeyed"] == 0, "数量是 int，不是集合"


def test_headline_wording_per_state():
    ok = _base(keys=["e1", "e2"], max_ts=1792000000000, snapshot_at="2026-10-21 09:54:58")
    line = mod.headline(ok, mod.split_due([_conv(eid="e9", ts=1792999999000)], ok, MY_UID))
    assert "较上次读数新增 1 条" in line and "水位线 2026-10-21 09:54:58（2 键）" in line, line
    assert "新会话 1" in line, line
    updated = mod.headline(ok, mod.split_due([_conv(eid="e1", ts=1792999999000)], ok, MY_UID))
    assert "老会话有新消息 1" in updated and "新会话 0" in updated, updated
    ok_zero = mod.headline(ok, mod.split_due([_conv(eid="e1")], ok, MY_UID))
    assert "新增 0 条" in ok_zero and "新会话" not in ok_zero, "零时不硬凑分档句"
    unkeyed = mod.headline(ok, mod.split_due([{"status": "hr_replied"}], ok, MY_UID))
    assert "没有可用会话键" in unkeyed, unkeyed
    assert "无历史基准" in mod.headline(mod.empty_base(), mod.split_due([], mod.empty_base()))
    corrupt = mod.empty_base() | {"state": mod.STATE_CORRUPT, "detail": "Expecting property name"}
    assert "原样保留不覆盖" in mod.headline(corrupt, mod.split_due([], corrupt))
    mismatch = mod.empty_base("blockchain") | {"state": mod.STATE_PROFILE_MISMATCH, "storedProfile": "ai"}
    assert "不串档" in mod.headline(mismatch, mod.split_due([], mismatch))


def test_watermark_phrase_is_the_single_source_of_the_wording():
    """两份报告念同一句（措辞不同源就会各自改词，同一个人在两个读数里看到两个名字）。"""
    ok = _base(keys=["e1"], snapshot_at="2026-10-21 09:54:58")
    assert mod.watermark_phrase(ok) == "水位线 2026-10-21 09:54:58（1 键）"
    assert mod.watermark_phrase(mod.empty_base()).startswith("无历史基准")


# --- 写侧：唯一入口 + 三种拒绝 ---


def test_advance_refuses_to_overwrite_a_corrupt_file(tmp_path):
    path = tmp_path / "wm.json"
    path.write_text("{坏文件", encoding="utf-8")
    base = mod.read_watermark(path, "blockchain")
    res = mod.advance_watermark(path, [_conv()], base, profile="blockchain")
    assert res["written"] is False and res["reason"]
    assert path.read_text(encoding="utf-8") == "{坏文件", "读不懂的现场是排查材料，机器不替人擦"


def test_advance_refuses_to_overwrite_another_profiles_file(tmp_path):
    path = tmp_path / "wm.json"
    mod.advance_watermark(path, [_conv(eid="别人的岗")], mod.empty_base(), profile="ai")
    base = mod.read_watermark(path, "blockchain")
    res = mod.advance_watermark(path, [_conv(eid="我的岗")], base, profile="blockchain")
    assert res["written"] is False
    assert json.loads(path.read_text(encoding="utf-8"))["keys"] == ["别人的岗"], "跨档写一律拒绝"


def test_advance_refuses_an_empty_snapshot(tmp_path):
    """登录过期采到 0 条（192616 实测）时推进＝把基准清空，下一场「新增 0」是假读数。"""
    base = _base(keys=["e1"])
    res = mod.advance_watermark(tmp_path / "wm.json", [], base, profile="blockchain")
    assert res["written"] is False and "清空" in res["reason"]
    assert not (tmp_path / "wm.json").exists()


def test_advance_refuses_a_reading_with_no_usable_keys(tmp_path):
    """review 条目 A（票面禁止形态换了门进来）：读数有 3 条、但**一个可用会话键都没有**
    （DOM 降级卡片全无加密 id）时写成 `state="ok"` 的基准，等于「基准存在但内容为空 ≡ 没有基准，
    却声称可用」——下一场 split_due 会把 68 条全念成「较上次读数新增 68 条（水位线…（0 键））」。
    ⇒ 零键拒写、原文件不动、下一场仍落「无可用基准」。"""
    path = tmp_path / "wm.json"
    unkeyed = [{"lastTS": 1792000000000, "status": "hr_replied"}] * 3
    res = mod.advance_watermark(
        path, unkeyed, mod.empty_base("blockchain"), profile="blockchain", snapshot_at="2026-10-21 09:54:58"
    )
    assert res["written"] is False and res["keys"] == 0, res
    assert "无可记录的会话键" in res["reason"], res
    assert not path.exists(), "零键读数连基准文件都不该建"
    base = mod.read_watermark(path, "blockchain")
    assert base["state"] == mod.STATE_FIRST_RUN and not mod.is_usable(base)
    assert mod.split_due([_conv(eid=f"e{n}") for n in range(68)], base, MY_UID)["newCount"] is None, "不许报全部条数"


def test_advance_keeps_an_existing_baseline_when_the_reading_has_no_keys(tmp_path):
    """同样零键，但这次**已有**一份好基准：拒写＝原文件字节不动（那是上次真读数的现场），
    下一场照旧对着它比，不能被一场降级读数抹平。"""
    path = tmp_path / "wm.json"
    mod.advance_watermark(path, [_conv(eid="e1")], mod.empty_base("blockchain"), profile="blockchain")
    before = path.read_text(encoding="utf-8")
    res = mod.advance_watermark(
        path,
        [{"lastTS": 1792999999000, "status": "hr_replied"}],
        mod.read_watermark(path, "blockchain"),
        profile="blockchain",
        snapshot_at="2026-10-21 10:00:00",
    )
    assert res["written"] is False and "无可记录的会话键" in res["reason"], res
    assert path.read_text(encoding="utf-8") == before, "拒写时一字节都不动"
    assert mod.read_watermark(path, "blockchain")["keys"] == frozenset({"e1"})


def test_a_zero_key_file_is_not_a_usable_baseline(tmp_path):
    """读侧同源：已经存在的 0 键基准（本条之前那个写入口留下的，或别处手搓的）与没有基准等价，
    不能声称 `ok`。它落 `no_keys` 这一格——读数 N=None，措辞说清「基准未推进：无可记录的会话键」，
    处置是一次能拿到会话键的 `--mark-read`（不要求人工移文件，因为它不是排查材料）。"""
    path = tmp_path / "wm.json"
    path.write_text(
        json.dumps(
            {"version": 1, "profile": "blockchain", "snapshotAt": "2026-10-21 09:54:58", "keys": [], "maxLastTS": None}
        ),
        encoding="utf-8",
    )
    base = mod.read_watermark(path, "blockchain")
    assert base["state"] == mod.STATE_NO_KEYS and not mod.is_usable(base), base
    assert base["keys"] == frozenset() and path.exists()
    due = mod.split_due([_conv(eid=f"e{n}") for n in range(68)], base, MY_UID)
    assert due["newCount"] is None, "0 键基准上算出来的「新增 68 条」是谎报"
    line = mod.headline(base, due)
    assert "无可记录的会话键" in line and "新增" not in line, line


def test_read_watermark_never_raises_on_a_missing_key(tmp_path):
    """形态核验说要的键，读的时候不能再按「必存在」取——`read_watermark` 的契约是**恒返回、不抛**
    （只读消费者没有 try 兜着，一处 KeyError 会把整场读数换成 traceback）。缺 `maxLastTS` 落 corrupt。"""
    path = tmp_path / "wm.json"
    path.write_text(
        json.dumps({"version": 1, "profile": "blockchain", "snapshotAt": "x", "keys": ["e1"]}), encoding="utf-8"
    )
    base = mod.read_watermark(path, "blockchain")
    assert base["state"] == mod.STATE_CORRUPT and "maxLastTS" in base["detail"], base


def test_no_keys_baseline_is_rebuilt_by_a_real_reading(tmp_path):
    """`no_keys` 不是排查材料（它什么都没说），所以允许被一次真有键的读数重建——
    与 `corrupt`（原样保留）、`profile_mismatch`（不串档）两种拒绝区分开。"""
    path = tmp_path / "wm.json"
    path.write_text(
        json.dumps(
            {"version": 1, "profile": "blockchain", "snapshotAt": "2026-10-21 09:54:58", "keys": [], "maxLastTS": None}
        ),
        encoding="utf-8",
    )
    base = mod.read_watermark(path, "blockchain")
    res = mod.advance_watermark(path, [_conv(eid="e1")], base, profile="blockchain", snapshot_at="2026-10-21 11:00:00")
    assert res["written"] is True, res
    assert mod.read_watermark(path, "blockchain")["state"] == mod.STATE_OK


def test_advance_refuses_an_empty_watermark_path(tmp_path, monkeypatch):
    """条目 E：空 `watermark_path` 不是「写在工作目录」，而是会让 `write_json_atomic` 把
    `Path("")` 当 `.` 用——`mkdir(".")` 通过、tmp 名落成 `..tmp`、`os.replace("..tmp", ".")`
    再炸，CWD 里留下一个孤儿临时文件。空路径＝调用方没接线，显式拒绝并说明原因。"""
    monkeypatch.chdir(tmp_path)
    res = mod.advance_watermark("", [_conv(eid="e1")], mod.empty_base("blockchain"), profile="blockchain")
    assert res["written"] is False and res["reason"], res
    assert "路径" in res["reason"], res
    assert list(Path(".").iterdir()) == [], "CWD 里不许留下孤儿 .tmp（含隐藏名 `..tmp`）"


def test_advance_refuses_a_blank_path_object(tmp_path, monkeypatch):
    """同上，但这次基准文件**已经存在**：拒写也不能碰它（空路径是调用方的接线问题，
    不是「基准不可用」的信号）。"""
    path = tmp_path / "wm.json"
    mod.advance_watermark(path, [_conv(eid="e1")], mod.empty_base("blockchain"), profile="blockchain")
    before = path.read_text(encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    res = mod.advance_watermark("   ", [_conv(eid="e2")], mod.read_watermark(path, "blockchain"), profile="blockchain")
    assert res["written"] is False and "路径" in res["reason"], res
    assert path.read_text(encoding="utf-8") == before


def test_advance_write_failure_keeps_the_old_reading(tmp_path, monkeypatch):
    """盘写不下去只降级读数，不抛——水位线是呈报层，不该反噬已经跑完的只读轮。"""

    def boom(path, data):
        # 形参按 `write_json_atomic(path, data)` 的真实签名对齐并留痕（vulture 只认被读到的名字）
        raise OSError(f"{Path(path)} 写不下去（要落 {len(data['keys'])} 个键）")

    monkeypatch.setattr(mod, "write_json_atomic", boom)
    res = mod.advance_watermark(tmp_path / "wm.json", [_conv()], mod.empty_base("blockchain"), profile="blockchain")
    assert res["written"] is False and "写入失败" in res["reason"]


def test_advance_with_explicit_snapshot_at(tmp_path):
    """推进用的时刻＝**本场读数的时刻**（快照 `generatedAt`），不是写盘那一刻：
    报出去的「较上次读数」要能对上人所看的那场。"""
    path = tmp_path / "wm.json"
    res = mod.advance_watermark(
        path, [_conv()], mod.empty_base(), profile="blockchain", snapshot_at="2026-10-21 19:12:59"
    )
    assert res["snapshotAt"] == "2026-10-21 19:12:59"
    assert mod.read_watermark(path, "blockchain")["snapshotAt"] == "2026-10-21 19:12:59"


def test_split_due_survives_a_junk_entry():
    """读数里混进非对象条目（半截 wapi 响应、别的采集器写歪了一条）：跳过它、不炸整场呈报。"""
    base = _base(keys=["e1"], max_ts=None)
    due = mod.split_due([_conv(eid="e2"), "不是对象", None], base, MY_UID)
    assert due["newCount"] == 1 and due["dueKeys"] == frozenset({"e2"})
    assert mod.split_due(None, base, MY_UID)["newCount"] == 0, "整份读数缺席也读得出零，不抛"
