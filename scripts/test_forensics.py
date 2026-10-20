#!/usr/bin/env python3
"""`_forensics.py` 的模块契约测试（#57 验收 1）。

`test_live_apply.py` 里那批取证用例钉的是**投递主链各节点有没有按期拍点**（谁在什么时候拍、
异常出口拍不拍 `*:error`）。本文件只钉只能在归属模块上成立的五件事：状态单例的归属（别名不能
变成第二份）、心跳 payload 与击杀 marker 的**字段名真源**、未绑定时的降级形状、取栈开关的
取值表方向、以及 #56 取证 A 的监听器/在途往返计数（注册表、平衡、enter 那一拍自己落盘）。
#57 把取证段拆出来的目的就是让「改字段名要同时改三处」这件事有个唯一出口——
没有这组用例，真源只是 docstring 上的声称。
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _forensics as fx  # noqa: E402
import live_apply as la  # noqa: E402
from _limits import BOOL_ENV_TWO_OFF  # noqa: E402


def _flow(idx=2, job=None):
    """`_heartbeat_job_start` 只读 flow.job / flow.idx，不必造完整会话上下文。"""
    return SimpleNamespace(
        job={"jobName": "区块链岗", "brandName": "测试公司", "encryptJobId": "eid-2"} if job is None else job,
        idx=idx,
    )


# --- 字段名真源：payload 与 marker 都从同一处派生 ---


def test_payload_shape_is_exactly_the_shared_field_tuple(tmp_path, monkeypatch):
    """`HEARTBEAT_FIELDS` 不是装饰性常量：payload 的键集合与顺序都由它决定。漂移的第一现场
    就是「进程内一切正常、看门狗读不到 eid」——B1 整场不入账且无声。"""
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    payload = fx._heartbeat_payload("job:2")
    assert tuple(payload) == fx.HEARTBEAT_FIELDS, (tuple(payload), fx.HEARTBEAT_FIELDS)
    assert set(fx.MARKER_IDENTITY_KEYS.values()) <= set(fx.HEARTBEAT_FIELDS), "marker 映射指向了心跳里没有的字段"


def test_marker_identity_keys_are_the_documented_contract():
    """击杀取证 marker 的身份键集（`_selfguard` 写、B1 读、runbook 判读三方共用的那份形状）。
    改名不是重构而是契约变更：三份读法里任何一处单独走，症状都是「呈报里岗位身份变成 None」。"""
    assert set(fx.MARKER_IDENTITY_KEYS) == {
        fx.MARKER_LAST_LABEL,
        fx.MARKER_LAST_EID,
        fx.MARKER_LAST_JOB,
        fx.MARKER_LAST_BRAND,
        fx.MARKER_IN_JOB,
    }
    assert fx.MARKER_LAST_EID == "last_eid" and fx.MARKER_IN_JOB == "in_job"
    assert fx.MARKER_IDENTITY_KEYS[fx.MARKER_LAST_EID] == "eid"


# --- 状态与别名的归属 ---


def test_heartbeat_state_lives_in_the_owner_module_not_in_live_apply():
    """别名接线的正确性前提：`live_apply` 上的那些名字必须是同一个函数对象，且函数的
    `__globals__` 就是 `_forensics.__dict__`——否则 `monkeypatch.setattr(fx, "_HEARTBEAT_DIR")`
    与被测代码读到的会是两份状态（#57 迁移时最容易静默踩空的一步）。"""
    for name in (
        "_heartbeat_bind",
        "_heartbeat_job_start",
        "_heartbeat_job_end",
        "_touch_heartbeat",
        "_heartbeat_beat",
        # #56 取证 A 走别名的四个函数：绑的必须是同一个函数对象，调用点才真的落在 `_forensics`
        # 那三份状态上（状态本身**不**经别名导出，理由见本模块 docstring 与下面的复位夹具）。
        "handler_roundtrip_enter",
        "handler_roundtrip_exit",
        "listener_attached",
        "listener_detached",
    ):
        assert getattr(la, name) is getattr(fx, name), f"{name} 的别名不再是 _forensics 里的同一个对象"
    assert fx._touch_heartbeat.__globals__ is fx.__dict__


