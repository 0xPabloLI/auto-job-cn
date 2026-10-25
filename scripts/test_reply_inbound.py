#!/usr/bin/env python3
"""#66 阶段 2 回复链路的判定/台账/读数层（不涉浏览器）。

夹具取自今日实测：`private/data/probe/inbound_coverage_v2_20261020-120316.json`
（16 条入站逐条的详情页 title 与身份判定）与 `内部调研笔记目录（未随本仓分发）boss-chat-card-shape.md` §3 的
wapi 字段口径。"""

import ast
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _inbound_watermark as wm  # noqa: E402  #水位线单一真源（#75），接线侧用例直接走它的读/写入口
import reply_inbound as mod  # noqa: E402
from platforms.chat_first import CHAT_URL, chat_url_for  # noqa: E402


def _conv(
    eid="e1",
    company="沐女士煦辰招聘者",
    source="wapi",
    status="hr_replied",
    applied=None,
    unread=1,
    time_="2026-10-20 11:00:00",
    last="方便把简历发我看一下吗",
):
    return {
        "encryptJobId": eid,
        "company": company,
        "brandName": "煦辰",
        "name": "沐女士",
        "title": "招聘者",
        "source": source,
        "status": status,
        "appliedJob": applied,
        "unreadMsgCount": unread,
        "time": time_,
        "lastMsg": last,
        "summary": last[:80],
    }


IDENT_OK = {"jobTitle": "区块链工程师招聘", "state": "ok", "rawTitle": "「区块链工程师招聘」_煦辰招聘-BOSS直聘"}


def test_classify_eligible_carries_identity_and_context():
    """正对照（不写这条，「全都跳过了」看起来像「判据生效」）：一条正常入站必须 eligible。"""
    report = {"conversations": [_conv()]}
    out = mod.classify(report, {"e1": IDENT_OK}, set())
    assert len(out) == 1
    cand = out[0]
    assert cand.get("eligible") is True and "skip" not in cand
    assert cand["jobTitle"] == "区块链工程师招聘"
    assert cand["lastMsg"] == "方便把简历发我看一下吗", "起草要读到的就是对方那句原文"
    assert cand["readState"] == "unread"


def test_classify_carries_deeplink_and_readback_fields():
    """#73：深链要的加密 id、回读比对的 lastTS/fromId 必须从快照一路带到候选——
    浏览器阶段只拿得到候选，缺一个就得多读一次 wapi。"""
    conv = {
        **_conv(),
        "encryptUid": "enc-uid-1",
        "encryptBossId": "enc-boss-1",
        "bossUid": 740868320,
        "lastTS": 1792000000000,
        "lastFromId": 740868320,
    }
    cand = mod.classify({"conversations": [conv]}, {"e1": IDENT_OK}, set())[0]
    assert (cand["encryptUid"], cand["encryptBossId"], cand["bossUid"]) == ("enc-uid-1", "enc-boss-1", 740868320)
    assert cand["lastTS"] == 1792000000000
    assert cand["lastFromId"] == 740868320


def test_skip_reasons_keep_historical_card_buckets():
    """分桶只增不减：卡片路径已移除，但 `conv_not_found` / `conv_ambiguous` / `conv_click_failed`
    这些历史桶**保留**，历史报告才可比；本路径新增自己的两个桶。"""
    for reason in ("conv_not_found", "conv_ambiguous", "conv_click_failed"):
        assert reason in mod.SKIP_REASONS, f"{reason} 是历史读数的一部分，不许删"
    for reason in ("conv_unreachable", "no_deeplink_id"):
        assert reason in mod.SKIP_REASONS
    assert len(mod.SKIP_REASONS) == len(set(mod.SKIP_REASONS)), "分桶不许重名"


def test_classify_skips_by_reason():
    cases = [
        (_conv(source="dom", eid=None), {"e1": IDENT_OK}, set(), "no_job_id"),
        (_conv(), {}, set(), "job_unknown"),
        (_conv(), {"e1": {"jobTitle": "Golang是什么职位", "state": "dead"}}, set(), "job_dead"),
        (_conv(), {"e1": {"jobTitle": "上海", "state": "unresolved"}}, set(), "job_unresolved"),
        (_conv(), {"e1": {"jobTitle": "", "state": "login"}}, set(), "login"),
        (_conv(), {"e1": IDENT_OK}, {"e1"}, "already_replied"),
    ]
    for conv, identities, replied, expected in cases:
        out = mod.classify({"conversations": [conv]}, identities, replied)
        assert out[0]["skip"] == expected, expected


def test_classify_replied_wins_over_job_dead():
    """判定顺序：已回过的**不再反查岗位**（省一次页面往返），所以 already_replied 要压过 job_dead。"""
    out = mod.classify({"conversations": [_conv()]}, {"e1": {"state": "dead", "jobTitle": ""}}, {"e1"})
    assert out[0]["skip"] == "already_replied"


def test_classify_ignores_ours_last_and_linked():
    """两条不该进候选的：末条是我方发言（球不在我们这侧）、已关联到投递记录（那是我们自己投的岗）。"""
    report = {"conversations": [_conv(eid="a", status="awaiting"), _conv(eid="b", applied="我们投过的岗")]}
    assert mod.classify(report, {}, set()) == []


def test_sort_waiting_newest_first_and_tolerates_display_strings():
    """DOM 降级路径的时间是「昨天」「09月09日」这类展示串——混型不得炸，只允许排到最后。"""
    convs = [_conv(eid="old", time_="2026-10-18 09:00:00"), _conv(eid="new", time_="2026-10-20 09:00:00")]
    assert [c["encryptJobId"] for c in mod.sort_waiting(convs)] == ["new", "old"]
    mixed = [_conv(eid="iso", time_="2026-10-18 09:00:00"), _conv(eid="chip", time_="昨天")]
    assert [c["encryptJobId"] for c in mod.sort_waiting(mixed)] == ["iso", "chip"]


def test_record_reply_idempotent_by_job_id(tmp_path):
    path = tmp_path / "inbound_replies.json"
    assert (
        mod.record_reply(path, {"encryptJobId": "e1", "replyText": "第一稿", "sentAt": "2026-10-20T18:00:00"})
        == "added"
    )
    assert (
        mod.record_reply(path, {"encryptJobId": "e1", "replyText": "第二稿", "sentAt": "2026-10-20T18:05:00"})
        == "updated"
    )
    ledger = mod.load_ledger(path)
    assert len(ledger) == 1, "同岗重复回写不得堆积成两条（台账是按岗的历史，不是按次的日志）"
    assert ledger[0]["replyText"] == "第二稿"


def test_record_reply_never_raises(tmp_path):
    """写盘失败只回 skipped——台账层绝不反噬一条**已经发出去**的消息（同 record_greeting 的纪律）。

    失败注入用的是「父目录是个普通文件」：`write_json_atomic` 会 `mkdir(parents=True)`，
    所以「目录不存在」**不是**失败现场（第一版这么写得到了 `added` 而不是 `skipped`，
    用例自己抓住了这个空过）。"""
    blocker = tmp_path / "afile"
    blocker.write_text("占位：父路径是个文件，mkdir 必然失败", encoding="utf-8")
    assert (
        mod.record_reply(blocker / "ledger.json", {"encryptJobId": "e1", "sentAt": "2026-10-20T18:00:00"}) == "skipped"
    )
    assert mod.record_reply(tmp_path / "l.json", {"replyText": "没有 encryptJobId"}) == "skipped"


def test_replied_today_counts_only_today():
    ledger = [
        {"encryptJobId": "a", "sentAt": f"{date.today().isoformat()}T10:00:00"},
        {"encryptJobId": "b", "sentAt": "2026-01-01T10:00:00"},
        {"encryptJobId": "c"},  # 没日期
        "不是 dict",
    ]
    assert mod.replied_today(ledger) == 1
    assert mod.replied_today(ledger, today=date(2026, 1, 1)) == 1, "按传入日期判，不绑死系统时钟"


def test_load_ledger_never_eats_a_corrupt_file():
    """读不出 = 空列表（不覆盖由原子写负责），坏 JSON 不得抛断整场。"""
    assert mod.load_ledger("/definitely/not/here.json") == []


def test_skip_stats_always_present_and_collects_unknown(tmp_path):
    stats = mod.skip_stats([{"skip": "job_dead"}, {"skip": "没在枚举里的原因"}, {"eligible": True}])
    assert stats["job_dead"] == 1
    assert stats["other"] == 1, "集外的原因必须落 other，不许静默丢"
    for reason in mod.SKIP_REASONS:
        assert reason in stats, f"{reason} 桶缺失——分桶只增不减才可比"


def test_summarize_shape_is_stable():
    report = {
        "generatedAt": "2026-10-20 18:00:00",
        "myUserId": 780045835,
        "totalConversations": 54,
        "unlinkedReplied": 16,
        "unlinkedRepliedUnread": 9,
    }
    # 真实流程里一条候选只可能来自一处：判定阶段跳过的条目根本不进浏览器阶段，
    # 所以 candidates 与 outcomes 不会同时为同一条计一次跳过（这里就按那个形状给）
    candidates = [{"eligible": True}, {"eligible": True}, {"skip": "job_dead"}]
    outcomes = [{"status": "replied"}, {"status": "no_input", "reason": "no_input"}]
    out = mod.summarize("dry_run", report, candidates, outcomes, [], "2026-10-20T18:10:00")
    assert out["mode"] == "dry_run"
    assert out["snapshotAt"] == "2026-10-20 18:00:00", "#63 分页未取全 ⇒ 呈报必须带快照生成时刻"
    assert out["myUserId"] == 780045835, "#73：回读判据的我方 userId 也要进呈报，现场可复核"
    assert out["inbound"] == 16 and out["inboundUnread"] == 9
    assert out["eligible"] == 2 and out["replied"] == 1
    assert out["skippedReasons"]["job_dead"] == 1, "判定阶段的跳过要进同一张表"
    assert out["skippedReasons"]["no_input"] == 1, "浏览器阶段的落空也要进这张表，否则「为什么没回」只答了一半"
    assert set(out["skippedReasons"]) == set(mod.SKIP_REASONS), "浏览器阶段还没跑过的桶也要恒在（0 写出来）"
    assert out["newSinceWatermark"] is None and out["watermark"]["state"] == "first_run", (
        "#75 水位线两键恒在：没接线时按「无历史基准」落，不能让报告缺键"
    )


