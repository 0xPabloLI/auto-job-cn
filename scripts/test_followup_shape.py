#!/usr/bin/env python3
"""`_followup_shape` 的契约用例（#63 切片 B）——签名判据与否决判据的行为面。

写这些用例的标准是**「行为不变要用对抗输入证伪」**：光给一组顺的夹具（「N 组 0 差异」）不算证据，
所以每条都带一个会把朴素实现打回原形的输入——库里混过的 `null`、整份无键的上一份、
键换形（上一份只有 `encryptJobId`、本场两个键都有）、`int` 与 `str` 的同一键、
以及「散落但跌幅过半」那条与现行判据相反的组合。

断言里的数字**由被测函数算出来**（`len(prev) - len(new)` 这类），不写死夹具里的常量：
写死的数字在判据改错时会跟着一起被改，算出来的数字才会当场把不一致暴露出来。
"""

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "_followup_shape", Path(__file__).resolve().parent / "_followup_shape.py"
)
assert _spec is not None and _spec.loader is not None, "importlib spec 加载失败（模块文件缺失？）"
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fs := mod)

import _inbound_watermark as wm  # noqa: E402

PREV_SIZE = 8
NEW_SIZE = 5


def _convs(keys, label="上"):
    """按 `encryptJobId` 造会话条目（列表顺序即「新→旧」，与 #63 的实物结论同构）。"""
    return [{"encryptJobId": k, "company": f"{label}司{k}"} for k in keys]


def _uids(keys, label="上"):
    return [{"encryptUid": k, "company": f"{label}司{k}"} for k in keys]


def _report(convs, total=None):
    return {"conversations": convs, "totalConversations": total if total is not None else len(convs)}


def _keys(count, start=0, step=1):
    """`p0 p1 p2 …` 这种序列键（step=2 用来造散落）。"""
    return [f"p{i}" for i in range(start, start + count * step, step)]


def _scattered_keys(total, keep):
    """从上一份 `total` 个键里**跳着**取 `keep` 个（取满就从剩下的补）⇒ 缺失一定非连续，
    而且取出的键**全在上一份的键空间里**：散落形态与「本场新增」两件事不许混在一条用例里。"""
    picked = [f"p{i}" for i in range(0, total, 2)]
    if len(picked) < keep:
        picked += [f"p{i}" for i in range(1, total, 2)][: keep - len(picked)]
    return picked[:keep]


def test_reading_keys_are_always_present_even_for_garbage_input():
    """恒在键契约：形态同 #62 / #65 / 切片 A——调用方（两个 writer + `[done]` 行）不许防缺键。
    输入换成 `None`/非列表也必须回同一套键。"""
    for prev, new in (([], []), (None, None), ({"not": "a list"}, "字符串"), (_convs(_keys(2)), [None, "junk"])):
        reading = fs.compare(prev, new)
        assert set(reading) == set(fs.READING_KEYS), reading
    # 四态是闭集：任何输入都只能落在这四个里
    assert fs.compare(_convs(_keys(PREV_SIZE)), _convs(_keys(NEW_SIZE)))["shape"] in fs.SHAPES


def test_both_empty_lists_is_unknown_not_stable():
    """两条列表都空 ⇒ `unknown`（没有可比条目），**不许**念成「stable，一条没掉」——
    那等于把「无从比较」谎报成「清单完好」（首跑不冒充，语义同 #75 的 `first_run`）。"""
    reading = fs.compare([], [])
    assert reading["shape"] == fs.SHAPE_UNKNOWN, reading
    assert reading["prevCount"] == 0 and reading["newCount"] == 0
    assert reading["missing"] == 0 and reading["missingFrom"] is None
    assert "无历史基准" in fs.phrase(reading)


def test_missing_prev_alone_is_unknown():
    """上一份不存在 / 空，本场有读数 ⇒ `unknown`：不比对，也不把全部条数说成新增。"""
    new = _convs(_keys(3))
    reading = fs.compare([], new)
    assert reading["shape"] == fs.SHAPE_UNKNOWN
    assert reading["missing"] == 0 and reading["appeared"] == len(new), "无基准时新增数不参与形态判断"


def test_contiguous_suffix_is_tail_truncated():
    """消失项正好是上一份的**连续后缀**（新→旧列表只下发了前缀）⇒ #59 的 3/100 形态。"""
    prev = _convs(_keys(PREV_SIZE))
    new = _convs(_keys(NEW_SIZE))
    reading = fs.compare(prev, new)
    assert reading["shape"] == fs.SHAPE_TAIL_TRUNCATED, reading
    assert reading["missing"] == len(prev) - len(new) == reading["prevCount"] - reading["newCount"]
    assert reading["missingFrom"] == len(new), "首个消失项就是被截断的那一位"
    assert reading["keptAfterFirstMissing"] == 0, "连续尾部 ⇒ 其后一条都没保留"
    assert reading["appeared"] == 0 and reading["unkeyedPrev"] == 0


