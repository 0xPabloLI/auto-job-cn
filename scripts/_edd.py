#!/usr/bin/env python3
"""EDD 趋势观测（#37 EDD 持续化）：eval_trend.jsonl 时序快照 + delta 对比 + 趋势渲染 + 审阅提示
（批次级 review_due + #45 密度阈值提示制 density_due——数据达标主动叫人审范式，决策权在用户）。

职责边界（#37 拍板 2026-09-14）：
- 充分性判定（sufficient）与进度观测性（observability）彻底解耦——趋势任何阶段全量输出，
  样本不足也如实展示累积进度（消除「离达标还差多远」的黑盒感），冷启动记录 baseline。
- 纯本地计算（<0.5s），不依赖浏览器/LLM；live_apply 会话收尾与 gen_greetings eval 都追加快照。
- 本模块不 import gen_greetings（其 eval 命令与 live_apply 反向依赖本模块），评估结构按
  eval_greetings 输出形态的鸭子类型消费（minTotal/sampleSize/replied/overallRate/groups）。
"""

import json
from datetime import datetime
from pathlib import Path

# 批次级审阅提示阈值（拍板 Q1）：样本较上次记录新增 ≥10 条，或距上次评估 >3 天且有新回复
REVIEW_SAMPLE_STEP = 10
REVIEW_STALE_DAYS = 3

# --- `append_trend` 的返回状态（#59 修复轮 R4-5：判序只有一份，长在咽喉里）---
# 旧版返回 bool，两个写趋势的调用方要各自再读一遍趋势历史才能出可读文案，判序因此在三处各存
# 一份；当「幂等跳过」与「假零」同时成立时 eval 侧报成假零，与咽喉内部判序相反。
# 现在调用方只读状态字符串出 reason；需要 bool 语义时写 `status == TREND_APPENDED`。
TREND_APPENDED = "appended"  # 已落盘
TREND_DUPLICATE = "duplicate"  # 与最新一条内容相同（除 ts）——幂等跳过（判序在前）
TREND_FALSE_ZERO = "false_zero"  # 0 样本而历史已有非 0 点——#59 防线 2 拦下


def stamp(now):
    """时间戳统一（isoformat 秒级）：测试传字符串直用，运行时取当前。
    gen_greetings 以 `from _edd import stamp as _stamp` 复用（review J1 去重）。"""
    if isinstance(now, str):
        return now
    return (now or datetime.now()).isoformat(timespec="seconds")


def trend_snapshot(evaluation, profile, now=None):
    """eval_greetings 输出 → 一条趋势快照（时序字段 + 分组回复率）。
    组内样本不足的组 replyRate=None 原样保留——观测层不替评估层补结论。"""
    groups = evaluation.get("groups", {})

    def rates(dim):
        return {k: v.get("replyRate") for k, v in (groups.get(dim) or {}).items()}

    return {
        "ts": stamp(now),
        "profile": profile,
        "sampleSize": evaluation.get("sampleSize", 0),
        "repliedCount": evaluation.get("replied", 0),
        "overallRate": evaluation.get("overallRate"),
        "minTotal": evaluation.get("minTotal"),
        "by_addressee": rates("by_addressee"),
        "by_pattern": rates("by_pattern"),
    }


def load_trend(path):
    """eval_trend.jsonl → 快照列表；坏行/空行跳过（追加型日志的坏行不清场）。"""
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def _content_key(snapshot):
    """去重键：除 ts 外的完整内容——同内容快照不重复入时序（重复会话收尾不刷屏）。"""
    return json.dumps({k: v for k, v in snapshot.items() if k != "ts"}, sort_keys=True, ensure_ascii=False)


