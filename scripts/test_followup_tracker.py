#!/usr/bin/env python3
"""followup_tracker 标注持久化测试（#19）：回复分类/时延结构化落盘，不触发浏览器。"""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from platforms.chat_first import build_report  # noqa: E402  #入站用例的读数交给生产函数算（理由见 _run_inbound_report）

_spec = importlib.util.spec_from_file_location("followup_tracker", SCRIPTS / "followup_tracker.py")
assert _spec is not None and _spec.loader is not None, "importlib spec 加载失败（模块文件缺失？）"
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _inbound_watermark as wm  # noqa: E402  #75 水位线单一真源（接线用例直接走它的读/写入口）


VALID_CATEGORIES = {"interested", "tech_question", "rejected", "no_reply"}


def _report():
    return {
        "generatedAt": "2026-09-09 13:00:00",
        "totalConversations": 2,
        "hrReplied": 1,
        "awaiting": 1,
        "replyRate": "50%",
        "linkedToApplied": 0,
        "linkedReplied": 0,
        "conversations": [
            {
                "name": "13:10",
                "company": "上海珀澜科技HR",
                "summary": "你好，方便聊聊吗",
                "time": "7年Solidity，做过稳定币",
                "status": "hr_replied",
                "appliedJob": "DeFi工程师",
            },
            {
                "name": "13:20",
                "company": "北京中利博冠",
                "summary": "[送达]",
                "time": "5年Rust",
                "status": "awaiting",
                "appliedJob": "链上开发",
            },
        ],
    }


def _ann(company, category, latency=None, applied_job=None, intent_sub=None):
    a = {"company": company, "replyCategory": category}
    if latency is not None:
        a["replyLatencyHours"] = latency
    if applied_job is not None:
        a["appliedJob"] = applied_job
    if intent_sub is not None:
        a["intent_sub"] = intent_sub
    return a


# --- 分类校验 ---


def test_validate_annotation_rejects_unknown_category():
    with pytest.raises(ValueError):
        mod.validate_annotation(_ann("某公司", "maybe_interested"))


# --- intent_sub 二级标签（#21 修订方案 §2） ---


def test_validate_annotation_accepts_intent_sub_on_interested():
    for sub in ("interview", "resume", "salary"):
        assert mod.validate_annotation(_ann("某公司", "interested", intent_sub=sub)) is True


def test_validate_annotation_rejects_intent_sub_on_other_categories():
    with pytest.raises(ValueError):
        mod.validate_annotation(_ann("某公司", "rejected", intent_sub="interview"))
    with pytest.raises(ValueError):
        mod.validate_annotation(_ann("某公司", "no_reply", intent_sub="salary"))


def test_validate_annotation_rejects_unknown_intent_sub():
    with pytest.raises(ValueError):
        mod.validate_annotation(_ann("某公司", "interested", intent_sub="chat"))


def test_merge_annotations_attaches_intent_sub():
    report = _report()
    out = mod.merge_annotations(report, [_ann("上海珀澜科技HR", "interested", 6.5, intent_sub="resume")])
    conv = out["conversations"][0]
    assert conv["intent_sub"] == "resume" and conv["replyCategory"] == "interested"
    assert "intent_sub" not in out["conversations"][1], "未标注对话不带 intent_sub"


def test_validate_annotation_accepts_known_categories():
    for c in VALID_CATEGORIES:
        assert mod.validate_annotation(_ann("某公司", c)) is True


# --- 标注合并 ---


def test_merge_annotations_attaches_fields():
    report = _report()
    out = mod.merge_annotations(report, [_ann("上海珀澜科技HR", "interested", 6.5)])
    conv = out["conversations"][0]
    assert conv["replyCategory"] == "interested"
    assert conv["replyLatencyHours"] == 6.5


def test_merge_annotations_matches_by_company_and_job():
    report = _report()
    # 同公司两条对话时 appliedJob 参与精确匹配
    report["conversations"].append(
        {
            "name": "13:30",
            "company": "上海珀澜科技HR",
            "summary": "[送达]",
            "time": "3年Go",
            "status": "awaiting",
            "appliedJob": "后端开发",
        }
    )
    anns = [
        _ann("上海珀澜科技HR", "tech_question", 2.0, applied_job="DeFi工程师"),
        _ann("上海珀澜科技HR", "no_reply", applied_job="后端开发"),
    ]
    out = mod.merge_annotations(report, anns)
    assert out["conversations"][0]["replyCategory"] == "tech_question"
    assert out["conversations"][2]["replyCategory"] == "no_reply"


def test_merge_annotations_without_job_applies_to_all_company_convs():
    report = _report()
    out = mod.merge_annotations(report, [_ann("上海珀澜科技HR", "rejected")])
    assert out["conversations"][0]["replyCategory"] == "rejected"


