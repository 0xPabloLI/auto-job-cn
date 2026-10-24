#!/usr/bin/env python3
"""
投递共用工具（#44 自 batch_apply_v2.py 迁出）。**消费者不止 live_apply**（#59 修复轮 R4-3
把这句话改准——旧版写的是「live_apply 是唯一消费者」，本批 #57 拆出的两个模块已经打了脸）：
- `live_apply`：节奏/限速/台账/布尔开关的主体消费者
- `_forensics`：`env_bool_switch` + `BOOL_ENV_TWO_OFF`（LIVE_WEDGE_DUMP 取值表）
- `_wedge_ledger`：`env_bool_switch` + `BOOL_ENV_TWO_OFF` / `BOOL_ENV_ON_WORDS`（LIVE_WEDGE_SKIP）
- `gen_greetings`：`notify`（#45 密度达标的 macOS 通知）
- `oneshot/verify_58_pacing.py`：`PACE_BEAT_INTERVAL` / `SessionPacer`（#58 缩样 A/B 探针）
- 日上限：固定值 / 自动阶梯（首日 20，每天 +2~5 抖动爬升，封顶 40）+ ±抖动
- 时段乘数（#11）与静默时段（0-7 暂停，支持跨午夜）
- 会话式突发节奏（#20 SessionPacer）+ 启动抖动 + 限速睡眠
- 限速睡眠可分段（#58 T1）：注入 beat_fn 后每 ≤PACE_BEAT_INTERVAL 秒拍一次点，
  等待总时长与随机取值口径都不动——它是「合法无心跳上界」的唯一来源，下游取证阈值以它为前提
- 台账：apply_log.jsonl（时间分布）+ applied_history.json（followup/learn 的消费台账）
- 布尔 env 开关：取值表**唯一真源**（下面「布尔 env 开关」小节）+ `env_bool_switch` 显式解析
  （#57 第二轮收口，各开关的差异原样保留；其余各处只留指针，不再抄一份对照表）
- macOS 系统通知、简历公司过滤、平台每日上限检测、站内错误码处理
行为不变迁移：函数体逐字平移，公开签名与默认参数不变。
"""

import json
import os
import random
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _atomic_io import write_json_atomic

# 以下常量是 legacy 默认路径（默认 profile，前缀 ""）——仅作函数默认参数；
# 入口一律经 _profile_paths 运行时解析（#28，不做 import 期绑定）。
APPLY_LOG = str(ROOT / "private/data/apply_log.jsonl")
HISTORY = str(ROOT / "private/data/applied_history.json")
LADDER_STATE = str(ROOT / "private/data/daily_limit_state.json")
EXCLUDED_COMPANIES = str(ROOT / "private/data/excluded_companies.json")


# 时段乘数（issue #11 方案 A）：工作时段 1.0、午休 1.5，其余（早间/晚间/夜间）2.0 保守慢速。
# 系数表常量化，本表与 DEFAULT_HOUR_MULTIPLIER 全改 1 即回退 issue #5 的静态限速行为。
HOUR_MULTIPLIERS = {
    9: 1.0,
    10: 1.0,
    11: 1.0,
    12: 1.5,
    13: 1.5,
    14: 1.0,
    15: 1.0,
    16: 1.0,
    17: 1.0,
}
DEFAULT_HOUR_MULTIPLIER = 2.0
QUIET_HOURS_DEFAULT = (0, 7)

