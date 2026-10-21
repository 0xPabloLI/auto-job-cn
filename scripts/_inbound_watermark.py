#!/usr/bin/env python3
"""入站水位线（#75）——「较上次读数新增哪几条」的单一真源。

**要解决什么**：用户需要知道「哪些对话是我没看过的」，好决定先看哪几条。Boss 自己那个未读数
（`unreadMsgCount`）已被实测否证——它在多场快照里恒 0，而其中有场次用户确认 App 上**有**未读会话。
**场次、条数与全部证据见 `内部调研笔记目录（未随本仓分发）boss-chat-card-shape.md` §7.3，本模块不自己数场次**
（数过的场次写进注释，下一次实测就会把它变成一句假话）。本模块**不拿那个字段当判据**，
也不给它打补丁。

**硬规矩（用户拍板）**：脚本永不替他决定「这条要不要读」——打开会话就会把它标成已读，而未读是
他唯一的自然线索。所以这里的全部判据都来自**只读**的会话列表接口（`getGeekFriendList`）：
键集合差 + 时间兜底，一次会话都不打开。

**推进时机（本模块最重要的一条纪律）**：默认**绝不自动推进**基准。消费者每次只读跑完只**报**
「较上次基准新增 N 条」；基准只在用户当面说过「这次读到的这批我看过了」之后，由
`reply_inbound.py --mark-read` 显式推进。推进的是**本次读到的整批**（列出来的、以及因末条是我方
或已归到投递记录而没被列出来的，都算看过）——只推列出的那几条，其余每场都会被重复提醒。
脚本每跑一次就推进，等于把「用户没看过的」偷换成「脚本没见过的」——下一场报「新增 0」而用户
一条没看。因此 `advance_watermark` 是本模块唯一的写入口，且只允许被 `--mark-read` 分支调用
（`test_reply_inbound.py` 里有源码钉用例守着，防后续 session 顺手在只读路径里推进）。

**读数形态**（恒在键，0 也要写出来，口径同 #62 `no_send_reasons` / #65 `recommend_*`）：
- `state="ok"`：有可用基准 ⇒ `newSinceWatermark` 是整数 N；
- `state="first_run"`：还没有基准文件 ⇒ N 是 `None`，**不把全部条数当新增**（那等于谎报）；
- `state="corrupt"`：文件读不出 ⇒ N 是 `None`，原文件**保留不覆盖**（同 #64「损坏文件原样保留」），
  重建要人先把它移走——机器不替人擦掉读不懂的现场；
- `state="profile_mismatch"`：文件里记的是别的 profile 的基准 ⇒ N 是 `None`，不串档；
- `state="no_keys"`：文件在、但里面一个会话键都没有 ⇒ 与没有基准**等价**，N 是 `None`。
  「基准存在但内容为空」最容易伪装成可用读数，所以它单占一格、措辞直说「基准未推进」。

**一条会话的键是它的全部可用键（#75 review 条目 I）**：拍板原文是 `encryptJobId` ∪ `encryptUid`，
所以两个都算这条会话的键。`platforms/chat_first.py` 的 `_conv_record` 会给归到投递记录的会话**回填**
`encryptJobId`（`eid or matched["encryptJobId"]`）——只认单键（eid 优先）时，同一条 HR 会话会在回填
那一刻从 uid 键换成 eid 键，下一场被念成「新会话」重复提醒。判「新会话」因此用**任一已知键**；
写基准时两类键都记。

**零键读数不配当基准（#75 review 条目 A）**：本次读数如果一个可用会话键都拿不到（DOM 降级卡片
全无加密 id），`advance_watermark` **拒绝写盘**——写下去就是上面那格「存在但比不出」的 0 键基准，
下一场会把全部条数念成「新增 N 条」，正是票面禁止的谎报换了门进来。拒绝时原文件一字节不动。
"""

import json
import time
from pathlib import Path
from typing import Any

from _atomic_io import write_json_atomic

WATERMARK_FILE = "data/inbound_watermark.json"  # 扁平命名口径由 _profile_paths.private_path 决定（#28）
VERSION = 1

STATE_OK = "ok"
STATE_FIRST_RUN = "first_run"
STATE_CORRUPT = "corrupt"
STATE_PROFILE_MISMATCH = "profile_mismatch"
STATE_NO_KEYS = "no_keys"

