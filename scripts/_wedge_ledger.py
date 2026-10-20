#!/usr/bin/env python3
"""
楔死岗跳岗台账（#56 B1）：击杀取证回灌 → 台账原子读写 → 归因合并 → 开跑呈报 → report 读数。

#57 第一轮从 `live_apply.py` **原样搬入**（纯搬迁，行为不变）：函数体、签名默认值、docstring
与段落注释逐字平移，不改写、不精简、不重排、不改名。既有调用点与测试的 monkeypatch 仍打在
`live_apply` 命名空间上——由 `live_apply.py` 顶部的 `from _wedge_ledger import ... as ...`
别名供给，本模块不是新的公开 seam。

**别名接线的 no-op 面（#59 修复轮 R4-8，口径同 `_forensics` 顶部那段）**：别名只是把同一个函数
对象再绑一个名字，**改绑 `live_apply` 上的别名，对本模块内部的互相调用是静默 no-op**——
`_backfill_wedge_ledger` 调 `_load_wedge_skipped` / `_merge_wedge_ledger` / `_save_wedge_skipped`
走的是它自己模块的 globals，不看 `live_apply` 那份。所以：
- 要 stub 掉「回灌读台账」这类**内部环节**，用例必须打在**归属模块**上（`_wedge_ledger._load_wedge_skipped`）；
  打在 `live_apply` 别名上只会静默失效、测试照常绿；
- 只有「`live_apply` 直接调那一层」的用例才可以继续打别名（既有测试因此一行没改）。
本模块的跨进程输入契约（击杀取证文件名 / 目录名形状 / marker 字段名）来自 `_forensics`，
名字同样只有一份真源。

职责边界：
- 入参一律 `Path` 与普通 dict/list；**不 import `live_apply`**（避免循环 import）。
- 台账路径由调用方经 `private_path(WEDGE_LEDGER_FILE)` 解析后传入（#28 路径知识单一出口），
  本模块不读全局 env 定路径。
- 耦合 session / pacer / results / 浏览噪声排除的消费侧接线（`_wedge_ledger_map` /
  `_prepare_wedge_ledger` / `_wedge_entry_for` / `_handle_wedge_skip` / `_WEDGE_STAGES` /
  `_surface_prior_wedge`）留在 `live_apply.py`——它们不属于台账本体。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _atomic_io import write_json_atomic  # noqa: E402
from _forensics import (  # noqa: E402  #57 第二轮第 5 点：击杀取证的文件/目录形状归位到取证模块
    MARKER_IN_JOB,
    MARKER_LAST_BRAND,
    MARKER_LAST_EID,
    MARKER_LAST_JOB,
    MARKER_LAST_LABEL,
    WATCHDOG_KILLED,
    _TS_DIR_RE,
)
from _limits import BOOL_ENV_ON_WORDS, BOOL_ENV_TWO_OFF, env_bool_switch  # noqa: E402  #57 第二轮：布尔 env 取值表单一真源
import _logging  # noqa: E402  # #38 机读日志旁路（回灌事件入账即落 JSONL）

# 回灌扫描的两项输入契约（#56 A 段取证目录形状，定义在 `_forensics.py`，本模块只读不改）：
#   · `WATCHDOG_KILLED` —— 看门狗在会话目录内落的击杀取证文件名（进程外写入）；
#   · `_TS_DIR_RE` —— 会话目录名形状（照 `sweep_expired_data` 的约束：非会话形态的目录不碰）。
# 归因要读的字段名同样来自 `_forensics.MARKER_IDENTITY_KEYS`（`MARKER_*` 那五个名字）——
# 心跳十键 ↔ 看门狗写 marker ↔ 本模块读 marker 三处引用同一份映射，漂移的代价是**静默少字段**
# （读不到 `in_job` / `last_eid` 就整场不入账），见该模块顶部。
# ⚠️ 本模块的入账判据**只吃身份五键**；#56 取证 A 镜像过来的另外三键（往返计数）落在
# `_forensics.MARKER_HB_MIRROR`，与上面那份映射**刻意分成两张表**——并成一张就等于把计数喂进
# 跳岗判据，那是行为变更不是收口。

# --- #56 B1：楔死岗跳岗台账（private/data/[<profile>.]wedge_skipped.json，默认开）---
#
# 写入源为什么是「下一场回灌」而不是「被杀的那一场自己写」：楔死的进程是被**进程外**的
# 心跳看门狗 SIGKILL 掉的，而主线程楔在无超时的协议往返上时「进程内不可能再执行任何
# Python 代码」（票面机制口径 §2）——它连自己的 finally 都跑不到，台账更写不了。
# 唯一还带着「这场死在哪个岗」的地方，是看门狗在进程外落的 WATCHDOG_KILLED.json
# （A2 起带 last_eid / last_job / last_brand / in_job）。所以入账只能发生在**下一场开跑时**：
# 扫历史击杀取证 → 归因 → 并进台账 → 呈报 → 本场起跳过。
#
# 票面事实：楔死的岗从未进 `applied_history`，也没有任何台账记得它，下场照样从它开始——
# 同一个「企贤 solidity开发 50-80K」连续 4 场 no_jd、又在 5 场会话里分别以第 11/11/3/1/2
# 岗位点楔死；默认行为等于每场白交 240s + 一次浏览器重启。
# 代价如实登记：该岗从此不投 = **漏投一个潜在达标岗**（用户 2026-09-28 拍板：默认开），
# 所以呈报里代价与放行办法（LIVE_WEDGE_SKIP=off）必须跟岗位一起念，不粉饰也不隐瞒。
# 消费点形状的依据：`excluded_companies`（`_apply_fresh_jobs` 里的 `is_excluded_company`
# 分支，票面点名的同款消费点）。

WEDGE_LEDGER_ENV = "LIVE_WEDGE_SKIP"  # B1④ 开关：默认开，关只认 0/off（方向论证见 `_wedge_skip_enabled`）
# 取值表的两行字面量收进 `_limits.py`（#57 第二轮：布尔 env 取值表单一真源，对照表也在那个小节）；
# 这里保留原有名字，绑的仍是同一对元组对象——调用点、`live_apply` 的别名与本模块的解析都读它们。
WEDGE_LEDGER_OFF_VALUES = BOOL_ENV_TWO_OFF
WEDGE_LEDGER_ON_VALUES = BOOL_ENV_ON_WORDS
# 台账相对路径：只在这里出现一次，交给 `private_path` 做 profile 化（#28 路径知识单一出口，
# 不许任何地方写死 private/data/wedge_skipped.json 字面量）
WEDGE_LEDGER_FILE = "data/wedge_skipped.json"


def _wedge_skip_enabled(env=None):
    """LIVE_WEDGE_SKIP：楔死岗跳岗台账的开关（#56 B1④），**默认开**（用户 2026-09-28 拍板）。
    关只认显式的 "0" / "off"（`strip()` + `lower()`），其余一律按开——含空串与 "ofs"/"false"/"no"。
    取值表与 A4 的 `_wedge_dump_enabled` **是同一张**（strip+lower + 两值）；与仓内其它布尔 env
    各不相同——**几张表的逐行对照只有一份真源**，在 `_limits.py` 的「布尔 env 开关」小节
    （#59 修复轮 R4-4：这里以前抄了一份对照表，两处各维护就会漂），按**行为**钉着这些表的用例是
    `scripts/test_live_apply.py::test_three_bool_env_switches_have_three_distinct_value_tables`。

    为什么非法值按「开」而不是按「关」——两个方向的代价不对称，且不对称的方向与 A4 相反：
      · **想关却被读成开**：代价是「某个岗被永久跳过 = 可能漏投一个潜在达标岗」；
      · **想开却被读成关**：代价是回到本票要治的现状——同一个岗再消耗一场会话（≈240s +
        一次浏览器重启），而且关掉时**不回灌**，连着几场都写错值会让台账一直是空的，
        直到某场写对值才补得回来（取证目录不会被 `sweep_expired_data` 删，它只删 shots/）。
    两侧都不可逆程度低，但「开」这一侧的代价**当场可见、当场可纠**：无论开关状态如何，
    `_surface_wedge_ledger` 都把台账岗位 + 本场生效的模式 + 放行办法念出来，读到不认识的
    原始值还额外打一行「按默认『开』处理，关闭只认 0/off」——写错值因此在开跑第一行就露出来，
    而默认值跟着用户拍板走，不跟着拼写错误走。反过来若非法值按关，一场「以为在止损、其实
    什么都没停」的会话是**静默**的，正好撞上 #54「呈报事实」的反面。"""
    # #57 第二轮：解析走 `_limits.env_bool_switch`，参数逐项就是上面这套口径（关只认
    # `WEDGE_LEDGER_OFF_VALUES`、可识别的「开」是 `WEDGE_LEDGER_ON_VALUES`、strip + lower、
    # 非法值打一行提示；「默认开」由 `env_bool_switch` 固定实现，见其 R4-4 注）；
    # 语义、文案与返回值一字未动。
    return env_bool_switch(
        env,
        WEDGE_LEDGER_ENV,
        off_values=WEDGE_LEDGER_OFF_VALUES,
        on_values=WEDGE_LEDGER_ON_VALUES,
        strip=True,
        lower=True,
        warn_on_unknown=True,
    )