def test_merge_annotations_unmatched_conv_untouched():
    report = _report()
    out = mod.merge_annotations(report, [_ann("不存在公司", "interested")])
    assert "replyCategory" not in out["conversations"][0]


# --- 投递记录关联（encryptJobId 回填） ---


def test_link_applied_ids_backfills_encrypt_job_id():
    report = _report()
    applied = [{"encryptJobId": "eid-1", "jobName": "DeFi工程师", "brandName": "珀澜科技"}]
    out = mod.link_applied_ids(report, applied)
    assert out["conversations"][0]["encryptJobId"] == "eid-1"
    assert "encryptJobId" not in out["conversations"][1]


# --- 持久化 ---


def test_upsert_annotations_persists_file(tmp_path):
    path = tmp_path / "reply_annotations.json"
    mod.upsert_annotations(path, [_ann("甲公司", "interested")], now="2026-09-09T19:00:00")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["updated_at"] == "2026-09-09T19:00:00"
    assert len(data["annotations"]) == 1
    # 同 company+appliedJob 再标注 → 原位覆盖，不重复堆积
    mod.upsert_annotations(path, [_ann("甲公司", "rejected")], now="2026-09-10T19:00:00")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data["annotations"]) == 1
    assert data["annotations"][0]["replyCategory"] == "rejected"
    assert data["updated_at"] == "2026-09-10T19:00:00"


def test_load_annotations_accepts_bare_list_and_wrapped(tmp_path):
    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps([_ann("甲公司", "interested")], ensure_ascii=False), encoding="utf-8")
    assert len(mod.load_annotations(bare)) == 1
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(json.dumps({"annotations": [_ann("乙公司", "rejected")]}, ensure_ascii=False), encoding="utf-8")
    assert len(mod.load_annotations(wrapped)) == 1


def test_annotate_blocks_with_clear_message_when_files_missing(tmp_path, capsys):
    ann = tmp_path / "ann.json"
    ann.write_text(json.dumps([_ann("甲公司", "interested")], ensure_ascii=False), encoding="utf-8")
    mod.annotate(
        str(ann),
        out_path=tmp_path / "none.json",
        history_path=tmp_path / "none2.json",
        store_path=tmp_path / "store.json",
    )
    assert "[blocked]" in capsys.readouterr().out
    assert not (tmp_path / "store.json").exists()


def test_validate_annotation_rejects_bad_latency():
    for bad in ("five", -1, True):
        with pytest.raises(ValueError):
            mod.validate_annotation(_ann("甲公司", "interested", bad))
    assert mod.validate_annotation(_ann("甲公司", "interested", 0)) is True
    assert mod.validate_annotation(_ann("甲公司", "interested", 3.5)) is True


# --- 入口机械校验加强（#27 项 2：匹配键必填 + 坏标注友好拒绝） ---


def test_validate_annotation_requires_matching_key():
    """company/appliedJob 是标注的匹配键，两项全空 = 永远无法配对的死数据，入口即拒。"""
    with pytest.raises(ValueError):
        mod.validate_annotation({"replyCategory": "interested"})
    with pytest.raises(ValueError):
        mod.validate_annotation({"company": " ", "replyCategory": "interested"})
    assert mod.validate_annotation({"company": "某公司", "replyCategory": "interested"}) is True
    assert mod.validate_annotation({"appliedJob": "DeFi工程师", "replyCategory": "interested"}) is True


def _annotate_env(tmp_path):
    out = tmp_path / "followup_report.json"
    out.write_text(json.dumps(_report()), encoding="utf-8")
    hist = tmp_path / "applied_history.json"
    hist.write_text(json.dumps({"applied": []}), encoding="utf-8")
    return out, hist, tmp_path / "reply_annotations.json"


