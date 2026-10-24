#!/usr/bin/env python3
"""_edd 的行为测试（#37 EDD 持续化）：趋势快照 JSONL、delta 对比、趋势 Markdown、批次级审阅提示。

拍板口径（issue #37 评论 2026-09-14）：
- sufficient 与观测性彻底解耦：趋势任何阶段全量输出，冷启动记 baseline；
- 批次级提示 = 样本较上次记录新增 ≥10，或距上次评估 >3 天且收到新回复。
"""

import importlib.util
import json
import re
from datetime import datetime
from pathlib import Path

_spec = importlib.util.spec_from_file_location("_edd", Path(__file__).resolve().parent / "_edd.py")
assert _spec is not None and _spec.loader is not None, "importlib spec 加载失败（模块文件缺失？）"
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_edd := mod)


def _evaluation(sample=32, replied=1, min_total=30, by_addressee=None, by_pattern=None):
    """构造 eval_greetings 输出形态的最小评估报告。"""
    return {
        "minTotal": min_total,
        "sufficient": sample >= min_total,
        "sampleSize": sample,
        "replied": replied,
        "overallRate": (replied / sample) if sample else None,
        "groups": {
            "by_addressee": by_addressee or {"hr": {"sample": sample, "replied": replied, "replyRate": None}},
            "by_pattern": by_pattern or {},
        },
    }


def test_trend_snapshot_fields():
    """快照携带时序字段 + 分组回复率；组内样本不足 replyRate=None 原样保留。"""
    ev = _evaluation(sample=32, replied=1, by_addressee={"hr": {"sample": 23, "replied": 1, "replyRate": 1 / 23}})
    snap = _edd.trend_snapshot(ev, profile="default", now=datetime(2026, 9, 17, 12, 0, 0))
    assert snap["ts"] == "2026-09-17T12:00:00"
    assert snap["profile"] == "default"
    assert snap["sampleSize"] == 32
    assert snap["repliedCount"] == 1
    assert snap["overallRate"] == 1 / 32
    assert snap["by_addressee"]["hr"] == 1 / 23
    assert snap["minTotal"] == 30


# --- JSONL 追加/加载 ---


def test_append_and_load_roundtrip(tmp_path):
    p = tmp_path / "eval_trend.jsonl"
    s1 = _edd.trend_snapshot(_evaluation(sample=10, replied=0), profile="p", now=datetime(2026, 9, 16))
    s2 = _edd.trend_snapshot(_evaluation(sample=20, replied=1), profile="p", now=datetime(2026, 9, 17))
    _edd.append_trend(p, s1)
    _edd.append_trend(p, s2)
    loaded = _edd.load_trend(p)
    assert [e["sampleSize"] for e in loaded] == [10, 20]


def test_load_trend_skips_bad_lines(tmp_path):
    p = tmp_path / "eval_trend.jsonl"
    p.write_text(
        "not json\n" + json.dumps({"ts": "2026-09-16T08:00:00", "sampleSize": 5}) + "\n\n",
        encoding="utf-8",
    )
    loaded = _edd.load_trend(p)
    assert len(loaded) == 1 and loaded[0]["sampleSize"] == 5


def test_load_trend_missing_file(tmp_path):
    assert _edd.load_trend(tmp_path / "nope.jsonl") == []


def test_append_dedupes_identical_content(tmp_path):
    """除 ts 外内容与最新一条相同则不追加——重复会话收尾不刷屏、不稀释增量判定。"""
    p = tmp_path / "eval_trend.jsonl"
    snap = _edd.trend_snapshot(_evaluation(sample=10, replied=0), profile="p", now=datetime(2026, 9, 16))
    assert _edd.append_trend(p, snap) == _edd.TREND_APPENDED
    again = _edd.trend_snapshot(_evaluation(sample=10, replied=0), profile="p", now=datetime(2026, 9, 17))
    assert _edd.append_trend(p, again) == _edd.TREND_DUPLICATE
    assert len(_edd.load_trend(p)) == 1


def test_append_dedupes_ignores_profile_change(tmp_path):
    """同内容不同 profile 是不同快照，仍追加。"""
    p = tmp_path / "eval_trend.jsonl"
    _edd.append_trend(p, _edd.trend_snapshot(_evaluation(sample=10), profile="a", now=datetime(2026, 9, 16)))
    _edd.append_trend(p, _edd.trend_snapshot(_evaluation(sample=10), profile="b", now=datetime(2026, 9, 16)))
    assert len(_edd.load_trend(p)) == 2