# 基准**可用**的只有一格（`ok`）；其余四格读数为 N=None，但措辞各不相同——把「首跑」「损坏」「串档」
# 「0 键」混成一个「无数据」会让人以为脚本坏了，而它们的处置方向完全不同（等一场 / 移文件 /
# 换 profile / 重跑一场能拿到会话键的读数再 --mark-read）。
# `no_keys` 可以就地重建：它不是排查材料（一个键都没记），而 `corrupt`/`profile_mismatch` 是。
WRITABLE_STATES = (STATE_OK, STATE_FIRST_RUN, STATE_NO_KEYS)


def is_usable(base: dict) -> bool:
    """基准可用（真的有东西可比）吗——只有 `ok` 那一格能产出条数读数。"""
    return base.get("state") == STATE_OK


def session_keys(conv: dict) -> tuple:
    """一条会话的**全部**可用键 = `encryptJobId` ∪ `encryptUid`（票面口径），jobId 在前。

    为什么不是单键：`platforms/chat_first.py` 的 `_conv_record` 会给归到投递记录的会话回填
    `encryptJobId`，同一条对话可能这场只有 uid 键、下场两个都有——只取一个键的话它会在
    回填那一刻「换键」，下一场被念成没见过的会话，重复提醒同一条（review 条目 I）。
    空元组 ⇒ DOM 降级卡片两样加密 id 都没有（#30 探测结论）：这类条目**不静默丢**，调用方
    按 `unkeyed` 单独计数并念出来；它也是 `advance_watermark` 零键拒写的判据（条目 A）。
    取 str 是为了比较不受 int/str 漂移影响（wapi 的 `uid` 是整数、`encryptJobId` 是串）。"""
    out = []
    for field in ("encryptJobId", "encryptUid"):
        value = conv.get(field)
        if value in (None, ""):
            continue
        key = str(value)
        if key not in out:
            out.append(key)
    return tuple(out)


def session_key(conv: dict) -> Any:
    """这条会话的**主键**（`session_keys` 的第一个，无键 ⇒ None）。

    判据侧一律用 `session_keys`（∪ 语义）；本函数只给「要一个代表键」的调用方（日志、
    台账定位）用，别拿它判「见过没见过」——那正是条目 I 那条假阳性的来源。"""
    keys = session_keys(conv)
    return keys[0] if keys else None


def last_message_is_ours(conv: dict, my_user_id: Any = None) -> bool:
    """末条是不是我方发的（**排除**判据：自己刚发出去的那条不该算「该读」）。

    **取 boss 的结论，取不到才自己比一次**（#75 review 条目 C）。这个仓里「末条归谁」早就有结论了：
    `platforms/chat_first.parse_friend_entries` 按 `lastMessageInfo.fromId == 我方 userId` 落成
    `status`（`awaiting` ⇔ 末条我方，`hr_replied` ⇔ 末条对方；`inbound_waiting` 消费的就是它）。
    水位线再拿 `fromId` 比一遍就是第二份判据——那边一改（`lastMessageInfo` 整体缺失时保守归
    `awaiting` 这类），这边静默漂。所以：
    1. 会话行自带 `status` ⇒ 直接取它（`followup_report` 的会话行走这支）；
    2. `status` 缺失（`reply_inbound.classify` 造的候选 dict 是呈报形状，没有结论键）⇒ 自己比一次
       `lastFromId == my_user_id`（比 str，防 int/str 漂移）；
    3. 两样都没有 ⇒ 判不出，**按「不是我方」放行**（宁可多列一条给人看，也不静默丢——被丢掉的
       那条恰恰可能是真该读的）。"""
    status = conv.get("status")
    if status in ("awaiting", "hr_replied"):
        return status == "awaiting"
    if my_user_id not in (None, "") and conv.get("lastFromId") not in (None, ""):
        return str(conv.get("lastFromId")) == str(my_user_id)
    return False


