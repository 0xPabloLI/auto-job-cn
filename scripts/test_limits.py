#!/usr/bin/env python3
"""_limits 的行为测试（#44 自 test_batch_apply_v2.py 平移，模块源换 _limits）：风控暂停、
可配置上限/限速、投递时间分布日志。死路径（apply_round/run_batches/compose_send_text）
的测试不迁移——那些函数随挂机盲投链路一并清理（#44）。"""

import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("_limits", Path(__file__).resolve().parent / "_limits.py")
assert _spec is not None and _spec.loader is not None, "importlib spec 加载失败（模块文件缺失？）"
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


# --- 风控码自动暂停（issue #5：37/38/1011 触发暂停+通知） ---


@pytest.mark.parametrize("code", [37, 38, 1011])
def test_risk_codes_pause(code):
    action, detail = mod.handle_code(code, "some message", {})
    assert action == "pause"
    assert detail


def test_non_risk_codes_unchanged():
    assert mod.handle_code(0, "", {}) == ("ok", "成功")
    assert mod.handle_code(35, "", {})[0] == "stop"
    assert mod.handle_code(36, "", {})[0] == "stop"
    assert mod.handle_code(1006, "", {})[0] == "wait"
    assert mod.handle_code(31, "", {})[0] == "stop"
    assert mod.handle_code(32, "", {})[0] == "stop"
    assert mod.handle_code(9999, "x", {})[0] == "skip"


# --- 可配置每日上限与静默时段 ---


def test_load_config_defaults():
    cfg = mod.load_config(env={})
    assert cfg["daily_limit"] == 20
    assert cfg["quiet_hours"] == (0, 7), "静默时段默认启用 0-7 点（issue #11）"


def test_load_config_env_override():
    cfg = mod.load_config(
        env={
            "APPLY_DAILY_LIMIT": "5",
            "APPLY_INTRA_MIN": "200",
            "APPLY_INTRA_MAX": "300",
        }
    )
    # #20 新增拟人参数后只断言覆盖项（其余键有各自默认值测试）
    assert cfg["daily_limit"] == 5
    assert cfg["intra_min"] == 200
    assert cfg["intra_max"] == 300
    assert cfg["quiet_hours"] == (0, 7)


def test_load_config_invalid_falls_back():
    cfg = mod.load_config(env={"APPLY_DAILY_LIMIT": "abc", "APPLY_SESSION_MAX": "0"})
    assert cfg["daily_limit"] == 20
    assert cfg["session_max"] == 4  # 非正数视为非法，回退默认


def test_load_config_swaps_reversed_ranges():
    cfg = mod.load_config(env={"APPLY_INTRA_MIN": "200", "APPLY_INTRA_MAX": "100"})
    assert (cfg["intra_min"], cfg["intra_max"]) == (100, 200)


# --- 平台每日沟通上限检测（greeting 提示 120） ---


def test_platform_daily_limit_detection():
    assert mod.is_platform_daily_limit("今天沟通人数已达120上限，请明日再沟通")
    assert not mod.is_platform_daily_limit("沟通成功")
    assert not mod.is_platform_daily_limit("120")


# --- 时段乘数与静默时段（issue #11 方案 A） ---


def test_hour_multiplier_table():
    assert mod.hour_multiplier(9) == 1.0
    assert mod.hour_multiplier(11) == 1.0
    assert mod.hour_multiplier(12) == 1.5, "午休放缓"
    assert mod.hour_multiplier(13) == 1.5
    assert mod.hour_multiplier(14) == 1.0
    assert mod.hour_multiplier(22) == 2.0, "晚间保守慢速"
    assert mod.hour_multiplier(3) == 2.0, "静默区间被改掉时夜间仍保守慢速"


def test_load_config_quiet_hours_off():
    assert mod.load_config(env={"APPLY_QUIET_HOURS": "off"})["quiet_hours"] is None


def test_load_config_quiet_hours_custom_wrap():
    assert mod.load_config(env={"APPLY_QUIET_HOURS": "23-6"})["quiet_hours"] == (23, 6)


def test_load_config_quiet_hours_invalid_falls_back(capsys):
    cfg = mod.load_config(env={"APPLY_QUIET_HOURS": "abc"})
    assert cfg["quiet_hours"] == (0, 7)
    assert "不是有效的静默时段" in capsys.readouterr().out