def test_annotate_rejects_bad_annotation_without_writing(tmp_path, capsys):
    out, hist, store = _annotate_env(tmp_path)
    ann = tmp_path / "ann.json"
    ann.write_text(json.dumps([{"company": "甲公司", "replyCategory": "maybe"}], ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SystemExit):
        mod.annotate(str(ann), out_path=out, history_path=hist, store_path=store)
    captured = capsys.readouterr().out
    assert "甲公司" in captured and "不合法" in captured, captured
    assert "重写" in captured or "重跑" in captured, captured
    assert not store.exists(), "坏标注不得落盘"


def test_annotate_rejects_malformed_json_without_writing(tmp_path, capsys):
    out, hist, store = _annotate_env(tmp_path)
    ann = tmp_path / "ann.json"
    ann.write_text("{oops", encoding="utf-8")
    with pytest.raises(SystemExit):
        mod.annotate(str(ann), out_path=out, history_path=hist, store_path=store)
    captured = capsys.readouterr().out
    assert "JSON" in captured, captured
    assert not store.exists(), "坏文件不得落盘"


def test_annotate_rejects_bad_top_level_shape_without_writing(tmp_path, capsys):
    """顶层非 list 非标注 wrapped 形态、或元素非 dict：友好拒绝退出码 1，
    不裸抛 AttributeError/TypeError（review 采纳项）。"""
    out, hist, store = _annotate_env(tmp_path)
    for raw in ('{"company": "某公司"}', '["好标注", 42]', '"裸字符串"'):
        ann = tmp_path / "ann.json"
        ann.write_text(raw, encoding="utf-8")
        with pytest.raises(SystemExit):
            mod.annotate(str(ann), out_path=out, history_path=hist, store_path=store)
        captured = capsys.readouterr().out
        assert "[blocked]" in captured, (raw, captured)
        assert not store.exists(), (raw, "坏文件不得落盘")


def test_annotate_rejects_keyless_annotation_with_position(tmp_path, capsys):
    """匹配键全空的标注指不出公司/岗位，报错须落到序号。"""
    out, hist, store = _annotate_env(tmp_path)
    ann = tmp_path / "ann.json"
    ann.write_text(json.dumps([{"replyCategory": "interested"}], ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SystemExit):
        mod.annotate(str(ann), out_path=out, history_path=hist, store_path=store)
    captured = capsys.readouterr().out
    assert "第 1 条" in captured, captured
    assert not store.exists()


def test_main_without_subcommand_falls_back_to_run(monkeypatch):
    import sys

    called = []
    monkeypatch.setattr(mod, "run", lambda: called.append(1))
    monkeypatch.setattr(sys, "argv", ["followup_tracker.py"])
    mod.main()
    assert called == [1], "裸调用必须保持 run() 行为（orchestrate 无参调用依赖）"


def test_validate_annotation_accepts_encrypt_job_id_as_key():
    # #32 段 2：encryptJobId 成为合法匹配键（防 company 解析漂移），三键全空仍拦
    assert mod.validate_annotation({"encryptJobId": "eid1", "replyCategory": "interested"})
    with pytest.raises(ValueError):
        mod.validate_annotation({"replyCategory": "interested"})


def test_upsert_annotations_keeps_eid_entry_apart_from_same_company(tmp_path):
    store = tmp_path / "reply_annotations.json"
    mod.upsert_annotations(
        str(store),
        [
            {"company": "汐女士", "encryptJobId": "eid1", "replyCategory": "interested"},
            {"company": "汐女士", "replyCategory": "no_reply"},
        ],
        now="2026-09-24T10:00:00",
    )
    data = json.loads(store.read_text(encoding="utf-8"))
    assert len(data["annotations"]) == 2, "eid 组与同名 company 组互不吞并"

    mod.upsert_annotations(
        str(store), [{"encryptJobId": "eid1", "replyCategory": "rejected"}], now="2026-09-24T11:00:00"
    )
    data = json.loads(store.read_text(encoding="utf-8"))
    assert len(data["annotations"]) == 2, "同 eid 重复 upsert 原位更新不堆积"
    eid_entry = [a for a in data["annotations"] if a.get("encryptJobId") == "eid1"][0]
    assert eid_entry["replyCategory"] == "rejected"


# --- 空报告不覆盖（192616 实测：登录过期采集 0 对话曾静默抹掉 94 条历史） ---


class _FakeCtx:
    """Camoufox 替身：with 语义 + new_page 返回占位页。"""

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def new_page(self):
        return object()


def test_run_keeps_previous_report_on_empty_conversations(tmp_path, monkeypatch, capsys):
    """采集到 0 对话（登录过期/掉线）→ 不覆盖已有报告，warn 明示沿用。与
    live_apply._collect_fresh_followup 同一条防线。"""
    report_path = tmp_path / "data" / "followup_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps({"totalConversations": 94, "conversations": [{"company": "甲"}] * 94}), encoding="utf-8"
    )
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "applied_history.json").write_text(
        json.dumps({"applied": [{"encryptJobId": "e1"}]}), encoding="utf-8"
    )
    monkeypatch.setattr(mod, "resolve_active_profile", lambda **k: ("default", ""))
    monkeypatch.setattr(mod, "private_path", lambda name, prefix="": tmp_path / name)
    monkeypatch.setattr(
        mod,
        "get_platform",
        lambda: SimpleNamespace(
            followup=lambda page, applied: {
                "totalConversations": 0,
                "hrReplied": 0,
                "awaiting": 0,
                "replyRate": None,
                "conversations": [],
                "linkedToApplied": 0,
                "linkedReplied": 0,
            }
        ),
    )
    monkeypatch.setattr(mod, "Camoufox", _FakeCtx)
    mod.run()
    assert json.loads(report_path.read_text(encoding="utf-8"))["totalConversations"] == 94
    assert "沿用上次报告" in capsys.readouterr().out