def test_request_payload_labels_it_a_reply():
    cand = {
        "encryptJobId": "e1",
        "company": "沐女士煦辰招聘者",
        "brandName": "煦辰",
        "bossName": "沐女士",
        "bossTitle": "招聘者",
        "jobTitle": "区块链工程师招聘",
        "lastMsg": "方便把简历发我看一下吗",
        "lastTime": "2026-10-20 11:00:00",
        "readState": "unread",
    }
    payload = mod.request_payload(3, cand)
    assert payload["kind"] == "inbound_reply", "起草侧要一眼看出这是回复不是首封（开头公式不同）"
    assert payload["idx"] == 3
    assert payload["theirLastMessage"] == "方便把简历发我看一下吗"
    assert payload["jobTitle"] == "区块链工程师招聘"


def test_gate_problems_uses_the_same_gate(monkeypatch):
    """回复正文过的是**同一个** pre_send_gate（无豁免分支）：notes 缺失照样拦。
    断言"参数原样透传"，防止后来人给回复另开一条更松的门。"""
    seen = {}

    def fake_gate(text, level, whitelist, notes="", sections=None, state_evidence=None):
        seen.update({"text": text, "level": level, "notes": notes, "sections": sections, "ev": state_evidence})
        return ["来源备注缺失"] if not str(notes).strip() else []

    monkeypatch.setattr(mod.la, "pre_send_gate", fake_gate)
    monkeypatch.setattr(mod.g, "load_whitelist_file", lambda p: [])
    cfg = {"profile_sections": {1, 2}, "fact_cards_path": "x"}
    out = mod.gate_problems("正文", "高", "", None, cfg)
    assert out == ["来源备注缺失"]
    assert seen["notes"] == "" and seen["sections"] == {1, 2}, "notes/sections 必须原样透传，不能在回复侧丢掉"


def test_warn_lines_reuses_both_soft_checks(monkeypatch):
    monkeypatch.setattr(mod.la, "salutation_warnings", lambda n, t, s: ["称呼问题"])
    monkeypatch.setattr(mod.la, "question_warnings", lambda s: ["提问问题"])
    salu, qwarn = mod.warn_lines({"bossName": "张三", "bossTitle": "HR"}, "正文")
    assert salu == ["称呼问题"] and qwarn == ["提问问题"]


# --- T4：浏览器阶段用假页面驱动（真机那一跳单独补，mock 全绿的结论不外推到真实形态）---
from types import SimpleNamespace  # noqa: E402

MY_UID = 780045835
_SENT = "好的，简历这就发您邮箱，方便的话我先把网站链接发您看？"


class _FakeInput:
    """聊天页输入框替身（contenteditable 形态：无独立发送按钮 ⇒ Enter 发送）。"""

    def __init__(self):
        self.filled = []
        self.clicks = 0

    def evaluate(self, script, *args):
        return "editable" if "isContentEditable" in script else ""

    def click(self):
        self.clicks += 1

    def fill(self, text):
        self.filled.append(text)

    def is_visible(self):
        return True


class _FakeDeeplinkPage:
    """聊天页替身：**没有会话卡片**（#73 真机形态：68 条会话只渲染 40 张，目标那条 0 命中）。
    任何 `query_selector_all` 调用都会直接炸——卡片路径一旦回来，用例立刻红。
    `header`/`headers` 是右侧会话头（目标判据；`headers` 用来造水合序列），
    `header_hits` 是锚的命中数（>1＝歧义），`header_calls` 记录被问了几次（钉判据顺序与轮询）。"""

    def __init__(
        self,
        landing_url=None,
        goto_error=None,
        url="https://www.zhipin.com/web/geek/chat",
        header="",
        headers=None,
        header_hits=1,
    ):
        self.gotos = []
        self._url = url
        self._landing = landing_url
        self._goto_error = goto_error
        self.header = header
        self._headers = list(headers) if headers else None
        self.header_hits = header_hits
        self.header_calls = 0
        self.pressed = []
        self.keyboard = SimpleNamespace(press=self._press)

    def _press(self, key):
        self.pressed.append(key)

    @property
    def url(self):
        return self._url

    def goto(self, url, **kwargs):
        self.gotos.append(url)
        if self._goto_error is not None:
            raise self._goto_error
        self._url = self._landing or url

    def evaluate(self, script, *args):
        if "base-info" in script:
            text = self.header
            if self._headers:
                text = self._headers.pop(0) if len(self._headers) > 1 else self._headers[0]
            self.header_calls += 1
            return {"text": text, "hits": self.header_hits}
        return ""

    def query_selector_all(self, sel):
        raise AssertionError(f"#73 起不再读会话卡片，问到 {sel} 说明卡片路径回来了")


def _cfg():
    return {
        "reply_timeout": 1,
        "fact_cards_path": "x",
        "profile_sections": {1},
        "quiet_hours": (0, 7),
        "paste_prob": 1.0,
        "type_delay_min": 1,
        "type_delay_max": 2,
    }


def _ctx(tmp_path, mode="send", my_user_id=None, snapshot_at="2026-10-20 18:00:00"):
    shots = tmp_path / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    return mod._Ctx(
        cfg=_cfg(),
        session_dir=tmp_path,
        shots_dir=shots,
        ledger_path=str(tmp_path / "inbound_replies.json"),
        login_selector="#login-dialog",
        mode=mode,
        pacer=SimpleNamespace(wait_sent=lambda: None, wait_skip=lambda: None),
        my_user_id=my_user_id,
        snapshot_at=snapshot_at,
    )


CAND = {
    "encryptJobId": "e1",
    "company": "沐女士煦辰招聘者",
    "brandName": "煦辰",
    "bossName": "沐女士",
    "bossTitle": "招聘者",
    "jobTitle": "区块链工程师招聘",
    "lastMsg": "方便把简历发我看一下吗",
    "lastTime": "2026-10-20 11:00:00",
    "lastTS": 1792000000000,
    "lastFromId": 740868320,
    "readState": "unread",
    "encryptUid": "enc-uid-1",
    "encryptBossId": "enc-boss-1",
    "bossUid": 740868320,
    "eligible": True,
}


def _target_page(**kwargs):
    """一条**正常打开**的替身页面：右侧会话头就是 CAND 那条，且落地 URL 已被 SPA 规范化成
    裸 `/web/geek/chat`——真机 2026-10-21 就是这个形态（从岗位详情页导航过去时 `?id=` 不留），
    所以用例必须在这种 URL 下仍然放行，判据只能是会话头。"""
    kwargs.setdefault("landing_url", "https://www.zhipin.com/web/geek/chat")
    kwargs.setdefault("header", CAND["company"])
    return _FakeDeeplinkPage(**kwargs)


def _wapi_conv(**over):
    conv = {
        "encryptJobId": "e1",
        "source": "wapi",
        "lastMsg": _SENT,
        "lastTS": CAND["lastTS"] + 1,
        "lastFromId": MY_UID,
    }
    conv.update(over)
    return conv


def _report(convs=None, my_user_id=MY_UID):
    convs = [_wapi_conv()] if convs is None else convs
    return {
        "generatedAt": "2026-10-20 18:00:00",
        "myUserId": my_user_id,
        "totalConversations": len(convs),
        "conversations": convs,
    }


class _FakePlatform:
    """`followup` 替身：按顺序吐报告（用尽后返回 None，模拟「读不回来」）。"""

    def __init__(self, *reports, repeat=False):
        self.reports = list(reports)
        self.repeat = repeat
        self.calls = 0
        self.kwargs = []
        self.login_dialog_selector = "#login-dialog"

    def followup(self, page, applied, **kwargs):
        self.calls += 1
        self.kwargs.append(kwargs)
        if not self.reports:
            return None
        return self.reports[0] if self.repeat else self.reports.pop(0)


def _patch_runtime(monkeypatch, *, in_chat_form=True, risk=None, logged_out=False):
    """`reply_one` 的浏览器依赖全换成桩：本文件测的是**编排**，真机形态另补。
    返回那个输入框替身，方便用例断言草稿真的进了框。"""
    inp = _FakeInput()
    monkeypatch.setattr(mod.la, "shot", lambda *a, **k: None)
    monkeypatch.setattr(mod.la, "_in_chat_form", lambda *a, **k: in_chat_form)
    monkeypatch.setattr(mod.la, "detect_risk_signal", lambda p: risk)
    monkeypatch.setattr(mod.la, "is_logged_out", lambda p, s: logged_out)
    monkeypatch.setattr(mod.la, "find_input_with_retry", lambda p: inp)
    monkeypatch.setattr(mod.la, "wait_reply", lambda d, i, timeout=None: _SENT)
    monkeypatch.setattr(mod.la, "read_reply", lambda d, i: {"send_text": "x", "level": "中", "notes": "§1"})
    monkeypatch.setattr(mod, "gate_problems", lambda *a, **k: [])
    monkeypatch.setattr(mod.la, "find_send_button", lambda p, **k: None)  # 走 contenteditable + Enter 那支
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(mod.random, "uniform", lambda a, b: 0.01)
    return inp


def _no_quiet(monkeypatch):
    """#74 的教训：别让用例的结论依赖宿主时区。凡是走 `_drive(send=True)` 的都把静默时段钉掉。"""
    monkeypatch.setattr(mod, "is_quiet_hour", lambda *_: False)