def test_load_config_quiet_hours_out_of_range_falls_back(capsys):
    cfg = mod.load_config(env={"APPLY_QUIET_HOURS": "2-25"})
    assert cfg["quiet_hours"] == (0, 7)
    assert "0-23" in capsys.readouterr().out


def test_is_quiet_hour_disabled():
    assert not mod.is_quiet_hour(3, None)


def test_is_quiet_hour_normal_range():
    q = (0, 7)
    assert mod.is_quiet_hour(0, q)
    assert mod.is_quiet_hour(6, q)
    assert not mod.is_quiet_hour(7, q), "区间为左闭右开，7 点恢复投递"


def test_is_quiet_hour_wrap_range():
    q = (23, 6)
    assert mod.is_quiet_hour(23, q)
    assert mod.is_quiet_hour(2, q)
    assert mod.is_quiet_hour(5, q)
    assert not mod.is_quiet_hour(6, q)
    assert not mod.is_quiet_hour(22, q)


def test_is_quiet_hour_zero_length_disabled():
    assert not mod.is_quiet_hour(5, (5, 5)), "start==end 视为零长度，不拦截"


# --- 投递时间分布日志 ---


def test_hour_distribution():
    ats = [
        "2026-09-06T10:01:00",
        "2026-09-06T10:30:00",
        "2026-09-06T11:15:00",
    ]
    assert mod.hour_distribution(ats) == {10: 2, 11: 1}


def test_log_apply_writes_jsonl(tmp_path):
    log_path = tmp_path / "apply_log.jsonl"
    mod.log_apply("2026-09-06T10:00:00", "Java工程师", "某某科技", 0, log_path=str(log_path))
    mod.log_apply("2026-09-06T10:20:00", "Python工程师", "另一家", 0, log_path=str(log_path))
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec == {"applied_at": "2026-09-06T10:00:00", "job": "Java工程师", "brand": "某某科技", "code": 0}


# --- issue #20：会话式突发节奏 / 日上限抖动 / 启动抖动 ---


class _Rng:
    """确定性伪随机：randint 取中值、random 固定、uniform 取中值。"""

    def __init__(self, randint_mid=True, random_val=0.5):
        self.random_val = random_val

    def randint(self, lo, hi):
        return (lo + hi) // 2

    def random(self):
        return self.random_val

    def uniform(self, lo, hi):
        return (lo + hi) / 2

    def choice(self, seq):
        return seq[0]


def test_session_pacer_intra_then_long_break():
    """会话连投 2 个（session 2-2）→ 第 2 个成功后长休息；等待序列：intra,intra,break,intra。"""
    calls = []
    cfg = {
        "session_min": 2,
        "session_max": 2,
        "intra_min": 60,
        "intra_max": 150,
        "break_min": 480,
        "break_max": 1500,
        "dismiss_min": 15,
        "dismiss_max": 40,
    }
    pacer = mod.SessionPacer(cfg, sleep_fn=lambda lo, hi: calls.append(round(lo, 1)), hour_fn=lambda: 10, rng=_Rng())
    pacer.wait_sent()
    pacer.wait_sent()
    pacer.wait_sent()
    # 第 1 个：intra 60（下界×1.0）；第 2 个：intra 60 + break 480；第 3 个：intra 60
    assert calls == [60, 60, 480, 60]


def test_session_pacer_skip_does_not_consume_session():
    """跳过/失败岗位走短等待，且不占会话名额（5 次 skip 后首次 sent 仍无长休息）。"""
    calls = []
    cfg = {
        "session_min": 2,
        "session_max": 2,
        "intra_min": 60,
        "intra_max": 150,
        "break_min": 480,
        "break_max": 1500,
        "dismiss_min": 15,
        "dismiss_max": 40,
    }
    pacer = mod.SessionPacer(cfg, sleep_fn=lambda lo, hi: calls.append(round(lo, 1)), hour_fn=lambda: 10, rng=_Rng())
    for _ in range(5):
        pacer.wait_skip()
    assert calls == [15] * 5, "skip 走 dismiss 短等（下界 15s ×1.0）"
    assert pacer.sent_in_session == 0
    pacer.wait_sent()
    assert calls[-1] == 60 and len(calls) == 6, "skip 后首次成功投递只有 intra，无长休息"