# --- #59 防线 2 的咽喉：假零快照在 append_trend 处拦下（会话收尾与 eval --trend 共用）---


def _seed_sizes(path, sizes):
    """按 sampleSize 列表预写趋势历史（一行一条，形态够判据读数即可）。"""
    path.write_text(
        "".join(
            json.dumps({"ts": f"2026-09-{10 + i:02d}T08:00:00", "profile": "p", "sampleSize": s, "repliedCount": 1})
            + "\n"
            for i, s in enumerate(sizes)
        ),
        encoding="utf-8",
    )
    return path


def _zero_snapshot():
    return _edd.trend_snapshot(_evaluation(sample=0, replied=0), profile="p", now=datetime(2026, 9, 30, 0, 10, 19))


def test_false_zero_snapshot_is_pure_predicate():
    """判据本体（无副作用，供咽喉与会话侧 reason 共用一份）：0 样本 + 历史存在非 0 点 ⇒ True。"""
    zero, nonzero = {"sampleSize": 0}, {"sampleSize": 51}
    assert _edd.false_zero_snapshot([{"sampleSize": 50}], zero) is True
    assert _edd.false_zero_snapshot([], zero) is False, "历史为空 = 冷启动首点，照常追加"
    assert _edd.false_zero_snapshot([{"sampleSize": 0}], zero) is False, "历史全 0 ⇒ 无非 0 点，不误拦"
    assert _edd.false_zero_snapshot([{"sampleSize": 50}, {"sampleSize": 0}], zero) is True, "非 0 点在任意位置都算"
    assert _edd.false_zero_snapshot([{"sampleSize": 50}], nonzero) is False, "非 0 样本不参与假零判定"
    assert _edd.false_zero_snapshot([{"sampleSize": 50}], {"sampleSize": None}) is False, "缺字段不判假零"
    assert _edd.false_zero_snapshot([{"profile": "p"}], zero) is False, "历史条目缺 sampleSize 不算非 0 点证据"


def test_append_trend_blocks_zero_sample_after_nonzero_history(tmp_path):
    """咽喉：0 样本而历史已有非 0 点 ⇒ 不追加、返回 `false_zero` 状态（#59 验收 2 的本体；
    R4-5 起返回值就是调用方出文案的唯一依据，bool 只能说「没写」，说不出**为什么**没写）。"""
    p = _seed_sizes(tmp_path / "eval_trend.jsonl", [50])
    assert _edd.append_trend(p, _zero_snapshot()) == _edd.TREND_FALSE_ZERO
    assert [e["sampleSize"] for e in _edd.load_trend(p)] == [50]


def test_append_trend_blocks_zero_sample_after_zero_point(tmp_path):
    """0→0 链堵得住：历史 [50, 0]（最新点是 0）时本场 0 样本仍被拒——判据取「存在非 0 点」，
    按「最新点非 0」判则第一个假零落盘后防线就永久失效。"""
    p = _seed_sizes(tmp_path / "eval_trend.jsonl", [50, 0])
    assert _edd.append_trend(p, _zero_snapshot()) == _edd.TREND_FALSE_ZERO
    assert [e["sampleSize"] for e in _edd.load_trend(p)] == [50, 0]


def test_append_trend_allows_zero_sample_on_empty_history(tmp_path):
    """冷启动：历史为空 ⇒ 0 样本首点照常追加（baseline 记录是既有语义）。"""
    p = tmp_path / "eval_trend.jsonl"
    assert _edd.append_trend(p, _zero_snapshot()) == _edd.TREND_APPENDED
    assert [e["sampleSize"] for e in _edd.load_trend(p)] == [0]


def test_append_trend_allows_zero_sample_when_history_all_zero(tmp_path):
    """历史里没有任何非 0 点 ⇒ 不判假零（防线只拦「有过真值却突然归零」的形态）。"""
    p = _seed_sizes(tmp_path / "eval_trend.jsonl", [0, 0])
    assert _edd.append_trend(p, _zero_snapshot()) == _edd.TREND_APPENDED
    assert len(_edd.load_trend(p)) == 3


def test_append_trend_allows_nonzero_after_zero_history(tmp_path):
    """非 0 样本照常追加，哪怕上一条是 0——防线不拦恢复的真值读数。"""
    p = _seed_sizes(tmp_path / "eval_trend.jsonl", [0])
    snap = _edd.trend_snapshot(_evaluation(sample=59, replied=2), profile="p", now=datetime(2026, 9, 30, 0, 14, 12))
    assert _edd.append_trend(p, snap) == _edd.TREND_APPENDED
    assert [e["sampleSize"] for e in _edd.load_trend(p)] == [0, 59]