def false_zero_snapshot(trend, snapshot):
    """假零快照判据（#59 防线 2 的**单一咽喉**，纯函数；**判定的唯一调用方是 `append_trend`**）：
    快照 sampleSize == 0 而趋势历史里已存在非 0 点 ⇒ True（这场会话的读数疑似残缺采集，不该进时序）。
    - 历史为空 = 冷启动首点 ⇒ False，照常追加（baseline 记录是既有语义）；
    - 判据取「存在非 0 点」而不是「最新点非 0」⇒ 一旦历史里有过一个真值，后续 0→0 链也全程
      被拦（按「最新点」判则第一个假零落盘后防线永久失效）；
    - 历史条目 sampleSize 为 0/缺失/None 视作「非 0 点证据不足」，其余真值（含坏形态字符串）
      视作非 0——方向是保守拦零，宁可这一场没快照。
    调用方**不要再自己判一遍**：#59 修复轮 R4-5 把判序收进 `append_trend` 的返回值，两处
    写趋势的入口只读状态、不重读趋势历史（旧形态下「幂等跳过」与「假零」同时成立时，
    调用方自判会报成假零，与咽喉内部判序相反）。"""
    if (snapshot or {}).get("sampleSize") != 0:
        return False
    return any((item or {}).get("sampleSize") for item in (trend or []))


def append_trend(path, snapshot):
    """追加一条快照，返回**状态字符串**（`TREND_APPENDED` / `TREND_DUPLICATE` / `TREND_FALSE_ZERO`；
    #59 修复轮 R4-5：旧版返回 bool，逼着两个调用方各自再读一遍趋势历史才能出可读文案，
    判序因此在三处各存一份。现在判序只有这一份，调用方按状态出 reason/措辞）。
    两种情况不写，且**判序就是返回值的优先级**：
    ① `TREND_DUPLICATE`——与最新一条内容相同（除 ts）：幂等跳过，重复会话收尾不刷屏；
    ② `TREND_FALSE_ZERO`：`false_zero_snapshot` 判为假零（0 样本而历史已有非 0 点）。
    咽喉放在这里，所有写趋势的入口（会话收尾 `record_session_snapshot` 与 eval --trend 的
    `persist_eval_output`）共用同一份判据，不再有第二处各写一份的余地。
    需要 bool 语义的调用方写 `status == TREND_APPENDED`。"""
    p = Path(path)
    trend = load_trend(p)
    if trend and _content_key(trend[-1]) == _content_key(snapshot):
        return TREND_DUPLICATE
    if false_zero_snapshot(trend, snapshot):
        return TREND_FALSE_ZERO
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
    return TREND_APPENDED


def _delta(current, ref):
    """三个核心指标的变化量；两侧任一缺失（None）则该指标 delta=None，不猜 0。"""
    if ref is None:
        return None
    out = {}
    for key in ("sampleSize", "repliedCount", "overallRate"):
        c, r = current.get(key), ref.get(key)
        out[key] = (c - r) if (c is not None and r is not None) else None
    return out


def trend_deltas(current, previous=None, baseline=None):
    """current 相对 previous（最新一条）与 baseline（首条）的变化量；冷启动两者皆 None。"""
    return {"vs_previous": _delta(current, previous), "vs_baseline": _delta(current, baseline)}


def _fmt_rate(rate):
    return f"{rate:.1%}" if rate is not None else "N/A"


def _fmt_pp(diff):
    """回复率变化量（百分点）；None 侧缺失时不输出数字。"""
    return f"{diff * 100:+.1f}pp" if diff is not None else "N/A"


_METRIC_ROWS = (("样本", "sampleSize", "{:+d}"), ("回复", "repliedCount", "{:+d}"))