def test_session_pacer_applies_hour_multiplier_to_intra():
    calls = []
    cfg = {"intra_min": 60, "intra_max": 150, "dismiss_min": 15, "dismiss_max": 40}
    p = mod.SessionPacer(cfg, sleep_fn=lambda lo, hi: calls.append(lo), hour_fn=lambda: 12, rng=_Rng())  # 午休 1.5
    p.wait_skip()
    assert calls[-1] == 22.5, "skip 短等也乘时段系数（15×1.5）"
    p.wait_sent()
    assert calls[-1] == 90.0, "会话内间隔 ×1.5（60×1.5）；长休息不乘"


# --- #58 T1：合法长等待的分段心跳（beat_fn 注入；等待总时长一字不动） ---
#
# 票面实证（会话 20260929-145750，一场零楔死的健康会话）：`wedge-stack.txt` 三次落栈，栈顶全是
# `_limits.py:318 rnd_sleep` / `search_list` 的等 XHR 轮询——#56 A4 的 90s 取栈阈值与 240s
# 停摆阈值都默认「节拍之间不会有长空隙」，而 `rnd_sleep` 是一次 `uniform` + 一次 `time.sleep(t)`，
# intra（60-150×时段系数，最长 300s）与长休息（480-1500s）期间心跳文件**一次都不刷新**。
# T1 把长等待分段：每段 ≤ PACE_BEAT_INTERVAL、段间拍点；随机取值口径与等待总时长都不动
# （动了就是改防风控语义，票面边界明令禁止）。


class _CountingRng(_Rng):
    """在 _Rng（中值取值）之上多记 `uniform` 的调用次数。
    为什么非数不可：分段实现最容易犯的错是「每段各抽一次随机数」——N 段的和不再是
    一次均匀抽样的分布（期望值会漂），等待总时长的口径就被改掉了。整段只许抽一次。"""

    def __init__(self):
        super().__init__()
        self.uniform_calls = 0

    def uniform(self, lo, hi):
        self.uniform_calls += 1
        return (lo + hi) / 2


def _paced_pacer(cfg, beats, segs, hour=10):
    """带拍点注入的 pacer：`beats` 收 label、`segs` 收每一段真实睡掉的秒数（时级注入
    `nap_fn` = 不真睡，只记账）。整段等待的取值走 pacer 的 rng，故与老用例同一口径（中值）。"""
    return mod.SessionPacer(cfg, hour_fn=lambda: hour, rng=_CountingRng(), beat_fn=beats.append, nap_fn=segs.append)


_PACE_CFG = {
    "session_min": 2,
    "session_max": 2,
    "intra_min": 60,
    "intra_max": 150,
    "break_min": 480,
    "break_max": 1500,
    "dismiss_min": 15,
    "dismiss_max": 40,
}


def test_rnd_sleep_segments_the_wait_without_changing_its_total(capsys):
    """#58 验收 1（取值层）：一次 `uniform` 决定总时长 → 切成 ≤PACE_BEAT_INTERVAL 的分段，
    每段之前拍一次点；`[限速]` 仍只一行（一次等待 = 一行日志，N 行是噪音）。"""
    beats, segs, rng = [], [], _CountingRng()
    total = mod.rnd_sleep(60, 150, beat_fn=beats.append, label="pace:intra", nap_fn=segs.append, rng=rng)
    assert total == 105.0 and rng.uniform_calls == 1, "整段只抽一次随机数（分段不得改取值口径）"
    assert sum(segs) == pytest.approx(105.0), "各段之和 == 分段前的单次取值：等待总时长不变"
    assert segs == [30, 30, 30, 15] and max(segs) <= mod.PACE_BEAT_INTERVAL
    assert beats == ["pace:intra"] * 4, "每一段之前拍一次点（等待期间持续报平安）"
    assert capsys.readouterr().out.count("[限速]") == 1


def test_rnd_sleep_without_beat_fn_sleeps_the_whole_wait_in_one_call():
    """对照用例（验收 1 的「不缩短」的另一半）：不注入 beat_fn（默认）→ 一次睡完，
    段序列就是 `[t]`——分段只在 sleep 的**调用方式**上，不引入第二处取值分支。"""
    segs = []
    assert mod.rnd_sleep(60, 150, nap_fn=segs.append, rng=_CountingRng()) == 105.0
    assert segs == [105.0]