def test_append_trend_orders_duplicate_before_false_zero(tmp_path):
    """R4-5 的实质：**判序只有咽喉这一份**。同一份 0 样本快照既「与最新一条内容相同」又「历史里
    存在非 0 点」时，咽喉返回 `duplicate`（幂等检查在前）。旧形态下调用方各自 `false_zero_snapshot`
    重判一遍，报出来的是 `false_zero`——与咽喉实际走的那条分支相反，文案因此会说谎。"""
    p = _seed_sizes(tmp_path / "eval_trend.jsonl", [50])
    zero = _zero_snapshot()
    p.write_text(
        p.read_text(encoding="utf-8") + json.dumps({k: v for k, v in zero.items() if k != "ts"}) + "\n",
        encoding="utf-8",
    )
    assert _edd.false_zero_snapshot(_edd.load_trend(p), zero) is True, "夹具须同时满足假零条件（否则本条没在测判序）"
    assert _edd.append_trend(p, zero) == _edd.TREND_DUPLICATE
    assert [e["sampleSize"] for e in _edd.load_trend(p)] == [50, 0], "两条防线都不写，历史须原样"


# --- delta 对比 ---


def test_trend_deltas_with_previous_and_baseline():
    baseline = {"ts": "2026-09-10T08:00:00", "sampleSize": 10, "repliedCount": 0, "overallRate": 0.0}
    previous = {"ts": "2026-09-16T08:00:00", "sampleSize": 25, "repliedCount": 1, "overallRate": 0.04}
    current = {"ts": "2026-09-17T08:00:00", "sampleSize": 32, "repliedCount": 2, "overallRate": 0.0625}
    d = _edd.trend_deltas(current, previous=previous, baseline=baseline)
    assert d["vs_previous"] == {"sampleSize": 7, "repliedCount": 1, "overallRate": 0.0625 - 0.04}
    assert d["vs_baseline"] == {"sampleSize": 22, "repliedCount": 2, "overallRate": 0.0625 - 0.0}


def test_trend_deltas_cold_start():
    d = _edd.trend_deltas({"sampleSize": 5, "repliedCount": 0, "overallRate": None})
    assert d["vs_previous"] is None and d["vs_baseline"] is None


# --- 趋势 Markdown（sufficient=false 也输出，#37 拍板 Q7）---


def test_render_trend_md_cold_start_under_sample():
    """冷启动 + 样本不足：仍输出块，含样本进度与距达标缺口。"""
    current = _edd.trend_snapshot(_evaluation(sample=22, replied=1), profile="p", now=datetime(2026, 9, 17))
    md = _edd.render_trend_md(current, trend=[])
    assert md.startswith("## EDD 趋势跟踪")
    assert "22/30" in md
    assert "还需 8 条配对" in md  # 距离有效推断还需 8 条（精确断言，review J4）
    assert "baseline" in md


def test_render_trend_md_sufficient_no_warning_gap():
    current = _edd.trend_snapshot(_evaluation(sample=32, replied=1), profile="p", now=datetime(2026, 9, 17))
    md = _edd.render_trend_md(current, trend=[])
    assert "## EDD 趋势跟踪" in md
    assert "32/30" in md


def test_render_trend_md_with_history_deltas():
    trend = [
        {"ts": "2026-09-10T08:00:00", "profile": "p", "sampleSize": 10, "repliedCount": 0, "overallRate": 0.0},
        {"ts": "2026-09-16T08:00:00", "profile": "p", "sampleSize": 25, "repliedCount": 1, "overallRate": 0.04},
    ]
    current = _edd.trend_snapshot(_evaluation(sample=32, replied=2), profile="p", now=datetime(2026, 9, 17))
    md = _edd.render_trend_md(current, trend=trend)
    assert "2026-09-16" in md  # previous
    assert "2026-09-10" in md  # baseline
    assert "+7" in md  # 样本增量 vs previous


# --- 批次级审阅提示判定（拍板 Q1）---


def test_review_due_on_sample_jump():
    """样本较上次记录新增 ≥10 → due。"""
    trend = [{"ts": "2026-09-16T08:00:00", "profile": "p", "sampleSize": 20, "repliedCount": 0, "overallRate": None}]
    current = _edd.trend_snapshot(_evaluation(sample=30, replied=0), profile="p", now=datetime(2026, 9, 17))
    verdict = _edd.review_due(current, trend, now=datetime(2026, 9, 17, 12, 0, 0))
    assert verdict["due"] and any("样本" in r for r in verdict["reasons"])