def test_scattered_missing_is_scattered_lost_even_with_big_drop():
    """同样的条数、**散落**的形态（10-21 实测：消失项从第 9 位散到第 83 位，保留项排在它们之后）
    ⇒ `scattered_lost`。这一条是整张票的分歧点：比例腿看不见它，覆盖与不覆盖的处置相反。"""
    prev = _convs(_keys(PREV_SIZE))
    new = _convs(_scattered_keys(PREV_SIZE, NEW_SIZE - 1))  # p0 p2 p4 p6 ⇒ 奇数位一条不落
    reading = fs.compare(prev, new)
    assert reading["shape"] == fs.SHAPE_SCATTERED_LOST, reading
    assert reading["missing"] == len(prev) - len(new)
    assert reading["missingFrom"] == 1
    assert reading["keptAfterFirstMissing"] == len(new) - 1, "除首位外全部排在首个消失项之后"


def test_single_missing_in_the_middle_is_scattered_not_truncated():
    """只掉一条、掉在中间 ⇒ 后缀条件不成立 ⇒ 散落。这条专治「用 max(missing)==len(prev)-1 之类的
    近似写法冒充后缀判断」的实现。"""
    prev = _convs(_keys(PREV_SIZE))
    new = _convs([k for k in _keys(PREV_SIZE) if k != f"p{PREV_SIZE // 2}"])
    reading = fs.compare(prev, new)
    assert reading["missing"] == 1 and reading["missingFrom"] == PREV_SIZE // 2
    assert reading["shape"] == fs.SHAPE_SCATTERED_LOST, reading
    assert reading["keptAfterFirstMissing"] == len(prev) - reading["missingFrom"] - 1


def test_every_item_missing_is_the_whole_list_as_a_suffix():
    """missing == 全部 ⇒ 整段列表本身就是它的连续后缀 ⇒ `tail_truncated`
    （本场一条都没采到，正是首页截断最极端的读数；0 条那条老防线在 writer 侧先兜住）。"""
    prev = _convs(_keys(PREV_SIZE))
    reading = fs.compare(prev, [])
    assert reading["shape"] == fs.SHAPE_TAIL_TRUNCATED, reading
    assert reading["missing"] == len(prev) and reading["missingFrom"] == 0
    assert reading["keptAfterFirstMissing"] == 0


def test_non_dict_entries_never_raise_and_never_count_as_missing():
    """真实库里混过 `null`：`e.get` 会把 `AttributeError` 冒进调用方（writer 的 try 会把它读成
    「采集失败」，一场好会话白丢）。非 dict 条目只计数、不比对、不计入消失。"""
    prev = _convs(_keys(3))
    prev.insert(1, None)
    prev.append("junk")
    new = _convs(_keys(3))
    reading = fs.compare(prev, new)
    assert reading["prevCount"] == len(prev), "条数按列表实数算（含坏条目），与 #59 的比例腿口径一致"
    assert reading["missing"] == 0, reading
    assert reading["unkeyedPrev"] == len(prev) - 3, "非 dict 与无键条目都归 unkeyed，不静默丢"
    assert reading["shape"] == fs.SHAPE_STABLE, reading


def test_unkeyed_entries_are_not_compared():
    """上一份整份无键（DOM 降级卡片正是这个形态）⇒ 没有可比条目 ⇒ `unknown`，
    **不许**因为「本场一个都没配上」把它念成截断。"""
    prev = [{"company": f"无键{i}"} for i in range(PREV_SIZE)]
    new = _convs(_keys(2))
    reading = fs.compare(prev, new)
    assert reading["shape"] == fs.SHAPE_UNKNOWN, reading
    assert reading["unkeyedPrev"] == len(prev) and reading["missing"] == 0
    assert reading["unkeyedNew"] == 0 and reading["newCount"] == len(new)


def test_key_migration_is_the_same_conversation():
    """#75 review 条目 I 的坑：上一份只有 `encryptJobId`，本场同一条两个键都有
    （boss 给归到投递记录的会话回填 jobId）。只比单键会把同一条念成「消失又出现」，
    于是 `missing` 与 `appeared` 一起虚高——流失签名会被做成一条假的增长。"""
    prev = [{"encryptJobId": "e1", "status": "hr_replied"}]
    new = [{"encryptJobId": "e1", "encryptUid": "u1", "status": "hr_replied"}]
    reading = fs.compare(prev, new)
    assert reading["missing"] == 0 and reading["appeared"] == 0, reading
    assert reading["shape"] == fs.SHAPE_STABLE
    # 反向也一样（本场只认回 uid 那侧），并且 uid 键与 int 形态都算同一条（session_keys 取 str）
    assert fs.compare(new, [{"encryptUid": "u1"}])["shape"] == fs.SHAPE_STABLE
    assert fs.compare(_uids(["7"]), [{"encryptUid": 7}])["shape"] == fs.SHAPE_STABLE