def test_rnd_sleep_beat_loop_still_sleeps_once_when_total_is_below_the_interval():
    """短等待（真人扫一眼划走的 dismiss）不必切段，但拍点照常打——分段循环对
    `t <= PACE_BEAT_INTERVAL` 的唯一差别是段数为 1，等待时长与拍点语义两边一致。"""
    beats, segs = [], []
    mod.rnd_sleep(15, 25, beat_fn=beats.append, label="pace:dismiss", nap_fn=segs.append, rng=_CountingRng())
    assert segs == [20] and beats == ["pace:dismiss"]


def test_session_pacer_without_beat_fn_keeps_the_pre_58_call_shape(monkeypatch):
    """#58 验收 1（默认路径零变更）：beat_fn=None 时 pacer 仍以**位置参数 (lo, hi)、零 kwargs**
    调用 `rnd_sleep` 一次/等待——老调用方（含仓内既有用例与 monkeypatch 桩）逐字不变，
    新增的分段参数不得从默认路径漏出去。"""
    calls = []
    monkeypatch.setattr(mod, "rnd_sleep", lambda *a, **k: calls.append((a, k)))
    p = mod.SessionPacer(_PACE_CFG, hour_fn=lambda: 10, rng=_CountingRng())
    p.wait_skip()
    p.wait_sent()
    assert calls == [((15, 40), {}), ((60, 150), {})]


def test_session_pacer_intra_wait_beats_every_segment():
    """#58 验收 1（intra）：会话内短等 60-150s（工作时段 ×1.0，中值 105s）在等待期间持续拍点。
    改前这里是**零拍**——105s 已经超过 90s 取栈阈值，健康会话因此被自己的取证链冒充成楔死现场。"""
    beats, segs = [], []
    p = _paced_pacer({**_PACE_CFG, "session_min": 9, "session_max": 9}, beats, segs)
    p.wait_sent()
    assert beats == ["pace:intra"] * 4
    assert sum(segs) == pytest.approx(105.0) and max(segs) <= mod.PACE_BEAT_INTERVAL


def test_session_pacer_long_break_beats_throughout_and_keeps_its_length():
    """#58 验收 1（break，票面后果 2 的正解）：长休息 480-1500s（中值 990s = 16.5 分钟）
    **不乘时段系数**，且 990s 远大于停摆阈值 240s——改前看门狗会把一场健康会话整树 SIGKILL。
    分段后：等待总长仍是那一次抽样，中间每 30s 一次 `pace:break` 拍点。"""
    beats, segs = [], []
    p = _paced_pacer(_PACE_CFG, beats, segs)
    p.wait_sent()  # 第 1 个：只有 intra
    p.wait_sent()  # 第 2 个：intra + 长休息（session 2/2 凑满）
    intra = ["pace:intra"] * 4
    assert beats == intra * 2 + ["pace:break"] * 33
    assert sum(segs) == pytest.approx(105.0 * 2 + 990.0), "等待总时长 == 分段前两次的单次取值之和"
    assert max(segs) <= mod.PACE_BEAT_INTERVAL
    assert sum(segs[8:]) == pytest.approx(990.0), "长休息那一段的时长没被分段切短"


def test_session_pacer_wait_skip_beats_with_the_dismiss_label():
    """wait_skip（跳过/失败岗的短等）同族拍点：label 按 kind 区分，读到 `pace:dismiss`
    就知道这次等待是划走岗、不是 intra 或长休息（C 段判栈要分得清）。"""
    beats, segs = [], []
    p = _paced_pacer(_PACE_CFG, beats, segs)
    p.wait_skip()
    assert beats == ["pace:dismiss"] and sum(segs) == pytest.approx(27.5)


def test_session_pacer_beat_pacing_honours_the_hour_multiplier():
    """时段系数（#11）在分段路径上语义不变：晚间 ×2.0 把 intra 拉到 120-300s（中值 210s），
    分段数随之变多，但每段上限仍是同一个 PACE_BEAT_INTERVAL（乘数只改总时长，不改拍点密度）。"""
    beats, segs = [], []
    p = _paced_pacer({**_PACE_CFG, "session_min": 9, "session_max": 9}, beats, segs, hour=22)
    p.wait_sent()
    assert sum(segs) == pytest.approx(210.0) and max(segs) <= mod.PACE_BEAT_INTERVAL
    assert len(beats) == len(segs) == 7 and set(beats) == {"pace:intra"}


