#!/usr/bin/env python3
"""会话清单签名（#63 切片 B）——「上一份有、本场没有」的那几条**掉在列表哪个位置**的单一判据。

**要解决什么**：`followup` 采集的会话条数从历史的 98/100 掉到 2026-10-09 三场的 76，EDD 评估样本
从 59 掉到 52。原票面给的修法是「跌幅超阈值就不覆盖」，用户已**明确否决**，改按位置签名分流。
三条实物结论（证据与场次在 `内部调研笔记目录（未随本仓分发）`，本模块不自己复述读数，只把判据落进代码）：

1. 我们监听的 `getGeekFriendList.json` **不分页**：请求体只有 `friendIds`，响应 `zpData` 只有
   `result`，既无 `hasMore` 也无游标 ⇒ 条数天花板来自前端下发了多少 id，采集侧**做不到**「翻到底」。
   所以「少了几条」不是我们少翻了页，判据要问的是**少的这几条掉在哪**。
2. `followup_report.json` 是**全量覆盖写**（两个 writer 都覆盖，只有 `annotations` 是 merge）
   ⇒ 采集少多少，报告就少多少，样本下降可以直接归因给列表条数下降。
3. 站点侧真的会**移除会话**：09-24 的原始快照 84 条里，到 10-21 有 63 条已从列表消失；两份列表都
   **严格按新→旧排序**，而消失项的下标从第 9 位一直散到第 83 位（有 12 条保留项排在首个消失项之后）
   ⇒ 形态是**散落**，不是「尾部连续一段」。

**为什么要签名而不是比例**：`#59` 那条 `n*2 < prev` 只看得见「少了一半」，看不见「怎么少的」。
首页截断（#59 的 3/100）与站点侧真实流失（10-21 那 63/84）在比例上一模一样，处置方向却相反：
前者是**我们**读数残缺，该沿用上一份；后者是**对方真的把会话移走了**，报告本来就该跟着变短——
把它判成残缺、不覆盖，等于把报告永久冻在一份旧名单上，新回复永远进不了 eval。
这条担忧的**全文现在住在这里**（#59 当初否决过草拟的 `max(0.5×prev, prev−10)`：它在 prev 大时
等价于「几乎不许变少」，100→89 就拒，会把会话自然过期造成的真实衰减一路判成残缺、从此长期停在
旧报告上，换一类失真；真实增减是 ±20% 级的渐进，所以当时把线画在「一半」。#63 保留了这条腿，
只是不再让它单独说话）——`live_apply` 与用例里的旧指针都指回这一处，别再往调用方抄第二遍。
于是本模块的判据是**两条腿同时成立才否决**（`decide`），严格窄于现行判据。

**读数形态**（恒在键，0 也要写出来，口径同 #62 `no_send_reasons` / #65 `recommend_*` /
#63 切片 A 的 `followupCollection` / #75 水位线）：调用方不必防缺键，取不到就是 `None`。

- `shape`：闭集四态（`SHAPES`）。`unknown` = 上一份没有可比条目（文件不存在、读不出、或一个可用键
  都没有）⇒ **首跑不冒充**（语义同 #75 的 `first_run`：宁可不比对，也不把「没有基准」念成「一条没掉」
  或「全掉了」）。
- `prevCount` / `newCount` / `missing`（上一份有、本场没有的条数）/ `appeared`（本场新增）/
  `unkeyedPrev` / `unkeyedNew`（没有可用会话键的条目数）。
- `missingFrom`（首个缺失项在上一份里的下标，无缺失 ⇒ `None`）/ `keptAfterFirstMissing`
  （排在它之后仍被保留的条数）——散落与截断的区分就在这两格里，人一眼能复核。

**判同一条 = 任一可用键相同**（复用 `_inbound_watermark.session_keys`，本模块不自己写键规则）：
上一份报告里的条目可能只有 `encryptJobId`，本场同一条两个键都有；只比单键会把**同一条会话**念成
「消失又出现」，从而把流失签名做成一条假的增长（#75 review 条目 I 同一个坑）。

**没有可用键的条目**（DOM 降级卡片正是这个形态）不参与匹配、也**不计入** `missing`——没有依据说它
消失了；只进 `unkeyed*`，由 `phrase` 念出来，不静默丢（口径同 #75 的零键拒写）。
非 dict 条目（真实库里混过 `null`）与零键条目同走这条路：只计数，绝不对它调 `.get`。
"""

from typing import Any

from _inbound_watermark import session_keys

SHAPE_STABLE = "stable"
SHAPE_TAIL_TRUNCATED = "tail_truncated"
SHAPE_SCATTERED_LOST = "scattered_lost"
SHAPE_UNKNOWN = "unknown"
SHAPES = (SHAPE_STABLE, SHAPE_TAIL_TRUNCATED, SHAPE_SCATTERED_LOST, SHAPE_UNKNOWN)