def test_appeared_counts_only_keyed_new_entries():
    """本场新增（键在上一份一个都没有）进 `appeared`，不影响形态（形态只看上一份掉在哪）。"""
    prev = _convs(_keys(2))
    new = _convs(_keys(2)) + _convs(_keys(2, start=50)) + [{"company": "无键新条目"}]
    reading = fs.compare(prev, new)
    assert reading["shape"] == fs.SHAPE_STABLE and reading["missing"] == 0
    assert reading["appeared"] == 2 and reading["unkeyedNew"] == 1, reading


def test_compare_reuses_watermark_key_rules():
    """判同一条必须走 `_inbound_watermark.session_keys`（不许本模块自己写键规则）：
    空串键不算键——把它塞进 `session_keys` 应得空元组，签名侧也就该记 `unkeyed`。"""
    assert wm.session_keys({"encryptJobId": "", "encryptUid": None}) == ()
    reading = fs.compare([{"encryptJobId": "", "company": "空串键"}], [{"encryptJobId": "", "company": "空串键"}])
    assert reading["unkeyedPrev"] == 1 and reading["shape"] == fs.SHAPE_UNKNOWN, reading


@pytest.mark.parametrize(
    ("kind", "prev_size", "new_size", "expected_veto"),
    [
        ("tail", 100, 3, True),  # 首页截断 + 跌幅过半 ⇒ 否决（#59 的 3/100 实测形态）
        ("tail", 100, 60, False),  # 连续尾部但未过半 ⇒ 覆盖（渐进衰减）
        ("scattered", 100, 40, False),  # **散落 + 跌幅过半 ⇒ 覆盖**（本切片唯一新增的行为，RED 那条）
        ("scattered", 100, 60, False),  # 散落且未过半 ⇒ 覆盖
    ],
)
def test_decide_vetoes_only_the_truncated_cliff(kind, prev_size, new_size, expected_veto):
    """`decide` 的四角组合：比例腿与形态腿各自单独成立都**不**否决。
    第 3 行就是那条与现行判据相反的组合（现行只看 `n*2 < prev` 会把它否掉，
    报告从此冻在旧名单上、新回复永远进不了 eval）。"""
    prev = _convs(_keys(prev_size))
    picked = _keys(new_size) if kind == "tail" else _scattered_keys(prev_size, new_size)
    new = _convs(picked)
    veto, reading = fs.decide(_report(prev), _report(new))
    assert reading["shape"] == (fs.SHAPE_TAIL_TRUNCATED if kind == "tail" else fs.SHAPE_SCATTERED_LOST), reading
    assert veto is expected_veto, (reading, expected_veto)
    assert reading["missing"] == len(prev) - len(new), reading
    assert reading["prevCount"] == len(prev) and reading["newCount"] == len(new)
    # 比例腿单独核对一遍：散落那两行「现行判据会否决」必须由这个式子说清楚，不靠注释
    ratio_leg_would_veto = reading["newCount"] * 2 < reading["prevCount"]
    old_judgement_vetoes = (prev_size, new_size) in [(100, 3), (100, 40)]
    assert ratio_leg_would_veto is old_judgement_vetoes, reading


def test_decide_unknown_and_stable_never_veto():
    """首跑（上一份读不出 / 整份无键）与 stable 都不否决：前者没有资格声称别人残缺，
    后者压根没掉东西。"""
    new = _report(_convs(_keys(3)))
    for prev in ({}, None, _report([{"company": "无键"}] * 200), _report({"不是": "列表"}, total=200)):
        veto, reading = fs.decide(prev, new)
        assert reading["shape"] == fs.SHAPE_UNKNOWN and veto is False, (prev, reading)
    same = _report(_convs(_keys(3)))
    veto, reading = fs.decide(same, same)
    assert reading["shape"] == fs.SHAPE_STABLE and veto is False, reading


def test_decide_count_falls_back_to_total_conversations():
    """比例腿的条数口径与 #59 同源（`conversations` 长度优先，退化到 `totalConversations`）：
    会话列表读不出时条数仍取报告字段，但**形态腿**没有可比条目 ⇒ 照样不否决
    （不覆盖的前提是「知道它掉在尾部」，不是「条数看着少」）。"""
    prev = _report(_convs(_keys(100)))
    prev["conversations"] = {"不是": "列表"}
    veto, reading = fs.decide(prev, _report(_convs(_keys(3))))
    assert veto is False and reading["shape"] == fs.SHAPE_UNKNOWN, reading
    assert fs.report_count(prev) == 100 and fs.report_count(_report([])) == 0
    assert fs.report_count({"totalConversations": True}) == 0, "布尔值不当条数（同 #59 坏形态不猜）"