def test_unbound_touch_is_silent_false(tmp_path, monkeypatch, capsys):
    """未绑定会话目录（进程刚起、preflight 之前）⇒ 返回 False、不落任何文件、不抛。
    看门狗的语义是「没有心跳文件＝无看门狗」，这里不许它反过来变成崩在投递路上。"""
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", None)
    assert fx._touch_heartbeat("early:beat") is False
    assert list(tmp_path.iterdir()) == []
    assert capsys.readouterr().out == ""


def test_identity_is_carried_inside_a_job_and_cleared_after_it(tmp_path, monkeypatch):
    """在岗时带身份、离岗时 `in_job=False` 且 eid 清空——B1 的归因门槛只看这两件事，
    离岗后不清就会把两岗之间的楔死记到上一岗头上（把一个正常岗永久拉黑）。"""
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    monkeypatch.setattr(fx, "_HEARTBEAT_JOB", None)

    assert fx._touch_heartbeat("scroll:1") is True
    out = json.loads((tmp_path / fx.SESSION_HEARTBEAT).read_text(encoding="utf-8"))
    assert out["in_job"] is False and out["eid"] is None

    fx._heartbeat_job_start(_flow())
    assert fx._touch_heartbeat("job:2") is True
    out = json.loads((tmp_path / fx.SESSION_HEARTBEAT).read_text(encoding="utf-8"))
    assert (out["eid"], out["job"], out["brand"], out["idx"], out["in_job"]) == (
        "eid-2",
        "区块链岗",
        "测试公司",
        2,
        True,
    )

    fx._heartbeat_job_end()
    assert fx._touch_heartbeat("shot:after-read") is True
    out = json.loads((tmp_path / fx.SESSION_HEARTBEAT).read_text(encoding="utf-8"))
    assert out["in_job"] is False and out["eid"] is None, "岗位上下文泄漏到了下一段"


# --- 取栈开关的取值表 ---


@pytest.mark.parametrize("raw", ["", "1", "on", "true", "ofs", "false", "no", "OFF2"])
def test_wedge_dump_only_recedes_for_the_two_off_words(raw, monkeypatch):
    """默认开；`strip()+lower()` 后只认 0/off（表与 LIVE_WEDGE_SKIP 同源，大小写与空白不敏感）。"""
    monkeypatch.setenv(fx.WEDGE_DUMP_ENV, raw)
    assert fx._wedge_dump_enabled() is True


@pytest.mark.parametrize("raw", ["0", "off", " OFF ", "0\t"])
def test_wedge_dump_switch_off_words_disable_it(raw, monkeypatch):
    monkeypatch.setenv(fx.WEDGE_DUMP_ENV, raw)
    assert fx._wedge_dump_enabled() is False


def test_wedge_dump_switch_is_quiet_on_unreadable_values(monkeypatch, capsys):
    """与跳岗开关的**方向相同、噪音不同**：本开关非法值静默按开（取栈是观测增强，写错一个字母
    不值得在日志里喊），跳岗开关非法值要喊（它决定「这个岗还投不投」）。差异登记在
    `_limits.py` 的取值表对照小节，这里钉住别被后来人统一掉。"""
    monkeypatch.setenv(fx.WEDGE_DUMP_ENV, "diabled")
    assert fx._wedge_dump_enabled() is True
    assert capsys.readouterr().out == ""
    assert fx.WEDGE_DUMP_ENV and set(fx._wedge_dump_enabled.__doc__ or "")  # docstring 非空（口径在码旁）
    assert BOOL_ENV_TWO_OFF == ("0", "off")


# --- #56 取证 A：监听器注册表与「handler 内协议往返」计数 ---