def test_url_target_id_requires_exactly_one_id():
    """落地 URL 的 id 判据：缺、多、空都算「读不出目标」——宁可落 conv_unreachable，不猜。"""
    assert mod._url_target_id(_FakeDeeplinkPage(url="https://www.zhipin.com/web/geek/chat?id=abc")) == "abc"
    assert mod._url_target_id(_FakeDeeplinkPage(url="https://www.zhipin.com/web/geek/chat?id=a&id=b")) is None
    assert mod._url_target_id(_FakeDeeplinkPage(url="https://www.zhipin.com/web/geek/chat")) is None
    assert mod._url_target_id(_FakeDeeplinkPage(url="https://www.zhipin.com/web/geek/chat?id=")) is None
    assert mod._url_target_id(SimpleNamespace(url="")) is None


def test_chat_url_round_trips_special_ids():
    """编码与解码共用一个底座：深链里 quote 过的 id，落地 URL 解析回来必须还是原串。
    这个读数只进 detail 与存证（真机 URL 会被 SPA 规范化掉），但读错了就是假归因。"""
    raw = "a b/c&d=e+f"
    page = _FakeDeeplinkPage(url=chat_url_for(raw))
    assert mod._url_target_id(page) == raw


def test_reply_one_opens_by_encrypt_uid_not_card(monkeypatch, tmp_path):
    """#73 主修：按 `encryptUid` 深链打开，**一次导航直达**，彻底不依赖卡片渲染
    （页面没有任何卡片，`query_selector_all` 一被调用就炸）；确认来自 wapi 回读。
    落地 URL 是**裸 `/web/geek/chat`**（真机形态：SPA 把 `?id=` 规范化掉了）——这种 URL 下
    仍必须放行，因为目标判据是右侧会话头，不是 URL。"""
    page = _target_page()
    ctx = _ctx(tmp_path)
    ctx.platform = _FakePlatform(_report())
    inp = _patch_runtime(monkeypatch)
    out = mod.reply_one(page, CAND, 1, ctx)
    assert out["status"] == "replied", out
    assert page.url == "https://www.zhipin.com/web/geek/chat", "用例前提：URL 里已经没有 id"
    assert page.header_calls >= 1, "目标判据必须是右侧会话头"
    assert page.gotos == [chat_url_for(CAND["encryptUid"])], "深链只用 encryptUid，且只导航一次"
    assert page.pressed == ["Enter"]
    assert inp.filled == [_SENT], "草稿应经 _type_greeting 进输入框"
    assert out["wapiBefore"] == {
        "lastMsg": CAND["lastMsg"],
        "lastTS": CAND["lastTS"],
        "lastFromId": CAND["lastFromId"],
        "snapshotAt": ctx.snapshot_at,
    }
    assert out["wapiAfter"]["lastMsg"] == _SENT
    assert out["wapiAfter"]["lastTS"] == CAND["lastTS"] + 1
    assert out["wapiAfter"]["lastFromId"] == MY_UID
    assert out["wapiAfter"]["readAt"], "回读时刻必落——点开前后的对照靠它可复核"
    assert ctx.platform.kwargs == [{"detach": True}], "同页重复读必须注销自己的 response handler"
    ledger = mod.load_ledger(ctx.ledger_path)
    assert len(ledger) == 1 and ledger[0]["encryptJobId"] == "e1"
    assert (tmp_path / "request_1.json").exists(), "起草协议用的就是 live_apply 那对文件名"


def test_reply_one_falls_back_to_encrypt_boss_id(monkeypatch, tmp_path):
    """`encryptUid` 缺失时退 `encryptBossId`（同一张聊天页的 id 来源），仍不碰卡片。"""
    page = _target_page()
    ctx = _ctx(tmp_path)
    ctx.platform = _FakePlatform(_report())
    _patch_runtime(monkeypatch)
    out = mod.reply_one(page, {**CAND, "encryptUid": None}, 1, ctx)
    assert out["status"] == "replied", out
    assert page.gotos == [chat_url_for(CAND["encryptBossId"])]


def test_reply_one_deeplink_lands_wrong_conversation_is_conv_unreachable(monkeypatch, tmp_path):
    """深链打开的**不是这条对话**（右侧会话头对不上）⇒ 独立桶 `conv_unreachable` + 存证。
    不许冒充 `conv_not_found`（那是「列表里没有这条」，与「打开时被站点换走了」是两回事），
    更不许往下走：一个字都不打。"""
    wrong = "千女士闾旻昱珀珀昱旻闾HR"
    page = _target_page(header=wrong)
    ctx = _ctx(tmp_path)
    ctx.platform = _FakePlatform(_report())
    asked = []
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(mod.la, "find_input_with_retry", lambda p: asked.append("input"))
    out = mod.reply_one(page, CAND, 1, ctx)
    assert out["status"] == "conv_unreachable" and out["reason"] == "conv_unreachable"
    assert out["status"] != "conv_not_found", "打开的不是这条与「列表未渲染」必须分开归因"
    assert asked == [] and page.pressed == [], "开错会话时一个字都不许打"
    saved = json.loads((tmp_path / "deeplink-mismatch-1.json").read_text(encoding="utf-8"))
    assert saved["expectedUrl"] == chat_url_for(CAND["encryptUid"])
    assert saved["expectedCompany"] == CAND["company"]
    assert saved["actualHeader"] == wrong, "存证要留下读到的那个头，否则事后无从复核"
    assert saved["headerHits"] == 1
    assert saved["candidate"]["encryptJobId"] == "e1"
    assert saved["at"], "存证要带时刻，否则事后无法与 report 对齐"


def test_chat_target_header_js_is_scoped_and_not_the_parent_layer():
    """锚要**带作用域**且不能是父层：`.user-info` 会把「更多」按钮卷进 innerText（185645 实测），
    全页 fallback 则可能在左栏卡片复用同名 class 时认错人——所以宁可 0 命中也不猜。"""
    js = mod.CHAT_TARGET_HEADER_JS
    assert ".top-info-content .base-info" in js
    assert ".user-info" not in js, "父层多一个「更多」，全等判据会把正常打开读成失败"
    assert "querySelectorAll" in js and "hits" in js, "要回命中数：>1 是歧义，不是「取第一个」"


def test_reply_one_waits_for_the_panel_header_to_hydrate(monkeypatch, tmp_path):
    """面板头水合要等：空 → 空 → 命中 必须放行，且真的轮询过三轮。
    第一版在这里栽过一次——只读一次就判失败，等于把水合竞态读成「站点换走了」。"""
    page = _target_page(header="", headers=["", "", CAND["company"]])
    ctx = _ctx(tmp_path)
    ctx.platform = _FakePlatform(_report())
    _patch_runtime(monkeypatch)
    out = mod.reply_one(page, CAND, 1, ctx)
    assert out["status"] == "replied", out
    assert page.header_calls == 3, "空读数要继续等，不是第一次就收"


def test_reply_one_matches_header_across_whitespace(monkeypatch, tmp_path):
    """DOM 的 innerText 带换行，wapi 的 company 是无分隔粘连串——两边都去空白才可比。"""
    page = _target_page(header="沐女士\n煦辰\n招聘者")
    ctx = _ctx(tmp_path)
    ctx.platform = _FakePlatform(_report())
    _patch_runtime(monkeypatch)
    assert mod.reply_one(page, CAND, 1, ctx)["status"] == "replied"


def test_reply_one_rejects_header_with_trailing_button_text(monkeypatch, tmp_path):
    """185645 那场的形态：读到 `…招聘经理更多`。全等判据必须判失败，
    不能用 startswith 之类的宽松比对把它放过去（那会重新引入认错人的风险）。"""
    page = _target_page(header=CAND["company"] + "更多")
    ctx = _ctx(tmp_path)
    ctx.platform = _FakePlatform(_report())
    _patch_runtime(monkeypatch)
    out = mod.reply_one(page, CAND, 1, ctx)
    assert out["status"] == "conv_unreachable"
    assert "打开的不是这条会话" in out["detail"]


def test_reply_one_empty_header_says_cannot_read_not_wrong_conversation(monkeypatch, tmp_path):
    """锚 0 命中（选择器腐烂／面板没出来）与「打开的是别人」是两种归因，处置方向不同：
    前者要去查形态，后者是这条不能发。detail 不许互相冒充。"""
    page = _target_page(header="", header_hits=0)
    ctx = _ctx(tmp_path)
    ctx.platform = _FakePlatform(_report())
    _patch_runtime(monkeypatch)
    out = mod.reply_one(page, CAND, 1, ctx)
    assert out["status"] == "conv_unreachable"
    assert "读不到右侧会话头" in out["detail"] and "打开的不是这条会话" not in out["detail"]
    saved = json.loads((tmp_path / "deeplink-mismatch-1.json").read_text(encoding="utf-8"))
    assert saved["headerHits"] == 0


def test_reply_one_ambiguous_header_hits_is_not_guessed(monkeypatch, tmp_path):
    """同屏两个锚＝歧义，直接收（不取第一个、也不把 5s 轮询跑满）。"""
    page = _target_page(header_hits=2)
    ctx = _ctx(tmp_path)
    ctx.platform = _FakePlatform(_report())
    _patch_runtime(monkeypatch)
    out = mod.reply_one(page, CAND, 1, ctx)
    assert out["status"] == "conv_unreachable"
    assert "2 个" in out["detail"]
    assert page.header_calls == 1, "歧义不必继续轮询"


def test_reply_one_rejects_stale_url_id_even_when_header_matches(monkeypatch, tmp_path):
    """URL 的 `?id=` 只反不立：没有＝被 SPA 规范化掉（正常），**带着别人的 id**＝深链没生效、
    面板还停在上一位——这时候会话头就算对得上也不发（那说明锚本身也认错了）。"""
    page = _target_page(landing_url="https://www.zhipin.com/web/geek/chat?id=someone-else")
    ctx = _ctx(tmp_path)
    ctx.platform = _FakePlatform(_report())
    _patch_runtime(monkeypatch)
    out = mod.reply_one(page, CAND, 1, ctx)
    assert out["status"] == "conv_unreachable"
    assert "矛盾" in out["detail"]