def test_run_writes_report_when_conversations_present(tmp_path, monkeypatch, capsys):
    """有对话 → 正常写报告（守卫不误伤正常路径）。"""
    report_path = tmp_path / "data" / "followup_report.json"
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "applied_history.json").write_text(
        json.dumps({"applied": [{"encryptJobId": "e1"}]}), encoding="utf-8"
    )
    monkeypatch.setattr(mod, "resolve_active_profile", lambda **k: ("default", ""))
    monkeypatch.setattr(mod, "private_path", lambda name, prefix="": tmp_path / name)
    monkeypatch.setattr(
        mod,
        "get_platform",
        lambda: SimpleNamespace(
            followup=lambda page, applied: {
                "totalConversations": 1,
                "hrReplied": 1,
                "awaiting": 0,
                "replyRate": 1.0,
                "conversations": [
                    {"company": "甲", "name": "HR", "summary": "hi", "appliedJob": "J1", "status": "hr_replied"}
                ],
                "linkedToApplied": 1,
                "linkedReplied": 1,
                "unlinkedConversations": 0,
                "unlinkedReplied": 0,
                "unreadConversations": 0,
                "unlinkedRepliedUnread": 0,
                "unlinkedRepliedRead": 0,
                "unlinkedRepliedReadUnknown": 0,
            }
        ),
    )
    monkeypatch.setattr(mod, "Camoufox", _FakeCtx)
    mod.run()
    out = capsys.readouterr().out
    assert json.loads(report_path.read_text(encoding="utf-8"))["totalConversations"] == 1
    assert "[统计] 对话 1" in out, out
    assert "[入站线索] 归不到投递记录 0 个" in out, out


def test_run_prints_inbound_leads_awaiting_our_reply(tmp_path, monkeypatch, capsys):
    """入站线索呈报（2026-10-16 用户点名的盲区）：未关联投递记录、末条是对方发言的对话，
    终端要报读数并逐条列出 公司｜职位｜原文——这批不在 eval 分母里，此处是它们的出口。"""
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "applied_history.json").write_text(
        json.dumps({"applied": [{"encryptJobId": "e1"}]}), encoding="utf-8"
    )
    monkeypatch.setattr(mod, "resolve_active_profile", lambda **k: ("default", ""))
    monkeypatch.setattr(mod, "private_path", lambda name, prefix="": tmp_path / name)
    inbound = {
        "company": "甲",
        "name": "HR",
        "title": "招聘者",
        "summary": "要聊一下吗",
        "appliedJob": None,
        "status": "hr_replied",
        "unreadMsgCount": 2,  # 未读（wapi 只读字段，链路不点开会话也能拿到）
    }
    ours = {"company": "乙", "name": "HR", "title": "HR", "summary": "收到", "appliedJob": "J1", "status": "hr_replied"}
    monkeypatch.setattr(
        mod,
        "get_platform",
        lambda: SimpleNamespace(
            followup=lambda page, applied: {
                "totalConversations": 2,
                "hrReplied": 2,
                "awaiting": 0,
                "replyRate": 1.0,
                "conversations": [inbound, ours],
                "linkedToApplied": 1,
                "linkedReplied": 1,
                "unlinkedConversations": 1,
                "unlinkedReplied": 1,
                "unreadConversations": 1,
                "unlinkedRepliedUnread": 1,
                "unlinkedRepliedRead": 0,
                "unlinkedRepliedReadUnknown": 0,
            }
        ),
    )
    monkeypatch.setattr(mod, "Camoufox", _FakeCtx)
    mod.run()
    out = capsys.readouterr().out
    assert "[入站线索] 归不到投递记录 1 个，其中 1 个末条是对方发言" in out, out
    assert "其中有未读标记 1 / 无未读标记 0 / 读态未知 0" in out, out
    assert "▸ [有未读] 甲 | 招聘者 | 要聊一下吗" in out, out
    assert "▸ 乙" not in out, "已关联到投递记录的对话不进此清单（那是我们自己投出去的）"