def test_review_due_on_stale_with_new_reply():
    """距上次评估 >3 天且收到新回复 → due。"""
    trend = [{"ts": "2026-09-10T08:00:00", "profile": "p", "sampleSize": 30, "repliedCount": 1, "overallRate": None}]
    current = _edd.trend_snapshot(_evaluation(sample=32, replied=2), profile="p", now=datetime(2026, 9, 17))
    verdict = _edd.review_due(current, trend, now=datetime(2026, 9, 17, 12, 0, 0))
    assert verdict["due"] and any("3 天" in r for r in verdict["reasons"])


def test_review_not_due_within_window():
    trend = [{"ts": "2026-09-16T08:00:00", "profile": "p", "sampleSize": 30, "repliedCount": 1, "overallRate": None}]
    current = _edd.trend_snapshot(_evaluation(sample=32, replied=1), profile="p", now=datetime(2026, 9, 17))
    verdict = _edd.review_due(current, trend, now=datetime(2026, 9, 17, 12, 0, 0))
    assert not verdict["due"]


def test_review_not_due_stale_without_new_reply():
    """超 3 天但无新回复（repliedCount 不增）→ 不 due。"""
    trend = [{"ts": "2026-09-10T08:00:00", "profile": "p", "sampleSize": 30, "repliedCount": 1, "overallRate": None}]
    current = _edd.trend_snapshot(_evaluation(sample=30, replied=1), profile="p", now=datetime(2026, 9, 17))
    verdict = _edd.review_due(current, trend, now=datetime(2026, 9, 17, 12, 0, 0))
    assert not verdict["due"]


def test_review_not_due_cold_start():
    """冷启动（无历史）不提示——首轮先记 baseline。"""
    current = _edd.trend_snapshot(_evaluation(sample=30, replied=1), profile="p", now=datetime(2026, 9, 17))
    assert not _edd.review_due(current, [], now=datetime(2026, 9, 17, 12, 0, 0))["due"]


# --- eval --trend 终端对比表格（拍板 Q2：baseline/previous/current 及 delta）---


def test_render_trend_table_shows_three_columns_with_deltas():
    trend = [
        {"ts": "2026-09-10T08:00:00", "profile": "p", "sampleSize": 10, "repliedCount": 0, "overallRate": 0.0},
        {"ts": "2026-09-16T08:00:00", "profile": "p", "sampleSize": 25, "repliedCount": 1, "overallRate": 0.04},
    ]
    current = _edd.trend_snapshot(_evaluation(sample=32, replied=2), profile="p", now=datetime(2026, 9, 17))
    table = _edd.render_trend_table(current, trend)
    for needle in ("baseline", "2026-09-10", "previous", "2026-09-16", "current", "+7", "+22"):
        assert needle in table, f"对比表缺少 {needle!r}"
    assert re.search(r"\+\d+\.\d+pp", table), "回复率 delta 须以 pp 百分点呈现"


def test_render_trend_table_cold_start():
    current = _edd.trend_snapshot(_evaluation(sample=22, replied=1), profile="p", now=datetime(2026, 9, 17))
    table = _edd.render_trend_table(current, trend=[])
    assert "baseline" in table and "22" in table
    assert "Δ" not in table, "无历史时无 delta 列"


# --- #45 密度阈值提示制（拍板 2026-09-24）：任一 eval 分组组内回复 ≥5 主动叫人审范式 ---


def test_density_due_triggers_at_threshold():
    """组内回复恰达 5 → due，触发组带样本量直显。"""
    ev = _evaluation(by_addressee={"hr": {"sample": 23, "replied": 5, "replyRate": 5 / 23}})
    verdict = _edd.density_due(ev)
    assert verdict["due"] is True
    assert verdict["threshold"] == _edd.DENSITY_GROUP_REPLIES
    assert verdict["groups"] == ["by_addressee:hr（回复 5/23）"]


def test_density_due_silent_below_threshold():
    """低密度静默（拍板 3）：不足阈值零触发——2 条回复不叫人；空评估同理。"""
    ev = _evaluation(by_addressee={"hr": {"sample": 23, "replied": 2, "replyRate": None}})
    assert _edd.density_due(ev) == {"due": False, "threshold": 5, "groups": []}
    assert _edd.density_due({})["due"] is False
    assert _edd.density_due(None)["due"] is False


