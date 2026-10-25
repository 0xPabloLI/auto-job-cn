#!/usr/bin/env python3
"""
入站对话回复（#66 阶段 2）：把「招聘方主动来找我们、我们没发过」的那批对话变成一条动作面。

现状与为什么单独一条链路（票面实测 2026-10-20）：54~81 条会话里 16~18 条归不到投递记录，
其中 16 条末条是对方发言＝球在我们这侧；这批在职位搜索面里不可达（真实词表命中 0/16，
加词救不了），而投递链路只发首封、没有「回已有对话」的入口。

三条与其它链路分开的口径，都是刻意的：
- **不占投递名额**：`APPLY_DAILY_LIMIT` 阶梯管的是「一天沟通多少个新岗」，回复已有对话不是那个
  动作，所以本链路既不看 `run.limit` 也不写 `applied_history`；风控侧共用同一套
  静默时段 / 突发节奏 / 验证码即停，不另造、也不新加数字上限（日上限的升降
  只由 `docs/agents/apply-tuning.md` 那三个风控信号驱动，判据在那里，不在这里）。
- **不进招呼语台账**：回复不写 `greetings.json`——#19 eval 的配对从招呼语台账侧驱动，
  把回复混进去会污染回复率分母（票面非目标第 1 条）。另立 `inbound_replies.json`。
- **默认 dry-run；真发只接受你点名的那一条**：打开会话会把它标成已读，而"这条读没读"的判定权归
  用户（2026-10-21 拍板：脚本永不替他决定要不要读）。所以 `--send` 必须配 `--only <eid>`，裸
  `--send` 直接拒绝启动——那一下会连开十几条他并没打算读的会话。只读阶段的信息全部来自 wapi
  会话列表 + 岗位详情页，零点击；`unreadMsgCount` 已被实测否证（见 `platforms/chat_first.read_state`），
  本模块不拿它当判据，只当原始字段念。

#73 起，打开与确认都**不依赖会话卡片是否渲染**（真机实测：wapi 68 条会话，DOM 只渲染 40 张卡片，
目标那条在整份 HTML 里 0 命中，滚页面 12 轮不增）：
- **打开**＝按 id 深链：wapi 条目自带 `encryptUid` / `encryptBossId`，`CHAT_URL?id=<招聘者加密 id>`
  一次导航直达，不受渲染数量限制。没有可用加密 id ⇒ `no_deeplink_id`；导航失败，或打开之后
  **右侧会话头不是这条对话** ⇒ `conv_unreachable`（独立桶 + 存证）。会话头而不是 URL 才是判据：
  真机从站内页导航过去时 SPA 会把 `?id=` 规范化掉（`20261021-182455` 那场把一次正常打开读成了
  失败）。`conv_not_found` 是**已移除的卡片路径**的历史桶，本路径不再产出——「列表没渲染这条」
  与「打开时被站点换走了」是两种归因，不许混。
- **确认**＝重新读 wapi `getGeekFriendList`（复用 `ChatFirstPlatform.followup`）：按 `encryptJobId`
  唯一命中同一条，要求末条 `fromId == 我方 userId` 且 `lastMsg` 等于我们发的那句，并用
  `lastTS` 相对点开前前进作对照。点开前后各回读一次，两次读数都落 `outcomes[]` 供复核。

机械件复用 `live_apply`（发送前 gate、称呼与软问题提醒、#62 的形态层与元素层守卫、
输入框轮询、打字与点击重试、行缓冲），不另写第二份。

#75 补的是同一件事的另一半：**「该先读哪几条」自己记水位线**。既然 Boss 的未读字段判不出、
又不许靠点开来分类，那就在每次只读读数之间记一个基准，下次只列比它新的那些。判据与写入口都在
`_inbound_watermark.py`（单一真源，`followup_tracker` 也读它），这里只接线：
- 只读跑（含 dry-run）**只报不写**——`newSinceWatermark` / `watermark` 进报告，候选清单每条带
  `sinceWatermark`；
- 基准只由 `--mark-read` 推进，语义是**用户当面说过「这次读到的这批我看过了」**。推进的是本次
  读到的一整批（候选清单列出的、以及因末条是我方或已归到投递记录而没列出的，都算看过）——只推
  列出的那几条，其余每场都会被重复提醒。脚本自己每跑一次就推进，等于把「用户没看过的」偷换成
  「脚本没见过的」，下一场就会报「新增 0」而用户一条没看——所以 `--mark-read` 也不点会话，
  它只写一个文件。
"""

import argparse
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from camoufox.sync_api import Camoufox  # noqa: E402

import gen_greetings as g  # noqa: E402  #白名单/四道 gate 的扫描实现处
import live_apply as la  # noqa: E402  #发送前 gate 与聊天页守卫的单一实现处
from _atomic_io import write_json_atomic  # noqa: E402
import _inbound_watermark as wm  # noqa: E402  #水位线单一真源（#75）；写入口只有 advance_watermark 一个
from _limits import (  # noqa: E402  #风控形态防护沿用投递那套，不另造
    SessionPacer,
    current_hour,
    is_quiet_hour,
    load_config,
    startup_jitter,
)
from _profile_paths import (  # noqa: E402  #28：私有路径唯一出口
    browser_profile_dir,
    private_path,
    profile_line,
    resolve_active_profile,
)
from platforms import get_platform  # noqa: E402
from platforms.chat_first import (  # noqa: E402
    CHAT_GOTO_TIMEOUT_MS,
    STATE_LABELS,
    chat_url_for,
    inbound_waiting,
    job_identity,
    read_state,
)

# 起草协议的文件名**故意与投递链路同名**（`request_N.json` / `reply_N.json`）：为的是原样复用
# `live_apply.wait_reply`——那份「轮询 + 损坏文件继续等 + SKIP 哨兵 + 超时后 90s 宽限复查迟到草稿」
# 的语义是 #55 实测出来的，fork 一份新的等于把已知的坑再踩一遍。
# 隔离靠**目录**：本链路写 `private/data/inbound_session/<ts>/`，投递写 `data/live_session/<ts>/`。
# 心跳也不受影响：`_touch_heartbeat` 在 `_HEARTBEAT_DIR` 未绑定时直接返回 False，本进程不绑它。
REQUEST_PREFIX = "request_"
REPLY_PREFIX = "reply_"
SESSION_DIR_FILE = "data/inbound_session"  # 会话产物根（request/reply/shots/report）
LEDGER_FILE = "data/inbound_replies.json"  # 跨会话台账，按 encryptJobId 幂等（去重的唯一真源）

# 跳过原因的全集（恒在、零也写出来，口径同 #62 `no_send_reasons`、#65 `recommend_*`）：
# 分桶只增不减，读数才可比。判定阶段（`classify`）与浏览器阶段（`reply_one`）产出的原因都在表里。
# #73 移除了卡片点开路径，`conv_not_found` / `conv_click_failed` 与卡片路径的 `conv_ambiguous`
# **保留**（历史报告要能按同一张表回放）；`conv_ambiguous` 另有新用途：`--only` 命中同一 eid
# 的多条会话时，深链按 id 也分不清是哪一位招聘者（见 `_drive` 的发前唯一性检查）。
SKIP_REASONS = (
    "no_job_id",  # DOM 降级路径或 wapi 少给 encryptJobId ⇒ 绑不上岗，不猜
    "already_replied",  # 同 eid 已回过（防的是「对方回一句我们再回一句」的自动往来）
    "job_dead",  # 详情页已不是岗位页（404 / 问答、SEO 内容页）
    "job_unresolved",  # 岗位身份认不出（title 取不到或去壳后短到不像岗位名）
    "job_unknown",  # 这一条根本没反查过（快照与反查集合不一致）
    "login",  # 反查时落在登录页（整场停，不是这条岗的问题）
    "conv_ambiguous",  # 同一 eid 命中多条可回会话（#73 深链后仍分不清是哪一位）⇒ 不发
    "conv_not_found",  # 卡片路径遗留（#73 起不再产出）：列表里找不到这条对话（快照与现场漂移）
    "off_chat",  # #62 形态层：深链落地后面板不在沟通形态
    "no_input",  # 输入框族全落空
    "gate_blocked",  # 发送前机械 gate 未过
    "agent_timeout",  # 草稿窗口 + 宽限复查均无果
    "agent_skip",  # agent 读了对方原文后判定这条不值得回（{"skip": true} + reason）
    "conv_click_failed",  # 卡片路径遗留（#73 起不再产出）：卡片唯一命中但点不动（站点形态漂移）
    "no_deeplink_id",  # #73：wapi 条目没有可用加密 id ⇒ 无法按 id 深链（不猜、不回列表）
    "conv_unreachable",  # #73：深链导航失败 / 落地 URL 的 id 不是目标（不是 conv_not_found）
    "send_btn_missing",  # 字已进输入框，既无发送按钮也非 contenteditable（口径同 #62）
    "send_click_failed",  # 字已进输入框，发送那一下没成（口径同 #62）
    "send_unconfirmed",  # 发了但回读没确认（**不写台账**，宁下次重审不双发）
    "risk_blocked",  # 验证码/安全验证
    "login_expired",  # 会话中途掉登录
    "silent_hours",  # 进入静默时段
    "other",
)

