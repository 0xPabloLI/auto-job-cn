"""全仓测试隔离兜底（2026-09-24 熔断 fixture 泄漏事故跟进）。

共享夹具 `_stop_session` 的隔离桩只护走它的熔断用例；这道 autouse 把
AUTOJOB_LOG_PATH 重定向兜到全仓级别——任何用例（含未来绕过夹具新写的）都
写不到真实 private/data/app.log。notify 不在此全局吞：断言通知内容的用例
自行打桩捕获，全局吞掉会掩盖「谁该发通知」的行为差异。

#56 A4 同族问题：live_apply 的楔死取栈用 `faulthandler.dump_traceback_later`，那是
**全进程单例**——一个用例经心跳武装的定时器会活过用例边界，在随机时刻往别的会话目录
落假栈并拖慢全仓。故这里把开关 `LIVE_WEDGE_DUMP` 默认关掉（开关关闭时代码零 faulthandler
调用），并在每个用例后无条件 cancel 一次兜住「别的模块/未来用例留下的定时器」。
专门验证取栈的用例（test_live_apply.py 的 #56 A4 段）自行显式打开开关。

#56 取证 A/B 另加两条同族隔离，见下面 `_isolate_handler_forensics` 的 docstring
（监听器注册表的跨用例泄漏 + 看门狗出网探针默认会真打 DNS/TCP）。
"""

import faulthandler
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _forensics  # noqa: E402  #56 取证 A：状态归属模块本体（复位打在这上面，理由见下）


@pytest.fixture(autouse=True)
def _isolate_real_log(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOJOB_LOG_PATH", str(tmp_path / "app.log"))


@pytest.fixture(autouse=True)
def _isolate_wedge_dump(monkeypatch):
    """楔死取栈（#56 A4）默认关 + 用例后无条件取消进程级定时器。"""
    monkeypatch.setenv("LIVE_WEDGE_DUMP", "off")
    yield
    try:
        faulthandler.cancel_dump_traceback_later()
    except Exception:  # noqa: BLE001  # teardown 不得抛：取消失败交给下一个用例兜
        pass


@pytest.fixture(autouse=True)
def _isolate_handler_forensics(monkeypatch):
    """#56 取证 A 的注册表复位 + #56 取证 B 的出网探针默认关（两条都得全仓级兜，理由各不相同）。

    · 注册表按**对象身份**存 handler 闭包，闭包一路引用着 container 与 page 替身：不清就是跨用例
      泄漏，而 `handlers_live` 是取证 A 唯一的读数，被别的用例抬高就全盘不可信。复位调归属模块的
      `_reset_handler_forensics`，**打在 `_forensics` 本体**上——打在 `live_apply` 的别名上是
      静默 no-op（该模块 docstring 与本仓那条专门钉 no-op 的用例都写着这条）。
    · **两代**看门狗（`_watchdog_main` 与 `_heartbeat_watchdog_main`）的默认探测都会真打
      DNS/TCP/HTTPS（2026-10-09 起 HTTP 段走 `http.client` 真取一次站点内容），而既有看门狗用例走的
      就是击杀路径 ⇒ 不兜这道，全仓会冒出一堆真实网络调用（CI 机器上还白等几秒）。关掉后 marker 里
      那个字段落 None；要验探测的用例自己开开关并注入假件（零网络）。开关名用字面量（与
      `_forensics.NET_PROBE_ENV` 同值，形态同上面的 `LIVE_WEDGE_DUMP`）。"""
    monkeypatch.setenv("LIVE_NET_PROBE", "off")
    yield
    _forensics._reset_handler_forensics()
