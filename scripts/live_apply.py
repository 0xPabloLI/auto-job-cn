#!/usr/bin/env python3
"""
实时浏览投递（live apply，#20 后续增强）：
搜索列表点卡片进详情 → JD 写文件队列 → Trae agent 实时起草招呼语 → 逐字打字发送。
- 导航从搜索列表点击卡片进入（真实浏览轨迹），卡片缺失才回退直跳并计数
- 招呼语不预写：会话中 agent 读 request_N.json 起草，写 reply_N.json 后脚本发送前
  机械核验三道 gate + level（pre_send_gate，违规拦截不发送——确定性不靠 agent 自评）
- 双轨录像（private/data/videos/<ts>/）：全程 webm（context.close 后落盘）+ 动作截图 shots/
- 结束不关窗：轮询 CLOSE 哨兵文件，收到信号（或 Ctrl+C）后退出，视频自动落盘
- 单路径投递（用户约定 2026-09-10）：旧流水线（crawl_jds.py + batch_apply_v2.py）已从
  流程移除并随 #44 物理清理，本脚本是唯一投递入口；节奏/限速共用品在 _limits.py
"""

import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from camoufox.sync_api import Camoufox

from platforms import get_platform
from platforms.chat_first import (
    CHAT_URL as SITE_CHAT_URL,
    JOB_DETAIL_URL,
    JOB_SEARCH_URL,
    click_with_retry,
    collect_new_jds,
    coordinate_click,
    detect_risk_signal,
    detail_post_description as detail_post_description,  # 归因段用同一个对象（`_no_jd` 内亦从此 import）；编排层不再另写第二份取值链
    find_comm_button,
    find_continue_button,
    grab_detail,
    human_read,
    human_scroll,
    human_type,
    is_logged_out,
    search_via_input,
)
from _limits import (  # #44：投递共用工具自 batch_apply_v2.py 迁出（行为不变）
    BOOL_ENV_EXACT_OFF,
    SessionPacer,
    current_hour,
    effective_daily_limit,
    env_bool_switch,
    handle_code,
    is_excluded_company,
    is_platform_daily_limit,
    is_quiet_hour,
    load_config,
    load_excluded_companies,
    log_apply,
    notify,
    record_history,
    rnd_sleep,
    startup_jitter,
)
from _profile_paths import (  # noqa: E402  #28：私有路径唯一出口（废除跨模块路径常量导入）
    browser_profile_dir,
    private_path,
    profile_line,
    resolve_active_profile,
    resolve_kb_profile,
)
from _atomic_io import write_json_atomic
import _edd  # #37 EDD 趋势观测：会话收尾快照的渲染层
import _issue_reporter  # #51 熔断/停批事件建票（HITL 提示 + AUTOJOB_AUTO_ISSUE=1 自动）
import _logging  # #38 机读日志旁路：print 人读层不动，事件旁路落 JSONL（双轨制）
import _forensics  # noqa: E402  #57 第二轮：心跳/取栈取证本体（#56 A 段）拆出 _forensics.py，纯搬迁行为不变
from _forensics import (  # noqa: E402
    # `X as X` 别名把取证符号重新绑回 live_apply 命名空间：调用点沿用原名，
    # `scripts/test_live_apply.py` 里打在这些名字上的**函数**用例也一行不改（搬迁票的验收核心）。
    # 只读常量与函数走别名；**模块级单例状态**（`_HEARTBEAT_DIR` / `_HEARTBEAT_JOB` / `_WEDGE_FH`）
    # 与「被函数体读、被用例 monkeypatch 的数值」（`WEDGE_STACK_SECONDS`）不走别名——见本文件
    # `_arm_session_watchdog` 里那处按模块属性的读法。
    SESSION_DONE as SESSION_DONE,  # 会话目录内：done 退场哨兵（看门狗据此不杀「等用户 CLOSE」）
    SESSION_HEARTBEAT as SESSION_HEARTBEAT,  # 会话目录内：心跳文件（看门狗读 mtime）
    SESSION_WATCHDOG_DEFAULT as SESSION_WATCHDOG_DEFAULT,  # 停摆阈值默认 240s（阈值论证在定义处）
    WATCHDOG_KILLED as WATCHDOG_KILLED,  # 击杀取证文件名（看门狗写、呈报与回灌读；#57 第二轮归位取证段）
    WEDGE_DUMP_ENV as WEDGE_DUMP_ENV,  # LIVE_WEDGE_DUMP 开关名（解析在 `_wedge_dump_enabled`）
    WEDGE_STACK as WEDGE_STACK,  # 会话目录内：楔死现场全线程栈
    WEDGE_STACK_SECONDS as WEDGE_STACK_SECONDS,  # 无心跳多久落栈（读的是 `_forensics` 里的这份）
    _TS_DIR_RE as _TS_DIR_RE,  # 会话目录名形状（回灌扫描与保留期清理共用；#57 第二轮归位取证段）
    # #56 取证 A 的四个登记/往返函数：走别名只读即可（同一个函数对象，状态仍归 `_forensics`，
    # 见顶部注释与本模块 docstring 的「patch 别名是静默 no-op」告诫——计数与注册表**不**导出）。
    handler_roundtrip_enter as handler_roundtrip_enter,  # handler 内一次协议往返开始（自带那一拍心跳）
    handler_roundtrip_exit as handler_roundtrip_exit,  # 同一次往返结束（try/finally 配对）
    listener_attached as listener_attached,  # `page.on("response", h)` 之后的登记
    listener_detached as listener_detached,  # `page.remove_listener` 成功之后的摘除
    MARKER_LAST_EID as MARKER_LAST_EID,  # 击杀取证的岗位身份字段名（单一真源见 `_forensics`）
    MARKER_LAST_JOB as MARKER_LAST_JOB,
    MARKER_LAST_LABEL as MARKER_LAST_LABEL,
    # 击杀相位字段名与两个字面量取值（呈报 `_surface_prior_wedge` 用；两代看门狗写同一份 marker，
    # 读侧不造第二份字面量，否则一代改了另一代还在念旧的）。
    MARKER_PHASE_KEY as MARKER_PHASE_KEY,
    MARKER_PHASE_PREFLIGHT as MARKER_PHASE_PREFLIGHT,
    MARKER_PHASE_SESSION as MARKER_PHASE_SESSION,
    # 出网探针的三格字段名（插页呈报用）：同样不造第二份字面量——写侧在 `_selfguard._probe_egress`，
    # 读侧在这里与 `WATCHDOG_KILLED.json` 的判读人之间只靠这份映射对齐（单一真源见 `_forensics`）。
    NET_PROBE_BLOCKED_KEY as NET_PROBE_BLOCKED_KEY,
    NET_PROBE_MARKER_KEY as NET_PROBE_MARKER_KEY,
    NET_PROBE_PATH_KEY as NET_PROBE_PATH_KEY,
    _arm_wedge_dump as _arm_wedge_dump,
    _cancel_wedge_dump as _cancel_wedge_dump,
    _close_wedge_file as _close_wedge_file,  # 武装句柄释放（`_forensics` 内部与收尾路径共用；live_apply 侧只导出）
    _heartbeat_beat as _heartbeat_beat,
    _heartbeat_bind as _heartbeat_bind,
    _heartbeat_job_end as _heartbeat_job_end,
    _heartbeat_job_start as _heartbeat_job_start,
    _heartbeat_payload as _heartbeat_payload,  # 心跳 payload 形状（#57 往返用例经这层别名读，别名因此不是死 import）
    _reset_wedge_dump_state as _reset_wedge_dump_state,
    _touch_heartbeat as _touch_heartbeat,
    _wedge_dump_enabled as _wedge_dump_enabled,
)
from _wedge_ledger import (  # noqa: E402  #57 第一轮：楔死跳岗台账本体（读写/归因/回灌/呈报/读数）拆出，纯搬迁行为不变
    # `X as X` 别名把台账符号重新绑回 live_apply 命名空间：调用点沿用原名，
    # `scripts/test_live_apply.py` 的 monkeypatch 也仍打在 live_apply 上（用例一行不改是本票验收核心）。
    # 例外（#57 第二轮）：**可变状态**不经别名给（别名是导入时的快照，写者读的是自己模块的变量，
    # patch 别名是静默 no-op）——`_HEARTBEAT_DIR` / `_HEARTBEAT_JOB` / `_WEDGE_FH` 一律经
    # `import _forensics` 按模块属性读，测试也直接打在 `_forensics` 上。
    WEDGE_LEDGER_ENV as WEDGE_LEDGER_ENV,
    WEDGE_LEDGER_FILE as WEDGE_LEDGER_FILE,
    WEDGE_LEDGER_OFF_VALUES as WEDGE_LEDGER_OFF_VALUES,
    WEDGE_LEDGER_ON_VALUES as WEDGE_LEDGER_ON_VALUES,
    _backfill_wedge_ledger as _backfill_wedge_ledger,
    _load_wedge_skipped as _load_wedge_skipped,
    _merge_wedge_ledger as _merge_wedge_ledger,
    _new_wedge_stats as _new_wedge_stats,
    _save_wedge_skipped as _save_wedge_skipped,
    _split_already_applied_wedge_items as _split_already_applied_wedge_items,
    _surface_wedge_ledger as _surface_wedge_ledger,
    _wedge_entry_from_marker as _wedge_entry_from_marker,
    _wedge_ledger_stats as _wedge_ledger_stats,
    _wedge_skip_enabled as _wedge_skip_enabled,
)
from _no_jd import (  # noqa: E402  #57 第二轮：no_jd 归因段（#56 A5②）拆出 _no_jd.py，纯搬迁行为不变
    # `X as X` 别名把归因符号重新绑回 live_apply 命名空间：调用点沿用原名，
    # `scripts/test_live_apply.py` 的读数与断言也仍打在 live_apply 上（用例一行不改是本票验收核心）。
    DETAIL_CODE_MESSAGE_CAP as DETAIL_CODE_MESSAGE_CAP,  # 非 0 返回码 message 截断长度（取证段也用）
    DOM_SHAPE_EMPTY_HITS as DOM_SHAPE_EMPTY_HITS,
    DOM_SHAPE_SHORT as DOM_SHAPE_SHORT,
    DOM_SHAPE_TEXT_HITS as DOM_SHAPE_TEXT_HITS,
    DOM_SHAPE_UNKNOWN as DOM_SHAPE_UNKNOWN,
    DOM_SHAPE_ZERO_HITS as DOM_SHAPE_ZERO_HITS,
    NO_JD_BOTH_EMPTY as NO_JD_BOTH_EMPTY,
    NO_JD_CLASS_DESC as NO_JD_CLASS_DESC,
    NO_JD_CLASS_SHORT as NO_JD_CLASS_SHORT,
    NO_JD_CLASSES as NO_JD_CLASSES,
    NO_JD_DOM_SHAPES as NO_JD_DOM_SHAPES,
    NO_JD_NO_RESPONSE as NO_JD_NO_RESPONSE,
    NO_JD_ROUNDTRIP_FAILED as NO_JD_ROUNDTRIP_FAILED,
    _dom_shape_label as _dom_shape_label,
    _dom_side_has_no_body as _dom_side_has_no_body,
    _no_jd_class_label as _no_jd_class_label,
    _no_jd_class_stats as _no_jd_class_stats,
    _no_jd_empty_url as _no_jd_empty_url,
    _no_jd_empty_urls as _no_jd_empty_urls,
    _no_jd_keep_phrase as _no_jd_keep_phrase,
    _no_jd_shape_stats as _no_jd_shape_stats,
    _no_jd_signal_readings as _no_jd_signal_readings,
    classify_no_jd as classify_no_jd,
    no_jd_detail_code as no_jd_detail_code,
    no_jd_dom_shape as no_jd_dom_shape,
)
import gen_greetings as g  # 发送前机械 gate：三道扫描与起草 gate 同一语义（用户约定 2026-09-10）

# 搜索词真源（#29 P0）：只在 profile JSON 的 queries / query_weights（可选）或
# env LIVE_QUERIES 显式设置——代码内不再有默认词表（DEFAULT_QUERIES/QUERY_WEIGHTS 已删，
# 静默回落曾让新人 onboarding 全绿却按 Web3 搜索词投递）；profile 名解析走
# _profile_paths.resolve_kb_profile（#29 P1 收敛点）
PROFILES = ROOT / "private/kb/profiles"  # 共享目录：行业 profile 注册（#28 不隔离）
SESSION_BASE = ROOT / "private/data/live_session"  # legacy 默认；run_live 按解析前缀重算
VIDEO_BASE = ROOT / "private/data/videos"
CLOSE_SENTINEL = "CLOSE"
SKIP = "__SKIP__"  # reply_N.json 写 {"skip": true} → agent 判定岗位不匹配，立即跳过不投
GRACE_REPLY_WINDOW = 90  # #55：agent 起草超时后的宽限复查窗口秒数（票面 60-120s 取中）
GRACE_REPLY_POLL = 5  # #55：宽限期内复查 reply 文件的间隔秒数
RETRY_ATTEMPTS_CAP = 3  # #55：断点台账 attempts 累计封顶——对永久问题岗位不无限重试

# 岗位来源（#33）：搜索与推荐两处来源**共用同一个页面** = 顶栏「职位」`/web/geek/jobs`
# （形态实测见 内部调研笔记（未随本仓分发） §10）。两个模式只差有没有 query：
#   无 query → GET  /wapi/zpgeek/pc/recommend/job/list.json?page=N&pageSize=15   （推荐列表）
#   带 query → POST /wapi/zpgeek/search/joblist.json                              （搜索结果）
#   注意搜索那个端点的**页码在 POST body 里**，URL 只剩 ?_=<ts>——只看 URL 会误判「没有分页」。
# 两个端点响应同形：`zpData.jobList`（每批 15 条）+ `zpData.hasMore`（到底哨兵）。
# 页面靠**滚动加载**追加，故两来源都走 scroll_collect 持续累积——只取第一批等于只吃了
# 页面的一小口（票面「投 100 封」的量原本就卡在这里）。
LIVE_RECOMMEND_URL = "https://www.zhipin.com/web/geek/jobs"
RECOMMEND_XHR_FRAG = "/wapi/zpgeek/pc/recommend/job/list.json"
SEARCH_XHR_FRAG = "/wapi/zpgeek/search/joblist.json"
JOB_LIST_KEY = "jobList"
LIST_SETTLE_SEC = 3  # 首条响应后再等一窗口才开始滚（页面初始化未稳时滚屏不触发追加）

# 滚动采集（#33 第七轮）：两个来源都是滚动加载、每批 15 条，故采集深度不再受「只取第一批」限制。
SCROLL_MAX_ROUNDS = 12  # 单来源单次采集最多滚动轮次（≈180 条；无哨兵时兜底，防跑飞）
SCROLL_IDLE_STOP = 3  # 连续 N 轮零新增即判定到底（hasMore=false 时不必等满）
SCROLL_WAIT_SEC = 8  # 单轮等待新响应的上限（超时即认为本轮没触发加载）
SCROLL_PAUSE = (1.5, 3.0)  # 轮间拟人停顿（秒）
SCROLL_STEPS = (2, 3)  # 每轮滚动次数
SCROLL_STEP_PX = (600, 1000)  # 单次滚动像素区间
COLLECT_SLACK = 3  # 采集目标 = 剩余额度 × 此系数（候选还要过相关性 gate / 去重 / 排除公司）
COLLECT_CAP_DEFAULT = 120  # 单来源单次采集上限默认值；0 = 不限（只受轮次与到底判据约束）
# #65：推荐轮的**消费**配额。与上一行是两件事——`COLLECT_CAP_DEFAULT` 管「看多少条」（采集深度），
# 本常数管「吃多少个」（消费数量）；截断只发生在传给 `_apply_fresh_jobs` 之前，采集读数不受影响。
RECOMMEND_CAP_DEFAULT = 10  # 代价（必须留着）：默认值依据只有 n=1 场——会话 20261009-104050 采集 49 条、遍历 9 个候选全不对口、投 0（约 12 分钟）

# 接口层控制（#44，2026-09-24）：**动作层熔断**，与既有的 risk_blocked 风控熔断并列。
# 区别：风控熔断看「页面有没有弹验证码/安全验证」；这道看「动作链路自己是不是坏了」——
# 2026-09-23 第二次验收实跑实测：16 岗 applied=0，卡片点击 0/16、「立即沟通」0/5 全超时，
# 但页面渲染正常、按钮在场、事后单独复现却能点动（探针 click ok，1318ms/28ms）。
# 这类**静默退化**跑完整个会话才知道，白耗额度与风控暴露面，故按同一模式加闸。
CLICK_FAIL_STOP = 3  # 连续「立即沟通」点击失败达此数即停批
DIRECT_FALLBACK_STOP = 0.6  # 卡片点击失败（直跳回退）占比超此值即停
DIRECT_FALLBACK_MIN = 5  # 直跳率判定的最小样本量（样本太少不许熔断，防前两岗误杀）
PREFLIGHT_CLICK_TIMEOUT = 8000  # 起飞前自检的点击超时（ms）

# 关键词轮导航重试（#35）：goto 单点超时不再终结整个投递会话
KEYWORD_GOTO_RETRIES = 1  # 失败后的重试次数（共 2 次尝试；再多会在系统性故障时白等）
KEYWORD_GOTO_BACKOFF = 5  # 重试前等待秒数基数
KEYWORD_GOTO_JITTER = 3  # 基数上叠加的随机抖动（秒）

# 聊天页输入框加载态重试（#42）：点击沟通成功但输入框延后就绪时轮询，不再一查落空即弃已过 gate 的草稿
FIND_INPUT_ATTEMPTS = 3  # 首查 + 2 次重试
FIND_INPUT_RETRY_WAIT = (2, 3)  # 重试间隔随机区间（秒）


def profile_query_config(profile, profiles_dir=PROFILES):
    """profile JSON 的搜索词配置（#22 Phase 2 机制 + #29 P0 收紧）：
    queries（非空字符串数组，必填）+ query_weights（可选 {词: 权重}，≥2 的词追加一轮）。
    文件缺失 / 坏 JSON / 形态非法 → None，由 parse_queries fail-loud。"""
    p = Path(profiles_dir) / f"{profile}.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    queries = data.get("queries")
    if not isinstance(queries, list) or not queries or not all(isinstance(q, str) and q.strip() for q in queries):
        return None
    weights = data.get("query_weights")
    if not isinstance(weights, dict):
        weights = {}
    return [q.strip() for q in queries], weights


def parse_queries(env, profile=None, profiles_dir=PROFILES):
    """搜索词三级解析（#29 P0 收紧为 fail-loud）：env LIVE_QUERIES 显式设置最优先
    （按原样不带权重）；其次 profile JSON 的 queries（可选 query_weights 高权加轮）。
    profile 缺有效 queries 且未设 env → 打印「怎么填」指引后非零退出——搜索词决定
    「投什么岗位」，绝不静默回落硬编码词表（旧 DEFAULT_QUERIES 是 Web3 专属，随 repo
    分发给每个 clone 用户会让新人全绿却投错工种）。"""
    raw = env.get("LIVE_QUERIES", "")
    queries = [q.strip() for q in raw.split(",") if q.strip()]
    if queries:
        return queries
    profile = profile or resolve_kb_profile(env=env)
    cfg = profile_query_config(profile, profiles_dir)
    if cfg is None:
        raise SystemExit(
            f"[blocked] 投递搜索词未配置（#29 fail-loud）：profile「{profile}」缺有效的 queries 字段。\n"
            f"  修复：在 private/kb/profiles/{profile}.json 加（岗位搜索关键词数组，可选加权）：\n"
            f'    "queries": ["大模型", "RAG", "AIGC"],\n'
            f'    "query_weights": {{"大模型": 2}}\n'
            f'  或本次显式设环境变量 LIVE_QUERIES="词1,词2"。拒绝静默按硬编码词投递。'
        )
    base, weights = cfg
    extra = [q for q, w in ((q, weights.get(q, 1)) for q in base) if isinstance(w, (int, float)) and w > 1]
    return list(base) + extra


def load_applied_ids(history_path=None):
    """已投递岗位 id 集合（applied_history.json 台账）；缺失文件返回空集。"""
    history_path = history_path or private_path("data/applied_history.json")
    try:
        with open(history_path, encoding="utf-8") as f:
            return {a["encryptJobId"] for a in json.load(f).get("applied", [])}
    except FileNotFoundError:
        return set()


GREETING_LEDGER_KEYS = ("added", "overwritten", "skipped")