def _new_wedge_stats():
    """回灌统计的零形态（`_backfill_wedge_ledger` 的起点，也是「本场没回灌」时的读数形态）。
    键集合固定：report.json 的 `wedge_ledger` 与 [done] 行都读它，缺键会让读报告的人分辨不出
    「没跑回灌」与「跑了但全是 0」。"""
    return {
        "scanned": 0,  # 扫到几份击杀取证
        "booked": 0,  # 本场新入账的取证数（= added + merged）
        "added": 0,  # 新建条目数（第一次见这个岗）
        "merged": 0,  # 并进既有条目的场次数（同一岗又楔了一场）
        "duplicates": 0,  # 已入账过、按幂等跳过的场次数
        "rejected": 0,  # 归因条件筛掉的场次数（岗位外被杀 / 旧格式 / 缺 eid）
        # #58 T2 守卫的独立读数：与 `rejected` **刻意分开**——「取证形态不合格」是取证层的事，
        # 「岗其实已经投出」是归因链条出了问题（T1 之后理论上不该再有，出现即要核查）。
        "already_applied": 0,
        "unreadable": 0,  # 读不出来的取证（坏 JSON / 非字典）
        "written": False,  # 台账是否真的被写过
    }


def _load_wedge_skipped(path):
    """读跳岗台账 → list；缺失 [] ；坏形态回落空列表 + warn（口径照 `_load_retry_pending`：
    一台机器的台账坏了不该让开跑崩掉，但那件事必须念出来，不自愈成「静默当没有」）。"""
    try:
        p = Path(path)
        if not p.exists():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("not a list")
        return [e for e in data if isinstance(e, dict)]
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 楔死台账读取失败，按空台账起步（{str(e)[:80]}）: {path}")
        return []