def test_run_marks_inbound_rows_with_unknown_read_state(tmp_path, monkeypatch, capsys):
    """读态未知的负对照（上一条只验了未读那一支）：wapi 少给 `unreadMsgCount` 或走 DOM 降级时，
    行标必须落「读态未知」，**不许显示成「无未读标记」**——后者会把「我们没看过」谎报成
    「我们看过了没理人」，而用户正是拿这列决定先读哪几条（#66 阶段 2 硬约束的展示侧）。"""
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "applied_history.json").write_text(json.dumps({"applied": []}), encoding="utf-8")
    monkeypatch.setattr(mod, "resolve_active_profile", lambda **k: ("default", ""))
    monkeypatch.setattr(mod, "private_path", lambda name, prefix="": tmp_path / name)
    no_unread = {
        "company": "丙",
        "name": "HR",
        "title": "招聘者",
        "summary": "方便加个微信吗",
        "appliedJob": None,
        "status": "hr_replied",
    }
    monkeypatch.setattr(
        mod,
        "get_platform",
        lambda: SimpleNamespace(
            followup=lambda page, applied: {
                "totalConversations": 1,
                "hrReplied": 1,
                "awaiting": 0,
                "replyRate": 1.0,
                "conversations": [no_unread],
                "linkedToApplied": 0,
                "linkedReplied": 0,
                "unlinkedConversations": 1,
                "unlinkedReplied": 1,
                "unreadConversations": 0,
                "unlinkedRepliedUnread": 0,
                "unlinkedRepliedRead": 0,
                "unlinkedRepliedReadUnknown": 1,
            }
        ),
    )
    monkeypatch.setattr(mod, "Camoufox", _FakeCtx)
    mod.run()
    out = capsys.readouterr().out
    assert "其中有未读标记 0 / 无未读标记 0 / 读态未知 1" in out, out
    assert "▸ [读态未知] 丙" in out, out
    assert "无未读标记] 丙" not in out, out


# --- 水位线读数（#75）：先落第一条 red——followup_report.json 里根本没有这两个键 ---


def _fake_platform(convs, my_user_id=None):
    return lambda: SimpleNamespace(
        followup=lambda page, applied: {
            "generatedAt": "2026-10-21 09:54:58",
            "myUserId": my_user_id,
            "totalConversations": len(convs),
            "hrReplied": sum(1 for c in convs if c.get("status") == "hr_replied"),
            "awaiting": sum(1 for c in convs if c.get("status") == "awaiting"),
            "replyRate": 1.0,
            "conversations": convs,
            "linkedToApplied": 0,
            "linkedReplied": 0,
            "unlinkedConversations": len(convs),
            "unlinkedReplied": sum(1 for c in convs if c.get("status") == "hr_replied"),
            "unreadConversations": 0,
            "unlinkedRepliedUnread": 0,
            "unlinkedRepliedRead": 0,
            "unlinkedRepliedReadUnknown": len(convs),
        }
    )


# --- 水位线接线（#75）：followup 是只读消费者——读它、报它，一个字都不写 ---

MY_UID = 780045835
HR_UID = 587222408
PREVIOUS_READ = "2026-10-19 09:00:00"


def _wm_conv(eid, ts=1792000000000, **over):
    """一条入站会话的真实形状（键名按 `parse_friend_entries` 的产出）。"""
    conv = {
        "company": f"公司{eid}",
        "name": "HR",
        "title": "招聘者",
        "summary": "方便聊一下吗",
        "appliedJob": None,
        "status": "hr_replied",
        "encryptJobId": eid,
        "lastTS": ts,
        "lastFromId": HR_UID,
    }
    conv.update(over)
    return conv


def _watermark_file(tmp_path, prefix=""):
    """基准文件在 #28 扁平命名下的位置：前缀加在**文件名**上（`data/ai.inbound_watermark.json`），
    不是目录上——替身也必须按这个形状走，否则「按 profile 隔离路径」这一维是空过的（条目 H②）。"""
    return tmp_path / "data" / f"{prefix}inbound_watermark.json"


def _seed_watermark(tmp_path, convs, profile="default", prefix="", at=PREVIOUS_READ):
    """用真实写入口落一份「上次读数」（`--mark-read` 走的就是它），不手搓形态。"""
    path = _watermark_file(tmp_path, prefix)
    path.parent.mkdir(parents=True, exist_ok=True)
    res = wm.advance_watermark(path, convs, wm.empty_base(profile), profile=profile, snapshot_at=at)
    assert res["written"], res
    return path


def _patch_followup_run(monkeypatch, tmp_path, convs, profile=("default", "")):
    name, prefix = profile
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / f"{prefix}applied_history.json").write_text(json.dumps({"applied": []}), encoding="utf-8")
    monkeypatch.setattr(mod, "resolve_active_profile", lambda **k: profile)

    def fake_private_path(relative, prefix=""):
        # 与 `_profile_paths.private_path` 同形：前缀落在文件名段，目录结构原样保留
        p = Path(relative)
        return tmp_path / p.parent / f"{prefix}{p.name}"

    monkeypatch.setattr(mod, "private_path", fake_private_path)
    monkeypatch.setattr(mod, "get_platform", _fake_platform(convs, my_user_id=MY_UID))
    monkeypatch.setattr(mod, "Camoufox", _FakeCtx)