# --- #58 T1：合法无心跳上界（分段节拍的段长）---
# 这个常数不是「节奏参数」，是**取证阈值的前提**：会话内所有经由 `SessionPacer` 的等待被切成
# ≤ 本值的分段，段间拍一次心跳，于是「两次心跳之间最长可以隔多久」从"intra/长休息的量级"
# 收敛成这一个数。下游两级阈值都以它为前提（对照表见 live_apply 的 WEDGE_STACK_SECONDS 与
# SESSION_WATCHDOG_DEFAULT 上方注释，#58 票面「事实」节）：
#   · intra 60-150s × 时段系数（9-17 点 1.0 / 12-13 点 1.5 / 其余 2.0）→ 最长 300s
#   · 连投满会话后的长休息 break 480-1500s（8-25 分钟，**不乘系数**）
#   · 二者都 > 取栈阈值 90s、长休息更 > 停摆阈值 240s ⇒ 改前一场健康会话会给自己落假栈、
#     并被自己的看门狗整树 SIGKILL（票面后果 1/2）。
# 取 30s：给 90s 取栈阈值留 3 倍余量（一拍写失败/一次 GIL 调度延迟都不致越界），同时
# 一次心跳只是一个本地原子写文件，等待期间的网络/页面观测面与一次长 sleep 无差别
# （防风控语义不变，票面边界）。**改动本值必须同步复核那两个阈值**——数值护栏用例
# `test_legitimate_silence_ceiling_sits_below_both_thresholds` 锁着
# PACE_BEAT_INTERVAL < WEDGE_STACK_SECONDS < SESSION_WATCHDOG_DEFAULT。
PACE_BEAT_INTERVAL = 30  # 秒

# LIVE_PACE 节奏预设（2026-09-26 用户定档「温和」）：只动启动抖动/跳过短等/阅读停留三处，
# 会话内间隔与会话间长休息不动（激进档未获批）。显式 APPLY_* env 仍最高优先（预设只改默认值）。
PACE_PRESETS = {
    "fast": {
        "start_jitter_max": 120,  # 600 → 120
        "dismiss_min": 8,  # 15/40 → 8/20（减半）
        "dismiss_max": 20,
        "dwell_min": 8,  # 10/35 → 8/25（微降）
        "dwell_max": 25,
    },
}


# --- #57 第二轮：布尔 env 开关的取值表（单一真源）+ 统一解析入口 ---
#
# 仓内几个布尔 env 各持一套取值表；本小节把那些表收进这一个文件（表本身是常量），解析走同一个
# `env_bool_switch`，于是「大小写敏感性 / 空串归属 / 非法值怎么走」都写在调用点上；
# **默认方向不用写**——仓内五个开关（#56 取证 B 起含 `LIVE_NET_PROBE`）全默认开，`env_bool_switch` 就按「未设置即开」实现
# （#59 修复轮 R4-4：`default_on` 形参与「默认关」那三条分支零调用方，删了；要加默认关的开关时
# 按那个调用点的真实语义重新设计，别从这里的旧注释里复原）。
# 这里是**对照表的唯一一份**：其余各处（`_forensics` / `_wedge_ledger` / `live_apply` 的调用点与
# 用例 docstring）只留一句指针到本小节。
# 「改一处要读全文件」的老毛病在 env 这条线上到此为止。
#
# 取值表对照（**刻意不合并成一套**：#56 修复轮 F2 逐字核对后，
# `test_three_bool_env_switches_have_three_distinct_value_tables` 把这些差异钉成了事实；
# 想统一就是显式的语义变更，得先改那条用例并另行开票）：
#   · LIVE_RECOMMEND —— 默认开；**精确串比较**（不 strip 不 lower），关只认字面 "off"
#                    （`BOOL_ENV_EXACT_OFF`）⇒ "0" / "OFF" / " off " 都关不掉它；空串按开。
#                    已走 `env_bool_switch`（调用点 `live_apply._apply_collect_cfg`；
#                    #105 下线录屏开关后，精确串族只剩它一个在用）。
#   · LIVE_WEDGE_DUMP —— 默认开；strip+lower 后关只认 `BOOL_ENV_TWO_OFF` 两个值，其余（含空串与
#                    "false"/"no"/"ofs"）一律按开；非法值**不提示**。
#                    已走 `env_bool_switch`（调用点 `_forensics._wedge_dump_enabled`，#57 第二轮后半接线；
#                    参数逐项等于接线前那句 `raw.strip().lower() not in ("0","off")` 的口径，未设提示）。
#   · LIVE_NET_PROBE —— 默认开；与 LIVE_WEDGE_DUMP **同一张表**（strip+lower 后关只认
#                    `BOOL_ENV_TWO_OFF`，其余一律按开、不提示）。#56 取证 B（击杀前出网探针）的
#                    逃生开关，调用点 `_forensics._net_probe_enabled`——它与取栈同族是因为两者的
#                    「关掉代价」同形：都是「下一次楔死只剩一条线索」，而不是各自有敏感性差异。
#   · LIVE_WEDGE_SKIP —— 默认开；strip+lower 后关只认 `BOOL_ENV_TWO_OFF`，「可识别的开」是
#                    `BOOL_ENV_ON_WORDS` 四个值；空串按开；两张表都不认的原始值**额外打一行**
#                    「按默认『开』处理，关闭只认 0/off」之后再按开走。
#                    已走 `env_bool_switch`（调用点 `_wedge_ledger._wedge_skip_enabled`）。
#   · LIVE_PREFLIGHT —— 默认开；strip+lower 后关认 `BOOL_ENV_FOUR_OFF` 四个值（比两值族宽在
#                    "false"/"no"）。接线状态：本轮未接线（不在本票四处之内），列在这儿只为让
#                    「几张表各长什么样」在一处读全。
BOOL_ENV_EXACT_OFF = ("off",)  # 精确串族：LIVE_RECOMMEND（#105 后该族唯一在用开关）
BOOL_ENV_TWO_OFF = ("0", "off")  # 两值族（strip+lower）：LIVE_WEDGE_DUMP / LIVE_WEDGE_SKIP
BOOL_ENV_ON_WORDS = ("1", "on", "true", "yes")  # 「可识别的开」词表（仅用于非法值提示，不参与返回值）
BOOL_ENV_FOUR_OFF = ("off", "0", "false", "no")  # 四值族（strip+lower）：LIVE_PREFLIGHT