def _save_wedge_skipped(path, entries):
    """原子写台账（`.tmp` + os.replace）：写一半被打断不损坏已有台账——这本台账的读者
    是下一场会话，半份台账等于给下一场喂坏数据。"""
    write_json_atomic(Path(path), entries)


def _wedge_entry_from_marker(session_name, data):
    """B1① 归因门槛（严格，宁窄勿宽）：`in_job is True` **且** `last_eid` 非空才配入账。
    为什么不放宽：错归因的代价是**把一个正常岗位永久跳掉**（漏投一个潜在达标岗），
    比「少跳一个岗」重得多——两岗之间被杀时把罪记给上一岗，那个岗可能完全无辜。
    真值判断用 `is True`：`in_job` 是布尔字段，读不出布尔（None/字符串/缺键）就是读不出，
    不做「看起来像 True」的猜测。不入账**不影响呈报**——`_surface_prior_wedge` 照旧念那一场
    （A2 的呈报面不因本切片缩水）。"""
    if data.get(MARKER_IN_JOB) is not True:
        return None
    eid = str(data.get(MARKER_LAST_EID) or "").strip()
    if not eid:
        return None
    return {
        "encryptJobId": eid,
        "jobName": data.get(MARKER_LAST_JOB),
        "brandName": data.get(MARKER_LAST_BRAND),
        "session": session_name,  # 来源会话目录名 = 幂等键
        "wedged_at": data.get("killed_at"),
        "last_label": data.get(MARKER_LAST_LABEL),  # 死在哪个节拍上（C 段判栈要用）
        "stale_seconds": data.get("stale_seconds"),
    }