def test_pace_beat_interval_is_the_new_legitimate_silence_ceiling():
    """#58 验收 4 的数值护栏（_limits 侧）：分段节拍存在的全部意义就是把「合法无心跳上界」
    从 intra/break 的量级（300s/1500s）压回一个常数——它必须**同时**小于两级阈值，
    否则阈值又被合法区间击穿（本票的根因）。跨模块不等式见
    `test_live_apply.py::test_legitimate_silence_ceiling_sits_below_both_thresholds`。"""
    assert mod.PACE_BEAT_INTERVAL > 0
    cfg = mod.load_config(env={})
    assert cfg["break_min"] > mod.PACE_BEAT_INTERVAL, "长休息必须靠分段才有拍点（否则 T1 白做）"


def test_effective_daily_limit_jitter_band():
    cfg = {"daily_limit": 60, "cap_jitter": 5, "daily_limit_fixed": True}
    vals = {mod.effective_daily_limit(cfg, state_path=None) for _ in range(200)}
    assert vals <= set(range(55, 66)), f"应落在 55-65，实际 {min(vals)}-{max(vals)}"
    assert len(vals) > 1, "多次取值应有波动，非固定值"


def test_effective_daily_limit_floor_is_one():
    assert (
        mod.effective_daily_limit(
            {"daily_limit": 1, "cap_jitter": 5, "daily_limit_fixed": True}, rng=_Rng(), state_path=None
        )
        >= 1
    )  # 中值 randint(-5,5)=0 → 1
    rng = _Rng()
    rng.randint = lambda lo, hi: lo  # 取最负抖动
    assert (
        mod.effective_daily_limit(
            {"daily_limit": 2, "cap_jitter": 5, "daily_limit_fixed": True}, rng=rng, state_path=None
        )
        == 1
    )


# --- 自动阶梯日上限（用户约定 2026-09-10）：首日 20，每天 +2~5 抖动爬升，封顶 ---


def test_advance_daily_limit_first_day_idempotent(tmp_path):
    import random as _random

    state = tmp_path / "state.json"
    d1 = datetime(2026, 9, 10).date()
    assert mod.advance_daily_limit(20, 40, 2, 5, d1, state, _random.Random(1)) == 20, "首日=起步值"
    assert mod.advance_daily_limit(20, 40, 2, 5, d1, state, _random.Random(1)) == 20, "同日幂等"
    st = json.loads(state.read_text())
    assert st == {"limit": 20, "last_date": "2026-09-10"}


def test_advance_daily_limit_climbs_with_jitter(tmp_path):
    import random as _random

    state = tmp_path / "state.json"
    d1 = datetime(2026, 9, 10).date()
    mod.advance_daily_limit(20, 40, 2, 5, d1, state, _random.Random(1))
    v2 = mod.advance_daily_limit(20, 40, 2, 5, d1 + timedelta(days=1), state, _random.Random(1))
    assert 22 <= v2 <= 25, "次日 +2~5 抖动爬升"
    assert 24 <= mod.advance_daily_limit(20, 40, 2, 5, d1 + timedelta(days=2), state, _random.Random(1)) <= 30


def test_advance_daily_limit_caps_and_catches_up(tmp_path):
    import random as _random

    state = tmp_path / "state.json"
    d1 = datetime(2026, 9, 10).date()
    mod.advance_daily_limit(20, 24, 2, 5, d1, state, _random.Random(1))
    assert mod.advance_daily_limit(20, 24, 2, 5, d1 + timedelta(days=30), state, _random.Random(1)) == 24, "封顶不再涨"


def test_effective_daily_limit_ladder_mode(tmp_path):
    cfg = mod.load_config(env={})
    assert cfg["daily_limit_fixed"] is False, "未显式设置 APPLY_DAILY_LIMIT → 阶梯模式"
    today = datetime.now().date()
    # _Rng 抖动取中值 0：day_cap 20 + 0
    assert mod.effective_daily_limit(cfg, rng=_Rng(), state_path=tmp_path / "s.json", today=today) == 20


