#!/usr/bin/env python3
"""`_no_jd` 的**模块契约**单测（#57 验收 1：拆出的新模块要有自己的单测文件）。

与 `test_live_apply.py` 里那批 no_jd 用例的分工（刻意不重复，那批钉三类判据、分流读数与
台账动作，本文件只钉只能在模块本身上成立的四条）：
① 归因模块不反向 import 编排层（防环，验收 3）；② JD 正文取值链没有第二份（单一真源）；
③ 判据对**坏形态容器**不误抛也不猜（监听器容器由页面行为喂出来，形态漂移是常态）；
④ `live_apply` 的别名与模块真身是同一对象（别名一旦变成复制，模块内部互调就与门面分叉）。
"""

import ast
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 一律普通 import，不用 `spec_from_file_location` 自载：那样会得到**第二个模块对象**，
# 而 `live_apply` 的别名读的是 `sys.modules` 里那一份——两本账各自绿，正是本批用例要防的失效。
import _no_jd as nj  # noqa: E402
import live_apply as la  # noqa: E402

SELF = Path(__file__).resolve().parent


def test_no_reverse_import_of_the_orchestrator():
    """防环（验收 3「模块间无循环 import」）：归因模块反向 import `live_apply` 就是环。今天
    装得上是靠加载顺序凑巧，一旦 `_no_jd` 里多一处这样的 import，整包会在收集期就炸。"""
    for name in ("_no_jd", "_wedge_ledger", "_forensics"):
        for node in ast.parse((SELF / f"{name}.py").read_text(encoding="utf-8")).body:
            if isinstance(node, ast.ImportFrom):
                assert node.module != "live_apply", f"{name} 不许 import 编排层"
            elif isinstance(node, ast.Import):
                assert all(a.name.split(".")[0] != "live_apply" for a in node.names), f"{name} 不许 import 编排层"


def test_jd_body_chain_is_not_forked():
    """「这份响应到底有没有 JD 正文」只允许有一个答案来源：`platforms.chat_first.detail_post_description`。
    归因模块若自带一份，`grab_detail` 与「DOM 与 XHR 双空」判据就会分叉——分叉的后果是同一个岗
    在 A 处算拿到正文、在 B 处算无正文，而 B 是唯一允许把岗移出补投台账的那一类（#56 修复轮 3
    R2 立的单一真源，这里钉它没被搬迁拆成两份）。"""
    from platforms import chat_first

    assert nj.detail_post_description is chat_first.detail_post_description
    defined = {
        n.name
        for n in ast.parse((SELF / "_no_jd.py").read_text(encoding="utf-8")).body
        if isinstance(n, ast.FunctionDef)
    }
    assert "detail_post_description" not in defined, "取值链被复制进归因模块＝两份真源"


@pytest.mark.parametrize(
    "container",
    [
        None,
        "not-a-dict",
        123,
        {},
        {"errors": "不是列表"},
        {"errors": [None, 1]},  # 列表里塞脏条目
        {"data": {"zpData": None}},  # 岗位失效时站点真实回的形状
        {"data": [], "empty_outcome": []},
        {"empty_outcome": {"hit_total": "0"}},  # 命中数不是整数
        {"nonzero": {"message": "没有 code"}},
    ],
)
def test_classifiers_never_raise_and_never_invent_evidence(container):
    """四个入口对畸形输入一律「给得出结论或落 unknown」，不抛、不猜。
    特别是**不许把取证缺口猜成双空**：双空是唯一会丢岗的归类，无正向证据时必须退回
    `detail_no_response`（留在补投台账那侧，#41/#42「漏投比错投代价高」）。"""
    assert nj.classify_no_jd(container) in nj.NO_JD_CLASSES
    assert nj.classify_no_jd(container) != nj.NO_JD_BOTH_EMPTY, "畸形容器不构成「两侧都无正文」的正向证据"
    assert nj.no_jd_dom_shape(container) in nj.NO_JD_DOM_SHAPES
    assert nj.no_jd_detail_code(container) is None
    assert isinstance(nj._no_jd_signal_readings(container), dict)


def test_readers_are_pure_and_report_serializable():
    """三条读数函数会被结果条目、事件与 report.json 三处消费：必须是纯函数（不改入参、
    重复调用同结果，否则第一处消费之后第二处读到脏数据），且产物可直接序列化。"""
    results = [
        {
            "status": "no_jd",
            "idx": 1,
            "job": "A",
            "jd_class": nj.NO_JD_NO_RESPONSE,
            "dom_shape": nj.DOM_SHAPE_ZERO_HITS,
        },
        {"status": "no_jd", "idx": 2, "job": "B"},
        {"status": "applied", "idx": 3, "job": "C"},
    ]
    snapshot = copy.deepcopy(results)
    once = (nj._no_jd_class_stats(results), nj._no_jd_shape_stats(results), nj._no_jd_empty_urls(results))
    twice = (nj._no_jd_class_stats(results), nj._no_jd_shape_stats(results), nj._no_jd_empty_urls(results))
    assert once == twice, "读数不是纯函数"
    assert results == snapshot, "读数函数改写了入参"
    classes, (shapes, nonzero_by_class) = once[0], once[1]
    assert all(k in classes for k in nj.NO_JD_CLASSES), "类别键恒在：『双空 0 个』与『没这个读数』含义不同"
    assert all(k in shapes for k in nj.NO_JD_DOM_SHAPES), shapes
    assert all(k in nonzero_by_class for k in nj.NO_JD_CLASSES), nonzero_by_class
    assert json.dumps(once, ensure_ascii=False)


def test_live_apply_aliases_are_the_same_objects_not_copies():
    """别名接线（#57 的迁移形态）只有在「别名指向模块真身」时才安全：谁把它改成复制实现，
    模块内部互调与门面就会分叉，而两边的测试都各自绿。"""
    for name in ("classify_no_jd", "no_jd_dom_shape", "no_jd_detail_code", "_no_jd_class_stats"):
        assert getattr(la, name) is getattr(nj, name), f"{name} 的别名不再是 _no_jd 里的同一个对象"
    assert nj.classify_no_jd.__globals__ is nj.__dict__, "归因函数被搬出了本模块命名空间"