def _as_int_ts(value: Any) -> Any:
    """毫秒时间戳只在**真的是 int** 时参与比较：`True` 也满足 `isinstance(_, int)`，显式排除；
    None / 串（DOM 降级）一律 None，不参与「时间兜底」那一臂。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def empty_base(profile: Any = None, path: Any = "") -> dict:
    """一个「什么都没有」的基准（首跑形态），也是坏形态读数的兜底形状——键齐全，调用方不必防。"""
    return {
        "state": STATE_FIRST_RUN,
        "snapshotAt": None,
        "keys": frozenset(),
        "maxLastTS": None,
        "profile": profile_label(profile),
        "path": str(path),
        "storedProfile": None,
        "detail": "",
    }


def profile_label(profile: Any = None) -> str:
    """基准文件里 `profile` 字段的写法：注册表名字，未注册（legacy）记 "default"。"""
    return str(profile) if profile else "default"


def read_watermark(path: Any, profile: Any = None) -> dict:
    """基准文件 → 基准对象（恒返回，不抛）。四种形态见模块头「读数形态」。

    只读：**这个函数一个字都不写盘**。读不懂就原样保留（#64 口径），将错就错的修复比坏掉更糟。"""
    base = empty_base(profile, path)
    p = Path(path)
    if not p.exists():
        return base
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        return {**base, "state": STATE_CORRUPT, "detail": f"{type(e).__name__}: {str(e)[:120]}"}
    bad = _shape_problem(data)
    if bad:
        return {**base, "state": STATE_CORRUPT, "detail": bad}
    stored = str(data["profile"])
    base = {**base, "snapshotAt": str(data["snapshotAt"]), "keys": frozenset(str(k) for k in data["keys"])}
    # `.get` 而不是 `[...]`：形态核验说这键可缺（`None` 是合法值），取的时候又按必存在取，
    # 就会让「恒返回、不抛」的读侧在一个少键文件上抛 KeyError（只读消费者没有 try 兜着）。
    max_ts = data.get("maxLastTS")
    base["maxLastTS"] = _as_int_ts(max_ts) if max_ts is not None else None
    base["storedProfile"] = stored
    if stored != base["profile"]:
        return {**base, "state": STATE_PROFILE_MISMATCH}
    if not base["keys"]:
        # 文件在、键一个都没有 ⇒ 与没有基准等价（见模块头「零键读数不配当基准」），不许声称 ok
        return {**base, "state": STATE_NO_KEYS}
    return {**base, "state": STATE_OK}


def _shape_problem(data: Any) -> Any:
    """基准形态核验 → 问题描述（None = 可读）。逐条给话术：只说「损坏」等于让人无从下手。"""
    if not isinstance(data, dict):
        return "顶层不是对象"
    if data.get("version") != VERSION:
        return f"version 不是 {VERSION}（得到 {data.get('version')!r}）"
    if not isinstance(data.get("snapshotAt"), str) or not data["snapshotAt"]:
        return "snapshotAt 缺失或不是非空字符串"
    if not isinstance(data.get("profile"), str) or not data["profile"]:
        return "profile 缺失或不是非空字符串"
    keys = data.get("keys")
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        return "keys 不是字符串数组"
    if "maxLastTS" not in data:
        # 键本身必须在（值可以是 null）：核验放行、读取时再按必存在取，等于给读侧留一次 KeyError
        return "maxLastTS 键缺失（null 是合法值，缺键不是）"
    max_ts = data.get("maxLastTS")
    if max_ts is not None and _as_int_ts(max_ts) is None:
        return "maxLastTS 不是整数或 null"
    return None


def due_reason(conv: dict, base: dict, my_user_id: Any = None) -> Any:
    """这条在水位线之上吗 → `"new"`（新会话）/ `"updated"`（老会话有新消息）/ None（不在线上或判不出）。

    两臂（票面口径「会话键集合 + 时间兜底」）：
    - **`session_keys` 一个都不在上次 `keys` 里 ⇒ `new`**：这条上次读数根本没见过。
      反过来，**任一**已知键在基准里就算见过（条目 I：boss 会给归到投递的会话回填
      `encryptJobId`，只认单键的话这条对话会在回填那一刻换键、被重复念成新会话）。
    - **键已知但 `lastTS` > 基准的 `maxLastTS` ⇒ `updated`**：老会话在基准之后又动了。
      用**严格大于**，所以同毫秒不会误报成新消息；而同一毫秒里**新出现**的会话由第一臂接住——
      这正是选「键集合 + 时间兜底」而不是纯 `lastTS` 的原因。
    两条附加约束：末条是我方的**不算**（`last_message_is_ours`）；没有可用会话键的**不算**
    （记不进基准，算进 N 就永远降不下来，改为单独计数呈报）。
    基准不可用（`state != ok`，含 0 键那格）时一律 None：宁可不报，也不拿「全部条数」冒充「新增条数」。"""
    if not is_usable(base):
        return None
    keys = session_keys(conv)
    if not keys:
        return None
    if last_message_is_ours(conv, my_user_id):
        return None
    known = base.get("keys") or frozenset()
    if not any(k in known for k in keys):
        return "new"
    max_ts = base.get("maxLastTS")
    ts = _as_int_ts(conv.get("lastTS"))
    if isinstance(max_ts, int) and ts is not None and ts > max_ts:
        return "updated"
    return None