def test_reply_one_without_company_does_not_navigate(monkeypatch, tmp_path):
    """`company` 是会话头判据的左半边；缺了它去导航＝打开一个无法确认是谁的面板，比不发更糟。"""
    page = _target_page()
    ctx = _ctx(tmp_path)
    ctx.platform = _FakePlatform(_report())
    _patch_runtime(monkeypatch)
    out = mod.reply_one(page, {**CAND, "company": "   "}, 1, ctx)
    assert out["status"] == "other" and page.gotos == [] and page.header_calls == 0


def test_reply_one_deeplink_navigation_error_is_conv_unreachable(monkeypatch, tmp_path):
    """导航自己抛（超时 / tab 被关）同样落 `conv_unreachable`，并留下同一份存证——
    「打不开」与「打开了但不是它」在读数上同一桶，但存证的 error 区分得开。"""
    page = _target_page(goto_error=RuntimeError("Timeout 60000ms exceeded"))
    ctx = _ctx(tmp_path)
    ctx.platform = _FakePlatform(_report())
    _patch_runtime(monkeypatch)
    out = mod.reply_one(page, CAND, 1, ctx)
    assert out["status"] == "conv_unreachable" and "Timeout" in out["detail"]
    assert page.header_calls == 0, "导航都没成，不该去问会话头"
    saved = json.loads((tmp_path / "deeplink-mismatch-1.json").read_text(encoding="utf-8"))
    assert saved["error"]
    assert saved["expectedUrl"] == chat_url_for(CAND["encryptUid"])
    assert saved["candidate"]["encryptJobId"] == "e1"


def test_reply_one_missing_encrypt_uid_is_no_deeplink_id(monkeypatch, tmp_path):
    """wapi 条目没有可用加密 id ⇒ 独立桶 `no_deeplink_id`，一次导航都不发生（不猜、不回列表）。"""
    page = _target_page()
    ctx = _ctx(tmp_path)
    ctx.platform = _FakePlatform(_report())
    _patch_runtime(monkeypatch)
    out = mod.reply_one(page, {**CAND, "encryptUid": None, "encryptBossId": None}, 1, ctx)
    assert out["status"] == "no_deeplink_id" and out["reason"] == "no_deeplink_id"
    assert page.gotos == [] and page.header_calls == 0


def test_reply_one_off_chat_after_deeplink_does_not_type(monkeypatch, tmp_path):
    """形态守卫的落点：深链之后、读会话头与找输入框之前。URL 还在 `/web/geek/chat` 这条路上
    但面板不在沟通形态时才落 `off_chat`；深链失败另走 `conv_unreachable`，两桶不混。"""
    page = _target_page()
    ctx = _ctx(tmp_path)
    ctx.platform = _FakePlatform(_report())
    asked = []
    _patch_runtime(monkeypatch, in_chat_form=False)
    monkeypatch.setattr(mod.la, "find_input_with_retry", lambda p: asked.append("input"))
    out = mod.reply_one(page, CAND, 1, ctx)
    assert out["status"] == "off_chat"
    assert asked == [], "形态不对时不该去问 DOM 要输入框（那会落进 no_input，把离岗当未就绪）"
    assert page.header_calls == 0, "形态都没过，不该去读会话头"


def test_reply_one_login_expired_after_deeplink_stops_the_round(monkeypatch, tmp_path):
    """登录态改在深链之后判：深链一跳就把页面带到聊天页，这一格才是「掉没掉登录」的现场。
    掉登录 ⇒ 整场停（relogin），不是这条岗的问题。"""
    page = _target_page()
    ctx = _ctx(tmp_path)
    ctx.platform = _FakePlatform(_report())
    _patch_runtime(monkeypatch, logged_out=True)
    out = mod.reply_one(page, CAND, 1, ctx)
    assert out["status"] == "login_expired" and ctx.stop == "relogin"
    assert page.header_calls == 0, "先判登录，再判会话头"


def test_reply_one_gate_blocked_does_not_type(monkeypatch, tmp_path):
    page = _target_page()
    ctx = _ctx(tmp_path)
    ctx.platform = _FakePlatform(_report())
    typed = []
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(mod, "gate_problems", lambda *a, **k: ["来源备注缺失:xxx"])
    monkeypatch.setattr(mod.la, "_type_greeting", lambda *a, **k: typed.append(1))
    out = mod.reply_one(page, CAND, 1, ctx)
    assert out["status"] == "gate_blocked" and typed == [] and not mod.load_ledger(ctx.ledger_path)


def test_reply_one_send_unconfirmed_keeps_ledger_empty(monkeypatch, tmp_path):
    """双发的最后一道：wapi 回读没确认 ⇒ 不写台账 ⇒ 下一场这条还会被重新提出
    （由人复看，脚本不自作重发）。读数要带上已读到的 `wapiAfter` 与原因，否则人无从复核。"""
    page = _target_page()
    ctx = _ctx(tmp_path)
    stale = _report([_wapi_conv(lastMsg=CAND["lastMsg"], lastTS=CAND["lastTS"], lastFromId=CAND["lastFromId"])])
    ctx.platform = _FakePlatform(stale, repeat=True)  # 两轮回读都还是原样
    _patch_runtime(monkeypatch)
    out = mod.reply_one(page, CAND, 1, ctx)
    assert out["status"] == "send_unconfirmed" and out["typed"] is True
    assert out["wapiReadbackError"], "未确认要给出可复核的原因，不只是一个桶名"
    assert out["wapiBefore"]["lastTS"] == CAND["lastTS"]
    assert out["wapiAfter"]["lastMsg"] == CAND["lastMsg"], "读到的那条照写，供人对照"
    assert ctx.platform.calls == mod.WAPI_CONFIRM_ATTEMPTS, "回读有重试，但次数受常量约束"
    assert mod.load_ledger(ctx.ledger_path) == []


def test_send_unconfirmed_without_any_hit_still_has_wapi_after_key(monkeypatch, tmp_path):
    """回读一条都没命中时 `wapiAfter` 必须是 null（键恒在），否则报告读者按 runbook
    找这一对对照会落空——`wapiBefore` 与 `wapiReadbackError` 仍要能让人复核。"""
    page = _target_page()
    ctx = _ctx(tmp_path)
    ctx.platform = _FakePlatform(_report([_wapi_conv(encryptJobId="别的岗")]), repeat=True)
    _patch_runtime(monkeypatch)
    out = mod.reply_one(page, CAND, 1, ctx)
    assert out["status"] == "send_unconfirmed"
    assert out["wapiAfter"] is None
    assert out["wapiBefore"]["lastTS"] == CAND["lastTS"]
    assert "命中 0 条" in out["wapiReadbackError"]


def test_confirm_sent_by_readback_requires_ours_text_and_time_advance(monkeypatch, tmp_path):
    """回读判据四件套 + fail-closed：eid 唯一命中、来源 wapi、末条是我们那句、fromId 是我方、
    lastTS 相对点开前前进。任何一条不成立都不算发出。"""
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(mod.random, "uniform", lambda a, b: 0.01)
    page = _FakeDeeplinkPage()

    ctx = _ctx(tmp_path)
    ctx.platform = _FakePlatform(_report())
    conv, err = mod.confirm_sent_by_readback(page, CAND, _SENT, ctx)
    assert conv is not None and err == "", f"正例必须过：{err}"

    # 我方 userId 也可以由 ctx 提供（快照里没给时，_drive 会从 report 取；两条来源都认）
    ctx2 = _ctx(tmp_path, my_user_id=MY_UID)
    ctx2.platform = _FakePlatform(_report([_wapi_conv()], my_user_id=None))
    conv2, err2 = mod.confirm_sent_by_readback(page, CAND, _SENT, ctx2)
    assert conv2 is not None and err2 == "", f"ctx.my_user_id 应能兜住：{err2}"

    def _one(report, my_user_id=None):
        c = _ctx(tmp_path, my_user_id=my_user_id)
        c.platform = _FakePlatform(report, repeat=True)  # 两轮都读回同一份坏数据
        return mod.confirm_sent_by_readback(page, CAND, _SENT, c)

    cases = {
        "末条不是我们那句": _one(_report([_wapi_conv(lastMsg="别的话")])),
        "fromId 不是我们": _one(_report([_wapi_conv(lastFromId=740868320)])),
        "lastTS 没有前进": _one(_report([_wapi_conv(lastTS=CAND["lastTS"])])),
        "回读来自 DOM 降级": _one(_report([_wapi_conv(source="dom")])),
        "eid 命中不唯一": _one(_report([_wapi_conv(), _wapi_conv()])),
        "myUserId 缺失": _one(_report([_wapi_conv()], my_user_id=None)),
    }
    for label, (_, err_) in cases.items():
        assert err_, f"{label} 必须落原因（fail-closed）"

    empty_conv, empty_err = mod.confirm_sent_by_readback(page, CAND, "", ctx)
    assert empty_conv is None and empty_err, "空文本永不该被判成已发出"


def _snapshot(entries):
    """按生产同款路径造快照（parse_friend_entries → build_report），不手搓报告键。"""
    from platforms import chat_first as chat_mod

    return chat_mod.build_report(
        chat_mod.parse_friend_entries(entries, my_user_id=MY_UID), "2026-10-20 18:00:00", my_user_id=MY_UID
    )


HR_INBOUND = {
    "name": "沐女士",
    "brandName": "煦辰",
    "title": "招聘者",
    "lastMsg": "方便把简历发我看一下吗",
    "lastTS": 1792000000000,
    "lastMessageInfo": {"fromId": 740868320, "status": 0},
    "encryptJobId": "e1",
    "unreadMsgCount": 2,
}