# 签名读数的键集合（恒在）。写成常量有两个落点：契约用例按它断言「键集合恒等」（改键名必须让
# 用例红，别让下游静默读空气），以及 `followup_tracker` 否决那条支路的机读事件按它整包落盘
# （#48：这类事件必须进 JSONL，且 level 要 error——默认阈值是 ERROR，warning 会被静默吞掉）。
# 落盘形态的名字归 LEDGER_KEYS，采集形态的名字归 boss 的 COLLECTION_KEYS。
READING_KEYS = (
    "shape",
    "prevCount",
    "newCount",
    "missing",
    "appeared",
    "unkeyedPrev",
    "unkeyedNew",
    "missingFrom",
    "keptAfterFirstMissing",
)

# 收尾台账的键集合（`report.json` 的 `followupLedger` 与 `followup_report.json` 里同源那一块）。
# `followupCollection` 是**切片 A 那个读数原样嵌入**，不复制字段——读数要有单一出处。
LEDGER_KEYS = ("collected", "previous", "missing", "appeared", "shape", "followupCollection")


def _keys_of(entry: Any) -> tuple:
    """条目的可用会话键；非 dict 条目（库里混过的 `null` 等）没有键，也不许对它 `.get`。"""
    return session_keys(entry) if isinstance(entry, dict) else ()


def _as_list(convs: Any) -> list:
    """会话列表形态核验：不是列表就当没有（`None`、dict、字符串都不猜）。"""
    return convs if isinstance(convs, list) else []


def report_conversations(report: Any) -> list:
    """报告 → 会话列表（按 `boss.build_report` 的形状读；读不出 ⇒ 空列表，不抛）。"""
    if not isinstance(report, dict):
        return []
    return _as_list(report.get("conversations"))


def report_count_or_none(report: Any) -> Any:
    """报告的会话条数，读不出 ⇒ **None**（这是唯一实现；`report_count` 与 `live_apply._followup_report_count`
    都从它取值）。口径：`conversations` 是列表就用长度，否则退到 `totalConversations`（须是非负整数），
    两者都不可用 ⇒ None＝「这一格没有读数」。
    为什么先要这一格再要那条 0：`decide` 的比例腿、`compare` 的两条计数、`denominator` 与 `phrase`
    念出来的「上一份多少条」**必须是同一个数**——双轴 review 用 `{conversations: <非列表>,
    totalConversations: 100}` 复现出三处各数一遍时给出 100 / 0 / None，而 0 会被当面念成
    「上一份 0 条」，那正是本仓 `null ≠ 0` 口径要防的（#61/#65）。"""
    if not isinstance(report, dict):
        return None
    convs = report.get("conversations")
    if isinstance(convs, list):
        return len(convs)
    total = report.get("totalConversations")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        return None
    return total


def report_count(report: Any) -> int:
    """报告条数的**整数**形态（读不出 ⇒ 0）：给只吃整数的调用方用。判据与措辞要区分
    「读不出」与「真的是 0 条」时，用 `report_count_or_none`。"""
    value = report_count_or_none(report)
    return 0 if value is None else value