MODES = ("dry_run", "send")
# wapi 路径的 time 是 `datetime.fromtimestamp(...).strftime("%Y-%m-%d %H:%M:%S")`（见
# platforms/chat_first.py 的 parse_friend_entries），这个前缀就是「可比较的刻度」的判据。
_ISO_TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def load_ledger(path):
    """回复台账 → 条目列表。文件缺失/损坏从 [] 起步（读不出**不覆盖**，由写侧的原子写决定结果）。"""
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def replied_job_ids(ledger):
    """台账里**已确认发出**的 encryptJobId 集合。

    `send_unconfirmed` 的条目不进这个集合（写侧也不会给它建条目）——回读确认是台账的唯一入口，
    所以「没确认」自然等于「可以再提出」，由人复看而不是脚本自动重发。"""
    return {e.get("encryptJobId") for e in ledger if isinstance(e, dict) and e.get("encryptJobId")}


def replied_today(ledger, today=None):
    """当日已确认回复条数（**只报不拦**：这列是给用户看的读数，不是额度闸门）。
    `sentAt` 形如 `2026-10-20T18:22:03`，按日期前缀比；非串形态不计（不把坏数据读成今天发的）。"""
    day = (today or date.today()).isoformat()
    return sum(1 for e in ledger if isinstance(e, dict) and str(e.get("sentAt") or "").startswith(day))


def record_reply(ledger_path, entry):
    """确认发出后回写台账，按 encryptJobId 幂等覆盖（同岗重复提出时更新那条，不堆积）。
    返回 added / updated / skipped；写盘失败只 skipped 不抛——台账层绝不反噬已经发出去的消息
    （与 `live_apply.record_greeting` 同一条纪律）。"""
    eid = entry.get("encryptJobId")
    if not eid:
        return "skipped"
    ledger = load_ledger(ledger_path)
    at = next((i for i, e in enumerate(ledger) if isinstance(e, dict) and e.get("encryptJobId") == eid), None)
    if at is None:
        ledger.append(entry)
    else:
        ledger[at] = {**ledger[at], **{k: v for k, v in entry.items() if v is not None}}
    try:
        write_json_atomic(Path(ledger_path), ledger)
    except OSError as e:
        print(f"    [warn] 回复台账写入失败（{str(e)[:80]}）——本条已发出但没入库，呈报侧会看得见缺口")
        return "skipped"
    return "added" if at is None else "updated"


def _time_sort_key(conv):
    """`time` 有两种刻度：wapi 路径是本地 ISO（`2026-10-20 11:00:00`），DOM 降级路径只有
    展示串（`昨天` / `09月09日`）。两者**不可直接比大小**——按裸串降序，CJK 码位高于数字，
    「昨天」会排到所有 ISO 之前（第一版就是这么错的，用例 `test_sort_waiting_...` 抓住）。
    所以先按「是不是 ISO」分档，展示串一律沉底，档内保持站点给的顺序。"""
    text = str(conv.get("time") or "")
    is_iso = bool(_ISO_TIME_RE.match(text))
    return (is_iso, text if is_iso else "")


def sort_waiting(convs):
    """等回话的那批按最后一条消息时间**新→旧**排。
    选新→旧而不是旧→新：越新的对话那个岗还在的概率越高，而「等了三天」这件事本身
    不构成优先回它的理由（票面实测里 4/16 的详情页已经 404）。"""
    return sorted(convs, key=_time_sort_key, reverse=True)


def classify(report, identities, replied, base=None, my_user_id=None):
    """会话快照 + 岗位身份反查 + 回复台账 → 逐条候选（含「为什么这条不能回」）。

    三条硬判据的顺序是有意义的：**先**问有没有 encryptJobId（没有就什么都绑不上），
    **再**问回没回过（回过的不必再去反查岗位，省一次页面往返），**最后**才问岗还在不在。
    每条候选都带 `skip` 或 `eligible`，二居其一——不许有「既没跳过也没被选中」的悬空条目。

    `base`（#75 的水位线基准）只用来给每条打 `sinceWatermark`——它是**呈报列**，不参与任何一条
    资格判据；判据全在 `_inbound_watermark.due_reason` 里，这里不重写。基准缺席（没接线/首跑）
    时整列为 False，不是「都是老的」而是「无从判定」，由报告里的 `watermark.state` 说话。"""
    out = []
    for conv in sort_waiting(inbound_waiting(report.get("conversations", []))):
        eid = conv.get("encryptJobId")
        cand = {
            "encryptJobId": eid,
            "company": conv.get("company"),
            "brandName": conv.get("brandName"),
            "bossName": conv.get("name"),
            "bossTitle": conv.get("title"),
            "lastMsg": conv.get("lastMsg") or conv.get("summary") or "",
            "lastTime": conv.get("time") or "",
            "readState": read_state(conv),
            "source": conv.get("source"),
            # #73 深链与回读的地址/对照字段（wapi 路径有值，DOM 降级一律 None）
            "encryptUid": conv.get("encryptUid"),
            "encryptBossId": conv.get("encryptBossId"),
            "bossUid": conv.get("bossUid"),
            "lastTS": conv.get("lastTS"),
            "lastFromId": conv.get("lastFromId"),
        }
        if not eid or cand["source"] != "wapi":
            # DOM 降级路径没有 encryptJobId（#30 探测结论），身份、去重、失效判定全都没有落点
            cand["skip"] = "no_job_id"
        elif eid in replied:
            cand["skip"] = "already_replied"
        else:
            ident = identities.get(eid)
            if not ident:
                cand["skip"] = "job_unknown"
            else:
                cand["jobTitle"] = ident.get("jobTitle") or ""
                cand["jobState"] = ident.get("state")
                cand["jobRawTitle"] = ident.get("rawTitle") or ""
                state = ident.get("state")
                if state == "ok":
                    cand["eligible"] = True
                elif state in ("dead", "unresolved", "login"):
                    cand["skip"] = f"job_{state}" if state != "login" else "login"
                else:
                    cand["skip"] = "other"
        cand["sinceWatermark"] = bool(base is not None and wm.above_line(cand, base, my_user_id))
        out.append(cand)
    return out


def skip_stats(candidates, outcomes=()):
    """跳过原因分桶，恒在（零也写）。集外的原因落 `other`，不静默丢。

    两个来源合到同一张表：判定阶段的 `cand["skip"]`，和浏览器阶段的 `outcome["reason"]`
    （只有 `status != "replied"` 的 outcome 才计）。真实流程里一条候选只会来自其中一处
    ——判定阶段跳过的条目根本不进浏览器阶段——所以不存在双计；把它们分开写会让
    `skippedReasons` 只覆盖一半，读数为「为什么没回」的人拿不到浏览器阶段的答案。"""
    stats = {reason: 0 for reason in SKIP_REASONS}

    def _bump(reason):
        stats[reason if reason in stats else "other"] += 1

    for cand in candidates:
        if cand.get("skip"):
            _bump(cand["skip"])
    for out in outcomes:
        if isinstance(out, dict) and out.get("status") not in (None, "replied") and out.get("reason"):
            _bump(out["reason"])
    return stats