def test_effective_daily_limit_fixed_env():
    cfg = mod.load_config(env={"APPLY_DAILY_LIMIT": "33"})
    assert cfg["daily_limit_fixed"] is True, "显式 APPLY_DAILY_LIMIT → 固定值"
    assert mod.effective_daily_limit(cfg, rng=_Rng(), state_path=None) == 33


# --- 简历公司过滤（用户约定 2026-09-10） ---


def test_is_excluded_company_matching():
    kws = ["昱琨", "exampleco", "旻昉", "示例大厂", "思科"]
    assert mod.is_excluded_company("昱琨智能科技（上海）", kws)
    assert mod.is_excluded_company("ExamplecoDAO", kws)
    assert mod.is_excluded_company("上海旻昉科技", kws)
    assert mod.is_excluded_company("示例大厂技术有限公司", kws)
    assert not mod.is_excluded_company("峰云科技", kws)
    assert not mod.is_excluded_company("", kws)
    assert not mod.is_excluded_company("示例大厂云", []), "空关键词列表不过滤"


def test_load_excluded_companies_missing_file(tmp_path, capsys):
    assert mod.load_excluded_companies(tmp_path / "nope.json") == []
    assert "未启用" in capsys.readouterr().out


def test_startup_jitter_zero_when_unconfigured():
    slept = []
    assert mod.startup_jitter({}, sleep_fn=lambda s: slept.append(s)) == 0
    assert not slept, "cfg 无 start_jitter_max 时不等待（测试/旧调用方安全）"


def test_startup_jitter_uses_config():
    slept = []
    w = mod.startup_jitter({"start_jitter_max": 600}, sleep_fn=lambda s: slept.append(s), rng=_Rng())
    assert w == 300 and slept == [300]


def test_load_config_humanize_defaults():
    cfg = mod.load_config(env={})
    assert cfg["session_min"] == 2 and cfg["session_max"] == 4
    assert cfg["intra_min"] == 60 and cfg["break_min"] == 480
    assert cfg["dwell_min"] == 10 and cfg["dwell_max"] == 35
    assert cfg["start_jitter_max"] == 600 and cfg["cap_jitter"] == 5
    assert cfg["typo_prob"] == 0.05
    assert cfg["type_delay_min"] == 40 and cfg["type_delay_max"] == 120


def test_load_config_probability_validation(capsys):
    cfg = mod.load_config(env={"APPLY_TYPO_PROB": "2.0"})
    assert cfg["typo_prob"] == 0.05, "概率超 0-1 回退默认"
    cfg = mod.load_config(env={"APPLY_PASTE_PROB": "0"})
    assert cfg["paste_prob"] == 0.0, "0 是合法概率值"


def test_notify_sends_macos_notification(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: calls.append((a, k)))
    mod.notify("投递暂停", '触发风控码 37 "含引号"')
    out = capsys.readouterr().out
    assert "投递暂停" in out
    assert calls, "darwin 下应调用 osascript 发送系统通知"
    a, k = calls[0]
    assert a[0][0] == "osascript"
    assert '\\"' in a[0][2], "AppleScript 字符串中的双引号必须转义"
    assert k.get("check") is False


# --- applied_history 台账（C1：followup/learn 的消费文件必须有写入方） ---

_JOB = {"encryptJobId": "abc123", "jobName": "Solidity工程师", "brandName": "某Web3公司"}


def test_record_history_missing_file_starts_fresh(tmp_path):
    """全新 clone 无 applied_history.json → 告警后从空台账起步，父目录自动创建。"""
    history_path = tmp_path / "data" / "applied_history.json"
    mod.record_history(_JOB, "2026-09-07T10:00:00", history_path=str(history_path))
    data = json.loads(history_path.read_text(encoding="utf-8"))
    assert data == {
        "applied": [
            {
                "encryptJobId": "abc123",
                "jobName": "Solidity工程师",
                "brandName": "某Web3公司",
                "date": "2026-09-07",
                "note": "批量投递",
            }
        ]
    }