@pytest.fixture(autouse=True)
def _reset_handler_forensics():
    """注册表与两个计数同为模块级单例：本段断言的全是**绝对读数**（`handlers_live == 2`），
    开局不给零点就会随用例顺序时红时绿。conftest 的 autouse 只兜 teardown，这里补 setup
    那一半（形态同 `test_live_apply.py` 用 `_reset_wedge_dump_state` 的习惯——复位一律打在
    `_forensics` 本体，不打在 `live_apply` 的别名上）。"""
    fx._reset_handler_forensics()
    yield
    fx._reset_handler_forensics()


def _beat(tmp_path):
    return json.loads((tmp_path / fx.SESSION_HEARTBEAT).read_text(encoding="utf-8"))


def test_listener_registry_counts_by_identity_and_detach_is_idempotent():
    """按 handler **对象身份**数而不是按事件名数：同一张 page 上那几个闭包的事件名全是 "response"。
    重复注销走 `discard`——既不抛，也不会把「本来就没挂着」读成失败，更不会顺手把另一个还挂着的
    handler 摘掉（那等于把 A1 修好的泄漏重新读成健康）。"""
    h1 = lambda resp: None  # noqa: E731  # 两个不同的 handler 闭包（身份不同才有意义）
    h2 = lambda resp: None  # noqa: E731
    fx.listener_attached(h1)
    fx.listener_attached(h2)
    fx.listener_attached(h1)  # 同一对象重复登记不双计
    assert fx.handler_forensics()["handlers_live"] == 2

    fx.listener_detached(h1)
    fx.listener_detached(h1)  # 重复注销幂等
    assert fx.handler_forensics()["handlers_live"] == 1
    fx.listener_detached(h2)
    fx.listener_detached(h2)
    assert fx.handler_forensics()["handlers_live"] == 0


def test_roundtrip_exit_balances_even_when_the_body_raises():
    """exit 走 `try/finally` 语义：`resp.json()` 抛异常（票面 A5 那条失败形态）时配对照样平衡。
    计数没有下限的话，一次漏配对就会让 `handlers_inflight` 永远为正——下一次卡死时这条读数
    直接作废，而它是区分嫌疑①与嫌疑②③的唯一依据。"""

    def body():
        fx.handler_roundtrip_enter("/wapi/zpgeek/job/detail.json")
        try:
            raise RuntimeError("Timeout 30000ms exceeded while reading response body")
        finally:
            fx.handler_roundtrip_exit()

    with pytest.raises(RuntimeError):
        body()
    assert fx.handler_forensics()["handlers_inflight"] == 0
    fx.handler_roundtrip_exit()  # 没有配对的 exit：下限 0，不许打成负数
    assert fx.handler_forensics()["handlers_inflight"] == 0


def test_enter_lands_its_own_beat_with_inflight_already_one(tmp_path, monkeypatch):
    """取证 A 的**核心口径**（这条决定整份取证有没有用）：enter 在 +1 之后**立刻自己拍一拍**，
    于是卡死时「最后一次写盘的 heartbeat」里 `handlers_inflight>=1`。若只等正常节拍来读这个数，
    handler 挂住那一拍永远来不及落盘，取证白做。
    反向读法同样在这条里：exit 之后落盘的是 0（⇒「往返挂住」被证伪，嫌疑移向浏览器或出网）。
    label 只带端点尾段、不带完整 frag——`handler_last_frag` 那一路才放完整片段。"""
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", tmp_path)
    monkeypatch.setattr(fx, "_HEARTBEAT_JOB", None)
    handler = lambda resp: None  # noqa: E731
    fx.listener_attached(handler)

    fx.handler_roundtrip_enter("/wapi/zpgeek/job/detail.json")
    beat = _beat(tmp_path)
    assert beat["label"] == "handler:detail.json:enter", beat
    assert "/wapi" not in beat["label"], "label 里不许塞完整 URL/query"
    assert (beat["handlers_live"], beat["handlers_inflight"]) == (1, 1), beat
    assert beat["handler_last_frag"] == "/wapi/zpgeek/job/detail.json"

    fx.handler_roundtrip_exit()
    beat = _beat(tmp_path)
    assert beat["label"] == "handler:detail.json:exit" and beat["handlers_inflight"] == 0, beat