def env_bool_switch(env, name, *, off_values, on_values=(), strip=False, lower=False, warn_on_unknown=False):
    """按**显式值表**解析一个布尔 env 开关，返回最终方向（True=开）。参数就是各开关既有口径，
    没有隐含默认，也不替调用方统一表：
    - **未设置 ⇒ 开**：`env` 里键缺失、值为 None、空串，或任何其它假值（0 / False 之类）都按开走。
      这一条照的是接线前 `_wedge_skip_enabled` 那句 `str(env.get(name) or "")` 的口径（假值先塌成
      空串 ⇒ 不命中 off 表 ⇒ 按默认走，也不提示），所以对抗输入下的返回值与改前逐字一致，
      不是「顺手规范化」。`" "`（纯空白）是**真值**，照常走下面的 strip / lower 与查表。
      #59 修复轮 R4-4：仓内当时四个开关（LIVE_RECOMMEND / LIVE_WEDGE_DUMP / LIVE_WEDGE_SKIP，
      外加一个后来随 #105 下线的录屏开关）**全部默认开**，「默认关」那个方向零调用方，于是把
      `default_on` 形参与它
      带出的三条死分支（未设置的方向、提示文案的方向字/动词/词表）一起删了——要加默认关的开关时
      再按那个调用点的真实语义补，不留没有调用方的分支。
      #56 取证 B 新加的 `LIVE_NET_PROBE` 方向相同（也默认开），所以「默认关」分支**仍然**零调用方。
    - `off_values`：命中其一即判「关」的取值表；
    - `on_values`：只参与「这个值可识别吗」的判定（给 `warn_on_unknown` 用），不影响返回值；
    - `strip` / `lower`：取值前先 `str.strip()` / `.lower()`——**不传就是精确串比较**，
      于是大小写与首尾空白的敏感性写在调用点上，而不是由解析器替它决定；
    - `warn_on_unknown`：去空白后非空、且两张表都不认时打一行提示（说明按默认「开」走），
      写错值不当哑事；文案与接线前的 `_wedge_skip_enabled` 逐字一致。
    空串归属：strip 后为空串**不算** `off_values` 的成员（两值族因此按开处理，与接线前一致），
    也不触发提示。"""
    env = os.environ if env is None else env
    raw = env.get(name)
    if not raw:
        return True
    text = str(raw)
    if strip:
        text = text.strip()
    if lower:
        text = text.lower()
    if warn_on_unknown and text and text not in off_values and text not in on_values:
        print(f"[配置] {name}={str(raw)!r} 不是可识别值，按默认「开」处理（关闭只认 {'/'.join(off_values)}）")
    return text not in off_values