def request_payload(idx, cand):
    """写 `request_N.json` 的载荷：会话上下文 + 对方原文 + 岗位身份。
    `kind` 恒为 `inbound_reply`——文件名与投递链路一致（为复用 `wait_reply` 的窗口语义），
    所以「这是回复不是首封」这件事得靠载荷里的这个键说话，起草侧一眼就要看见。"""
    return {
        "idx": idx,
        "kind": "inbound_reply",
        "encryptJobId": cand.get("encryptJobId"),
        "company": cand.get("company"),
        "brandName": cand.get("brandName"),
        "bossName": cand.get("bossName"),
        "bossTitle": cand.get("bossTitle"),
        "jobTitle": cand.get("jobTitle"),
        "theirLastMessage": cand.get("lastMsg"),
        "theirMessageTime": cand.get("lastTime"),
        "readState": cand.get("readState"),
        "requested_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def gate_problems(send_text, level, notes, state_evidence, cfg):
    """回复正文走**同一个**发送前机械 gate（无豁免分支）：四道 gate + level + notes 备注区
    + 未实测状态词。事实纪律不因「这是回复不是首封」而松——脑补一条事实的代价与文体无关。
    白名单缺失按 live_apply 的语义跳过白名单那一项，其余照拦。"""
    whitelist = g.load_whitelist_file(cfg.get("fact_cards_path") or g.FACT_CARDS)
    if whitelist is None:
        print("  [warn] fact_cards 白名单不可用——白名单 gate 跳过，其余三项照拦")
    return la.pre_send_gate(
        send_text,
        level,
        whitelist,
        notes=notes,
        sections=cfg.get("profile_sections"),
        state_evidence=state_evidence,
    )


def warn_lines(cand, send_text):
    """两条软提醒（不阻断，口径同首封）：称呼规范 v3 + 结尾只带软问题。
    复用 live_apply 的函数，判据不重写——回复场景的对面同样是那个人，答不上来的问题照样答不上来。"""
    salu = la.salutation_warnings(cand.get("bossName"), cand.get("bossTitle"), send_text)
    for w in salu:
        print(f"  [warn 称呼] {w}")
    qwarn = la.question_warnings(send_text)
    for w in qwarn:
        print(f"  [warn 提问] {w}")
    return salu, qwarn


def summarize(mode, report, candidates, outcomes, ledger, started_at, base=None, due=None):
    """本场回复报告（恒在键）。`repliedToday` 只报不拦——额度治理的既有结论在
    `docs/agents/apply-tuning.md`（阶梯 + 风控信号驱动升降），这里不另造第二套。

    #75 两个读数键恒在（0 也写出来，口径同 `skippedReasons`）：`newSinceWatermark` 是整数 N，
    基准不可用时是 `null`（**不是** 0、**不是**全部条数）；`watermark.state` 说明是哪一种不可用。
    `base`/`due` 缺席（调用方没接线，如老的用例形状）时按首跑落键——宁可读数说「无基准」，
    也不让报告缺键。"""
    base = base if base is not None else wm.empty_base()
    due = due if due is not None else wm.split_due([], base)
    return {
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "startedAt": started_at,
        "mode": mode,
        # 快照生成时刻跟着进呈报：#63（会话列表分页未取全）没修之前，这批条数只会偏小
        "snapshotAt": report.get("generatedAt"),
        # 回读判据的我方 userId（#73）：快照采集那一刻拿到，报告里留一份供事后复核
        "myUserId": report.get("myUserId"),
        "snapshotConversations": report.get("totalConversations"),
        "inbound": report.get("unlinkedReplied"),
        "inboundUnread": report.get("unlinkedRepliedUnread"),
        "candidates": len(candidates),
        "eligible": sum(1 for c in candidates if c.get("eligible")),
        "skippedReasons": skip_stats(candidates, outcomes),
        "replied": sum(1 for o in outcomes if o.get("status") == "replied"),
        "repliedToday": replied_today(ledger),
        # 水位线读数（#75）：较上次基准新增几条 + 基准本身长什么样（判据都在 _inbound_watermark）
        "newSinceWatermark": due["newCount"],
        "watermark": wm.watermark_reading(base, due),
        "outcomes": outcomes,
    }


# --- 浏览器层：按 id 深链打开、发送与 wapi 回读确认 --------------------------------
# #73 的判据来源：打开＝wapi 条目的 `encryptUid` / `encryptBossId` → `CHAT_URL?id=<招聘者 id>`
# （`platforms.boss.chat_url_for`）；确认＝重新读 `getGeekFriendList`（复用 `ChatFirstPlatform.followup`），
# 不再读卡片末条。真机上 68 条会话只渲染 40 张卡片、目标那条 0 命中，所以「卡片是否渲染」这个
# 前提已从本链路彻底移除——重名不再靠文案（深链按 id），但同一 eid 多会话仍会落
# `conv_ambiguous`（见 `_drive` 的发前唯一性检查）。
# **真机 2026-10-21 补一条形态事实（`20261021-182455` 那场 `conv_unreachable`）**：深链确实打开了
# 目标会话（截图右侧就是「槿先生 岑淞覃邝邝覃 招聘经理」），但从站内页（岗位详情页）导航过去时
# SPA 会把 URL 规范化成不带 `?id=` 的 `/web/geek/chat`——所以**落地 URL 不能当目标判据**，
# 否则一次正常打开会被读成失败。改用右侧会话头 `.base-info` 的去空白文本，
# 它与 wapi 的 `company`（姓名+品牌+抬头）逐字相等，且不依赖左侧列表渲染到哪一条。
# 归因分工：`no_deeplink_id`＝wapi 没给可用加密 id（形态漂移，不猜）；`conv_unreachable`＝有 id
# 但打开失败（导航抛异常 / 打开的不是这条会话，存证在 `deeplink-mismatch-<idx>.json`）；
# `conv_not_found` 是已移除的卡片路径的历史桶，本路径**不再产出**——「列表没渲染这条」与
# 「打开时被站点换走了」是两种归因，混桶会把排查引向错误的方向（假归因比崩溃难查）。
WAPI_CONFIRM_ATTEMPTS = 2  # 发送后回读的次数上限（列表刷新可能比消息落库慢一拍）
WAPI_CONFIRM_RETRY_SLEEP = (3.0, 5.0)  # 两轮回读之间的随机等待（量级＝一次列表重渲染）
# 发送触发到回读导航之间的落库窗口：回读第一跳是 `page.goto(CHAT_URL)`，发得太紧会把还在
# 路上的发送请求打断。这不是绕过渲染的等待，而是让**已经触发**的那次发送先完成。
WAPI_CONFIRM_SETTLE = (2.0, 4.0)
IDENTITY_HOP_SLEEP = (2.0, 4.5)  # 逐岗反查之间的随机短等（与投递的读页停顿同量级）
# 右侧会话头 = 目标判据（真机 2026-10-21：落地 URL 的 ?id= 会被 SPA 规范化掉，不能当判据）。
# 锚是 `.top-info-content .base-info`：去空白后与 wapi 的 `company`（姓名+品牌+抬头）**逐字相等**。
# 两个刻意选择：① **不带全页 fallback**——`.base-info` 一旦哪天也出现在左栏卡片里，全页第一个
# 可能是别人的会话，宁可读不到也不认错；② 不用父层 `.user-info`，它把「更多」按钮一起卷进
# innerText（`…招聘经理更多`，实测于 `20261021-185645` 的 actualHeader），全等判据会把一次
# 正常打开读成失败。JS 一并回命中数：>1 就是歧义，不猜哪一个。
CHAT_TARGET_HEADER_JS = (
    "() => { const els = document.querySelectorAll('.top-info-content .base-info'); "
    "const t = els.length ? (els[0].innerText || '').replace(/\\s+/g, '') : ''; "
    "return { text: t, hits: els.length }; }"
)
TARGET_HEADER_POLL_ROUNDS = 10  # 等右侧那一栏水合的重试数（× 下面的间隔 = 5s 上限）
TARGET_HEADER_POLL_INTERVAL = 0.5


def _norm(text):
    """去空白比对底座：wapi 的 `company` 是 `"".join(name, brand, title)`，DOM 的 `name_box` 是
    `re.sub(r"\\s+", "", innerText)`——两条路径归一到同一种形态才可比。发送确认也用它比
    `lastMsg`：站点可能在我们那句里塞/吞空白，逐字符比会把「已发出」判成未确认。"""
    return re.sub(r"\s+", "", str(text or ""))


def _page_url(page):
    """页面 URL 的读取点（读不到按空串）：存证、detail 与 `_url_target_id` 共用一个 try。"""
    try:
        return getattr(page, "url", "") or ""
    except Exception:  # noqa: BLE001  # 页面已关 / 上下文销毁时读 URL 会抛
        return ""


def _url_target_id(page):
    """`page.url` 的 query 里那一个 `id` → 串；缺失 / 空 / 多个都返回 None（宁缺毋造）。
    **只反不立**：真机从站内页导航进深链时 SPA 会把 query 规范化掉（`20261021-182455` 那场会话
    确实打开了，URL 却回到裸 `/web/geek/chat`），所以「没有 id」是正常态、不能当失败；
    但「带着别人的 id」是矛盾（深链没生效、面板还停在上一位），那一格拿来否决。
    「多个」也算读不出：那时无从判断站点认的是哪一个，拿第一个去比等于猜。"""
    try:
        values = parse_qs(urlparse(_page_url(page)).query).get("id") or []
    except Exception:  # noqa: BLE001  # 非法 URL 不把整条链路带崩
        return None
    if len(values) != 1:
        return None
    value = str(values[0]).strip()
    return value or None


def _chat_header_text(page):
    """右侧会话头 → `(去空白文本, 命中数)`；读不到按 `("", None)`，不把一次读失败伪装成「不是这条」。"""
    try:
        got = page.evaluate(CHAT_TARGET_HEADER_JS) or {}
        return str(got.get("text") or ""), got.get("hits")
    except Exception:  # noqa: BLE001  # 页面在换 / 上下文销毁时读 DOM 会抛
        return "", None


def _wait_for_open_target(page, cand, sleep_fn=None, rounds=TARGET_HEADER_POLL_ROUNDS):
    """等右侧那一栏水合并判它是不是这条对话 → `(状态, 末次读数, 末次命中数)`。
    状态四格分开，因为**处置方向不同**：`matched` 放行；`empty`＝锚没命中（选择器腐烂或面板
    没出来，要去查形态）；`mismatch`＝打开的是别人（这条不能发）；`ambiguous`＝同屏多个锚
    （不猜哪一个，直接收）。把 empty 说成 mismatch 会把「选择器失效」读成「站点换走了」，
    那正是本票要避免的假归因。
    判据是**去空白全等**（子串会让「沐女士煦辰招聘」与「沐女士煦辰招聘者」互撞）；
    等的是面板水合，不是「多等一会儿左侧也许就渲染出来了」——左侧那条路已被深链整体换掉。
    按**轮数**而不是挂钟收口：`sleep_fn` 在测试里是桩，用墙钟判上限会让桩变成空转。
    `sleep_fn` 缺省在调用时解析（`=time.sleep` 会在 import 时绑死原函数，桩打不住）。"""
    target = _norm(cand.get("company"))
    sleep = sleep_fn or time.sleep
    header, hits = "", None
    for attempt in range(rounds):
        header, hits = _chat_header_text(page)
        if isinstance(hits, int) and hits > 1:
            return "ambiguous", header, hits
        if target and _norm(header) == target:
            return "matched", header, hits
        if attempt < rounds - 1:
            sleep(TARGET_HEADER_POLL_INTERVAL)
    return ("empty" if not _norm(header) else "mismatch"), header, hits


def _save_deeplink_failure(ctx, idx, expected_url, actual_url, cand, error="", actual_header="", header_hits=None):
    """深链失败的独立存证（best-effort）：截图之外再落一份机器可读的对照。
    写不进不能反噬流程——存证是复核材料，不是动作的前置条件。"""
    payload = {
        "idx": idx,
        "expectedUrl": expected_url,
        "actualUrl": actual_url,
        "expectedCompany": cand.get("company"),
        "actualHeader": actual_header,
        "headerHits": header_hits,
        "candidate": cand,
        "error": str(error)[:500],
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    try:
        write_json_atomic(Path(ctx.session_dir) / f"deeplink-mismatch-{idx}.json", payload)
    except Exception as e:  # noqa: BLE001  # 存证失败不反噬动作，但必须留下看得见的缺口
        print(f"    [warn] 深链失败存证写入失败（{str(e)[:80]}）——复核时只剩 shots/ 截图")


def _wapi_reading(conv, at_key, at):
    """一条会话在某个时刻的 wapi 读数投影：发送前快照与发送后回读共用这三个判据字段。"""
    return {
        "lastMsg": conv.get("lastMsg"),
        "lastTS": conv.get("lastTS"),
        "lastFromId": conv.get("lastFromId"),
        at_key: at,
    }


def _wapi_before(cand, ctx):
    """点开前的 wapi 读数（来自发送前快照）：回读对照的左半。`snapshotAt` 说明它属于哪一场快照。"""
    return _wapi_reading(cand, "snapshotAt", ctx.snapshot_at)


def _wapi_after(conv, read_at):
    """回读命中条目的同一组键（对照的右半）。`readAt` 是这次回读的时刻。"""
    return _wapi_reading(conv, "readAt", read_at)


def _readback_problem(conv, target, my_uid, before_ts):
    """一条回读条目是否构成「我们那句已发出」的证据 → None 成立，否则返回原因。
    拆出来是给 `confirm_sent_by_readback` 的语句数让路（ruff PLR0915 有硬闸），判据本身不分叉。"""
    if conv.get("source") != "wapi":
        return f"回读来源是 {conv.get('source')!r}，不是 wapi"
    if not my_uid:
        return "我方 userId 不可用（ctx.my_user_id 与报告 myUserId 都空）"
    if _norm(conv.get("lastMsg")) != target:
        return f"末条不是我们那句（读回 {str(conv.get('lastMsg'))[:40]!r}）"
    if str(conv.get("lastFromId")) != str(my_uid):
        return f"末条 fromId={conv.get('lastFromId')} 不是我方 {my_uid}"
    after_ts = conv.get("lastTS")
    ts_pair = (after_ts, before_ts)
    if all(isinstance(v, int) and not isinstance(v, bool) for v in ts_pair) and after_ts <= before_ts:
        return f"lastTS 未前进（{before_ts} → {after_ts}）"
    return None


def confirm_sent_by_readback(page, cand, sent_text, ctx):
    """发送确认 = **重新读 wapi 会话列表**，那条会话的末条变成了我们这句。

    判据（全过才算发出，任何一条不过都不写台账）：
    - `encryptJobId` 在回读里**唯一命中**（重名/重岗不猜）；
    - 命中条目 `source == "wapi"`（DOM 降级没有 fromId / lastTS，验不了）；
    - 我方 userId 非空（`ctx.my_user_id` 优先，其次是报告里的 `myUserId`）；
    - `lastMsg` 去空白后等于我们发出去的那句；
    - `lastFromId == 我方 userId`（归属守卫：字进了别的会话时这条不会变）；
    - 双方 `lastTS` 都是 int 时，要求回读值 > 点开前快照值（时间真的前进了）。
    最多 `WAPI_CONFIRM_ATTEMPTS` 轮：消息落库 / 列表刷新比页面慢一拍是真实现象，但不无限等
    （等待不是判据，轮数受常量约束）。返回 `(最后一次命中的条目或 None, 失败原因)`；
    原因带逐轮读数，进 `wapiReadbackError` 供人复核。"""
    target = _norm(sent_text)
    if not target:
        return None, "空文本不判确认"
    eid = cand.get("encryptJobId")
    if not eid:
        return None, "候选没有 encryptJobId，回读无从定位"
    before_ts = cand.get("lastTS")
    last_hit = None
    reasons = []
    for attempt in range(1, WAPI_CONFIRM_ATTEMPTS + 1):
        try:
            # detach=True：同页重复读，必须注销自己挂的 response handler，否则
            # `handlers_live` 会从「天然偏高 1」涨成 2、3…（#73 review finding）。
            report = ctx.platform.followup(page, [], detach=True)
        except Exception as e:  # noqa: BLE001  # 回读失败是一条的归因，不外抛
            report = None
            reasons.append(f"第 {attempt} 轮回读异常 {type(e).__name__}: {str(e)[:80]}")
        if not report:
            reasons.append(f"第 {attempt} 轮回读为空（followup 返回 None）")
        else:
            hits = [c for c in report.get("conversations") or [] if c.get("encryptJobId") == eid]
            if len(hits) != 1:
                reasons.append(f"第 {attempt} 轮按 encryptJobId 命中 {len(hits)} 条（要求唯一）")
            else:
                conv = hits[0]
                why = _readback_problem(conv, target, ctx.my_user_id or report.get("myUserId"), before_ts)
                if why is None:
                    return conv, ""
                last_hit = conv
                reasons.append(f"第 {attempt} 轮：{why}")
        if attempt < WAPI_CONFIRM_ATTEMPTS:
            time.sleep(random.uniform(*WAPI_CONFIRM_RETRY_SLEEP))
    return last_hit, "; ".join(reasons)


def resolve_identities(page, eids):
    """逐岗反查详情页身份 → `(identities, 停批信号)`。
    每一跳之间跑 `detect_risk_signal`：这是本链路唯一会连续做 N 次页面往返的段落，
    风控面比点一次沟通大得多，出现验证码就整场停（沿用投递的「当日不再重启」约定）。
    `login` 也整场停，不把它折算成「这批岗都失效了」。"""
    identities = {}
    for n, eid in enumerate(eids, 1):
        ident = job_identity(page, eid)
        identities[eid] = ident
        label = (ident.get("jobTitle") or ident.get("rawTitle") or "")[:36]
        print(f"  [身份 {n}/{len(eids)}] {eid} → {ident['state']} {label}")
        if ident.get("state") == "login":
            return identities, "relogin"
        risk = la.detect_risk_signal(page)
        if risk:
            print(f"  [熔断] {risk}（身份反查第 {n} 跳）")
            return identities, "stop"
        if n < len(eids):
            time.sleep(random.uniform(*IDENTITY_HOP_SLEEP))
    return identities, None


@dataclass
class _Ctx:
    """一场回复会话的依赖打包（注入进去，好让浏览器阶段能拿假页面驱动 —— R3 的
    「无真实数据不得声称已验」在这里的反面：能验的部分全用桩验掉，真机只补那一跳）。"""

    cfg: dict
    session_dir: Path
    shots_dir: Path
    ledger_path: str
    login_selector: str
    platform: Any = None  # 熔断存证走平台层（口径同投递：risk_blocked 要在 crash/ 留下一份现场）
    mode: str = "dry_run"
    pacer: Any = None
    stop: str | None = None
    my_user_id: Any = None  # #73：回读判「末条是不是我方」的 userId（快照采集那一刻拿到的）
    snapshot_at: Any = None  # #73：发送前快照的时刻，落进 outcome 的 wapiBefore 作对照


def _bail(out, reason, ctx, detail=""):
    """一条候选的落空出口：`status` 与 `reason` 同值（恒在表内），汇报侧能直接对 `skippedReasons`。
    浏览器阶段的落空要 `pacer.wait_skip()` 短等再划走——节奏形态与投递一致，
    连续零间隔动作才是风控想看见的东西。"""
    out["status"] = reason
    out["reason"] = reason
    if detail:
        out["detail"] = str(detail)[:200]
    if ctx.mode == "send" and ctx.pacer is not None:
        ctx.pacer.wait_skip()
    return out


def _type_and_trigger_send(page, input_el, text, idx, ctx, out):
    """把草稿送进输入框并触发发送（阶段 5b 的同形拆分，理由与 `live_apply` 一致：
    「字已进框之后」的落空是另一类归因——那些**不自动补投**，因为可能已经实际发出，
    重发有双发风险；用户 2026-10-09 拍板「只捞确定没打一个字的」）。
    返回 None 表示发送动作已触发（成没成由 `confirm_sent_by_readback` 判），返回 out 表示这条已落空。"""
    input_el.click()
    time.sleep(random.uniform(0.4, 0.9))
    la._type_greeting(page, input_el, text, ctx.cfg)  # 粘贴/逐字混合的行为形态与首封一致
    time.sleep(random.uniform(1.5, 3))
    la.shot(page, ctx.shots_dir, f"typed-{idx}")
    send_btn = la.find_send_button(page)
    if send_btn is not None:
        if not la.click_with_retry(page, send_btn, action="发送按钮"):
            out["typed"] = True  # 字已进框：不自动补投，读数就是它唯一的可复核出口（同 #62）
            return _bail(out, "send_click_failed", ctx)
        return None
    # 判据与 live_apply **同源**（#68 起两边都问浏览器自己的 `isContentEditable`，它同时覆盖
    # contenteditable 元素与 designMode 容器；那边原先查 `"contenteditable" in tagName + '|' + 属性值`，
    # 实得 "DIV|true" 串里永不含那个词 ⇒ Enter 兜底恒不触发，已在本票修掉）。
    # 同源指的是**同一个属性**，不是一字相同：这边回字符串哨兵再比对，那边用 `!!` 取 bool。
    if input_el.evaluate("el => (el.isContentEditable ? 'editable' : '')") == "editable":
        print("    [发送] 聊天页输入框，Enter 发送")
        page.keyboard.press("Enter")
        return None
    out["typed"] = True
    return _bail(out, "send_btn_missing", ctx)


def _open_conversation(page, cand, idx, ctx, out):
    """按 id 深链打开这条会话 → None 表示已落在目标的聊天形态；否则返回已落空的 out。

    顺序有信息量：**先**验候选带不带 `company`（没有就无从验证打开的是谁，浏览器动作都不该发），
    **再**判登录（深链一跳已把页面带到聊天页，这一格才是「掉没掉登录」的现场），**再**问形态
    （URL 还在不在 `/web/geek/chat`），**再**验右侧会话头是不是这条对话，最后才走风控与输入框。
    落地 URL 的 `?id=` **只反不立**：真机站内导航会把它规范化掉（没有＝正常），但**带着别人的 id**
    就是矛盾——那是深链没生效、面板还停在上一位。深链失败一律落独立桶 `conv_unreachable` + 存证，
    绝不冒充 `conv_not_found`（后者是已移除的卡片路径口径）。
    """
    uid = cand.get("encryptUid") or cand.get("encryptBossId")
    if not uid:
        return _bail(out, "no_deeplink_id", ctx)
    if not _norm(cand.get("company")):
        # 会话头判据的左半边没了：这时候去导航等于「打开一个无法确认是谁的面板」，比不发更糟。
        return _bail(out, "other", ctx, detail="候选没有 company，无法验证打开的是谁")
    target_url = chat_url_for(uid)
    try:
        page.goto(target_url, wait_until="domcontentloaded", timeout=CHAT_GOTO_TIMEOUT_MS)
    except Exception as e:  # noqa: BLE001  # 导航失败是这条的归因，不是整场的崩溃
        la.shot(page, ctx.shots_dir, f"deeplink-failed-{idx}")
        _save_deeplink_failure(ctx, idx, target_url, _page_url(page), cand, error=f"{type(e).__name__}: {e}")
        return _bail(out, "conv_unreachable", ctx, detail=f"深链导航失败 {type(e).__name__}: {str(e)[:100]}")
    time.sleep(random.uniform(2, 4))  # 原来点开卡片的等待量级：让聊天面板水合
    if la.is_logged_out(page, ctx.login_selector):
        ctx.stop = "relogin"
        return _bail(out, "login_expired", ctx)
    if not la._in_chat_form(page, cand.get("encryptJobId"), cand.get("jobTitle")):
        # 这一格也要现场：被带回非 chat 路径（含整页登录变体漏判时）只剩一条 URL 就没法复核
        la.shot(page, ctx.shots_dir, f"off-chat-{idx}")
        return _bail(out, "off_chat", ctx, detail=f"深链后不在聊天形态 URL={_page_url(page)[:120]}")
    state, header, hits = _wait_for_open_target(page, cand)
    if state != "matched":
        # 轮询这 5s 里才弹窗的形态：先补判一次登录，别把掉登录读成「开错会话」
        if la.is_logged_out(page, ctx.login_selector):
            ctx.stop = "relogin"
            return _bail(out, "login_expired", ctx)
        la.shot(page, ctx.shots_dir, f"deeplink-mismatch-{idx}")
        _save_deeplink_failure(ctx, idx, target_url, _page_url(page), cand, actual_header=header, header_hits=hits)
        why = {
            "empty": f"读不到右侧会话头（锚 0 命中——选择器腐烂或面板未出，URL id={_url_target_id(page)}）",
            "ambiguous": f"右侧会话头有 {hits} 个，不猜哪一个（读回 {header[:60]!r}）",
            "mismatch": (
                f"打开的不是这条会话（面板头={header[:60]!r} 期望={str(cand.get('company'))[:60]!r} "
                f"URL id={_url_target_id(page)}）"
            ),
        }[state]
        return _bail(out, "conv_unreachable", ctx, detail=why)
    actual_id = _url_target_id(page)
    allowed = {str(v) for v in (uid, cand.get("bossUid")) if v not in (None, "")}
    if actual_id is not None and actual_id not in allowed:
        la.shot(page, ctx.shots_dir, f"deeplink-mismatch-{idx}")
        _save_deeplink_failure(ctx, idx, target_url, _page_url(page), cand, actual_header=header, header_hits=hits)
        return _bail(
            out,
            "conv_unreachable",
            ctx,
            detail=f"URL 带着别人的 id={actual_id}（期望 {sorted(allowed)}）而会话头却对得上——矛盾，不发",
        )
    risk = la.detect_risk_signal(page)
    if risk:
        ctx.stop = "stop"
        if ctx.platform is not None:
            # runbook「边界」承诺熔断要存证，入站这一面也照做
            ctx.platform.save_evidence(page, f"inbound-risk-{idx}", cand)
        return _bail(out, "risk_blocked", ctx, detail=f"{risk}（深链打开会话后弹出）")
    return None


def reply_one(page, cand, idx, ctx):
    """一条对话的一次回复尝试（**只在 `--send` 下被调用**；dry-run 根本不进这个函数）。

    打开方式与守卫顺序见 `_open_conversation`（深链按 encryptUid，不再点卡片）；发送后的确认
    走 `confirm_sent_by_readback`（wapi 回读，不再读卡片末条）。点开前后的两次 wapi 读数都写进
    outcome（`wapiBefore` / `wapiAfter`），失败时另带 `wapiReadbackError`。
    """
    out = {
        "idx": idx,
        "encryptJobId": cand.get("encryptJobId"),
        "company": cand.get("company"),
        "jobTitle": cand.get("jobTitle"),
        "theirMessage": cand.get("lastMsg"),
    }
    bailed = _open_conversation(page, cand, idx, ctx, out)
    if bailed is not None:
        return bailed
    input_el = la.find_input_with_retry(page)  # 含 #42 加载态轮询与 #62 反馈浮层排除
    if input_el is None:
        return _bail(out, "no_input", ctx)
    write_json_atomic(Path(ctx.session_dir) / f"{REQUEST_PREFIX}{idx}.json", request_payload(idx, cand))
    print(f"    [队列] 已写 request_{idx}.json，等待 agent 起草回复…")
    text = la.wait_reply(str(ctx.session_dir), idx, timeout=ctx.cfg["reply_timeout"])
    if text is None:
        return _bail(out, "agent_timeout", ctx)
    if text == la.SKIP:
        # 原因要带出来：runbook 让「要附件/要微信」这类交用户处置，处置靠的正是这句话，
        # 只留一个计数等于把裁量所需的材料丢了（口径同 live_apply 的 reason_stats）
        out["skip_reason"] = la.reply_reason(str(ctx.session_dir), idx)
        return _bail(out, "agent_skip", ctx, detail=out["skip_reason"])
    draft = la.read_reply(str(ctx.session_dir), idx) or {}
    problems = gate_problems(text, draft.get("level"), draft.get("notes"), draft.get("state_evidence"), ctx.cfg)
    if problems:
        return _bail(out, "gate_blocked", ctx, detail="; ".join(problems[:3]))
    salu, qwarn = warn_lines(cand, text)
    out.update({"sendText": text, "level": draft.get("level"), "notes": draft.get("notes")})
    if salu:
        out["salutation_warns"] = salu
    if qwarn:
        out["question_warns"] = qwarn
    bailed = _type_and_trigger_send(page, input_el, text, idx, ctx, out)
    if bailed is not None:
        return bailed
    la.shot(page, ctx.shots_dir, f"after-send-{idx}")
    time.sleep(random.uniform(*WAPI_CONFIRM_SETTLE))  # 让发送请求先落库，再导航回列表回读
    # 点开前后的 wapi 读数都落 outcome（#73 验收）：失败的条目也要能被人照着两次读数复核，
    # 所以 before 无论成败都写，after 恒在（没命中就是 null，原因看 wapiReadbackError）。
    out["wapiBefore"] = _wapi_before(cand, ctx)
    out["wapiAfter"] = None
    conv, readback_error = confirm_sent_by_readback(page, cand, text, ctx)
    if conv is not None:
        out["wapiAfter"] = _wapi_after(conv, time.strftime("%Y-%m-%dT%H:%M:%S"))
    if readback_error or conv is None:
        out["typed"] = True
        out["wapiReadbackError"] = readback_error or "回读未命中"
        return _bail(out, "send_unconfirmed", ctx)
    out["status"] = "replied"
    out["sentAt"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    out["ledger"] = record_reply(
        ctx.ledger_path,
        {
            "encryptJobId": cand.get("encryptJobId"),
            "jobTitle": cand.get("jobTitle"),
            "company": cand.get("company"),
            "brandName": cand.get("brandName"),
            "bossName": cand.get("bossName"),
            "bossTitle": cand.get("bossTitle"),
            "theirMessage": cand.get("lastMsg"),
            "replyText": text,
            "level": draft.get("level"),
            "notes": draft.get("notes"),
            "sentAt": out["sentAt"],
            "sessionDir": str(ctx.session_dir),
        },
    )
    if ctx.pacer is not None:
        ctx.pacer.wait_sent()
    return out


def print_inbound_table(candidates):
    """候选清单（人读层）：逐条给「能不能回 / 为什么不能 / 对方那句原文」，并带 `encryptJobId`
    ——`--send --only <eid>` 要点名单条，这一行是它唯一的现场取号处（让人再去翻 report.json
    等于把门槛摆回人肉比对）。
    只报事实与判据，不替用户决定回不回——回不回由起草侧的 `{"skip": true}` 或当次授权表达。
    最后一列是水位线（#75）：`线上新` = 上次读数之后才出现或才动的、且末条不是我方的那条，
    「先看哪几条」看这一列，不看 `unreadMsgCount`（那个字段已被实测否证）。基准不可用时整列
    是 `线以下`，此时以 `[水位线]` 那行的 `state` 措辞为准（首跑/损坏/串档各有各的处置）。
    原文那格截到窗口但不静默截断：长于窗口行尾补 `…`，短于或等于不加——依据是 #66 票面验收①
    「汇报里恒有逐条公司·岗位·原文」与关票后的订正（这一格当时「不带任何截断标记」）。与
    `followup_tracker` 的 `[入站线索]` 清单**只共享判据**（`boss.read_state` 与 `STATE_LABELS`），
    窗口各算各的：本行还带取号用的 `encryptJobId` 与水位线列，所以是 52 而非那边的 46。
    `chat_first.py` 那句「措辞也必须同源」只管读态标签的**名字**，不涵盖截断标记。"""
    window = 52
    for cand in candidates:
        mark = "可回" if cand.get("eligible") else f"跳过：{cand.get('skip')}"
        read = STATE_LABELS.get(cand.get("readState"), "读态未知")
        line = "线上新" if cand.get("sinceWatermark") else "线以下"
        msg = cand.get("lastMsg") or ""
        print(
            f"  ▸ [{mark}] {cand.get('encryptJobId')} {cand.get('company')} | "
            f"{cand.get('jobTitle') or '（身份未反查）'} | {read} | {line} | "
            f"对方：{msg[:window]}"
            f"{'…' if len(msg) > window else ''}"
        )


def _send_round(page, candidates, ctx):
    """真发一轮：逐条 `reply_one`，直到候选跑完或收到停批信号。

    不再先回消息页：深链自己会把页面带到每条会话（#73），发送路径彻底不依赖列表页，
    所以也没有「回不到列表 ⇒ 整场不发」的分支了。登录态由 `reply_one` 在深链之后判
    （那时页面才真的在聊天页上，这一格才有信息量）。
    从 `_drive` 里分出来的理由不只是语句数上限：dry-run 与真发是两条动作面，读代码的人要能
    一眼看出「进这一段就开始有外部副作用」——这段里每一跳都是对招聘方可见的动作。
    静默时段那一格也算一条 outcome，好让它出现在 `skippedReasons` 里而不是只留一行日志。"""
    outcomes = []
    idx = 0
    for cand in candidates:
        if not cand.get("eligible"):
            continue
        if is_quiet_hour(current_hour(), ctx.cfg["quiet_hours"]):
            outcomes.append(_bail({"idx": idx + 1, "company": cand.get("company")}, "silent_hours", ctx))
            print("  [停] 进入静默时段，未回的那批下一场还在池子里")
            break
        idx += 1
        out = reply_one(page, cand, idx, ctx)
        outcomes.append(out)
        print(f"    [结果] #{idx} {out.get('status')}")
        if ctx.stop:
            print(f"  [停] {ctx.stop}（本条之后结束整场）")
            break
    return outcomes


def _drive(
    platform,
    page,
    cfg,
    applied,
    ledger_path,
    session_dir,
    shots_dir,
    send,
    only,
    started_at,
    watermark_path="",
    profile=None,
    mark_read=False,
):
    """浏览器阶段主干：只读快照 → 水位线比对 → 身份反查 → 判定 → （仅 `--send`）逐条深链回复。

    水位线（#75）：`watermark_path` 空（没接线）时按首跑落读数，两个键仍恒在。
    **只有 `mark_read=True` 这一支会写基准**——默认（含 dry-run）只读只报，绝不推进；
    推进的是**本次读到的整批**（不是候选清单那几条），`--mark-read` 也不点会话，它只写一个文件
    （`test_reply_inbound` 有源码钉用例守着这条）。
    """
    print(f"[会话目录] {session_dir}")
    if send:
        # 只给真发那一场留「模拟真人开工时间」：10-21 实测这一跳是 **593s**，而只读轮抖这 10 分钟
        # 不减少任何风控面（它不产生对外可见动作，页面加载本来就是复盘该做的事），换来的只是
        # 一场取数等十分钟。真发不同——那一刻起每个动作都对外可见，伪装才有意义。
        startup_jitter(cfg)
    snapshot = platform.followup(page, applied)  # 打开消息页拿会话列表：只读、零点击
    if not snapshot or not snapshot.get("conversations"):
        print("[停] 会话列表没拿到（登录态过期或采集残缺），本场不做任何判定")
        return None
    # 基准在这一次读**只读**拿：整个函数里唯一写它的分支在下面 `if mark_read:`，别无他途。
    # 两个恒在键由 `attach_readings` 就地落进快照（条目 B：三个消费者共用同一个函数，不各接一遍）。
    base, due = wm.attach_readings(snapshot, profile=profile, path=watermark_path)
    ledger = load_ledger(ledger_path)
    replied = replied_job_ids(ledger)
    waiting = sort_waiting(inbound_waiting(snapshot["conversations"]))
    if only:
        waiting = [c for c in waiting if c.get("encryptJobId") in set(only)]
    need = sorted(
        {
            c["encryptJobId"]
            for c in waiting
            if c.get("encryptJobId") and c.get("source") == "wapi" and c["encryptJobId"] not in replied
        }
    )
    print(f"[快照] 对话 {snapshot['totalConversations']} 条 | 入站等回话 {len(waiting)} 条 | 待反查岗位 {len(need)} 个")
    identities, stop = resolve_identities(page, need) if need else ({}, None)
    candidates = classify(snapshot, identities, replied, base=base, my_user_id=snapshot.get("myUserId"))
    if only:
        candidates = [c for c in candidates if c.get("encryptJobId") in set(only)]
    print_inbound_table(candidates)
    print(wm.headline(base, due))
    outcomes = []
    mode = "send" if send else "dry_run"
    ctx = _Ctx(
        cfg=cfg,
        session_dir=session_dir,
        shots_dir=shots_dir,
        ledger_path=ledger_path,
        login_selector=platform.login_dialog_selector,
        platform=platform,
        mode=mode,
        pacer=SessionPacer(cfg) if send else None,
        my_user_id=snapshot.get("myUserId"),
        snapshot_at=snapshot.get("generatedAt"),
    )
    eligible_candidates = [c for c in candidates if c.get("eligible")]
    if stop:
        print(f"[停] 身份反查阶段收到停批信号：{stop}")
    elif send and len(eligible_candidates) > 1:
        # `--only` 给的是一个 encryptJobId，但同一 eid 可能对应多条会话（同岗多招聘者）：
        # 此时按 id 也分不清该开哪一位，开哪条都是猜——一条都不发，落 `conv_ambiguous`。
        print(f"[停] --only 对应 {len(eligible_candidates)} 条可回候选（同一 eid 多会话），无法唯一确定，本场不发送")
        ctx.stop = "conv_ambiguous"
        # 发前就拒绝，没有任何对外动作，不走 `_bail` 的发送节奏等待（那会白等两轮 pacer）。
        outcomes = [
            {"idx": n, "company": c.get("company"), "status": "conv_ambiguous", "reason": "conv_ambiguous"}
            for n, c in enumerate(eligible_candidates, 1)
        ]
    elif send:
        outcomes = _send_round(page, candidates, ctx)
    else:
        for n, cand in enumerate((c for c in candidates if c.get("eligible")), 1):
            write_json_atomic(Path(session_dir) / f"{REQUEST_PREFIX}{n}.json", request_payload(n, cand))
        print("[dry-run] 未点开会话、未键入、未发送（点开会话会消耗未读，那是「该读哪几条」的唯一线索）")
    report = summarize(mode, snapshot, candidates, outcomes, ledger, started_at, base=base, due=due)
    report["stopSignal"] = ctx.stop
    report["sessionDir"] = str(session_dir)
    write_json_atomic(Path(session_dir) / "report.json", report)
    if mark_read:
        # 用户当面确认过「这次读到的这批我看过了」才走到这里；推进的是**本次读到的整批**的
        # 会话键集合与其中最大 lastTS（条目 F①：不是候选清单那几条——没列出的那些若不入基准，
        # 每场都会重复提醒）。拒绝推进时原文件不动（损坏/串档/零键/空路径都是如此）。
        res = wm.advance_watermark(
            watermark_path, snapshot["conversations"], base, profile=profile, snapshot_at=snapshot.get("generatedAt")
        )
        if res["written"]:
            previous = res["previousSnapshotAt"] or "（无历史基准）"
            # 三个数分开念（条目 F②）：旧句里的 N 是「线上新」的分臂数，最容易被念成候选清单条数，
            # 而真正进基准的是整批读数——读到几条 / 列出几条 / 记入几个键，各是各的意思。
            read_count = snapshot.get("totalConversations") or len(snapshot["conversations"])
            above = report["newSinceWatermark"]
            above_txt = f"较上次读数新增 {above} 条" if above is not None else "上次没有可比基准，本场只建立"
            print(
                f"水位线已从 {previous} 推进到 {res['snapshotAt']}："
                f"本次读到 {read_count} 条会话、候选清单列出 {report['candidates']} 条、"
                f"记入 {res['keys']} 个会话键（{above_txt}）。"
                "推进的是本次读到的整批——列出的、以及因末条是我方或已归到投递而没列出的，一并算看过。"
            )
        else:
            print(f"[水位线] 未推进——{res['reason']}")
    return report


def run_reply(env=None, send=False, page=None, only=(), mark_read=False):
    """入口。默认 dry-run；`send=True` 才会深链打开会话、才会发送（发送是外部动作，逐次授权）。
    `page` 注入用于测试与「复用已开窗口」，不注时自己起 Camoufox（与 followup_tracker 同款参数）。
    `mark_read=True`（CLI `--mark-read`）＝**用户当面说过「这次读到的这批我看过了」**：跑的还是
    那一场只读，只是末尾把基准写下去（推进的是本次读到的整批，见 `_drive` 里那一段）。它与
    `--send`／`--only` 互斥，理由见下面两道拒绝处的注释。
    行缓冲必须在第一条 print 之前打开（#73）：真发那场把输出重定向到文件时，块缓冲会让
    agent 全程看不到 `[队列]` / `[结果]` 这些实时信号。"""
    la._enable_line_buffering()
    env = env if env is not None else os.environ
    name, prefix = resolve_active_profile(env=env)
    print(profile_line(name, prefix))
    if send and len(only) != 1:
        # 用户的规则（2026-10-21 拍板）：脚本永不替他做「这条要不要读」的决定——只有他**已经读过**
        # 的那条才允许发。而 Boss 的未读字段已被实测否证（三次快照恒 0，用户确认有未读时也是 0），
        # 机器判不出来"读过没"，所以凭据只能是他点名单条这件事本身。裸 `--send` 一次会打开十几条
        # 会话、把未读全烧掉；`--only` 给两个也一样（一次只发一条、发一条验一条），所以不是
        # "多写一层校验"的问题，是要在启动前就拒绝。
        print(
            "[blocked] --send 必须配且只配一个 --only <encryptJobId>——一次只开一条、发一条验一条。"
            "先跑一次不带 --send 的只读模式拿候选清单（零点击），再从里面挑一条点名。"
        )
        return None
    if mark_read and send:
        # 两个方向相反的动作不该同时表达「我看过了」与「我要发」：真发那一场会点开会话（消耗未读），
        # 而推进水位线要的是「用户看过、脚本一列都没动」的那场读数。分开跑，各自都有明确读数。
        print("[blocked] --mark-read 与 --send 互斥——先只读跑出清单、确认看过并 --mark-read，再单独点名真发。")
        return None
    if mark_read and only:
        # 推进的是**本次读到的整批**（票面第 1 条）。带 `--only` 时清单被裁成一个子集，
        # 拿子集去推整条水位线＝把没列出的那些也替他记成「看过」，正是本票要防的偷换。
        print("[blocked] --mark-read 不能配 --only——推进的必须是本次读到的整批，点名过滤后列出的只是子集。")
        return None
    history_path = Path(private_path("data/applied_history.json", prefix))
    if not history_path.exists():
        print(
            f"[blocked] 缺 {history_path}——「入站」这条判据本身就是「归不到我们投出去的岗」，"
            "没有投递台账整批对话都会被当成人家主动来撩。先跑一次投递或按 onboarding 建 Profile。"
        )
        return None
    cfg = load_config(env)
    cfg["reply_timeout"] = int(env.get("LIVE_REPLY_TIMEOUT", str(la.REPLY_TIMEOUT_DEFAULT)) or la.REPLY_TIMEOUT_DEFAULT)
    cfg["fact_cards_path"] = str(private_path("kb/fact_cards.md", prefix))
    sections = la.profile_sections(private_path("profile_background.md", prefix))
    if sections is None:
        print("[warn] profile_background 不可用——备注 §存在性核验跳过，备注非空仍强制")
    cfg["profile_sections"] = sections
    if is_quiet_hour(current_hour(), cfg["quiet_hours"]):
        print(f"[停] 静默时段 {cfg['quiet_hours'][0]}-{cfg['quiet_hours'][1]} 点，不启浏览器")
        return None
    applied = json.loads(history_path.read_text(encoding="utf-8")).get("applied", [])
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    ts = time.strftime("%Y%m%d-%H%M%S")
    session_dir = Path(private_path(f"{SESSION_DIR_FILE}/{ts}", prefix))
    shots_dir = session_dir / "shots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = str(private_path(LEDGER_FILE, prefix))
    # 水位线基准按 profile 隔离（#28 扁平命名口径：默认档不带后缀、其余 `<name>.` 前缀）——
    # 两个账号各有各的「看过了」，串档会让 A 的读数替 B 说「新增 0」。
    watermark_path = str(private_path(wm.WATERMARK_FILE, prefix))
    platform = get_platform()
    if page is not None:
        return _drive(
            platform,
            page,
            cfg,
            applied,
            ledger_path,
            session_dir,
            shots_dir,
            send,
            only,
            started_at,
            watermark_path=watermark_path,
            profile=name,
            mark_read=mark_read,
        )
    with Camoufox(
        humanize=True,
        geoip=True,
        os="macos",
        headless=False,
        persistent_context=True,
        user_data_dir=browser_profile_dir(name),
    ) as context:
        return _drive(
            platform,
            context.new_page(),
            cfg,
            applied,
            ledger_path,
            session_dir,
            shots_dir,
            send,
            only,
            started_at,
            watermark_path=watermark_path,
            profile=name,
            mark_read=mark_read,
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description="回复入站对话（#66 阶段 2）。默认 dry-run，不点会话不发送。")
    parser.add_argument("--send", action="store_true", help="真发（外部动作，需当次授权；必须配且只配一个 --only）")
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="ENCRYPT_JOB_ID",
        help="点名单条（dry-run 可重复过滤；`--send` 时必填且只能一个）——脚本不替你决定这条读没读",
    )
    parser.add_argument(
        "--mark-read",
        dest="mark_read",
        action="store_true",
        help="把本次读到的整批记为「用户已确认看过」，推进水位线（#75；只写文件、不点任何会话）。"
        "默认不推进——跑一次推一次等于替用户决定他没看过的东西",
    )
    args = parser.parse_args(argv)
    if args.mark_read and (args.send or args.only):
        # 这两道互斥在 run_reply 里也拦（那里拦的是编程调用），CLI 层提前拒绝好让 `--help`
        # 的读者一眼看见：推进的必须是本次读到的整批。
        print("[blocked] --mark-read 与 --send / --only 互斥：只读跑法不带发送、不带点名过滤。")
        return 1
    report = run_reply(send=args.send, only=tuple(args.only), mark_read=args.mark_read)
    if report is None:
        return 1
    print(
        f"[done] mode={report['mode']} 候选 {report['candidates']} | 可回 {report['eligible']} | "
        f"已回 {report['replied']} | 今日累计 {report['repliedToday']} | "
        f"水位线以上 {report['watermark']['state']} / 新增 {report['newSinceWatermark']} 条"
    )
    nonzero = {k: v for k, v in report["skippedReasons"].items() if v}
    if nonzero:
        print(f"[跳过] {nonzero}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