def test_handler_forensics_shape_is_the_heartbeat_tuple_tail():
    """`handler_forensics()` 的键集与心跳末三键**同一份真源**（`HEARTBEAT_FIELDS`）：
    两处各写一份字面量，早晚漂成「现场读得到、心跳里没有」。"""
    reading = fx.handler_forensics()
    assert list(reading) == list(fx.HEARTBEAT_FIELDS[-3:])
    assert reading == {"handlers_live": 0, "handlers_inflight": 0, "handler_last_frag": None}


def test_forensics_observers_never_raise_on_any_input(tmp_path, monkeypatch):
    """观测/取证层铁律（同 `_heartbeat_beat`）：不可哈希的 handler、None/怪类型的 frag、
    心跳未绑定……这些函数挂在投递动作的必经之路上，一个都不许把异常送回 dispatcher。"""
    monkeypatch.setattr(fx, "_HEARTBEAT_DIR", None)
    fx.listener_attached([1, 2])  # 不可哈希 ⇒ set.add 抛 TypeError，必须被吞掉
    fx.listener_detached(None)  # 本来就没挂着 ⇒ discard 幂等，不抛 KeyError
    fx.listener_detached([3, 4])  # 不可哈希 ⇒ set.discard 也抛 TypeError，同样吞掉
    fx.handler_roundtrip_enter(None)
    fx.handler_roundtrip_enter(object())
    fx.handler_roundtrip_exit()
    assert set(fx.handler_forensics()) == set(fx.HEARTBEAT_FIELDS[-3:])
    assert fx._handler_label_tail(None) == "?" and fx._handler_label_tail("///") == "?"
    # 未绑定会话目录时 enter 也照记不误（生产里 preflight 之前就有 handler），只是那一拍不写盘
    fx.handler_roundtrip_enter("/wapi/zpgeek/friend/add.json")
    assert fx.handler_forensics()["handlers_inflight"] == 2
    assert list(tmp_path.iterdir()) == []

    # 兜底网的**可达性**也钉一下（否则「绝不抛」只是一句声称）：`str(frag)` 自己抛、
    # 以及状态被写成非数字时，enter/exit 仍然只吐一行拍点、不改计数、不外抛。
    class _BadStr:
        def __str__(self):
            raise RuntimeError("no repr")

    fx.handler_roundtrip_enter(_BadStr())
    assert fx.handler_forensics()["handlers_inflight"] == 2, "计数那一步没做成就不许假装 +1 了"
    monkeypatch.setattr(fx, "_HANDLER_INFLIGHT", "不是数字")
    fx.handler_roundtrip_exit()  # `> 0` 比较直接抛：吞掉，label 退化成 `handler:?:exit`
    monkeypatch.setattr(fx, "_HANDLER_INFLIGHT", 7)
    fx.handler_roundtrip_exit()
    assert fx.handler_forensics()["handlers_inflight"] == 6, "正常路径照旧 -1（兜底网没把实现也废掉）"


def test_net_probe_switch_shares_the_wedge_dump_value_table(monkeypatch):
    """出网探针开关（#56 取证 B）与取栈开关用**同一张**布尔表（默认开、关只认 0/off）：
    对照表真源在 `_limits.py`，这里只钉方向——关掉它是有代价的（marker 里那个字段落 None），
    所以拼错的值必须仍然按开，而不是静默把取证关掉。"""
    monkeypatch.setenv(fx.NET_PROBE_ENV, "diabled")
    assert fx._net_probe_enabled() is True
    monkeypatch.setenv(fx.NET_PROBE_ENV, "off")
    assert fx._net_probe_enabled() is False
    monkeypatch.setenv(fx.NET_PROBE_ENV, "0")
    assert fx._net_probe_enabled() is False