def _resolve_pace_preset(env):
    """解析 LIVE_PACE：命中 PACE_PRESETS 返回该预设（作为 load_config 的默认值层）；
    未设置/空白返回空 dict；未知档位打印提示并返回空 dict（回退原默认节奏）。"""
    raw = str(env.get("LIVE_PACE") or "").strip().lower()
    if not raw:
        return {}
    preset = PACE_PRESETS.get(raw)
    if preset is None:
        print(f"[配置] LIVE_PACE={raw!r} 未知档位（可用：{', '.join(PACE_PRESETS)}），使用默认节奏")
        return {}
    print(f"[配置] LIVE_PACE={raw} 预设生效（启动抖动/跳过短等/阅读停留已调快）")
    return preset


def hour_multiplier(hour):
    """当前小时的投递间隔乘数。"""
    return HOUR_MULTIPLIERS.get(hour, DEFAULT_HOUR_MULTIPLIER)


def current_hour():
    """当前小时（0-23）；独立函数便于测试注入。"""
    return datetime.now().hour


def is_quiet_hour(hour, quiet):
    """hour 是否落在静默时段内。quiet 为 (start, end)，支持跨午夜（如 23-6）；
    None 或 start==end（零长度）视为未启用。区间左闭右开：0-7 即 0 点起、7 点整恢复。"""
    if not quiet:
        return False
    start, end = quiet
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _parse_quiet_hours(raw):
    """解析 APPLY_QUIET_HOURS：'off' 关闭；'start-end'（可跨午夜）；
    未设置或非法值回退默认 0-7（限速保护不可被配置意外关闭）。"""
    if raw is None or raw == "":
        raw = f"{QUIET_HOURS_DEFAULT[0]}-{QUIET_HOURS_DEFAULT[1]}"
    text = str(raw).strip().lower()
    if text in ("off", "none"):
        return None
    m = re.fullmatch(r"(\d{1,2})\s*-\s*(\d{1,2})", text)
    if not m:
        print(
            f"[配置] APPLY_QUIET_HOURS={raw!r} 不是有效的静默时段，"
            f"使用默认 {QUIET_HOURS_DEFAULT[0]}-{QUIET_HOURS_DEFAULT[1]}"
        )
        return QUIET_HOURS_DEFAULT
    start, end = int(m.group(1)), int(m.group(2))
    if start > 23 or end > 23:
        print(
            f"[配置] APPLY_QUIET_HOURS={raw!r} 小时需在 0-23，"
            f"使用默认 {QUIET_HOURS_DEFAULT[0]}-{QUIET_HOURS_DEFAULT[1]}"
        )
        return QUIET_HOURS_DEFAULT
    return (start, end)


