#!/usr/bin/env python3
"""gen_greetings 的行为测试：草稿晋级、growing memory、规则扫描 gate、回复率评估。"""

import argparse
import importlib.util
import json
import re
from datetime import date
from pathlib import Path


_spec = importlib.util.spec_from_file_location("gen_greetings", Path(__file__).resolve().parent / "gen_greetings.py")
assert _spec is not None and _spec.loader is not None, "importlib spec 加载失败（模块文件缺失？）"
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

import _followup_shape as fshape  # noqa: E402  #63 切片 B：签名读数与台账由生产函数造，夹具不手搓键名


# --- 批量只到草稿：promote 只收 approved ---


def _draft(eid, status="pending_review", greeting="g"):
    return {
        "encryptJobId": eid,
        "jobName": f"J{eid}",
        "brandName": f"B{eid}",
        "level": "高",
        "greeting": greeting,
        "status": status,
    }


def test_promote_only_approved():
    drafts = [_draft("a"), _draft("b", status="approved", greeting="ok"), _draft("c", status="approved", greeting="")]
    greetings, n, kept_sent = mod.promote_drafts(drafts, [])
    assert n == 1 and kept_sent == 0
    assert [g["encryptJobId"] for g in greetings] == ["b"]
    assert greetings[0]["greeting"] == "ok"
    assert greetings[0]["addressee"] == "hr"


def test_promote_replaces_same_job_keeps_order():
    existing = [{"encryptJobId": "b", "jobName": "旧", "greeting": "旧"}]
    drafts = [_draft("a", status="approved", greeting="新a"), _draft("b", status="approved", greeting="新b")]
    greetings, n, kept_sent = mod.promote_drafts(drafts, existing)
    assert n == 2 and kept_sent == 0
    assert [g["encryptJobId"] for g in greetings] == ["b", "a"]
    assert greetings[0]["greeting"] == "新b"
    assert greetings[1]["greeting"] == "新a"


def test_promote_passes_contact_fields():
    """issue #12：姓名/抬头要透传到投递队列，发送时拼个性化称呼。"""
    d = _draft("a", status="approved", greeting="ok")
    d.update({"bossName": "砚女士", "bossTitle": "HR"})
    greetings, _, _ = mod.promote_drafts([d], [])
    assert greetings[0]["bossName"] == "砚女士"
    assert greetings[0]["bossTitle"] == "HR"


def test_promote_keeps_sent_entry_over_draft():
    """#64 P2：真投出记录（source=live_apply）不得被草稿顶掉——原样保留原位，计入 kept_sent；
    promoted 只数真正写入/替换掉的条数，没晋级的不算「晋级」。"""
    sent = {"encryptJobId": "b", "greeting": "实发稿", "date": "2026-10-10T09:00:00", "source": "live_apply"}
    drafts = [
        _draft("a", status="approved", greeting="新岗"),
        _draft("b", status="approved", greeting="从未发出的旧稿"),
    ]
    greetings, promoted, kept_sent = mod.promote_drafts(drafts, [sent])
    assert kept_sent == 1 and promoted == 1
    assert [g["encryptJobId"] for g in greetings] == ["b", "a"]
    assert greetings[0] == sent, "已投出条目原样保留，不被草稿替换"
    assert greetings[1]["greeting"] == "新岗", "新 eid 照常追加"


def test_promote_replacing_entry_carries_date_and_source():
    """#64 P2：替换 legacy/未打标条目时把旧条目的 date/source 带过去——
    草稿晋级不改变「有没有发出去过」这件事；值为 None 的不补键。"""
    old = {"encryptJobId": "b", "greeting": "旧稿", "date": None, "source": "legacy_draft_only"}
    drafts = [_draft("b", status="approved", greeting="新稿")]
    greetings, promoted, kept_sent = mod.promote_drafts(drafts, [old])
    assert promoted == 1 and kept_sent == 0
    e = greetings[0]
    assert e["greeting"] == "新稿" and e["source"] == "legacy_draft_only"
    assert "date" not in e, "旧 date=None 不补键（补日期＝伪造 EDD 输入）"


def test_promote_duplicate_eid_drafts_counts_and_appends_once():
    """#64 P2 收尾：同 eid 的两份 approved 草稿只落 1 条——晋级数按 eid 实数，
    既不虚报「晋级 2 条」，也不把同一条文案在台账里 append 两遍（笛卡尔式重复计数）。"""
    drafts = [_draft("a", status="approved", greeting="首稿"), _draft("a", status="approved", greeting="改稿")]
    greetings, promoted, kept_sent = mod.promote_drafts(drafts, [])
    assert promoted == 1 and kept_sent == 0
    assert [g["encryptJobId"] for g in greetings] == ["a"], "同 eid 草稿不重复追加"
    assert greetings[0]["greeting"] == "改稿", "by_eid 后到为准（原语义不变）"


def test_cmd_promote_reports_kept_sent(tmp_path, capsys, monkeypatch):
    """#64 P2 人读层：有已投出条目被草稿保住时 [done] 后补一行说明；kept_sent 为零不出现。"""
    monkeypatch.setattr(mod, "load_kb_context", lambda **k: (None, None))
    greetings_p = tmp_path / "greetings.json"
    drafts_p = tmp_path / "drafts.json"
    paths = {
        "kb": tmp_path / "kb.md",
        "fact_cards": tmp_path / "cards.md",
        "drafts": drafts_p,
        "greetings": greetings_p,
    }
    sent = [{"encryptJobId": "b", "greeting": "实发稿", "date": "t0", "source": "live_apply"}]
    greetings_p.write_text(json.dumps(sent, ensure_ascii=False), encoding="utf-8")
    drafts_p.write_text(
        json.dumps([_draft("b", status="approved", greeting="旧稿")], ensure_ascii=False), encoding="utf-8"
    )
    mod._cmd_promote(argparse.Namespace(drafts=None, yes=True), paths, None)
    out = capsys.readouterr().out
    assert "[done] 晋级 0 条" in out
    assert "另有 1 条已投出的招呼语未被草稿覆盖" in out
    assert json.loads(greetings_p.read_text(encoding="utf-8")) == sent

    legacy = [{"encryptJobId": "b", "greeting": "旧稿", "source": "legacy_draft_only"}]
    greetings_p.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    mod._cmd_promote(argparse.Namespace(drafts=None, yes=True), paths, None)
    out = capsys.readouterr().out
    assert "[done] 晋级 1 条" in out
    assert "[promote]" not in out, "kept_sent 为零时整行不出现"


# --- growing memory ---


def test_match_brand_substring_both_ways():
    assert mod.match_brand("槿女士上海珀澜科技HR", "上海珀澜科技")
    assert mod.match_brand("珀淞区块链", "北京珀淞区块链有限公司")
    assert not mod.match_brand("A公司", "B公司")
    assert not mod.match_brand("短", "短")  # 长度守卫


def test_build_memory_replied_detection():
    followup = {
        "totalConversations": 3,
        "hrReplied": 1,
        "conversations": [
            {"company": "槿女士上海珀澜科技HR", "summary": "[送达] 7年Solidity", "status": "awaiting"},
            {"company": "上海珀澜科技有限公司HR", "summary": "好的，聊聊", "status": "hr_replied"},
            {"company": "珀淞区块链HR", "summary": "方便发简历吗", "status": "hr_replied"},
        ],
    }
    history = {"applied": [{"brandName": "上海珀澜科技"}, {"brandName": "珀淞区块链"}]}
    mem = mod.build_memory(followup, history, now=None)
    assert mem["global"]["conversations"] == 3
    brand = mem["brands"]["上海珀澜科技"]
    assert brand == {"conversations": 2, "replied": 1}
    assert mem["brands"]["珀淞区块链"]["replied"] == 1


def test_merge_memory_keeps_old_brands():
    old = {"updated_at": "d1", "global": {"conversations": 1}, "brands": {"老公司": {"conversations": 1, "replied": 0}}}
    new = mod.build_memory(
        {"totalConversations": 1, "hrReplied": 0, "conversations": [{"company": "新公司", "summary": "[送达] x"}]},
        {"applied": []},
    )
    merged = mod.merge_memory(old, new)
    assert "老公司" in merged["brands"] and "新公司" in merged["brands"]
    assert merged["global"]["conversations"] == 1


def test_load_kb_context_appends_industry_layer(tmp_path):
    """#9 架构改造：个人层 + 行业层按 profile 拼装；行业文件缺失静默降级。"""
    kb = tmp_path / "personal.md"
    kb.write_text("个人事实", encoding="utf-8")
    cards = tmp_path / "cards.md"
    cards.write_text('```json\n{"years": [7], "entities": []}\n```', encoding="utf-8")
    profs = tmp_path / "profiles"
    profs.mkdir()
    (profs / "blockchain.json").write_text('{"industry": "blockchain"}', encoding="utf-8")
    inds = tmp_path / "industries"
    inds.mkdir()
    (inds / "blockchain.md").write_text("Show Me era 行业语境", encoding="utf-8")
    text, _ = mod.load_kb_context(
        kb_path=kb, cards_path=cards, profile="blockchain", profiles_dir=profs, industries_dir=inds
    )
    assert "个人事实" in text and "Show Me era 行业语境" in text
    # 行业文件缺失 → 只有个人层
    (profs / "ai.json").write_text('{"industry": "ai"}', encoding="utf-8")
    text_ai, _ = mod.load_kb_context(
        kb_path=kb, cards_path=cards, profile="ai", profiles_dir=profs, industries_dir=inds
    )
    assert "个人事实" in text_ai and "Show Me era" not in text_ai


def test_promote_gate_blocks_and_allows():
    """#9 诉求③自动化：promote 是投递出口，白名单缺口在此默认拦截、--yes 放行、无缺口直通。"""
    assert mod.promote_gate([]) is True
    assert mod.promote_gate(["未佐证实体: newrepo"]) is False
    assert mod.promote_gate(["未佐证实体: newrepo"], yes=True) is True


def test_kb_whitelist_gaps_reports_unsynced_kb_content():
    """doctor：KB 新增 repo/年限没同步白名单时要报出来。"""
    wl = {"years": {7.0}, "entities": {"aaveapy"}}
    gaps = mod.kb_whitelist_gaps("做了 9年 开发，旗舰项目 newrepo 已上线", wl)
    assert any("9" in g and "年限" in g for g in gaps)
    assert any("newrepo" in g for g in gaps)
    assert mod.kb_whitelist_gaps("7年，aaveapy 在跑", wl) == []


def test_ai_fingerprint_scan_flags_ai_closing():
    """AI 客套结尾是典型一眼假信号，进痕迹词表确定性扫描。"""
    assert mod.ai_fingerprint_scan("项目都做完了，期待有机会多聊聊！") == ["期待有机会"]
    assert mod.ai_fingerprint_scan("简历发您了，期待您的回复。") == ["期待您的回复"]


# --- AI 痕迹规避（issue #7 启示 7） ---


def test_ai_fingerprint_scan_detects_banned():
    text = "您好，我非常希望能加入贵公司，本人具备精通Rust的能力"
    hits = mod.ai_fingerprint_scan(text)
    assert "贵公司" in hits and "本人具备" in hits and "精通" in hits
    assert any("非常希望" in h for h in hits)


def test_ai_fingerprint_scan_clean_text():
    assert mod.ai_fingerprint_scan("7年Solidity,交付过主网合约,熟悉AMM机制") == []


def test_build_memory_aggregates_intent_sub():
    """#21：interested 对话的 intent_sub 聚合进品牌 intent 列表（同品牌再起草定向引用）。"""
    followup = {
        "totalConversations": 2,
        "hrReplied": 2,
        "conversations": [
            {"company": "甲公司", "summary": "发个简历看看", "status": "hr_replied", "appliedJob": "DeFi工程师"},
            {"company": "乙公司", "summary": "[送达]", "status": "awaiting", "appliedJob": "链上开发"},
        ],
    }
    anns = [{"company": "甲公司", "replyCategory": "interested", "intent_sub": "resume", "appliedJob": "DeFi工程师"}]
    mem = mod.build_memory(followup, {"applied": []}, annotations=anns)
    assert mem["brands"]["甲公司"]["intent"] == [{"job": "DeFi工程师", "sub": "resume"}]
    assert "intent" not in mem["brands"]["乙公司"], "未回复对话不聚合 intent"