def test_dry_run_never_opens_a_conversation(monkeypatch, tmp_path):
    """用户的硬约束落成一枚闸：dry-run 一次都不点会话（点了就消耗未读，
    而未读是「该去读哪几条」的唯一线索）。这里连 `reply_one` 都不该被叫到，深链也不许发。"""
    called = []
    page = _FakeDeeplinkPage()
    monkeypatch.setattr(mod, "startup_jitter", lambda cfg, **k: None)
    monkeypatch.setattr(
        mod, "job_identity", lambda p, eid: {"jobTitle": "区块链工程师招聘", "state": "ok", "rawTitle": "x"}
    )
    monkeypatch.setattr(mod.la, "detect_risk_signal", lambda p: None)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(mod.random, "uniform", lambda a, b: 0.01)
    monkeypatch.setattr(mod, "reply_one", lambda *a, **k: called.append(1))
    platform = SimpleNamespace(followup=lambda p, a: _snapshot([HR_INBOUND]), login_dialog_selector="#login-dialog")
    report = mod._drive(
        platform, page, _cfg(), [], str(tmp_path / "ledger.json"), tmp_path, tmp_path / "shots", False, (), "start"
    )
    assert called == [], "dry-run 决不能进发送阶段"
    assert page.gotos == [] and page.pressed == [], "一次导航与点击都不许发生"
    assert report["mode"] == "dry_run" and report["eligible"] == 1 and report["inbound"] == 1
    assert report["inboundUnread"] == 1, "未读那列要能从只读快照一路传到回复读数"
    assert report["myUserId"] == MY_UID, "快照里的 userId 要随报告出来（回读判据的来源）"
    assert (tmp_path / "request_1.json").exists(), "dry-run 仍写草稿载荷，供 agent 先看再决定"


def test_dry_run_marks_dead_job_and_keeps_bucket_zero_filled(monkeypatch, tmp_path):
    """失效岗（今日实测 4/16 的形态）在只读阶段就被挡，且**不进**浏览器阶段的桶。"""
    page = _FakeDeeplinkPage()
    monkeypatch.setattr(mod, "startup_jitter", lambda cfg, **k: None)
    monkeypatch.setattr(
        mod, "job_identity", lambda p, eid: {"jobTitle": "Golang是什么职位", "state": "dead", "rawTitle": "x"}
    )
    monkeypatch.setattr(mod.la, "detect_risk_signal", lambda p: None)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    platform = SimpleNamespace(followup=lambda p, a: _snapshot([HR_INBOUND]), login_dialog_selector="#login-dialog")
    report = mod._drive(
        platform, page, _cfg(), [], str(tmp_path / "ledger.json"), tmp_path, tmp_path / "shots", False, (), "start"
    )
    assert report["eligible"] == 0
    assert report["skippedReasons"]["job_dead"] == 1
    assert report["skippedReasons"]["no_input"] == 0, "没跑过的桶恒在且为 0"


def test_run_reply_refuses_without_apply_history(monkeypatch, tmp_path):
    """缺投递台账 ⇒ 拒绝启动。入站这条判据本身是「归不到我们投出去的岗」，
    没有台账时整批对话都会被判成人家主动来撩，包括我们自己投出去的那些。"""
    monkeypatch.setattr(mod, "resolve_active_profile", lambda **k: ("default", ""))
    monkeypatch.setattr(mod, "private_path", lambda name, prefix="": tmp_path / name)
    monkeypatch.setattr(mod, "Camoufox", lambda **k: (_ for _ in ()).throw(AssertionError("不该启浏览器")))
    assert mod.run_reply(env={}, send=False) is None


def test_run_reply_stops_in_quiet_hours_without_browser(monkeypatch, tmp_path):
    """静默时段：不启浏览器（省一次真实登录态暴露），判据与投递同一条 `is_quiet_hour`。"""
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "applied_history.json").write_text('{"applied": []}', encoding="utf-8")
    monkeypatch.setattr(mod, "resolve_active_profile", lambda **k: ("default", ""))
    monkeypatch.setattr(mod, "private_path", lambda name, prefix="": tmp_path / name)
    monkeypatch.setattr(mod, "is_quiet_hour", lambda h, q: True)
    monkeypatch.setattr(mod, "Camoufox", lambda **k: (_ for _ in ()).throw(AssertionError("静默时段不该启浏览器")))
    assert mod.run_reply(env={}, send=True, only=("e1",)) is None, "点名一条后才走到静默时段这道门"


def test_run_reply_enables_line_buffering_before_anything_else(monkeypatch, tmp_path):
    """#73 顺手项：入场先切行缓冲，否则输出重定向到文件时全程看不到进度（10-21 真发那场只能
    等进程退出）。即使后面因缺台账早退，这一步也必须先发生——所以用例从「早退」那一场验。"""
    calls = []
    monkeypatch.setattr(mod.la, "_enable_line_buffering", lambda: calls.append(1) or True)
    monkeypatch.setattr(mod, "resolve_active_profile", lambda **k: ("default", ""))
    monkeypatch.setattr(mod, "private_path", lambda name, prefix="": tmp_path / name)
    monkeypatch.setattr(mod, "Camoufox", lambda **k: (_ for _ in ()).throw(AssertionError("不该启浏览器")))
    assert mod.run_reply(env={}, send=False) is None
    assert calls == [1], "run_reply 的第一件事就是打开行缓冲"


def test_agent_skip_carries_the_reason(monkeypatch, tmp_path):
    """`{"skip": true, "reason": …}` 的原因要带出来——runbook 让「要附件/要微信」这类交用户处置，
    处置靠的就是那句话，只留计数等于把裁量所需材料丢了。"""
    page = _target_page()
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(mod.la, "wait_reply", lambda d, i, timeout=None: mod.la.SKIP)
    monkeypatch.setattr(mod.la, "reply_reason", lambda d, i: "要附件，不给文本链接")
    out = mod.reply_one(page, CAND, 1, _ctx(tmp_path))
    assert out["status"] == "agent_skip"
    assert out["skip_reason"] == "要附件，不给文本链接" and out["detail"] == "要附件，不给文本链接"


def test_send_round_buckets_conv_unreachable_separately(monkeypatch, tmp_path):
    """深链失败要落**独立桶**：报告里 `conv_unreachable` 计数为 1、历史桶 `conv_not_found` 仍 0。
    不再有「回消息页失败 ⇒ 整场 nav_failed」的分支（发送路径不依赖列表页）。"""
    page = _FakeDeeplinkPage()
    monkeypatch.setattr(mod, "startup_jitter", lambda cfg, **k: None)
    monkeypatch.setattr(
        mod, "job_identity", lambda p, eid: {"jobTitle": "区块链工程师招聘", "state": "ok", "rawTitle": "x"}
    )
    monkeypatch.setattr(mod.la, "detect_risk_signal", lambda p: None)
    monkeypatch.setattr(
        mod, "reply_one", lambda p, c, i, ctx: {"status": "conv_unreachable", "reason": "conv_unreachable"}
    )
    _no_quiet(monkeypatch)
    platform = SimpleNamespace(followup=lambda p, a: _snapshot([HR_INBOUND]), login_dialog_selector="#login-dialog")
    report = mod._drive(
        platform, page, _cfg(), [], str(tmp_path / "ledger.json"), tmp_path, tmp_path / "shots", True, (), "start"
    )
    assert report["skippedReasons"]["conv_unreachable"] == 1
    assert report["skippedReasons"]["conv_not_found"] == 0, "深链失败不许冒充列表未渲染"
    assert (tmp_path / "report.json").exists(), "失败也要落报告，否则这场像什么都没发生过"


def test_send_refuses_when_only_matches_multiple_conversations(monkeypatch, tmp_path):
    """同一 `encryptJobId` 对应两条会话（同岗多招聘者）时，`--only` 只给了一个 id 也分不清
    该开哪一位；开哪条都是猜，所以发前就停、两条都落 `conv_ambiguous`，`reply_one` 一次不调。"""
    page = _FakeDeeplinkPage()
    other = {**HR_INBOUND, "name": "枫先生", "brandName": "别家", "title": "HR", "encryptUid": "enc-uid-2"}
    tried = []
    monkeypatch.setattr(mod, "startup_jitter", lambda cfg, **k: None)
    monkeypatch.setattr(
        mod, "job_identity", lambda p, eid: {"jobTitle": "区块链工程师招聘", "state": "ok", "rawTitle": "x"}
    )
    monkeypatch.setattr(mod.la, "detect_risk_signal", lambda p: None)
    monkeypatch.setattr(mod, "reply_one", lambda *a, **k: tried.append(1) or {"status": "stub"})
    _no_quiet(monkeypatch)
    platform = SimpleNamespace(
        followup=lambda p, a: _snapshot([HR_INBOUND, other]), login_dialog_selector="#login-dialog"
    )
    report = mod._drive(
        platform, page, _cfg(), [], str(tmp_path / "ledger.json"), tmp_path, tmp_path / "shots", True, ("e1",), "start"
    )
    assert tried == [], "无法唯一确定哪条会话时一条都不许开"
    assert report["skippedReasons"]["conv_ambiguous"] == 2
    assert report["stopSignal"] == "conv_ambiguous"


def test_risk_signal_stores_evidence_like_the_apply_path(monkeypatch, tmp_path):
    """runbook「边界」对熔断的要求是「停下**并存证** `private/data/crash/`」——入站这一面不能只
    停下、把那句承诺读成只对投递生效。"""
    saved = []
    page = _target_page()
    ctx = _ctx(tmp_path)
    ctx.platform = SimpleNamespace(save_evidence=lambda p, tag, cand: saved.append(tag), login_dialog_selector="#x")
    _patch_runtime(monkeypatch, risk="出现验证码")
    out = mod.reply_one(page, CAND, 1, ctx)
    assert out["status"] == "risk_blocked" and ctx.stop == "stop"
    assert saved == ["inbound-risk-1"], "熔断现场必须落到 crash/，否则事后无从复核"