def test_phrase_names_three_numbers_and_shape_on_one_line():
    """措辞只有一份：`[done]` 行与两个 writer 都调它，所以三个数字（本场/上一份/消失）与签名
    必须由读数本身算出来——同一句话在两个 writer 各拼一遍是 #64/#75 的老事故。"""
    prev = _convs(_keys(PREV_SIZE))
    new = _convs(_scattered_keys(PREV_SIZE, NEW_SIZE))
    reading = fs.compare(prev, new)
    text = fs.phrase(reading)
    assert "\n" not in text, text
    for needle in (
        f"本场 {reading['newCount']} 条",
        f"上一份 {reading['prevCount']} 条",
        f"消失 {reading['missing']} 条",
    ):
        assert needle in text, (needle, text)
    assert reading["shape"] in text
    tail = fs.phrase(fs.compare(prev, _convs(_keys(NEW_SIZE))))
    assert fs.SHAPE_TAIL_TRUNCATED in tail and "连续尾部" in tail, tail
    stable = fs.phrase(fs.compare(prev, prev))
    assert "一致" in stable and "消失 0 条" in stable, stable
    assert fs.phrase(fs.compare([], prev)).startswith("无历史基准")
    assert fs.phrase({}) != "", "缺键读数也要出一行话，不许抛"


def test_phrase_names_unkeyed_instead_of_dropping_them():
    """无可用键的条目（不参与比对、不计入消失）必须在措辞里点名，口径同 #75 的零键拒写。"""
    keyed = _convs(_keys(2))
    prev = keyed + [{"company": "无键"}]
    reading = fs.compare(prev, keyed)
    assert reading["unkeyedPrev"] == 1
    assert "无可用键条目" in fs.phrase(reading), fs.phrase(reading)
    assert "无可用键条目" not in fs.phrase(fs.compare(keyed, keyed)), "0 不进措辞（同 #75 只在非零时念）"


def test_ledger_keys_are_always_present_and_cells_default_to_none():
    """`followupLedger` 六键恒在；取不到的格落 `None` 不落 `0`
    （口径同 #61 的 `source=null`、切片 A 的 `requestedIds`）。"""
    empty = fs.build_ledger()
    assert set(empty) == set(fs.LEDGER_KEYS), empty
    assert all(value is None for value in empty.values()), empty
    reading = fs.compare(_convs(_keys(4)), _convs(_keys(2)))
    collection = {"path": "wapi", "batches": 1, "union": 2}
    ledger = fs.build_ledger(reading, collection)
    assert ledger["collected"] == reading["newCount"] and ledger["previous"] == reading["prevCount"]
    assert ledger["missing"] == reading["missing"] and ledger["appeared"] == reading["appeared"]
    assert ledger["shape"] == reading["shape"]
    assert ledger["followupCollection"] == collection, "切片 A 的读数原样嵌入，不复制字段"
    assert fs.build_ledger(reading, None)["followupCollection"] is None, "没接到采集读数时落 None 不落空字典"
    assert fs.build_ledger("不是读数")["shape"] is None, "坏形态输入也不许抛"


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        (None, None),
        ({}, None),
        ({"conversations": {"不是": "列表"}}, None),
        ({"conversations": _convs(_keys(3))}, {"conversations": 3, "shape": None}),
        (
            {
                "conversations": _convs(_keys(3)),
                "followupLedger": fs.build_ledger(fs.compare(_convs(_keys(3)), _convs(_keys(3)))),
            },
            {"conversations": 3, "shape": fs.SHAPE_STABLE},
        ),
        (
            {"followupLedger": {"shape": fs.SHAPE_SCATTERED_LOST}},
            {"conversations": None, "shape": fs.SHAPE_SCATTERED_LOST},
        ),
    ],
)
def test_denominator_shape_or_none(report, expected):
    """趋势的分母读数：两个来源（会话列表条数 / 落进报告的签名）都拿不到 ⇒ **None**，不补 0
    ——`0` 是一条读数，`None` 才是「这一格这次没有读数」。"""
    assert fs.denominator(report) == expected, report


def test_denominator_count_comes_from_the_conversation_list():
    """分母里的条数就是报告实到的会话数（与 `compare` 的 `newCount` 同口径），不是 eval 的样本数。"""
    convs = _convs(_keys(3))
    assert fs.denominator(_report(convs + [{"company": "无键"}])) == {"conversations": 4, "shape": None}