def split_due(convs: Any, base: dict, my_user_id: Any = None) -> dict:
    """一场读数 → 水位线以上的清单与计数。`newCount` 在基准不可用时是 `None`（不是 0、不是全量）。

    `dueKeys` 收的是那批会话的**全部**可用键（与 `advance_watermark` 写盘的口径一致），
    所以「这条在线上」与「这条记进基准」不会是两套说法。"""
    due_keys: set = set()
    new_convs = 0
    updated_convs = 0
    unkeyed = 0
    for conv in convs or []:
        if not isinstance(conv, dict):
            continue
        keys = session_keys(conv)
        if not keys:
            unkeyed += 1
        reason = due_reason(conv, base, my_user_id)
        if reason is None:
            continue
        if reason == "new":
            new_convs += 1
        else:
            updated_convs += 1
        due_keys.update(keys)
    usable = is_usable(base)
    return {
        "usable": usable,
        "newCount": (new_convs + updated_convs) if usable else None,
        "dueKeys": frozenset(due_keys),
        "newConversations": new_convs if usable else None,
        "updatedConversations": updated_convs if usable else None,
        "unkeyed": unkeyed,
    }


def above_line(conv: dict, base: dict, my_user_id: Any = None) -> bool:
    """这条在水位线之上吗（布尔出口，供候选清单逐列打勾）。判据全在 `due_reason`，这里不重写。"""
    return due_reason(conv, base, my_user_id) is not None


def watermark_reading(base: dict, due: dict) -> dict:
    """落进报告的 `watermark` 对象（**必须 JSON-safe**：基准里的 `keys` 在这里折算成数量，
    frozenset 不进报告——它是判据的工作形态，不是读数）。"""
    return {
        "snapshotAt": base.get("snapshotAt"),
        "keys": len(base.get("keys") or ()),
        "state": base.get("state"),
        "maxLastTS": base.get("maxLastTS"),
        "profile": base.get("profile"),
        "unkeyed": due.get("unkeyed"),
        "newConversations": due.get("newConversations"),
        "updatedConversations": due.get("updatedConversations"),
        "detail": base.get("detail") or "",
    }


def attach_readings(report: dict, *, profile: Any = None, path: Any = "") -> tuple:
    """一场只读读数 → **就地**落两个恒在键，返回 `(base, due)` 供打印那句 headline。

    「读基准 + `split_due` + 落两键」收成这一个函数，是 #75 review 条目 B 的修法：三个消费者
    （`followup_tracker.run` / `reply_inbound._drive` / `live_apply._collect_fresh_followup`）
    各接一遍时，后写的调用方会漏——`live_apply` 那条整份重写同一个 `followup_report.json`，
    键不在它那儿，而投递场次收尾正是用户读这份报告的路径。判据、形态、措辞仍全在本模块。

    **只读**：本函数一个字都不写盘（推进只由 `--mark-read` 走 `advance_watermark`）。
    `path` 为空＝调用方没接线（老的用例形状、单测注入）⇒ 按首跑落键，两个键照样恒在。"""
    base = read_watermark(path, profile) if path else empty_base(profile, path)
    due = split_due(report.get("conversations"), base, report.get("myUserId"))
    report["newSinceWatermark"] = due["newCount"]
    report["watermark"] = watermark_reading(base, due)
    return base, due


def watermark_phrase(base: dict) -> str:
    """那句「水位线 <时刻>」的念法（两份报告共用，措辞同源——两处各自改词会让同一个人在
    两个读数里看到两个名字）。不可用的四格各给各自的处置方向，不混成「无数据」。"""
    state = base.get("state")
    if state == STATE_OK:
        return f"水位线 {base.get('snapshotAt')}（{len(base.get('keys') or ())} 键）"
    if state == STATE_CORRUPT:
        return f"基准文件读不出（{base.get('detail')}）——原样保留不覆盖，要重建请先人工移走 {base.get('path')}"
    if state == STATE_PROFILE_MISMATCH:
        return f"基准是 {base.get('storedProfile')!r} 的（本档 {base.get('profile')}），不串档"
    if state == STATE_NO_KEYS:
        return (
            f"基准未推进：文件里无可记录的会话键（{base.get('snapshotAt')} 那次写的，0 键）"
            f"≡ 没有基准，本场不比对；重跑一场拿到会话键后 --mark-read 即可重建 {base.get('path')}"
        )
    return "无历史基准（首跑，本场只建立、不比对）"