def record_greeting(job, send_text, applied_at, session_dir=None, idx=None, greetings_path=None, recruiter_active=None):
    """发送成功后回写招呼语台账 greetings.json（#23：与投递动作 1:1，#19 eval 闭环前置）。
    条目结构与投递队列同语义（eval 消费 encryptJobId/brandName/level/greeting）；
    level/addressee 可选读 reply_N.json（agent 起草时附带）；recruiter_active（#32 段 2）可选
    携带 bossActiveDesc/bossOnline 供 #27 项 4 按活跃度分组。返回三种结局字符串：
    "added"（新 eid 追加）/ "overwritten"（同 eid 旧条目被本次真投出字段级更新——旧条可能
    只是从未投出的草稿，#64）/ "skipped"（写盘失败，异常不冒泡）；
    文件缺失/损坏从 [] 起步原子写。"""
    eid = job.get("encryptJobId")
    greetings_path = greetings_path or private_path("data/greetings.json")
    try:
        with open(greetings_path, encoding="utf-8") as f:
            entries = json.load(f)
        if not isinstance(entries, list):
            entries = []
    except (FileNotFoundError, json.JSONDecodeError):
        entries = []
    meta = {}
    if session_dir and idx is not None:
        try:
            meta = json.loads((Path(session_dir) / f"reply_{idx}.json").read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    entry = {
        "encryptJobId": eid,
        "jobName": job.get("jobName"),
        "brandName": job.get("brandName"),
        "level": meta.get("level"),
        "greeting": send_text,
        "addressee": meta.get("addressee", "hr"),
        "bossName": job.get("bossName"),
        "bossTitle": job.get("bossTitle"),
        **(recruiter_active or {}),
        "date": applied_at,
        "source": "live_apply",
    }
    # 非 dict 条目（手工编辑留下的 null 之类）原样留在库里且不参与匹配——台账层绝不反噬投递
    at, old = next(
        ((i, e) for i, e in enumerate(entries) if eid and isinstance(e, dict) and e.get("encryptJobId") == eid),
        (None, None),
    )
    if at is None:
        entries.append(entry)
    else:
        # 票面要「更新」不是整条替换：本次没带值（None）的字段留在原条目上，别静默丢掉
        entries[at] = {**old, **{k: v for k, v in entry.items() if v is not None}}
    try:
        write_json_atomic(Path(greetings_path), entries)
    except Exception as e:  # 台账层绝不反噬投递：写不进就计数呈报，不中断发送链路
        print(f"    [warn] 招呼语台账写入失败，本条文案未入库（{str(e)[:80]}）")
        return "skipped"
    if old is None:
        print(f"    [台账] 招呼语已回写 greetings.json（{len(entries)} 条）")
        return "added"
    print(
        f"    [台账] 招呼语覆盖旧稿 greetings.json（旧 date={old.get('date')} / "
        f"旧 source={old.get('source')} / 旧稿「{str(old.get('greeting') or '')[:40]}」/ 共 {len(entries)} 条）"
    )
    return "overwritten"


# #64 追加验收 5/6：迁移读数四键（两个 legacy 桶普查 + 缺口普查 + 本场新打标数）
GREETING_MIGRATION_KEYS = (g.LEGACY_UNDATED, g.LEGACY_DRAFT_ONLY, "applied_without_greeting", "tagged")


def migrate_greeting_ledger(greetings_path=None, history_path=None, dry_run=False):
    """招呼语台账历史条目打标（#64 追加验收 5，由代码做、不手改数据文件）。
    判据以票面为准：`date` 为空（None/""/缺失）且尚未打过 legacy 标 ⇒ eid 在投递台账里
    打 `legacy_undated`，不在打 `legacy_draft_only`；已有真实 date 的条目一律不动
    （补日期＝伪造 EDD 输入，与「不回填那 27 条正文」同一条理由）。缺口普查只数不回填
    （验收 6）。损坏文件 warn 后原样保留、返回全 0 读数——绝不拿 [] 覆盖读不出的台账。"""
    greetings_path = greetings_path or private_path("data/greetings.json")
    stats = {k: 0 for k in GREETING_MIGRATION_KEYS}
    try:
        with open(greetings_path, encoding="utf-8") as f:
            entries = json.load(f)
        if not isinstance(entries, list):
            entries = []
    except FileNotFoundError:
        entries = []  # 无条可打标，缺口普查照算
    except json.JSONDecodeError as e:
        print(f"[warn] 招呼语台账读不出，迁移跳过、不落盘（{str(e)[:80]}）")
        return stats
    applied = load_applied_ids(history_path)
    tagged = {g.LEGACY_UNDATED: 0, g.LEGACY_DRAFT_ONLY: 0}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("date") or g.is_legacy_greeting(entry):
            continue
        bucket = g.LEGACY_UNDATED if entry.get("encryptJobId") in applied else g.LEGACY_DRAFT_ONLY
        entry["source"] = bucket
        tagged[bucket] += 1
    new_tagged = sum(tagged.values())
    for entry in entries:
        if isinstance(entry, dict) and entry.get("source") in tagged:
            stats[entry["source"]] += 1
    known = {e.get("encryptJobId") for e in entries if isinstance(e, dict) and e.get("encryptJobId")}
    stats["applied_without_greeting"] = len(applied - known)
    stats["tagged"] = new_tagged
    if new_tagged:
        print(
            f"[台账] 招呼语台账历史条目打标 legacy_undated {tagged[g.LEGACY_UNDATED]} 条 / "
            f"legacy_draft_only {tagged[g.LEGACY_DRAFT_ONLY]} 条（EDD 按文案分组已排除）"
        )
        if not dry_run:
            try:
                write_json_atomic(Path(greetings_path), entries)
            except Exception as e:  # 台账层绝不反噬投递：迁移写不进只 warn，读数照常呈报
                print(f"    [warn] 招呼语台账迁移写入失败，打标未落盘（{str(e)[:80]}）")
    return stats


def shot(page, shots_dir, action):
    """动作截图（快速回看轨）：HHMMSS_action.png；失败静默不影响投递。"""
    _touch_heartbeat(f"shot:{action}")  # 截图节点即机械节点：楔死检测的节拍源之一
    try:
        stamp = datetime.now().strftime("%H%M%S")
        path = Path(shots_dir) / f"{stamp}_{action}.png"
        page.screenshot(path=str(path), full_page=False, timeout=8000)
        print(f"    [录像] 截图 {path.name}")
    except Exception as e:
        print(f"    [录像] 截图失败: {str(e)[:60]}")


def request_greeting(session_dir, idx, job, jd_text, recruiter_active=None):
    """写 request_N.json（原子）：岗位上下文 + JD 全文，供 agent 起草。返回请求文件路径。
    recruiter_active（#32 段 2）可选携带 bossActiveDesc/bossOnline，起草时可感知招聘者活跃度。"""
    req = {
        "idx": idx,
        "jobName": job.get("jobName"),
        "brandName": job.get("brandName"),
        "salaryDesc": job.get("salaryDesc"),
        "cityName": job.get("cityName"),
        "encryptJobId": job.get("encryptJobId"),
        "bossName": job.get("bossName"),
        "bossTitle": job.get("bossTitle"),
        "jdText": jd_text,
        "requested_at": datetime.now().isoformat(timespec="seconds"),
    }
    if recruiter_active:
        req.update(recruiter_active)
    path = Path(session_dir) / f"request_{idx}.json"
    write_json_atomic(path, req)
    print(f"    [队列] JD 已写 {path.name}，等待 agent 起草…")
    return path


def read_reply(session_dir, idx):
    """读 reply_N.json → dict；文件缺失/损坏/非 dict 返回 None（由调用方决定语义）。"""
    path = Path(session_dir) / f"reply_{idx}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def pre_send_gate(send_text, level, whitelist, notes="", sections=None, state_evidence=None):
    """发送前机械核验（用户约定 2026-09-10：确定性不靠 agent 自评）：
    level 合法 + 四道 gate（AI 痕迹/内部标记/运行时状态词/白名单规则）+ 事实来源备注
    （#27 项 3）全 CLEAN。
    返回违规列表（空 = 放行）；whitelist=None（无 fact_cards）时白名单项跳过，
    空白名单（cards 存在但没解析出实体）不跳过——fail-closed，拦截并暴露配置问题。
    notes 备注区：每条 claim 的来源（§N/repo/链上/日期）须记入 reply_N.json 的 notes，
    缺失即拦截；引用的 §编号须在 sections（profile_background 小节编号集合）内，
    sections=None（来源文件缺失）仅跳过存在性核验，不豁免备注非空。
    state_evidence（#34）：正文写现在时状态断言（「运行至今/持续运行/在跑」）时须在此记
    近期实测（≤7 天，含日期，如「链上 polygonscan 2026-09-14 调用确认」）才放行——KB 里
    通常只有部署快照，现状断言不实测就是脑补；快照式过去时无需佐证。
    agent 起草自检是快路径，这里是脚本硬门——违规草稿绝不发出。"""
    problems = []
    if level not in ("高", "中", "初"):
        problems.append(f"level 非法: {level!r}（须为 高/中/初）")
    problems += [f"AI痕迹:{w}" for w in g.ai_fingerprint_scan(send_text)]
    problems += [f"内部标记:{w}" for w in g.internal_marker_scan(send_text)]
    problems += [f"未实测状态词:{w}" for w in g.runtime_state_scan(send_text, state_evidence)]
    if whitelist is not None:
        problems += [f"白名单外:{v}" for v in g.rule_scan(send_text, whitelist)]
    notes_text = notes if isinstance(notes, str) else ""  # 非 str 形态按缺失拦，不崩中途回
    if not notes_text.strip():
        problems.append("来源备注缺失:每条 claim 的来源（§N/repo/链上/日期）记入 reply notes 备注区")
    elif sections is not None:
        unknown = sorted({int(n) for n in re.findall(r"§(\d+)", notes_text)} - sections)
        if unknown:
            problems.append(f"备注引用不存在的小节: §{', §'.join(map(str, unknown))}（profile_background 无此编号）")
    return problems


def profile_sections(path):
    """profile_background.md 的小节编号集合（## N. 标题行）。文件缺失或无编号小节返回
    None——调用方跳过 §存在性核验并打 warn（与 whitelist 不可用同语义），备注非空仍强制。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    nums = {int(n) for n in re.findall(r"^## (\d+)\.", text, re.M)}
    return nums or None


# 称呼规范 v3（用户约定 2026-09-10，apply-runbook §2）可确定性核验的名字形态
SALUTATION_HONORIFICS = ("先生", "女士", "总", "老师", "经理")
HEADHUNTER_TITLE_KEYWORDS = ("猎头", "经纪人")


def salutation_warnings(contact_name, contact_title, send_text):
    """称呼规范 v3 的确定性子集核验（#27 项 1）。**软拦截**：返回 warning 列表由调用方
    打印，不阻断发送——猎头「无把握兜底姓+总」是合法分支、名字性别推测（岑建明→
    先生/女士）不可代码化，规范只覆盖确定性子集，误拦正确草稿的代价大于提醒价值。
    规范未定义的形态（Peter王 等混合名）不检查。"""
    name = (contact_name or "").strip()
    text = send_text or ""
    if not name:
        return [] if "您好" in text else ["无姓名应「您好，」抬头"]
    if re.fullmatch(r"[A-Za-z][A-Za-z .'\-]*", name):
        return [] if "您好" not in text else [f"纯英文名应英文抬头（Hi {name}），不加「您好」"]
    # 平台装饰前缀（A商务-李经理）取含敬称的末段比对
    form = re.split(r"[-_·]", name)[-1].strip()
    if form.endswith(SALUTATION_HONORIFICS):
        return [] if form in text else [f"已带敬称「{form}」应原样保留"]
    if not re.fullmatch(r"[\u4e00-\u9fff]{2,4}", name):
        return []  # 混合形态等规范未定义输入不检查
    if name in text:
        return ["中文姓名不叫全名（应用姓+敬称）"]
    if any(k in (contact_title or "") for k in HEADHUNTER_TITLE_KEYWORDS):
        return []  # 姓+先生/女士 或兜底 姓+总 均合法，性别推测归 runbook 约束
    # 公司招聘者中文姓名：姓+总（复姓容错 欧阳雪→欧阳总），不做性别推测
    forms = (name[0] + "总", name[:2] + "总")
    if not any(f in text for f in forms):
        return [f"公司招聘者中文姓名应「{name[0]}总」抬头（不做性别推测）"]
    return []


# 软问题句式（2026-10-16「结尾只带软问题」约定的唯一放行面，apply-runbook §2）：只问
# 「我方材料能不能发过去／双方下一步」——对方查自己的收件箱和日程就能答，不需要知道团队现状。
# 方向与 pre_send_gate 一致：**默认报，白名单放行**。历史 45 条实发按此判据报 43 条、放行 2 条
# （放行的正是「方便的话我把…发您看？」这类），因为旧规范要求的恰恰是这里拦的知识型提问。
# 误报代价是一行 warn 不阻断，漏报代价是继续给 HR 发他答不上来的问题。
_SOFT_QUESTION_RES = (
    re.compile(r"方便.{0,16}(?:发|看|聊|说|给|约|沟通|过来|时间)"),
    re.compile(r"(?:这岗|这个岗位|该岗|岗位).{0,12}(?:还在|还看|还招|在招|在看人|开放|招)"),
    re.compile(r"(?:简历|网站|repo|地址|链接).{0,14}(?:发|给|需要|要不要)"),
    re.compile(r"(?:您|你)(?:这边)?方便(?:的)?(?:时间|时候)"),
)
_QUESTION_SENTENCE_RE = re.compile(r"[^。！？?]*[？?]")  # 逐问句切段：？」之后的下一句要单独判，不能整段放行


def question_warnings(send_text):
    """「结尾只带软问题」规范的确定性核验（用户约定 2026-10-16，apply-runbook §2）。
    **软提醒不阻断**（同 #27 项 1 称呼门一个模子）：逐个问句切段，问句不匹配任何软问题句式
    ＝知识型提问（要对方具备工程知识或内部信息才答得上来）→ 报命中句。
    只回答「这句问的是不是对方本人就能答的事」，不判问题写得好不好。"""
    out = []
    for sent in _QUESTION_SENTENCE_RE.findall(send_text or ""):
        q = sent.strip()
        if not q or any(p.search(q) for p in _SOFT_QUESTION_RES):
            continue
        out.append(f"知识型提问（对方答不上来）：{q[:40]}")
    return out[:2]


def dry_run_stop(results, idx, job, send_text, salu, qwarn=()):
    """冒烟 dry-run 截停（#40）：发送前 gate 已放行的岗位记 dry_run_ok 后立即结束会话——
    不点「立即沟通」、不发送、不写台账（log_apply/record_history/record_greeting 全不发生，
    零额度消耗零对话建立）。返回 "stop" 供调用方结束循环：冒烟一岗走到 gate 即达目的，
    限量语义由「第一份过 gate 的草稿」承载，不靠数字上限。
    两道软提醒（称呼/提问）都记进条目——冒烟是它们唯一的无真机执行痕迹来源。"""
    print("  [dry-run] 发送前 gate 全过——冒烟到此为止：不点沟通、不发送、不写台账")
    results.append(
        {
            "idx": idx,
            "job": job.get("jobName"),
            "brand": job.get("brandName"),
            "status": "dry_run_ok",
            "send_text": send_text,
            "salutation_warns": salu,
            "question_warns": list(qwarn),
        }
    )
    return "stop"


def _read_reply_text(path):
    """reply_N.json → send_text / SKIP / None（缺失/损坏/空白视为未回复）。
    纯读取不打印——wait_reply 主轮询与宽限复查（#55）两阶段共用。"""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("skip"):
            return SKIP
        text = (data.get("send_text") or "").strip()
        return text or None
    except Exception:
        return None  # 写一半的 JSON：继续等


def wait_reply(
    session_dir,
    idx,
    timeout=600,
    poll=3,
    sleep_fn=time.sleep,
    now_fn=time.time,
    grace=GRACE_REPLY_WINDOW,
    grace_poll=GRACE_REPLY_POLL,
):
    """轮询 reply_N.json 直到出现合法 send_text 或超时（返回 None）。
    {"skip": true} 返回 SKIP 哨兵（agent 判定岗位不匹配）。
    损坏/空白文件（agent 写一半）不判定，继续轮询到超时。
    超时后不立即翻篇（#55 部分一）：再宽限复查 grace 秒（每 grace_poll 秒查一次）——
    迟到草稿在窗口内送达即正常消费（调用方无感，日志标「救回」）；窗口过仍无 →
    「已作废」。背景：2026-09-26 实测晚 94s 送达的 reply 被静默丢弃；grace=0 退回旧语义。"""
    path = Path(session_dir) / f"reply_{idx}.json"
    t0 = now_fn()
    while now_fn() - t0 < timeout:
        _touch_heartbeat(f"wait_reply:{idx}")  # agent 起草等待是合法长停，逐拍豁免误杀
        got = _read_reply_text(path)
        if got is not None:
            if got == SKIP:
                print("    [队列] agent 判定岗位不匹配，跳过")
            else:
                print(f"    [队列] 收到 agent 草稿（{len(got)} 字）")
            return got
        sleep_fn(poll)
    print(f"    [队列] 等待 agent 回复超时（{timeout}s）——宽限复查 {grace}s（每 {grace_poll}s 查一次，#55）")
    t1 = now_fn()
    while now_fn() - t1 < grace:
        _touch_heartbeat(f"wait_reply:{idx}")  # 宽限复查同样逐拍
        got = _read_reply_text(path)
        if got is not None:
            print("    [宽限] 迟到草稿在宽限窗口内送达——已被救回，岗位正常处理")
            if got == SKIP:
                print("    [队列] agent 判定岗位不匹配，跳过")
            else:
                print(f"    [队列] 收到 agent 草稿（{len(got)} 字）")
            return got
        sleep_fn(grace_poll)
    print("    [队列] agent 回复已作废（超时 + 宽限窗口均无果），跳过该岗位")
    return None


def reply_reason(session_dir, idx):
    """读 reply_N.json 的 skip reason（用户约定 2026-09-10）：agent_skip 的原因要落进
    report.json，会话结束按类反馈「哪类投了/哪类没投」。读失败返回空串。"""
    try:
        p = Path(session_dir) / f"reply_{idx}.json"
        return str(json.loads(p.read_text(encoding="utf-8")).get("reason") or "")[:150]
    except Exception:
        return ""


# --- #55 部分三：断点台账（retry_pending.json）——跨会话补投的自包含状态 ---

_RETRY_SNAPSHOT_FIELDS = ("encryptJobId", "jobName", "brandName", "salaryDesc", "cityName", "bossName", "bossTitle")
# #56 A5②纠偏：旧值「no_jd: 详情不可达（岗位疑已失效）」是给静默吞异常扣的定性帽子——
# 同一处往返失败与岗位失效根本是两件事，而该口径还让 `apply_retry_outcome` 把站点抖动
# 造成的 no_jd 当失效岗**静默移出补投队列**（票面点名的那个动作）。
# 现只保留真正有「无内容」证据的那一类。
_RETRY_REMOVE_DESC = {
    "applied": "已投出",
    "agent_skip": "agent 跳过",
    "skipped": "已沟通过",
    "no_jd_dom_xhr_both_empty": NO_JD_CLASS_DESC[NO_JD_BOTH_EMPTY],
    "attempts_cap": f"累计 {RETRY_ATTEMPTS_CAP} 次补投未成",
    # #56 B1：这是票面批判的「静默移出补投队列」（旧 `apply_retry_outcome` 的 no_jd 分支）的
    # **反面**——移出理由带证据（楔死台账里有它）、带播报（终端一行 + 结果条目 + report 读数），
    # 并指得出台账文件供复核。
    "wedge_skipped": "楔死台账命中（该岗在详情入口反复楔死，为止损不再投——见 wedge_skipped.json，代价：可能漏投一个达标岗）",
}


def _retry_snapshot(job, attempts=0, jd_text=""):
    """断点台账条目：岗位字段 + jdText 快照 + timeout_at/attempts——**自包含**，
    不依赖会话目录存活（票面明定字段集）。"""
    snap = {k: job.get(k) for k in _RETRY_SNAPSHOT_FIELDS}
    snap.update({"jdText": jd_text, "timeout_at": datetime.now().isoformat(timespec="seconds"), "attempts": attempts})
    return snap


def _request_jd_text(session_dir, idx):
    """读 request_N.json 的 jdText 快照（超时岗位入台账时抄录）；缺失返回空串。"""
    try:
        p = Path(session_dir) / f"request_{idx}.json"
        return str(json.loads(p.read_text(encoding="utf-8")).get("jdText") or "")
    except Exception:
        return ""


def _load_retry_pending(path):
    """读台账 → list；缺失返回 []；坏形态回落空列表 + warn（#51 reported 同款自愈，不静默）。"""
    try:
        p = Path(path)
        if not p.exists():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("not a list")
        return [e for e in data if isinstance(e, dict)]
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 断点台账读取失败，按空台账起步（{str(e)[:80]}）: {path}")
        return []


def _save_retry_pending(path, entries):
    """原子写台账。"""
    write_json_atomic(Path(path), entries)


def apply_retry_outcome(entry, status, jd_class=None):
    """台账处置路径（#55 验收 3 + #56 A5③，纯函数——直接改 entry["attempts"]）：
    - applied / agent_skip / skipped（已沟通过）→ 移出；
    - wedge_skipped（#56 B1）→ **移出**：一个从此不投的岗没理由留在补投队列里。与票面批判的
      「no_jd 静默移出补投队列」（旧 `apply_retry_outcome` 的 no_jd 分支）的区别是这条有证据
      （楔死台账里有它）、有播报（调用方打一行 + 结果条目 + report 读数），且 attempts 不递增
      （它没获得补投机会，不该烧 attempts）；
    - no_jd **按归因类分流**（#56 A5③，票面：旧口径把站点抖动造成的 no_jd 从补投队列里
      静默移除，是反向作用）：
        · dom_xhr_both_empty（code=0 的详情响应已到、按 `grab_detail` 的同一条取值链取不到 JD
          正文，且 DOM 探测有证据、也没有正文＝岗位确实无内容）→ 移出；
        · detail_roundtrip_failed / detail_no_response（站点抖动，岗位本身仍有效）→ attempts+1 保留；
        · 类别缺失 / 未知（桩数据、旧结果条目、容器形态诡异）→ **同样保留**：宁可多给一次
          补投机会，不可静默丢岗（#41/#42 既定裁决「漏投比错投代价高」，与 classify_no_jd
          的保守方向一致——只有拿到「无内容」的正向证据才允许丢岗）；
    - 其余（agent_timeout / no_btn / no_input / gate_blocked 等岗位仍有效但本次没投成）
      → attempts+1 保留；
    - attempts 达 RETRY_ATTEMPTS_CAP → 移出（告警由调用方打）——抖动型 no_jd 也因此有出口，
      保留不等于无限重试。
    返回 (keep, reason)。"""
    if status in ("applied", "agent_skip", "skipped"):
        return False, status
    if status == "wedge_skipped":  # #56 B1：楔死台账内的岗不再补投（有播报的移出）
        return False, "wedge_skipped"
    if status == "no_jd" and jd_class == NO_JD_BOTH_EMPTY:
        return False, f"no_jd_{jd_class}"
    attempts = int(entry.get("attempts") or 0) + 1
    entry["attempts"] = attempts
    if attempts >= RETRY_ATTEMPTS_CAP:
        return False, "attempts_cap"
    return True, "kept"


# skip 原因粗分类（顺序敏感：先命中先归类）。reason 是 agent 起草时的自由文本，
# 关键词归大类供会话反馈用；未命中归 other。
REASON_RULES = [
    ("keyword_noise", ("误中", "完全不符")),  # 「合约」误中法务/建筑/销售
    ("job_type", ("实习", "零工", "兼职", "临时")),
    ("hard_lang", ("Java", "Golang", "无 Go", "Go 为硬", "Rust", "C++", "Flutter", "PHP")),
    ("salary_band", ("低于", "段位", "偏低", "薪资")),
    # 注意：不含「产品经理/PM」——AI 产品经理在目标岗位族内（用户 2026-09-12 拍板），
    # 传统非技术 PM 由 agent 按 JD 逐个以其他理由跳过
    ("non_eng_role", ("商务", "销售", "运营", "BD", "工程师定位", "求职定位")),
    ("domain_gap", ("领域", "智驾", "座舱", "车载", "攻防", "训练栈")),
    ("senior_gap", ("CTO", "团队管理", "负责人")),
]


def classify_reason(reason):
    for key, kws in REASON_RULES:
        if any(kw in reason for kw in kws):
            return key
    return "other"


def wait_close_sentinel(sentinel_path, poll=5, sleep_fn=time.sleep):
    """轮询 CLOSE 哨兵文件；出现即返回。Ctrl+C 由上层捕获（KeyboardInterrupt 同样退出）。"""
    sentinel = Path(sentinel_path)
    print(f"[保持] 浏览器保持打开；写 {sentinel} 结束会话并保存视频（Ctrl+C 亦可）")
    while not sentinel.exists():
        sleep_fn(poll)
    print("[保持] 收到 CLOSE 信号")


def _noop_beat(_label):
    """beat_fn 的默认替身（#56 A3）：不注入时节拍是纯 no-op——「默认 None = 零行为变更」
    这条保证的实现体（与 platforms/chat_first.py 的 `_no_beat` 同一角色，各自持有，不让平台层
    反向依赖编排层）。"""


def enter_detail(page, eid, goto_fn, beat_fn=None):
    """从列表点卡片进详情（真实浏览轨迹）。卡片缺失/点击失败回退直跳。
    返回导航方式 "card" | "direct_no_card" | "direct_click_failed"。
    #44：点卡片先用坐标点击（coordinate_click）——列表分栏视图上 elementHandle.click
    的 scrollIntoViewIfNeeded 永不返回（实测 16/16 超时而几何全正常）；坐标点击实测
    33ms 点动。坐标失败再试一次 handle.click 作双通道兜底，仍失败才直跳。
    #50：区分真实点击失败（"direct_click_failed"）与无卡片直跳（"direct_no_card"），
    消除推荐流/分栏视图下无卡片数据态导致的假阳性熔断。
    #56 A3：本函数开头那句 `page.query_selector` 是 card 分支里**第一个** timeout=None
    的裸奔协议调用（票面机制口径 §1，纠正旧表述「第一个是 coordinate_click 的 scrollIntoView」），
    5/5 场楔死都落在「岗头心跳之后、detail-open 截图之前」这一段——于是段内每个调用
    （query_selector / 卡片点击 / 回退 goto）前后各拍一次点，label 里显式带 pre/post。
    beat_fn 由调用方注入（见 `_heartbeat_beat`），默认 None = 零行为变更。"""
    beat = beat_fn or _noop_beat
    card = None
    beat("detail:query-card:pre")
    try:
        card = page.query_selector(f"a[href*='{eid}']")
    except Exception:
        card = None
    beat("detail:query-card:post")
    if card:
        beat("detail:card-click:pre")
        try:
            if click_with_retry(page, card, action="职位卡片", beat_fn=beat):
                beat("detail:card-click:post")
                time.sleep(random.uniform(3, 6))
                return "card"
        except Exception as e:
            print(f"    [导航] 卡片点击失败，回退直跳: {str(e)[:60]}")
        beat("detail:direct-goto:pre")
        goto_fn(JOB_DETAIL_URL.format(eid=eid), wait_until="domcontentloaded", timeout=30000)
        beat("detail:direct-goto:post")
        time.sleep(random.uniform(2, 4))
        return "direct_click_failed"
    beat("detail:direct-goto:pre")
    goto_fn(JOB_DETAIL_URL.format(eid=eid), wait_until="domcontentloaded", timeout=30000)
    beat("detail:direct-goto:post")
    time.sleep(random.uniform(2, 4))
    return "direct_no_card"


# 新版职位页分栏视图（/web/geek/jobs?query=…）的**右栏详情**锚点（#33 第八轮探针实测）。
# 该形态 URL **不含 job_detail**，故 #41 的 URL 判据在此恒 False——误判「被弹回」→ 主动重进，
# 把右栏连同「立即沟通」按钮一起毁掉。改判「右栏是否承载本岗」。
JOB_PANEL_TITLE_SELECTORS = [
    ".job-detail-info .job-name",
    ".job-detail-header .job-name",
    ".job-detail-info",
    ".job-detail-header",
]

PANEL_TITLE_JS = """(sels) => {
    for (const s of sels) {
        for (const el of document.querySelectorAll(s)) {
            const r = el.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) continue;
            const t = (el.innerText || '').trim();
            if (t) return t.slice(0, 120);
        }
    }
    return '';
}"""


def job_panel_title(page):
    """分栏视图右栏的标题文本；非分栏形态/无右栏/取不到一律返回 ""（保守，不误判）。"""
    try:
        return page.evaluate(PANEL_TITLE_JS, JOB_PANEL_TITLE_SELECTORS) or ""
    except Exception:
        return ""


def on_job_detail(page, eid, job_name=None):
    """当前页是否仍在该岗位的详情（#41 判据 + #33 第八轮扩展为**形态无关**）。

    实测两种形态（探针 3/3 一致）：
    - **独立详情页** `/job_detail/{eid}.html`：URL 含 job_detail 且含本岗 eid ⇒ 命中
      （实测**不会**被重定向走，URL 就是它）；
    - **分栏视图** `/web/geek/jobs?query=…`（URL **不含** job_detail）：改判**右栏是否承载本岗**
      —— 右栏标题以本岗 jobName 开头即算在详情。校验岗位名而非仅「有右栏」，防误判别岗。
    取不到右栏标题（含桩对象/异常）⇒ 保守返回 False。"""
    url = getattr(page, "url", "") or ""
    if "job_detail" in url and eid in url:
        return True
    title = job_panel_title(page)
    if not title:
        return False
    if job_name:
        return title.startswith(job_name)
    return True


# 聊天页形态的 URL 锚：自平台层 `CHAT_URL`（platforms/chat_first.py）取 path，单一真值来源——
# 生产端改了 URL，锚跟着走，不必记着改第二处（用例另有 `CHAT_URL_MARK in chat_first.CHAT_URL` 钉住）。
# 用 path 而非整条 URL 是按仓内先例（`safe_go_back` 用「URL 含 /chat」判已偏离列表）：判形态
# 不该绑死 host，而真实聊天页就是 `…/web/geek/chat?id=<招聘者 id>`。
CHAT_URL_MARK = urlsplit(SITE_CHAT_URL).path


def _in_chat_form(page, eid, job_name):
    """#62 打字前的**形态**判据：此刻还在不在本岗的沟通/详情形态。

    两种形态是**并集**，少认一种就把正常投递全挡死（比本票要修的 bug 更糟）：
    - `on_job_detail(page, eid, job_name)`（#41/#33 第八轮的形态无关判据：独立详情页 URL 含
      job_detail+eid，或分栏视图右栏标题以本岗 jobName 开头）；
    - **聊天页** `/web/geek/chat?id=<招聘者用户 id>`：URL 带的是**招聘者**的 id，
      **不含本岗 encryptJobId** ⇒ `on_job_detail` 在此恒 False。而「点沟通直接进聊天页」
      是实测存在的正常形态（#4 峰云岗：输入框是 div[contenteditable]）⇒ 只认上一条判据
      就会把所有这类岗误挡。

    URL 取不到（异常／空）或两种判据都不成立 ⇒ 保守判「不在沟通形态」——与 `on_job_detail`
    取不到右栏标题返回 False 同向：这里的假阳性代价是「把招呼语打进别的框」，
    假阴性代价只是「本岗少投一次」，而后者能被 #55 台账捞回。"""
    try:
        url = getattr(page, "url", "") or ""
    except Exception:
        return False
    if CHAT_URL_MARK in url:
        return True
    try:
        return bool(on_job_detail(page, eid, job_name))
    except Exception:
        return False


def open_standalone_detail(page, eid):
    """直跳独立详情页 `/job_detail/{eid}.html`（#33 第八轮新增的稳定兜底）。

    探针实测：该形态仍是旧版组件，`COMM_BTN_SELECTORS` 的旧组 3/3 命中，且 URL 恒含
    `job_detail`+eid（判据稳定）。相较 #41 原来的「回列表点卡片重进」——后者在吸顶筛选栏
    下 `click` 会被拦截（8s 超时），且点中后渲染回分栏视图，兜底效果不稳定。"""
    page.goto(JOB_DETAIL_URL.format(eid=eid), wait_until="domcontentloaded", timeout=30000)
    time.sleep(random.uniform(2, 4))


def find_comm_button_with_recovery(page, eid, job_name=None, beat_fn=None):
    """沟通按钮查找 + 详情兜底（#41，第八轮按实测重写）。

    **进入 `/web/geek/jobs?query=X` 时右栏会自动渲染第一张卡片的详情**（探针实测：目标卡片
    缺失时右栏显示的是别岗）。`find_comm_button` 是全局查找，在分栏视图里会命中**右栏那个岗**
    的按钮 ⇒ 若右栏不是本岗，直接点就是**误投别岗**。故先校验右栏归属：

    1. 右栏有标题且**不是本岗** → 直跳本岗独立详情页（换形态，按钮归属确定）；
    2. 首查 `find_comm_button`：自第八轮起选择器已覆盖两种形态（旧版 `btn-startchat` 组 +
       新版分栏 `op-btn-chat` 组），正常情况首查即中；
    3. 首查落空只兜底一次：直跳独立详情页再找一次（不再走「回列表点卡片重进」——卡片点击会被
       吸顶筛选栏拦截、且点中后渲染回分栏视图，兜底效果不稳定）。仍在详情却落空 ⇒ DOM 又漂移，
       直跳换形态是唯一还能试的动作；不在详情 ⇒ 起草期间被弹回。两情形收敛到同一动作，只打不同日志。
    仍落空返回 None，由调用方 no_btn 存证（#43：存证 URL 如实反映位置）。
    #56 修复轮 F4②：`find_comm_button` 里每一次 `query_selector_all` 都是票面 §1 点名的
    无超时裸奔调用，A3 段漏接——beat_fn 由调用方注入并透给下层两次查找（首查与兜底重查），
    默认 None 语义不变、不新增重试、不改查找顺序。"""
    beat = beat_fn or _noop_beat
    title = job_panel_title(page)
    if title and job_name and not title.startswith(job_name):
        print(f"    [导航] 分栏右栏是别岗（{title[:24]}），直跳本岗独立详情页再找按钮")
        open_standalone_detail(page, eid)
    btn = find_comm_button(page, beat_fn=beat)
    if btn is not None:
        return btn
    if on_job_detail(page, eid, job_name):
        print("    [导航] 已在目标岗详情但按钮未命中（DOM 漂移），直跳独立详情页换形态重试")
    else:
        print("    [导航] 未在目标岗详情（起草期间被弹回 / 卡片未进详情），直跳独立详情页重试")
    open_standalone_detail(page, eid)
    return find_comm_button(page, beat_fn=beat)


# --- #62：站点「对搜索结果是否满意？」反馈浮层的识别（纵深防御的第一层）-------------
#
# 现场（private/data/crash/20261009-115327-昱昉网络科技-live-no-send-4.html + 同批 .url.txt，
# 2026-10-09 11:53）：点「立即沟通」后页面**已弹回搜索结果列表** `/web/geek/jobs?…&query=Web3`
# （右栏写「没有找到相关职位，打开 APP，查看全部职位库」），而那份 HTML 里**全页只有一个
# textarea、零 contenteditable**——它就是反馈浮层的
# `<textarea class="input" placeholder="请填写更多反馈意见…">` ⇒ `find_input` 的裸 `textarea`
# 兜底命中它，199 字招呼语被打进了站点的意见反馈渠道。
# 实测祖先链（自该存证逐层抄得，类名照原文，含站点自己拼错的那个）：
#   textarea.input
#     → .input-wrap.input-wrap-textarea.input-wrap-resize-none
#     → .nput-wrap-textarea                    ← 站点拼错的类名，原样照抄，不是本文件的笔误
#     → .c-satisfaction-feedback.clearfix.card-feedback.c-satisfaction-feedback-large
#   同层埋点锚：ka="satisfaction_feedback_submit_click"（提交）/ ka="satisfaction-feedback_close"（关闭）
FEEDBACK_OVERLAY_SELECTORS = [
    ".c-satisfaction-feedback",
    ".c-satisfaction-feedback-large",
    ".card-feedback",
    ".satisfaction-submit-btn",
    ".satisfaction-list",
]
# ka= 埋点锚的两种拼法都得认：提交按钮用下划线、关闭按钮用连字符（实测同一浮层内并存）
FEEDBACK_ANCHOR_PREFIXES = ("satisfaction_feedback", "satisfaction-feedback")
_FEEDBACK_PROBE_ARG = {"sels": FEEDBACK_OVERLAY_SELECTORS, "anchors": FEEDBACK_ANCHOR_PREFIXES, "depth": 32}

# 自元素起沿 parentElement 上溯，命中容器类或 ka= 锚即算在浮层内；自己命中也算
# （提交按钮本身就是 satisfaction-submit-btn，不必有浮层祖先）。返回 1/0 而非布尔，
# 是为了让 Python 侧能把「真的探不出」与「探出个 0」分开（见 _in_feedback_overlay）。
IN_FEEDBACK_OVERLAY_JS = """(el, cfg) => {
    // AUTOJOB_FEEDBACK_PROBE —— #62 反馈浮层探针的标记，单测替身按它分派应答
    let n = el;
    for (let i = 0; n && i < cfg.depth; i++) {
        for (const s of cfg.sels) {
            try {
                if (n.matches && n.matches(s)) return 1;
            } catch (e) {
                /* 选择器不合 CSS 语法：不算命中，继续往上走 */
            }
        }
        const ka = (n.getAttribute && n.getAttribute('ka')) || '';
        for (const a of cfg.anchors) {
            if (ka.indexOf(a) === 0) return 1;
        }
        n = n.parentElement;
    }
    return 0;
}"""


def _in_feedback_overlay(el):
    """元素是否落在满意度反馈浮层内（它自己就是浮层里的提交按钮也算）。

    返回 True 的三种情形：正向命中容器类、命中 `ka=` 埋点锚、**或探测本身失败**。
    探测失败也判 True 的理由（保守方向，与 `on_job_detail` 取不到右栏标题返回 False、
    发送前 gate 拿不到证据不放行同源）：真句柄探不出祖先链的现实形态就是「节点已从 DOM
    上摘掉／执行上下文随导航销毁」＝页面正好在跳走，而这正是本 bug 的现场；此时误打字的
    代价（求职信进了非投递通道、且该岗不进任何台账）高于少投一次——漏投能被 #55 台账捞回，
    打进反馈框的内容捞不回。

    唯一例外：`el` 根本没有 `evaluate`（单测替身、非 DOM 句柄）时判 False——那种调用方上
    不存在可查询的 DOM，判「在浮层里」既无依据也无意义。

    与形态层的分工：`_in_chat_form` 管**整页形态**（弹回列表 ⇒ 打字前守卫直接拦下，压根不去
    问 DOM 要输入框）；本函数管**元素归属**（页面形态没问题、但反馈浮层就嵌在这一页上时，
    靠它把浮层里的控件从 `find_input` / `find_send_button` 的候选里摘掉）。两层各挡一种形态，
    少任何一层都会漏：只有形态层挡不住「沟通页里嵌着反馈浮层」，只有元素层挡不住「弹回列表后
    页面上别的可编辑区」。"""
    probe = getattr(el, "evaluate", None)
    if probe is None:
        return False
    try:
        return probe(IN_FEEDBACK_OVERLAY_JS, _FEEDBACK_PROBE_ARG) != 0
    except Exception:
        return True


def find_input(page, beat_fn=None):
    """详情页/聊天页沟通输入框：兼容 textarea（弹窗）与 contenteditable（聊天页，
    #4 峰云岗实测：部分岗位点沟通直接进聊天页，输入框是 div[contenteditable]）。
    #56 修复轮 F4②：逐个选择器的 `query_selector`（timeout=None）接拍点，
    默认 None = 零行为变更、不改查找顺序。
    #56 修复轮 4（A1）：`except: continue` 的吞异常出口补 `*:error` 拍点——补点前
    「pre 有 post 无」同时表示「卡在这个调用里」与「这个调用抛了异常被吞」，C 段判栈二义。
    #62：命中反馈浮层（站点「对搜索结果是否满意」）里的控件时**视同未命中**继续往下找——
    查找顺序与重试语义（由 `find_input_with_retry` 管）一字未动（#42 的语义属他人领地），
    只在既有「可见性检查」那一格后面加一道排除；裸 `textarea` 兜底照旧保留（真实聊天弹窗就是它）。
    返回值语义不变：找到返回句柄，全族落空返回 None。"""
    beat = beat_fn or _noop_beat
    for n, sel in enumerate(
        [
            "textarea[placeholder*='简短']",
            "textarea[placeholder*='招呼']",
            "textarea[placeholder*='描述']",
            ".chat-input textarea",
            ".chat-input [contenteditable='true']",
            "[contenteditable='true']",
            "textarea",
        ],
        1,
    ):
        try:
            beat(f"find:input:query:{n}:pre")
            el = page.query_selector(sel)
            beat(f"find:input:query:{n}:post")
            if el and el.is_visible() and not _in_feedback_overlay(el):
                return el
        except Exception:
            beat(f"find:input:query:{n}:error")
            continue
    return None


def find_input_with_retry(page, beat_fn=None):
    """#42：聊天页输入框加载态轮询重试——点击沟通成功但输入框延后就绪时，不再一查落空
    即弃已过 gate 的草稿。首查 + FIND_INPUT_ATTEMPTS-1 次重试（间隔 FIND_INPUT_RETRY_WAIT
    随机区间）；耗尽返回 None，由调用方走既有 no_input 存证兜底。
    #56 修复轮 F4②：拍点透进每一次重试（重试次数由常量约束，一字未动）。"""
    for attempt in range(FIND_INPUT_ATTEMPTS):
        el = find_input(page, beat_fn=beat_fn)
        if el is not None:
            return el
        if attempt < FIND_INPUT_ATTEMPTS - 1:
            wait = random.uniform(*FIND_INPUT_RETRY_WAIT)
            print(
                f"    [等待] 输入框未就绪（聊天页加载态），{wait:.1f}s 后重试（{attempt + 1}/{FIND_INPUT_ATTEMPTS - 1}）"
            )
            time.sleep(wait)
    return None


def find_send_button(page, beat_fn=None):
    """发送招呼按钮（与 ChatFirstPlatform.apply 同选择器序列）。
    #56 修复轮 F4②：与 `find_input` 同族的 `query_selector`（timeout=None）接拍点，
    默认 None = 零行为变更、不新增重试、不改查找顺序。
    #56 修复轮 4（A2）：`except: pass` 的吞异常出口补 `*:error` 拍点，判据同 `find_input`。
    #62 纵深防御：满意度反馈浮层的提交按钮**一律不认作发送按钮**——实测它今天只靠文案
    （「提交」）侥幸躲过这四条选择器，站点哪天把文案改成「发送」，`button:has-text('发送')`
    就会把求职信提交进满意度渠道（`[class*='send']` 那条也挡不住按 class 改名的改版）。
    判据复用 `_in_feedback_overlay`：它自己命中 `.satisfaction-submit-btn`／`ka=satisfaction_feedback*`
    锚，或有 `.c-satisfaction-feedback` 祖先，都算不是发送按钮。"""
    beat = beat_fn or _noop_beat
    for n, sel in enumerate(
        ["button:has-text('发送招呼')", "button:has-text('发送')", ".btn-send", "[class*='send']"], 1
    ):
        try:
            beat(f"find:send:query:{n}:pre")
            btn = page.query_selector(sel)
            beat(f"find:send:query:{n}:post")
            if btn and btn.is_visible() and not _in_feedback_overlay(btn):
                return btn
        except Exception:
            beat(f"find:send:query:{n}:error")
    return None


# --- #56 A5：监听器内静默吞异常 → 可观测（容器字段 + 机读事件）---
#
# 同一处 `resp.json()` → `Response.body()` → `send("body", None)` 是**一次无超时协议往返**：
# 它失败（抛异常）过去被 `except Exception: pass` 吞干净，于是 `container["data"]` 永为 None、
# no_jd 台账只写一句「详情不可达（岗位疑已失效）」——票面点名的「零可观测」面。
# handler 跑在 dispatcher 的 EventGreenlet 里，**任何往外抛都会改变协议链路行为**（#56 A 段口径：
# 除 A1 外零行为变更），所以取证只往旁路加、不改控制流；取证自身也全吞（#38 观测层不新增失败面）。

XHR_ERROR_RECORD_CAP = 5  # 单容器错误记录条数上限（一岗可能有多个交错响应，取证不留无界缓冲）


def _observe_xhr_failure(sink, event, resp, url_frag, exc):
    """把一次被吞掉的往返失败落成两处证据：① `sink["errors"]` 里的稳定记录（供 no_jd 归因
    与 report 读数），② 一条 error 级机读事件（供事后回溯「哪条 URL、什么异常」——#48 口径：
    安全/事件类要 error 才不被默认阈值吞掉）。
    sink 为 None（调用方没有可写字典，如 accum 的 flags 缺省）时只落事件，归因侧退化为
    「超时未回」——保守保留补投机会，不因此丢岗。"""
    try:
        url = ""
        try:
            url = str(resp.url)[:300]
        except Exception:  # noqa: BLE001  # 协议链路已断时连 url 都可能取不到，空着而不是再抛
            pass
        rec = {
            "url": url,
            "frag": url_frag,
            "error": f"{type(exc).__name__}: {exc}"[:200],
            "at": datetime.now().isoformat(timespec="seconds"),
        }
        if isinstance(sink, dict):
            errs = sink.get("errors")
            if not isinstance(errs, list):
                errs = []
                sink["errors"] = errs
            if len(errs) < XHR_ERROR_RECORD_CAP:
                errs.append(rec)
        _logging.log_event(event, level="error", **rec)
    except BaseException:  # noqa: BLE001  # 取证失败退回「零可观测」，绝不冒泡进 dispatcher
        return None


def _observe_xhr_nonzero_code(container, resp, url_frag, payload):
    """接口回了非 0 码＝**往返成功、内容失败**（风控 / 岗位失效的返回码），与
    `_observe_xhr_failure` 那种「往返本身抛异常」是两件事，因此三处都分开：
    ① 不写 `errors`（写了就被 `classify_no_jd` 读成往返失败，把假说一的证据量灌水）；
    ② 不写 `data`（写了它就成了「响应到了」的形态，配上 DOM 侧零命中的探测证据就会被判成
      「双空」＝拿一次风控返回码丢一个可补投的岗）；
    ③ 事件名单独一条 `xhr_capture_nonzero_code`——`xhr_capture_failed` 的计数是票面假说一
      （无超时往返失败）的直接读数，并进来就没法把「往返挂了几次」与「接口报了几次错」分开；
      两条同前缀 `xhr_capture_*`，grep 一把仍全中。
    级别仍取 error（#48 口径：非 error 会被默认阈值吞掉，等于没落）——详情接口非 0 就是
    这次取详情失败，不是一条正常流水。
    留痕落 `container["nonzero"]`（后到者覆盖，与 `data` 同一语义）：`code` 原样、`message`
    截断到 `DETAIL_CODE_MESSAGE_CAP`（风控文案可能是整页 HTML）。取证自身全吞、不外抛，
    **不得改变 handler 的控制流**（跑在 dispatcher 的 EventGreenlet 里，同 `_observe_xhr_failure`）。
    #56 修复轮 3 R3（求值次数的契约）：`payload` 由**调用方求值一次后传入**，本函数绝不回头
    再取响应体——`resp.json()` 每一次都是 `Response.body()` → `send("body", None)` 的无超时协议
    往返，同一次往返里多取一次就多一次楔死机会；而第二次取体的下场无论哪种都在改读数：抛出来
    被本函数的 `except BaseException` 吞掉 → 这条留痕与事件**一起丢**（假说一被记轻），
    外抛给 handler → 一次往返落 `xhr_capture_nonzero_code` + `xhr_capture_failed` **两个事件名**
    （被记重）。两种都由「只求值一次」排除，实测锚见 test_live_apply 的
    `test_capture_xhr_reads_the_body_once_and_logs_a_single_event_name`。
    `except BaseException`（而不是 Exception）也是同一条考量：本函数已经是「读现成 payload」，
    里面只剩取证自己的形态问题（`payload` 可能是 None / 非 dict——`isinstance` 守卫已挡），
    让它外抛只会被 handler 的 `except Exception` 接住、再补一个 `xhr_capture_failed`，
    正好落回上面那个双记；宁缺毋造，取证失败就退回「这一条没落成」。"""
    try:
        url = ""
        try:
            url = str(resp.url)[:300]
        except Exception:  # noqa: BLE001  # 协议链路已断时连 url 都可能取不到，空着而不是再抛
            pass
        rec = {
            "code": payload.get("code") if isinstance(payload, dict) else None,
            "message": str(payload.get("message") or "")[:DETAIL_CODE_MESSAGE_CAP] if isinstance(payload, dict) else "",
            "url": url,
            "frag": url_frag,
            "at": datetime.now().isoformat(timespec="seconds"),
        }
        if isinstance(container, dict):
            container["nonzero"] = rec
        _logging.log_event("xhr_capture_nonzero_code", level="error", **rec)
    except BaseException:  # noqa: BLE001  # 取证失败退回「零可观测」，绝不冒泡进 dispatcher
        return None


def capture_xhr(page, url_frag, container):
    """挂 XHR 响应监听（joblist/detail），命中写入 container["data"]。返回监听器以便移除。
    #56 A5：`resp.json()` 抛异常时不再 `pass`——异常落进 `container["errors"]`（稳定三字段
    url/error/at）并落一条 `xhr_capture_failed` 事件；handler 仍然不往外抛（见上）。
    #56 修复轮 F1：`code != 0` 也不再一字不留——那份响应既没进 `data`（只有 code==0 才写）、
    也没进 `errors`（往返本身是成功的），于是「接口自己报了错」与「站点压根没回」在归因上
    混成同一个「超时未回」。现在它落 `container["nonzero"]` + 一条独立事件，控制流一字不动。
    #56 修复轮 3 R3（一次往返一个事件名）：`resp.json()` 是本 handler 里**唯一**的取体点，
    求值一次；非 0 分支把已经拿到的 `j` 直接交给 `_observe_xhr_nonzero_code`，下游不再回头
    取第二次（第二次取体要么把留痕连同事件一起吞掉、要么一次往返落两个事件名，两种都在改读数）。
    `j` 是 None / 非字典时 `j.get` 抛 → 只落一条 `xhr_capture_failed`，helper 侧的 `isinstance`
    守卫原样保留。
    #56 取证 A：`page.on` 之后登记 handler、真正取体的那次 `resp.json()` 前后各包一次
    enter/exit——enter 自带那一拍心跳，于是卡死时 `handlers_inflight=1` 来得及落盘
    （口径见 `_forensics.handler_roundtrip_enter`）。两个铁律一字未动：`resp.json()` 仍**只求值
    一次**（包的是同一条语句，没有第二次取体），异常照旧落进容器 + 事件、绝不外抛
    （`try/finally` 只加了一句收尾，不改控制流）。"""

    def h(resp):
        try:
            if url_frag in resp.url:
                handler_roundtrip_enter(url_frag)
                try:
                    j = resp.json()
                finally:
                    handler_roundtrip_exit()
                if j.get("code") == 0:
                    container["data"] = j
                else:
                    # payload 复用上面那一次求值（#56 修复轮 3 R3）：helper 不得再取响应体第二次
                    _observe_xhr_nonzero_code(container, resp, url_frag, j)
        except Exception as e:  # noqa: BLE001  # #56 A5：静默吞掉 → 容器 + 事件，控制流不变
            _observe_xhr_failure(container, "xhr_capture_failed", resp, url_frag, e)

    page.on("response", h)
    listener_attached(h)  # `on` 成功之后才登记（`on` 抛了就没挂上，登记等于虚报泄漏）
    return h


def capture_xhr_accum(page, url_frag, buf, list_key, flags=None):
    """挂**累积式** XHR 监听（#33）：命中响应把 `zpData.<list_key>` 追加进 buf。
    与 capture_xhr 的唯一差别是不覆盖——列表页初始化会并发拉多个请求（实测同一页
    两个频道 / 首屏与滚动加载交错），覆盖式只留最后一条，先到的整批丢掉（15 条白拿）。

    flags（#33 第七轮）：额外落 `zpData.hasMore` 到调用方持有的字典——两个列表端点都带
    这个到底哨兵，比「滚几轮没动静」更早也更准地判定到底。

    #56 A5：同款吞异常一并可观测化，但事件名单独一条 `xhr_list_capture_failed`——
    列表采集与详情采集是两条不同的证据链（no_jd 归因只吃详情容器），混成一个事件名
    会让「详情往返失败了几次」这个读数被列表噪声污染。错误记录落 flags["errors"]
    （调用方持有的字典；flags 缺省则只落事件）。
    #56 取证 A：与 `capture_xhr` 同法登记 handler + 包住那**一次**取体（求值次数、事件名、
    控制流三条都一字未动）。列表监听器在 `search_list` / `recommend_list` 的 finally 里注销，
    计数也跟着那里摘——两个列表端点交错并发时「在途」可能同时是 2，这正是要读的数。"""

    def h(resp):
        try:
            if url_frag in resp.url:
                handler_roundtrip_enter(url_frag)
                try:
                    j = resp.json()
                finally:
                    handler_roundtrip_exit()
                if j.get("code") == 0:
                    zd = j.get("zpData") or {}
                    items = zd.get(list_key) or []
                    if items:
                        buf.extend(items)
                    if flags is not None and "hasMore" in zd:
                        flags["has_more"] = bool(zd["hasMore"])
        except Exception as e:  # noqa: BLE001  # #56 A5：同上，事件名与容器都不同
            _observe_xhr_failure(flags, "xhr_list_capture_failed", resp, url_frag, e)

    page.on("response", h)
    listener_attached(h)  # 同 `capture_xhr`：`on` 成功后才登记
    return h


def _drop_response_listener(page, handler):
    """注销 response 监听器：**先 remove，成功后才摘计数**。
    为什么是这个顺序：`handlers_live` 是楔死取证里「现场还挂着几个可能持有无超时往返的入口」
    的读数。`page.remove_listener` 自己抛异常（协议链路正在抖——正是 A1 论证过的那种）时计数
    保持「仍挂着」才是诚实读数；先摘后 remove 会把一次泄漏读成健康，等于把取证做成了误导。
    remove 的异常**照原样外抛**：三个既有 finally 调用点（`search_list` / `recommend_list` /
    `_type_and_send_greeting`）的传播语义一字不动，只有 `_detach_detail_listener` 按它自己的
    「吞掉 + 打警告」口径在外面接。"""
    page.remove_listener("response", handler)
    listener_detached(handler)


def dedupe_jobs(listing):
    """按 encryptJobId 去重（保序）：丢弃无 id 项与非法条目。
    跨频道合并后有重复（实测 15+15 → 28 条唯一），不去重会让重复项白占单轮额度。"""
    seen, out = set(), []
    for j in listing:
        if not isinstance(j, dict):
            continue
        eid = j.get("encryptJobId")
        if eid and eid not in seen:
            seen.add(eid)
            out.append(j)
    return out


def _unique_count(buf):
    """唯一岗位数（按 encryptJobId 去重）——**采集目标与「本轮有无新增」的比对口径**。

    #44（2026-09-24 实测定位）：不能用 `len(buf)`。列表页初始化会**并发拉两个频道**，
    两个频道返回的是**同一批** 15 条，原始 buf 因此是 30；拿它跟 target 比，首批就判
    「已够」而滚 0 轮。两次验收实跑日志均为「拿到 15 条（滚 **0** 轮，到底=False）」——
    不是到底，是这个口径错。口径必须和「候选数」一致：候选是去重后的。"""
    seen = set()
    for j in buf:
        if isinstance(j, dict):
            eid = j.get("encryptJobId")
            if eid:
                seen.add(eid)
    return len(seen)


def _scroll_round(page):
    """一轮拟人滚动（2~3 次 scrollBy，每次 600~1000px）：一次滚满整屏容易被判成脚本，
    且部分懒加载钩子挂在中间位置。滚动异常不外抛——本轮没触发加载会由调用方按
    「零新增」自然记账，不值得为一个滚动失败终结采集。"""
    for _ in range(random.randint(*SCROLL_STEPS)):
        try:
            human_scroll(page, random.randint(*SCROLL_STEP_PX))
        except Exception:
            pass
        time.sleep(random.uniform(0.3, 0.8))


def scroll_collect(page, buf, target=0, flags=None):
    """滚到底持续累积列表条目（#33 第七轮）。两个来源的列表页都是滚动加载、每批 15 条，
    取第一批就停等于只吃了页面的一小口。

    停止条件（任一命中）：① 响应带 `hasMore=false`（页面自己说没了）；② 已够 `target`
    条**唯一岗位**；③ 连续 `SCROLL_IDLE_STOP` 轮零新增；④ 滚满 `SCROLL_MAX_ROUNDS` 轮。
    结果写回 flags（`rounds` / `at_bottom`）供会话报告口径使用——不另立返回值，
    调用方的既有「返回列表」签名不变。

    #44（2026-09-24）：② 与「本轮有无新增」都按 **唯一岗位数**（`_unique_count`）而非
    `len(buf)` 判定——buf 是原始累积、含跨频道重复，用它比对会让滚动首批即退（滚 0 轮）。
    返回值仍是原始 buf（去重归消费端，避免掩盖条目形态漂移），此处只改判定口径。"""
    if flags is None:
        flags = {}
    rounds = idle = 0
    have = _unique_count(buf)
    while rounds < SCROLL_MAX_ROUNDS and idle < SCROLL_IDLE_STOP:
        if flags.get("has_more") is False or (target and have >= target):
            break
        before = have
        _scroll_round(page)
        rounds += 1
        _touch_heartbeat(f"scroll:{rounds}")  # 采集滚动轮是单场最长的无截图区段，逐轮报平安
        # 滚动可能已同步触发追加（测试里 SCROLL_WAIT_SEC=0，等待循环一次都不进），
        # 故先无条件重算一次再进入等待——否则「本轮零新增」会被误记为 idle。
        have = _unique_count(buf)
        deadline = time.time() + SCROLL_WAIT_SEC
        while have == before and time.time() < deadline:
            page.wait_for_timeout(300)
            have = _unique_count(buf)
        idle = idle + 1 if have == before else 0
        if idle < SCROLL_IDLE_STOP:
            time.sleep(random.uniform(*SCROLL_PAUSE))
    flags["rounds"] = rounds
    flags["at_bottom"] = flags.get("has_more") is False or idle >= SCROLL_IDLE_STOP
    return flags


def _remember_list_url(page):
    try:
        url = getattr(page, "url", None)
        if isinstance(url, str) and url.startswith("http"):
            setattr(page, "_autojob_list_url", url)
    except Exception:
        pass


def safe_go_back(page, timeout=10000):
    """回列表页（2026-09-24 #44 修复）：
    - 若当前 URL 已经在列表页（分栏视图下卡片点击不跳页，如 /web/geek/jobs），
      绝对不能调用 page.go_back()，否则会把搜索结果弹出历史记录、退回到自检页或空白页，
      导致后续全部岗位找不到卡片而退化直跳（#44 direct_fallback 80% 熔断根因）；
    - 若当前 URL 偏离了列表（如直跳独立详情页 /job_detail/ 或进入聊天页 /web/geek/chat）：
      优先 goto 回列表（SPA 下比 go_back 更可靠，见 commit 0f9d541）；
      若无列表 URL 则容错尝试 go_back。"""
    try:
        current_url = getattr(page, "url", "") or ""
    except Exception:
        current_url = ""

    list_url = getattr(page, "_autojob_list_url", None)

    # 1. 若当前已经在列表页（URL 含 /web/geek/job 且不在独立详情 /job_detail/ 或聊天 /chat）
    if "/web/geek/job" in current_url and "/job_detail/" not in current_url and "/chat" not in current_url:
        if not list_url or current_url == list_url:
            return

    # 2. 当前偏离列表页（如在 /job_detail/ 或 /chat），需恢复到列表页
    if list_url:
        try:
            page.goto(list_url, wait_until="domcontentloaded", timeout=timeout)
            return
        except Exception as e:
            print(f"    [导航] goto 回列表失败，尝试 go_back: {str(e)[:60]}")

    # 3. 兜底 go_back
    try:
        page.go_back(timeout=timeout)
    except Exception as e:
        print(f"    [导航] go_back 超时/失败（忽略，下一轮重新搜索）: {str(e)[:60]}")


def goto_with_retry(page, url, **kwargs):
    """搜索导航局部重试（#35）：goto 超时（网络抖动 / 页面响应慢）等待退避后重试
    KEYWORD_GOTO_RETRIES 次，仍失败抛出最后一次异常，由关键词级边界（keyword_round）
    捕获后跳过该关键词。"""
    last = None
    for attempt in range(KEYWORD_GOTO_RETRIES + 1):
        try:
            page.goto(url, **kwargs)
            return
        except Exception as e:
            last = e
            if attempt >= KEYWORD_GOTO_RETRIES:
                break
            wait = KEYWORD_GOTO_BACKOFF + random.uniform(0, KEYWORD_GOTO_JITTER)
            print(
                f"    [导航] 搜索页加载失败，{wait:.0f}s 后重试（{attempt + 1}/{KEYWORD_GOTO_RETRIES}）: {str(e)[:60]}"
            )
            time.sleep(wait)
    raise last


def search_list(page, query, target=0, flags=None):
    """打开搜索页 + 逐字输入关键词，**滚动累积**返回 jobList 原始条目。

    #35 稳定性：goto 走 goto_with_retry（裸调时一次超时即冒泡终结整轮）；监听器注销
    放进 finally（原实现失败路径漏注销，会泄漏到后续关键词轮）。重试后仍失败则异常
    向上抛出——调用方 keyword_round 记 keyword_skips 后转下一关键词。

    #33 第七轮：该页的搜索结果是**滚动加载**（POST body 带 page，每批 15 条），旧实现只读
    第一条响应 = 每个关键词永远只吃 15 条；现改为首条命中后持续滚到底累积。
    返回值是**原始累积条目**（跨批可能重复）：去重由消费端 `collect_new_jds` 统一负责，
    它同时管已投递、会话级 collected 与单列表内重复；此处若先走 dedupe_jobs，会把
    「条目形态漂移」（非 dict）静默丢掉，掩盖页面改版信号。
    """
    buf = []
    flags = {} if flags is None else flags
    h = capture_xhr_accum(page, SEARCH_XHR_FRAG, buf, JOB_LIST_KEY, flags)
    try:
        goto_with_retry(page, JOB_SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        if not search_via_input(page, query):
            from urllib.parse import quote

            print("    [浏览] 未找到搜索框，回退 URL 直拼")
            goto_with_retry(
                page, f"{JOB_SEARCH_URL}?query={quote(query)}&page=1", wait_until="domcontentloaded", timeout=30000
            )
        time.sleep(random.uniform(4, 7))
        # #58 T1：等首批 XHR 的轮询**逐轮拍点**——改前这里是 60s 零心跳，票面三份假栈里的
        # dump 2 就停在 `while not buf` 这一行的 `wait_for_timeout` 上（合法等待冒充楔死现场）。
        # 只加拍点：轮询间隔 300ms、60s 上界与 `while not buf` 的退出条件一字不动
        # （label 与 `scroll_collect` 的 `scroll:{n}` 同族，轮次从 1 起计，读的人据此数得出等了多久）。
        t0 = time.time()
        xhr_round = 0
        while not buf and time.time() - t0 < 60:
            xhr_round += 1
            _heartbeat_beat(f"search:wait-xhr:{xhr_round}")
            page.wait_for_timeout(300)
        if not buf:
            return []
        time.sleep(LIST_SETTLE_SEC)
        scroll_collect(page, buf, target=target, flags=flags)
    finally:
        _drop_response_listener(page, h)  # #56 取证 A：remove 成功后才摘计数
    return buf


def recommend_list(page, limit=None, target=0, flags=None):
    """打开顶栏「职位」页的**无 query 模式**（推荐列表），滚动累积 → 按 eid 去重 → 上限截断。

    与 search_list 的差异只有一处：不带 query（推荐列表无关键词维度）。导航同样走
    goto_with_retry，异常向上抛由推荐流轮级边界记账；首条命中后再等一窗口，因为页面
    初始化会并发拉请求，覆盖式收集只留最后一条会丢先到的整批。

    形态（实测 2026-09-22，见 内部调研笔记（未随本仓分发） §10）：端点
    `GET pc/recommend/job/list.json?page=N&pageSize=15`，响应 `zpData.jobList` + `hasMore`，
    滚动加载每批 15 条——旧实现只吃第一批，故「池子只有 28 条」的结论是采集深度造成的假象。
    """
    buf = []
    flags = {} if flags is None else flags
    h = capture_xhr_accum(page, RECOMMEND_XHR_FRAG, buf, JOB_LIST_KEY, flags)
    try:
        goto_with_retry(page, LIVE_RECOMMEND_URL, wait_until="domcontentloaded", timeout=30000)
        t0 = time.time()
        xhr_round = 0
        while not buf and time.time() - t0 < 60:
            # #58 T1 对称补漏：与 search_list 同型的 60s 等首批 XHR 循环。60s 单独不破 90s
            # 取栈阈值，但它前面叠着 goto_with_retry（30s goto + 5-8s 退避），最坏可逼近——
            # 合法等待期间必须持续报平安，否则 wedge-stack.txt 又会落一份「不是楔死」的栈。
            xhr_round += 1
            _heartbeat_beat(f"recommend:wait-xhr:{xhr_round}")
            page.wait_for_timeout(300)
        if not buf:
            return []
        time.sleep(LIST_SETTLE_SEC)  # 初始化并发请求到齐后再滚，避免把在途请求算成「本轮零新增」
        scroll_collect(page, buf, target=target, flags=flags)
    finally:
        _drop_response_listener(page, h)  # #56 取证 A：remove 成功后才摘计数
    listing = dedupe_jobs(buf)
    if limit and limit > 0:
        return listing[:limit]
    return listing


def keyword_round(page, query, state, applied_ids, collected=None, target=0):
    """关键词轮（#35 关键词级边界）：搜索列表 → 过滤新岗位。搜索与列表解析链上任何单点
    异常都不得冒泡终结整个投递会话——失败时记 state["keyword_skips"] 并返回 None，
    调用方跳过本关键词继续下一轮（与单岗级 apply_live 的 try/except 构成两级异常边界）。
    返回值用 None 而非 []：与「搜索成功但无新岗位」区分，被跳过的关键词数才可如实汇报。

    collected（#33）：**会话级**已收集列表，由调用方持有并跨轮/跨来源共用——同一个
    encryptJobId 无论来自第几个关键词还是推荐流，都只收集一次（票面验收 6「来源无关
    去重」）。不传则退化为单轮内去重（同 #33 之前的旧行为）。

    target（#33 第七轮）：本关键词的采集目标条数（search_list 滚到够即停）。搜索轮滚动
    轮次累加进 state["search_scrolls"]，供报告口径观察采集深度。
    """
    flags = {}
    try:
        listing = search_list(page, query, target=target, flags=flags)
        fresh = collect_new_jds(listing, [] if collected is None else collected, applied_ids)
    except Exception as e:
        state.setdefault("keyword_skips", []).append({"query": query, "error": str(e)[:150]})
        note_browser_exit(state, str(e))  # #54：browser-closed 类异常在此分类
        print(f"    [跳过] 关键词「{query}」搜索失败（已重试，转下一关键词）: {str(e)[:100]}")
        return None
    state["search_scrolls"] = state.get("search_scrolls", 0) + flags.get("rounds", 0)
    print(f"    拿到 {len(listing)} 条（滚 {flags.get('rounds', 0)} 轮），新岗位 {len(fresh)} 个")
    return fresh


# ---------------------------------------------------------------------------
# #39 存量单体拆分：一次会话 = LiveSessionContext（会话级共享上下文）+ 单岗 _JobFlow
# （流程中间态）。apply_live 保留原有 10 形参公开签名——它是会话主循环与行为测试共用的
# seam，改成别的东西只会把复杂度搬到调用方；内部按 6 阶段串联（进详情 / 阅读预检 /
# 取 JD 等草稿 / 过 gate / 沟通发送 / 落台账），每段可单测、可读、语句数受 ruff 闸门约束。
# ---------------------------------------------------------------------------


@dataclass
class LiveSessionContext:
    """一次投递会话的共享上下文（#39）：会话内不变的东西 + 跨岗累加的 results / state。
    只做聚合不做校验——pacer / platform / results / state 本身就是要跨岗共享的可变对象，
    这里不复制、不包装，避免出现「上下文里的 state 与主循环手里的 state 不是同一个」的坑。"""

    session_dir: Path
    shots_dir: Path
    cfg: dict
    pacer: Any
    platform: Any
    results: list
    state: dict


@dataclass
class _JobFlow:
    """单岗流程的中间态（#39）：6 个阶段共享的可变数据，替代阶段间多返回值互传。"""

    page: Any
    job: dict
    idx: int
    ctx: LiveSessionContext
    detail: dict = field(default_factory=lambda: {"data": None})  # 详情 XHR 容器（grab_detail 消费）
    detail_listener: Any = None  # #56 A1：本岗 detail.json 监听器引用（单岗末尾注销用）
    greeting: str = ""  # 过了发送前 gate 的草稿正文
    recruiter_active: dict = field(default_factory=dict)  # #32 段 2：详情响应旁听提取的 招聘者活跃度字段
    salutation_warns: list = field(default_factory=list)  # 称呼规范软拦截命中项（#27 项 1）
    question_warns: list = field(default_factory=list)  # 「结尾只带软问题」软提醒命中项（2026-10-16）
    input_el: Any = None  # 沟通输入框元素（textarea / contenteditable）
    add_response: dict = field(default_factory=lambda: {"data": None})  # friend/add XHR 容器


def _skip_job(flow, status, **extra):
    """单岗跳过（#39）：结果记账 + 节奏跳过 + 回列表，统一返回 "next"。
    8 个跳过分支形态一致（已沟通 / 无 JD / agent skip / 超时 / gate 拦截 / 无按钮 /
    无输入框 / 无发送按钮），集中一处才不会出现「漏掉 safe_go_back，让下一岗从错误页面
    开始」这类漂移。额外字段（reason / detail）按调用方顺序追加进结果条目。"""
    flow.ctx.results.append({"idx": flow.idx, "job": flow.job.get("jobName"), "status": status, **extra})
    flow.ctx.pacer.wait_skip()
    safe_go_back(flow.page)
    return "next"


def _record_agent_timeout(flow):
    """#55：agent_timeout 记账分流——正常轮次进会话内补投列表（队列耗尽后直跳重试一次）；
    补投轮次（retry_pass 标记）再超时直接转跨会话台账暂存（收尾统一落盘），不回补投
    列表——会话内每岗位最多补投 1 次。"""
    ctx, job = flow.ctx, flow.job
    if ctx.state.get("retry_pass"):
        ctx.state["retry_touched"] = True
        ctx.state["retry_new_count"] = ctx.state.get("retry_new_count", 0) + 1  # #55 收尾统计：本场新增超时
        ctx.state.setdefault("retry_pending", []).append(
            _retry_snapshot(job, attempts=0, jd_text=_request_jd_text(ctx.session_dir, flow.idx))
        )
        print(f"    [补投] 再超时——落入跨会话台账（累计封顶 {RETRY_ATTEMPTS_CAP} 次）: {job.get('jobName')}")
        return
    ctx.state.setdefault("session_retries", []).append({"job": dict(job), "request_idx": flow.idx})
    print(f"    [队列] 超时岗位记入会话内补投列表（队列耗尽后直跳重试一次）: {job.get('jobName')}")


# --- 阶段 1：进详情 ---


def _enter_and_inspect_job(flow):
    """阶段 1（#39）：列表点卡片进详情 + 挂详情 XHR 监听 + 降级计数 + 开屏截图。
    #50：区分 card / direct_click_failed / direct_no_card，消除无卡片假阳性熔断。
    #56 A1：handler 引用**必须留在 flow 上**——旧注释「处理器无需保留引用（grab_detail
    抓完即弃），不像 friend/add 需要 remove_listener」是错的：抓完即弃的只是容器引用，
    监听器永久挂在同一个 page 的 response 事件上（对照 search_list / recommend_list /
    friend/add 三处已注销，#35 修掉的就是同一类）。注销由 `_apply_job` 末尾的
    `_detach_detail_listener` 负责，
    不在本函数返回时做——本岗后续阶段（grab_detail/_extract_recruiter_active）还要吃这个容器。"""
    page, job, idx = flow.page, flow.job, flow.idx
    flow.detail_listener = capture_xhr(page, "/wapi/zpgeek/job/detail.json", flow.detail)
    # #56 A3：拍点接进心跳——这一段（岗头心跳之后、detail-open 截图之前）就是 5/5 场楔死的现场，
    # 楔死时只剩的那一条 heartbeat label 因此能指到具体是哪个无超时调用没回来。
    mode = enter_detail(page, job.get("encryptJobId"), lambda url, **kw: page.goto(url, **kw), beat_fn=_heartbeat_beat)
    st = flow.ctx.state
    st["detail_entered"] = st.get("detail_entered", 0) + 1  # #44 进详情总次数
    if mode == "card":
        st["card_clicks"] = st.get("card_clicks", 0) + 1
    elif mode == "direct_click_failed":
        st["card_click_fails"] = st.get("card_click_fails", 0) + 1
        st["direct_fallback"] = st.get("direct_fallback", 0) + 1
    elif mode == "direct_no_card":
        st["direct_no_card"] = st.get("direct_no_card", 0) + 1
        st["direct_fallback"] = st.get("direct_fallback", 0) + 1
        print("    [导航] 列表未渲染卡片，主动直跳详情（正常数据态）")
    elif mode == "direct":  # 兼容历史桩/测试
        st["direct_fallback"] = st.get("direct_fallback", 0) + 1
    shot(page, flow.ctx.shots_dir, f"detail-open-{idx}")


# --- 阶段 2：拟人阅读与开工前检查 ---


def _human_read_and_precheck(flow):
    """阶段 2（#39）：拟人阅读停留 → 风控熔断 → 登录态过期 → 已沟通复核。
    返回 None 继续本岗；"stop" / "relogin" 终止本轮（由主循环决定停批还是提示重登）；
    "next" 表示本岗跳过。"""
    page, job, idx, ctx = flow.page, flow.job, flow.idx, flow.ctx
    human_read(page, lo=ctx.cfg.get("dwell_min", 10), hi=ctx.cfg.get("dwell_max", 35))
    shot(page, ctx.shots_dir, f"after-read-{idx}")
    risk = detect_risk_signal(page)
    if risk:
        print(f"  [熔断] {risk}")
        _logging.log_event(
            "risk_blocked",
            level="error",  # #48：安全事件默认落盘（error ≥ 默认 ERROR 阈值；warning 曾被静默丢弃）
            job=job.get("jobName"),
            brand=job.get("brandName"),
            detail=risk,
            stage="pre_click",
        )
        ctx.platform.save_evidence(page, f"live-risk-{idx}", job)
        shot(page, ctx.shots_dir, f"risk-{idx}")
        ctx.results.append({"idx": idx, "job": job.get("jobName"), "status": "risk_blocked", "detail": risk})
        _record_breaker(ctx, breaker="risk_blocked", stage="pre_click", detail=risk)
        return "stop"
    if is_logged_out(page, ctx.platform.login_dialog_selector):
        print("  [停] 登录态过期")
        ctx.results.append({"idx": idx, "job": job.get("jobName"), "status": "login_expired"})
        return "relogin"
    # 已沟通检测（进了详情才知道，放起草请求前——省一次 agent 起草；
    # #36 同型收紧：class + 文本约束，text= 模糊匹配会误报 skipped 静默漏投）
    if find_continue_button(page):
        print("  [跳过] 已沟通过")
        return _skip_job(flow, "skipped")
    return None


# --- 阶段 3：取 JD、等 agent 起草 ---


def _extract_recruiter_active(detail_container):
    """#32 段 2：详情 wapi 响应 → 招聘者活跃度字段（旁听页面自己发的 detail.json，零额外请求）。
    结构缺失/未捕获返回 {}——台账字段可缺省，不阻塞投递。"""
    data = (detail_container or {}).get("data") or {}
    recruiter = (data.get("zpData") or {}).get("bossInfo") or {}
    out = {}
    if recruiter.get("activeTimeDesc"):
        out["bossActiveDesc"] = recruiter["activeTimeDesc"]
    if "bossOnline" in recruiter:
        out["bossOnline"] = recruiter["bossOnline"]
    return out


def _log_detail_empty(page, flow, jd_class):
    """#56 A5①：grab_detail 落空退出时落一条 `detail_empty_outcome` 机读事件，把票面点名的
    四件事一次答完——**哪条 URL / 什么异常（来自容器 errors）/ 等了多久 / DOM 选择器命中数**。
    为什么在编排层落而不是在平台层落：平台层不 import 日志侧车（#56 A3 的「平台层不 import 心跳」
    那半条口径从 #56 取证 A 起**已收窄**——`platforms/chat_first.py` 现在确实 import `_forensics`，但只用它
    记 handler 往返，见该文件顶部 import 处的注释；日志侧车这一侧一字未动，事件仍只在编排层落），
    而且岗位身份（eid / jobName / idx）只有编排层有——取证要能指到具体哪个岗。
    grab_detail 的探测证据（`empty_outcome`）由平台层写进容器；容器里没有它（桩页面 /
    替换过的 grab_detail）时相关字段留 None 并带 `probe_present=False`，照常落事件——
    「没有探测证据」本身也是证据，不能因为取证缺口就退回零可观测。
    #56 修复轮 F1：事件同时带两条分流读数 `dom_shape` / `detail_code`（与结果条目、report
    同源同判据，见 `_no_jd_signal_readings`）——事后翻 JSONL 与读 report.json 必须得到同一个答案。
    观测层异常全吞（#38）。"""
    try:
        container = flow.detail if isinstance(flow.detail, dict) else {}
        errors = container.get("errors") if isinstance(container.get("errors"), list) else []
        probe = container.get("empty_outcome") if isinstance(container.get("empty_outcome"), dict) else {}
        signals = _no_jd_signal_readings(container)
        try:
            url = str(getattr(page, "url", "") or "")[:300]
        except Exception:  # noqa: BLE001  # 页面已关时 url 属性本身就抛
            url = ""
        first = errors[0] if errors and isinstance(errors[0], dict) else {}
        _logging.log_event(
            "detail_empty_outcome",
            level="error",  # #48：事件类要 error 级才不被默认阈值吞掉
            job=flow.job.get("jobName"),
            eid=flow.job.get("encryptJobId"),
            idx=flow.idx,
            jd_class=jd_class,
            url=url or probe.get("url") or "",
            error=str(first.get("error") or ""),
            error_count=len(errors),
            waited_seconds=probe.get("seconds"),
            dom_seconds=probe.get("dom_seconds"),
            xhr_seconds=probe.get("xhr_seconds"),
            dom_rounds=probe.get("dom_rounds"),
            xhr_rounds=probe.get("xhr_rounds"),
            dom_hits=probe.get("hit_total"),
            dom_nonempty=probe.get("nonempty_total"),
            hit_selectors=probe.get("hit_selectors") or [],
            selectors=probe.get("selectors") or [],
            probe_present=bool(probe),
            probe_error=str(probe.get("probe_error") or ""),
            dom_shape=signals["dom_shape"],  # 修复轮 F1/E：DOM 侧形态（零命中的两假说 vs 取证缺口）
            detail_code=signals["detail_code"],  # 非 0 返回码留痕（无则 None，不是 0）
            detail_code_nonzero=signals["detail_code"] is not None,
            empty_url=signals["empty_url"],  # 修复轮 4/E：落空时刻 URL（探针写进容器的那份）
        )
    except BaseException:  # noqa: BLE001  # 取证层绝不反噬投递（同 _observe_xhr_failure）
        return None


def _acquire_and_draft_greeting(flow):
    """阶段 3（#39）：JD 全文 → 文件队列 request_N.json → 等 agent 写 reply_N.json。
    草稿就绪时存进 flow.greeting 返回 None；本岗跳过时返回 "next"。
    #56 A5②：grab_detail 返回空串时按容器证据给本岗归因（三类之一，`classify_no_jd`），
    结果条目带 `jd_class`、终端打印按类说话（人读层措辞前缀不变），并落一条
    `detail_empty_outcome` 事件——台账口径不再默认「岗位疑已失效」。
    #56 修复轮 F1：结果条目再带两条分流读数 `dom_shape` / `detail_code`（类别与台账政策都不变），
    「往返没回/挂起」与「详情页落空」在终端、事件与 report 三处都读得出来。
    #56 修复轮 4/E：`dom_shape=zero_hits` 的措辞回到两假说并列（形态漂移 / 人已离岗弹回列表，
    判据边界见 `no_jd_dom_shape`），落空时刻 url 随结果条目（`empty_url`）、事件与 report 读数
    （`no_jd_empty_urls`）一起落盘——人工判读靠它（与 C 段栈）分辨，播报层不下结论。
    #56 修复轮 3 R2（request 侧的语义，写进契约）：**只有拿到非空字符串正文时才产
    request_N.json**，且产出的 `jdText` 恒非空——拿不到正文（含平台层返回空白串或非字符串这类
    越契约形态）一律走 no_jd，绝不让 agent 对着一份没有 JD 正文的请求写招呼语。
    判「有没有正文」的正主是 `platforms/chat_first.py::detail_post_description`（`grab_detail` 与
    `classify_no_jd` 共用同一条链），这里的守卫只兜「返回值越了契约」这一种分叉。"""
    page, job, idx, ctx = flow.page, flow.job, flow.idx, flow.ctx
    # #56 A3：grab_detail 的 DOM 轮询（page.evaluate，timeout=None）与 XHR 空等循环是同族
    # 无超时站点，拍点接进心跳后，「死在轮询第几轮」也读得出来（返回契约未动）。
    jd_text = grab_detail(page, flow.detail, beat_fn=_heartbeat_beat)
    if not (isinstance(jd_text, str) and jd_text.strip()):
        jd_class = classify_no_jd(flow.detail)
        signals = _no_jd_signal_readings(flow.detail)  # 修复轮 F1：两条分流读数同源再算一次
        _log_detail_empty(page, flow, jd_class)  # #56 A5①：把静默面变成可观测
        # 落空现场当场念出来：它是票面「若 no_jd 不降则独立开票」的判据，等回读 report 太晚。
        # 修复轮 4/E：措辞从断言式改成两假说并列——零命中不是「形态漂移」的单义证据（分栏视图
        # URL 不含 job_detail，人已离岗被弹回列表同形，见 `no_jd_dom_shape`），分辨靠结果条目的
        # `empty_url` 与 C 段楔死栈，不许在播报层替它们下结论。
        drift = (
            "；DOM 零命中＝形态漂移或人已离岗弹回列表（靠落空 url 与 C 段栈分辨）"
            if signals["dom_shape"] == DOM_SHAPE_ZERO_HITS
            else ""
        )
        print(f"  [跳过] 没拿到 JD 全文（{_no_jd_class_label(jd_class)}{drift}）")
        return _skip_job(flow, "no_jd", jd_class=jd_class, **signals)
    flow.recruiter_active = _extract_recruiter_active(flow.detail)  # grab_detail 成功 ⇒ detail 响应已就位
    request_greeting(ctx.session_dir, idx, job, jd_text, recruiter_active=flow.recruiter_active)
    shot(page, ctx.shots_dir, f"request-{idx}")
    send_text = wait_reply(ctx.session_dir, idx, timeout=ctx.cfg.get("reply_timeout", 600))
    if send_text == SKIP:
        return _skip_job(flow, "agent_skip", reason=reply_reason(ctx.session_dir, idx))
    if not send_text:
        _record_agent_timeout(flow)  # #55：超时岗位分流记账（补投列表或跨会话台账）
        return _skip_job(flow, "agent_timeout", encryptJobId=job.get("encryptJobId"))
    flow.greeting = send_text
    return None


# --- 阶段 4：发送前机械 gate ---


def _verify_greeting_gates(flow):
    """阶段 4（#39）：发送前机械核验（用户约定 2026-09-10：确定性不靠 agent 自评）——
    level 合法 + 四道 gate（AI 痕迹 / 内部标记 / 运行时状态词 / 白名单）全 CLEAN。
    违规草稿拦截不发送，留在 reply_N.json 供复盘；称呼规范（#27 项 1）与「结尾只带软问题」
    规范（2026-10-16）都只打 warning，不阻断发送。"""
    reply = read_reply(flow.ctx.session_dir, flow.idx) or {}
    whitelist = g.load_whitelist_file(flow.ctx.cfg.get("fact_cards_path") or g.FACT_CARDS)
    if whitelist is None:
        print(
            "  [warn] fact_cards 白名单不可用（缺失或 JSON 损坏）——本岗位白名单 gate 跳过，"
            "建议跑 gen_greetings.py doctor --sync 后继续"
        )
    violations = pre_send_gate(
        flow.greeting,
        reply.get("level"),
        whitelist,
        notes=reply.get("notes"),
        sections=flow.ctx.cfg.get("profile_sections"),
        state_evidence=reply.get("state_evidence"),
    )
    if violations:
        print(f"  [拦截] 发送前 gate 未过（{'; '.join(violations[:3])}）")
        _logging.log_event("gate_blocked", level="warning", job=flow.job.get("jobName"), violations=violations[:5])
        return _skip_job(flow, "gate_blocked", detail="; ".join(violations[:3]))
    flow.salutation_warns = salutation_warnings(flow.job.get("bossName"), flow.job.get("bossTitle"), flow.greeting)
    for w in flow.salutation_warns:
        print(f"  [warn 称呼] {w}")
    flow.question_warns = question_warnings(flow.greeting)
    for w in flow.question_warns:
        print(f"  [warn 提问] {w}")
    return None


# --- 阶段 5：点沟通、键入、发送 ---


def _record_off_chat_retry(flow):
    """#62 拍板口径「只捞确定没打一个字的」：守卫发生在任何打字动作**之前** ⇒ 本岗确定一个字
    都没进过页面 ⇒ 重投零双发风险 ⇒ 落 #55 的跨会话补投台账 `retry_pending.json`。
    条目照 `_retry_snapshot` 的既有契约（`attempts=0`、带 `jd_text` 快照），落盘交给
    `_save_retry_pending_state`（dry-run 不写）、封顶仍由 `RETRY_ATTEMPTS_CAP` 在
    `apply_retry_outcome` 里管。

    与 `_record_agent_timeout` 的两处差别都是刻意的：
    - **不按 `retry_pass` 分流**、直接进跨会话台账——「弹回列表」不是等几秒就能好的站点抖动，
      同一场里再点一次这个岗多半还是落到同一个反馈框；跨会话才补得回、也看得到。
    - **不动 `retry_new_count`**——那个计数是 #55 收尾行「本场新增**超时** M 个」的取数口
      （runbook 步骤 6 ⑤ 照此呈报），off_chat 不是超时，掺进去就把成因说错了；本岗的可见性
      由即时 `[补投]` 行、`no_send` + `reason="off_chat"` 明细与 report 的 `no_send_reasons`
      三处承担。`retry_touched` 要置位——它是「台账被写过 ⇒ 收尾要落盘」的开关，语义无关成因。"""
    ctx, job = flow.ctx, flow.job
    ctx.state["retry_touched"] = True
    ctx.state.setdefault("retry_pending", []).append(
        _retry_snapshot(job, attempts=0, jd_text=_request_jd_text(ctx.session_dir, flow.idx))
    )
    print(f"    [补投] 打字前拦下（一个字都没进页面）→ 落入跨会话台账: {job.get('jobName')}")


def _skip_off_chat_input(flow, cause):
    """#62 守卫触发时的收尾：一行说明（`cause` + 落空 URL，读终端就能认出「不在沟通形态」而非
    发送段故障）+ 截图 + 存证（新标签 `live-off-chat-{idx}`，不再和真正的发送失败挤在
    `live-no-send-*` 里）+ 进台账 + 跳岗。
    状态串**仍用既有 `no_send`**，不新造：#47 的 applied/skipped/failed 三桶按状态串分桶，
    新造一个串会掉进「未收录状态保守归 failed」以外的地方、既有断言也连带受影响。
    两种成因靠 `reason` 分辨——本条 `off_chat`（打字前拦下，`detail` 记 `cause`），
    `_type_and_send_greeting` 那两条是字已打进输入框之后的 `send_btn_missing` / `send_click_failed`。"""
    page, idx, ctx, job = flow.page, flow.idx, flow.ctx, flow.job
    url = getattr(page, "url", "") or ""
    print(f"  [跳过] {cause}（落空 URL={url[:120]}），招呼语未键入")
    shot(page, ctx.shots_dir, f"off-chat-{idx}")
    ctx.platform.save_evidence(page, f"live-off-chat-{idx}", job)
    _record_off_chat_retry(flow)
    return _skip_job(flow, "no_send", reason="off_chat", detail=cause)


def _open_chat_and_find_input(flow):
    """阶段 5a（#39）：找「立即沟通」（#36 专属 class 优先 + #41 弹回重进）→ 点击 →
    点击后复检风控 / 登录 → **#62 打字前形态守卫** → 找输入框（#42 加载态轮询）。
    命中的输入框存进 flow.input_el 返回 None；否则返回 "stop"/"relogin"/"next"。"""
    page, job, idx, ctx = flow.page, flow.job, flow.idx, flow.ctx
    # #56 修复轮 F4②：查找段（query_selector 全族裸奔）也接上同一套拍点——A3 只接了点击段，
    # 「死在找按钮 / 找输入框」过去读不出来。
    btn = find_comm_button_with_recovery(page, job.get("encryptJobId"), job.get("jobName"), beat_fn=_heartbeat_beat)
    # #56 A3：点击段（bounding_box / evaluate / mouse.click 全裸奔）接同一套拍点，
    # 楔死在「立即沟通」这一跳时最后一条心跳落在 click:* 上而不是停在 detail-open 截图。
    if not btn or not click_with_retry(page, btn, action="立即沟通按钮", beat_fn=_heartbeat_beat):
        _save_click_diagnostics(page, btn, idx, ctx)  # #44 失败当下取证（事后复现会骗人）
        ctx.platform.save_evidence(page, f"live-no-comm-{idx}", job)
        # #44 台账防漏记（2026-09-24 实证）：actionability 报超时但点击事件可能已派发——
        # 09-23 会话 5 例 no_btn 复查有 2 例按钮已变「继续沟通」（实际已发送，台账漏记）。
        # 复查命中则按已发送补记台账（code=None → -1），不再记 no_btn、不计连续失败。
        time.sleep(1.5)
        if find_continue_button(page):
            print("    [补记] 点击超时但按钮已变「继续沟通」→ 按已发送登记台账")
            _record_applied(flow, None, note="no_btn 误判：点击实际已生效（复查按钮为继续沟通）")
            return "next"
        ctx.state["consec_no_btn"] = ctx.state.get("consec_no_btn", 0) + 1  # #44 连续失败熔断
        return _skip_job(flow, "no_btn")
    time.sleep(random.uniform(2, 4))
    shot(page, ctx.shots_dir, f"after-comm-click-{idx}")
    risk = detect_risk_signal(page)
    if risk:
        print(f"  [熔断] {risk}（点击沟通后弹出）")
        _logging.log_event(
            "risk_blocked",
            level="error",  # #48：安全事件默认落盘
            job=job.get("jobName"),
            brand=job.get("brandName"),
            detail=risk,
            stage="post_click",
        )
        ctx.platform.save_evidence(page, f"live-risk-click-{idx}", job)
        shot(page, ctx.shots_dir, f"risk-click-{idx}")
        ctx.results.append(
            {"idx": idx, "job": job.get("jobName"), "status": "risk_blocked", "detail": f"{risk}（点击沟通后弹出）"}
        )
        _record_breaker(session=ctx, breaker="risk_blocked", stage="post_click", detail=risk)
        return "stop"
    if is_logged_out(page, ctx.platform.login_dialog_selector):
        print("  [停] 点击后弹出登录框")
        ctx.results.append({"idx": idx, "job": job.get("jobName"), "status": "login_expired"})
        return "relogin"
    # #62 打字前守卫（形态层）：**必须在找输入框之前**，不能等找到之后再验。出事那页（点沟通后
    # 弹回搜索列表 + 底部「对搜索结果是否满意？」浮层）上全页只有一个 textarea，就是反馈框——
    # 先找输入框会先撞上它，而它现在被 `find_input` 的浮层排除挡回去 ⇒ 落 `no_input`
    # （#42「聊天页加载态未就绪」的语义），把「人已离岗」和「输入框还没就绪」混成一桶，
    # 还丢掉补投台账。顺序反过来才拿得到票面要的归因（`no_send` + `off_chat`），
    # 顺带省掉形态不对时那 7 次 `query_selector` + 一次祖先链探测。
    # 守卫就落在这一格：`_type_and_send_greeting`（5b）的唯一上游是本函数（5a 返回 None 才进 5b），
    # 所以从这里挡回去就保证 5b 永远吃不到「不在沟通形态」的页面——放在 5b 里等于先打字再验，
    # 正是本票的现场。
    if not _in_chat_form(page, job.get("encryptJobId"), job.get("jobName")):
        return _skip_off_chat_input(flow, "页面已不在本岗沟通形态")
    flow.input_el = find_input_with_retry(page, beat_fn=_heartbeat_beat)  # #42：聊天页输入框加载态轮询重试
    if not flow.input_el:
        print("  [跳过] 没找到输入框")
        ctx.platform.save_evidence(page, f"live-no-input-{idx}", job)
        return _skip_job(flow, "no_input")
    return None


def _type_greeting(page, input_el, text, cfg):
    """输入草稿（#39 提取）：以粘贴为主（浏览时已备好内容，点开沟通后直接粘贴），
    paste_prob 概率逐字打字混合，行为不至于单一。
    except 同时兜住两种失败：fill 不兼容，以及逐字键入自身异常——后者会再试一次键入
    （原实现如此，消息文案统一为「fill 失败」）。"""
    type_kwargs = {
        "delay_lo": cfg.get("type_delay_min", 40),
        "delay_hi": cfg.get("type_delay_max", 120),
        "typo_prob": cfg.get("typo_prob", 0.05),
    }
    try:
        if random.random() < cfg.get("paste_prob", 0.7):
            input_el.fill(text)
        else:
            print("    [输入] 本条逐字键入（行为混合）")
            human_type(page, text, **type_kwargs)
    except Exception as e:
        print(f"    [输入] fill 失败，回退逐字键入: {str(e)[:60]}")
        human_type(page, text, **type_kwargs)


def _type_and_send_greeting(flow):
    """阶段 5b（#39）：键入草稿 → 发送按钮或 Enter（聊天页 contenteditable 无独立按钮）
    → 等响应落 friend/add → 卸监听器。
    friend/add 监听器用 try/finally 保证三条出口（发送失败 / 无发送按钮 / 正常发出）都注销，
    与 #35 修掉的 search_list 监听器泄漏是同一类问题。"""
    page, idx, ctx = flow.page, flow.idx, flow.ctx
    fa_listener = capture_xhr(page, "/wapi/zpgeek/friend/add.json", flow.add_response)
    try:
        input_el = flow.input_el
        is_contenteditable = "contenteditable" in (
            input_el.evaluate("el => el.tagName + '|' + (el.getAttribute('contenteditable') || '')") or ""
        )
        input_el.click()
        time.sleep(random.uniform(0.4, 0.9))
        shot(page, ctx.shots_dir, f"typing-start-{idx}")
        _type_greeting(page, input_el, flow.greeting, ctx.cfg)
        time.sleep(random.uniform(1.5, 3))
        shot(page, ctx.shots_dir, f"typed-{idx}")
        send_btn = find_send_button(page, beat_fn=_heartbeat_beat)  # #56 修复轮 F4②：查找段接节拍
        if send_btn:
            if not click_with_retry(page, send_btn, action="发送招呼按钮", beat_fn=_heartbeat_beat):
                ctx.platform.save_evidence(page, f"live-no-send-{idx}", flow.job)
                # #62 归因：字**已经打进**沟通输入框，只是发送那一下没成 ⇒ **不进**补投台账
                # （可能已实际发出，自动重投有双发风险；用户 2026-10-09 拍板「只捞确定没打
                # 一个字的」）。可见性交给 report.json 的 no_send_reasons 读数。
                return _skip_job(flow, "no_send", reason="send_click_failed")
        elif is_contenteditable:
            # 聊天页无独立发送按钮：Enter 发送（页面提示「按 Enter 键发送」）
            print("    [发送] 聊天页输入框，Enter 发送")
            page.keyboard.press("Enter")
        else:
            ctx.platform.save_evidence(page, f"live-no-send-{idx}", flow.job)
            # #62 归因：同上——字已进框才落空，不自动补投，只保证读数里看得见它。
            return _skip_job(flow, "no_send", reason="send_btn_missing")
        time.sleep(random.uniform(4, 7))
        shot(page, ctx.shots_dir, f"sent-{idx}")
    finally:
        _drop_response_listener(page, fa_listener)  # #56 取证 A：remove 成功后才摘计数
    return None


# --- 阶段 6：响应判定与台账 ---


def _record_applied(flow, code, note=None):
    """台账三件套 + 结果累加（#39 阶段 6 的公共尾）：apply_log / applied_history /
    greetings 同一时间戳、同一事务点，greeting_applied 事件与台账同点落一条（#38 裁定）。"""
    job, idx = flow.job, flow.idx
    applied_at = datetime.now().isoformat(timespec="seconds")
    log_code = -1 if code is None else code
    log_apply(applied_at, job["jobName"], job["brandName"], log_code, log_path=flow.ctx.cfg.get("apply_log_path"))
    record_history(
        job, applied_at, history_path=flow.ctx.cfg.get("history_path"), recruiter_active=flow.recruiter_active
    )
    outcome = record_greeting(
        job,
        flow.greeting,
        applied_at,
        flow.ctx.session_dir,
        idx,
        greetings_path=flow.ctx.cfg.get("greetings_path"),
        recruiter_active=flow.recruiter_active,
    )
    flow.ctx.state.setdefault("greeting_ledger", {k: 0 for k in GREETING_LEDGER_KEYS})[outcome] += 1
    fields = {"job": job["jobName"], "brand": job["brandName"], "code": code}
    if note:
        fields["note"] = note
    _logging.log_event("greeting_applied", **fields)
    flow.ctx.state["applied"] += 1
    flow.ctx.state["consec_no_btn"] = 0  # #44 动作链路已恢复，连续失败计数清零
    entry = {
        "idx": idx,
        "job": job["jobName"],
        "brand": job["brandName"],
        "status": "applied",
        "code": code,
        "send_text": flow.greeting,
        "applied_at": applied_at,
    }
    if note:
        entry["note"] = note
    # salutation_warns 紧随原末位键序（与旧 report.json 逐行对比时只看这一个键）；
    # question_warns 是 2026-10-16 新末位键，旧报告没有它，对比时按缺键＝无命中读
    entry["salutation_warns"] = flow.salutation_warns
    entry["question_warns"] = flow.question_warns
    flow.ctx.results.append(entry)


def _record_apply_outcome(flow):
    """阶段 6（#39）：friend/add 响应判定 → 台账回写 → 结果累加。
    实测（2026-09-09）：部分岗位发送后 friend/add.json 不出现，但招呼语实际已发出
    （typed/sent 截图可证）。此时仍登记台账，避免 applied_ids 过滤漏掉导致重复投递。"""
    fa = flow.add_response["data"]
    if not fa:
        print("  [?] friend/add 未捕获，按已发送登记（见 typed/sent 截图）")
        _record_applied(flow, code=None, note="friend/add 未捕获")
        flow.ctx.pacer.wait_sent()
        safe_go_back(flow.page)
        return "next"
    code = fa.get("code")
    zp = fa.get("zpData", {})
    action, detail = handle_code(code, fa.get("message", ""), zp)
    if is_platform_daily_limit(str(zp.get("greeting", ""))):
        action = "stop"
        detail = "每日沟通上限"
    print(f"  [{action}] code={code} {detail}")
    if action == "ok":
        _record_applied(flow, code=code)
        flow.ctx.pacer.wait_sent()
    elif action in ("pause", "stop"):
        notify("投递暂停", detail)
        _logging.log_event(
            "apply_paused", level="error", job=flow.job["jobName"], code=code, detail=detail
        )  # #48 安全事件默认落盘
        _record_breaker(flow.ctx, breaker="apply_paused", code=code, detail=detail)  # #51 建票数据源
        flow.ctx.results.append(
            {"idx": flow.idx, "job": flow.job["jobName"], "status": "paused", "code": code, "detail": detail}
        )
        flow.ctx.pacer.wait_skip()
        safe_go_back(flow.page)
        return "stop"
    else:
        flow.ctx.results.append(
            {"idx": flow.idx, "job": flow.job["jobName"], "status": "skip", "code": code, "detail": detail}
        )
        flow.ctx.pacer.wait_skip()
    safe_go_back(flow.page)
    time.sleep(random.uniform(2, 4))
    return "next"


def _detach_detail_listener(flow):
    """#56 A1：注销本岗的 detail.json 监听器（单岗生命周期末尾的唯一出口）。
    为什么是「头号楔死候选」：handler 里的 `resp.json()` → `Response.body()` →
    `send("body", None)` 是一次**无超时**协议往返，而每个事件处理器都是 dispatcher 按序
    切换的 EventGreenlet——任一次往返不返回，dispatcher 就再也不泵主线程的 task
    （现象即观测到的 Python 0% CPU + SIGINT 无效）。不注销则往返数随岗数 O(N²) 累积，
    与 5/5 场楔死（含「第 11 岗高频」）吻合。
    注销自身的异常必须吞掉并打一行可见警告：本仓纪律是观测/取证层绝不新增失败面
    （#38 观测层异常全吞不崩业务、#54 观测层全降级不阻断投递）——此处往外抛会把一个
    已经发出去招呼的岗位变成 page_error。
    #56 取证 A：计数改由 `_drop_response_listener` 摘，**只在成功分支里摘**（remove 抛异常时
    `handlers_live` 保持「仍挂着」，那才是诚实读数）；吞异常 + 警告这两步一字未改。"""
    if flow.detail_listener is None:
        return  # 本岗没挂过监听器（进详情前就跳过 / 桩页面路径）
    listener, flow.detail_listener = flow.detail_listener, None
    try:
        _drop_response_listener(flow.page, listener)
    except Exception as e:  # noqa: BLE001
        print(f"    [warn] detail 监听器注销失败（{str(e)[:60]}）——本岗结果不受影响，但泄漏仍在")


def _apply_job(flow):
    """单岗 6 阶段串联（#39）：任一阶段返回 stop / relogin / next 即提前结束本岗。
    dry-run 截停（#40）刻意留在「点沟通之前」——「不点沟通、不发送、不写台账」这条安全
    属性在代码结构上直接看得见，不靠调用方记住顺序。
    #56 A1/A2：整段套 try/finally——本岗的 detail 监听器注销与心跳岗位上下文清除共用这
    一个出口，无论走哪条分支（applied / 8 个跳过 / stop / relogin / 抛异常）都不残留。
    注销必须放在**单岗末尾**而不是 `_enter_and_inspect_job` 返回时：详情响应要在后续阶段
    才被 grab_detail 消费，提前注销等于把楔死修复做成 no_jd 回归。"""
    try:
        for phase in (
            _enter_and_inspect_job,
            _human_read_and_precheck,
            _acquire_and_draft_greeting,
            _verify_greeting_gates,
        ):
            signal = phase(flow)
            if signal:
                return signal
        if flow.ctx.cfg.get("dry_run"):
            return dry_run_stop(
                flow.ctx.results, flow.idx, flow.job, flow.greeting, flow.salutation_warns, flow.question_warns
            )
        signal = _open_chat_and_find_input(flow)
        if signal:
            return signal
        signal = _type_and_send_greeting(flow)
        if signal:
            return signal
        return _record_apply_outcome(flow)
    finally:
        _heartbeat_job_end()  # #56 A2：与监听器注销共用这一个出口，不开第二个 finally
        _detach_detail_listener(flow)


def apply_live(page, job, session_dir, idx, shots_dir, cfg, pacer, platform, results, state):
    """单岗位：点卡片进详情 → 拟人浏览 → JD 入队 → agent 草稿 → 逐字发送。
    10 形参签名保持（#39 拆分约束）：这是会话主循环与行为测试共用的 seam，收敛只发生在
    函数内部——建成 LiveSessionContext + _JobFlow，按 6 阶段串联（_apply_job）。"""
    print(f"=== [{idx}] {job.get('jobName')} | {job.get('brandName')} | {job.get('salaryDesc')} ===")
    ctx = LiveSessionContext(
        session_dir=Path(session_dir),
        shots_dir=Path(shots_dir),
        cfg=cfg,
        pacer=pacer,
        platform=platform,
        results=results,
        state=state,
    )
    flow = _JobFlow(page=page, job=job, idx=idx, ctx=ctx)
    # #56 A2：岗位上下文先于岗头心跳——楔死的那一拍（5/5 场都是 `job:{idx}`）必须已经带 eid
    _heartbeat_job_start(flow)
    _touch_heartbeat(f"job:{idx}")  # 岗头心跳：20260927 三场楔死都发生在岗头→detail-open 之间
    return _apply_job(flow)


# ---------------------------------------------------------------------------
# 会话主循环（run_live）拆分：启动段 → 轮次段 → 报告段 → 收尾段。
# 原来 188 行的一条直线里混着「路径解析 / 配置组装 / 门禁预检 / 轮次循环 / 报告落盘 /
# 录像收尾」，任一段出问题都得读完全文；现在每段一个函数，段间只传 _LiveRun。
# ---------------------------------------------------------------------------


@dataclass
class _LiveRun:
    """run_live 一次运行的上下文（#39）：启动段组装，后续三段只读它。
    session 是会话级共享上下文（与单岗流程同一容器），其余是本轮运行期参数。"""

    env: dict
    ts: str
    reported_path: Path  # #51：防重台账 private/data/[<profile>.]reported_issues.json
    browser_dir: Path
    video_base: Path
    video_kwargs: dict
    queries: list
    record: bool
    limit: int
    applied_ids: set
    excluded: set
    session: LiveSessionContext
    # #33：会话级已收集岗位（跨关键词轮 + 跨来源共用）。票面验收 6 要求「同一
    # encryptJobId 全局只收集一次」——各来源各自去重再 concat 会漏跨来源重复，
    # 故这一个列表由关键词轮与推荐流轮共享。
    collected: list = field(default_factory=list)


def _open_session_dirs(session_base, video_base, record, ts):
    """建会话目录 / 截图目录 / （可选）录像目录，返回 (session_dir, shots_dir, video_kwargs)。
    录像目录必须先存在——Camoufox 的 record_video_dir 只收已存在目录，且 ts 与会话目录对齐，
    收尾段才能按同一 ts 找回落盘视频。"""
    session_dir = session_base / ts
    session_dir.mkdir(parents=True, exist_ok=True)
    shots_dir = session_dir / "shots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    video_kwargs = {}
    if record:
        vid_dir = video_base / ts
        vid_dir.mkdir(parents=True, exist_ok=True)
        video_kwargs = {"record_video_dir": str(vid_dir), "record_video_size": {"width": 1280, "height": 720}}
    return session_dir, shots_dir, video_kwargs


def _limit_desc(cfg):
    """日上限横幅文案（#39 提取）：固定档 vs 自动阶梯档。"""
    if cfg.get("daily_limit_fixed"):
        return f"固定 {cfg['daily_limit']}±{cfg['cap_jitter']}"
    return (
        f"自动阶梯：起步 {cfg['daily_limit']}，每日 +{cfg['ladder_step_min']}-"
        f"{cfg['ladder_step_max']}，封顶 {cfg['ladder_cap']}，±{cfg['cap_jitter']} 抖动"
    )


def session_banner(queries, limit, limit_desc, applied_ids, excluded, session_dir, dry_run, recommend_desc=""):
    """开跑横幅（#35 build_report 同型提取）：会话参数一览；dry_run（#40 冒烟）追加截停说明；
    recommend_desc（#33）显示推荐流来源的开关与单来源采集上限。"""
    smoke = "；冒烟 dry-run：到发送前 gate 截停，不点沟通不发送不写台账" if dry_run else ""
    print(
        f"[Live 投递] 关键词 {queries}，每日上限 {limit}（{limit_desc}），"
        f"已投历史 {len(applied_ids)} 个，简历公司过滤 {len(excluded)} 个关键词，"
        f"会话目录 {session_dir}{recommend_desc}{smoke}\n"
    )


def _apply_collect_cfg(cfg, env):
    """采集配置（#33）：LIVE_RECOMMEND 开关（默认 on，票面 v2「搜索与推荐并用」）
    + LIVE_COLLECT_CAP 单来源单次采集上限（默认 120，0 = 不限）。

    上限自 #33 第七轮起同时管两个来源（搜索与推荐都是滚动加载），故不再叫「推荐流单轮
    上限」；旧的 LIVE_RECOMMEND_PER_ROUND 作为别名继续接受（只有它被设置时才生效并打印
    提示），避免既有环境变量静默失效。非法值回退默认并提示——一个写错的配置不该中断
    整场会话。"""
    # 取值表口径与 `LIVE_RECORD` **同族**（同一张 `BOOL_ENV_EXACT_OFF`）；几张表的逐行对照只有
    # 一份真源，见 `_limits.py` 的「布尔 env 开关」小节（#59 修复轮 R4-4：副本改成指针）；
    # 此处只换解析入口，语义一字未动。
    cfg["live_recommend"] = env_bool_switch(env, "LIVE_RECOMMEND", off_values=BOOL_ENV_EXACT_OFF)
    legacy = (env.get("LIVE_RECOMMEND_PER_ROUND") or "").strip()
    raw = (env.get("LIVE_COLLECT_CAP") or legacy).strip()
    try:
        cap = int(raw) if raw else COLLECT_CAP_DEFAULT
    except ValueError:
        print(f"[配置] 采集上限 {raw!r} 不是整数，使用默认 {COLLECT_CAP_DEFAULT}")
        cap = COLLECT_CAP_DEFAULT
    if cap < 0:
        print(f"[配置] 采集上限 {cap} 为负，使用默认 {COLLECT_CAP_DEFAULT}")
        cap = COLLECT_CAP_DEFAULT
    if legacy and not env.get("LIVE_COLLECT_CAP"):
        print(f"[配置] LIVE_RECOMMEND_PER_ROUND 已并入 LIVE_COLLECT_CAP（本次按 {cap} 采集）")
    cfg["collect_cap"] = cap


def _apply_recommend_cap_cfg(cfg, env):
    """消费配额（#65）：LIVE_RECOMMEND_CAP = 推荐轮实际消费几个候选（默认 10，0 = 不限）。

    与 `LIVE_COLLECT_CAP` 分家（那把管采集深度，本配额只管消费数量），故只截
    `_recommend_round` 传给 `_apply_fresh_jobs` 的列表，`recommend_listing` / `recommend_scrolls`
    照旧反映真实召回深度。0 的语义与非法值回退口径逐项照 `_apply_collect_cfg`——一个写错的
    配置不该中断整场会话。与 `LIVE_RECOMMEND=off` 的区别：那把整轮关掉（退回纯搜索），
    本配额留着但限量（仍给画像匹配一个小额度）。"""
    raw = (env.get("LIVE_RECOMMEND_CAP") or "").strip()
    try:
        cap = int(raw) if raw else RECOMMEND_CAP_DEFAULT
    except ValueError:
        print(f"[配置] 推荐轮消费配额 {raw!r} 不是整数，使用默认 {RECOMMEND_CAP_DEFAULT}")
        cap = RECOMMEND_CAP_DEFAULT
    if cap < 0:
        print(f"[配置] 推荐轮消费配额 {cap} 为负，使用默认 {RECOMMEND_CAP_DEFAULT}")
        cap = RECOMMEND_CAP_DEFAULT
    cfg["recommend_cap"] = cap


def _collect_banner_desc(cfg):
    """横幅里的采集描述（#33）：开关为 off 时明说 off，避免报告里看不出来源被关掉；
    上限 0 是「不限」而非「不采集」，文案要写清，否则读者会把策略读反。"""
    if not cfg.get("live_recommend"):
        return "，推荐流 off（纯搜索）"
    cap = cfg.get("collect_cap") or 0
    return f"，单来源采集上限 {cap} 条" if cap else "，单来源采集不限量（仅受滚动轮次约束）"


def _init_live_session(env):
    """启动段（#39）：profile 解析 → 路径 / 配置组装 → 启动门禁预检 → 打印开跑横幅。
    处于静默时段时通知用户并返回 None——调用方据此直接以 0 退出，不启浏览器（省一次
    真实登录态暴露）。搜索词缺配置由 parse_queries fail-loud 直接非零退出，不静默回落。"""
    name, prefix = resolve_active_profile(env=env)
    print(profile_line(name, prefix))
    browser_dir = browser_profile_dir(name)
    reported_path = _issue_reporter.reported_path_for(prefix)  # #51 防重台账（路径知识单一出口）
    session_base = private_path("data/live_session", prefix)
    video_base = private_path("data/videos", prefix)
    kb_path = private_path("profile_background.md", prefix)
    fact_cards_path = private_path("kb/fact_cards.md", prefix)
    history_path = private_path("data/applied_history.json", prefix)
    excluded_path = private_path("data/excluded_companies.json", prefix)
    cfg = load_config(env)
    env = env if env is not None else os.environ
    cfg["greetings_path"] = private_path("data/greetings.json", prefix)
    cfg["history_path"] = history_path
    cfg["apply_log_path"] = private_path("data/apply_log.jsonl", prefix)
    cfg["retry_pending_path"] = private_path("data/retry_pending.json", prefix)  # #55 断点台账
    # #56 B1 楔死岗跳岗台账：路径同样必须经 private_path + profile 前缀（#28 口径——A 的楔死岗
    # 不该让 B 的会话跳过），开关在这里解析一次并落 cfg，三个消费点只读那一个键。
    cfg["wedge_skipped_path"] = private_path(WEDGE_LEDGER_FILE, prefix)
    cfg["wedge_skip_enabled"] = _wedge_skip_enabled(env)
    ladder_state_path = private_path("data/daily_limit_state.json", prefix)
    cfg["effective_limit"] = effective_daily_limit(cfg, state_path=ladder_state_path)
    # 默认 1200：agent 起草要读 JD+查白名单+跑四道 gate 自检，600s 在流中断时会误杀
    # （2026-09-25 实跑 request_6/7 因起草超 600s 被跳过且事后补 reply 无效）
    cfg["reply_timeout"] = int(env.get("LIVE_REPLY_TIMEOUT", "1200") or "1200")
    # #33：来源配置必须在这里落进 cfg——`_recommend_turn_open` 读的是 cfg["live_recommend"]，
    # 而 load_config 不认识这个键。7 轮实现曾漏掉这一行，结果推荐流轮在生产里恒不执行
    # （测试只直接调 _apply_recommend_cfg，测不到接线），由 test_live_apply 的接线用例兜住。
    _apply_collect_cfg(cfg, env)
    # #65：消费配额必须同样在这里落进 cfg——接线漏了的后果与上面那条注释记着的同族
    # （`_recommend_round` 读不到 cfg["recommend_cap"] 就恒不截断，测试只直接调解析函数测不到接线）。
    _apply_recommend_cap_cfg(cfg, env)
    # 备注 §存在性核验的小节集合（#27 项 3）：会话级加载一次，不可用只 warn 一次
    # （逐岗 warn 是噪音），缺失跳过存在性核验但备注非空仍强制
    cfg["profile_sections"] = profile_sections(kb_path)
    cfg["fact_cards_path"] = fact_cards_path
    if cfg["profile_sections"] is None:
        print("[warn] profile_background 不可用（缺失或无编号小节）——备注 §存在性核验跳过，备注非空仍强制")
    queries = parse_queries(env)
    # #57 第二轮收口（票面「同批登记」第 1 条）：解析走 `_limits.env_bool_switch`，参数逐项就是
    # 接线前的口径——精确串比较（`BOOL_ENV_EXACT_OFF`）、非法值不提示。**取值表常量与几张表的逐行
    # 对照只有一份真源**，在 `_limits.py` 的「布尔 env 开关」小节（#59 修复轮 R4-4：原先散在
    # ≥6 处的副本改成指针，两处各维护必然漂），按行为钉住的用例是
    # `test_three_bool_env_switches_have_three_distinct_value_tables`。
    record = env_bool_switch(env, "LIVE_RECORD", off_values=BOOL_ENV_EXACT_OFF)
    dry_run = env.get("LIVE_DRY_RUN", "") == "1"  # #40 冒烟：到发送前 gate 截停
    cfg["dry_run"] = dry_run

    quiet = cfg["quiet_hours"]
    if is_quiet_hour(current_hour(), quiet):
        notify("Live 投递暂停", f"当前处于静默时段 {quiet[0]}-{quiet[1]} 点，暂停投递")
        return None
    startup_jitter(cfg)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    session_dir, shots_dir, video_kwargs = _open_session_dirs(session_base, video_base, record, ts)
    # 两条呈报各管一件事、刻意不合并（#56 B1）：这条答「刚死了一场」，下一条答
    # 「从此不再碰哪些岗、凭什么、代价是什么」。
    _surface_prior_wedge(session_base, ts)
    platform = get_platform()
    applied_ids = load_applied_ids(history_path)
    excluded = load_excluded_companies(excluded_path)
    # #58 T1：注入 A3 的同一个拍点器——`SessionPacer` 的三类合法长等待（intra / 长休息 /
    # 跳过短等）由此分段睡并逐段报平安（label `pace:{kind}`）。不注入的话，一场健康会话的
    # 长休息（480-1500s）会同时击穿 90s 取栈阈值与 240s 停摆阈值（票面后果 1/2）。
    # 拍点异常由 `_heartbeat_beat` 吞（与 A3 同口径：观测层绝不新增失败面），节奏参数一律不动。
    pacer = SessionPacer(cfg, beat_fn=_heartbeat_beat)
    limit = cfg["effective_limit"]
    session_banner(
        queries,
        limit,
        _limit_desc(cfg),
        applied_ids,
        excluded,
        session_dir,
        dry_run,
        _collect_banner_desc(cfg),
    )
    session = LiveSessionContext(
        session_dir=session_dir,
        shots_dir=shots_dir,
        cfg=cfg,
        pacer=pacer,
        platform=platform,
        results=[],
        state={
            "applied": 0,
            "direct_fallback": 0,
            "keyword_skips": [],
            "dry_run": dry_run,
            # #44 动作层熔断口径：连续点击失败数 / 进入详情次数（直跳率的分母）
            "consec_no_btn": 0,
            "detail_entered": 0,
            # #50 细分点击退化口径：真实尝试点击卡片数与点击失败数
            "card_clicks": 0,
            "card_click_fails": 0,
            "direct_no_card": 0,
            # #51：熔断/停批事件记录（收尾 _issue_reporter 消费）
            "breakers": [],
            # #55：超时兜底状态——session_retries 会话内补投列表；retry_pass 补投轮标记
            # （期间超时直接转台账）；retry_pending 台账工作副本；retry_touched 是否动过台账
            "session_retries": [],
            "retry_pass": False,
            "retry_pending": [],
            "retry_touched": False,
            # #56 B1：楔死岗跳岗台账——`wedge_eids` 是台账工作副本（三个消费点查表用），
            # `wedge_skips` 是本场跳过明细（进 report 的 wedge_ledger），两者由
            # `_prepare_wedge_ledger` 在开跑时填
            "wedge_eids": {},
            "wedge_skips": [],
            "last_idx": 0,
            # #61 验收 2：轮次编号跨度（来源归因的唯一数据源），由 _span_start/_span_end
            # 在关键词轮 / 推荐流轮 / 两个补投轮三处写入
            "source_spans": [],
            # #64：招呼语台账结局计数（入库/覆盖/异常跳过），report 的 greeting_ledger 读数同源
            "greeting_ledger": {k: 0 for k in GREETING_LEDGER_KEYS},
            # #64 追加验收 5/6：历史条目打标与缺口普查读数，开跑时由
            # `migrate_greeting_ledger` 填（report 的 greeting_ledger_migration 同源）
            "greeting_migration": {k: 0 for k in GREETING_MIGRATION_KEYS},
        },
    )
    # #64 追加验收 5：招呼语台账历史条目开跑打标（幂等；dry-run 只算不写）。
    # 与 `_prepare_wedge_ledger` 同一约束：state 建好之后、投递开始之前。
    session.state["greeting_migration"] = migrate_greeting_ledger(
        cfg.get("greetings_path"), cfg.get("history_path"), dry_run
    )
    # #56 B1：开跑回灌上一场的击杀取证 → 载入台账工作副本 → 呈报（含代价与放行办法）。
    # 必须在 state 建好之后、投递开始之前：跳岗判定读的是 state["wedge_eids"]。
    # #58 T2：同一份 `applied_ids` 一并交给回灌侧做正向守卫（已投出的岗不得进跳岗台账）。
    _prepare_wedge_ledger(session, session_base, ts, applied_ids)
    return _LiveRun(
        env=env,
        ts=ts,
        reported_path=reported_path,
        browser_dir=browser_dir,
        video_base=video_base,
        video_kwargs=video_kwargs,
        queries=queries,
        record=record,
        limit=limit,
        applied_ids=applied_ids,
        excluded=excluded,
        session=session,
    )


def _save_keyword_evidence(page, session, query):
    """关键词轮失败存证（#35）：失败本身已在 keyword_round 记进 state["keyword_skips"]，
    存证只是留现场，再失败不上抛——存证失败不该终结整轮。"""
    try:
        session.platform.save_evidence(
            page, f"live-keyword-{len(session.state.get('keyword_skips', []))}", {"brandName": query}
        )
    except Exception:
        pass


def _wedge_ledger_map(session):
    """本场**生效**的楔死台账工作副本，归一成 {eid 字符串: 条目}——「这个岗在不在台账里」的
    唯一口径（#56 修复轮 F3）：开关 off、工作副本缺失或形态不对时恒空表。
    为什么只留这一处：`_wedge_entry_for`（投递路径按岗查条目）与 `_noise_exclusions`
    （噪声路径取排除集）过去各写一份判定，而且**不等价**——前者 `eids.get(str(eid))` 按字符串键
    查、后者 `ids |= set(eids)` 取 dict 的**原始键**。同一个岗于是会在投递路径被跳过、
    在噪声路径照样被点开（绕过止损）；更要紧的是 `_browse_list` 拿这个集合的元素去做
    `e in href` 子串判断，混进非字符串直接 TypeError、被那层的 try/except 吞成「这条列表
    没点噪声」——一个静默的行为与取证双重降级。键归一只在这一个地方做。
    两处消费点（`_wedge_entry_for` 查条目 / `_noise_exclusions` 取集合）都**直调本函数**，
    不再各写一份判定，也不再引入只回 bool 的中间层（那种函数在 `_wedge_entry_for` 这条
    返回条目的链上没有立足点，只会多一次查表、多一处与实现不符的注释）。"""
    if not session.cfg.get("wedge_skip_enabled"):
        return {}
    eids = session.state.get("wedge_eids")
    if not isinstance(eids, dict):
        return {}
    return {str(k): v for k, v in eids.items() if k}


def _noise_exclusions(session, fresh):
    """浏览噪声的排除集 = 本轮候选池 ∪ 楔死台账内的岗（#56 B1 消费面的补漏）。
    为什么台账岗也要排除：噪声点是**真的 coordinate_click 进详情页**（`_browse_list` ③），
    而 5/5 场楔死都落在「岗头→详情」这一段——投递路径跳过了它、噪声路径却照样点开它，
    等于绕开止损、把同一场会话再交一遍学费。`hover` 噪声只移鼠标不导航，无需按 eid 过滤。
    #56 修复轮 F3：集合恒为**字符串**（候选池 eid 也归一）且台账侧走 `_wedge_ledger_map`
    这一条共享口径——`None`/整数不再混进来把整段噪声炸掉。"""
    ids = {str(j.get("encryptJobId") or "") for j in fresh}
    return {i for i in ids if i} | set(_wedge_ledger_map(session))


def _browse_list(page, session, label, exclude_ids=frozenset()):
    """列表页拟人浏览（#39 提取）：滚动看卡片，随机 hover 1-2 张（真人鼠标会划过卡片、
    悬停看公司名——纯「滚→等」循环是脚本签名，#44 反侦察），再截图留证。
    全部失败不影响投递。

    #44 复盘补丁（2026-09-24 用户质询暴露的三个缺陷）：
    ① hover/噪声点击**禁用 elementHandle API**——handle.hover/handle.click 的
       actionability 序列在本页会挂起（13.4 根因），一律走坐标（bounding_box 是
       纯几何读取，安全）；
    ② 噪声点开的卡片**排除本轮候选池**（exclude_ids）——随机点开马上要投的岗位
       会污染该岗的浏览/沟通状态（用户质询：「确定是为我们不匹配的岗位吗？」——
       原实现不是，是全池随机）；排除后无卡可挑就跳过噪声，宁缺不污染；
    ③ 点开回列表**不依赖 go_back**（SPA 下不可靠，失败会停在详情页，后续
       enter_detail 全部找不到卡片 → 整场退化直跳），goto 回入口 URL 强制复位。

    #56 修复轮 F4②（A3 漏接点）：这段的 `query_selector_all` 与噪声那一次 `coordinate_click`
    都在票面 §1 点名的「timeout=None 裸奔调用」族里，而噪声点击是**真的导航进详情页**——
    5/5 场楔死就落在「岗头→详情」那一段，这里没拍点就等于楔死后只剩上一条心跳可读。
    拍点由 `_heartbeat_beat` 注入（其异常由注入方吞），本层的 try/except 与返回值一字未动。
    #56 修复轮 4（A3/A4）：两段各自的吞异常出口（`except: pass`）补 `browse:*:error` 拍点——
    「最后一条是 post」不再同时表示「走完了」与「抛过异常被吞」，两段分得开。"""
    entry_url = page.url
    for i in range(random.randint(2, 4)):
        try:
            human_scroll(page, random.randint(400, 800))
        except Exception:
            pass
        # hover 噪声：约一半的滚动轮里划过一张卡片（真人在找岗位时鼠标不停扫）
        if i > 0 and random.random() < 0.5:
            try:
                _heartbeat_beat("browse:hover-cards:pre")
                cards = page.query_selector_all("a[href*='/job_detail/']")
                _heartbeat_beat("browse:hover-cards:post")
                pick = random.choice(cards) if cards else None
                if pick is not None:
                    box = pick.bounding_box()
                    if box:
                        page.mouse.move(
                            box["x"] + box["width"] / 2 + random.uniform(-20, 20),
                            box["y"] + box["height"] / 2 + random.uniform(-8, 8),
                        )
                        time.sleep(random.uniform(0.5, 1.5))
            except Exception:
                # 异常被吞出口（修复轮 4 A3）：留下 error 拍点，与「走到 post 后卡住」分开
                _heartbeat_beat("browse:hover-cards:error")
        time.sleep(random.uniform(1.5, 3.5))
    # 浏览噪声（#44 反侦察）：约 1/4 的列表页会点开一张卡片看几秒再返回——
    # 真人找岗位就是「点开→扫两眼→关掉」，从未点开过任何卡片的会话反而不像人。
    # 失败不阻断；回列表用 goto 复位而非 go_back（见函数头 ③）。
    if random.random() < 0.25:
        try:
            _heartbeat_beat("browse:noisy-cards:pre")
            cards = page.query_selector_all("a[href*='/job_detail/']")
            _heartbeat_beat("browse:noisy-cards:post")
            others = [c for c in cards if not any(e and e in (c.get_attribute("href") or "") for e in exclude_ids)]
            pick = random.choice(others) if others else None
            if pick is not None and coordinate_click(page, pick, beat_fn=_heartbeat_beat):
                time.sleep(random.uniform(4, 8))
                try:
                    page.goto(entry_url, wait_until="domcontentloaded", timeout=20000)
                    time.sleep(random.uniform(1, 2))
                except Exception:
                    pass
        except Exception:
            # 异常被吞出口（修复轮 4 A4）：两段各留各的 error 拍点（点开详情这一跳是楔死区间）
            _heartbeat_beat("browse:noisy-cards:error")
    shot(page, session.shots_dir, f"list-{label}")


# #44（2026-09-24）：点击失败**当下**的运行时取证。
# 2026-09-23 的教训是**事后复现会骗人**：16 岗全超时，但事后另开浏览器点同样的两个 URL
# 却是 click ok（1318ms / 28ms），几何、遮挡、动画、主线程全部正常——复现不出生产状态。
# 所以取证必须发生在失败那一刻、同一个页面、同一个句柄上。
CLICK_DIAG_JS = """(el) => {
  if (!el) return null;
  const out = {tag: el.tagName, cls: el.className || '', ka: el.getAttribute('ka') || '',
               text: (el.innerText || '').trim().slice(0, 40)};
  const r = el.getBoundingClientRect();
  out.rect = [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)];
  out.inViewport = r.top >= 0 && r.bottom <= innerHeight;
  const cx = Math.round(r.x + r.width / 2), cy = Math.round(r.y + r.height / 2);
  const top = document.elementFromPoint(cx, cy);
  out.fromPoint = top ? {tag: top.tagName, cls: top.className || '',
                         isSelf: top === el, isDescendant: el.contains(top)} : null;
  const cs = getComputedStyle(el);
  out.pointerEvents = cs.pointerEvents;
  out.position = cs.position;
  out.animations = el.getAnimations ? el.getAnimations().length : -1;
  const chain = [];
  let node = el.parentElement, i = 0;
  while (node && i < 3) {
    const p = getComputedStyle(node);
    chain.push({tag: node.tagName, cls: (node.className || '').toString().slice(0, 60),
                pointerEvents: p.pointerEvents, position: p.position, zIndex: p.zIndex});
    node = node.parentElement; i++;
  }
  out.ancestors = chain;
  return out;
}"""


def _save_click_diagnostics(page, btn, idx, ctx):
    """#44：把点击失败那一刻的现场写进 `shots/click-diag-<idx>.json`。

    与 `save_evidence`（截图 + HTML + URL）互补：那三样只证明「按钮在页面上」，
    这份证明「点击为什么点不动」——主线程是否还活着（evaluate 计时）、落点被谁接住
    （`elementFromPoint`）、元素在不在视口、有没有动画。
    取证自己出任何问题都**不得影响投递主流程**，故全程静默兜底。"""
    out: dict[str, Any] = {"idx": idx, "url": None, "btn": None, "eval_ms": None}
    try:
        out["url"] = page.url
    except Exception:  # noqa: BLE001
        pass
    if btn is None:
        out["btn"] = "not_found"
    else:
        try:
            _t = time.time()
            page.evaluate("1 + 1")
            out["eval_ms"] = round((time.time() - _t) * 1000)
        except Exception as e:  # noqa: BLE001
            out["eval_ms"] = f"FAIL: {str(e)[:120]}"
        try:
            out["btn"] = page.evaluate(CLICK_DIAG_JS, btn)
        except Exception as e:  # noqa: BLE001
            out["btn"] = f"FAIL: {str(e)[:120]}"
    try:
        p = Path(ctx.shots_dir) / f"click-diag-{idx}.json"
        p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"    [取证] 点击失败现场已存 {p.name}")
    except Exception:  # noqa: BLE001
        pass


# --- 起飞前自检看门狗（20260925-215522 会话楔死实测）---
# 楔死形态：python 主线程卡在不可中断的 C 层 read（SIGINT/进程内 signal 均无法打断）。
# 唯一可靠看门狗在进程外：子进程轮询哨兵/父进程存活，超时 SIGKILL 整树（零投递零产物，强杀即止损）。
PREFLIGHT_WATCHDOG_ENV = "LIVE_PREFLIGHT_WATCHDOG"
PREFLIGHT_WATCHDOG_DEFAULT = 180  # 秒；覆盖 preflight 全路径（goto 30s + sleep + 三次点击重试）仍有余量

RETENTION_ENV = "LIVE_RETENTION_DAYS"
RETENTION_DEFAULT = 7  # 天；2026-09-26 用户约定：每次启动顺手清理超期视频与会话截图
# 会话目录名形状 `_TS_DIR_RE` 与击杀取证文件名 `WATCHDOG_KILLED` 定义在 `_forensics.py`（#57 第二轮
# 按取证职责归位；本文件顶部别名导入）——保留期清理、回灌扫描与呈报认同一份契约，不留第二份字面量。


def _parse_preflight_watchdog(raw):
    """解析 LIVE_PREFLIGHT_WATCHDOG：秒数；0=关闭；未设置/非法/负数回退默认 180。"""
    if raw is None or str(raw).strip() == "":
        return PREFLIGHT_WATCHDOG_DEFAULT
    try:
        val = int(str(raw).strip())
    except ValueError:
        print(f"[配置] {PREFLIGHT_WATCHDOG_ENV}={raw!r} 不是整数，使用默认 {PREFLIGHT_WATCHDOG_DEFAULT}")
        return PREFLIGHT_WATCHDOG_DEFAULT
    if val < 0:
        print(f"[配置] {PREFLIGHT_WATCHDOG_ENV}={raw!r} 需为非负整数，使用默认 {PREFLIGHT_WATCHDOG_DEFAULT}")
        return PREFLIGHT_WATCHDOG_DEFAULT
    return val


# --- 会话中途看门狗（20260927 三场楔死实测：181318/183551 楔在岗位 11、185716 楔在岗位 3，
# --- 全部卡在 enter_detail 协议链路，Python 0% CPU 等不可中断 read、浏览器主进程 30% 空转，
# --- 6 分钟+ 不自愈。preflight 看门狗只护起飞前，中途靠「心跳文件停摆」检测 + 进程外击杀） ---
#
# #57 第二轮：心跳 payload / 取栈武装 / 节拍注入（取证本体）连同 `SESSION_HEARTBEAT`、
# `SESSION_DONE`、`WATCHDOG_KILLED`、`WEDGE_STACK*`、`SESSION_WATCHDOG_DEFAULT` 这些**文件形状**
# 拆到了 `_forensics.py`（进程外的 `_selfguard` 与台账读的就是它写出的那份形状）。本文件只留
# env 解析（`_parse_session_watchdog`）与「拉起子进程 + 落 done 哨兵」的编排接线。

SESSION_WATCHDOG_ENV = "LIVE_WATCHDOG"


def _parse_session_watchdog(raw):
    """解析 LIVE_WATCHDOG：停摆阈值秒数；0=关闭；未设置/非法/负数回退默认 240。"""
    if raw is None or str(raw).strip() == "":
        return SESSION_WATCHDOG_DEFAULT
    try:
        val = int(str(raw).strip())
    except ValueError:
        print(f"[配置] {SESSION_WATCHDOG_ENV}={raw!r} 不是整数，使用默认 {SESSION_WATCHDOG_DEFAULT}")
        return SESSION_WATCHDOG_DEFAULT
    if val < 0:
        print(f"[配置] {SESSION_WATCHDOG_ENV}={raw!r} 需为非负整数，使用默认 {SESSION_WATCHDOG_DEFAULT}")
        return SESSION_WATCHDOG_DEFAULT
    return val


def _spawn_session_watchdog(session, stall, popen_fn=None):
    """拉起会话中途看门狗子进程，返回 Popen；stall<=0 返回 None。约定同 _spawn_preflight_watchdog。"""
    if stall <= 0:
        return None
    popen_fn = popen_fn or subprocess.Popen
    scripts_dir = str(Path(__file__).resolve().parent)
    src = (
        "import sys\n"
        f"sys.path.insert(0, {scripts_dir!r})\n"
        "import _selfguard\n"
        "sys.exit(_selfguard._heartbeat_watchdog_main(int(sys.argv[1]), float(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]))\n"
    )
    sdir = Path(session.session_dir)
    return popen_fn(
        [
            sys.executable,
            "-c",
            src,
            str(os.getpid()),
            str(stall),
            str(sdir / SESSION_HEARTBEAT),
            str(sdir / SESSION_DONE),
            str(sdir / WATCHDOG_KILLED),
        ],
        cwd=str(ROOT),
    )


def _arm_session_watchdog(session, rebind=False):
    """武装中途看门狗：绑定会话目录 → 首拍心跳 → 拉子进程（解析 env 内聚）。
    rebind=True（preflight 阶段）强制重绑到本会话目录——单例可能残留在同进程上一场
    会话/测试的目录上（20260928 实测跨测试污染），不重绑会写到别的会话目录去。
    off 或首拍失败都不拉——首拍都写不进去的会话必被误杀。"""
    stall = _parse_session_watchdog(os.environ.get(SESSION_WATCHDOG_ENV))
    if stall <= 0:
        return None
    # `_HEARTBEAT_DIR` 是 `_forensics` 的模块级单例（#57 第二轮）：这里按**模块属性**读，
    # 不走近顶部名别名——别名是导入时的快照，读不到 `_heartbeat_bind` 后来的赋值，
    # 会让「已绑定就跳过重绑」这一步恒判未绑定。
    if rebind or _forensics._HEARTBEAT_DIR is None:
        _heartbeat_bind(session.session_dir)
    if not _touch_heartbeat("browser-up"):
        return None
    return _spawn_session_watchdog(session, stall)


def _mark_session_done(session):
    """报告写盘后落 done 哨兵：看门狗退场，之后的 CLOSE 等待按用户节奏，不受停摆检测管。"""
    try:
        (Path(session.session_dir) / SESSION_DONE).touch()
    except OSError as e:
        # 写不进去时看门狗会在停摆阈值点强杀本会话（此后本就只剩收尾提示），必须可观测
        print(f"[warn] done 哨兵写入失败（{e}）——收尾等待期间看门狗可能强杀本会话")
    _touch_heartbeat("done")
    # #56 A4：取消必须排在 done 那一拍之后——done 是会话最后一拍，之后的 CLOSE 等待是
    # 用户节奏（合法无心跳），留着定时器就会落一份假栈冒充楔死现场。
    _cancel_wedge_dump()


def _surface_prior_wedge(session_base, ts):
    """开跑呈报：上一场被看门狗击杀时立刻提醒操作者（楔死→重启→知情的闭环）。
    只看场名早于本场的最新一个目录；取证损坏也报场名，不阻断启动。"""
    try:
        dirs = sorted(d for d in Path(session_base).iterdir() if d.is_dir() and d.name < ts)
    except OSError:
        return
    for d in reversed(dirs):
        marker = d / WATCHDOG_KILLED
        if not marker.exists():
            continue
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            desc = f"停摆 {data.get('stale_seconds')}s @ {data.get(MARKER_LAST_LABEL)}"
            # 相位：哪一代看门狗掐的（两代在 preflight 段并存，先到期者留证）。死在哪一段决定
            # 后面往哪儿查——起飞前自检的点击卡住与会话中途的详情往返卡住不是同一件事。
            # 旧 marker 没有这个键就不显示相位：历史会话目录里的文件改不了，缺项是它的正常读法。
            phase = data.get(MARKER_PHASE_KEY)
            if phase == MARKER_PHASE_PREFLIGHT:
                desc = "起飞前自检段击杀，" + desc
            elif phase == MARKER_PHASE_SESSION:
                desc = "会话中途击杀，" + desc
            # #56 A2：拿到岗位身份就一并呈报（重启前就知道「这场死在哪个岗上」）；
            # 旧取证/降级场景没有这些键 → 照旧只报 label，不因缺项少报一场楔死。
            # 字段名取自 `_forensics.MARKER_IDENTITY_KEYS`（跨进程取证字段名的单一真源）。
            ident = " / ".join(str(v) for v in (data.get(MARKER_LAST_JOB), data.get(MARKER_LAST_EID)) if v)
            if ident:
                desc += f"（岗位 {ident}）"
            # 插页格（#56 取证 B 的 `blocked_shape`）命中就点名写进呈报行：它与「路不通」是两种
            # 处置——取到插页要换出口或等风控冷却，路不通先查本机网络；两者都不该被读成「站点没内容」。
            # 缺键（2026-10-15 之前的旧 marker）与 False 一律不出声：这一格只在命中时才有可报的事实。
            probe = data.get(NET_PROBE_MARKER_KEY)
            if isinstance(probe, dict) and probe.get(NET_PROBE_BLOCKED_KEY):
                desc += f"（出网探针 {probe.get(NET_PROBE_PATH_KEY)} 取回的是站点插页、非内容）"
        except Exception:  # noqa: BLE001
            desc = "取证损坏（细节不可读）"
        print(f"[warn] 上一场 {d.name} 被看门狗强杀（{desc}）——取证 {marker}")
        return


# --- #56 B1 楔死岗跳岗：台账本体（env 取值表 / 读写 / 归因合并 / 回灌 / 呈报 / report 读数）
# --- 在 `_wedge_ledger.py`（本文件顶部别名导入）；以下留的是**消费侧接线**——耦合 session、
# --- pacer、results 与浏览噪声排除的部分，不属于台账本体（#57 第一轮边界）。
# 三个消费点的段落名 + 该处的两条既有语义（是否走 wait_skip / 是否记结果条目），
# 取值一律照**该处 `is_excluded_company` 兄弟分支**的现状：
#   · 搜索/推荐流（`_apply_fresh_jobs`）：excluded 分支记结果条目且 wait_skip → 同款；
#   · 会话内补投轮（`_retry_session_jobs`）：excluded 分支只打一行不记条目不 wait → 同款
#     （同一岗本场已有一条 agent_timeout 结果条目，再记一条会让 #47 分桶把同一个岗算两次）；
#   · 跨会话台账轮（`_retry_pending_jobs`）：excluded 分支只打一行 → 本切片**额外记一条结果
#     条目**，因为「从补投队列里移出」（`apply_retry_outcome` 的 no_jd 分支）是票面点名要看见的
#     动作，只落终端会丢审计。
_WEDGE_STAGES = {
    "fresh": ("搜索/推荐流", True, True),
    "session_retry": ("会话内补投轮", False, False),
    "pending": ("跨会话台账轮", False, True),
}


def _prepare_wedge_ledger(session, session_base, ts, applied_ids):
    """B1 开跑三步（`_init_live_session` 的唯一接线点）：回灌写入 → 载入工作副本 → 呈报。
    两个「不写」的方向（④）：开关关→不回灌也不写（关掉止损机制不该还偷偷写台账），
    但仍呈报并明示「本场不跳过」；dry-run→不回填不写（#55 验收 4 同款约束），仍呈报。
    工作副本落 `state["wedge_eids"]`（eid → 条目）：三个消费点查表不再碰磁盘——跳岗判定
    只看开跑那一刻的快照（含本场刚回灌进来的岗位），会话中途不会有第二份真相冒出来。
    读数落 `state["wedge_ledger"]`，report.json 的 `wedge_ledger` 字段即此。
    `applied_ids`（#58 T2）：同一场开跑已经读出来的已投台账（`load_applied_ids` 的结果，
    与投递主循环过滤岗位用的是**同一份**，不开第二份真相）——回灌侧的正向守卫靠它。"""
    cfg, state = session.cfg, session.state
    raw_path = cfg.get("wedge_skipped_path")
    if not raw_path:
        print("[warn] 楔死台账路径未接线（cfg['wedge_skipped_path'] 缺失）——本场不做楔死岗跳过")
        return
    path = Path(raw_path)
    enabled = bool(cfg.get("wedge_skip_enabled"))
    dry_run = bool(state.get("dry_run"))
    stats = _new_wedge_stats()
    if enabled and not dry_run:
        stats = _backfill_wedge_ledger(session_base, ts, path, applied_ids)
    entries = _load_wedge_skipped(path)
    eids = {}
    for e in entries:
        eid = e.get("encryptJobId")
        if isinstance(eid, str) and eid:
            eids.setdefault(eid, e)
    state["wedge_eids"] = eids
    # 读数键名在这里定一次（`booked`→`backfilled`），report.json 与 [done] 行都读这份：
    # 回灌函数的统计词是它自己的口径，报表口径不该跟着内部命名漂。
    state["wedge_ledger"] = {
        "enabled": enabled,
        "total": len(entries),
        "backfilled": stats["booked"],
        "rejected": stats["rejected"],
        "already_applied": stats["already_applied"],  # #58 T2：已投出的岗出现在取证里被拦下的场次数
        "scanned": stats["scanned"],
        "added": stats["added"],
        "merged": stats["merged"],
        "duplicates": stats["duplicates"],
        "written": stats["written"],
        "dry_run": dry_run,
    }
    if dry_run:
        # 空台账也要念：演练场「不动台账」这件事本身是读者需要知道的一次降级（不然
        # 「台账没长」会被读成「这台机器没有楔死过」）。
        print("[跳岗] 演练会话（LIVE_DRY_RUN=1）不动台账：不回填历史取证、不写文件；跳过判定照常生效")
    _surface_wedge_ledger(entries, enabled)


def _wedge_entry_for(session, eid):
    """B1③ 三个消费点共用的查表入口，条目取自 `_wedge_ledger_map`（#56 修复轮 F3：与
    `_noise_exclusions` 同一份「开关 + 键归一」口径，非字符串 eid 不再在两处表现不同）。
    开关为 off 时恒返回 None——「临时放行」的判断只长在 `wedge_skip_enabled` 那一处，
    三个消费点就不会各自长出一个开关判断（漏一个就是本次要治的重复消耗）。"""
    return _wedge_ledger_map(session).get(str(eid)) if eid else None


def _handle_wedge_skip(session, job, entry, stage):
    """B1③ 跳过动作（三处共用，形状照该处的 `is_excluded_company` 分支）：
    一行讲清为什么跳 + 计数明细（进 report 的 `wedge_ledger.skipped_jobs`）+
    按该处既有语义决定是否记 `wedge_skipped` 结果条目、是否走 `pacer.wait_skip()`
    （取值见 `_WEDGE_STAGES` 的注释）。
    导航语义：与 excluded 分支一致，**不做** `safe_go_back`——读代码确认过，excluded 分支
    只 `wait_skip()` + `continue`，因为岗位还没进详情、页面仍停在列表上，此时回退等于把
    列表翻页/滚掉，下一岗反而找不到入口。
    状态用 `wedge_skipped` 而不是复用 `excluded_company`：两者代价语义不同——excluded 是
    「用户按简历决定不投这家公司」，本状态是「这个岗把会话楔死过，为止损不再碰它（可能漏投）」。"""
    desc, paces, records = _WEDGE_STAGES[stage]
    eid = job.get("encryptJobId") or entry.get("encryptJobId")
    count = entry.get("wedge_count") or 1
    print(
        f"  [跳岗] {job.get('jobName')}（eid {eid}）在楔死台账第 {count} 次"
        f"（最近一场 {entry.get('session')}，死在 {entry.get('last_label')}）——{desc}不投不补"
        f"（#56 B1；代价：可能漏投一个达标岗，放行 {WEDGE_LEDGER_ENV}=off）"
    )
    detail = {
        "encryptJobId": eid,
        "job": job.get("jobName"),
        "brand": job.get("brandName"),
        "wedge_count": count,
        "last_session": entry.get("session"),
        "stage": desc,
    }
    session.state.setdefault("wedge_skips", []).append(detail)
    if records:
        # 结果条目即明细本身（岗位/品牌/eid/第几次楔/最近哪场/在哪一段被跳），不再叠第二份字段
        session.results.append({"status": "wedge_skipped", **detail})
    if paces:
        session.pacer.wait_skip()
    _logging.log_event("wedge_job_skipped", **detail)


def _parse_retention_days(raw):
    """解析 LIVE_RETENTION_DAYS：天数；0=关闭；未设置/非法/负数回退默认 7。"""
    if raw is None or str(raw).strip() == "":
        return RETENTION_DEFAULT
    try:
        val = int(str(raw).strip())
    except ValueError:
        print(f"[配置] {RETENTION_ENV}={raw!r} 不是整数，使用默认 {RETENTION_DEFAULT}")
        return RETENTION_DEFAULT
    if val < 0:
        print(f"[配置] {RETENTION_ENV}={raw!r} 需为非负整数，使用默认 {RETENTION_DEFAULT}")
        return RETENTION_DEFAULT
    return val


def sweep_expired_data(video_base, session_base, retention_days, now, rmtree_fn=None, log=print):
    """清理超期产物（2026-09-26 用户约定，随会话启动执行）：
    - video_base/<ts>/ 整目录删除（webm 原片与剪辑）；
    - session_base/<ts>/shots/ 整目录删除——会话的 request/reply/report 保留
      （台账与 EDD/followup 消费它们，且体量小）；
    - 只动目录名匹配 YYYYMMDD-HHMMSS 的目录，其他名字一律不碰；
      「超过 retention_days」为严格大于，恰好第 7 天保留；retention_days<=0 直接跳过。
    返回已删除路径列表。"""
    if retention_days <= 0:
        return []
    rmtree_fn = rmtree_fn or shutil.rmtree
    removed = []
    for base, shots_only in ((video_base, False), (session_base, True)):
        base = Path(base)
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            if not _TS_DIR_RE.match(entry.name) or not entry.is_dir():
                continue
            target = entry / "shots" if shots_only else entry
            if not target.is_dir():
                continue
            ts = datetime.strptime(entry.name, "%Y%m%d-%H%M%S")
            age_days = (now - ts).total_seconds() / 86400
            if age_days > retention_days:
                rmtree_fn(target)
                removed.append(str(target))
                log(f"[清理] 超期 {age_days:.0f} 天，已删除 {target}")
    return removed


def _spawn_preflight_watchdog(sentinel, timeout, heartbeat, marker, popen_fn=None):
    """拉起进程外看门狗子进程，返回 Popen；timeout<=0 返回 None。约定同 _spawn_session_watchdog。
    子进程 import 同一 `_selfguard._watchdog_main`（单一实现）；
    父进程不 join 不 wait——楔死场景下任何等待都不可靠，子进程自会因
    哨兵出现/父进程死亡而退出。
    `heartbeat` / `marker` 是击杀取证的读写点（2026-10-07 补的口：那一代快看门狗杀完什么都不留，
    那一格「那一刻这台机器通不通」至今空着），文件名一律用 `_forensics` 的别名、不写字面量。"""
    if timeout <= 0:
        return None
    popen_fn = popen_fn or subprocess.Popen
    scripts_dir = str(Path(__file__).resolve().parent)
    src = (
        "import sys\n"
        f"sys.path.insert(0, {scripts_dir!r})\n"
        "import _selfguard\n"
        "sys.exit(_selfguard._watchdog_main(int(sys.argv[1]), float(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]))\n"
    )
    return popen_fn(
        [
            sys.executable,
            "-c",
            src,
            str(os.getpid()),
            str(timeout),
            str(sentinel),
            str(heartbeat),
            str(marker),
        ],
        cwd=str(ROOT),
    )


def _preflight_guarded(page, session):
    """带进程外看门狗的起飞前自检：
    - LIVE_PREFLIGHT_WATCHDOG>0（默认 180）时拉看门狗，0=off 不拉；
    - 无论 preflight 正常返回还是抛异常，finally 落哨兵 preflight_ok 让看门狗自行退场；
    - 真楔死时父进程收不到任何 Python 异常，由看门狗强杀整树兜底。
    本段两代看门狗并存（下面 `rebind=True` 那次武装就是②），180s 的①通常先到期——所以①也必须
    留证（读同一份心跳、写同一份 marker，靠 `watchdog_phase` 认出是谁掐的）。"""
    timeout = _parse_preflight_watchdog(os.environ.get(PREFLIGHT_WATCHDOG_ENV))
    if timeout > 0:
        # 不持有返回值：父进程不 join 不 wait（楔死场景下任何等待都不可靠），
        # 子进程自会因哨兵出现/父进程死亡而退出。
        _spawn_preflight_watchdog(
            session.session_dir / "preflight_ok",
            timeout,
            session.session_dir / SESSION_HEARTBEAT,
            session.session_dir / WATCHDOG_KILLED,
        )
    _arm_session_watchdog(session, rebind=True)  # 心跳版：preflight 段也纳入停摆检测+取证覆盖
    try:
        return _preflight_click_check(page, session)
    finally:
        try:
            (session.session_dir / "preflight_ok").touch()
        except OSError as e:
            # 哨兵写不进去时看门狗会在超时点强杀整树——正常会话也可能被误杀，必须可观测
            print(f"[warn] 看门狗哨兵写入失败（{e}）——若后续无进展，看门狗超时后将强杀本会话")


# 起飞前自检的候选选择器（顺序与旧实现一致，只是从「取第一命中」改成「在第一命中里挑可见的」）。
PREFLIGHT_CARD_SELECTORS = ("a[href*='/job_detail/']", "[class*='job-card']", ".job-card-box")


def _preflight_visible_card(page):
    """列表页上**第一张可见**的岗位卡片，返回 `(元素或 None, 候选总数, 可见数)`。

    为什么要挑可见的（2026-10-09 09:28 会话 `20261009-092824` 实测）：旧写法取
    `query_selector` 的第一命中、不看可见性，选中隐藏节点后 `click_with_retry` 四步全抛
    `Element is not visible` ⇒ 自检返回 False ⇒ 整场不开跑、零投递，而 `shots/` 里一张图都没有，
    事后分不开「真点不动」与「选中了隐藏节点」。
    可见性判据用 `is_visible()` 而不是 `bounding_box()`：本仓真点击路径用的就是前者
    （`chat_first.find_comm_button` / `chat_first.dismiss_overlays` / 本文件的按钮判定都走 `is_visible()`），
    而 `Element is not visible` 正是 Playwright 按同一条判据报出来的——自检与投递共用一套判据，
    自检挑出来的卡才是「投递也点得动」的卡；`bounding_box()` 只给坐标点击取几何，隐藏节点照样
    可能有盒，拿它当可见性判据会挑出同一种点不动的卡。
    逐选择器扫：**一旦**某组里出现可见卡就停手不查后面的组（省掉健康那一路的协议往返）；
    全不可见时三组扫完，于是「候选 N / 可见 0」是**全量**读数而不是第一条选择器的局部读数。
    `is_visible()` 自身抛异常按**不可见**计：判不出可见性就没有「点得动」的证据，硬着头皮交给
    `click_with_retry` 也是同样的结局，但那样就变成「一次没必要的点击」而不是「一条读数」。
    """
    chosen = None
    total = visible = 0
    for sel in PREFLIGHT_CARD_SELECTORS:
        try:
            found = page.query_selector_all(sel) or []
        except Exception:  # noqa: BLE001  # 这一组查不动（驱动异常）就换下一组，形态同旧实现
            found = []
        total += len(found)
        for el in found:
            try:
                ok = bool(el.is_visible())
            except Exception:  # noqa: BLE001  # 判据本身失败 ⇒ 按不可见计，理由见 docstring
                ok = False
            if not ok:
                continue
            visible += 1
            if chosen is None:
                chosen = el
        if chosen is not None:
            break
    return chosen, total, visible


def _preflight_click_failed(page, session, detail):
    """自检判「不开跑」的唯一收口：先留现场，再落结果条目，恒返回 False。
    截图是观测层（#38/#54 口径：观测不许新增失败面）——自身失败只 warn，**判定结果一字不改**；
    `shot()` 已经吞掉 page.screenshot 的异常，这里兜的是 `session.shots_dir` 之类的取用失败。
    2026-10-09 09:28 那场 `shots/` 全空、事后无从分辨「真点不动」与「选中隐藏节点」，补的就是这一格。
    """
    try:
        shot(page, session.shots_dir, "preflight-click-failed")
    except Exception as e:  # noqa: BLE001
        print(f"    [warn] 自检失败存证截图没落（{str(e)[:60]}）——判定结果不变")
    session.results.append({"job": None, "brand": None, "status": "preflight_click_failed", "detail": detail})
    return False


def _preflight_click_check(page, session):
    """起飞前自检（#44）：正式投递前先验证「点击通道是通的」，不通就不开跑。

    与 `_pre_job_stop` 里的动作层熔断是**互补的两段**：熔断在会话中途（有样本后）拦，
    自检在**会话开始前**拦——后者更省：一次都点不动就别白跑十几个岗、白耗日额度与
    风控暴露面。

    点的是**无害目标**：列表里第一张**可见**的岗位卡片（挑法见 `_preflight_visible_card`），
    只进详情，**不发任何东西、不碰「立即沟通」**，所以不存在误投风险。自检自身出意外（导航慢、
    页面没卡片）时**不阻断**——那只是自检没做成，交给中途熔断兜底，避免因自检脆弱而误杀正常会话。
    用 LIVE_PREFLIGHT=off 关闭。
    """
    if os.environ.get("LIVE_PREFLIGHT", "on").strip().lower() in ("off", "0", "false", "no"):
        print("[自检] 起飞前自检已关闭（LIVE_PREFLIGHT=off）")
        return True
    try:
        page.goto(LIVE_RECOMMEND_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
    except Exception as e:  # noqa: BLE001
        print(f"[自检] 列表页打开失败（{str(e)[:60]}）——跳过自检，由中途熔断兜底")
        return True
    card, total, visible = _preflight_visible_card(page)
    if not total:
        print("[自检] 列表页未取到卡片元素——跳过自检，由中途熔断兜底")
        return True
    # 两个数一起打：「候选 0」与「候选 20 / 可见 0」从此是两种读数（前者跳过自检、后者不开跑）
    print(f"[自检] 卡片候选 {total} 个 / 可见 {visible} 个（取第一张可见的）")
    if card is None:
        print(f"[自检] {total} 张候选全部不可见——本次不开跑")
        return _preflight_click_failed(page, session, f"all {total} card candidates invisible")
    try:
        # #56 A3：起飞前自检走的是同一个 click_with_retry（015515 那场楔死就在这一段）——
        # 接上拍点后，preflight 段的停摆也答得出停在哪个无超时调用上。
        if not click_with_retry(page, card, action="自检卡片", beat_fn=_heartbeat_beat):
            raise RuntimeError("card click returned False")
        print("[自检] 点击通道正常（列表卡片可点），开始投递")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[自检] 点击通道异常：卡片点击超时（{str(e)[:80]}）——本次不开跑")
        return _preflight_click_failed(page, session, str(e)[:200])


def _record_breaker(session, **fields):
    """#51：熔断/停批事件记入 state["breakers"]——收尾建票（_issue_reporter）的数据源。
    与 log_event（机读日志）和 notify（人读通知）并列第三消费者；只存机械字段，
    脱敏在渲染层兜（白名单 + scrub）。观测性质，绝不抛出。"""
    try:
        rec = dict(fields)
        rec["ts"] = datetime.now().isoformat(timespec="seconds")
        session.state.setdefault("breakers", []).append(rec)
    except Exception as e:  # noqa: BLE001
        # 不阻断投递，但不许静默（de98212 先例：哨兵写失败静默会被误杀掩盖）——
        # 记录失败意味着收尾开票草稿会缺这条事件，必须可见
        print(f"[warn] 熔断事件记录失败（{str(e)[:80]}）——该事件将不进收尾建票草稿")


# #54：浏览器退出原因分类——启动期经进程树两跳（python → node driver → camoufox/firefox）
# 采集浏览器 pid；首次 browser-closed 类异常时 os.kill(pid, 0) 无损探活 + 检索 macOS
# DiagnosticReports 近 N 分钟崩溃报告，分类落 state["browser_exit"] → report.json 顶层
# 与收尾 [done] 行。归因有据，读报告者不必再猜「浏览器为什么没了」（2026-09-26 实测：
# 进程被外部终结，agent 先入为主按缺陷排查）。观测层：任何失败只 warn 降级，绝不阻断投递。
BROWSER_CLOSED_RE = re.compile(r"(?:browser|context|page) has been closed", re.IGNORECASE)
CRASH_REPORT_DIR = Path.home() / "Library/Logs/DiagnosticReports"
CRASH_REPORT_WINDOW_MIN = 10  # 近 N 分钟内的崩溃报告才算同场证据
BROWSER_EXIT_DESC = {
    "browser_alive": "浏览器进程仍在——页面级异常（页面/context 被关，浏览器没死）",
    "crash_suspected": "疑似崩溃（DiagnosticReports 有浏览器崩溃报告）",
    "external_kill": "外部终结（待确认）——进程已死且无近期崩溃报告",
    "unknown": "无法分类（启动期未采集到浏览器 pid）",
}


def _pgrep_children(parent_pid, pattern):
    """pgrep 列出 parent 进程下名字匹配 pattern 的子 pid；pgrep 不可用/无命中返回 []。"""
    try:
        out = subprocess.run(["pgrep", "-P", str(parent_pid), pattern], capture_output=True, text=True, timeout=5)
        return [int(x) for x in out.stdout.split()]
    except Exception:  # noqa: BLE001
        return []


def discover_browser_pids(self_pid=None, pgrep_fn=_pgrep_children):
    """进程树两跳定位 Camoufox 主进程：python(self) → node driver → camoufox/firefox
    （Playwright 驱动是 node 子进程，浏览器是驱动的直接子进程）。多命中全保留——
    探活时任一存活即视为浏览器还在。采集失败返回 []。"""
    pids = []
    for node_pid in pgrep_fn(self_pid if self_pid is not None else os.getpid(), "node"):
        pids.extend(pgrep_fn(node_pid, "camoufox|firefox"))
    return pids


def _probe_alive(pid, kill_fn=os.kill):
    """os.kill(pid, 0) 无损探活：True=在；False=已死（ESRCH）；EPERM=在（属主不同）。"""
    try:
        kill_fn(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:  # noqa: BLE001
        return False


def find_crash_report(now=None, report_dir=None, window_min=CRASH_REPORT_WINDOW_MIN):
    """DiagnosticReports 近 window_min 分钟内浏览器二进制的崩溃报告（*.ips，文件名含
    camoufox/firefox），取最新；目录缺失（非 macOS / CI）优雅降级返回 None。"""
    d = report_dir if report_dir is not None else CRASH_REPORT_DIR
    try:
        if not d.is_dir():
            return None
        cutoff = (now or datetime.now()).timestamp() - window_min * 60
        newest, newest_mtime = None, cutoff
        for f in d.glob("*.ips"):
            name = f.name.lower()
            if "camoufox" not in name and "firefox" not in name:
                continue
            mtime = f.stat().st_mtime
            if mtime >= newest_mtime:
                newest, newest_mtime = f, mtime
        return newest
    except Exception:  # noqa: BLE001
        return None


def classify_browser_exit(pids, kill_fn=os.kill, crash_fn=find_crash_report):
    """分类判定（纯机械，测试用替身桩注入探活与崩溃报告）：pid 任一存活 → browser_alive
    （页面级异常，浏览器没死）；全死 → 查崩溃报告，有 → crash_suspected，无 →
    external_kill（待确认——macOS 不告知关闭者身份）；pid 未采集 → unknown 不猜。
    返回 (classification, evidence)。"""
    if not pids:
        return "unknown", {"pids": [], "detail": "启动期未能采集浏览器进程 pid"}
    alive = [p for p in pids if _probe_alive(p, kill_fn)]
    if alive:
        return "browser_alive", {"pids": pids, "alive": alive}
    report = crash_fn()
    if report is not None:
        return "crash_suspected", {"pids": pids, "crash_report": str(report)}
    return "external_kill", {"pids": pids, "crash_report": None, "note": "无近期崩溃报告，待确认"}


def note_browser_exit(state, err_msg, now=None):
    """#54：首次 browser-closed 类异常时分类浏览器进程去向，写 state["browser_exit"]
    （首见为准，后续同类异常不覆盖）。观测层：绝不抛出，返回是否写入。"""
    if state.get("browser_exit") or not BROWSER_CLOSED_RE.search(err_msg or ""):
        return False
    try:
        classification, evidence = classify_browser_exit(state.get("browser_pids") or [])
        state["browser_exit"] = {
            "classification": classification,
            "detected_at": (now or datetime.now()).isoformat(timespec="seconds"),
            "evidence": evidence,
        }
        print(f"[浏览器] 退出分类: {BROWSER_EXIT_DESC.get(classification, classification)}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 浏览器退出分类失败（不影响投递）: {str(e)[:80]}")
        return False


def _record_browser_pids(session, discover_fn=discover_browser_pids):
    """#54 启动期：采集 Camoufox 主进程 pid 存 state["browser_pids"]（退出分类的探活
    依据）。采集失败降级空表 + warn——此时退出分类将落 unknown，不静默。"""
    try:
        pids = discover_fn()
        session.state["browser_pids"] = pids
        if pids:
            print(f"[浏览器] Camoufox 进程 pid 采集: {', '.join(str(p) for p in pids)}")
        else:
            print("[warn] 未能定位 Camoufox 进程 pid——浏览器退出分类将不可用（不阻断投递）")
    except Exception as e:  # noqa: BLE001
        session.state["browser_pids"] = []
        print(f"[warn] Camoufox pid 采集失败（不影响投递）: {str(e)[:80]}")


def _pre_job_stop(session, run):
    """单岗开工前的停止检查（#39）：日上限 / 静默时段 / CLOSE 哨兵 / **动作层熔断（#44）**。
    静默时段在会话中途跨午夜进入时同样要停（2026-09-09 23:57 实测缺口）。返回 True 即停整轮。

    #44 动作层熔断与既有的 risk_blocked 风控熔断是**两道独立的闸**：风控那道拦「页面弹出
    验证码/安全验证」，这道拦「动作链路自己坏了」——点击三次全超时但页面照常渲染、按钮
    也在场。2026-09-23 第二次验收实跑 16 岗 applied=0 就是后者，跑完才知道，白耗额度。
    """
    if session.state["applied"] >= run.limit:
        return True
    st = session.state
    if st.get("consec_no_btn", 0) >= CLICK_FAIL_STOP:
        print(
            f"[停] 连续 {st['consec_no_btn']} 次「立即沟通」点击未成功（页面正常、按钮在场）"
            f"——动作链路异常，停止投递（非风控信号）"
        )
        _logging.log_event(
            "circuit_break",
            level="error",  # #48：安全事件默认落盘
            breaker="consec_no_btn",
            count=st["consec_no_btn"],
            applied=st["applied"],
            detail_entered=st.get("detail_entered", 0),
            direct_fallback=st.get("direct_fallback", 0),
            detail="连续「立即沟通」点击失败，页面无验证码（疑动作链路退化或账号限流）",
            stage="pre_job_stop",
        )
        _record_breaker(
            session, breaker="consec_no_btn", count=st["consec_no_btn"], threshold=CLICK_FAIL_STOP, stage="pre_job_stop"
        )
        notify(
            "Live 投递熔断",
            f"连续 {st['consec_no_btn']} 次「立即沟通」点击未成功，已停批"
            f"（非投递失败；页面无验证码，疑动作链路退化或账号限流）",
        )
        return True
    # #50：卡片通道退化判定——分母仅计算卡片点击尝试总数（card_clicks + card_click_fails），
    # 彻底排除推荐流/DOM 无卡片直跳（direct_no_card）导致的假阳性。
    card_clicks = st.get("card_clicks", 0)
    card_fails = st.get("card_click_fails", 0)
    card_attempts = card_clicks + card_fails
    # 兼容直接向 state 传入 legacy 口径的测试/夹具：若未显式记录 card_clicks/fails 且未记录 direct_no_card
    if card_attempts == 0 and "card_clicks" not in st and "card_click_fails" not in st and "direct_no_card" not in st:
        card_attempts = st.get("detail_entered", 0)
        card_fails = st.get("direct_fallback", 0)

    if card_attempts >= DIRECT_FALLBACK_MIN:
        rate = card_fails / card_attempts
        if rate >= DIRECT_FALLBACK_STOP:
            click_msg = (
                f"卡片点击通道退化 {rate:.0%}（{card_fails}/{card_attempts}）"  # #47：print/notify 同源，防口径漂移
            )
            print(f"[停] {click_msg}——入口动作全线失效，停止投递")
            _logging.log_event(
                "circuit_break",
                level="error",  # #48：安全事件默认落盘
                breaker="direct_fallback",
                rate=round(rate, 3),
                direct_fallback=st.get("direct_fallback", 0),
                card_click_fails=card_fails,
                card_attempts=card_attempts,
                detail_entered=st.get("detail_entered", 0),
                applied=st["applied"],
                detail="卡片点击退化率超阈（卡片点击失败占比），入口动作全线失效",
                stage="pre_job_stop",
            )
            _record_breaker(
                session,
                breaker="direct_fallback",
                rate=round(rate, 3),
                fails=card_fails,
                attempts=card_attempts,
                threshold=DIRECT_FALLBACK_STOP,
                stage="pre_job_stop",
            )
            notify(
                "Live 投递熔断",
                f"{click_msg}，已停批——非投递结果失败",
            )
            return True
    quiet = session.cfg["quiet_hours"]
    if is_quiet_hour(current_hour(), quiet):
        print(f"[停] 进入静默时段 {quiet[0]}-{quiet[1]} 点，停止投递")
        notify("Live 投递暂停", f"静默时段 {quiet[0]}-{quiet[1]} 点，已投 {session.state['applied']} 个")
        return True
    if (session.session_dir / CLOSE_SENTINEL).exists():
        print("[停] 收到 CLOSE 信号，优雅停止")
        return True
    return False


def _apply_one_job(page, job, idx, session):
    """单岗投递 + 单岗级异常边界（#39）：返回 "stop" / "relogin" / None（继续下一岗）。
    单岗意外异常不崩整轮（2026-09-10 实测 go_back 超时崩掉整轮的教训）：存证后跳过，
    safe_go_back 已兜底导航。与 keyword_round 的关键词级边界构成两级保护。"""
    try:
        return apply_live(
            page,
            job,
            session.session_dir,
            idx,
            session.shots_dir,
            session.cfg,
            session.pacer,
            session.platform,
            session.results,
            session.state,
        )
    except Exception as e:
        print(f"  [失败] 单岗异常: {str(e)[:120]}")
        note_browser_exit(session.state, str(e))  # #54：browser-closed 类异常在此分类
        try:
            session.platform.save_evidence(page, f"live-unexpected-{idx}", job)
        except Exception:
            pass
        session.results.append(
            {"idx": idx, "job": job.get("jobName"), "status": "page_error", "detail": f"未预期异常: {str(e)[:150]}"}
        )
        session.pacer.wait_skip()
        safe_go_back(page)
        return None


def _apply_fresh_jobs(page, run, fresh, idx):
    """把一轮新岗位逐个投递（#33 提取）：关键词轮与推荐流轮共用同一段投递内层循环——
    两个来源各自维护一份循环，迟早出现「一边改了另一边没改」的行为漂移。返回 (stop, idx)。"""
    session = run.session
    for job in fresh:
        if _pre_job_stop(session, run):
            return True, idx
        # 简历出现过的公司不投递（用户约定 2026-09-10，列表在 private/data/excluded_companies.json）
        if is_excluded_company(job.get("brandName"), run.excluded):
            print(f"  [跳过] 简历公司 {job.get('brandName')}，不投递")
            session.results.append(
                {"job": job.get("jobName"), "brand": job.get("brandName"), "status": "excluded_company"}
            )
            session.pacer.wait_skip()
            continue
        # #56 B1③ 消费点 1（搜索流与推荐流共用的这条内层循环）：楔死过的岗不再进投递链路。
        # 形状照上面的 excluded 分支（票面点名的同款消费点）：呈报 + 记结果 + wait_skip + continue。
        wedge = _wedge_entry_for(session, job.get("encryptJobId"))
        if wedge is not None:
            _handle_wedge_skip(session, job, wedge, "fresh")
            continue
        idx += 1
        outcome = _apply_one_job(page, job, idx, session)
        if outcome == "stop":
            return True, idx
        if outcome == "relogin":
            notify("登录态过期", "请在浏览器中重新登录后重跑 live_apply")
            return True, idx
    return False, idx


def _recommend_round(page, run, idx):
    """推荐流轮（#33）：取「职位」页的推荐列表岗位投递，额度口径同关键词轮（剩余额度 ×
    采集余量）。开轮位置由调用方决定——#61 方案 A 起它在关键词轮**之前**（理由见
    _execute_query_rounds 的注释）。与关键词轮同构（取列表 → 拟人浏览 → 逐岗投递）。
    消费侧另受 #65 配额截断（cfg["recommend_cap"]，0 = 不限）——它只管「吃多少个」，
    采集侧的 `recommend_listing` / `recommend_scrolls` 读数照旧反映真实召回深度。
    轮级异常边界：推荐流失败只记账后跳过——不得影响已完成的搜索流投递，也不得终结整场会话。"""
    session = run.session
    print("\n[推] 推荐列表（顶栏「职位」无 query）")
    flags = {}
    try:
        listing = recommend_list(
            page,
            limit=session.cfg.get("collect_cap"),
            target=_collect_target(run, session.cfg.get("collect_cap")),
            flags=flags,
        )
        fresh = collect_new_jds(listing, run.collected, run.applied_ids)
    except Exception as e:
        session.state.setdefault("recommend_errors", []).append(str(e)[:150])
        note_browser_exit(session.state, str(e))  # #54：browser-closed 类异常在此分类
        print(f"    [跳过] 推荐流失败（转收尾）: {str(e)[:100]}")
        return False, idx
    session.state["recommend_listing"] = len(listing)
    session.state["recommend_scrolls"] = flags.get("rounds", 0)
    print(
        f"    拿到 {len(listing)} 条（滚 {flags.get('rounds', 0)} 轮，到底={flags.get('at_bottom')}），新岗位 {len(fresh)} 个"
    )
    # #65 消费配额：上面两行采集读数先落账再截断，故截断改不动它们（本票与「改采集上限」的分界）。
    cap = session.cfg.get("recommend_cap") or 0  # 键缺失按 0=不限，形态同 cfg["collect_cap"]
    consumed = len(fresh) if not cap else min(len(fresh), cap)
    session.state["recommend_candidates"] = len(fresh)
    session.state["recommend_consumed"] = consumed
    if not fresh:
        return False, idx
    if not cap:
        print(f"    [配额] 推荐轮候选 {len(fresh)} 个 / 消费 {consumed} 个 / 未消费 0 个（LIVE_RECOMMEND_CAP=0，不限）")
    elif consumed < len(fresh):
        # 静默截断是本票要防的病（形态同 #47「宁可显眼不可静默」）：三个数、代价、退路都要在行里。
        print(
            f"    [配额] 推荐轮候选 {len(fresh)} 个 / 消费 {consumed} 个 / 未消费 {len(fresh) - consumed} 个"
            "——未消费的岗本场不投也不进台账（不是超时、不是卡死，是没轮到），这是 #65 登记的代价；"
            "要放开就设 LIVE_RECOMMEND_CAP=0，要整轮关掉用 LIVE_RECOMMEND=off"
        )
    _remember_list_url(page)
    # 噪声排除按**完整候选池**（#44 语义不变）：未消费的岗也不该被随机点开——点开照样白耗一次详情往返。
    _browse_list(page, session, "recommend", exclude_ids=_noise_exclusions(session, fresh))
    _remember_list_url(page)
    return _apply_fresh_jobs(page, run, fresh[:consumed], idx)


def _collect_target(run, cap):
    """本轮采集目标条数（#33 第七轮）：剩余额度 × COLLECT_SLACK，再受采集上限封顶。
    留 3 倍余量是因为候选还要过相关性 gate、跨来源去重与简历公司过滤；额度已满返回 0，
    调用方按 0 处理（不再采集）。上限为 0 表示不限，此时只受滚动轮次与到底判据约束。"""
    remaining = run.limit - run.session.state["applied"]
    if remaining <= 0:
        return 0
    target = remaining * COLLECT_SLACK
    return min(target, cap) if cap else target


def _span_start(session, idx):
    """轮次跨度的起点快照（#61 验收 2，与 _span_end 成对）：记下「本轮开跑前 results 已有
    几条」与「本轮要发出的第一个编号」。起点必须在轮次开跑前取，终点在轮次返回后取——
    中间该轮往 session.results 追加的条目才都落进这段跨度。"""
    return len(session.results), idx + 1


def _span_end(session, source, query, start, idx):
    """把一轮的编号跨度落进 state["source_spans"]（#61 验收 2 的唯一写入点）。
    只在三处调用：关键词轮 `_apply_fresh_jobs` 之后、推荐流轮 `_recommend_round` 之后、
    #55 两个补投轮的逐岗循环之后（本会话补投与跨会话补投都算 "retry"）。

    本轮没发过号就不落账（idx 未推进——`_pre_job_stop` 命中首岗、候选全被 excluded/楔死
    跳过、推荐流取列表失败直接 return）：没有编号就没有可归属的对象，落一条空跨度只是给
    build_report 多一个「看着能归属」的假区间。

    跨度除编号区间外还带 res_from/res_to（results 列表的切片区间），原因见 _result_source：
    跨会话补投轮与搜索流的编号是两个各自从 1 起的区间，只看 idx 会把「台账里那一封」读成
    「搜索里那一封」。"""
    res_from, idx_from = start
    if idx < idx_from:
        return
    session.state.setdefault("source_spans", []).append(
        {
            "source": source,
            "query": query,
            "idx_from": idx_from,
            "idx_to": idx,
            "res_from": res_from,
            "res_to": len(session.results),
        }
    )


def _execute_query_rounds(page, run):
    """轮次段（#39）：一段推荐流轮（#33；LIVE_RECOMMEND=off 时跳过）+ 逐个关键词轮
    「搜索 → 列表拟人浏览 → 逐岗投递」，直到停批信号。推荐流轮在前（#61 方案 A）。
    三级异常边界在此收敛：关键词级在 keyword_round 内记账后跳过，推荐流级在
    _recommend_round 内记账后跳过，单岗级在 _apply_one_job 内存证后跳过——任何一岗、
    一个关键词或推荐流的一轮异常都不得终结整场会话。"""
    session = run.session
    if not _preflight_guarded(page, session):
        notify("Live 投递未启动", "起飞前自检：点击通道不可用（列表卡片点击超时），本次未投递")
        return
    cap = session.cfg.get("collect_cap")
    idx = 0
    stop = False
    # #61 方案 A：推荐流前置——开轮挪到关键词轮之前，且开轮不再看停批信号（见
    # _recommend_turn_open）。票面实测：会话 20261005-215607 的 report.json 里
    # recommend_listing / recommend_scrolls 全 null、recommend_errors 为空，即推荐流整段没跑。
    # 旧位置（全部关键词轮之后）+ 旧条件里的 `not stop` 叠加，使任何一个停批信号（跨入静默
    # 时段、日限/额度耗尽、风控熔断、动作链路熔断、用户写 CLOSE）都会把推荐流**主动跳过**，
    # 而不只是「没轮到」——第二岗位来源恰好在会话要收批时缺席。停批语义没有放松，改由逐岗
    # 预检承担，核实过的因果链写在 _recommend_turn_open 的 docstring。
    # 代价（产品决策，不是缺陷）：推荐流原来「用剩余额度」，前置后变成「先吃额度」——
    # _collect_target(run, cap) 按剩余额度 × COLLECT_SLACK 计，会随已投数自动收缩，于是后面
    # 每个关键词轮的候选目标条数变小；额度被推荐流吃满时关键词轮不再有投递额度。退路是现成
    # 的 LIVE_RECOMMEND=off（关掉即回到纯搜索流），不新造阈值。
    if _recommend_turn_open(run):
        span = _span_start(session, idx)
        stop, idx = _recommend_round(page, run, idx)
        _span_end(session, "recommend", None, span, idx)  # #61 验收 2：推荐来源的跨度
    for q in run.queries:
        if stop or session.state["applied"] >= run.limit:
            break
        print(f"\n[搜] {q}")
        fresh = keyword_round(page, q, session.state, run.applied_ids, run.collected, target=_collect_target(run, cap))
        if fresh is None:
            _save_keyword_evidence(page, session, q)
            rnd_sleep()
            continue
        if not fresh:
            rnd_sleep()
            continue
        _remember_list_url(page)
        _browse_list(page, session, q, exclude_ids=_noise_exclusions(session, fresh))
        _remember_list_url(page)
        span = _span_start(session, idx)
        stop, idx = _apply_fresh_jobs(page, run, fresh, idx)
        _span_end(session, "search", q, span, idx)  # #61 验收 2：每个关键词轮各落一条跨度
    session.state["last_idx"] = idx  # #55：补投轮的 idx 从这里续号（request_N 不与主轮撞号）


def _recommend_turn_open(run):
    """推荐流轮的开轮条件（#33，#61 方案 A 收口）：开关为 on + 仍有剩余额度。
    开关默认 on（票面 v2：搜索与推荐并用，非二选一）。

    原来还有第三条 `not stop`，随推荐流前置一并删除——`_execute_query_rounds` 是本函数唯一
    调用方，前置后调用点的 `stop` 恒为 False，留着就是一条永不成立的死条件，还会让读代码的
    人以为「停批会吃掉整个推荐轮」（票面 #61 报的正是这个副作用）。

    删掉它**不放松「静默时段不许投递」**这条设计意图，两阶段各自已有落点（均已核对源码）：
    · 起跑前：`_init_live_session` 在启浏览器之前就查 `is_quiet_hour(current_hour(), quiet)`，
      命中即 notify 并返回 None，调用方 `run_live` 直接以 0 退出——整场会话根本不启动，
      所以排在前面的推荐轮不可能落在静默时段里；
    · 会话中途跨过静默边界：停批由 `_pre_job_stop` 的同一判据逐岗拦（返回 True 即停）。
      推荐轮的逐岗投递用的是这条链——`_recommend_round` → `_apply_fresh_jobs`，而
      `_apply_fresh_jobs` 在每岗之前先调 `_pre_job_stop`，命中即 `return True, idx`。
    即：停批语义由逐岗预检承担，不靠「整段跳过推荐流」这种副作用。"""
    session = run.session
    return bool(session.cfg.get("live_recommend")) and session.state["applied"] < run.limit


def _retry_session_jobs(page, run):
    """本会话补投轮（#55 部分二）：搜索/推荐队列耗尽后、收尾前，对本场超时岗位逐个
    **直跳**补投一次（enter_detail 无卡片自动走 #50 直跳语义），每岗位最多补投 1 次；
    补投期间超时由 retry_pass 标记转入跨会话台账（_record_agent_timeout 分流）。
    与搜索轮同链约束（验收 4）：_pre_job_stop（日限/风控熔断/静默/CLOSE）逐岗预检，
    停批即剩余岗位直接落台账兜底；excluded_companies 命中不补投不进台账；
    dry-run 会话不补投不写台账。"""
    session = run.session
    retries = session.state.pop("session_retries", [])
    if not retries:
        return
    if session.state.get("dry_run"):
        print(f"[补投] dry-run 会话不补投（{len(retries)} 个超时岗位仅记账，不重试不写台账）")
        return
    print(f"\n[补投] 队列耗尽——本场超时岗位 {len(retries)} 个，直跳补投（每岗限 1 次，#55）")
    session.state["retry_pass"] = True
    idx = session.state.get("last_idx", 0)
    span = _span_start(session, idx)  # #61 验收 2：本会话补投轮算 retry 来源
    for pos, entry in enumerate(retries):
        job = entry["job"]
        if _pre_job_stop(session, run):
            print(f"[补投] 停批信号——剩余 {len(retries) - pos} 个不重试，直接落跨会话台账兜底")
            session.state["retry_touched"] = True
            for rest in retries[pos:]:
                session.state["retry_new_count"] = session.state.get("retry_new_count", 0) + 1
                session.state.setdefault("retry_pending", []).append(
                    _retry_snapshot(
                        rest["job"], attempts=0, jd_text=_request_jd_text(session.session_dir, rest["request_idx"])
                    )
                )
            break
        if is_excluded_company(job.get("brandName"), run.excluded):
            print(f"    [补投] 品牌已进排除清单，不再补投: {job.get('brandName')}")
            continue
        # #56 B1③ 消费点 2：超时岗若已在楔死台账里就不再直跳——「直跳补投」正是那场白交
        # 240s 的动作。停批分支把剩余岗位无差别落台账，楔死岗会在下一场的消费点 3 被
        # 有播报地移出，故此处不另加判定（同一件事只长在一处）。
        wedge = _wedge_entry_for(session, job.get("encryptJobId"))
        if wedge is not None:
            _handle_wedge_skip(session, job, wedge, "session_retry")
            continue
        idx += 1
        print(f"    [补投] 直跳重试（第 {pos + 1}/{len(retries)} 个）: {job.get('jobName')}")
        _apply_one_job(page, job, idx, session)
    session.state["retry_pass"] = False
    session.state["last_idx"] = idx
    _span_end(session, "retry", None, span, idx)  # #61 验收 2：与跨会话补投同一来源


def _retry_pending_jobs(page, run):
    """跨会话补投轮（#55 部分三）：先于新岗位搜索消费 retry_pending.json 台账。
    逐条处置：excluded 品牌 → 移出；楔死台账命中（#56 B1）→ 有播报地移出；完整 apply 链补投后
    按 apply_retry_outcome 分流（投出/跳过/已沟通 → 移出；no_jd 按 #56 A5 归因类分流——站点抖动型
    保留、双空型移出；再超时等 → attempts+1 保留，封顶移出并告警）。
    停批信号（返回 True）让调用方跳过新搜索——剩余条目原样保留（未获补投机会，
    attempts 不动）。台账本轮处置完即时落盘（会话中途崩溃也不重复投已处置岗位）。
    dry-run 会话不消费不写（验收 4）。"""
    session = run.session
    if session.state.get("dry_run"):
        return False
    path = Path(session.cfg["retry_pending_path"])
    entries = _load_retry_pending(path)
    if not entries:
        return False
    print(f"\n[补投] 跨会话台账 {len(entries)} 个历史超时岗位，先于搜索逐个补投（#55）")
    session.state["retry_pass"] = True
    idx = session.state.get("last_idx", 0)
    # 跨度起点与 kept/stopped 同一行取（#61 验收 2）：起点必须在逐岗循环之前取，而本函数
    # 的语句数贴着 ruff 的 max-statements=50 门槛，单独占一行会顶破它。
    # 台账轮的 idx 区间可与搜索流重叠（见 _result_source），靠 res 切片区分来源。
    kept, stopped, span = [], False, _span_start(session, idx)
    for pos, entry in enumerate(entries):
        if is_excluded_company(entry.get("brandName"), run.excluded):
            print(f"    [台账] 品牌已进排除清单，移出: {entry.get('brandName')}")
            continue
        # #56 B1③ 消费点 3：楔死台账命中 → 有证据、有播报地移出补投队列（一个从此不投的岗
        # 没理由留在队列里骚扰每一场会话）。不在此处进 `_apply_one_job`——那才是本票要省的
        # 那次导航；同时记一条 `wedge_skipped` 结果条目，让 report 里看得见这条移出。
        wedge = _wedge_entry_for(session, entry.get("encryptJobId"))
        if wedge is not None:
            print(f"    [台账] 移出（{_RETRY_REMOVE_DESC['wedge_skipped']}）: {entry.get('jobName')}")
            _handle_wedge_skip(session, entry, wedge, "pending")
            continue
        if _pre_job_stop(session, run):
            print(f"[补投] 停批信号——剩余 {len(entries) - pos} 个台账条目原样保留（下次会话再投）")
            kept.extend(entries[pos:])
            stopped = True
            break
        idx += 1
        print(f"    [补投] 台账重试（attempts={entry.get('attempts', 0)}）: {entry.get('jobName')}")
        rv = _apply_one_job(page, entry, idx, session)
        # results[-1] 是本岗唯一记账点（applied/skip/失败各路径均恰好追加一条）
        last = session.results[-1] if session.results else {}
        outcome = last.get("status")
        # #56 A5③：no_jd 带上归因类一起处置——站点抖动型留在台账（attempts+1 走封顶路径），
        # 只有「DOM 与 XHR 双空」才移出；类别读不到时 apply_retry_outcome 保守保留。
        keep, reason = apply_retry_outcome(entry, outcome, jd_class=last.get("jd_class"))
        if keep:
            kept.append(entry)
            if outcome == "no_jd":
                print(
                    f"    [台账] 保留（{_no_jd_keep_phrase(last.get('jd_class'))}，"
                    f"attempts={entry.get('attempts')}）: {entry.get('jobName')}"
                )
        elif reason == "attempts_cap":
            print(f"    [warn 台账] {entry.get('jobName')}: {_RETRY_REMOVE_DESC[reason]}，移出台账不再重试")
        else:
            print(f"    [台账] 移出（{_RETRY_REMOVE_DESC.get(reason, reason)}）: {entry.get('jobName')}")
        if rv in ("stop", "relogin"):
            print(f"[补投] {'停批' if rv == 'stop' else '登录态过期'}——剩余台账条目原样保留")
            kept.extend(entries[pos + 1 :])
            stopped = True
            break
    session.state["retry_pass"] = False
    session.state["last_idx"] = idx
    _span_end(session, "retry", None, span, idx)  # #61 验收 2
    session.state["retry_reprocessed"] = len(entries)
    session.state["retry_touched"] = True
    session.state["retry_pending"] = kept
    _save_retry_pending(path, kept)
    print(f"[补投] 台账处置完成：移出 {len(entries) - len(kept)}，保留 {len(kept)}")
    return stopped


# #47：结果三桶口径——applied=真投出；skipped=没投但链路按设计工作（主动跳过 agent_skip/no_jd、
# 规矩拦停 excluded_company/gate_blocked、限速 paused/skip、演练截停 dry_run_ok、#56 B1 的
# 止损跳岗 wedge_skipped）；failed=异常与动作失败（risk_blocked/no_send/agent_timeout/
# login_expired/no_btn/no_input/page_error/preflight_click_failed）。
# 映射显式列举，未收录的新状态保守归 failed（宁可显眼不可静默）。
# 背景：2026-09-24 用户把 agent_skip 小计读成「投递失败」——跳过与失败必须在展示层可分。
_OUTCOME_FAILED_STATUSES = {
    "risk_blocked",
    "no_send",
    "agent_timeout",
    "login_expired",
    "no_btn",
    "no_input",
    "page_error",
    "preflight_click_failed",
}
_OUTCOME_SKIPPED_STATUSES = {
    "agent_skip",
    "no_jd",
    "skipped",
    "excluded_company",
    "gate_blocked",
    "paused",
    "skip",
    "dry_run_ok",
    # #56 B1：楔死台账按设计不再投＝跳过（止损），不是失败——归进 failed 会把「链路坏了」
    # 与「这个岗被判定为会楔死」混成同一个读数（#47 分桶要义）。
    "wedge_skipped",
}


def outcome_summary(results):
    """逐岗 results → 三桶小计（#47）：applied / skipped / failed。"""
    summary = {"applied": 0, "skipped": 0, "failed": 0}
    for r in results:
        status = r.get("status")
        if status == "applied":
            summary["applied"] += 1
        elif status in _OUTCOME_SKIPPED_STATUSES:
            summary["skipped"] += 1
        else:
            summary["failed"] += 1  # 未收录状态保守归 failed
    return summary


def _span_covers(span, position, idx):
    """单条跨度是否覆盖某条 result（#61 验收 2 的判据，两条同时成立才算覆盖）。
    · 编号：result 的 idx 落在该轮发出的 [idx_from, idx_to]——条目必须带编号，
      excluded_company / wedge_skipped / preflight_click_failed 这几类没有编号，
      没有编号就没有可核对的东西，不猜；
    · 切片：result 在 results 列表里的位置落在该轮跑时的 [res_from, res_to)——
      条目确实是这一轮追加进来的。"""
    return (
        isinstance(idx, int)
        and span["idx_from"] <= idx <= span["idx_to"]
        and span["res_from"] <= position < span["res_to"]
    )


def _result_source(spans, position, idx):
    """一条 result 的来源（"search" | "recommend" | "retry" | None）——归不上就 None。

    为什么要「编号 + 切片」两条判据同时核对，而不只看 idx：跨会话补投轮
    （`_retry_pending_jobs`）从 state["last_idx"] 起号，而搜索流（`_execute_query_rounds`）
    的 idx 从 0 起（既有语义，本次不动，#55 的续号只覆盖补投轮之间）——于是台账轮与搜索轮
    的编号区间**都是 1..N、可以重叠**，只看 idx 会把台账里投出的那一封读成搜索流投出的。
    results 列表的切片在同一场会话里严格递增，只有它能把两者分开。
    命中多个来源时（正常会话到不了这一步，台账轮与搜索轮切片不重叠）也返回 None：
    分不清就不写。"""
    hits = {span["source"] for span in spans if _span_covers(span, position, idx)}
    return hits.pop() if len(hits) == 1 else None


def _attach_result_sources(results, spans):
    """按轮次跨度给每条 result 补 `source` 字段（#61 验收 2：报告要答得出「这几封来自
    搜索还是推荐」，A 方案改完也要能证明收益）。
    就地写进 results 的条目（而非新建一份副本）：`build_report` 返回的 `results` 与本函数
    写入的是同一批对象，收尾 `[done]` 行、report.json 与 session.results 因此只有一副面孔。
    **`source` 为 null 表示「无法归属」，不是「来自搜索」**（无编号、编号在跨度外、
    落在所有跨度之前/之后都会是 null）。"""
    for position, entry in enumerate(results):
        entry["source"] = _result_source(spans, position, entry.get("idx"))


def _applied_by_source(results):
    """投出去的那几封按来源计数（#61 验收 2 的聚合读数）：只统计 status == "applied" 的条目，
    计数直接读逐条 `source`——两边同源，所以永远对得上（用例钉的就是这条自洽）。
    `unknown` 恒在：归不上源的 applied 不静默吞掉（同 #47「未知状态保守归 failed」的口径，
    宁可显眼不可静默）；正常会话里它应为 0。"""
    counts = {"search": 0, "recommend": 0, "retry": 0, "unknown": 0}
    for r in results:
        if r.get("status") == "applied":
            counts[r["source"] if r.get("source") in counts else "unknown"] += 1
    return counts


# #62：no_send 的三种成因（状态串照旧只有一个 no_send，#47 的三桶分桶不受影响）。
_NO_SEND_REASONS = ("off_chat", "send_btn_missing", "send_click_failed")


def _no_send_reason_stats(results):
    """#62 机读面：`no_send` 按成因分桶，四键恒在（0 也写出来，照 #56 验收 4 `no_jd_classes`
    与 #65 `recommend_*` 的「新键恒在」口径）。这三桶回答的是「这批岗能不能自动补投」：

    - `off_chat`＝打字**前**的形态守卫拦下（#62）——一个字都没进页面，已进 `retry_pending`
      台账，下一场会被 `_retry_pending_jobs` 捞回；
    - `send_btn_missing` / `send_click_failed`＝字**已打进**沟通输入框之后才落空——
      按拍板口径**不自动补投**（可能已实际发出，自动重投有双发风险），这两个读数是把它们
      「摆在收尾看得见」的唯一手段；
    - `other`：缺 reason 的历史/桩条目或未知成因，恒在（同 `unknown` 的口径，不静默吞）。
    """
    stats = {k: 0 for k in _NO_SEND_REASONS}
    stats["other"] = 0
    for r in results:
        if r.get("status") != "no_send":
            continue
        key = r.get("reason")
        stats[key if key in stats else "other"] += 1
    return stats


def _greeting_ledger_stats(state):
    """#64 report 读数：本次文案入库 N / 覆盖 M / 异常跳过 K，三键恒在（0 也写出来，
    口径同 `_no_send_reason_stats`）。未接线（桩 state / 老报告读者）给零形态而不是缺键。"""
    stats = {k: 0 for k in GREETING_LEDGER_KEYS}
    stats.update(state.get("greeting_ledger") or {})
    return stats


def _greeting_migration_stats(state):
    """#64 追加验收 5/6 report 读数：历史条目打标普查 + 已投无文案缺口，四键恒在
    （写法与 `_greeting_ledger_stats` 同构，未接线给零形态而不是缺键）。"""
    stats = {k: 0 for k in GREETING_MIGRATION_KEYS}
    stats.update(state.get("greeting_migration") or {})
    return stats


def build_report(queries, state, results):
    """会话报告（report.json）：reason_stats 供 agent 分类反馈；keyword_skips（#35）
    记录导航重试后仍被跳过的关键词轮；recommend_listing / recommend_errors / 两个 scrolls
    字段（#33）记录两来源的可观测口径（拿到的条数与滚了几轮）；results 为逐岗状态明细，
    每条带 source（#61 验收 2，按 state["source_spans"] 的轮次跨度归属，null＝无法归属），
    applied_by_source 是同一批归属的 applied 小计。"""
    skip_stats = {}
    for r in results:
        if r.get("status") in ("agent_skip", "agent_timeout"):
            key = classify_reason(r.get("reason", "")) if r.get("reason") else "no_reason"
            skip_stats[key] = skip_stats.get(key, 0) + 1
    dom_shapes, code_nonzero_by_class = _no_jd_shape_stats(results)
    _attach_result_sources(results, state.get("source_spans", []))
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "queries": queries,
        "applied": state["applied"],
        "direct_fallback": state["direct_fallback"],
        "card_clicks": state.get("card_clicks", 0),
        "card_click_fails": state.get("card_click_fails", 0),
        "direct_no_card": state.get("direct_no_card", 0),
        "dry_run": state.get("dry_run", False),
        "keyword_skips": state.get("keyword_skips", []),
        # #33：listing 为 None = 推荐流轮没跑（开关 off / 额度已满；#61 方案 A 起「已停批」
        # 不再是理由），与 0（跑了但一条没拿到）区分——报告读者不必反推开关状态。
        "recommend_listing": state.get("recommend_listing"),
        "recommend_errors": state.get("recommend_errors", []),
        # #33 第七轮：采集深度口径——搜索轮滚动次数累计，推荐轮为 None 表示该轮没跑
        "search_scrolls": state.get("search_scrolls", 0),
        "recommend_scrolls": state.get("recommend_scrolls"),
        # #65：消费配额读数——candidates = 去重后进消费口的候选总数，consumed = 配额截断后实际消费的
        # 个数（差值就是「本场没轮到」的岗数）。两键恒在：None = 推荐轮没跑，与 recommend_listing
        # 的 null≠0 口径同源（见 docs/agents/apply-forensics.md）；未截断时两数相等。
        "recommend_candidates": state.get("recommend_candidates"),
        "recommend_consumed": state.get("recommend_consumed"),
        "reason_stats": skip_stats,
        # #56 验收 4：no_jd 的分流读数——三类恒在（0 也写出来），首轮真实投递后
        # 「往返失败/超时未回 占比高 = 站点往返为真」与「双空为主 = 岗位无内容」读报告即可分流。
        "no_jd_classes": _no_jd_class_stats(results),
        # #56 验收 4（修复轮 F1）：类别旁边必须再给两条能把两个假说分开的读数，否则
        # 「详情超时未回」里混着的「DOM 选择器全落空（形态漂移）」与「接口回非 0 码（站点报错）」
        # 都读不出来，验收 4 的分流判据就落不了地。
        "no_jd_dom_shapes": dom_shapes,  # 四类恒在：zero_hits 高＝详情页落空一侧（两假说并列，见 no_jd_empty_urls）
        # 非 0 返回码按类交叉（修复轮 4/B）：扁平总数读不出它属于哪一类、与哪个 no_jd 同容器
        "no_jd_code_nonzero_by_class": code_nonzero_by_class,
        # 落空时刻的 URL（修复轮 4/E）：zero_hits 两假说（形态漂移 / 人已离岗弹回列表）靠它
        # 与 C 段栈人工分辨——url 含 job_detail 说明落空时还在某详情页，只是形态或岗位变了
        "no_jd_empty_urls": _no_jd_empty_urls(results),
        # #47：跳过与失败分桶小计——reason_stats 只拆 agent_skip/no_jd 的细分原因，
        # 读者（agent 汇报给用户时）应按此口径播报，不得把 skipped 说成投递失败。
        "outcome_summary": outcome_summary(results),
        # #62：no_send 的成因分桶（四键恒在）——off_chat 那批已进 retry_pending 会被下轮捞回，
        # 另两桶是「字已打进输入框」的，按拍板口径不自动补投，只能靠本读数看见它们。
        "no_send_reasons": _no_send_reason_stats(results),
        # #61 验收 2：投出去的这几封来自哪个来源——与逐条 results[].source 同源的一次归属，
        # 两边加起来必然对得上；unknown 恒在（归不上源的 applied 不静默吞）。
        "applied_by_source": _applied_by_source(results),
        "results": results,
        # #49：None = 没采集（dry-run 跳过），False = 采集失败或本次判残缺未覆盖（沿用上次
        # 复盘数据；#59 加了残缺这条路径），True = 收尾已采集最新会话——报告读者不必反推
        # eval 数据新旧。
        "followup_fresh": state.get("followup_fresh"),
        # #54：浏览器退出分类——None = 会话内未发生 browser-closed 类异常（正常会话
        # 零行为变化）；发生过则 {classification, detected_at, evidence}，读报告者不必
        # 再猜「浏览器为什么没了」。
        "browser_exit": state.get("browser_exit"),
        # #55：断点台账最终快照（空列表 = 无在途超时岗位）；收尾 [done] 行的
        # 「历史超时补投 N 个 / 本场新增超时 M 个」是本字段的摘要口径。
        "retry_pending": state.get("retry_pending", []),
        # #56 B1：楔死跳岗台账读数——答两件事：台账总条数（total）、本场因台账跳过了几岗
        # （session_skipped + skipped_jobs 明细）；另带 backfilled（本场新入账）与 rejected
        # （被归因门槛筛掉的场数）——不然「台账是空的」读起来分不清是该跳的没死过还是取证读不出。
        "wedge_ledger": _wedge_ledger_stats(state),
        # #64：招呼语台账结局计数（三键恒在）——「写过草稿」与「真投出」靠 overwritten
        # 读数分辨，skipped 非零说明有文案没进库（写盘失败被台账层吞下，只能从这里看见）。
        "greeting_ledger": _greeting_ledger_stats(state),
        # #64 追加验收 5/6：历史条目打标普查与「投出了但台账没文案」缺口（四键恒在）——
        # 缺口只数不回填，将来要补正文必须另开票写明来源（票面验收 6）。
        "greeting_ledger_migration": _greeting_migration_stats(state),
    }


def _followup_report_count(report):
    """followup 报告的会话条数口径：conversations 列表长度优先，退化到 totalConversations。
    #59 起现场采集与「上一份基准」共用这一条口径——两侧不同源的话，条数对比就不对齐。"""
    convs = report.get("conversations")
    n = len(convs) if isinstance(convs, list) else 0
    return n or (report.get("totalConversations") or 0)


def _prev_followup_count(path):
    """上一份 followup 报告的会话条数（#59 残缺判定的基准）。

    只有两种情况归入「没有可比基准」（返回 0 ⇒ 本次照常落盘）：文件不存在，以及内容读得出
    却解不开（半写/坏 JSON，直接覆盖它才是恢复方向）。**其余异常（权限等）一律向上抛**——
    本函数无法判断基准时不许把「读不到」当成「没有基准」放行覆盖；调用方的外层 try 会把它
    记成 `followup_fresh=False` + warn，宁可不覆盖。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return 0
    return _followup_report_count(data) if isinstance(data, dict) else 0


def _collect_fresh_followup(run, page):
    """收尾现场采集 followup（#49，用户 2026-09-24 提出）：浏览器还开着（报告写盘前），
    顺手抓最新会话列表——收尾 eval 吃到本场投递后的真实回复状态。eval 全量重算
    （招呼语台账 + reply_annotations 历史标注 + followup 报告），「连同上一次的复盘内容
    一起 evaluate」由既有合并语义天然保证，无需额外拼接。
    护栏：任何失败只 warn + followup_fresh=False，不影响收尾报告与退出码（观测层语义，
    同 edd_wrapup）；dry-run 跳过（零副作用）；annotate（读回复原文分类）仍归 runbook
    步骤 5 的 agent——本采集只供数，不替代标注。#59：采到条数不足上一份的一半同样判残缺
    ——不覆盖、followup_fresh=False（0 条那条老防线保留原语义）。"""
    state = run.session.state
    if state.get("dry_run"):
        state["followup_fresh"] = None
        return
    _touch_heartbeat("followup")  # 收尾采集段起拍（段内平台调用自身有 goto 超时）
    try:
        applied = json.loads(Path(run.session.cfg["history_path"]).read_text(encoding="utf-8")).get("applied", [])
        # beat_fn 注入（#59 修复轮 R4-1）：本段的「聊天页 goto 上界 + 固定停顿」是会话内最长的
        # 合法静默之一，逐轮拍点把它压到 `_forensics.WEDGE_STACK_SECONDS` 之下——不注入的话
        # 健康但慢的聊天页会给健康会话落一份非空 wedge-stack.txt，推翻 runbook 的取证判读。
        report = run.session.platform.followup(page, applied, beat_fn=_heartbeat_beat)
        if report is None:
            state["followup_fresh"] = False
            print("[warn] 收尾 followup 采集无结果（沿用上次复盘数据做收尾 eval）")
            return
        n_conv = _followup_report_count(report)
        if not n_conv:
            # 登录过期/掉线等场景抓到 0 对话（192616 实测）：空报告会把历史数据静默抹成 0，
            # 并让 eval 吃 0 样本污染趋势。宁可沿用上次数据并明示降级，不静默覆盖。
            state["followup_fresh"] = False
            print("[warn] 收尾 followup 采集到 0 条对话（疑似登录态过期/掉线）——沿用上次报告，不覆盖")
            return
        out = g.resolve_paths(env=run.env)["followup"]
        prev = _prev_followup_count(out)
        if prev > 0 and n_conv * 2 < prev:
            # #59：护栏从「0 条才不覆盖」收紧为「与上一份条数对比」。首页截断的形态是**断崖**
            # （20260929-202514 实测 3/100，随后 followup_tracker 同一函数拿到 100 条），
            # 残缺但非零的报告过去照样覆盖全量，收尾 eval 于是算出 0 样本并追加进 eval_trend
            # ——终端打出「样本 0/30、vs baseline −32」这种不存在的退化。阈值取「一半」而不是
            # 草拟过的 max(0.5×prev, prev−10)：后者在 prev 大时等价于「几乎不许变少」（100→89
            # 即拒），会把会话自然过期造成的真实衰减也判成残缺，从此长期停在旧报告上（新回复
            # 永远进不了 eval），换一类失真；真实增减是 ±20% 级的渐进，一半这条线落在两者之间。
            state["followup_fresh"] = False
            print(
                f"[warn] 收尾 followup 采集到 {n_conv} 条对话，不足上一份报告（{prev} 条）的一半"
                "——判为残缺采集（首页截断形态），沿用上次报告不覆盖；收尾 eval 不吃这份残缺数据"
            )
            return
        # 原子写（#59 二轮）：上一版这里是 `out.write_text(json.dumps(...))`——被中断/并发写时
        # 留下的半写报告正是「读不出基准」的自造来源，而 `_load_wedge_skipped` 那类台账读法
        # 已有自愈回落，这里不该再靠自己制造一次损坏。
        write_json_atomic(out, report)
        state["followup_fresh"] = True
        replied = sum(1 for c in report.get("conversations", []) if c.get("status") == "hr_replied")
        print(
            f"[followup] 现场采集 {n_conv} 条对话"
            f"（HR 回复 {replied}）——收尾 eval 用最新数据；回复原文标注仍在复盘环节（runbook 步骤 5）"
        )
    except Exception as e:
        state["followup_fresh"] = False
        note_browser_exit(state, str(e))  # #54：浏览器死在收尾采集前同样分类
        print(f"[warn] 收尾 followup 采集失败（不影响报告与退出码）: {str(e)[:100]}")


def edd_wrapup(paths, profile, now=None):
    """投递会话收尾的 EDD 轻量快照（#37 拍板 Q1 会话级）：追加趋势快照 + 输出趋势块 +
    批次级审阅提示。纯本地 <0.5s；**异常吞掉打 warn 返回 None**——EDD 是观测层，
    故障绝不阻断投递收尾与退出码（投递结果落盘优先）。"""
    try:
        outcome = g.record_session_snapshot(paths, profile=profile, now=now)
        if outcome.get("trend_appended"):
            print(_edd.render_trend_md(outcome["snapshot"], _edd.load_trend(paths["trend"])))
        else:
            # 无 followup / 空报告（登录过期 192616 实测）→ 快照未追加（防线在
            # record_session_snapshot 源头：空 conversations 不追加），仅不渲染趋势块；这两类在
            # eval **之前**就退出，返回体不带 review_due / density_due（下面两节靠 `or {}` 兜住）。
            # #59 二轮加了第三类：0 样本而历史已有非 0 点的「疑似残缺采集」——旧文案一律说成
            # 「失败会话」对这一类不成立（会话本身跑完了，是采集读数残缺），措辞改成中性说法，
            # 具体原因由 reason 自带；R4-2 之后这一类**照带** review_due / density_due 两键。
            # `or '无新快照'` 是缺 reason 形态（外部/降级 outcome）的兜底：观测层不因为少一个键
            # 就抛出去（外层 try/except 会把它记成「EDD 收尾快照失败」，把一场好会话读成故障）。
            print(f"[EDD] 跳过趋势快照（{outcome.get('reason') or '无新快照'}）——这一场不往趋势里追加读数")
        verdict = outcome.get("review_due") or {}
        if verdict.get("due"):
            print("💡 [EDD 批次审阅] " + "；".join(verdict["reasons"]))
            print(
                "   人工审阅：python scripts/gen_greetings.py eval --trend；"
                "候选确认 promote-winners --group <key> --yes（候选队列见 pending_promotions.json）"
            )
        # #45 密度阈值提示制（拍板 2026-09-24）：任一分组组内回复 ≥5 → 系统通知 + 提示块
        # 附候选清单，主动叫人审范式；低密度静默不扰。决策权在用户，不自动晋级；
        # 观测层故障由外层 try/except 兜底（通知失败不阻断投递收尾）。
        density = outcome.get("density_due") or {}
        if density.get("due"):
            print(_edd.render_density_prompt(density, _edd.load_pending_candidates(paths.get("pending"))))
            notify("招呼语范式审阅提醒", f"分组回复密度达标（{'；'.join(density['groups'])}）——跑 eval 看候选清单")
        return outcome
    except Exception as e:
        print(f"[warn] EDD 收尾快照失败（不影响投递结果）: {str(e)[:100]}")
        return None


def _maybe_report_issues(run, report, drafts):
    """#51 收尾建票：默认 HITL——只打印 💡 提示与确认命令（执行
    `python3 scripts/_issue_reporter.py --report <report.json>` 才是提交动作）；
    AUTOJOB_AUTO_ISSUE=1 显式开启时自动建票。远端建票是 consequential external
    action（AGENTS.md 硬门禁），默认绝不发起。dry-run 会话跳过（#49 同口径）；
    任何异常只 warn，不阻断收尾。"""
    if not drafts:
        return
    if run.session.cfg.get("dry_run"):
        print("[开票] dry-run 会话不建票（草稿已随 report.json 落盘供检查）")
        return
    try:
        _issue_reporter.wrapup_report(
            drafts,
            session_id=run.ts,
            reported_path=run.reported_path,
            report_path=report,
            env=run.env,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 熔断开票环节异常（不影响收尾）: {str(e)[:100]}")


def _save_retry_pending_state(run):
    """#55 部分三：会话收尾把台账工作副本落盘。dry-run 跳过（不写台账，验收 4）；
    本会话从未触及台账（retry_touched=False）时不写盘——正常会话零变化，不凭空造文件。"""
    session = run.session
    if session.state.get("dry_run") or not session.state.get("retry_touched"):
        return
    _save_retry_pending(Path(session.cfg["retry_pending_path"]), session.state.get("retry_pending", []))


def _write_session_report(run):
    """报告段（#39）：report.json 落盘（reason_stats 供 agent 做分类反馈）+ EDD 会话级
    轻量快照（#37 拍板 Q1，异常已在 edd_wrapup 内吞掉）+ 熔断事件建票草稿与提示（#51）
    + 断点台账落盘（#55）+ 收尾统计行。"""
    session = run.session
    _save_retry_pending_state(run)  # #55：补投轮新入台账的岗位在此落盘（dry-run/未触及跳过）
    report_data = build_report(run.queries, session.state, session.results)
    gpaths = g.resolve_paths(env=run.env)
    edd_outcome = edd_wrapup(gpaths, profile=gpaths["name"] or "default")
    if edd_outcome is not None:
        report_data["edd"] = edd_outcome
    breakers = session.state.get("breakers") or []
    drafts = _issue_reporter.drafts_for(breakers, run.ts) if breakers else []
    if drafts:
        report_data["issue_drafts"] = drafts
    report = session.session_dir / "report.json"
    write_json_atomic(report, report_data)
    _maybe_report_issues(run, report, drafts)
    summary = outcome_summary(session.results)  # #47：跳过与失败分桶播报，不混称「失败」
    skipped = [s["query"] for s in session.state.get("keyword_skips", [])]
    skip_desc = f"；跳过关键词 {len(skipped)} 个: {', '.join(skipped)}" if skipped else ""
    # #54：browser_exit 存在（发生过浏览器关闭类异常）时收尾行直接带分类描述
    bx = session.state.get("browser_exit")
    bx_desc = f"；浏览器退出: {BROWSER_EXIT_DESC.get(bx['classification'], bx['classification'])}" if bx else ""
    # #55：补投统计——历史超时补投 N 个（跨会话台账本轮处置数）/ 本场新增超时 M 个（落入台账）
    n_reprocessed = session.state.get("retry_reprocessed", 0)
    n_new = session.state.get("retry_new_count", 0)
    retry_desc = f"；历史超时补投 {n_reprocessed} 个 / 本场新增超时 {n_new} 个" if (n_reprocessed or n_new) else ""
    # #56 验收 4：no_jd 归因分布随收尾行念出来（口径同 #47 的分桶播报——只读数、不发明
    # 新展示层）；读数本体在 report.json 的 `no_jd_classes`，此处同源取数不重复计算。
    jd_stats = report_data["no_jd_classes"]
    jd_total = sum(jd_stats.values())
    jd_desc = (
        "；no_jd 归因 " + " / ".join(f"{_no_jd_class_label(c)}×{n}" for c, n in jd_stats.items() if n)
        if jd_total
        else ""
    )
    # #56 验收 4（修复轮 F1，修复轮 4/B 把非 0 读数改成按类交叉）：分流读数跟着收尾行进终端
    # ——「往返失败/挂起」还是「形态漂移」要当场读得出，不该等人事后翻 report.json。
    # 0 的形态不进这一行（报告里恒在，零噪音）；非 0 条数按归因类念，扁平总数读不出它属于哪一类。
    if jd_total:
        shapes = report_data["no_jd_dom_shapes"]
        parts = [f"{_dom_shape_label(s)}×{n}" for s, n in shapes.items() if n]
        nz_by_class = report_data["no_jd_code_nonzero_by_class"]
        nz_parts = [f"{_no_jd_class_label(c)}×{n}" for c, n in nz_by_class.items() if n]
        if nz_parts:
            parts.append("非 0 返回码（" + " / ".join(nz_parts) + "）")
        if parts:
            jd_desc += "；no_jd 分流 " + " / ".join(parts)
    # #56 B1：本场被台账跳过的岗数带一句（口径同 #47 的分桶播报，只读数不发明新展示层）；
    # 0 时整段不出现——正常会话零噪音，别把「一次都没撞上楔死岗」也刷成一行。
    wl = report_data["wedge_ledger"]
    wedge_desc = (
        f"；楔死跳岗 {wl['session_skipped']} 岗（台账共 {wl.get('total', 0)} 条，"
        f"{WEDGE_LEDGER_ENV}={'开' if wl.get('enabled') else '关'}）"
        if wl.get("session_skipped")
        else ""
    )
    # #64：招呼语台账计数带一句（读数同 report 的 greeting_ledger，同源不重复计算）；
    # 三个计数全为 0 时整段不出现——正常会话零噪音。
    gl = report_data["greeting_ledger"]
    greet_bits = [
        f"{label} {gl[key]}"
        for key, label in (("added", "入库"), ("overwritten", "覆盖"), ("skipped", "跳过"))
        if gl.get(key)
    ]
    greet_desc = "；招呼语台账 " + " / ".join(greet_bits) if greet_bits else ""
    print(
        f"\n[done] 投递 {session.state['applied']} 个（直跳回退 {session.state['direct_fallback']} 次），"
        f"未投 {summary['skipped']}（跳过/拦停），失败 {summary['failed']}，"
        f"报告 {report}" + skip_desc + bx_desc + retry_desc + jd_desc + wedge_desc + greet_desc
    )


def _hold_browser_until_closed(session):
    """结束不关窗（#39）：通知后轮询 CLOSE 哨兵（Ctrl+C 亦可），退出后 Playwright 才落盘视频。"""
    notify("Live 投递完成", f"投递 {session.state['applied']} 个；写 CLOSE 文件结束并保存视频")
    try:
        wait_close_sentinel(session.session_dir / CLOSE_SENTINEL)
    except KeyboardInterrupt:
        print("\n[保持] Ctrl+C，直接退出")


def _finalize_recording(run):
    """收尾段（#39）：视频落盘清点 + 自动剪辑（按 shots/ 动作时刻剪掉长停留，用户约定
    2026-09-10；ffmpeg 缺失则跳过）+ 截图路径提示。"""
    if run.record:
        vid_dir = run.video_base / run.ts
        vids = sorted(vid_dir.rglob("*.webm"))
        print(f"[录像] 视频已落盘 {len(vids)} 个 → {vid_dir}")
        try:
            subprocess.run(
                [sys.executable, str(ROOT / "scripts/trim_live_video.py"), str(run.session.session_dir)],
                check=True,
                cwd=ROOT,
            )
        except Exception as e:
            print(f"[录像] 自动剪辑跳过: {str(e)[:80]}")
    print(f"[录像] 动作截图 → {run.session.shots_dir}")


def _enable_line_buffering():
    """把 stdout 切成行缓冲——投递会话的推进靠轮询 `request_*.json`/`reply_*.json`，
    而读的一方看的是重定向后的日志。Python 在非 tty（重定向）下默认**块缓冲**，09-29 实测
    起会话后 25s 内日志 0 字节、连「[启动] 随机延迟」都要等缓冲区满才吐，agent 只能盲等
    或重启一次（本场确实为此重启过）。在入口自己开 `line_buffering`，`-u`/`PYTHONUNBUFFERED`
    就不再是人工前置——手动 export 过的东西最终要落回代码默认值。
    被 capsys 换掉的流、或任何不支持 reconfigure 的流：保持默认即可，不影响功能。"""
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:  # noqa: BLE001  # 观测性开关绝不成为启动失败的原因
        return False
    return True


def run_live(env=None):
    _enable_line_buffering()  # 必须在第一条 print 之前：重定向下块缓冲会吃掉实时队列信号
    run = _init_live_session(env)
    if run is None:
        return 0  # 静默时段：预检已通知，不启浏览器
    sweep_expired_data(
        run.video_base,
        run.session.session_dir.parent,
        _parse_retention_days((env or os.environ).get(RETENTION_ENV)),
        datetime.now(),
    )
    with Camoufox(
        humanize=True,
        geoip=True,
        os="macos",
        headless=False,
        persistent_context=True,
        user_data_dir=run.browser_dir,
        **run.video_kwargs,
    ) as context:
        page = context.new_page()
        _record_browser_pids(run.session)  # #54：启动期采集 Camoufox pid，供退出分类探活
        _arm_session_watchdog(run.session)  # 中途看门狗：从补投轮起全程护住（楔死整树止损）
        if not _retry_pending_jobs(page, run):  # #55 部分三：先于新岗位搜索补投历史超时；停批则跳过新搜索
            _execute_query_rounds(page, run)
            _retry_session_jobs(page, run)  # #55 部分二：队列耗尽后、收尾前补投本场超时岗位
        _collect_fresh_followup(run, page)  # #49：报告写盘前现场采集，收尾 eval 用最新数据
        _write_session_report(run)
        _mark_session_done(run.session)  # done 哨兵：看门狗退场，CLOSE 等待按用户节奏
        _hold_browser_until_closed(run.session)
    _finalize_recording(run)
    return run.session.state["applied"]


if __name__ == "__main__":
    run_live()