def load_config(env=None):
    """每日上限与静默时段；环境变量可覆盖：
    APPLY_DAILY_LIMIT / APPLY_QUIET_HOURS（'off'/'start-end'）；
    issue #20 拟人节奏：APPLY_SESSION_MIN/MAX（会话连投数）、APPLY_INTRA_MIN/MAX（会话内间隔秒）、
    APPLY_BREAK_MIN/MAX（会话间长休息秒）、APPLY_DISMISS_MIN/MAX（跳过岗位短等秒）、
    APPLY_DWELL_MIN/MAX（详情页阅读停留秒）、APPLY_START_JITTER_MAX（启动抖动秒）、
    APPLY_CAP_JITTER（日上限抖动量）、
    APPLY_TYPE_DELAY_MIN/MAX（打字键间延迟毫秒）、APPLY_TYPO_PROB（打字错字概率）。
    LIVE_PACE=fast（2026-09-26 温和提速预设）：一次调快启动抖动/跳过短等/阅读停留三处默认值，
    显式 APPLY_* env 仍最高优先。
    日上限阶梯（用户约定 2026-09-10）：未显式设置 APPLY_DAILY_LIMIT 时走自动阶梯——
    首日 20，每天 +APPLY_LADDER_STEP_MIN~MAX 抖动爬升到 APPLY_LADDER_CAP 封顶，
    状态落 private/data/daily_limit_state.json；显式设置则固定值（阶梯关闭）。
    APPLY_PASTE_PROB（live 链路粘贴概率，默认 0.7：粘贴为主偶尔逐字打字）。"""
    env = os.environ if env is None else env
    pace = _resolve_pace_preset(env)

    def get_int(key, default):
        raw = env.get(key)
        if raw is None or raw == "":
            return default
        try:
            val = int(raw)
        except ValueError:
            print(f"[配置] {key}={raw!r} 不是整数，使用默认 {default}")
            return default
        if val <= 0:
            print(f"[配置] {key}={raw!r} 需为正整数，使用默认 {default}")
            return default
        return val

    def get_float(key, default):
        raw = env.get(key)
        if raw is None or raw == "":
            return default
        try:
            val = float(raw)
        except ValueError:
            print(f"[配置] {key}={raw!r} 不是数值，使用默认 {default}")
            return default
        if val < 0 or val > 1:
            print(f"[配置] {key}={raw!r} 需在 0-1，使用默认 {default}")
            return default
        return val

    raw_limit = str(env.get("APPLY_DAILY_LIMIT") or "").strip()
    cfg = {
        # 显式设置 APPLY_DAILY_LIMIT → 固定值；未设置 → 自动阶梯（effective_daily_limit）
        "daily_limit": get_int("APPLY_DAILY_LIMIT", 20),
        "daily_limit_fixed": bool(raw_limit),
        "quiet_hours": _parse_quiet_hours(env.get("APPLY_QUIET_HOURS")),
        # issue #20：会话式突发节奏（连投几个→长休息），替代均匀节拍器间隔
        "session_min": get_int("APPLY_SESSION_MIN", 2),
        "session_max": get_int("APPLY_SESSION_MAX", 4),
        "intra_min": get_int("APPLY_INTRA_MIN", 60),
        "intra_max": get_int("APPLY_INTRA_MAX", 150),
        "break_min": get_int("APPLY_BREAK_MIN", 480),  # 8 分钟
        "break_max": get_int("APPLY_BREAK_MAX", 1500),  # 25 分钟
        "dismiss_min": get_int("APPLY_DISMISS_MIN", pace.get("dismiss_min", 15)),  # 已沟通过：扫一眼划走
        "dismiss_max": get_int("APPLY_DISMISS_MAX", pace.get("dismiss_max", 40)),
        "dwell_min": get_int("APPLY_DWELL_MIN", pace.get("dwell_min", 10)),  # 详情页阅读停留
        "dwell_max": get_int("APPLY_DWELL_MAX", pace.get("dwell_max", 35)),
        "start_jitter_max": get_int("APPLY_START_JITTER_MAX", pace.get("start_jitter_max", 600)),  # 启动抖动 0-10 分钟
        "cap_jitter": get_int("APPLY_CAP_JITTER", 5),  # 日上限 ±抖动，去整数规律
        "type_delay_min": get_int("APPLY_TYPE_DELAY_MIN", 40),  # 毫秒
        "type_delay_max": get_int("APPLY_TYPE_DELAY_MAX", 120),
        "typo_prob": get_float("APPLY_TYPO_PROB", 0.05),
        # 自动阶梯日上限（用户约定 2026-09-10）：首日 20，每天 +2~5 抖动爬升，40 封顶
        "ladder_cap": get_int("APPLY_LADDER_CAP", 40),
        "ladder_step_min": get_int("APPLY_LADDER_STEP_MIN", 2),
        "ladder_step_max": get_int("APPLY_LADDER_STEP_MAX", 5),
        # live 链路输入方式：粘贴为主，偶尔逐字打字（全 1 = 永远粘贴）
        "paste_prob": get_float("APPLY_PASTE_PROB", 0.7),
    }
    for lo, hi in (
        ("session_min", "session_max"),
        ("intra_min", "intra_max"),
        ("break_min", "break_max"),
        ("dismiss_min", "dismiss_max"),
        ("dwell_min", "dwell_max"),
        ("type_delay_min", "type_delay_max"),
    ):
        if cfg[lo] > cfg[hi]:
            cfg[lo], cfg[hi] = cfg[hi], cfg[lo]
    return cfg