def headline(base: dict, due: dict) -> str:
    """验收①的那句话：`较上次读数新增 N 条（水位线 <时刻>）`。基准不可用时如实说「无从比较」，
    绝不把全部条数报成新增。"""
    count = due.get("newCount")
    phrase = watermark_phrase(base)
    if count is None:
        return f"[水位线] 较上次读数无从比较——{phrase}"
    parts = [f"[水位线] 较上次读数新增 {count} 条（{phrase}"]
    breakdown = f"新会话 {due.get('newConversations')} / 老会话有新消息 {due.get('updatedConversations')}"
    if due.get("newConversations") or due.get("updatedConversations"):
        parts.append(f"，其中 {breakdown}）")
    else:
        parts.append("）")
    if due.get("unkeyed"):
        parts.append(f"；另有 {due.get('unkeyed')} 条没有可用会话键、记不进基准（不静默丢）")
    return "".join(parts)


def advance_watermark(path: Any, convs: Any, base: dict, profile: Any = None, snapshot_at: Any = None) -> dict:
    """把这次读到的整批写成新基准 → `{"written", "reason", "snapshotAt", "previousSnapshotAt", "keys"}`。

    **唯一写入口，只允许被 `--mark-read` 分支调用**（语义＝用户当面确认「这次读到的这批我看过了」）。
    只读路径一次都不许碰它：见模块头的推进时机纪律，`test_reply_inbound.py` 有源码钉用例守着。

    五种拒绝（都**不动**已有文件）：
    - `corrupt`：读不懂的现场要原样保留（#64 口径），覆盖就等于把排查材料擦了；
    - `profile_mismatch`：那是别的 profile 的基准，写下去就是串档；
    - 本场一条会话都没读到：写下去等于把基准清空，清空后下一场「新增 0」是假读数；
    - 本场读到了会话但**一个可用键都没有**（DOM 降级卡片全无加密 id）：写下去是一份「存在但
      比不出」的 0 键基准，下一场会把全部条数念成「新增 N 条」——票面禁止形态换了门进来（条目 A）。
    - `path` 是空的：`Path("")` 是 `.`，`write_json_atomic` 的 `mkdir(".")` 照样通过、tmp 名成
      `..tmp`、`os.replace("..tmp", ".")` 才炸——CWD 留下孤儿临时文件（条目 E）。空路径的语义是
      「调用方没接线」，那就直接拒绝，不许它退化成一个写入目标。
    写入的是**本次读数**的全部可用键（`encryptJobId` ∪ `encryptUid`，条目 I）与其中最大
    `lastTS`（票面第 1 条：推进的正是这次读到的这批）；没有任何 int 时间戳时对应字段留 None，
    由读数侧的 `unkeyed` 说明代价。"""
    result = {
        "written": False,
        "reason": None,
        "snapshotAt": None,
        "previousSnapshotAt": base.get("snapshotAt"),
        "keys": 0,
    }
    if not str(path).strip():
        result["reason"] = (
            "水位线基准路径是空的——空路径的意思是「调用方没接线」，不是「写到当前目录」："
            "空串经 Path 就是 `.`，落盘会先在 CWD 留一个孤儿 `..tmp` 再失败。拒绝，本场读数照旧可信"
        )
        return result
    state = base.get("state")
    if state not in WRITABLE_STATES:
        result["reason"] = watermark_phrase(base)
        return result
    snapshot = [c for c in (convs or []) if isinstance(c, dict)]
    if not snapshot:
        result["reason"] = "本场一条会话都没读到——写下去等于把基准清空（清空后下一场的「新增 0」是假读数），拒绝"
        return result
    keys = sorted({k for c in snapshot for k in session_keys(c)})
    if not keys:
        result["reason"] = (
            f"基准未推进：本次读数无可记录的会话键（{len(snapshot)} 条都没有加密 id，DOM 降级形态）——"
            "写下去就是一份「存在但比不出」的 0 键基准，下一场会把全部条数念成新增，正是票面禁止的谎报；"
            "原文件一字节不动"
        )
        return result
    stamps = [ts for ts in (_as_int_ts(c.get("lastTS")) for c in snapshot) if ts is not None]
    at = str(snapshot_at or base.get("snapshotAt") or "") or time.strftime("%Y-%m-%d %H:%M:%S")
    payload = {
        "version": VERSION,
        "profile": profile_label(profile if profile is not None else base.get("profile")),
        "snapshotAt": at,
        "maxLastTS": max(stamps) if stamps else None,
        "keys": keys,
    }
    try:
        write_json_atomic(Path(path), payload)
    except OSError as e:
        result["reason"] = f"基准写入失败（{str(e)[:80]}）——水位线仍是 {base.get('snapshotAt')}，本场读数照旧可信"
        return result
    return {**result, "written": True, "snapshotAt": at, "keys": len(keys)}