def _report_json(tmp_path, prefix=""):
    return json.loads((tmp_path / "data" / f"{prefix}followup_report.json").read_text(encoding="utf-8"))


def test_run_writes_watermark_readings_with_no_baseline(tmp_path, monkeypatch, capsys):
    """#75 验收④（首跑形态）：`newSinceWatermark` / `watermark` 恒在写进 followup_report.json，
    且无基准时 N 是「无历史基准」（null），不是「全部条数」。"""
    _patch_followup_run(monkeypatch, tmp_path, [_wm_conv("e1")])
    mod.run()
    data = _report_json(tmp_path)
    assert data["newSinceWatermark"] is None, data
    assert data["watermark"]["state"] == "first_run", data
    out = capsys.readouterr().out
    assert "无历史基准" in out and "新增 1 条" not in out, out
    assert not _watermark_file(tmp_path).exists(), "只读轮不建基准——建它是 --mark-read 的事"


def test_run_reports_conversations_above_the_baseline(tmp_path, monkeypatch, capsys):
    """验收①的读数面：较上次读数新增 N 条（水位线 <时刻>），N 与判据明细都进报告。"""
    old = _wm_conv("e1")
    _seed_watermark(tmp_path, [old])
    _patch_followup_run(monkeypatch, tmp_path, [old, _wm_conv("e2", ts=1792999999000)])
    mod.run()
    data = _report_json(tmp_path)
    assert data["newSinceWatermark"] == 1, data
    assert data["watermark"]["state"] == "ok" and data["watermark"]["keys"] == 1
    assert data["watermark"]["snapshotAt"] == PREVIOUS_READ
    assert data["watermark"]["newConversations"] == 1 and data["watermark"]["updatedConversations"] == 0
    out = capsys.readouterr().out
    assert f"较上次读数新增 1 条（水位线 {PREVIOUS_READ}（1 键）" in out, out


def test_run_never_advances_the_watermark(tmp_path, monkeypatch):
    """本脚本只读：**跑一场不推一次基准**。推了就把「用户没看过的」换成「脚本没见过的」，
    下一场会报「新增 0」而用户一条没看（#75 岔口 1）。"""
    path = _seed_watermark(tmp_path, [_wm_conv("e1")])
    before = path.read_text(encoding="utf-8")
    _patch_followup_run(monkeypatch, tmp_path, [_wm_conv("e1"), _wm_conv("e2")])
    mod.run()
    mod.run()
    assert path.read_text(encoding="utf-8") == before, "连跑两场，基准仍要停在上次读数那一刻"
    assert _report_json(tmp_path)["newSinceWatermark"] == 1, "第二场仍要报同样的 N"


def test_run_marks_only_rows_above_the_line(tmp_path, monkeypatch, capsys):
    """`[入站线索]` 那几行末尾挂「｜线上新」，只挂在线上的：老行格式不动（既有 capsys 契约）。"""
    _seed_watermark(tmp_path, [_wm_conv("e1")])
    _patch_followup_run(monkeypatch, tmp_path, [_wm_conv("e1"), _wm_conv("e2", ts=1792999999000)])
    mod.run()
    rows = [line for line in capsys.readouterr().out.splitlines() if line.strip().startswith("▸")]
    assert len(rows) == 2, rows
    assert [row.count("线上新") for row in rows] == [0, 1], rows
    assert "公司e2" in rows[1]


def test_run_counts_their_new_message_in_an_old_conversation(tmp_path, monkeypatch):
    """验收③后半（followup 面）：老会话里对方新发一条要被算进来，而**自己刚发的那条不算**。"""
    _seed_watermark(tmp_path, [_wm_conv("e1")])
    theirs = _wm_conv("e1", ts=1792999999000)
    ours = _wm_conv("e0", ts=1792999999000, lastFromId=MY_UID, status="awaiting")
    _patch_followup_run(monkeypatch, tmp_path, [theirs, ours])
    mod.run()
    data = _report_json(tmp_path)
    assert data["newSinceWatermark"] == 1, data
    assert data["watermark"]["updatedConversations"] == 1 and data["watermark"]["newConversations"] == 0


def test_run_degrades_loudly_on_a_corrupt_baseline(tmp_path, monkeypatch, capsys):
    """验收④（损坏）：N 不给、state 说清、原文件保留不覆盖，终端一句人话讲完降级。"""
    _patch_followup_run(monkeypatch, tmp_path, [_wm_conv("e1")])
    path = _watermark_file(tmp_path)
    path.write_text("{这不是 JSON", encoding="utf-8")
    mod.run()
    data = _report_json(tmp_path)
    assert data["newSinceWatermark"] is None and data["watermark"]["state"] == "corrupt", data
    assert path.read_text(encoding="utf-8") == "{这不是 JSON", "读不懂的现场是排查材料，机器不替人擦"
    out = capsys.readouterr().out
    assert "无从比较" in out and "原样保留不覆盖" in out, out