def test_startup_jitter_only_for_the_sending_round(monkeypatch, tmp_path):
    """只读轮不等「模拟真人开工时间」——10-21 实测那一跳是 593s，而它这段时间不产生任何对外可见
    动作，伪装换不来风控收益，只把一场取数拖成十分钟。真发那一场照旧抖（那一刻起每个动作都可看见）。"""

    def followup(p, applied):
        p.goto(CHAT_URL)
        return _snapshot([HR_INBOUND])

    def fake_identity(p, eid):
        return {"jobTitle": "区块链工程师招聘", "state": "ok", "rawTitle": "x"}

    def run(send):
        jittered = []
        monkeypatch.setattr(mod, "startup_jitter", lambda cfg, **k: jittered.append(1))
        monkeypatch.setattr(mod, "job_identity", fake_identity)
        monkeypatch.setattr(mod.la, "detect_risk_signal", lambda p: None)
        monkeypatch.setattr(mod.time, "sleep", lambda s: None)
        monkeypatch.setattr(mod, "reply_one", lambda *a, **k: {"status": "stub"})
        _no_quiet(monkeypatch)
        platform = SimpleNamespace(followup=followup, login_dialog_selector="#login-dialog")
        mod._drive(
            platform,
            _FakeDeeplinkPage(),
            _cfg(),
            [],
            str(tmp_path / "ledger.json"),
            tmp_path,
            tmp_path / "shots",
            send,
            (),
            "start",
        )
        return jittered

    assert run(False) == [], "dry-run 不该等启动抖动"
    assert run(True) == [1], "真发那一场仍要抖（这是用户刻意保留的伪装）"


def test_bare_send_is_refused_before_anything_opens(monkeypatch, tmp_path):
    """用户 2026-10-21 定的规则（选 B）：脚本永不替他决定「这条读没读」⇒ 真发必须单条点名。
    裸 `--send` 一次会连开十几条他并没打算读的会话，未读全烧掉——所以这是在启浏览器之前
    就拒绝，不是"少写一层校验"。"""
    monkeypatch.setattr(mod, "resolve_active_profile", lambda **k: ("default", ""))
    monkeypatch.setattr(mod, "Camoufox", lambda **k: (_ for _ in ()).throw(AssertionError("不该启浏览器")))
    assert mod.run_reply(env={}, send=True, only=()) is None
    assert mod.main(["--send"]) == 1, "CLI 层也得非零退出，不能静悄悄什么都不做"


def test_send_with_two_only_is_refused_before_anything_opens(monkeypatch, tmp_path):
    """`--only` 在 CLI 层是可重复的（dry-run 过滤用），但真发必须**恰好一条**：
    传两个等于一次开两条，绕过了「一次只发一条、发一条验一条」的授权粒度。"""
    monkeypatch.setattr(mod, "resolve_active_profile", lambda **k: ("default", ""))
    monkeypatch.setattr(mod, "Camoufox", lambda **k: (_ for _ in ()).throw(AssertionError("不该启浏览器")))
    assert mod.run_reply(env={}, send=True, only=("e1", "e2")) is None
    assert mod.main(["--send", "--only", "e1", "--only", "e2"]) == 1


def test_named_send_still_passes_the_gate(monkeypatch, tmp_path):
    """正对照（不然上一条只要一直返回 None 就永远"通过"）：点名之后那道门要放行，
    走到下一道检查（缺投递台账拒启）——证明 None 是**过了门之后**被别的门拦的。"""
    calls = []
    monkeypatch.setattr(mod, "resolve_active_profile", lambda **k: ("default", ""))
    monkeypatch.setattr(mod, "private_path", lambda name, prefix="": tmp_path / name)
    monkeypatch.setattr(mod, "Camoufox", lambda **k: (_ for _ in ()).throw(AssertionError("不该启浏览器")))
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    out = mod.run_reply(env={}, send=True, only=("e1",))
    assert out is None  # 缺 applied_history ⇒ 下一道门拦下
    assert not calls


# --- 水位线读数（#75）：先落第一条 red——报告里根本没有这两个键 ---


def _drive_readonly(monkeypatch, tmp_path, entries, **over):
    """只读跑一场（dry-run 与 `--mark-read` 共用这条路径），返回 `(报告, 页面替身)`。"""
    page = _FakeDeeplinkPage()
    monkeypatch.setattr(mod, "startup_jitter", lambda cfg, **k: None)
    monkeypatch.setattr(
        mod, "job_identity", lambda p, eid: {"jobTitle": "区块链工程师招聘", "state": "ok", "rawTitle": "x"}
    )
    monkeypatch.setattr(mod.la, "detect_risk_signal", lambda p: None)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(mod.random, "uniform", lambda a, b: 0.01)
    platform = SimpleNamespace(followup=lambda p, a: _snapshot(entries), login_dialog_selector="#login-dialog")
    kwargs = {"watermark_path": str(tmp_path / "inbound_watermark.json"), "profile": "default"}
    kwargs.update(over)
    report = mod._drive(
        platform,
        page,
        _cfg(),
        [],
        str(tmp_path / "ledger.json"),
        tmp_path,
        tmp_path / "shots",
        False,
        (),
        "start",
        **kwargs,
    )
    return report, page


def test_report_always_carries_the_watermark_keys(monkeypatch, tmp_path):
    """#75 验收④（首跑形态）：两个读数键恒在，且首跑的 `newSinceWatermark` 是「无历史基准」
    而不是把全部条数当新增。"""
    report, _page = _drive_readonly(monkeypatch, tmp_path, [HR_INBOUND])
    assert "newSinceWatermark" in report and "watermark" in report
    assert report["newSinceWatermark"] is None, "无基准时不报条数——报「新增 1 条」会被读成「多了一条没看的」"
    assert report["watermark"]["state"] == "first_run"


HR_LATER = {**HR_INBOUND, "encryptJobId": "e2", "name": "枫先生", "brandName": "别家", "lastTS": 1792999999000}
PREVIOUS_READ = "2026-10-19 09:00:00"  # 「上次读数」的时刻（本场快照固定是 2026-10-20 18:00:00）


def _seed_watermark(tmp_path, entries, profile="default", at=PREVIOUS_READ):
    """把「上次读数」落成基准文件——走真实写入口，不手搓 JSON 形态（手搓的形态会骗过用例）。"""
    path = tmp_path / "inbound_watermark.json"
    res = wm.advance_watermark(
        path, _snapshot(entries)["conversations"], wm.empty_base(profile), profile=profile, snapshot_at=at
    )
    assert res["written"], res
    return path


def test_two_readonly_runs_report_the_same_n_and_write_nothing(monkeypatch, tmp_path, capsys):
    """#75 岔口 1（最重要的一条）：**默认绝不自动推进**。两场只读报同一个 N，
    基准文件一个字节都不动。脚本每跑一次就推进＝把「用户没看过的」偷换成「脚本没见过的」，
    下一场报「新增 0」而用户一条没看——那才是本票真正要防的事故。"""
    path = _seed_watermark(tmp_path, [HR_INBOUND])
    before = path.read_text(encoding="utf-8")
    first, _ = _drive_readonly(monkeypatch, tmp_path, [HR_INBOUND, HR_LATER])
    assert first["newSinceWatermark"] == 1, first
    second, _ = _drive_readonly(monkeypatch, tmp_path, [HR_INBOUND, HR_LATER])
    assert second["newSinceWatermark"] == 1, "同一批读数连跑两场必须报同一个 N"
    assert second["watermark"]["snapshotAt"] == PREVIOUS_READ, "基准时刻不该被第二场悄悄换掉"
    assert path.read_text(encoding="utf-8") == before, "只读路径一个字都不许写基准"
    out = capsys.readouterr().out
    assert out.count("较上次读数新增 1 条") == 2, out


def test_mark_read_advances_the_whole_read_batch_and_says_so(monkeypatch, tmp_path, capsys):
    """显式 `--mark-read`（语义＝用户当面确认「这次读到的这批我看过了」）才写新值：
    推进的是**本次读到的整批**的键集合与最大 lastTS，终端回显那一行是它的回执。"""
    path = _seed_watermark(tmp_path, [HR_INBOUND])
    report, _ = _drive_readonly(monkeypatch, tmp_path, [HR_INBOUND, HR_LATER], mark_read=True)
    assert report["newSinceWatermark"] == 1
    out = capsys.readouterr().out
    assert (
        f"水位线已从 {PREVIOUS_READ} 推进到 2026-10-20 18:00:00：本次读到 2 条会话、"
        "候选清单列出 2 条、记入 2 个会话键（较上次读数新增 1 条）" in out
    ), out
    assert "推进的是本次读到的整批" in out, out
    data = json.loads(path.read_text(encoding="utf-8"))
    assert sorted(data["keys"]) == ["e1", "e2"] and data["maxLastTS"] == 1792999999000
    assert data["snapshotAt"] == "2026-10-20 18:00:00", "新基准的时刻＝本场读数的时刻，不是写盘那一刻"
    again, _ = _drive_readonly(monkeypatch, tmp_path, [HR_INBOUND, HR_LATER])
    assert again["newSinceWatermark"] == 0, "推进过的这批，下一场不再算新增"