def test_merge_memory_keeps_and_dedupes_intent_history():
    """同品牌新报告覆盖时不抹掉旧意向；job+sub 重复不堆积。"""
    old = {
        "updated_at": "old",
        "global": {"conversations": 5, "hr_replied": 2},
        "brands": {"甲公司": {"conversations": 2, "replied": 1, "intent": [{"job": "旧岗", "sub": "interview"}]}},
    }
    new = {
        "updated_at": "new",
        "global": {"conversations": 1, "hr_replied": 1},
        "brands": {
            "甲公司": {
                "conversations": 1,
                "replied": 1,
                "intent": [{"job": "旧岗", "sub": "interview"}, {"job": "新岗", "sub": "salary"}],
            }
        },
    }
    merged = mod.merge_memory(old, new)
    intent = merged["brands"]["甲公司"]["intent"]
    assert {"job": "旧岗", "sub": "interview"} in intent
    assert {"job": "新岗", "sub": "salary"} in intent
    assert len(intent) == 2, "重复 job+sub 去重"


def test_build_memory_empty_summary_counts_unreplied():
    mem = mod.build_memory(
        {"totalConversations": 1, "hrReplied": 0, "conversations": [{"company": "某公司", "summary": ""}]},
        {"applied": [{"brandName": "某公司"}]},
    )
    assert mem["brands"]["某公司"]["replied"] == 0