def compare(prev_convs: Any, new_convs: Any) -> dict:
    """上一份会话列表 + 本场会话列表 → 签名读数（**键恒在**，纯函数、不读盘、不写盘）。

    四条判据：
    - `unknown`：上一份是空列表，或里面**一个可用键都没有** ⇒ 没有可比条目。首跑不冒充读数。
    - `stable`：`missing == 0`（`appeared` 再多也不改变「上一份的这条还在」这件事）。
    - `tail_truncated`：缺失项在上一份有序列表里的下标集合 == 该列表的**连续后缀**。
      列表按新→旧排是实物结论，所以「首页只下发了前 k 条」正是掉在尾部那一段。
    - `scattered_lost`：其余形态（缺失项之间夹着保留项）⇒ 站点侧移除。

    前置条件（不是巧合，是这条判据的地基）：**两侧列表都按站点给的新→旧排**。位置签名问的是
    「消失的那批是不是排在尾部」，列表一旦乱序，真截断就会长成散落。排序由采集侧保证
    （`platforms.boss._sorted_newest_first`，`test_platforms` 的前缀型/头部型两条用例钉住），
    本模块不再自己按时间重排——报告条目里的 `time` 有两种形态（wapi 是全 datetime，DOM 降级
    是「昨天」这类卡片文案），拿它重排会造出一条只有半批数据能走的假路径，比信这条不变量更糟。

    两条已登记的保守方向（写清楚不藏）：
    - 上一份**末尾**混着无键条目时，真实截断会被念成 `scattered_lost`（无键条目不计入 missing，
      缺失下标集合就不再等于连续后缀）。后果只是「该否决的那场没否决」，读数仍当场点名三个数字
      与签名，人看得见——比反过来把真实流失判成残缺、从此冻住报告轻。
    - 条目里的 `encryptJobId` 有可能是 `_conv_record` 按投递台账**回填**出来的（唯一命中才回填）。
      两条不同的会话被回填成同一个岗位键时，「任一可用键相同即同一条」会把它们并成一条，
      `missing` 就少算——表现是「真掉了一条却报 stable」。这条与 #75 的判据同源（水位线按同一
      规则判「新会话」），要改得连着 #75 一起改；本模块不改行为，只把风险登记在这里。
    """
    prev = _as_list(prev_convs)
    new = _as_list(new_convs)
    new_keys: set = set()
    prev_keys: set = set()
    unkeyed_new = 0
    for entry in new:
        keys = _keys_of(entry)
        if keys:
            new_keys.update(keys)
        else:
            unkeyed_new += 1
    unkeyed_prev = 0
    missing_indices: list = []
    keyed_prev_indices: list = []
    for index, entry in enumerate(prev):
        keys = _keys_of(entry)
        if not keys:
            unkeyed_prev += 1
            continue
        prev_keys.update(keys)
        keyed_prev_indices.append(index)
        if any(key in new_keys for key in keys):
            continue
        missing_indices.append(index)
    appeared = 0
    for entry in new:
        keys = _keys_of(entry)
        if keys and not any(key in prev_keys for key in keys):
            appeared += 1

    missing_from = missing_indices[0] if missing_indices else -1
    found_missing = bool(missing_indices)
    missing_set = set(missing_indices)
    # 「排在首个消失项之后仍被保留」只在有消失项时才有意义；无缺失时两格都落 None/0（不谎报位置）
    kept_after = sum(1 for index in keyed_prev_indices if index > missing_from and index not in missing_set)
    if not prev or not prev_keys:
        shape = SHAPE_UNKNOWN
    elif not missing_indices:
        shape = SHAPE_STABLE
    elif missing_indices == list(range(len(prev) - len(missing_indices), len(prev))):
        shape = SHAPE_TAIL_TRUNCATED
    else:
        shape = SHAPE_SCATTERED_LOST
    return {
        "shape": shape,
        "prevCount": len(prev),
        "newCount": len(new),
        "missing": len(missing_indices),
        "appeared": appeared,
        "unkeyedPrev": unkeyed_prev,
        "unkeyedNew": unkeyed_new,
        "missingFrom": missing_indices[0] if found_missing else None,
        "keptAfterFirstMissing": kept_after if found_missing else 0,
    }


def decide(prev_report: Any, new_report: Any) -> tuple:
    """两份报告 → `(veto, 签名读数)`。**否决判据只有这一处**，两个 writer 都调它。

    `veto = (shape == tail_truncated) and n_new * 2 < n_prev` —— **两条腿同时成立才否决**：
    - 比例腿照抄 #59 既有语义（`* 2 <`，不新增任何比例/条数阈值）；
    - 形态腿是本切片新增的那一半：只有「消失项构成上一份的连续尾部」才是**我们**读数残缺
      （#59 的 3/100 正是这个形态），散落形态（10-21 实测的 63/84）是站点侧把人家的会话移走了，
      报告本来就该跟着变短。
    为什么必须比现行判据**窄**：现行只看 `n*2 < prev`，散落的断崖照样被否——否决一场真实流失，
    等于把 `followup_report.json` 冻在一份旧名单上，而它是全量覆盖写的（模块头第 2 条），
    旧名单意味着新回复永远进不了 eval。反过来 `unknown`（上一份没有可比条目）也不否决：首跑没有
    资格声称别人残缺。"""
    reading = compare(report_conversations(prev_report), report_conversations(new_report))
    n_new = report_count_or_none(new_report)
    n_prev = report_count_or_none(prev_report)
    # 两条计数以**报告级口径**为准并回填进读数：比例腿、`phrase` 念出来的那句、`build_ledger`
    # 落的 `collected`/`previous` 必须是同一个数。`compare` 只看列表，遇到「`conversations`
    # 不是列表、只有 `totalConversations`」这种形态会数出 0——不回填就会当面念成「上一份 0 条」，
    # 把读不出写成一条读数（双轴 review 用该夹具复现出 100 / 0 / None 三个数）。
    reading["prevCount"] = n_prev
    reading["newCount"] = n_new
    veto = reading["shape"] == SHAPE_TAIL_TRUNCATED and n_prev is not None and n_new is not None and n_new * 2 < n_prev
    return veto, reading