def advance_daily_limit(base, cap, step_lo, step_hi, today, state_path=LADDER_STATE, rng=None):
    """自动阶梯日上限（用户约定 2026-09-10）：首日 = base（20），此后每过一天随机涨
    step_lo~step_hi（2-5），到 cap（40）封顶。状态落 daily_limit_state.json，
    停机数天后跨天一次性追平增量（只涨对应天数）；同日重复调用幂等。
    state_path=None 时不读不写（测试用），恒返回 base。"""
    rng = rng or random
    if state_path is None:
        return base
    try:
        with open(state_path, encoding="utf-8") as f:
            st = json.load(f)
        limit = int(st["limit"])
        last = datetime.fromisoformat(st["last_date"]).date()
    except Exception:
        limit, last = base, today
    for _ in range(max(0, (today - last).days)):
        if limit >= cap:
            break
        limit = min(cap, limit + rng.randint(step_lo, step_hi))
    write_json_atomic(state_path, {"limit": limit, "last_date": today.isoformat()})
    return limit


def effective_daily_limit(cfg, rng=None, state_path=LADDER_STATE, today=None):
    """日上限：默认自动阶梯（首日 20，每天 +2~5 抖动爬升到 ladder_cap=40 封顶，
    状态落 private/data/daily_limit_state.json）；APPLY_DAILY_LIMIT 显式设置时固定值。
    最终叠加 ±cap_jitter 抖动去整数规律，不低于 1。"""
    rng = rng or random
    base = cfg.get("daily_limit", 20)
    jitter = cfg.get("cap_jitter", 0)
    if cfg.get("daily_limit_fixed"):
        day_cap = base
    else:
        day_cap = advance_daily_limit(
            base,
            cfg.get("ladder_cap", 40),
            cfg.get("ladder_step_min", 2),
            cfg.get("ladder_step_max", 5),
            today or datetime.now().date(),
            state_path,
            rng,
        )
    return max(1, day_cap + rng.randint(-jitter, jitter))


def startup_jitter(cfg, sleep_fn=None, rng=None):
    """开跑前随机延迟（#20）：每次启动不在同一刻，模拟"想起来才打开 App"。
    cfg 缺 start_jitter_max（如单测传 {}）时等待 0。"""
    rng = rng or random
    wait = rng.uniform(0, cfg.get("start_jitter_max", 0))
    if wait > 0:
        print(f"[启动] 随机延迟 {wait:.0f}s 后开始（模拟真人开工时间）")
        (sleep_fn or time.sleep)(wait)
    return wait