def test_run_reports_a_foreign_profile_baseline_instead_of_guessing(tmp_path, monkeypatch, capsys):
    """验收④（profile 切换）：注册表 default 换人时两个档会共用同一个文件名，这时候靠文件里的
    `profile` 字段兜住——不串档，也不拿别人的键算出一个假 N。"""
    _patch_followup_run(monkeypatch, tmp_path, [_wm_conv("e1")], profile=("blockchain", "blockchain."))
    # 同一个文件名（blockchain. 那一份）里装的是 ai 的读数：正是「default 换人」的形态
    _seed_watermark(tmp_path, [_wm_conv("e1")], profile="ai", prefix="blockchain.")
    mod.run()
    data = _report_json(tmp_path, "blockchain.")
    assert data["newSinceWatermark"] is None, data
    assert data["watermark"]["state"] == "profile_mismatch", data
    assert "不串档" in capsys.readouterr().out


def test_run_reads_only_its_own_profiles_baseline_file(tmp_path, monkeypatch, capsys):
    """条目 H②：#28「基准按 profile 隔离路径」这一维此前是**空过**的——替身把 prefix 丢了
    （`tmp_path / name`），ai 那一场读的写的都落在 default 的文件名上，用例照样绿。
    现在替身保住前缀，这条用例才真能抓住「把 ai 的基准当成本档的」：
    ① 别的档有基准 ⇒ 本档仍是「无历史基准」，绝不报「新增 0」（那是拿别人的键充数）；
    ② 本档自己的基准 ⇒ 才被读到；报告也各写各的文件。"""
    _seed_watermark(tmp_path, [_wm_conv("e1")])  # default 档的基准：e1 已看过
    _patch_followup_run(monkeypatch, tmp_path, [_wm_conv("e1")], profile=("ai", "ai."))
    mod.run()
    assert _report_json(tmp_path, "ai.")["watermark"]["state"] == "first_run", "拿别人的基准充数＝谎报"
    assert _report_json(tmp_path, "ai.")["newSinceWatermark"] is None
    assert not (tmp_path / "data" / "followup_report.json").exists(), "ai 那场的报告不许写进 default 文件"
    assert "无历史基准" in capsys.readouterr().out
    # 本档落了属于自己的基准（只认 eid 那一条会话）之后，e2 才是真新增
    _seed_watermark(tmp_path, [_wm_conv("e1")], profile="ai", prefix="ai.")
    _patch_followup_run(monkeypatch, tmp_path, [_wm_conv("e1"), _wm_conv("e2")], profile=("ai", "ai."))
    mod.run()
    data = _report_json(tmp_path, "ai.")
    assert data["watermark"]["state"] == "ok" and data["newSinceWatermark"] == 1, data
    assert _report_json(tmp_path, "ai.")["watermark"]["profile"] == "ai"


def _inbound_conv(company, **over):
    """一条「入站且在等回话」的对话（末条对方发言 + 归不到投递记录）。
    键对齐 `boss.build_rec` 落进报告的形状：`build_report` 直接索引 `status`/`appliedJob`，
    展示层还要 `company`/`title`/`summary`。`over` 覆盖任意键（`status` / `appliedJob` /
    `summary` / `unreadMsgCount` …），负对照那两支就是这么搭出来的。"""
    rec = {
        "name": "HR",
        "company": company,
        "title": "招聘者",
        "summary": "要聊一下吗",
        "time": "09:54",
        "status": "hr_replied",
        "appliedJob": None,
        "unreadMsgCount": 0,
    }
    rec.update(over)
    return rec


def _run_inbound_report(tmp_path, monkeypatch, capsys, convs):
    """把 run() 的展示层跑起来，返回终端输出。
    读数**一律由生产函数 `boss.build_report` 算**（假平台只回它那份 dict），夹具不手搓任何
    统计数——早前按 `len(convs)` 硬写读数，而每条夹具又恰好都满足 `inbound_waiting` 判据，
    于是「16 行 ≡ 16」是夹具自己造的恒等式，断言从不与真正打印出来的那句读数对照
    （#66 验收 1 要的是统计侧〔boss〕与呈报侧〔followup_tracker〕两条独立链路）。
    `generatedAt` 传固定串，别用 `time.strftime`：本仓吃过宿主时区导致的偶发失败（#74 教训）。"""
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "applied_history.json").write_text(json.dumps({"applied": []}), encoding="utf-8")
    monkeypatch.setattr(mod, "resolve_active_profile", lambda **k: ("default", ""))
    monkeypatch.setattr(mod, "private_path", lambda name, prefix="": tmp_path / name)
    report = build_report(convs, "2026-10-21 09:54:58")
    monkeypatch.setattr(
        mod,
        "get_platform",
        lambda: SimpleNamespace(followup=lambda page, applied: report),
    )
    monkeypatch.setattr(mod, "Camoufox", _FakeCtx)
    mod.run()
    return capsys.readouterr().out