def should_announce(reading: Any) -> bool:
    """这份签名要不要在终端/收尾行点名（**判序只有这一处**：live_apply 的采集段、`[done]` 行、
    followup_tracker 三处原来各写一遍「shape 存在且不是 stable」，写法还不一致——本仓
    #59 R4-5 收口的就是「判序复制到调用方」）。
    `stable`＝一条没掉，正常会话零噪音；没有读数（dry-run / 采集失败）也不念，终端不编话。"""
    shape = reading.get("shape") if isinstance(reading, dict) else None
    return bool(shape) and shape != SHAPE_STABLE


def build_ledger(reading: Any = None, collection: Any = None) -> dict:
    """签名读数 + 切片 A 的采集读数 → 收尾台账（`LEDGER_KEYS` 六键恒在）。
    取不到的格落 `None` 不落 `0`——「这一格这次没有读数」与「读数是 0」是两件事
    （口径同 #61 的 `source=null`、#65 的 `recommend_*=null`、切片 A 的 `requestedIds`）。
    `collection` 原样嵌入（不逐字段复制）：它的键集合归 `boss.COLLECTION_KEYS` 所有，
    本模块不 import boss（`_followup_shape` 保持只依赖 stdlib + `_inbound_watermark`，
    这样 boss / live_apply / followup_tracker 都能安全 import 它，不会把 playwright 拖进来）。"""
    given = reading if isinstance(reading, dict) else {}
    return {
        "collected": given.get("newCount"),
        "previous": given.get("prevCount"),
        "missing": given.get("missing"),
        "appeared": given.get("appeared"),
        "shape": given.get("shape"),
        "followupCollection": collection if isinstance(collection, dict) else None,
    }


def denominator(report: Any) -> Any:
    """报告 → 趋势快照的分母读数 `{"conversations": 条数, "shape": 签名}`（#63 验收 4：
    样本数为什么变，趋势里要带着当时的分母与签名，不让人事后翻报告反推）。
    两个来源都拿不到 ⇒ **None**，不补 0；只拿到一个时另一格是 None（分母在、签名未知，
    正是 `followupLedger` 没落上的旧报告的样子）。"""
    if not isinstance(report, dict):
        return None
    count = report_count_or_none(report)  # 与 decide/phrase/ledger 同一个数，不多算一遍
    ledger = report.get("followupLedger")
    shape = ledger.get("shape") if isinstance(ledger, dict) else None
    if count is None and shape is None:
        return None
    return {"conversations": count, "shape": shape}


def phrase(reading: Any) -> str:
    """一行人类可读措辞，**两个 writer 与 `[done]` 收尾行共用**（同一句话不许在 live_apply 与
    followup_tracker 各拼一遍——#64/#75 都栽在两个 writer 各写一份）。
    只描述读数，不写处置动作（覆不覆盖是 `decide` 那条比例腿的事，动作由调用方各自补）。"""
    given = reading if isinstance(reading, dict) else {}
    shape = given.get("shape")
    if shape == SHAPE_UNKNOWN:
        return "无历史基准（上一份没有可比条目），本场只建立、不比对"
    numbers = f"本场 {given.get('newCount')} 条 / 上一份 {given.get('prevCount')} 条 / 消失 {given.get('missing')} 条"
    unkeyed = _unkeyed_note(given)
    if shape == SHAPE_STABLE:
        return f"{numbers}——清单与上一份一致（新增 {given.get('appeared')} 条）{unkeyed}"
    positions = (
        f"首个消失项在上一份第 {given.get('missingFrom')} 位，其后仍保留 {given.get('keptAfterFirstMissing')} 条"
    )
    if shape == SHAPE_TAIL_TRUNCATED:
        return f"{numbers}——签名 {shape}（消失项构成上一份的连续尾部＝首页截断形态）；{positions}{unkeyed}"
    if shape == SHAPE_SCATTERED_LOST:
        return f"{numbers}——签名 {shape}（消失项散落在列表中间＝站点侧移除，不是我们的截断）；{positions}{unkeyed}"
    return f"{numbers}——签名 {shape!r}（不在闭集里：判据没覆盖到这一格，读数原样念出来，不猜形态）"


def _unkeyed_note(reading: dict) -> str:
    """无可用会话键的条目数（不参与比对、不计入消失）——有就说出来，不静默丢（#75 口径）。"""
    prev, new = reading.get("unkeyedPrev"), reading.get("unkeyedNew")
    if not prev and not new:
        return ""
    return f"；另有无可用键条目 上一份 {prev} / 本场 {new}（不参与比对）"