def _split_already_applied_wedge_items(items, applied_ids):
    """#58 T2 正向守卫（纯函数）：把归因合格的取证条目按「eid 是否已在 `applied_history`」分堆，
    返回 `(可入账, 已投出而被拦下)`。

    为什么要这道守卫（票面后果 3，本票最重的一条）：击杀点在 `_record_apply_outcome` 调
    `pacer.wait_sent()` 时，而 `_apply_job` 的 `finally`（`_heartbeat_job_end` 清岗位上下文）
    排在它**之后** ⇒ 被杀瞬间心跳仍是 `in_job=true` + eid=刚**成功投出**的那个岗。B1 的归因门槛
    只看「取证形态合不合格」，看不出「这个岗其实已经投出去了」，于是把一个达标成功岗当成
    嫌疑人永久拉黑，且全程无声——B1 的立意是「学费只交一次」，不是「把已成功的人销档」。

    为什么落在纯函数上（照 `_wedge_entry_from_marker` / `_merge_wedge_ledger` 的形状）：
    守卫的正确性只取决于两个集合的关系，不该被磁盘与时序牵进来，单测因此能直接钉住它。
    eid 两侧都归一成去空白字符串（#56 修复轮 F3 同口径：非字符串条目不参与匹配）。
    与 `_wedge_entry_from_marker` 的分工：这里只回答「岗已投出」，形态不合格的归 `rejected`
    ——两件事的处置动作不同（前者要核查归因，后者要修取证），读数不许混。"""
    seen = {str(e).strip() for e in (applied_ids or set()) if str(e).strip()}
    bookable, applied = [], []
    for item in items:
        # 两侧都归一（docstring 的声称在这行兑现）：条目侧今天由 `_wedge_entry_from_marker` 洗过，
        # 但本函数被写成纯函数就是为了「不依赖上游卫生」——少一次 strip 的代价是守卫静默失效，
        # 症状正好是它要防的那件事（刚投成功岗被永久跳掉，且全程无声）。
        (applied if str(item["encryptJobId"]).strip() in seen else bookable).append(item)
    return bookable, applied