def test_mark_read_echo_separates_read_listed_and_recorded_counts(monkeypatch, tmp_path, capsys):
    """条目 F②：回显里那个 N 原来是「线上新」的分臂数，最容易被念成候选清单条数；而进基准的是
    **本次读到的整批**（没被列出的那些也算看过——只推列出的那几条会让其余每场重复提醒）。
    三个数各是各的意思，所以这场故意让它们互不相等：读到 3、列出 2、记入 3、线上新 1。"""
    _seed_watermark(tmp_path, [HR_INBOUND])
    ours = {**HR_INBOUND, "encryptJobId": "e9", "name": "涓先生", "lastMessageInfo": {"fromId": MY_UID, "status": 1}}
    report, _ = _drive_readonly(monkeypatch, tmp_path, [HR_INBOUND, HR_LATER, ours], mark_read=True)
    out = capsys.readouterr().out
    assert "本次读到 3 条会话、候选清单列出 2 条、记入 3 个会话键（较上次读数新增 1 条）" in out, out
    assert report["candidates"] == 2, "末条是我方那条不进候选清单（inbound_waiting 判掉）"
    keys = json.loads((tmp_path / "inbound_watermark.json").read_text(encoding="utf-8"))["keys"]
    assert sorted(keys) == ["e1", "e2", "e9"], "没列出的那条同样入基准（整批语义）"
    again, _ = _drive_readonly(monkeypatch, tmp_path, [HR_INBOUND, HR_LATER, ours])
    assert again["newSinceWatermark"] == 0, "其余那几条若不入基准，下一场会被重复提醒"


def test_mark_read_never_opens_a_conversation(monkeypatch, tmp_path):
    """验收②：`--mark-read` 也只写文件——它跑的是那一场只读，一次会话都不打开、一张截图都不落。"""
    called = []
    _seed_watermark(tmp_path, [HR_INBOUND])
    monkeypatch.setattr(mod, "reply_one", lambda *a, **k: called.append(1))
    report, page = _drive_readonly(monkeypatch, tmp_path, [HR_INBOUND, HR_LATER], mark_read=True)
    assert report["mode"] == "dry_run"
    assert called == [] and page.gotos == [] and page.pressed == [], "推进水位线不是动作，是读数"
    assert list((tmp_path / "shots").glob("*")) == [], "截图目录为空才算全程零点击"


# DOM 降级路径的卡片：两样加密 id 都没有（`parse_friend_entries` 也回填不到 eid，applied 为空）
KEYLESS_ENTRY = {
    "name": "沐女士",
    "brandName": "煦辰",
    "title": "招聘者",
    "lastMsg": "方便把简历发我看一下吗",
    "lastTS": 1792000000000,
    "lastMessageInfo": {"fromId": 740868320, "status": 0},
}


def test_keyless_mark_read_writes_nothing_and_next_run_still_has_no_baseline(monkeypatch, tmp_path, capsys):
    """review 条目 A 的消费者面：DOM 降级那场一个可用会话键都拿不到 ⇒ `--mark-read` 不许把
    「0 键」写成可用基准（票面禁止形态换了门进来：下一场会把全部条数念成「新增 N 条」）。
    这一格必须是**可读降级**——终端说清「基准未推进：本次读数无可记录的会话键」，
    文件不出现，下一场照旧报「无历史基准」。"""
    report, _ = _drive_readonly(monkeypatch, tmp_path, [KEYLESS_ENTRY, dict(KEYLESS_ENTRY)], mark_read=True)
    out = capsys.readouterr().out
    assert "[水位线] 未推进" in out and "无可记录的会话键" in out, out
    assert not (tmp_path / "inbound_watermark.json").exists(), "零键读数连基准文件都不该建"
    assert report["newSinceWatermark"] is None and report["watermark"]["state"] == "first_run"
    assert report["watermark"]["unkeyed"] == 2, "记不进基准的那几条要念得出，不静默丢"
    _drive_readonly(monkeypatch, tmp_path, [KEYLESS_ENTRY])
    out2 = capsys.readouterr().out
    assert "无历史基准" in out2 and "新增 1 条" not in out2, out2


def test_table_shows_which_candidates_are_above_the_watermark(monkeypatch, tmp_path, capsys):
    """人读表格看得见「哪几条在线上」（票面接线要求）：线上新/线以下逐条给，不让人回去翻 JSON。"""
    _seed_watermark(tmp_path, [HR_INBOUND])
    _drive_readonly(monkeypatch, tmp_path, [HR_INBOUND, HR_LATER])
    out = capsys.readouterr().out
    rows = [line for line in out.splitlines() if line.strip().startswith("▸")]
    assert len(rows) == 2, out
    # 原来这里还有一句「两行的『线上新』计数等于 [1,0] 或 [0,1]」——它是下面那条（恰有一行含
    # 线上新、且那一行含 e2）的同义反复，删掉不减少覆盖（条目 H①）。
    above = [line for line in rows if "线上新" in line]
    assert len(above) == 1 and "e2" in above[0], above
    assert "较上次读数新增 1 条" in out, out


def test_our_own_last_message_is_not_counted_as_due(monkeypatch, tmp_path):
    """验收③前半：自己刚发出去的那条不算「该读」（`fromId == myUserId`，与 boss 判 awaiting 同源），
    哪怕它的会话键是新的、时间戳也更新了。"""
    ours = {**HR_INBOUND, "encryptJobId": "e9", "lastMessageInfo": {"fromId": MY_UID, "status": 1}}
    _seed_watermark(tmp_path, [HR_INBOUND])
    report, _ = _drive_readonly(monkeypatch, tmp_path, [HR_INBOUND, ours])
    assert report["newSinceWatermark"] == 0, report
    assert report["watermark"]["newConversations"] == 0


def test_older_conversation_with_their_new_message_is_due(monkeypatch, tmp_path):
    """验收③后半：老会话里对方新发一条要被算进来（键在基准里、lastTS 越过了基准的最大值）。"""
    _seed_watermark(tmp_path, [HR_INBOUND])
    report, _ = _drive_readonly(monkeypatch, tmp_path, [{**HR_INBOUND, "lastTS": 1792999999000}])
    assert report["newSinceWatermark"] == 1, report
    assert report["watermark"]["updatedConversations"] == 1 and report["watermark"]["newConversations"] == 0


def test_first_run_lists_nothing_as_above_the_line_and_says_why(monkeypatch, tmp_path, capsys):
    """验收④（首跑）：N 是「无历史基准」而不是「全部条数」，候选整列按「无从判定」呈现，
    落盘的 report.json 与内存读数一致。"""
    report, _ = _drive_readonly(monkeypatch, tmp_path, [HR_INBOUND, HR_LATER])
    assert report["newSinceWatermark"] is None and report["watermark"]["state"] == "first_run"
    out = capsys.readouterr().out
    assert "无历史基准" in out and "新增 2 条" not in out, out
    assert "线上新" not in out, "无基准时不假装知道哪条是新的"
    on_disk = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert on_disk["newSinceWatermark"] is None and on_disk["watermark"]["state"] == "first_run"
    assert not (tmp_path / "inbound_watermark.json").exists(), "首跑只读不建基准——建它是 --mark-read 的事"


def test_corrupt_baseline_degrades_loudly_and_survives_mark_read(monkeypatch, tmp_path, capsys):
    """验收④（损坏）：N 不给、state 说清、原文件保留不覆盖（#64 口径）；
    `--mark-read` 也**拒绝**在读不懂的现场上盖新值——那是排查材料。"""
    path = tmp_path / "inbound_watermark.json"
    path.write_text("{这不是 JSON", encoding="utf-8")
    report, _ = _drive_readonly(monkeypatch, tmp_path, [HR_INBOUND])
    assert report["newSinceWatermark"] is None and report["watermark"]["state"] == "corrupt"
    out = capsys.readouterr().out
    assert "无从比较" in out and "原样保留不覆盖" in out, out
    _drive_readonly(monkeypatch, tmp_path, [HR_INBOUND], mark_read=True)
    out2 = capsys.readouterr().out
    assert "未推进" in out2, out2
    assert path.read_text(encoding="utf-8") == "{这不是 JSON"


def test_run_reply_passes_the_profile_prefixed_watermark_path(monkeypatch, tmp_path):
    """#28 口径：基准按 profile 隔离。注册表 default 换人时两个档会共用同一个文件名，
    所以既要有 `<name>.` 前缀的路径，也要文件里的 profile 字段兜住——这里钉的是路径这一半。"""
    (tmp_path / "ai.applied_history.json").write_text('{"applied": []}', encoding="utf-8")
    captured = {}
    monkeypatch.setattr(mod, "resolve_active_profile", lambda **k: ("ai", "ai."))
    monkeypatch.setattr(mod, "private_path", lambda name, prefix="": tmp_path / f"{prefix}{Path(name).name}")
    monkeypatch.setattr(mod, "is_quiet_hour", lambda h, q: False)

    def fake_drive(*args, **kwargs):
        captured.update(kwargs)
        return {"stub": True}

    monkeypatch.setattr(mod, "_drive", fake_drive)
    monkeypatch.setattr(mod, "Camoufox", lambda **k: _FakeCamoufoxCtx())
    assert mod.run_reply(env={}) == {"stub": True}
    assert Path(captured["watermark_path"]).name == "ai.inbound_watermark.json", captured
    assert captured["profile"] == "ai"


class _FakeCamoufoxCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def new_page(self):
        return object()


def _is_bare_mark_read_test(test):
    """这个 `if` 的判据**本身**是不是「指名 `mark_read` 的裸 Name/Attribute」。

    条目 D 收紧的地方：原判据是「`ast.dump(test)` 里出现过 `mark_read` 字样」，于是
    `if not mark_read:`（推进挂在了反臂上）与 `if mark_read and send:`（合取里还挂着别的条件）
    都被念成「已守」。这两种形状下推进的都不是「用户说看过了」那一刻，钉据必须把它们判违规。"""
    if isinstance(test, ast.Name):
        return test.id == "mark_read"
    if isinstance(test, ast.Attribute):
        return test.attr == "mark_read"
    return False


def _in_if_body(if_node, node):
    """`node` 落在这个 `if` 的 **body** 子树里吗（`elif`／`else` 那一支不算守住）。"""
    return any(node is sub for stmt in if_node.body for sub in ast.walk(stmt))