def test_density_due_spans_dimensions_and_skips_bad_shape():
    """多维命中都列出；replied 缺失/坏形态/布尔真值的组跳过不猜（提示层不猜数据）。"""
    ev = {
        "groups": {
            "by_addressee": {"hr": {"sample": 10, "replied": 6, "replyRate": 0.6}},
            "by_pattern": {
                "question_cta": {"sample": 8, "replied": 5, "replyRate": 5 / 8},
                "no_count": {"sample": 9, "replyRate": None},
                "bad_type": {"sample": 7, "replied": "5", "replyRate": None},
                "bool_true": {"sample": 5, "replied": True, "replyRate": None},
            },
        }
    }
    verdict = _edd.density_due(ev)
    assert verdict["due"] is True
    assert verdict["groups"] == ["by_addressee:hr（回复 6/10）", "by_pattern:question_cta（回复 5/8）"]


def test_density_due_threshold_overridable():
    """阈值可传参覆盖（常量只作默认值）——未来与 #27 项 4 会诊时不必改签名。"""
    ev = _evaluation(by_addressee={"hr": {"sample": 23, "replied": 4, "replyRate": 4 / 23}})
    assert _edd.density_due(ev, threshold=4)["due"] is True
    assert _edd.density_due(ev)["due"] is False


def test_load_pending_candidates_missing_and_bad(tmp_path):
    """文件缺失/坏 JSON → 空列表（提示层不因队列损坏而崩）。"""
    assert _edd.load_pending_candidates(tmp_path / "nope.json") == []
    p = tmp_path / "pending.json"
    p.write_text("not json", encoding="utf-8")
    assert _edd.load_pending_candidates(p) == []


def test_load_pending_candidates_filters_non_dict(tmp_path):
    p = tmp_path / "pending.json"
    p.write_text(
        json.dumps(
            {
                "updated_at": "2026-09-24T10:00:00",
                "candidates": [
                    {"key": "by_addressee:hr", "kind": "winning", "sample": 23, "replyRate": 0.217},
                    "junk",
                ],
            }
        ),
        encoding="utf-8",
    )
    out = _edd.load_pending_candidates(p)
    assert len(out) == 1 and out[0]["key"] == "by_addressee:hr"


def test_render_density_prompt_lists_candidates_and_manual_gate():
    """提示块：触发组 + 候选清单 + promote-winners 人工 gate 指引（拍板 4：不自动晋级）。"""
    verdict = {"due": True, "threshold": 5, "groups": ["by_addressee:hr（回复 5/23）"]}
    cands = [{"key": "by_addressee:hr", "kind": "winning", "sample": 23, "replyRate": 0.217}]
    text = _edd.render_density_prompt(verdict, cands)
    assert "是不是要更新招呼语范式" in text
    assert "by_addressee:hr" in text and "22%" in text
    assert "promote-winners --group <key> --yes" in text
    assert "#27 项 4" in text, "联动提示（triage #45）：触发时与项 4 分组评估同会话处理"
    empty = _edd.render_density_prompt(verdict, [])
    assert "暂无" in empty


# --- #63 切片 B：样本数下降不静默（appended_decline）+ 快照分母（验收 4）---


def _snap(sample, replied=2, denominator=None, day=1):
    ev = _evaluation(sample=sample, replied=replied)
    return _edd.trend_snapshot(ev, profile="p", now=datetime(2026, 10, day, 8, 0, 0), denominator=denominator)


def test_append_trend_flags_declining_sample_size_but_still_writes(tmp_path):
    """#63 验收 4：样本数低于趋势最新一条 ⇒ **已写盘**，状态点名下降（不静默追加）。
    判序仍只有一份、仍在咽喉里：duplicate → false_zero → 写盘 →（下降则 appended_decline）。
    把 decline 做成「拒写」就是把报告冻在旧读数上——正是用户否决原票面修法的那条理由。"""
    p = _seed_sizes(tmp_path / "eval_trend.jsonl", [59])
    assert _edd.append_trend(p, _snap(52)) == _edd.TREND_APPENDED_DECLINE
    assert [e["sampleSize"] for e in _edd.load_trend(p)] == [59, 52], "decline 是额外状态，不是拒绝"