class SessionPacer:
    """会话式突发节奏（issue #20）：连投 session_min~max 个（会话内短间隔）→
    长休息 break_min~max → 新会话。会话内间隔乘时段系数（#11），长休息不乘。
    跳过/失败岗位只短等（真人扫到"已沟通过"很快划走），不占会话名额。
    sleep_fn 注入便于测试（生产用 rnd_sleep，打印并真实 sleep）。

    #58 T1 的两个新入参（默认 None = 与本票之前逐字节一致）：
    - `beat_fn(label)`：合法长等待期间的「报平安」拍点注入器（live_apply 注 `_heartbeat_beat`）。
      给出后，**所有经由 `_sleep` 的等待**（intra / 长休息 / wait_skip）改走分段睡，段间拍一次；
      等待总时长与随机取值口径都不动（详见 `rnd_sleep` docstring）。
    - `nap_fn(seconds)`：秒级睡眠钩子，只在分段路径生效，单测用它记账代替真睡。
      为什么和 `sleep_fn` 分两个口子：`sleep_fn` 的契约是「接管**整段**等待」（签名 (lo, hi)，
      取值与打印都在它里面），注入它时分段与拍点无从插手——两个语义挤一个参数会让老用例的
      `lambda lo, hi: ...` 直接 TypeError。两者同时给出时以 `sleep_fn` 为准（生产不注它）。"""

    def __init__(self, cfg, sleep_fn=None, hour_fn=None, rng=None, beat_fn=None, nap_fn=None):
        self.cfg = cfg
        self.rng = rng or random
        # 延迟绑定 rnd_sleep（方法内解析全局名），保证测试 monkeypatch mod.rnd_sleep 生效
        self._sleep_fn = sleep_fn
        self._hour_fn = hour_fn
        self._beat_fn = beat_fn
        self._nap_fn = nap_fn
        self.sent_in_session = 0
        self.session_target = self._new_target()

    def _sleep(self, lo, hi, kind):
        """一次等待：注入整段 `sleep_fn` 或未注 `beat_fn` → 历史形态（一次调用）；
        否则走分段路径。`kind`（intra/break/dismiss）进拍点 label，让读到栈/心跳的人
        分辨得出「这次长等待是哪一类合法等待」——#56 C 段判栈要的正是这个区分。"""
        if self._beat_fn is None or self._sleep_fn is not None:
            (self._sleep_fn or rnd_sleep)(lo, hi)
            return
        rnd_sleep(lo, hi, beat_fn=self._beat_fn, label=f"pace:{kind}", nap_fn=self._nap_fn, rng=self.rng)

    def _hour(self):
        return (self._hour_fn or current_hour)()

    def _new_target(self):
        lo, hi = self.cfg.get("session_min", 2), self.cfg.get("session_max", 4)
        return self.rng.randint(min(lo, hi), max(lo, hi))

    def _intra(self):
        m = hour_multiplier(self._hour())
        self._sleep(self.cfg.get("intra_min", 60) * m, self.cfg.get("intra_max", 150) * m, "intra")

    def wait_sent(self):
        """成功投递后：会话内短等；凑满一个会话则长休息并重置。"""
        self._intra()
        self.sent_in_session += 1
        if self.sent_in_session >= self.session_target:
            lo, hi = self.cfg.get("break_min", 480), self.cfg.get("break_max", 1500)
            print(f"    [会话] 已连投 {self.sent_in_session} 个，长休息 {lo // 60}-{hi // 60} 分钟后开新会话")
            self._sleep(lo, hi, "break")
            self.sent_in_session = 0
            self.session_target = self._new_target()

    def wait_skip(self):
        """跳过/失败岗位后：短等划走，不推进会话。"""
        m = hour_multiplier(self._hour())
        self._sleep(self.cfg.get("dismiss_min", 15) * m, self.cfg.get("dismiss_max", 40) * m, "dismiss")


def rnd_sleep(lo=15, hi=25, beat_fn=None, chunk=PACE_BEAT_INTERVAL, label="pace", nap_fn=None, rng=None):
    """随机取一次 (lo,hi) 秒的等待并睡掉。

    #58 T1 的分段（`beat_fn` 给定时才走）：整段等待**只抽一次**随机数、`[限速]` **只打一行**，
    然后把这一次睡眠切成 ≤ `chunk` 的分段，每段之前拍一次点。两条硬约束：
    - 取值口径不变——历史实现是「一次 `uniform(lo,hi)` 再睡」，若改成每段各抽一次，
      N 段之和的分布就不是那个均匀分布了（防风控语义被悄悄改掉，票面边界明令禁止）；
    - 等待总时长不变——分段只切 `time.sleep` 的**调用次数**，末段取余数，各段之和恒等于 t。
    为什么打点只在一行：`[限速] t s` 是「这次等待等了多久」的人读凭证，打 N 行会把一次等待
    念成 N 次等待（会话日志是 runbook 判读的第一现场）。
    为什么段长不加随机：拍点只是本地写一个心跳文件，不产生任何网络/页面活动，风控观测面上
    与一次长睡眠无差别——随机化段长只换来「等待总时长」的二次扰动，不值。

    `nap_fn` 注入真实的秒级睡眠（默认 `time.sleep`）、`rng` 注入随机源（默认全局 random），
    两者都是单测口子：前者用来统计分段序列（不真睡 480s），后者用来钉住取值。
    `beat_fn=None`（默认，含仓内所有老调用点）时逐字节等价于历史行为。"""
    t = (rng or random).uniform(lo, hi)
    print(f"    [限速] {t:.1f}s")
    sleep = nap_fn or time.sleep
    if beat_fn is None:
        sleep(t)
        return t
    rest = t
    while rest > 0:
        seg = min(chunk, rest)
        beat_fn(label)  # 拍点在睡眠**之前**：这一拍把取栈定时器推到段末之后，段内绝不会再冒出一拍
        sleep(seg)
        rest -= seg
    return t