def _advance_call_sites(path):
    """源码里所有 `advance_watermark` **调用点** → `(行号, 是否只在 `if mark_read:` 的 body 里)`。

    相对路径按本测试文件所在目录解析（生产模块与用例同目录平铺）；用例植入的临时副本传绝对路径。
    写入口的名字只有一个：`write_watermark` 全仓不存在，原判据一起匹配它等于留了个永远为空的
    防分支（看着有防护、实际一处也钉不到），条目 D 点名后删掉。"""
    p = Path(path)
    source = (p if p.is_absolute() else Path(__file__).parent / p).read_text(encoding="utf-8")
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = ""
        if isinstance(fn, ast.Attribute):
            name = fn.attr
        elif isinstance(fn, ast.Name):
            name = fn.id
        if name != "advance_watermark":
            continue
        # 沿祖先链向上走到**最近**的那个 `if`——它就是这次调用的守门条件；再往上的 `if` 不算，
        # 否则「外层恰好有个 mark_read 分支、内层却挂到别的条件上」会被误判成守住。
        holder, guarded, seen_if = node, False, False
        while holder in parents:
            holder = parents[holder]
            if isinstance(holder, ast.If):
                seen_if = True
                guarded = _is_bare_mark_read_test(holder.test) and _in_if_body(holder, node)
                break
        if not seen_if:
            guarded = False  # 裸只读路径上的调用：没有任何 `if` 守着它
        sites.append((node.lineno, guarded))
    return sites


def test_mark_read_on_an_unwired_path_refuses_and_litters_nothing(monkeypatch, tmp_path, capsys):
    """条目 E 的消费者面：`watermark_path` 空＝调用方没接线，`--mark-read` 要**显式拒绝**并说清原因，
    不能按 `Path("")` → `.` 去 CWD 落一个孤儿 `.tmp`（`write_json_atomic` 的 `mkdir(".")` 会过，
    tmp 名成 `..tmp`，`os.replace("..tmp", ".")` 再炸——垃圾留在场，读数也没了）。"""
    monkeypatch.chdir(tmp_path)
    report, _ = _drive_readonly(monkeypatch, tmp_path, [HR_INBOUND], mark_read=True, watermark_path="")
    out = capsys.readouterr().out
    assert "[水位线] 未推进" in out and "路径" in out, out
    assert report["watermark"]["state"] == "first_run"
    assert not (tmp_path / "..tmp").exists() and not (tmp_path / ".tmp").exists(), "CWD 不许留孤儿 tmp 文件"


# --- 源码钉（条目 D）：守的是「只读路径推不推进」，判据本身也要有用例 ---


def test_source_pin_catches_the_negated_conjunct_and_bare_sites(tmp_path):
    """条目 D 的自证：光钉现网那一条 call site 判不出**它守的是什么**——原来的判据是
    「某个 enclosing `ast.If` 的 test 里出现过 `mark_read` 字样」，于是 `if not mark_read:`
    （推进挂在反臂上）与 `if mark_read and send:`（合取）都被念成「已守」，只读裸调用以外的
    形态全放过去了。这里把四种真实形状写进临时副本，逐个验判据。"""
    calls = "    wm.advance_watermark(p, convs, base, profile=profile)\n"
    shapes = {
        "if_mark_read": ("if mark_read:\n" + calls, True),
        "if_not_mark_read": ("if not mark_read:\n" + calls, False),
        "if_mark_read_and_send": ("if mark_read and send:\n" + calls, False),
        "bare_readonly_call": (calls.strip() + "\n", False),
        "call_in_else": ("if sending:\n    pass\nelse:\n" + calls, False),
    }
    for label, (source, expected) in shapes.items():
        p = tmp_path / f"{label}.py"
        p.write_text(source, encoding="utf-8")
        sites = _advance_call_sites(p)
        assert sites, f"{label}：植入的调用点没被发现，这条钉是空的"
        assert [guarded for _, guarded in sites] == [expected], f"{label} 判据错位：{sites}"


def test_watermark_advance_is_reachable_only_from_mark_read():
    """源码钉：只读路径不许推进基准。行为用例钉得住这一场，钉不住一年后的重构——「顺手在只读
    末尾也记一次」看着像补漏，实际把 #75 的整个语义换掉了（本仓有这种防回退用例的先例）。"""
    hits = _advance_call_sites("reply_inbound.py")
    assert hits, "reply_inbound 必须真的调得到写入口，否则这条钉是空的"
    assert all(guarded for _, guarded in hits), f"只读分支里出现了推进：{[ln for ln, guarded in hits if not guarded]}"
    for other in ("followup_tracker.py", "live_apply.py", "loop.py", "orchestrate.py"):
        assert _advance_call_sites(other) == [], f"{other} 不许推进水位线（只读链路只报不写）"


def test_mark_read_cli_refuses_the_combinations_it_means_to(monkeypatch, tmp_path):
    """`--mark-read` 的语义是「这一批我看过了」：与 `--send` 同场（那会点开会话）或与 `--only`
    同场（那列出的只是子集）都推不出「这批」，两层都得拒绝；但 `--mark-read` 单独跑要放行。"""
    monkeypatch.setattr(mod, "resolve_active_profile", lambda **k: ("default", ""))
    monkeypatch.setattr(mod, "Camoufox", lambda **k: (_ for _ in ()).throw(AssertionError("不该启浏览器")))
    assert mod.main(["--mark-read", "--send", "--only", "e1"]) == 1
    assert mod.main(["--mark-read", "--only", "e1"]) == 1
    assert mod.run_reply(env={}, send=True, only=("e1",), mark_read=True) is None
    assert mod.run_reply(env={}, only=("e1",), mark_read=True) is None
    passed = {}

    def spy(**kwargs):
        passed.update(kwargs)
        return None

    monkeypatch.setattr(mod, "run_reply", spy)
    assert mod.main(["--mark-read"]) == 1
    assert passed.get("mark_read") is True and passed.get("send") is False, "拒的是组合，不是 --mark-read 本身"


# --- 候选表「对方那句原文」的截断可见标记（#66 验收 1 的接线侧） ---
# 依据是 #66 票面验收①「汇报里恒有逐条公司·岗位·原文」与关票后的订正（这一格当时不带截断标记）。
# **不是** platforms/chat_first.py 那句「措辞也必须同源」——那句管的是 `STATE_LABELS` 的读态标签名字，
# 不涵盖截断标记；两边同源的只有判据（`boss.read_state`），窗口各算各的（这边 52、那边 46）。


def _table_rows(out):
    """候选表打出来的 `▸` 行（只取展示层，不经 `_drive`/`run_reply`，因此零浏览器、零发送面）。"""
    return [ln for ln in out.splitlines() if ln.strip().startswith("▸")]


def test_inbound_table_marks_truncated_their_message(capsys):
    """原文**长于 52 字** ⇒ 行尾要带 `…`（#66 验收 1）：静默截断会把「对方到底说了什么」读成
    「对方就说完了」，而这一格是 `--send --only <eid>` 点名单条时唯一的现场读数处。
    候选一律由生产码 `classify` 产出（照 `2a7e7c5` 在 test_followup_tracker 里立的规矩：形状由
    生产码给，不由夹具手搓），用例只覆盖原文那一格——`CAND` 没有 `source` / `sinceWatermark` /
    `jobState`，拿它当承重那条的输入等于让展示层读一组生产上不会同时出现的键。
    两条断言各拦一头：`endswith` 钉版式，`"方" * 53 not in` 钉**真的切了**——只写前者时 53 字
    的全文行也以同样字符结尾，把生产里的切片整个删掉三条用例照样全绿（本轮 review 查出的洞）。"""
    cand = mod.classify({"conversations": [_conv(last="方" * 53)]}, {"e1": IDENT_OK}, set())[0]
    mod.print_inbound_table([cand])
    rows = _table_rows(capsys.readouterr().out)
    assert len(rows) == 1, rows
    assert rows[0].endswith("方" * 52 + "…"), rows[0]
    assert "方" * 53 not in rows[0], rows[0]


def test_inbound_table_leaves_a_message_exactly_at_the_window_unmarked(capsys):
    """等号边界单独钉：原文**恰好 52 字**时一个字都没被切掉，行尾不许有 `…`。
    「只把切片改成带标记、判据却写成 `>=`」正是上一轮 review 认定会复发的形态（补标记的条件
    必须和切片的窗口同源，所以那边收成 `window` 单点绑定后，这里也照同一个条件表达式）。
    这一条与下一条钉的都是补标记的两个边界，值本身在生产上取得到（`classify` 的 `lastMsg`
    取值链是 `conv.get("lastMsg") or conv.get("summary") or ""`），所以继续直接在 dict 上覆盖
    那一格，不必再走一遍 `classify`——形状由生产码给那条规矩管的是承重用例读到的键集。"""
    exact = "方" * 52
    mod.print_inbound_table([{**CAND, "lastMsg": exact}])
    rows = _table_rows(capsys.readouterr().out)
    assert len(rows) == 1, rows
    assert rows[0].endswith(exact) and "…" not in rows[0], rows[0]


def test_inbound_table_leaves_an_empty_or_missing_message_unmarked(capsys):
    """原文为空串 / 整键缺失 ⇒ 这一格照旧是空，不加 `…` 也不换成别的措辞（钉现状：
    「无原文」要不要有个显式占位是另一种设计，本任务不授权）。
    空串那支是 `classify` 取值链走到底的真实产出；**缺键那支只能是手搓 dict**——`classify`
    恒写 `lastMsg` 键，生产路径不会交出没有这一格的候选，这里保留它是为了钉展示层用的是
    `cand.get(...)`：少一个键既不炸、也不静默换成别的写法。"""
    no_key = {k: v for k, v in CAND.items() if k != "lastMsg"}
    mod.print_inbound_table([{**CAND, "lastMsg": ""}, no_key])
    rows = _table_rows(capsys.readouterr().out)
    assert len(rows) == 2, rows
    for row in rows:
        assert row.endswith("对方：") and "…" not in row, row