def _merge_wedge_ledger(entries, items):
    """把取证条目并进台账（纯函数：不改入参），返回 (台账, 统计)。
    同 eid 多场楔死合并成**一条**并累计次数（票面实证就是同一岗连续 4-5 场），合并后
    要能回答「这个岗第几次楔的」（wedge_count）与「最近一场是哪场」（session / sessions 末位）。
    幂等口径：来源场名已在该条的 `sessions` 里 → 跳过，不重复计数——取证目录每场都会被重扫
    （`sweep_expired_data` 只删 shots/），没有这条就会把同一场楔死念成第 N 次。
    读不懂的既有条目照原样保留：本切片无权判定、也无权删除别人写进台账的东西。"""
    out = []
    index = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        item = dict(e)
        if isinstance(item.get("sessions"), list):
            item["sessions"] = list(item["sessions"])
        out.append(item)
        eid = item.get("encryptJobId")
        if isinstance(eid, str) and eid:
            index.setdefault(eid, len(out) - 1)
    stats = {"added": 0, "merged": 0, "duplicates": 0}
    for src in items:  # 调用方按场名升序喂进来，故最后并进的那场就是最近一场
        eid = src["encryptJobId"]
        pos = index.get(eid)
        if pos is None:
            entry = dict(src)
            entry["sessions"] = [src["session"]]
            entry["wedge_count"] = 1
            out.append(entry)
            index[eid] = len(out) - 1
            stats["added"] += 1
            continue
        entry = out[pos]
        sessions = entry.get("sessions")
        if not isinstance(sessions, list):
            sessions = [entry.get("session")] if entry.get("session") else []
        if src["session"] in sessions:
            stats["duplicates"] += 1
            continue
        sessions = sessions + [src["session"]]
        entry["sessions"] = sessions
        entry["session"] = src["session"]
        entry["wedge_count"] = len(sessions)
        # 「第几次楔」= 来源场数；岗位字段取最新一场的（岗位名/品牌会改，节拍与停摆时长同理）
        entry["wedged_at"] = src.get("wedged_at")
        entry["last_label"] = src.get("last_label")
        entry["stale_seconds"] = src.get("stale_seconds")
        for key in ("jobName", "brandName"):
            if src.get(key):
                entry[key] = src[key]
        stats["merged"] += 1
    return out, stats


def _backfill_wedge_ledger(session_base, ts, ledger_path, applied_ids, log=print):
    """B1① 回灌写入：扫 `session_base` 下**早于本场**的会话目录里尚未入账的
    WATCHDOG_KILLED.json，归因合格的岗位并进台账（原子写），返回统计字典。
    没有可入账岗位时**不写文件**（口径同 #55 的 `retry_touched`：正常会话不凭空造台账）。
    扫描范围按 `_TS_DIR_RE` 认目录名（照 `sweep_expired_data` 的约束：非会话形态的目录不碰），
    且只认严格早于本场 ts 的场名（本场目录里的取证=自己还活着；更晚的场名不可能是上一场）。
    任何一处坏输入（目录不可读 / 取证坏 JSON / 台账写不进去）都只 warn 后继续——
    台账是止损辅助，它自己坏了不许把投递会话终结掉。

    `applied_ids`（#58 T2）：本场开跑读到的已投台账（`applied_history` 的 eid 集合）。
    归因门槛**通过**的取证若 eid 已在其中，一律不入账并当场点名（正向守卫，论证见
    `_split_already_applied_wedge_items`）。调用点必须显式交代这个集合——留默认空集等于
    把守卫悄悄关掉，而这条路径的代价是「把已成功投出的岗永久跳过」。"""
    stats = _new_wedge_stats()
    try:
        dirs = sorted(
            d for d in Path(session_base).iterdir() if d.is_dir() and _TS_DIR_RE.match(d.name) and d.name < ts
        )
    except OSError as e:
        log(f"[warn] 楔死台账回灌扫描失败（{str(e)[:60]}）——本场不从历史取证入账岗位")
        return stats
    items = []
    for d in dirs:
        marker = d / WATCHDOG_KILLED
        if not marker.exists():
            continue
        stats["scanned"] += 1
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001  # 坏取证跳过：呈报面（_surface_prior_wedge）已另行念过
            stats["unreadable"] += 1
            continue
        if not isinstance(data, dict):
            stats["unreadable"] += 1
            continue
        item = _wedge_entry_from_marker(d.name, data)
        if item is None:
            stats["rejected"] += 1
            continue
        items.append(item)
    if stats["unreadable"]:
        log(f"[warn] {stats['unreadable']} 份击杀取证读不出来（跳过入账，开跑呈报不受影响）")
    items, applied_items = _split_already_applied_wedge_items(items, applied_ids)
    stats["already_applied"] = len(applied_items)
    if applied_items:
        who = "、".join(
            f"{it['jobName'] or '岗位名未取证'}（eid {it['encryptJobId']}，来源 {it['session']}）"
            for it in applied_items
        )
        log(
            f"[跳岗] {len(applied_items)} 份击杀取证的岗位**已在已投台账 applied_history 里**："
            f"已投出的岗位出现在击杀取证中＝归因可疑，跳过入账并**请核查**（#58 T2；"
            f"T1 后合法长等待已持续报平安，正常不该再出现这条）：{who}"
        )
    if not items:
        return stats
    entries, merged_stats = _merge_wedge_ledger(_load_wedge_skipped(ledger_path), items)
    stats.update(merged_stats)
    stats["booked"] = stats["added"] + stats["merged"]
    if not stats["booked"]:
        return stats  # 全是重复场次 → 台账原样，不写盘
    try:
        _save_wedge_skipped(ledger_path, entries)
        stats["written"] = True
    except OSError as e:
        log(f"[warn] 楔死台账写入失败（{str(e)[:60]}）——本场按已读到的台账继续，历史取证下次再入账")
    _logging.log_event(
        "wedge_ledger_backfilled",
        new_entries=stats["added"],
        merged=stats["merged"],
        duplicates=stats["duplicates"],
        rejected=stats["rejected"],
        already_applied=stats["already_applied"],  # #58 T2 守卫（与 rejected 分桶，理由见键定义处）
        written=stats["written"],
        ledger=str(ledger_path),
    )
    return stats