def test_learn_handles_dict_shaped_annotations_store(tmp_path, monkeypatch):
    """learn 读标注库：upsert_annotations 落盘是 {"annotations": […]} dict，
    整包传进 build_memory 会迭代出字符串 key 崩溃（2026-09-12 真实数据踩中）。"""
    import _profile_paths as pp  # 与 gen_greetings 共享同一模块实例（sys.modules）

    repo = tmp_path / "repo"
    (repo / "private" / "data").mkdir(parents=True)
    (repo / "private" / "data" / "followup_report.json").write_text(
        json.dumps(
            {
                "totalConversations": 1,
                "hrReplied": 1,
                "conversations": [{"company": "某公司", "summary": "发个简历", "status": "hr_replied"}],
            }
        ),
        encoding="utf-8",
    )
    (repo / "private" / "data" / "applied_history.json").write_text(
        json.dumps({"applied": [{"brandName": "某公司"}]}), encoding="utf-8"
    )
    (repo / "private" / "data" / "reply_annotations.json").write_text(
        json.dumps({"updated_at": "t", "annotations": [{"company": "某公司", "replyCategory": "interested"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(pp, "ROOT", repo)
    monkeypatch.setattr("sys.argv", ["gen_greetings.py", "learn"])
    mod.main()
    mem = json.loads((repo / "private" / "data" / "greeting_memory.json").read_text(encoding="utf-8"))
    assert mem["brands"]["某公司"]["replied"] == 1


# --- 规则扫描 gate（#18 M6②）：卡片白名单 + 年限/实体确定性比对 ---

WL = {"years": {7}, "entities": {"solidity", "aave", "v3", "eip712", "erc1155", "chainlink", "vrf", "polygon", "uops"}}

CARDS_FIXTURE = """# 事实卡片

## 卡片：demo（源 §1）
- 事实：21 链 Aave 收益对比

## 白名单（规则扫描 gate 机器读取区）

```json
{"years": [7], "entities": ["Aave", "Chainlink VRF", "EIP712"]}
```
"""


def test_extract_whitelist_parses_machine_block():
    wl = mod.extract_whitelist(CARDS_FIXTURE)
    assert wl["years"] == {7}
    assert {"aave", "chainlink", "vrf", "eip712"} <= wl["entities"], "多词条目拆成单词入白名单"


def test_extract_whitelist_without_block_returns_none():
    assert mod.extract_whitelist("# 只有卡片没有白名单区") is None
    assert mod.extract_whitelist("```json\n{not json}\n```") is None


def test_load_whitelist_file(tmp_path):
    missing = tmp_path / "nope.md"
    assert mod.load_whitelist_file(missing) is None, "卡片缺失→扫描跳过而不是崩溃"
    good = tmp_path / "cards.md"
    good.write_text(CARDS_FIXTURE, encoding="utf-8")
    assert mod.load_whitelist_file(good)["years"] == {7}


def test_rule_scan_flags_fabricated_years():
    hits = mod.rule_scan("3年Solidity经验，做过Aave", WL)
    assert any("3年" in h for h in hits), "白名单外的年限必须命中"
    assert not any("Solidity" in h or "Aave" in h for h in hits), "白名单内实体不误报"


def test_rule_scan_allows_whitelisted_claims():
    assert mod.rule_scan("7年Solidity，Aave V3 收益对比，EIP712 元交易", WL) == []


def test_rule_scan_flags_unknown_entity():
    hits = mod.rule_scan("7年经验，Solana 生态开发", WL)
    assert any("Solana" in h for h in hits), "知识库没有的项目/协议名必须命中"


def test_rule_scan_flags_unsubstantiated_erc_number():
    hits = mod.rule_scan("7年Solidity，做过 ERC-4626 金库", WL)
    assert any("4626" in h for h in hits), "KB 明确缺失的 ERC 编号不能溜过 gate"


def test_extract_whitelist_bad_years_returns_none():
    assert mod.extract_whitelist('```json\n{"years": ["abc"], "entities": []}\n```') is None


def test_rule_scan_flags_decimal_years():
    hits = mod.rule_scan("3.5年Solidity经验", WL)
    assert any("3.5年" in h for h in hits), "小数年限是编造的常见形态，不能漏检"


def test_rule_scan_ignores_calendar_years():
    assert mod.rule_scan("2022年部署主网，2026年还在跑", WL) == [], "四位日历年不是年限主张"


def test_rule_scan_flags_chinese_numeral_years():
    hits = mod.rule_scan("三年Solidity开发经验", WL)
    assert hits, "中文数字年限同样要拦"


def test_rule_scan_allows_hyphen_variant_of_whitelisted_repo():
    """#9 E2E 实测误报：repo 名带版本后缀（marshmallow-v1）或连字符组合
    （dfpunk-aztec）被整体当未知实体。分段后每段都在白名单→放行。"""
    wl = {"years": {7}, "entities": {"marshmallow", "dfpunk", "aztec", "noir"}}
    assert mod.rule_scan("marshmallow-v1 主网部署", wl) == []
    assert mod.rule_scan("dfpunk-aztec Noir 迁移", wl) == []


def test_rule_scan_flags_mixed_known_unknown_segments():
    """分段匹配只放行'每段都可佐证'的组合：非版本未知段仍要命中。"""
    wl = {"years": {7}, "entities": {"marshmallow"}}
    hits = mod.rule_scan("marshmallow-turbo 主网部署", wl)
    assert hits, "未知段 turbo 不能借已知前缀溜过"


def test_rule_scan_allows_version_only_combo():
    """doctor 实测：V3+V4 这类纯版本号组合是标注不是人设主张，不命中。"""
    wl = {"years": {7.0}, "entities": {"aave", "sdk"}}
    assert mod.rule_scan("Aave V3+V4 双SDK", wl) == []
    assert mod.rule_scan("Aave V3+newrepo", wl)  # 混入未知实体仍命中


def test_rule_scan_jd_is_input_not_claim():
    """#9 E2E 实测误报：'JD' 是输入侧词（匹配 JD 的某某要求），永不构成候选人人设主张。"""
    assert mod.rule_scan("匹配 JD 的区块链系统设计需求", WL) == []


def test_rule_scan_allows_digit_prefixed_repo():
    """#9 E2E 第二轮误报：repo 名以数字开头（10kpfp）时，字母开头正则只提到 'kpfp'
    误报。数字开头的 token 要整体提取，与白名单整体比对。"""
    wl = {"years": {7}, "entities": {"10kpfp"}}
    assert mod.rule_scan("10kpfp 审计合约交付", wl) == []
    assert mod.rule_scan("10kpfp审计PDF", wl) == []


def test_rule_scan_allows_common_tech_tokens():
    """#9 E2E 第二轮误报：白名单实体的描述组成词/通用格式词不构成人设主张。"""
    assert mod.rule_scan("审计 PDF 与 VRF reveal 流程", WL) == []
    assert mod.rule_scan("MetaTx 免 Gas 架构", WL) == []
    assert mod.rule_scan("Aave V4 Hub-Spoke 研究", WL) == []
    assert mod.rule_scan("CI workflows 全绿", WL) == []


def test_rule_scan_allows_standalone_numbers_and_versions():
    """#9 E2E 第二轮：数字开头正则后，'21链'/'193测试' 等纯数字 token 是标注不是主张。"""
    assert mod.rule_scan("21链收益对比，193测试文件", WL) == []


def test_internal_marker_scan_flags_leaked_markers():
    """#9 E2E 第二轮：8b drafter 仍把 §编号/白名单/JD 写进正文，reviewer 拦不住——
    确定性扫描兜底，正文是发给 HR 的对话，内部标记绝不能出现。"""
    hits = mod.internal_marker_scan("主网部署（§2），见白名单 years 字段，匹配JD要求")
    assert set(hits) == {"§编号引用", "白名单字样", "JD字样"}
    assert mod.internal_marker_scan("7年Solidity，主网部署过，方便聊聊？") == []


def test_internal_marker_scan_flags_card_number_variant():
    """#9 E2E 第四轮：few-shot 反例压下 § 后，模型改用「卡片2/条目3」变体躲避——
    扫描器要覆盖同族编号引用，不陪模型打地鼠。"""
    hits = mod.internal_marker_scan("主网部署过ERC1155平台（卡片2），见条目3")
    assert set(hits) == {"卡片/条目编号引用"}
    assert mod.internal_marker_scan("我们聊聊卡片式的产品设计") == [], "普通词义不误报"


# --- 运行时状态词扫描（#34：KB 只有部署快照，写「运行至今」是脑补出来的现状断言） ---


def test_runtime_state_scan_flags_continuity_words():
    """#34 真实事故：2022 年部署快照被写成「生产运行至今」并发给 HR。"""
    hits = mod.runtime_state_scan("主网生产运行至今，合约已验证。")
    assert "运行至今" in hits
    for word in ("稳定运行至今", "持续运行", "在线运行", "至今在跑", "至今活跃", "仍在跑", "运行中", "至今仍在运行"):
        assert mod.runtime_state_scan(f"项目{word}。"), f"{word} 应命中（可由覆盖词命中）"


def test_runtime_state_scan_allows_snapshot_past_tense():
    """快照式过去时是本票要求的正确写法（部署过 + 验证过），无需佐证不拦截。"""
    assert mod.runtime_state_scan("2022 年在主网部署并已验证，曾稳定运行过。") == []
    assert mod.runtime_state_scan("写过 Solidity 合约，做过审计。") == []


def test_runtime_state_scan_recent_evidence_passes():
    """有条件拦截：state_evidence 记了近期实测（≤7 天）才放行现状断言。"""
    today = date(2026, 9, 14)
    ev = "链上 polygonscan 2026-09-14 调用确认，合约仍被调用"
    assert mod.runtime_state_scan("主网合约仍在跑。", evidence=ev, today=today) == []


def test_runtime_state_scan_missing_or_stale_evidence_blocked():
    """fail-closed：无佐证 / 无日期 / 过期 / 未来日期一律拦截（未来时间不是实测）。"""
    today = date(2026, 9, 14)
    for ev in (
        None,
        "",
        "   ",
        123,
        ["2026-09-14"],
        "链上确认过（没写日期）",
        "2026-01-01 链上确认",
        "2026-09-15 未来日期",
    ):
        assert mod.runtime_state_scan("生产运行至今。", evidence=ev, today=today) == ["运行至今"], ev


def test_runtime_state_scan_evidence_age_boundary():
    """7 天边界：当天到第 7 天算近期，第 8 天起算陈旧。"""
    today = date(2026, 9, 14)
    assert mod.runtime_state_scan("持续运行", evidence="链上 2026-09-07 确认", today=today) == []
    assert mod.runtime_state_scan("持续运行", evidence="链上 2026-09-06 确认", today=today) == ["持续运行"]


def test_runtime_state_scan_flags_continuity_adverb_phrasings():
    """#34 方向④：状态断言不止「运行至今」一词——延续词 + 仍/还/持续/稳定 也要命中；
    但「我目前在寻找新机会」是正常表述，不误伤（review 采纳项）。"""
    assert mod.runtime_state_scan("合约目前仍在服务线上业务。")
    assert mod.runtime_state_scan("系统至今仍在使用。")
    assert mod.runtime_state_scan("线上一直在跑。")
    assert mod.runtime_state_scan("我目前在寻找新的机会。") == []


def test_runtime_state_scan_accepts_chinese_date_evidence():
    """佐证日期两种写法都认（ISO 与中文年月日）——中文日期被判无佐证会让门显得无故拦截。"""
    today = date(2026, 9, 14)
    assert mod.runtime_state_scan("持续运行", evidence="链上复核 2026 年 9 月 14 日 通过", today=today) == []
    assert mod.runtime_state_scan("持续运行", evidence="链上复核 2026/9/14", today=today) == []


def test_runtime_state_scan_clean_text_skips_evidence_check():
    """无状态词时不做任何佐证要求——过去时/无状态断言草稿零摩擦。"""
    assert mod.runtime_state_scan("7 年 Solidity 经验，主网部署过 ERC1155。", evidence=None) == []


def test_load_kb_context_prepends_cards(tmp_path):
    kb = tmp_path / "kb.md"
    kb.write_text("# 知识库原文\n§1", encoding="utf-8")
    cards = tmp_path / "cards.md"
    cards.write_text(CARDS_FIXTURE, encoding="utf-8")
    no_ind = {"profiles_dir": tmp_path / "none", "industries_dir": tmp_path / "none"}  # 隔离真实行业层
    text, wl = mod.load_kb_context(kb_path=kb, cards_path=cards, **no_ind)
    assert text.index("事实卡片") < text.index("知识库原文"), "卡片常驻：拼在原文前"
    assert wl["years"] == {7}
    no_cards_text, no_wl = mod.load_kb_context(kb_path=kb, cards_path=tmp_path / "none.md", **no_ind)
    assert no_cards_text == kb.read_text(encoding="utf-8") and no_wl is None


# --- #19 Loop Evaluation：模式标签与长度分桶 ---


def test_pattern_tags_question_cta():
    assert "question_cta" in mod.pattern_tags("我有7年经验，请问贵司技术栈是什么？")


def test_pattern_tags_link_availability_no_year_opener():
    tags = mod.pattern_tags("我有3年Rust经验，做过链上撮合，详见 https://github.com/x，可随时到岗")
    assert "has_link" in tags and "availability" in tags
    assert "question_cta" not in tags


def test_pattern_tags_plain_text_no_tags():
    assert mod.pattern_tags("我交付过NFT市场合约和钱包基础设施，主网均已上线") == []


def test_pattern_tags_year_opener_not_tagged():
    # #19 前置 B 拍板（2026-09-10）：#13 文案标准强制年限开头，year_opener 零方差，
    # 不再作为结构变体标签参与分组。
    assert mod.pattern_tags("我有7年Solidity经验，交付过NFT市场合约") == []


def test_length_bucket_boundaries():
    assert mod.length_bucket("a" * 100) == "≤100"
    assert mod.length_bucket("a" * 150) == "101-150"
    assert mod.length_bucket("a" * 151) == ">150"


# --- #19：对话 ↔ 招呼语关联 ---


def _eval_report():
    return {
        "conversations": [
            {
                "company": "上海珀澜科技HR",
                "summary": "你好，方便聊聊",
                "status": "hr_replied",
                "appliedJob": "DeFi工程师",
                "replyCategory": "interested",
                "replyLatencyHours": 6.5,
            },
            {"company": "北京中利博冠", "summary": "[送达]", "status": "awaiting", "appliedJob": "链上开发"},
        ]
    }


def _eval_greetings():
    return [
        {
            "encryptJobId": "e1",
            "jobName": "DeFi工程师",
            "brandName": "珀澜科技",
            "level": "高",
            "addressee": "hr",
            "greeting": "我有7年Solidity经验，交付过NFT市场合约",
        },
        {
            "encryptJobId": "e2",
            "jobName": "链上开发",
            "brandName": "中利博冠",
            "level": "初",
            "addressee": "hr",
            "greeting": "我有5年Rust经验，做过撮合引擎，请问贵司用的什么链？",
        },
    ]


def test_join_eval_records_links_conv_to_greeting():
    records = mod.join_eval_records(_eval_report(), _eval_greetings(), [])
    assert len(records) == 2
    by_brand = {r["brandName"]: r for r in records}
    assert by_brand["珀澜科技"]["replied"] is True
    assert by_brand["珀澜科技"]["level"] == "高"
    assert by_brand["珀澜科技"]["latency"] == 6.5
    assert by_brand["珀澜科技"]["addressee"] == "hr"
    assert by_brand["珀澜科技"]["lengthBucket"] == "≤100"
    assert by_brand["中利博冠"]["replied"] is False
    assert "question_cta" in by_brand["中利博冠"]["tags"]


def test_join_eval_records_unmatched_conv_dropped():
    report = _eval_report()
    report["conversations"].append({"company": "无匹配公司", "summary": "x", "status": "hr_replied", "appliedJob": "y"})
    records = mod.join_eval_records(report, _eval_greetings(), [])
    assert all(r["brandName"] != "无匹配公司" for r in records)


def test_join_eval_records_brand_fallback_1to1_consumption():
    """match_brand 兜底必须 1:1 消耗（2026-09-12 审查）：一个招呼语配多条同品牌对话
    会造成样本数膨胀/笛卡尔积，已配对的对话不再参与后续兜底。"""
    report = {
        "conversations": [
            {"company": "甲子光年公司HR砚女士", "summary": "聊聊？", "status": "hr_replied"},
            {"company": "甲子光年公司HR桐先生", "summary": "[送达]", "status": "awaiting"},
        ]
    }
    greetings = [
        {"jobName": "岗A", "brandName": "甲子光年公司", "level": "高", "greeting": "我有7年经验，请问技术栈？"}
    ]  # 无 encryptJobId → 品牌兜底
    records = mod.join_eval_records(report, greetings, [])
    assert len(records) == 1, "1 个招呼语只配 1 条对话，不笛卡尔积"

    greetings2 = [
        {"jobName": "岗A", "brandName": "甲子光年公司", "level": "高", "greeting": "稿一"},
        {"jobName": "岗B", "brandName": "甲子光年公司", "level": "高", "greeting": "稿二"},
    ]
    records2 = mod.join_eval_records(report, greetings2, [])
    assert len(records2) == 2, "2 招呼语 × 2 对话 = 2 条 1:1 配对，而非笛卡尔积 4 条"
    assert len({r["greeting"] for r in records2}) == 2, "每条招呼语只被消耗一次"


def test_join_eval_records_replied_single_source_status():
    """replied 单一真源 = conv.status == "hr_replied"（2026-09-12 审查；#30 修复后
    [已读] 归 awaiting，summary 是消息正文不再承担状态语义）。"""
    report = {
        "conversations": [
            {"company": "甲子光年公司", "summary": "正常的最后一条消息正文", "status": "awaiting"},
        ]
    }
    greetings = [{"jobName": "岗A", "brandName": "甲子光年公司", "level": "高", "greeting": "稿"}]
    records = mod.join_eval_records(report, greetings, [])
    assert records[0]["replied"] is False, "status=awaiting 时正文再像回复也不算已回复"


# --- #19：eval 分组统计与样本门槛 ---


def test_eval_insufficient_sample_no_conclusions():
    report = _eval_report()
    out = mod.eval_greetings(report, _eval_greetings(), [], now="2026-09-09T20:00:00")
    assert out["sufficient"] is False, "总量<30 不得出结论"
    for dim in ("by_level", "by_addressee", "by_pattern"):
        for g in out["groups"][dim].values():
            assert g["replyRate"] is None and "样本不足" in g["conclusion"]
    assert out["candidates"]["winning"] == [] and out["candidates"]["losing"] == []


# --- #64 追加验收 7：legacy_* 历史遗留条目不参与按文案分组的回复率统计 ---


def _legacy_eval_fixtures():
    """eid 精确配对形态（#30 后 wapi 常态）：e1/e2 是历史遗留条目（正文与实发可能不一致），
    e3 是真投出条目。e1 的对话恰好 hr_replied——若不排除会把回复算到旧稿头上。"""
    report = {
        "conversations": [
            {"encryptJobId": "e1", "company": "甲公司", "status": "hr_replied", "appliedJob": "工程师"},
            {"encryptJobId": "e2", "company": "乙公司", "status": "awaiting", "appliedJob": "工程师"},
            {"encryptJobId": "e3", "company": "丙公司", "status": "hr_replied", "appliedJob": "工程师"},
        ]
    }
    greetings = [
        {
            "encryptJobId": "e1",
            "brandName": "甲公司",
            "level": "高",
            "addressee": "hr",
            "greeting": "历史稿1",
            "source": "legacy_undated",
        },
        {
            "encryptJobId": "e2",
            "brandName": "乙公司",
            "level": "高",
            "addressee": "hr",
            "greeting": "历史稿2",
            "source": "legacy_draft_only",
        },
        {
            "encryptJobId": "e3",
            "brandName": "丙公司",
            "level": "高",
            "addressee": "hr",
            "greeting": "实发稿",
            "source": "live_apply",
        },
    ]
    return report, greetings


def test_eval_greetings_excludes_legacy_sources():
    report, greetings = _legacy_eval_fixtures()
    out = mod.eval_greetings(report, greetings, [], now="2026-10-10T10:00:00")
    assert out["legacyExcluded"] == 2
    # 只剩 e3 参与：样本 1、回复 1（若 e1 的旧稿混进来会是 3 样本 / 2 回复）
    assert out["sampleSize"] == 1 and out["replied"] == 1 and out["overallRate"] == 1.0
    assert out["groups"]["by_addressee"]["hr"]["sample"] == 1


def test_eval_greetings_only_excludes_legacy_sources():
    """排除判据是 LEGACY_SOURCES 闭集成员：source=None / 缺 source 键的未打标条目与
    live_apply 照常参与，不按前缀通配。"""
    report, greetings = _legacy_eval_fixtures()
    greetings[0]["source"] = None
    del greetings[1]["source"]
    out = mod.eval_greetings(report, greetings, [], now="2026-10-10T10:00:00")
    assert out["legacyExcluded"] == 0
    assert out["sampleSize"] == 3 and out["replied"] == 2


def test_is_legacy_greeting_closed_set():
    """#64 S2：判据是闭集成员判定，不是前缀通配——词表外的 legacy_* 不算历史遗留。"""
    assert mod.is_legacy_greeting({"source": "legacy_undated"}) is True
    assert mod.is_legacy_greeting({"source": "legacy_draft_only"}) is True
    assert mod.is_legacy_greeting({"source": "legacy_other"}) is False
    assert mod.is_legacy_greeting({"source": "live_apply"}) is False
    assert mod.is_legacy_greeting({"source": None}) is False
    assert mod.is_legacy_greeting({}) is False


def test_eval_greetings_legacy_excluded_key_always_present():
    """legacyExcluded 键恒在：全非 legacy 台账给 0，不缺键（「新键恒在」口径）。"""
    out = mod.eval_greetings(_eval_report(), _eval_greetings(), [], now="2026-09-09T20:00:00")
    assert out["legacyExcluded"] == 0


def _bulk_fixture(total, pattern):
    """构造 total 条记录：pattern 组 replied，其余未回复，使 pattern 组回复率显著高于总体。"""
    convs, grs = [], []
    for i in range(total):
        brand = f"公司{i:03d}"
        convs.append(
            {
                "company": brand,
                "summary": "[送达]" if pattern != f"公司{i:03d}" else "你好",
                "status": "hr_replied" if pattern == brand else "awaiting",
                "appliedJob": f"岗{i:03d}",
            }
        )
        grs.append(
            {
                "encryptJobId": f"e{i}",
                "jobName": f"岗{i:03d}",
                "brandName": brand,
                "level": "高",
                "greeting": "我有7年经验" if pattern == brand else "我有7年经验，可到岗",
            }
        )
    return {"conversations": convs}, grs


def test_eval_sufficient_sample_produces_rates_and_candidates():
    # 30 条总量、组内 6 条：question_cta 组 6 条全回复，availability 组 24 条零回复；
    # 回复组走 leader 称呼、未回复组 hr——by_addressee 同步验证方差维度。
    convs, grs = [], []
    for i in range(30):
        brand = f"公司{i:03d}"
        replied = i < 6
        convs.append(
            {
                "company": brand,
                "summary": "你好" if replied else "[送达]",
                "status": "hr_replied" if replied else "awaiting",
                "appliedJob": f"岗{i:03d}",
            }
        )
        grs.append(
            {
                "encryptJobId": f"e{i}",
                "jobName": f"岗{i:03d}",
                "brandName": brand,
                "level": "高",
                "addressee": "leader" if replied else "hr",
                "greeting": "我有7年经验，请问技术栈？" if replied else "我有7年经验，可随时到岗",
            }
        )
    out = mod.eval_greetings({"conversations": convs}, grs, [], now="2026-09-09T20:00:00")
    assert out["sufficient"] is True
    assert "by_length" not in out["groups"], "by_length 已按 #19 前置 B 拍板移除"
    qp = out["groups"]["by_pattern"]["question_cta"]
    assert qp["sample"] == 6 and qp["replyRate"] == 1.0
    av = out["groups"]["by_pattern"]["availability"]
    assert av["sample"] == 24 and av["replyRate"] == 0.0
    ld = out["groups"]["by_addressee"]["leader"]
    assert ld["sample"] == 6 and ld["replyRate"] == 1.0
    hr = out["groups"]["by_addressee"]["hr"]
    assert hr["sample"] == 24 and hr["replyRate"] == 0.0
    winning_keys = [c["key"] for c in out["candidates"]["winning"]]
    losing_keys = [c["key"] for c in out["candidates"]["losing"]]
    assert "by_pattern:question_cta" in winning_keys
    assert "by_pattern:availability" in losing_keys
    assert "by_addressee:leader" in winning_keys
    assert "by_addressee:hr" in losing_keys
    assert all(c["examples"] for c in out["candidates"]["winning"]), "正例候选必须带原文样例"


def test_eval_latency_median():
    convs = [
        {"company": "测试公司A", "summary": "hi", "status": "hr_replied", "appliedJob": "J1", "replyLatencyHours": 10},
        {"company": "测试公司B", "summary": "hi", "status": "hr_replied", "appliedJob": "J2", "replyLatencyHours": 2},
        {"company": "测试公司C", "summary": "hi", "status": "hr_replied", "appliedJob": "J3", "replyLatencyHours": 8},
    ]
    grs = [
        {"encryptJobId": f"e{i}", "jobName": f"J{i + 1}", "brandName": b, "level": "高", "greeting": "我有7年经验"}
        for i, b in enumerate(["测试公司A", "测试公司B", "测试公司C"])
    ]
    out = mod.eval_greetings({"conversations": convs}, grs, [])
    assert out["latency"]["medianHours"] == 8 and out["latency"]["sample"] == 3


# --- #19：promote-winners 人工 gate 与回写 ---


def _evaluation_with_candidates():
    return {
        "generatedAt": "2026-09-09T20:00:00",
        "candidates": {
            "winning": [
                {
                    "key": "by_pattern:question_cta",
                    "sample": 6,
                    "replyRate": 1.0,
                    "examples": ["我有7年经验，请问技术栈？"],
                }
            ],
            "losing": [
                {
                    "key": "by_pattern:availability",
                    "sample": 24,
                    "replyRate": 0.0,
                    "examples": ["我有7年经验，可随时到岗"],
                }
            ],
        },
    }


def test_promote_winners_requires_yes(tmp_path, capsys):
    out_path = tmp_path / "winning-examples.md"
    res = mod.promote_winners(
        _evaluation_with_candidates(),
        ["by_pattern:question_cta"],
        out_path=out_path,
        yes=False,
        now="2026-09-09T20:00:00",
    )
    assert res is False and not out_path.exists(), "未 --yes 不得写文件（人工 gate）"
    assert "by_pattern:question_cta" in capsys.readouterr().out


def test_promote_winners_writes_sections(tmp_path):
    out_path = tmp_path / "winning-examples.md"
    assert (
        mod.promote_winners(
            _evaluation_with_candidates(),
            ["by_pattern:question_cta", "by_pattern:availability"],
            out_path=out_path,
            yes=True,
            now="2026-09-09T20:00:00",
        )
        is True
    )
    text = out_path.read_text(encoding="utf-8")
    assert "## 正例（few-shot）" in text and "## 避坑（低回复模式）" in text
    assert "我有7年经验，请问技术栈？" in text and "我有7年经验，可随时到岗" in text


def test_promote_winners_unknown_group_blocked(tmp_path):
    out_path = tmp_path / "winning-examples.md"
    assert (
        mod.promote_winners(_evaluation_with_candidates(), ["by_pattern:不存在"], out_path=out_path, yes=True) is False
    )
    assert not out_path.exists()


def test_load_winning_examples_parses_sections(tmp_path):
    p = tmp_path / "winning-examples.md"
    p.write_text(
        "# 头\n\n## 正例（few-shot）\n\n- 正例一\n- 正例二\n\n## 避坑（低回复模式）\n\n- 坑一\n", encoding="utf-8"
    )
    out = mod.load_winning_examples(p)
    assert out["winners"] == ["正例一", "正例二"] and out["avoid"] == ["坑一"]
    assert mod.load_winning_examples(tmp_path / "none.md") == {"winners": [], "avoid": []}


def test_join_prefers_exact_encrypt_job_id_over_brand():
    report = {
        "conversations": [
            {
                "company": "甲公司招聘HR",
                "summary": "你好",
                "status": "hr_replied",
                "appliedJob": "岗B",
                "encryptJobId": "e2",
            }
        ]
    }
    greetings = [
        {"encryptJobId": "e1", "jobName": "岗A", "brandName": "甲公司", "level": "高", "greeting": "我有7年经验"},
        {"encryptJobId": "e2", "jobName": "岗B", "brandName": "乙公司", "level": "高", "greeting": "我有5年经验"},
    ]
    records = mod.join_eval_records(report, greetings, [])
    assert len(records) == 1 and records[0]["encryptJobId"] == "e2", (
        "双方都有 encryptJobId 时必须精确匹配，不得品牌串岗错配"
    )


def test_annotation_for_conv_exact_job_priority():
    anns = [
        {"company": "甲", "appliedJob": "岗1", "replyCategory": "interested"},
        {"company": "甲", "replyCategory": "rejected"},
    ]
    assert mod.annotation_for_conv({"company": "甲", "appliedJob": "岗1"}, anns)["replyCategory"] == "interested"
    assert mod.annotation_for_conv({"company": "甲", "appliedJob": "岗2"}, anns)["replyCategory"] == "rejected"
    assert mod.annotation_for_conv({"company": "乙"}, anns) is None


# --- #25 白名单自动推导：cards → derive → doctor diff → --sync 入表 ---

_CARDS = """# 事实卡片库

> 来源说明行（引用 ERC-4626 也不入白名单）

## 卡片：AaveAPY 双仓（§1）

7年Solidity，aaveapy 生产运行，用 Dune 做分析。

## 卡片：职业经历早段（§14）

示例大厂 | 解决方案销售 | 2011.12-2015.02 做BSS/VAS。

```json
{"years": [7], "entities": ["Solidity", "aaveapy", "React"]}
```
"""


def _json_block(cards_text):
    m = re.search(r"```json\s*(\{.*?\})\s*```", cards_text, re.S)
    return json.loads(m.group(1))


def test_derive_card_facts_years_from_phrase_and_range():
    """「N年」短语与时间段都编译成年限候选；时间段给 span 与 span-1（2011-2015 → 4/3）。"""
    facts = mod.derive_card_facts(_CARDS)
    assert 7 in facts["years"]
    assert {3, 4} <= facts["years"], "issue #25 实例：示例大厂三年不得再拦"


def test_derive_card_facts_entities_from_card_text():
    facts = mod.derive_card_facts(_CARDS)
    assert "dune" in facts["entities"] and "bss" in facts["entities"]


def test_derive_card_facts_ignores_json_block_quotes_and_dates():
    """JSON 块/blockquote/标题不参与推导（union 由 --sync 负责）；裸年份是日期不是年限主张。"""
    facts = mod.derive_card_facts(_CARDS)
    assert facts["entities"] == {"solidity", "aaveapy", "dune", "bss", "vas"}
    assert "react" not in facts["entities"], "JSON 块里的条目不得重复计入推导"
    assert "erc-4626" not in facts["entities"], "blockquote 引用的坏实体不得入推导"
    assert facts["years"] == {3, 4, 7}


def test_derive_card_facts_excludes_negation_segments():
    cards = (
        "## 卡片\nstaketh 仅技术栈实践不作能力证明；7年经验。\n"
        "用户缺口（不装懂）：ERC-4626/4337 实现。\n"
        "Wishing-Pool（fork）不可直接主张，另有 Aztec 新经历"
    )
    facts = mod.derive_card_facts(cards)
    assert "staketh" not in facts["entities"]
    assert "erc-4626" not in facts["entities"] and "wishing-pool" not in facts["entities"]
    assert "aztec" in facts["entities"]
    assert 7 in facts["years"]


def test_derive_card_facts_strips_urls():
    cards = "## 卡片\n详见 https://github.com/x/verynewrepo，2026 活跃"
    facts = mod.derive_card_facts(cards)
    assert facts["entities"] == set() and facts["years"] == set()


def test_derive_card_facts_stopwords_not_proposed():
    # 英文功能词永不构成主张（to/of/me 等），不进 diff 噪声
    facts = mod.derive_card_facts("## 卡片\nto B 方案，Show Me era，用 Dune 做分析")
    assert "to" not in facts["entities"] and "me" not in facts["entities"]
    assert "dune" in facts["entities"]


def test_card_gaps_reports_unregistered_facts():
    cards = "## 卡片\n示例大厂经历 2011-2015；7年Solidity；用过 Dune 做分析"
    wl = {"years": {7}, "entities": {"solidity"}}
    gaps = mod.card_gaps(cards, wl)
    assert gaps["years"] == [3, 4], "span 与 span-1 都要补"
    assert gaps["entities"] == ["dune"]


def test_card_gaps_empty_when_in_sync():
    cards = "## 卡片\n7年Solidity"
    gaps = mod.card_gaps(cards, {"years": {7}, "entities": {"solidity"}})
    assert gaps["years"] == [] and gaps["entities"] == []


def test_sync_card_facts_merges_preserves_and_idempotent():
    out = mod.sync_card_facts(_CARDS)
    block = _json_block(out)
    assert block["years"] == [3, 4, 7]
    assert block["entities"] == ["Solidity", "aaveapy", "React", "bss", "dune", "vas"]
    # 幂等：二次 sync 逐字节不变
    assert mod.sync_card_facts(out) == out


def test_sync_card_facts_creates_block_when_absent():
    out = mod.sync_card_facts("## 卡片\n7年Solidity")
    block = _json_block(out)
    assert block["years"] == [7] and block["entities"] == ["solidity"]


# --- #37 EDD 持续化：维度注册表 / 趋势持久化 / 候选队列 ---


def test_eval_dimensions_registry_extensible():
    """#37 拍板 Q5：轻量维度注册表——新维度注册后 eval_greetings 自动产出分组，
    Phase 2 时段维/Phase 3 活跃度维到位时注册即可，不改函数体。"""
    mod.EVAL_DIMENSIONS.append(("by_kind", lambda r: [r["greeting"][:2] or "未知"]))
    try:
        out = mod.eval_greetings(_eval_report(), _eval_greetings(), [], now="2026-09-09T20:00:00")
        assert "by_kind" in out["groups"], "注册维度必须自动产出分组"
        assert set(out["groups"]) >= {"by_level", "by_addressee", "by_pattern", "by_kind"}
    finally:
        mod.EVAL_DIMENSIONS.pop()


def _pending(key="by_pattern:question_cta", created="2026-09-09T20:00:00", sample=6):
    return {
        "key": key,
        "kind": "winning",
        "sample": sample,
        "replied": sample,
        "replyRate": 1.0,
        "examples": ["我有7年经验，请问技术栈？"],
        "created": created,
    }


def test_pending_upsert_dedupes_by_key():
    """同 key 候选重复产出时原位覆盖：created 保留首见、last_seen 刷新为本次（review C1）——
    持续出现的候选不因首见久远被误过期，同时 last_seen 反映最近活跃。"""
    data = mod.pending_upsert(
        {"updated_at": "d0", "candidates": [_pending()]}, [_pending(sample=8)], now="2026-09-17T10:00:00"
    )
    assert len(data["candidates"]) == 1
    assert data["candidates"][0]["sample"] == 8
    assert data["candidates"][0]["created"] == "2026-09-09T20:00:00"  # 首见保留
    assert data["candidates"][0]["last_seen"] == "2026-09-17T10:00:00"  # 刷新


def test_pending_upsert_appends_new_keys():
    data = mod.pending_upsert(
        {"updated_at": "d0", "candidates": [_pending()]}, [_pending(key="by_level:高")], now="2026-09-17T10:00:00"
    )
    assert [c["key"] for c in data["candidates"]] == ["by_pattern:question_cta", "by_level:高"]


def test_pending_prune_expires_after_30d():
    """超 30 天未审阅的候选自动废弃（拍板 Q8）：过期即清，下次 eval 仍会重新入队。"""
    data = {
        "updated_at": "d0",
        "candidates": [
            _pending(created="2026-08-01T08:00:00"),  # 47 天前 → 过期
            _pending(key="by_level:高", created="2026-09-01T08:00:00"),  # 16 天前 → 保留
        ],
    }
    pruned = mod.pending_prune(data, now="2026-09-17T10:00:00")
    assert [c["key"] for c in pruned["candidates"]] == ["by_level:高"]


def test_persist_eval_output_writes_trend_and_queue(tmp_path):
    """eval 产物持久化组合：报告写盘 + 趋势快照追加 + 有候选入队。
    返回 (待审数, 快照, 趋势写入状态, append 前历史)（#59：追加结果要能传给终端措辞；R4-5：状态是
    咽喉的返回值本身，调用方不再自己重读 trend 判第二遍；#82：第 4 项是渲染方要的 append 前历史）。"""
    convs, grs = _bulk_fixture(30, "公司003")
    out = mod.eval_greetings(convs, grs, [], now="2026-09-09T20:00:00")
    eval_path = tmp_path / "greeting_evaluation.json"
    trend_path = tmp_path / "eval_trend.jsonl"
    pending_path = tmp_path / "pending_promotions.json"
    n, snap, trend_status, _history = mod.persist_eval_output(
        out,
        evaluation_path=eval_path,
        trend_path=trend_path,
        pending_path=pending_path,
        profile="p",
        now="2026-09-09T20:00:00",
    )
    assert trend_status == mod.TREND_APPENDED, "非 0 样本照常追加"
    assert eval_path.exists() and trend_path.exists()
    trend = json.loads(trend_path.read_text(encoding="utf-8").splitlines()[-1])
    assert snap["sampleSize"] == 30 and trend["sampleSize"] == 30
    assert n > 0, "有 winning/losing 候选时须入待审队列"
    queue = json.loads(pending_path.read_text(encoding="utf-8"))
    expected_keys = {c["key"] for c in out["candidates"]["winning"] + out["candidates"]["losing"]}
    assert {c["key"] for c in queue["candidates"]} == expected_keys


def test_persist_eval_output_no_candidates_touches_no_queue(tmp_path):
    """无候选（样本不足）时不建/不动队列文件；趋势快照照记（观测与充分性解耦）。"""
    out = mod.eval_greetings(_eval_report(), _eval_greetings(), [], now="2026-09-09T20:00:00")
    trend_path = tmp_path / "eval_trend.jsonl"
    pending_path = tmp_path / "pending_promotions.json"
    n, snap, trend_status, _history = mod.persist_eval_output(
        out,
        evaluation_path=tmp_path / "e.json",
        trend_path=trend_path,
        pending_path=pending_path,
        profile="p",
        now="2026-09-09T20:00:00",
    )
    assert n == 0 and not pending_path.exists()
    assert trend_status == mod.TREND_APPENDED
    assert snap["sampleSize"] == 2 and trend_path.exists()


def test_persist_eval_output_false_zero_blocked_at_chokepoint(tmp_path):
    """#59 问题 2 的入口本身：eval（含 `eval --trend`）走的 persist_eval_output 也必须在
    「0 样本 + 历史已有非 0 点」时被咽喉拦下——上一轮实现只在会话侧设防，这条入口照样把
    假零写进了 eval_trend.jsonl。返回的状态就是 `false_zero`，供终端如实措辞（R4-5 起
    措辞只读这一个状态，不再有第二个「是否假零」的并行判据）。"""
    out = mod.eval_greetings(_unpaired_report(), _eval_greetings(), [], now="2026-09-30T00:14:12")
    trend_path = tmp_path / "eval_trend.jsonl"
    _seed_trend(trend_path, 59)
    before = trend_path.read_text(encoding="utf-8")
    _, snap, trend_status, _history = mod.persist_eval_output(
        out,
        evaluation_path=tmp_path / "e.json",
        trend_path=trend_path,
        pending_path=tmp_path / "pending_promotions.json",
        profile="p",
        now="2026-09-30T00:14:12",
    )
    assert snap["sampleSize"] == 0
    assert trend_status == mod.TREND_FALSE_ZERO
    assert trend_path.read_text(encoding="utf-8") == before, "假零从 eval 入口写进了趋势历史"


def test_persist_eval_output_reports_duplicate_not_false_zero(tmp_path):
    """R4-5 的调用点侧：两条拒写原因同时成立时，状态必须是咽喉实际走的那条（`duplicate`，
    幂等检查在前）。旧形态下 `persist_eval_output` 自己 `false_zero_snapshot(load_trend(...))`
    重判一遍，报的是假零——终端措辞与咽喉实际做的不一致，正是「同一判据被读三次、两处各维护一份」
    留下的错话。"""
    out = mod.eval_greetings(_unpaired_report(), _eval_greetings(), [], now="2026-09-30T00:14:12")
    trend_path = tmp_path / "eval_trend.jsonl"
    _seed_trend(trend_path, 59)
    before = trend_path.read_text(encoding="utf-8")
    _, _, first, _history = mod.persist_eval_output(
        out,
        evaluation_path=tmp_path / "e.json",
        trend_path=trend_path,
        pending_path=tmp_path / "p.json",
        profile="p",
        now="2026-09-30T00:14:12",
    )
    assert first == mod.TREND_FALSE_ZERO, "夹具先要真的走到假零那一支（否则本条没在测判序）"
    # 咽喉拒写 ⇒ 历史没动；把最新一条换成同一份 0 样本快照，于是「内容相同」与「假零」同时成立
    zero = mod.trend_snapshot(out, profile="p", now="2026-09-30T00:14:12")
    trend_path.write_text(before + json.dumps({k: v for k, v in zero.items() if k != "ts"}) + "\n", encoding="utf-8")
    _, _, second, _history = mod.persist_eval_output(
        out,
        evaluation_path=tmp_path / "e.json",
        trend_path=trend_path,
        pending_path=tmp_path / "p.json",
        profile="p",
        now="2026-09-30T00:14:12",
    )
    assert second == mod.TREND_DUPLICATE, "措辞要跟咽喉实际走的分支一致：幂等跳过优先于假零"


def test_resolve_paths_carries_trend_and_pending():
    """#37 路径接入 resolve_paths（#28 唯一出口）：趋势时序 + 待审队列。"""
    paths = mod.resolve_paths(env={})
    assert paths["trend"].name == "eval_trend.jsonl"
    assert paths["pending"].name == "pending_promotions.json"
    assert paths["pending"].parent.name == "kb"


# --- #37 正例池硬上限与淘汰（拍板 Q3：FIFO+降权建议、人工 --retire 确认、绝不静默删除）---


def _seed_winners(n, tmp_path):
    """写入 n 条正例的 winning-examples.md，条目日期/回复率随序号可辨（条目 i 较旧/较低）。"""
    lines = ["# 招呼语效果回写", "", "## 正例（few-shot）", ""]
    for i in range(n):
        rate = 10 - i % 10  # 条目 0 回复率最高、9 最低，循环
        day = f"2026-08-{i + 1:02d}" if i < 28 else f"2026-09-{i - 27:02d}"
        lines.append(f"- [by_level:中 | 回复率 {rate}% | 样本 17 | {day}] 正例条目{i}")
    lines += ["", "## 避坑（低回复模式）", "", "（暂无）", ""]
    p = tmp_path / "winning-examples.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def test_suggest_retirements_fifo_then_low_rate():
    """建议淘汰排序：最旧优先，同旧看低回复率（拍板 Q3「FIFO 结合近期回复率降权」）。"""
    entries = [
        "[k | 回复率 5% | 样本 10 | 2026-08-05] 较新但低效",
        "[k | 回复率 8% | 样本 10 | 2026-08-01] 最旧高效",
        "[k | 回复率 3% | 样本 10 | 2026-08-01] 最旧低效",
        "坏格式条目",
    ]
    picks = mod.suggest_retirements(entries, overflow=3)
    assert picks[0] == "最旧低效", "最旧+最低回复率最优先"
    assert picks[1] == "最旧高效"
    assert picks[2] == "坏格式条目", "解析失败的坏条目视为无凭据，排最后但仍在建议内"


def test_promote_winners_over_limit_blocked_with_suggestions(tmp_path, capsys):
    """超上限且未点名淘汰：blocked 不写盘，审阅界面列出建议淘汰项（绝不静默删除）。"""
    out_path = _seed_winners(mod.WINNING_MAX_TOTAL, tmp_path)
    res = mod.promote_winners(
        _evaluation_with_candidates(),
        ["by_pattern:question_cta"],
        out_path=out_path,
        yes=True,
        now="2026-09-17T10:00:00",
    )
    assert res is False, "超硬上限且无 --retire 时须拒绝写入"
    out = capsys.readouterr().out
    assert "Suggested retirement" in out and "正例条目" in out


def test_promote_winners_under_limit_no_suggestion(tmp_path, capsys):
    out_path = _seed_winners(3, tmp_path)
    res = mod.promote_winners(
        _evaluation_with_candidates(),
        ["by_pattern:question_cta"],
        out_path=out_path,
        yes=True,
        now="2026-09-17T10:00:00",
    )
    assert res is True
    assert "Suggested retirement" not in capsys.readouterr().out


def test_promote_winners_retire_removes_named_and_writes(tmp_path):
    """--retire 点名淘汰 + --yes：指定条目移除、新候选写入，总数回到上限内。"""
    out_path = _seed_winners(mod.WINNING_MAX_TOTAL, tmp_path)
    res = mod.promote_winners(
        _evaluation_with_candidates(),
        ["by_pattern:question_cta"],
        out_path=out_path,
        yes=True,
        retire=["正例条目0"],
        now="2026-09-17T10:00:00",
    )
    assert res is True
    text = out_path.read_text(encoding="utf-8")
    assert "正例条目0" not in text, "被点名的条目须移除"
    assert "我有7年经验，请问技术栈？" in text, "新候选照常写入"
    winners = mod.load_winning_examples(out_path)["winners"]
    assert len(winners) == mod.WINNING_MAX_TOTAL, "淘汰 1 进 1，总数守住上限"


def test_promote_winners_retire_ambiguous_or_missing_blocked(tmp_path, capsys):
    out_path = _seed_winners(3, tmp_path)
    res = mod.promote_winners(
        _evaluation_with_candidates(),
        ["by_pattern:question_cta"],
        out_path=out_path,
        yes=True,
        retire=["不存在的条目"],
        now="2026-09-17T10:00:00",
    )
    assert res is False and "[blocked]" in capsys.readouterr().out


# --- #37 起草端确定性正例匹配（拍板 Q4；声明性偏离：注入点 compose_greeting_prompt 已退役，改工具+runbook）---


def _winning_with_tagged_entries(tmp_path):
    p = tmp_path / "winning-examples.md"
    p.write_text(
        "# 头\n\n## 正例（few-shot）\n\n"
        "- [by_addressee:hr | 回复率 4% | 样本 23 | 2026-09-15] hr 范式正文\n"
        "- [by_level:中 | 回复率 6% | 样本 17 | 2026-09-15] 中级范式正文\n"
        "- [by_pattern:question_cta | 回复率 5% | 样本 21 | 2026-09-15] 提问范式正文\n"
        "- [by_addressee:leader | 回复率 0% | 样本 5 | 2026-09-15] leader 范式正文\n\n"
        "## 避坑（低回复模式）\n\n- [by_pattern:availability | 回复率 0% | 样本 24 | 2026-09-15] 避坑正文\n",
        encoding="utf-8",
    )
    return p


def test_match_examples_prefers_addressee_then_level(tmp_path):
    """拍板 Q4 确定性标签匹配：addressee 优先（+3），其次 level（+2）；pattern 仅作同分
    次序键不独立触发（无 addressee/level 命中 → 空，不强行注入）。"""
    p = _winning_with_tagged_entries(tmp_path)
    picks = mod.match_examples(p, addressee="hr", level="中", limit=2)
    assert picks[0]["key"] == "by_addressee:hr"
    assert picks[0]["text"] == "hr 范式正文"
    assert picks[1]["key"] == "by_level:中"


def test_match_examples_respects_limit(tmp_path):
    p = _winning_with_tagged_entries(tmp_path)
    assert len(mod.match_examples(p, addressee="hr", limit=1)) == 1


def test_match_examples_skips_avoid_section(tmp_path):
    """避坑条目不入选——起草只参考正例范式。"""
    p = _winning_with_tagged_entries(tmp_path)
    picks = mod.match_examples(p, addressee="hr", level="中", limit=10)
    assert all(p["key"] != "by_pattern:availability" for p in picks)


def test_match_examples_no_match_returns_empty(tmp_path):
    p = _winning_with_tagged_entries(tmp_path)
    assert mod.match_examples(p, addressee="ceo", level="初") == []


def test_match_examples_missing_file_returns_empty(tmp_path):
    assert mod.match_examples(tmp_path / "nope.md", addressee="hr") == []


# --- #37 会话级 EDD 收尾快照（拍板 Q1：live_apply 收尾自动调用，纯本地 <0.5s）---


def _session_paths(tmp_path):
    return {
        "followup": tmp_path / "followup_report.json",
        "greetings": tmp_path / "greetings.json",
        "annotations": tmp_path / "reply_annotations.json",
        "trend": tmp_path / "eval_trend.jsonl",
    }


def test_record_session_snapshot_appends_trend_and_reviews(tmp_path):
    """有 followup 数据 → 跑 eval → 追加趋势快照 + 批次级审阅判定。"""
    paths = _session_paths(tmp_path)
    paths["followup"].write_text(json.dumps(_eval_report()), encoding="utf-8")
    paths["greetings"].write_text(json.dumps(_eval_greetings()), encoding="utf-8")
    paths["annotations"].write_text("{}", encoding="utf-8")
    result = mod.record_session_snapshot(paths, profile="p", now="2026-09-17T10:00:00")
    assert result["trend_appended"] is True
    assert result["snapshot"]["sampleSize"] == 2
    assert "due" in result["review_due"] and "reasons" in result["review_due"]
    trend = json.loads(paths["trend"].read_text(encoding="utf-8").splitlines()[-1])
    assert trend["sampleSize"] == 2


def test_record_session_snapshot_skips_without_followup(tmp_path):
    """无 followup 数据（从未复盘过的全新账号）→ 跳过快照不报错；#49 起正常投递会话
    收尾已现场采集过一次，本路径只剩冷启动场景。"""
    paths = _session_paths(tmp_path)
    result = mod.record_session_snapshot(paths, profile="p", now="2026-09-17T10:00:00")
    assert result["trend_appended"] is False
    assert "no followup" in result["reason"]
    assert not paths["trend"].exists()


def test_record_session_snapshot_eval_failure_propagates(tmp_path):
    """followup 坏 JSON → 抛异常（由 live_apply 收尾 try/except 兜底，函数本身不吞，
    保证调用方能感知故章而非静默跳过）。"""
    paths = _session_paths(tmp_path)
    paths["followup"].write_text("not json", encoding="utf-8")
    paths["greetings"].write_text("[]", encoding="utf-8")
    try:
        mod.record_session_snapshot(paths, profile="p", now="2026-09-17T10:00:00")
        assert False, "坏 followup 须抛异常而非静默跳过"
    except (ValueError, json.JSONDecodeError):
        pass


# --- #59 防线 2：0 样本快照不追加趋势（残缺采集算出的假零点会污染 eval_trend 历史）---


def _n_pair_fixtures(n):
    """n 组「会话 × 招呼语」可配对记录（形态同 _dense_eval_fixtures，品牌互不撞名）：
    给「历史 50 / 本场 51」这类非零变化用例供数。"""
    convs, greets = [], []
    for i in range(n):
        brand = f"增长测试公司{i}"
        convs.append({"company": brand, "summary": "[送达]", "status": "awaiting", "appliedJob": "区块链工程师"})
        greets.append(
            {
                "encryptJobId": f"g{i}",
                "jobName": "区块链工程师",
                "brandName": brand,
                "level": "高",
                "addressee": "hr",
                "greeting": f"我有7年经验，交付过DeFi协议{i}",
            }
        )
    return {"conversations": convs}, greets


def _unpaired_report(n=3):
    """conversations 非空、但一条都配不上招呼语台账 ⇒ eval 出 sampleSize 0
    （#59 里被首页截断的残缺报告在收尾 eval 上的形态）。"""
    convs = [{"company": f"没发过招呼{i}", "summary": "[送达]", "status": "awaiting"} for i in range(n)]
    return {"conversations": convs}


def _seed_trend(path, sample_size, ts="2026-09-29T23:59:00"):
    """趋势历史先写一条快照（历史里是否存在非 0 点是 #59 防线的对照基准）。"""
    path.write_text(
        json.dumps({"ts": ts, "profile": "p", "sampleSize": sample_size, "repliedCount": 2, "overallRate": 0.04})
        + "\n",
        encoding="utf-8",
    )


def test_record_session_snapshot_skips_zero_sample_after_nonzero_history(tmp_path):
    """#59 验收 2：历史最新点 50、本场 0 样本 ⇒ eval_trend.jsonl 不新增行，
    outcome 带 trend_appended=False + reason + snapshot（假零不进趋势历史）。"""
    paths = _session_paths(tmp_path)
    paths["followup"].write_text(json.dumps(_unpaired_report()), encoding="utf-8")
    paths["greetings"].write_text(json.dumps(_eval_greetings()), encoding="utf-8")
    paths["annotations"].write_text("{}", encoding="utf-8")
    _seed_trend(paths["trend"], 50)
    before = paths["trend"].read_text(encoding="utf-8")
    result = mod.record_session_snapshot(paths, profile="p", now="2026-09-30T00:10:19")
    assert result["trend_appended"] is False
    assert result["snapshot"]["sampleSize"] == 0
    assert "0 样本" in result["reason"] and "残缺" in result["reason"], result
    assert paths["trend"].read_text(encoding="utf-8") == before, "0 样本快照写进了趋势历史"


def test_record_session_snapshot_refusal_branches_match_the_normal_shape(tmp_path):
    """R4-2：跑过 eval 之后的两个「拒写」出口（假零 / 幂等跳过）返回体形状必须与正常分支一致——
    `review_due` + `density_due` 两键恒在。旧版假零分支只回 reason + snapshot，注释却声称
    「density_due 吃 eval 输出、不受本防线影响」，而那个返回体里键根本不存在：
    `live_apply.edd_wrapup` 对两键只做 `.get(...) or {}`，缺键时整块提示判定在消费侧消失，
    消费方分辨不了「没有键」与「判定为否」（该形态下 due 大概率为 False，但那是另一件事）。
    #63 切片 B 给两个后-eval 出口都加了 `trend_status`（`is_appended` 的唯一取数处，
    样本下降那一格靠它念出来）——本条钉的不变量没变：**拒写分支形状 = 正常分支形状 + reason**，
    下面 `shape()` 用的就是 `set(normal) | {"reason"}`，所以加键不用改那条断言。"""
    normal_paths = _session_paths(tmp_path / "normal")
    (tmp_path / "normal").mkdir()
    normal_paths["followup"].write_text(json.dumps(_eval_report()), encoding="utf-8")
    normal_paths["greetings"].write_text(json.dumps(_eval_greetings()), encoding="utf-8")
    normal_paths["annotations"].write_text("{}", encoding="utf-8")
    normal = mod.record_session_snapshot(normal_paths, profile="p", now="2026-09-17T10:00:00")
    assert normal["trend_appended"] is True
    # 两个出口的键集合对称性本身（#82 起带 `trend_history`：append 前的历史，渲染方一次性输入；
    # #63 起带 `trend_status`：「写没写盘」的唯一问法）。注册新键不改变这条要守的东西。
    assert set(normal) == {
        "trend_appended",
        "trend_status",
        "snapshot",
        "review_due",
        "density_due",
        "trend_history",
    }, normal

    def shape(result):
        assert result["trend_appended"] is False, "夹具须真的走到拒写分支"
        assert set(result) == set(normal) | {"reason"}, result
        assert "due" in result["review_due"] and "due" in result["density_due"], result

    # ① 假零：历史已有非 0 点、本场 0 样本
    (tmp_path / "false_zero").mkdir()
    fz = _session_paths(tmp_path / "false_zero")
    fz["followup"].write_text(json.dumps(_unpaired_report()), encoding="utf-8")
    fz["greetings"].write_text(json.dumps(_eval_greetings()), encoding="utf-8")
    fz["annotations"].write_text("{}", encoding="utf-8")
    _seed_trend(fz["trend"], 50)
    shape(mod.record_session_snapshot(fz, profile="p", now="2026-09-30T00:10:19"))
    # ② 幂等跳过：同一份报告连跑两次（第二次与最新一条内容相同）——旧版这条也没带 reason
    dup = mod.record_session_snapshot(normal_paths, profile="p", now="2026-09-17T10:00:00")
    assert "幂等" in dup["reason"], dup
    shape(dup)


def test_record_session_snapshot_hands_out_the_pre_append_history(tmp_path):
    """#82 会话侧契约：`trend_history` 是**追加之前**读到的历史，不含本场快照自己——渲染方
    （`live_apply.edd_wrapup`）靠它出「vs 上次」；写后读的话 `trend[-1]` 就是本场，增量恒 +0。
    拒写分支也带这一键（形状对称），内容同样是 append 前那份（咽喉没写，历史本就未变）。"""
    paths = _session_paths(tmp_path)
    paths["followup"].write_text(json.dumps(_eval_report()), encoding="utf-8")
    paths["greetings"].write_text(json.dumps(_eval_greetings()), encoding="utf-8")
    paths["annotations"].write_text("{}", encoding="utf-8")
    _seed_trend(paths["trend"], 50, ts="2026-10-20T21:25:13")
    result = mod.record_session_snapshot(paths, profile="p", now="2026-10-22T05:38:11")
    assert result["trend_appended"] is True
    assert [h["ts"] for h in result["trend_history"]] == ["2026-10-20T21:25:13"], result["trend_history"]
    assert result["snapshot"]["ts"] not in [h["ts"] for h in result["trend_history"]]
    assert len(paths["trend"].read_text(encoding="utf-8").splitlines()) == 2, "本场快照没入账"

    refused = mod.record_session_snapshot(paths, profile="p", now="2026-10-22T06:00:00")
    assert refused["trend_appended"] is False and "幂等" in refused["reason"], refused
    assert [h["sampleSize"] for h in refused["trend_history"]] == [50, 2], "拒写分支的历史不是 append 前那份"


def test_record_session_snapshot_refusal_reason_comes_from_the_chokepoint_status(tmp_path):
    """R4-5 的会话侧：reason 只按 `append_trend` 的返回状态出，本函数不再自己
    `false_zero_snapshot` 判第二遍（旧形态判序在咽喉与调用方各存一份）。两条拒写原因各有
    自己的文案，都点名「没写进趋势」这件事——历史读数只进文案、不进判定。"""
    paths = _session_paths(tmp_path)
    paths["followup"].write_text(json.dumps(_unpaired_report()), encoding="utf-8")
    paths["greetings"].write_text(json.dumps(_eval_greetings()), encoding="utf-8")
    paths["annotations"].write_text("{}", encoding="utf-8")
    _seed_trend(paths["trend"], 50)
    result = mod.record_session_snapshot(paths, profile="p", now="2026-09-30T00:10:19")
    assert "不追加趋势以免写进假零点" in result["reason"], result
    assert "最新 50 样本" in result["reason"], result  # 历史里的最新读数进的是文案


def test_record_session_snapshot_appends_zero_sample_on_empty_history(tmp_path):
    """历史为空 ⇒ 维持现状仍追加首点（冷启动记 baseline 是既有语义，防线只拦「历史非 0」）。"""
    paths = _session_paths(tmp_path)
    paths["followup"].write_text(json.dumps(_unpaired_report()), encoding="utf-8")
    paths["greetings"].write_text(json.dumps(_eval_greetings()), encoding="utf-8")
    paths["annotations"].write_text("{}", encoding="utf-8")
    result = mod.record_session_snapshot(paths, profile="p", now="2026-09-30T00:10:19")
    assert result["trend_appended"] is True
    assert json.loads(paths["trend"].read_text(encoding="utf-8").splitlines()[-1])["sampleSize"] == 0


def test_record_session_snapshot_appends_growth_after_nonzero_history(tmp_path):
    """历史 50、本场 51 ⇒ 正常追加（防线只认 0 样本，不拦真实增长）。"""
    paths = _session_paths(tmp_path)
    report, greets = _n_pair_fixtures(51)
    paths["followup"].write_text(json.dumps(report), encoding="utf-8")
    paths["greetings"].write_text(json.dumps(greets), encoding="utf-8")
    paths["annotations"].write_text("{}", encoding="utf-8")
    _seed_trend(paths["trend"], 50)
    result = mod.record_session_snapshot(paths, profile="p", now="2026-09-30T00:10:19")
    assert result["snapshot"]["sampleSize"] == 51
    assert result["trend_appended"] is True
    assert len(paths["trend"].read_text(encoding="utf-8").splitlines()) == 2


_SAMPLE_JUMP_REASON = "样本较上次记录新增 10 条（≥10）"


def test_record_session_snapshot_append_branch_reviews_against_pre_append_history(tmp_path):
    """#82 同族缺陷（追加成功分支）：`review_due` 必须吃 append **之前**的历史。
    旧写法写完才 `load_trend` ⇒ `trend[-1]` 是本场快照自己 ⇒ `step` 恒 0，
    批次级审阅提示（#37 拍板 Q1 的决策级机制）在正常收尾路径上永不触发。
    历史 40、本场 50 ⇒ step=10 须给出「样本较上次记录新增 10 条」这条理由。"""
    paths = _session_paths(tmp_path)
    report, greets = _n_pair_fixtures(50)
    paths["followup"].write_text(json.dumps(report), encoding="utf-8")
    paths["greetings"].write_text(json.dumps(greets), encoding="utf-8")
    paths["annotations"].write_text("{}", encoding="utf-8")
    _seed_trend(paths["trend"], 40)
    result = mod.record_session_snapshot(paths, profile="p", now="2026-09-30T00:10:19")
    assert result["trend_appended"] is True, result
    assert result["snapshot"]["sampleSize"] == 50
    assert _SAMPLE_JUMP_REASON in result["review_due"]["reasons"], result["review_due"]
    assert result["review_due"]["due"] is True, result["review_due"]


def test_record_session_snapshot_append_branch_stays_quiet_below_the_step(tmp_path):
    """反向钉住阈值（防把判定写宽）：历史 47、本场 50 ⇒ step=3 < 10，
    不出现样本跃迁理由（另一条「超 3 天且有新回复」由夹具的 ts 差 0 天排除）。"""
    paths = _session_paths(tmp_path)
    report, greets = _n_pair_fixtures(50)
    paths["followup"].write_text(json.dumps(report), encoding="utf-8")
    paths["greetings"].write_text(json.dumps(greets), encoding="utf-8")
    paths["annotations"].write_text("{}", encoding="utf-8")
    _seed_trend(paths["trend"], 47)
    result = mod.record_session_snapshot(paths, profile="p", now="2026-09-30T00:10:19")
    assert result["trend_appended"] is True, result
    assert not any("样本较上次记录新增" in r for r in result["review_due"]["reasons"]), result["review_due"]
    assert result["review_due"]["due"] is False, result["review_due"]


def test_annotation_for_conv_prefers_encrypt_job_id():
    # #32 段 2：同 eid 两轮采集 company 串漂移（砚女士煦辰招聘者 → 砚女士），eid 优先防失配
    conv = {"company": "砚女士", "encryptJobId": "eid1"}
    anns = [
        {"company": "砚女士", "replyCategory": "no_reply"},
        {"company": "砚女士煦辰招聘者", "encryptJobId": "eid1", "replyCategory": "interested"},
    ]
    assert mod.annotation_for_conv(conv, anns)["replyCategory"] == "interested"


def test_annotation_for_conv_falls_back_to_company_without_eid():
    conv = {"company": "某公司", "appliedJob": "区块链工程师"}
    anns = [{"company": "某公司", "appliedJob": "区块链工程师", "replyCategory": "interested"}]
    assert mod.annotation_for_conv(conv, anns)["replyCategory"] == "interested"
    assert mod.annotation_for_conv({"company": "无匹配"}, anns) is None


# --- #45 密度阈值提示制：eval 与会话收尾快照携带密度判定，达按主动叫人（提示制非自动晋级）---


def _dense_eval_fixtures(n_replied=5, n_total=7):
    """by_addressee:hr 组内回复 ≥5 的真实 eval 输入（走 join_eval_records 全链，非合成 groups）。"""
    convs, greets = [], []
    for i in range(n_total):
        replied = i < n_replied
        brand = f"测试公司{i}"  # match_brand min_len=4：名字过短配不上对
        convs.append(
            {
                "company": brand,
                "summary": "聊聊吗" if replied else "[送达]",
                "status": "hr_replied" if replied else "awaiting",
                "appliedJob": "区块链工程师",
            }
        )
        greets.append(
            {
                "encryptJobId": f"e{i}",
                "jobName": "区块链工程师",
                "brandName": brand,
                "level": "高",
                "addressee": "hr",
                "greeting": f"我有7年经验，交付过DeFi协议{i}",
            }
        )
    return {"conversations": convs}, greets


def _eval_cmd_paths(tmp_path, report, greetings):
    paths = {
        "name": "p",
        "followup": tmp_path / "followup_report.json",
        "greetings": tmp_path / "greetings.json",
        "annotations": tmp_path / "reply_annotations.json",
        "evaluation": tmp_path / "greeting_evaluation.json",
        "trend": tmp_path / "eval_trend.jsonl",
        "pending": tmp_path / "pending_promotions.json",
    }
    paths["followup"].write_text(json.dumps(report), encoding="utf-8")
    paths["greetings"].write_text(json.dumps(greetings), encoding="utf-8")
    paths["annotations"].write_text("{}", encoding="utf-8")
    return paths


def test_record_session_snapshot_carries_density_due(tmp_path, monkeypatch):
    """快照 outcome 携带密度判定（透传本次 eval 输出给 density_due）。"""
    paths = _session_paths(tmp_path)
    paths["followup"].write_text(json.dumps(_eval_report()), encoding="utf-8")
    paths["greetings"].write_text(json.dumps(_eval_greetings()), encoding="utf-8")
    paths["annotations"].write_text("{}", encoding="utf-8")
    seen = {}

    def fake_density(ev, threshold=None):
        seen["ev"] = ev
        return {"due": True, "threshold": 5, "groups": ["by_addressee:hr（回复 5/5）"]}

    monkeypatch.setattr(mod, "density_due", fake_density)
    result = mod.record_session_snapshot(paths, profile="p", now="2026-09-17T10:00:00")
    assert result["density_due"]["due"] is True
    assert seen["ev"]["sampleSize"] == 2


def test_record_session_snapshot_density_silent_below_threshold(tmp_path):
    """低密度（现有 2 条配对 1 回复）→ density_due due=False，不抛错不额外输出。"""
    paths = _session_paths(tmp_path)
    paths["followup"].write_text(json.dumps(_eval_report()), encoding="utf-8")
    paths["greetings"].write_text(json.dumps(_eval_greetings()), encoding="utf-8")
    paths["annotations"].write_text("{}", encoding="utf-8")
    result = mod.record_session_snapshot(paths, profile="p", now="2026-09-17T10:00:00")
    assert result["density_due"]["due"] is False


def test_cmd_eval_prompts_on_density(tmp_path, capsys, monkeypatch):
    """eval 命令是「跑 followup 复盘」通道：密度达标 → 提示块 + 候选清单 + macOS 通知。"""
    report, greetings = _dense_eval_fixtures()
    paths = _eval_cmd_paths(tmp_path, report, greetings)
    paths["pending"].write_text(
        json.dumps({"candidates": [{"key": "by_addressee:hr", "kind": "winning", "sample": 5, "replyRate": 1.0}]}),
        encoding="utf-8",
    )
    notified = []
    monkeypatch.setattr(mod, "notify", lambda t, m: notified.append((t, m)))
    mod._cmd_eval(argparse.Namespace(trend=False), paths)
    out = capsys.readouterr().out
    assert "💡 [范式审阅]" in out and "是不是要更新招呼语范式" in out
    assert "by_addressee:hr" in out and "promote-winners" in out
    assert len(notified) == 1 and "范式" in notified[0][0]


def test_cmd_eval_silent_below_density(tmp_path, capsys, monkeypatch):
    """低密度静默（拍板 3）：不出现范式审阅块、不发通知（既有输出不变）。"""
    report, greetings = _eval_report(), _eval_greetings()
    paths = _eval_cmd_paths(tmp_path, report, greetings)
    notified = []
    monkeypatch.setattr(mod, "notify", lambda t, m: notified.append((t, m)))
    mod._cmd_eval(argparse.Namespace(trend=False), paths)
    out = capsys.readouterr().out
    assert "范式审阅" not in out
    assert notified == []


def test_cmd_eval_reports_legacy_exclusion(tmp_path, capsys, monkeypatch):
    """#64 追加验收 7：eval 输出如实打出被排除的历史遗留条数；为零时整行不出现。"""
    report, greetings = _legacy_eval_fixtures()
    paths = _eval_cmd_paths(tmp_path, report, greetings)
    monkeypatch.setattr(mod, "notify", lambda t, m: None)
    mod._cmd_eval(argparse.Namespace(trend=False), paths)
    assert "排除历史遗留招呼语 2 条" in capsys.readouterr().out


def test_cmd_eval_silent_without_legacy(tmp_path, capsys, monkeypatch):
    paths = _eval_cmd_paths(tmp_path, _eval_report(), _eval_greetings())
    monkeypatch.setattr(mod, "notify", lambda t, m: None)
    mod._cmd_eval(argparse.Namespace(trend=False), paths)
    assert "历史遗留" not in capsys.readouterr().out


# --- #63 切片 B：趋势分母接上两个写趋势入口 + decline 状态不静默 ---


def _ledger_report(convs, shape, prev_n):
    """一份「writer 已把签名落进去」的 followup 报告形状。
    `followupLedger` 由生产函数（`compare` + `build_ledger`）造，夹具不手搓键名——
    键集合改了这边会跟着红，而不是静默空过。`shape` 覆盖成用例要点名那一格。"""
    prev = {"conversations": [{"encryptJobId": f"p{i}"} for i in range(prev_n)]}
    reading = {**fshape.compare(fshape.report_conversations(prev), convs), "shape": shape}
    return {"conversations": convs, "followupLedger": fshape.build_ledger(reading)}


def test_record_session_snapshot_carries_denominator_into_trend(tmp_path):
    """会话侧（投递收尾真读的那条路）：快照里 `denominator` 恒在，分母取自刚 eval 的那份报告——
    报告带着 writer 落的 `followupLedger` 时签名一起进去（#75 review 条目 B 同病：只接一个入口，
    恒在键恰在用户真读的那条路上破）。"""
    paths = _session_paths(tmp_path)
    convs = _eval_report()["conversations"]
    paths["followup"].write_text(json.dumps(_ledger_report(convs, "scattered_lost", prev_n=9)), encoding="utf-8")
    paths["greetings"].write_text(json.dumps(_eval_greetings()), encoding="utf-8")
    paths["annotations"].write_text("{}", encoding="utf-8")
    result = mod.record_session_snapshot(paths, profile="p", now="2026-10-09 12:00:00")
    expected = {"conversations": len(convs), "shape": "scattered_lost"}
    assert result["snapshot"]["denominator"] == expected, result
    trend = json.loads(paths["trend"].read_text(encoding="utf-8").splitlines()[-1])
    assert trend["denominator"] == expected, trend


def test_record_session_snapshot_denominator_is_none_without_evidence(tmp_path):
    """旧报告（没有 `followupLedger` 那一格）：条数读得到 ⇒ 分母在、签名是 None；
    两个都读不到 ⇒ 整个 `denominator` 是 None，**不补 0**。"""
    paths = _session_paths(tmp_path)
    paths["followup"].write_text(json.dumps(_eval_report()), encoding="utf-8")
    paths["greetings"].write_text(json.dumps(_eval_greetings()), encoding="utf-8")
    paths["annotations"].write_text("{}", encoding="utf-8")
    result = mod.record_session_snapshot(paths, profile="p", now="2026-10-09 12:00:00")
    assert result["snapshot"]["denominator"] == {"conversations": 2, "shape": None}, result


def test_record_session_snapshot_reports_decline_as_written(tmp_path):
    """#63 验收 4 的会话侧：样本比趋势最新一条少 ⇒ `trend_appended` 仍是 True（已落盘），
    额外的状态走 `trend_status`（「写没写盘」由 `_edd.is_appended` 一处问，判序不在这里复制）。"""
    paths = _session_paths(tmp_path)
    paths["followup"].write_text(json.dumps(_eval_report()), encoding="utf-8")
    paths["greetings"].write_text(json.dumps(_eval_greetings()), encoding="utf-8")
    paths["annotations"].write_text("{}", encoding="utf-8")
    _seed_trend(paths["trend"], 50)
    result = mod.record_session_snapshot(paths, profile="p", now="2026-10-09 12:00:00")
    assert result["trend_appended"] is True and result["trend_status"] == mod.TREND_APPENDED_DECLINE, result
    assert len(paths["trend"].read_text(encoding="utf-8").splitlines()) == 2, "decline 被当成拒写，样本消失了一场"
    assert result["snapshot"]["sampleSize"] == 2
    # 拒写分支的形状不变量仍在：`trend_status` 同样带上，只是 `trend_appended` 为 False
    _seed_trend(paths["trend"], 50)
    paths["followup"].write_text(json.dumps(_unpaired_report()), encoding="utf-8")
    refused = mod.record_session_snapshot(paths, profile="p", now="2026-10-09 13:00:00")
    assert refused["trend_appended"] is False and refused["trend_status"] == mod.TREND_FALSE_ZERO, refused


def test_persist_eval_output_passes_denominator_through(tmp_path):
    """eval 侧入口：调用方给得出分母就必须落进快照与磁盘那一行；给不出（默认 None）时键恒在、值 None。"""
    out = mod.eval_greetings(_eval_report(), _eval_greetings(), [], now="2026-10-09T12:00:00")
    trend_path = tmp_path / "eval_trend.jsonl"
    given = {"conversations": 76, "shape": "scattered_lost"}
    _, snap, status, _hist = mod.persist_eval_output(
        out,
        evaluation_path=tmp_path / "e.json",
        trend_path=trend_path,
        pending_path=tmp_path / "p.json",
        profile="p",
        now="2026-10-09T12:00:00",
        denominator=given,
    )
    assert status == mod.TREND_APPENDED and snap["denominator"] == given
    assert json.loads(trend_path.read_text(encoding="utf-8").splitlines()[-1])["denominator"] == given
    _, bare, _st, _h2 = mod.persist_eval_output(
        out,
        evaluation_path=tmp_path / "e2.json",
        trend_path=tmp_path / "t2.jsonl",
        pending_path=tmp_path / "p2.json",
        profile="p",
        now="2026-10-09T12:00:00",
    )
    assert bare["denominator"] is None, "缺省不落 0"


def test_cmd_eval_lands_denominator_from_the_report_it_evaluated(tmp_path, monkeypatch, capsys):
    """接线证明（#33 那类「函数没问题但调用点没接」的教训）：`eval` 命令把**它自己刚读的那份报告**
    的分母传给 `persist_eval_output`，不是事后反推。"""
    seen = {}
    real = mod.persist_eval_output

    def spy(out, **kw):
        seen["denominator"] = kw.get("denominator")
        return real(out, **kw)

    monkeypatch.setattr(mod, "persist_eval_output", spy)
    report = _ledger_report(_eval_report()["conversations"], "tail_truncated", prev_n=9)
    paths = _eval_cmd_paths(tmp_path, report, _eval_greetings())
    monkeypatch.setattr(mod, "notify", lambda t, m: None)
    mod._cmd_eval(argparse.Namespace(trend=False), paths)
    assert seen["denominator"] == {"conversations": 2, "shape": "tail_truncated"}, seen
    assert "趋势快照已追加" in capsys.readouterr().out


def test_cmd_eval_decline_wording_names_the_drop(tmp_path, monkeypatch, capsys):
    """`eval --trend` 侧的措辞：decline 那一格的动词仍是「已追加」（说了谎的「未追加」更糟），
    括注必须点名「样本数低于最新一条」（票面验收 4：不静默追加）。"""
    paths = _eval_cmd_paths(
        tmp_path, _ledger_report(_eval_report()["conversations"], "stable", prev_n=2), _eval_greetings()
    )
    _seed_trend(paths["trend"], 50)
    monkeypatch.setattr(mod, "notify", lambda t, m: None)
    mod._cmd_eval(argparse.Namespace(trend=False), paths)
    out = capsys.readouterr().out
    assert "趋势快照已追加" in out and "样本数低于最新一条" in out, out


def _seed_trend_history(path, points):
    """预置多条趋势历史（真实 eval_trend.jsonl 形状，#82 用）。"""
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in points),
        encoding="utf-8",
    )
    return path


def test_cmd_eval_trend_table_compares_the_pre_append_history(tmp_path, capsys, monkeypatch):
    """#82 的 `eval --trend` 侧：对比表的 previous 列与 Δprev 必须是**上一条**，不是本场自己。
    旧写法在 `persist_eval_output` 追加之后才 `load_trend` ⇒ `trend[-1] == current`，previous 列
    与 current 列同值、Δprev 恒 +0。历史 40/1 + 47/2 → 本场 50/4 ⇒ Δprev 样本 +3 / 回复 +2。"""
    report, greetings = _dense_eval_fixtures(4, 50)
    paths = _eval_cmd_paths(tmp_path, report, greetings)
    _seed_trend_history(
        paths["trend"],
        [
            {
                "ts": "2026-10-18T08:00:00",
                "profile": "p",
                "sampleSize": 40,
                "repliedCount": 1,
                "overallRate": 0.025,
                "minTotal": 30,
            },
            {
                "ts": "2026-10-20T21:25:13",
                "profile": "p",
                "sampleSize": 47,
                "repliedCount": 2,
                "overallRate": 2 / 47,
                "minTotal": 30,
            },
        ],
    )
    monkeypatch.setattr(mod, "notify", lambda t, m: None)
    mod._cmd_eval(argparse.Namespace(trend=True), paths)
    out = capsys.readouterr().out
    assert "== EDD 趋势对比 ==" in out, out
    table = out.split("== EDD 趋势对比 ==")[1]
    row = next(line for line in table.splitlines() if line.strip().startswith("样本"))
    assert "previous(2026-10-20)" in table, f"previous 列不是上一条：{table}"
    assert "+3" in row and "47" in row, f"Δprev 不是相对上一条的差值：{row}"
    replied_row = next(line for line in table.splitlines() if line.strip().startswith("回复"))
    assert "+2" in replied_row, f"回复 Δprev 须 +2：{replied_row}"


def _trend_table_row(text, label):
    """从 `eval --trend` 的终端输出里按行首标签切出对比表的一行单元格（列值与 Δ 值都不含空格）。"""
    table = text.split("== EDD 趋势对比 ==")[1]
    line = next(ln for ln in table.splitlines() if ln.strip().startswith(label))
    return line.split()


def test_cmd_eval_trend_table_keeps_the_duplicate_snapshot_as_previous(tmp_path, capsys, monkeypatch):
    """#82 禁法的正例（duplicate 分支）：本场快照入账后紧接着再跑一次同内容的 `eval --trend` ⇒
    `append_trend` 幂等跳过、趋势文件没长，但 append **前**那份历史里**确实**含着刚入账的这条
    ⇒ previous 列就是它，Δprev +0 是**合法读数**（同一份数据读两遍，本来就没变），读数不塌冷启动。
    「末条内容与 current 相同就剔除」的兜底会把 previous 退到更早的一条（Δprev 变成样本 +3 / 回复 +2、
    回复率 +3.7pp），历史只剩一条时更直接塌成「baseline 记录于本条」——那种兜底一进仓本条就红。
    末段再钉一种同内容且**同 ts** 的入账形态（同一秒内重跑 `eval` 就是这样）：上面 CLI 那次的 ts 由
    墙钟决定、与本场是否同秒不可控，按 ts 剔除的兜底只有在同秒时才被前面那几条断言抓到，这里用
    显式 `now` 把它变成必然。"""
    report, greetings = _dense_eval_fixtures(4, 50)
    paths = _eval_cmd_paths(tmp_path, report, greetings)
    _seed_trend_history(
        paths["trend"],
        [
            {
                "ts": "2026-10-18T08:00:00",
                "profile": "p",
                "sampleSize": 40,
                "repliedCount": 1,
                "overallRate": 0.025,
                "minTotal": 30,
            },
            {
                "ts": "2026-10-20T21:25:13",
                "profile": "p",
                "sampleSize": 47,
                "repliedCount": 2,
                "overallRate": 2 / 47,
                "minTotal": 30,
            },
        ],
    )
    monkeypatch.setattr(mod, "notify", lambda t, m: None)
    mod._cmd_eval(argparse.Namespace(trend=True), paths)
    first = capsys.readouterr().out
    assert "趋势快照已追加" in first, f"夹具没先把本场快照写进趋势：{first}"
    trend_lines = paths["trend"].read_text(encoding="utf-8").splitlines()
    assert len(trend_lines) == 3, f"本场快照没入账：{trend_lines}"
    recorded = json.loads(trend_lines[-1])
    assert (recorded["sampleSize"], recorded["repliedCount"]) == (50, 4), recorded

    # 同一份数据再跑一次：咽喉幂等跳过（趋势文件不涨），但历史里那条是真的上一条
    mod._cmd_eval(argparse.Namespace(trend=True), paths)
    second = capsys.readouterr().out
    assert "幂等跳过" in second, f"第二次没走到 duplicate 分支：{second}"
    assert len(paths["trend"].read_text(encoding="utf-8").splitlines()) == 3, "duplicate 那次把趋势写长了"
    assert _trend_table_row(second, "样本") == ["样本", "40", "50", "50", "+0", "+10"], second
    assert _trend_table_row(second, "回复") == ["回复", "1", "4", "4", "+0", "+3"], second
    assert _trend_table_row(second, "回复率") == ["回复率", "2.5%", "8.0%", "8.0%", "+0.0pp", "+5.5pp"], second
    assert "baseline 记录于本条" not in second, f"读数被剔成冷启动：{second}"

    # 同内容**且同 ts**（显式 `now` 取历史末条那条的 ts）：按 ts 剔除的兜底在这里必然误伤。
    # 分母那一格必须与上面 CLI 那次写进去的**同一个读数**，否则这条直接调用就不再是「同内容」——
    # #63 起快照带 `denominator`，而它是调用方从自己刚读的那份报告算出来的（#75 条目 B 同一条纪律）。
    _, snap3, status3, hist3 = mod.persist_eval_output(
        mod.eval_greetings(report, greetings, []),
        evaluation_path=paths["evaluation"],
        trend_path=paths["trend"],
        pending_path=paths["pending"],
        profile="p",
        now=recorded["ts"],
        denominator=fshape.denominator(report),
    )
    assert status3 == mod.TREND_DUPLICATE, status3
    assert hist3[-1]["ts"] == snap3["ts"] and hist3[-1]["sampleSize"] == snap3["sampleSize"], hist3
    table3 = mod.render_trend_table(snap3, hist3)
    assert _trend_table_row(table3, "样本") == ["样本", "40", "50", "50", "+0", "+10"], table3
    assert "baseline 记录于本条" not in table3, table3