def render_trend_table(current, trend):
    """eval --trend 的终端对比表格（拍板 Q2）：baseline / previous / current 及变化幅度。
    冷启动只有 current 一列（baseline 记录于本条）。"""
    baseline = trend[0] if trend else None
    previous = trend[-1] if trend else None
    ts_col = lambda entry: f"({entry.get('ts', '')[:10]})" if entry else ""  # noqa: E731
    headers = ["指标", f"baseline{ts_col(baseline)}", f"previous{ts_col(previous)}", "current"]
    if previous:
        headers += ["Δprev", "Δbase"]
    rows = []
    for label, key, fmt in _METRIC_ROWS:
        row = [
            label,
            str(baseline.get(key)) if baseline else "—",
            str(previous.get(key)) if previous else "—",
            str(current.get(key)),
        ]
        if previous:
            row += [fmt.format(current[key] - previous[key]), fmt.format(current[key] - baseline[key])]
        rows.append(row)
    c_rate, p_rate = current.get("overallRate"), (previous or {}).get("overallRate")
    b_rate = (baseline or {}).get("overallRate")
    row = ["回复率", _fmt_rate(b_rate), _fmt_rate(p_rate), _fmt_rate(c_rate)]
    if previous:
        row += [
            _fmt_pp(c_rate - p_rate) if (c_rate is not None and p_rate is not None) else "—",
            _fmt_pp(c_rate - b_rate) if (c_rate is not None and b_rate is not None) else "—",
        ]
    rows.append(row)
    widths = [max(len(r[i]) for r in rows + [headers]) for i in range(len(headers))]
    pad = lambda cells: "  ".join(c.ljust(w) for c, w in zip(cells, widths))  # noqa: E731
    lines = ["== EDD 趋势对比 ==", pad(headers)]
    lines += [pad(r) for r in rows]
    if baseline is None:
        lines.append("(baseline 记录于本条——下次评估起展示对比)")
    return "\n".join(lines)


def render_trend_md(current, trend):
    """趋势 → `## EDD 趋势跟踪` Markdown 块（投递会话汇报直显 / eval --trend 复用）。
    sufficient 与观测解耦：样本不足也输出，含距达标缺口（拍板 Q7 示例口径：
    「当前样本 22/30 (73.3%)，回复 1/22 (4.5%)，距离有效推断还需 8 条配对」）。"""
    lines = ["## EDD 趋势跟踪"]
    total, min_total = current.get("sampleSize", 0), current.get("minTotal")
    rate = current.get("overallRate")
    progress = f"样本 {total}/{min_total}" if min_total else f"样本 {total}"
    lines.append(f"- {progress} | 回复 {current.get('repliedCount', 0)}（{_fmt_rate(rate)}）")
    if min_total and total < min_total:
        lines.append(f"- 距离有效推断还需 {min_total - total} 条配对（门槛 {min_total}，机制照常累积）")
    previous = trend[-1] if trend else None
    baseline = trend[0] if trend else None
    deltas = trend_deltas(current, previous=previous, baseline=baseline)
    if previous is None:
        lines.append(f"- baseline 记录于本条（{current['ts']}）——后续轮次基于此计算增量")
        return "\n".join(lines) + "\n"
    vp = deltas["vs_previous"]
    lines.append(
        f"- vs 上次（{previous.get('ts')}）：样本 {vp['sampleSize']:+d} / "
        f"回复 {vp['repliedCount']:+d} / 回复率 {_fmt_pp(vp['overallRate'])}"
    )
    if baseline is not None and baseline is not previous:
        vb = deltas["vs_baseline"]
        lines.append(
            f"- vs baseline（{baseline.get('ts')}）：样本 {vb['sampleSize']:+d} / "
            f"回复 {vb['repliedCount']:+d} / 回复率 {_fmt_pp(vb['overallRate'])}"
        )
    return "\n".join(lines) + "\n"


def parse_ts(ts):
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def review_due(current, trend, now=None):
    """批次级审阅提示判定（拍板 Q1「决策级」）：样本较上次记录新增 ≥REVIEW_SAMPLE_STEP，
    或距上次评估 >REVIEW_STALE_DAYS 天且收到新回复。冷启动（无历史）不提示。
    返回 {"due": bool, "reasons": [...]}——reasons 供提示卡片直显。"""
    reasons = []
    if not trend:
        return {"due": False, "reasons": reasons}
    previous = trend[-1]
    step = current.get("sampleSize", 0) - previous.get("sampleSize", 0)
    if step >= REVIEW_SAMPLE_STEP:
        reasons.append(f"样本较上次记录新增 {step} 条（≥{REVIEW_SAMPLE_STEP}）")
    prev_ts = parse_ts(previous.get("ts"))
    now_dt = now if isinstance(now, datetime) else parse_ts(stamp(now))
    if prev_ts is not None and now_dt is not None:
        stale_days = (now_dt - prev_ts).total_seconds() / 86400
        new_reply = current.get("repliedCount", 0) > previous.get("repliedCount", 0)
        if stale_days > REVIEW_STALE_DAYS and new_reply:
            reasons.append(f"距上次评估超 {REVIEW_STALE_DAYS} 天（{stale_days:.1f} 天）且收到新回复")
    return {"due": bool(reasons), "reasons": reasons}