def _inbound_rows(out):
    return [ln for ln in out.splitlines() if ln.startswith("  ▸ [")]


def test_run_lists_every_inbound_lead_without_cap(tmp_path, monkeypatch, capsys):
    """入站清单不设行数上限（#66 验收 1）：打出的 `▸` 行数必须等于**终端那句读数**。
    真机 2026-10-21 09:54 那场 `unlinkedReplied = 16`、清单只列 15 行，第 16 条「对方主动来撩、
    正在等我们回话」被静默丢掉，而读数报的是 16——这批就是待办清单本身，漏一条等于漏一个候选岗。
    清单上界是会话列表本身，不是拍脑袋的 15。夹具掺进两条**不该进清单**的对话（已关联 / awaiting），
    让读数与行数这对不等式有真实区分力，而不是同义反复。"""
    convs = [_inbound_conv(f"丙司{i:02d}", summary=f"聊一下岗位{i}") for i in range(1, 17)]
    convs.append(_inbound_conv("已关联公司", appliedJob="链上开发"))
    convs.append(_inbound_conv("我方待回公司", status="awaiting", summary="你好，我们看过简历了"))
    out = _run_inbound_report(tmp_path, monkeypatch, capsys, convs)
    assert "其中 16 个末条是对方发言" in out, out
    rows = _inbound_rows(out)
    assert len(rows) == 16, out
    assert any("丙司16" in ln for ln in rows), "第 16 条入站线索必须出现在清单里"
    for absent in ("已关联公司", "我方待回公司"):
        assert not any(absent in ln for ln in rows), (absent, rows)


def test_run_marks_truncated_inbound_summary_with_visible_ellipsis(tmp_path, monkeypatch, capsys):
    """截断可见化（#66 验收 1 的原文那一格）：窗口仍是 46 字、版式不动，但原文**长于窗口**时
    行尾要带 `…`，让人知道后面还有内容——静默截断会把「对方到底说了什么」读成说完了。
    短于窗口不加；`summary` 缺失照旧显示 `（无摘要）` 且也不加。"""
    long_text = "我" * 60
    convs = [
        _inbound_conv("长句公司", summary=long_text),
        _inbound_conv("短句公司", summary="方便发份简历吗"),
        # 空串而不是 None：`summary` 为 None 会在下游 `replied` 那段（本次不动、也不在本任务范围）
        # 直接 TypeError，夹具就搭不出「同一条对话走两段展示」的现场了。
        _inbound_conv("无摘要公司", summary=""),
    ]
    out = _run_inbound_report(tmp_path, monkeypatch, capsys, convs)
    rows = _inbound_rows(out)
    assert len(rows) == 3, out

    def row_of(company):
        return next(ln for ln in rows if company in ln)

    long_row = row_of("长句公司")
    assert long_row.endswith("我" * 46 + "…"), long_row
    # 只断 `endswith` 拦不住「不截断、只补标记」：60 字的全文行也以同样那 46 字收尾（同一形态
    # 在 reply_inbound 那边也被查出过），所以等式另一头要钉「确实被切掉了」。
    assert "我" * 47 not in long_row, long_row
    short_row = row_of("短句公司")
    assert short_row.endswith("方便发份简历吗") and "…" not in short_row, short_row
    empty_row = row_of("无摘要公司")
    assert empty_row.endswith("（无摘要）") and "…" not in empty_row, empty_row


def test_run_keeps_inbound_wording_for_small_batch(tmp_path, monkeypatch, capsys):
    """负对照（防把守卫改成「全列」时顺手把读数措辞也改哑）：2 条入站 ⇒ 2 行，
    `[入站线索]` 那句写法逐字不变。"""
    convs = [_inbound_conv(f"丁司{i}", summary=f"在吗{i}") for i in (1, 2)]
    out = _run_inbound_report(tmp_path, monkeypatch, capsys, convs)
    assert len(_inbound_rows(out)) == 2, out
    assert "[入站线索] 归不到投递记录 2 个，其中 2 个末条是对方发言" in out, out
    assert "其中有未读标记 0 / 无未读标记 2 / 读态未知 0" in out, out