def test_append_trend_does_not_call_a_non_decline_decline(tmp_path):
    """不宣布下降的三种：样本增加、样本持平（`<` 不是 `<=`）、历史为空（冷启动首点没有可比对象）。
    「无从比较」念成「样本下降」是新造的谎，所以这三条都要钉住。"""
    p = _seed_sizes(tmp_path / "eval_trend.jsonl", [40])
    assert _edd.append_trend(p, _snap(52)) == _edd.TREND_APPENDED
    q = _seed_sizes(tmp_path / "eval_trend_q.jsonl", [52])
    assert _edd.append_trend(q, _snap(52, replied=1, day=2)) == _edd.TREND_APPENDED
    assert _edd.append_trend(tmp_path / "fresh.jsonl", _snap(52)) == _edd.TREND_APPENDED


def test_append_trend_needs_int_sample_to_claim_decline(tmp_path):
    """历史最新一条的 sampleSize 不是整数（缺键 / 串 / 布尔）⇒ 不判下降，照常 appended：
    读数不是 int 就不参与比较（排除 `True` 的形态同 `_inbound_watermark._as_int_ts`）。"""
    for index, bad in enumerate(({}, {"sampleSize": "59"}, {"sampleSize": True}, {"sampleSize": None})):
        p = tmp_path / f"bad_{index}.jsonl"
        p.write_text(json.dumps({"ts": "2026-09-29T08:00:00", "profile": "p", **bad}) + "\n", encoding="utf-8")
        assert _edd.sample_declined(_snap(52), _edd.load_trend(p)) is False, bad
        assert _edd.append_trend(p, _snap(52)) == _edd.TREND_APPENDED, bad


def test_is_appended_is_the_only_written_question():
    """「写没写盘」只有一个问法（#63 切片 B）：两个 appended 状态都算写了。
    调用方写 `== TREND_APPENDED` 会把 decline 那一场读成「没写」，给它编一句未追加的谎。"""
    assert _edd.is_appended(_edd.TREND_APPENDED) is True
    assert _edd.is_appended(_edd.TREND_APPENDED_DECLINE) is True
    for status in (_edd.TREND_DUPLICATE, _edd.TREND_FALSE_ZERO, None, "", "appended_loudly"):
        assert _edd.is_appended(status) is False, status


def test_trend_snapshot_denominator_key_is_always_present():
    """恒在键 `denominator`：没接到分母就是 `None`，**不补 0**
    （0 是一条读数，None 才是「这一格这次没有读数」，口径同 #61/#65/切片 A）。"""
    plain = _edd.trend_snapshot(_evaluation(sample=52), profile="p")
    assert plain["denominator"] is None and "denominator" in plain
    given = {"conversations": 76, "shape": "scattered_lost"}
    assert _edd.trend_snapshot(_evaluation(sample=52), profile="p", denominator=given)["denominator"] == given


def test_same_sample_different_denominator_is_not_duplicate(tmp_path):
    """`_content_key` 是「除 ts 外全内容」，加了分母之后「同样本、不同分母」**不再算 duplicate**——
    这是本切片的**目的**不是副作用（票面验收 4：样本没变但会话清单掉了 24 条，趋势里要看得见）。
    后续 session 若把它当回归改回「只看样本数」，这条会红。"""
    p = tmp_path / "eval_trend.jsonl"
    first = _snap(52, denominator={"conversations": 100, "shape": "stable"}, day=1)
    second = _snap(52, denominator={"conversations": 76, "shape": "scattered_lost"}, day=2)
    assert _edd.append_trend(p, first) == _edd.TREND_APPENDED
    assert _edd.append_trend(p, second) == _edd.TREND_APPENDED
    assert [e["denominator"]["conversations"] for e in _edd.load_trend(p)] == [100, 76]
    # 负对照：分母也一样时才是幂等跳过（判据没被改成「永远追加」）
    third = _snap(52, denominator={"conversations": 76, "shape": "scattered_lost"}, day=3)
    assert _edd.append_trend(p, third) == _edd.TREND_DUPLICATE


def test_history_without_denominator_appends_once_not_as_broken_dedupe(tmp_path):
    """历史条目**没有** `denominator` 键 ⇒ 同一份内容的快照必然再追加一次。
    这是键集合变化的结果，不是 duplicate 判据坏了（否则历史里每一条都要被误判成重复）。"""
    snap = _snap(52, day=2)
    legacy = {k: v for k, v in snap.items() if k != "denominator"}
    legacy["ts"] = "2026-10-01T08:00:00"
    p = tmp_path / "eval_trend.jsonl"
    p.write_text(json.dumps(legacy, ensure_ascii=False) + "\n", encoding="utf-8")
    assert _edd.append_trend(p, snap) == _edd.TREND_APPENDED
    assert len(_edd.load_trend(p)) == 2