def _surface_wedge_ledger(entries, enabled):
    """B1② 开跑呈报：台账里的岗位逐个念出来（岗位名 + eid + 第几次楔 + 最近哪场 + 死在哪个节拍），
    再念清**代价**与**放行办法**。
    与 `_surface_prior_wedge`（上一场被击杀）刻意不合并成一条：那条答「刚死了一场」——
    哪怕归因条件不满足、台账里没入账也照报；这条答「从此不再碰哪些岗、凭什么、代价是什么」。
    两种信息混在一行里，读的人就没法区分「历史死过」与「本场不再投」。"""
    if not entries:
        return
    print(
        f"[跳岗] 楔死台账 {len(entries)} 个岗位（{WEDGE_LEDGER_ENV}={'开' if enabled else '关'}，"
        f"默认开＝这些岗不再投递）"
    )
    for e in entries:
        print(
            f"  - {e.get('jobName') or '岗位名未取证'}（eid {e.get('encryptJobId')}）"
            f"第 {e.get('wedge_count') or 1} 次楔，最近一场 {e.get('session') or '来源未知'}，"
            f"死在 {e.get('last_label') or '节拍未取证'}，停摆 {e.get('stale_seconds')}s"
        )
    if not enabled:
        # 关闭侧同样要念，而且要把「关」的代价念出来：这些岗会照常消耗本场会话。
        # 与 dry-run 那行（`_prepare_wedge_ledger` 里）分开打，两种降级各自可读。
        print(
            f"[跳岗] {WEDGE_LEDGER_ENV}=off——本场不跳过上述岗位：它们会照常消耗会话，"
            "每个再楔一场 ≈240s + 一次浏览器重启；本场也不回灌历史取证"
        )
        return
    print(
        "[跳岗] 代价如实登记：这些岗从此不投＝可能漏投一个潜在达标岗（用户 2026-09-28 拍板默认开）；"
        f"临时放行（换网络/换时段再试一次）：{WEDGE_LEDGER_ENV}=off"
    )


def _wedge_ledger_stats(state):
    """B1⑤ report 读数：答两件事——台账总条数（`total`）、本场因台账跳过了几岗
    （`session_skipped` + `skipped_jobs` 明细）。
    未接线（桩 state / 老报告读者）时给零形态而不是缺键：读报告的人要能分辨
    「台账是空的」与「这版代码没跑过开跑」，两种情况的处置动作不一样。"""
    skips = state.get("wedge_skips") or []
    stats = {"enabled": False, "total": 0, "backfilled": 0, "rejected": 0, "already_applied": 0}
    stats.update(state.get("wedge_ledger") or {})
    stats["session_skipped"] = len(skips)
    stats["skipped_jobs"] = list(skips)
    return stats