# --- #45 密度阈值提示制（拍板 2026-09-24）：数据密度达标时主动叫人审范式，决策权在用户 ---

# 密度阈值（triage 拍板）：任一 eval 分组组内回复 ≥5 即触发——与 #27 项 4 分组评估的数据
# 门槛同源（组内 ≥5 条回复才可作分组结论），提示触发时两项同会话处理。
DENSITY_GROUP_REPLIES = 5


def density_due(evaluation, threshold=DENSITY_GROUP_REPLIES):
    """密度提示判定：任一 eval 分组组内回复 ≥ threshold → 提示审范式（#45 提示制，
    promote-winners 的人工 gate 不动）。输入是 eval_greetings 输出形态
    （groups.{dim}.{key}.{replied, sample}）的鸭子类型，与 trend_snapshot 同源。
    返回 {"due": bool, "threshold": int, "groups": [...]}——groups 供提示块直显。
    replied 缺失/坏形态（非 int，含 bool）的组不参与判定——提示层不猜数据，
    坏数据由标注/台账修复链路兜。"""
    hit = []
    for dim, keys in ((evaluation or {}).get("groups") or {}).items():
        for key, stats in (keys or {}).items():
            rep = (stats or {}).get("replied")
            if isinstance(rep, bool) or not isinstance(rep, int):
                continue
            if rep >= threshold:
                sample = (stats or {}).get("sample")
                shown = f"/{sample}" if isinstance(sample, int) else ""
                hit.append(f"{dim}:{key}（回复 {rep}{shown}）")
    return {"due": bool(hit), "threshold": threshold, "groups": hit}


def load_pending_candidates(path):
    """pending_promotions.json → 候选列表；路径未提供（旧调用方/降级场景）、文件缺失、
    坏 JSON、坏形态一律返回 []——提示层不因队列损坏而崩（队列本体由 eval 命令的
    pending_upsert/pending_prune 维护，修复不归本函数）。"""
    if not path:
        return []
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    cands = data.get("candidates") if isinstance(data, dict) else None
    if not isinstance(cands, list):
        return []
    return [c for c in cands if isinstance(c, dict)]


def render_density_prompt(verdict, candidates):
    """密度提示块（#45 双通道的控制台形态；macOS 通知文案由调用方取 groups 另拼短句）：
    触发组 + 候选审阅建议清单 + 人工拍板指引。决策权在用户——本模块只「叫人」，
    不写任何 KB 文件。"""
    lines = [
        "💡 [范式审阅] 分组回复密度达标（" + "；".join(verdict["groups"]) + "）"
        "——根据 evaluation，是不是要更新招呼语范式？"
    ]
    if candidates:
        lines.append("   待审候选（pending_promotions，30 天未审自动过期）：")
        for c in candidates[:10]:
            rate = c.get("replyRate")
            rate_s = f"{rate:.0%}" if isinstance(rate, (int, float)) and not isinstance(rate, bool) else "N/A"
            lines.append(f"   - {c.get('key')} | {rate_s}（样本 {c.get('sample')}，{c.get('kind')}）")
    else:
        lines.append("   待审候选：暂无（下次 eval 产出候选会自动入队）")
    lines.append(
        "   人工拍板：python3 scripts/gen_greetings.py promote-winners --group <key> --yes"
        "（人工 gate 不变；明确推迟也可以，候选 30 天过期兜底）"
    )
    lines.append("   联动（#45 triage）：密度门槛与 #27 项 4 分组评估的数据门槛同源——本提示触发时两项同会话处理")
    return "\n".join(lines)