def notify(title, message):
    """macOS 系统通知；发送失败只打印，不中断投递。"""
    print(f"[通知] {title}: {message}")

    def esc(s):
        return str(s).replace('"', '\\"')

    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{esc(message)}" with title "{esc(title)}"'],
            timeout=10,
            check=False,
        )
    except Exception as e:
        print(f"  [通知失败] {e}")


def log_apply(applied_at, job, brand, code, log_path=APPLY_LOG):
    """追加一条成功投递记录（jsonl），供时间分布分析。"""
    record = {"applied_at": applied_at, "job": job, "brand": brand, "code": code}
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def record_history(job, applied_at, history_path=HISTORY, recruiter_active=None):
    """成功投递同步登记 applied_history.json 台账（followup_tracker/gen_greetings learn
    的消费方，全新 clone 无此文件时缺写入方会让编排挂掉）。
    recruiter_active（#32 段 2）可选携带 bossActiveDesc/bossOnline，键在才写、缺省零行为变化。
    文件缺失/损坏→告警后从 {"applied": []} 起步；按 encryptJobId 去重，已登记跳过。"""
    try:
        with open(history_path, encoding="utf-8") as f:
            history = json.load(f)
        if not isinstance(history, dict) or not isinstance(history.get("applied"), list):
            raise ValueError("结构异常")
    except FileNotFoundError:
        print(f"[台账] {history_path} 不存在，从空台账起步")
        history = {"applied": []}
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[警告] {history_path} 损坏（{e}），从空台账重建")
        history = {"applied": []}
    if any(e.get("encryptJobId") == job["encryptJobId"] for e in history["applied"]):
        print(f"[台账] encryptJobId={job['encryptJobId']} 已登记，跳过")
        return
    history["applied"].append(
        {
            "encryptJobId": job["encryptJobId"],
            "jobName": job["jobName"],
            "brandName": job["brandName"],
            "date": applied_at[:10],
            "note": "批量投递",
            **(recruiter_active or {}),
        }
    )
    write_json_atomic(history_path, history)


def hour_distribution(applied_ats):
    """按小时统计投递时间分布，如 {10: 3, 11: 5}。"""
    hours = [datetime.fromisoformat(ts).hour for ts in applied_ats]
    return dict(sorted(Counter(hours).items()))


def load_excluded_companies(path=EXCLUDED_COMPANIES):
    """简历出现过的公司关键词（用户约定 2026-09-10：命中 brandName 即跳过不投）。
    列表存 private/data/excluded_companies.json（个人履历信息不入 git）；
    文件缺失/损坏时返回空列表，不阻断投递。关键词统一小写做包含匹配。"""
    try:
        with open(path, encoding="utf-8") as f:
            return [str(x).strip().lower() for x in json.load(f) if str(x).strip()]
    except FileNotFoundError:
        print(f"[提示] 未找到 {path}，简历公司过滤未启用")
        return []
    except Exception as e:
        print(f"[警告] 简历公司列表损坏（{e}），本次不过滤")
        return []


def is_excluded_company(brand, keywords):
    """brandName 是否命中简历公司关键词（不区分大小写包含匹配）。"""
    b = str(brand or "").strip().lower()
    return bool(b) and any(k in b for k in keywords)


def is_platform_daily_limit(greet_str):
    return "120" in greet_str and "沟通" in greet_str


def handle_code(code, msg, zp_data):
    """错误码处理，返回 (action, detail)。action: ok/pause/skip/stop/wait"""
    if code == 0:
        return ("ok", "成功")
    if code in (35, 36):
        return ("stop", f"滑块/风险高 code={code}，停止投递，需人工处理")
    if code == 38:
        return ("pause", "需登录 code=38，自动暂停投递，请重新登录")
    if code in (37, 1011):
        return ("pause", f"触发风控码 code={code} ({msg})，自动暂停投递，需人工检查")
    if code == 1006:
        return ("wait", "限速，等10s")
    if code == 31:
        return ("stop", "未登录")
    if code == 32:
        return ("stop", "封禁")
    return ("skip", f"未知 code={code} {msg}")