def test_record_history_appends_new_entry(tmp_path):
    """已有台账时追加新岗位，原有条目保留。"""
    history_path = tmp_path / "applied_history.json"
    history_path.write_text(
        json.dumps(
            {"applied": [{"encryptJobId": "old1", "jobName": "旧岗位", "brandName": "旧公司", "date": "2026-09-01"}]}
        ),
        encoding="utf-8",
    )
    mod.record_history(_JOB, "2026-09-07T10:00:00", history_path=str(history_path))
    data = json.loads(history_path.read_text(encoding="utf-8"))
    assert [e["encryptJobId"] for e in data["applied"]] == ["old1", "abc123"]
    assert data["applied"][1] == {
        "encryptJobId": "abc123",
        "jobName": "Solidity工程师",
        "brandName": "某Web3公司",
        "date": "2026-09-07",
        "note": "批量投递",
    }


def test_record_history_dedups_by_encrypt_job_id(tmp_path, capsys):
    """同一 encryptJobId 已登记 → 跳过不重复写。"""
    history_path = tmp_path / "applied_history.json"
    mod.record_history(_JOB, "2026-09-07T10:00:00", history_path=str(history_path))
    mod.record_history(_JOB, "2026-09-07T11:00:00", history_path=str(history_path))
    data = json.loads(history_path.read_text(encoding="utf-8"))
    assert len(data["applied"]) == 1
    assert "已登记，跳过" in capsys.readouterr().out


def test_record_history_corrupted_file_rebuilds(tmp_path, capsys):
    """文件损坏 → 告警后从空台账重建，本次投递正常登记。"""
    history_path = tmp_path / "applied_history.json"
    history_path.write_text("{not valid json!!!", encoding="utf-8")
    mod.record_history(_JOB, "2026-09-07T10:00:00", history_path=str(history_path))
    out = capsys.readouterr().out
    assert "[警告]" in out and "损坏" in out
    data = json.loads(history_path.read_text(encoding="utf-8"))
    assert [e["encryptJobId"] for e in data["applied"]] == ["abc123"]


def test_record_history_optional_recruiter_active_fields(tmp_path):
    # #32 段 2：recruiter_active 键在才写；缺省零行为变化（旧条目形态不变）
    history_path = tmp_path / "applied_history.json"
    mod.record_history(
        _JOB,
        "2026-09-24T10:00:00",
        history_path=str(history_path),
        recruiter_active={"bossActiveDesc": "刚刚活跃", "bossOnline": True},
    )
    entry = json.loads(history_path.read_text(encoding="utf-8"))["applied"][0]
    assert entry["bossActiveDesc"] == "刚刚活跃" and entry["bossOnline"] is True

    history_path2 = tmp_path / "h2.json"
    mod.record_history(_JOB, "2026-09-24T10:00:00", history_path=str(history_path2))
    entry2 = json.loads(history_path2.read_text(encoding="utf-8"))["applied"][0]
    assert "bossActiveDesc" not in entry2 and "bossOnline" not in entry2


# --- LIVE_PACE 节奏预设（2026-09-26 用户定档「温和」：启动抖动/跳过短等/阅读停留；显式 APPLY_* 仍最高优先） ---


def test_load_config_pace_fast_preset():
    cfg = mod.load_config({"LIVE_PACE": "fast"})
    assert cfg["start_jitter_max"] == 120  # 600 → 120
    assert cfg["dismiss_min"] == 8 and cfg["dismiss_max"] == 20  # 15/40 减半
    assert cfg["dwell_min"] == 8 and cfg["dwell_max"] == 25  # 10/35 微降
    assert cfg["intra_min"] == 60 and cfg["break_min"] == 480  # 未获批砍的节奏不动


def test_load_config_preset_yields_to_explicit_env():
    cfg = mod.load_config({"LIVE_PACE": "fast", "APPLY_START_JITTER_MAX": "300"})
    assert cfg["start_jitter_max"] == 300  # 显式 env > 预设


def test_load_config_unknown_pace_ignored(capsys):
    cfg = mod.load_config({"LIVE_PACE": "turbo"})
    assert cfg["start_jitter_max"] == 600  # 未知档位不生效
    assert "未知档位" in capsys.readouterr().out


def test_load_config_pace_case_insensitive_and_default():
    assert mod.load_config({"LIVE_PACE": "FAST"})["dismiss_min"] == 8  # 大小写不敏感
    assert mod.load_config({})["start_jitter_max"] == 600  # 未设置 → 原默认
    assert mod.load_config({"LIVE_PACE": " "})["dismiss_max"] == 40  # 空白 → 原默认
