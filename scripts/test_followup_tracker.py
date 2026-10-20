#!/usr/bin/env python3
"""followup_tracker 标注持久化测试（#19）：回复分类/时延结构化落盘，不触发浏览器。"""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

_spec = importlib.util.spec_from_file_location(
    "followup_tracker", Path(__file__).resolve().parent / "followup_tracker.py"
)
assert _spec is not None and _spec.loader is not None, "importlib spec 加载失败（模块文件缺失？）"
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


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
            {"company": "潘女士", "encryptJobId": "eid1", "replyCategory": "interested"},
            {"company": "潘女士", "replyCategory": "no_reply"},
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
            }
        ),
    )
    monkeypatch.setattr(mod, "Camoufox", _FakeCtx)
    mod.run()
    out = capsys.readouterr().out
    assert "[入站线索] 归不到投递记录 1 个，其中 1 个末条是对方发言" in out, out
    assert "▸ 甲 | 招聘者 | 要聊一下吗" in out, out
    assert "▸ 乙" not in out, "已关联到投递记录的对话不进此清单（那是我们自己投出去的）"
