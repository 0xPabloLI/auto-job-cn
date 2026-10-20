#!/usr/bin/env python3
"""
no_jd 归因段（#56 A5②）：一次「没拿到 JD 正文」按容器证据分成三类，另给两条独立分流读数。

#57 第二轮从 `live_apply.py` **原样搬入**（纯搬迁，行为不变）：段落注释、函数体、签名默认值与
docstring 逐字平移，不改写、不精简、不重排、不改名。调用点与 `scripts/test_live_apply.py` 的断言
仍打在 `live_apply` 命名空间上——由 `live_apply.py` 顶部的 `from _no_jd import ... as ...` 别名供给，
**本模块不是新的公开 seam**：编排层、补投台账（`apply_retry_outcome`）与报告读数都经 `live_apply`
用它，别绕过别名依赖本模块的内部名。

**别名接线的 no-op 面（#59 修复轮 R4-8，口径同 `_forensics` 顶部那段）**：别名只是把同一个函数
对象再绑一个名字，**改绑 `live_apply` 上的别名，对本模块内部的互相调用是静默 no-op**——
`classify_no_jd` 调 `_dom_side_has_no_body`、后者调 `no_jd_dom_shape`，走的是本模块自己的 globals，
不看 `live_apply` 那份。所以想 stub 掉归因链里的**某一环**，用例必须打在**归属模块**上
（`_no_jd.no_jd_dom_shape`）；打在 `live_apply` 别名上只会静默失效、测试照常绿。
本模块无常量以外的可变状态，`_no_jd` 的符号都可安全地经 `live_apply` 别名读。

判据语义（论证在各函数 docstring 里，这里只做索引、不复述，以免两处漂）：
- 三类归因（`NO_JD_CLASSES`）只回答「该不该保留补投机会」：`detail_roundtrip_failed`（监听器取响应体
  时协议往返抛异常＝站点抖动）/ `detail_no_response`（没拿到可用响应，既含「压根没回」也含接口回非 0 码）/
  `dom_xhr_both_empty`（XHR 与 DOM 两侧都确实没有正文）。
- 两条独立分流读数不新增类别、也不改台账政策：`no_jd_dom_shape` 的 DOM 四形态（`NO_JD_DOM_SHAPES`＝
  `zero_hits` / `hits_empty` / `hits_text` / `unknown`）、`no_jd_detail_code` 的非 0 返回码留痕，
  以及 `_no_jd_empty_url` 的落空时刻 URL（供人工分辨 `zero_hits` 的两假说）。
- **纪律（票面点名的误杀防线）**：三类里只有 `dom_xhr_both_empty` 持有「岗位无内容」的正向证据，
  因此也只有它允许把岗位移出补投台账；其余两类与一切「取证缺口／证据不足」的形态都保留补投机会
  （#41/#42 既定裁决：漏投比错投代价高）。取证没拿到的东西不许冒充任何一种读数——`unknown` 不等于
  `zero_hits`，宁缺毋造的口径同 `platforms/chat_first.py::_record_empty_detail`。

依赖取最小集：JD 正文的取值链仍是 `platforms.chat_first.detail_post_description`（单一真源，本模块不重写
第二份、不另猜 key）；**不 import `live_apply`**（避免循环 import）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from platforms.chat_first import detail_post_description  # noqa: E402  JD 正文取值链单一真源（与 `grab_detail` 同一条链）

# --- #56 A5②：no_jd 按证据三类归因（票面「no_jd 与楔死是同一处站点两面」）---
#
# 判据数据源就是 A5① 落的容器字段，不再靠「岗位疑已失效」这种未经证据的定性：
#   detail_roundtrip_failed —— 容器里有 errors：监听器里那次 `resp.json()` 协议往返**抛异常**了
#   dom_xhr_both_empty     —— 容器有 data 且**两侧都没有正文**：XHR 侧按 `grab_detail` 实际取值的
#                             那条链（`detail_post_description`）取不到 JD 正文，DOM 侧的命中探测
#                             也有证据且无正文（零命中 / 命中皆空）。这是唯一持有「岗位无内容」
#                             正向证据、也是唯一允许移出台账的类（修复轮 3 R1：光看「容器里有 data」
#                             会把已经拿到正文的岗判成双空并永久丢岗）
#   detail_no_response     —— 容器既无 data 也无 error：没拿到可用响应。**这一类里混着两种形态**
#                             （接口回了非 0 码 / 压根没回），类别不区分、由下面的 detail_code 读数区分
# 类别只回答「该不该保留补投机会」；票面验收 4 要分流的两个假说（「站点的往返失败/挂起」vs
# 「详情页形态漂移」）由两条**独立分流读数**回答（修复轮 F1）：
#   dom_shape    —— DOM 侧形态：选择器命中数为 0 只说明探测一条都没中——**形态漂移与「人已离岗
#                   被弹回列表」都可能**（#33 第八轮既定事实：分栏视图 `/web/geek/jobs?query=…`
#                   的 URL 不含 job_detail，弹回列表同 URL；两假说靠落空 url 与 C 段栈分辨，
#                   判据边界见 `no_jd_dom_shape` 的 docstring）；探测缺失/异常＝unknown，
#                   绝不拿取证缺口冒充 0 命中（`platforms/chat_first.py::_record_empty_detail` 已立的
#                   诚实性口径，与 #43/#53 同族）
#   detail_code  —— 非 0 返回码的留痕（code + message 截断）：把「接口自己报了错」从
#                   「站点压根没回」里分出来，同时进结果条目、事件与 report
# 两条读数都不新增类别、不改台账政策（`apply_retry_outcome` 只看类别）。

NO_JD_ROUNDTRIP_FAILED = "detail_roundtrip_failed"
NO_JD_NO_RESPONSE = "detail_no_response"
NO_JD_BOTH_EMPTY = "dom_xhr_both_empty"
NO_JD_CLASSES = (NO_JD_ROUNDTRIP_FAILED, NO_JD_NO_RESPONSE, NO_JD_BOTH_EMPTY)
# 人读短名（终端打印与 [done] 行用）与台账长口径分开：短名要能塞进一行里逐个念，
# 长口径要说清「凭什么这么归」。两者都只有一处定义，避免措辞漂移。
# 三个类别名保持票面原话（修复轮改的是判据与读数，不是类别名）。
NO_JD_CLASS_SHORT = {
    NO_JD_ROUNDTRIP_FAILED: "详情往返失败",
    NO_JD_NO_RESPONSE: "详情超时未回",
    NO_JD_BOTH_EMPTY: "DOM 与 XHR 双空",
}
NO_JD_CLASS_DESC = {
    NO_JD_ROUNDTRIP_FAILED: "详情往返失败（监听器取响应体时协议往返抛异常——站点抖动，岗位仍可能有效）",
    NO_JD_NO_RESPONSE: "详情超时未回（等满 XHR 窗口没拿到可用响应：既含「压根没回」也含接口回非 0 码，"
    "两者靠 no_jd 的 detail_code 读数分开；站点侧问题，岗位仍可能有效）",
    NO_JD_BOTH_EMPTY: "DOM 与 XHR 双空（详情接口 code=0 的响应已到、按 grab_detail 的同一条取值链"
    "拿不到 JD 正文，DOM 侧的命中探测也有证据且同样没有正文——岗位无内容的正向证据；"
    "DOM 选择器命中数另见 dom_shape 读数）",
}

# DOM 形态读数：来自 grab_detail 落空出口的那一次选择器命中探测（`empty_outcome`）。
# 「没拿到正文」有完全不同的两种成因——选择器一条都不存在（页面形态漂了）与
# 选择器存在、元素里确实没字（岗位真无内容），旧实现把两者一起折进「超时未回＝站点抖动」。
# 零命中**不单指漂移**（修复轮 4/E）：分栏视图 URL 不含 job_detail，人已离岗被弹回列表时
# 探测同样零命中；两假说靠落空 url（`no_jd_empty_urls`）与 C 段栈分辨，见 `no_jd_dom_shape`。
DOM_SHAPE_ZERO_HITS = "zero_hits"
DOM_SHAPE_EMPTY_HITS = "hits_empty"  # 命中了，但命中元素正文皆空
DOM_SHAPE_TEXT_HITS = "hits_text"  # 命中且有 >20 字文本——落空时刻与探测时刻不一致，如实标出
DOM_SHAPE_UNKNOWN = "unknown"  # 探测缺失或探测异常＝取证缺口，不许冒充以上任何一种
NO_JD_DOM_SHAPES = (DOM_SHAPE_ZERO_HITS, DOM_SHAPE_EMPTY_HITS, DOM_SHAPE_TEXT_HITS, DOM_SHAPE_UNKNOWN)
DOM_SHAPE_SHORT = {
    DOM_SHAPE_ZERO_HITS: "DOM 零命中（形态漂移或已离岗弹回）",
    DOM_SHAPE_EMPTY_HITS: "DOM 命中但正文空",
    DOM_SHAPE_TEXT_HITS: "DOM 命中且有正文",
    DOM_SHAPE_UNKNOWN: "DOM 探测无证据",
}
DETAIL_CODE_MESSAGE_CAP = 80  # 非 0 返回码的 message 截断长度（风控文案可能整页 HTML）


def _no_jd_class_label(jd_class):
    """归因类的终端短名；类别缺失/未知照实说「归因未知」，不粉饰也不静默丢（与
    `apply_retry_outcome` 的保守方向配对：读不出类别时既不谎报站点抖动、也不谎报岗位无内容）。"""
    return NO_JD_CLASS_SHORT.get(jd_class, "归因未知")


def _dom_shape_label(dom_shape):
    """DOM 形态读数的人读短名；读不出时照实说「DOM 探测无证据」，不猜成零命中。"""
    return DOM_SHAPE_SHORT.get(dom_shape, DOM_SHAPE_SHORT[DOM_SHAPE_UNKNOWN])


def _no_jd_keep_phrase(jd_class):
    """台账保留行要说的那句话。**只有真拿到「站点抖动」证据（往返失败 / 超时未回）才这样讲**；
    归因缺失时写「证据不足，保守保留」——保留的理由是「不能证明岗位无内容」，
    不是「已知站点在抖」。两条都会落进同一句播报里混起来说，就是拿猜测冒充证据。"""
    if jd_class in (NO_JD_ROUNDTRIP_FAILED, NO_JD_NO_RESPONSE):
        return f"{_no_jd_class_label(jd_class)}＝站点抖动、岗位仍有效"
    return "归因未知＝证据不足，保守保留"


def classify_no_jd(container):
    """按容器证据给一次 no_jd 归类（纯函数，票面三类，判别顺序即优先级）。
    为什么往返失败优先于其他两类：一次抛异常就是「这一处无超时往返失败」的直接证据，
    而 data 缺失只是它的下游结果——把它归到「双空」会给站点抖动扣上「岗位已失效」的帽子，
    正是票面点名的误杀。
    为什么「双空」要求**两侧都确实没有正文**（修复轮 3 R1 收窄）：
    - XHR 侧：`data` 有值，且按 `grab_detail` 实际取值的那条链（`detail_post_description`，
      单一真源，编排层不再自己猜 key）取不到 JD 正文；
    - DOM 侧：`no_jd_dom_shape` 读出「探测有证据且无正文」——零命中（选择器不存在）或
      命中皆空（元素在但没字）。
    只有这两条同时成立，才配得上「这个岗位没内容」这句定性——也是**唯一允许把岗位移出台账**
    的证据形态。旧写法只看 `container.get("data")` 的真值，于是「带正文的 code=0 响应 + 探测
    零命中」（票面「详情页形态漂移」那一侧）也被判双空：grab_detail 明明拿到了正文，岗却被
    当成「无内容的正向证据」永久丢掉，`no_jd_classes[双空]` 同时被形态漂移的岗污染，验收 4
    的分流被推向票面另一侧。
    为什么不满足以上任一条就退回 `detail_no_response`（保留补投机会）：
    - 有正文 ⇒ XHR 侧根本不是空，落空侧的读数由 `dom_shape=zero_hits` 如实报出
      （形态漂移或人已离岗弹回列表，两假说并列——判据边界见 `no_jd_dom_shape`）；
    - 探测缺失/异常（`dom_shape=unknown`）⇒ 取证缺口，不许凭空造出「双空」这句定性
      （`platforms/chat_first.py::_record_empty_detail` 的宁缺毋造同口径）；
    - 探测反而读出正文（`dom_shape=hits_text`，落空时刻与探测时刻不一致）⇒ 「两侧都空」不成立。
    为什么 `code != 0` 与「完全没回」同归 `detail_no_response`：
    - 类别只管「给不给补投机会」，这两者的机会一样（都没拿到可用响应，岗位仍可能有效）；
    - 非 0 不是「往返抛异常」，写进 errors 会把它误报成假说一的证据量；
    - 它也不是「响应到了、两侧都没有正文」，归双空就是拿一次风控/失效返回码丢一个可补投的岗；
    - 两者靠 **`no_jd_detail_code`** 这条独立读数分开：`capture_xhr` 在非 0 分支留一份
      `container["nonzero"]`（code + message 截断），report 的 `no_jd_code_nonzero_by_class`
      即它的**按类交叉**计数（修复轮 4/B：扁平总数读不出这条非 0 属于哪一类）。
    边界形态一律保守归 `detail_no_response`（留在补投台账的那一类）：
    - 容器不是字典 / 缺 `data` 与 `errors` 键（桩页面、旧形态容器、被替换的 grab_detail）：
      「没有任何证据说这个岗位无内容」，而双空是唯一会丢岗的类——无证据就不许丢岗
      （#41/#42 既定裁决：漏投比错投代价高）。
    不静默丢：归类结果恒为三类之一，并随结果条目、事件与 report 读数落盘。"""
    if not isinstance(container, dict):
        return NO_JD_NO_RESPONSE
    errors = container.get("errors")
    if isinstance(errors, list) and errors:
        return NO_JD_ROUNDTRIP_FAILED
    data = container.get("data")
    if data and not detail_post_description(data) and _dom_side_has_no_body(container):
        return NO_JD_BOTH_EMPTY
    return NO_JD_NO_RESPONSE


def no_jd_dom_shape(container):
    """DOM 侧形态读数（纯函数，四类之一）：读 `grab_detail` 落空出口的命中探测。
    为什么必须单独成一条读数：票面头号嫌疑是「详情页形态漂移」，而它的直接证据就是
    「选择器一条都没命中」——过去这份证据只进事件、从不参与分流，于是「往返没回 + 选择器
    全落空」与「往返没回但 DOM 有元素」读起来是同一个「站点抖动」，验收 4 就分不了流。
    **诚实边界**（`platforms/chat_first.py::_record_empty_detail` 已立、此处不破）：探测缺失、
    探测异常、命中数不是整数，一律 `unknown`——「取证没拿到」不等于「命中 0 次」。
    **判据边界（修复轮 4/E）**：`zero_hits` 只报「探测一条都没中」这一事实，**不单指形态漂移**
    ——#33 第八轮既定事实：分栏视图 `/web/geek/jobs?query=…` 的 URL 不含 `job_detail`，人已
    离岗被弹回列表时探测同样零命中；要当场分开就得再取右栏标题（又一次无超时 `page.evaluate`，
    本票正在治这类站点，禁止新增），故此处按「如实降精度」固定两假说并列，由落空 url
    （`_no_jd_empty_url` → 结果条目 `empty_url` / report `no_jd_empty_urls`）与 C 段楔死栈
    在人工判读时分。"""
    if not isinstance(container, dict):
        return DOM_SHAPE_UNKNOWN
    probe = container.get("empty_outcome")
    if not isinstance(probe, dict) or not probe or probe.get("probe_error"):
        return DOM_SHAPE_UNKNOWN
    hits = probe.get("hit_total")
    if isinstance(hits, bool) or not isinstance(hits, int):
        return DOM_SHAPE_UNKNOWN
    if hits == 0:
        return DOM_SHAPE_ZERO_HITS
    nonempty = probe.get("nonempty_total")
    if isinstance(nonempty, int) and not isinstance(nonempty, bool) and nonempty > 0:
        return DOM_SHAPE_TEXT_HITS
    return DOM_SHAPE_EMPTY_HITS


def _dom_side_has_no_body(container):
    """「双空」的 DOM 侧那一半：探测**有证据**、且证据里没有正文。
    零命中（选择器不存在）与命中皆空（元素在但没字）在这件事上回答一致，都算「DOM 侧无正文」；
    `unknown`（探测缺失/异常）与 `hits_text`（探测反而读出正文）都不算——前者是取证缺口，
    不许凭空造出一句「岗位无内容」的定性（`platforms/chat_first.py::_record_empty_detail` 的宁缺毋造
    同口径）；后者让「两侧都空」根本不成立。判据只这一处，与 DOM 侧形态读数
    （`no_jd_dom_shape`）共用同一个答案，不在类别里另立一套读法。"""
    return no_jd_dom_shape(container) in (DOM_SHAPE_ZERO_HITS, DOM_SHAPE_EMPTY_HITS)


def no_jd_detail_code(container):
    """非 0 详情返回码的留痕（`capture_xhr` 的 code!=0 分支写的 `container["nonzero"]`）。
    有则返回 {"code", "message"}，没有则 None——`code == 0` 与「完全没回」都不写它，
    这正是它把 `detail_no_response` 那一类劈成两半的作用。"""
    if not isinstance(container, dict):
        return None
    rec = container.get("nonzero")
    if not isinstance(rec, dict) or "code" not in rec:
        return None
    return {"code": rec.get("code"), "message": str(rec.get("message") or "")[:DETAIL_CODE_MESSAGE_CAP]}


def _no_jd_empty_url(container):
    """落空时刻的页面 URL（`platforms/chat_first.py::_record_empty_detail` 已写进 `empty_outcome.url`）。
    为什么必须随读数一起落盘：#33 第八轮既定事实——分栏视图 `/web/geek/jobs?query=…` 的 URL
    **不含 `job_detail`**，人已离岗被弹回列表也正是这个 URL；`zero_hits` 因此不是「形态漂移」
    的单义证据（两假说并列，判据边界见 `no_jd_dom_shape` 的 docstring）。要当场分开就得再取
    一次右栏标题（又一次无超时 `page.evaluate`，本票正在治这类站点，禁止新增），所以只如实落
    url、降精度不分流——人工判读（配合 C 段栈）才分。取证缺口（无探测 / 无 url）返回空串。"""
    if not isinstance(container, dict):
        return ""
    probe = container.get("empty_outcome")
    if not isinstance(probe, dict):
        return ""
    return str(probe.get("url") or "")[:300]


def _no_jd_signal_readings(container):
    """一次 no_jd 的分流读数（结果条目、事件、report 三处同源，同一判据）：DOM 形态 +
    非 0 返回码留痕 + 落空时刻 URL（修复轮 4/E：零命中两假说并列，url 是人工判读的锚）。"""
    return {
        "dom_shape": no_jd_dom_shape(container),
        "detail_code": no_jd_detail_code(container),
        "empty_url": _no_jd_empty_url(container),
    }


def _no_jd_class_stats(results):
    """#56 验收 4 的分流读数：本场 no_jd 的归因类分布（进 report.json）。
    三类恒在（0 也写出来）——「双空 0 个」与「没这个读数」含义不同：首轮真实投递后
    「A1/A5 落地后 no_jd 是否显著下降、各类占比如何」要**读报告就能分流**，不再翻日志。
    类别缺失/异常的结果条目归入 `unknown` 键（只在真出现时才存在），不静默丢。"""
    stats = dict.fromkeys(NO_JD_CLASSES, 0)
    for r in results:
        if r.get("status") != "no_jd":
            continue
        cls = r.get("jd_class")
        if not isinstance(cls, str) or not cls:
            cls = "unknown"
        stats[cls] = stats.get(cls, 0) + 1
    return stats


def _no_jd_shape_stats(results):
    """#56 验收 4 的两条分流读数（修复轮 F1 起，进 report.json，与 `no_jd_classes` 并排）：
    · `no_jd_dom_shapes`＝no_jd 的 DOM 形态分布（四类恒在，0 也写出来，同 `no_jd_classes` 口径）
      —— **`zero_hits` 高＝详情页落空一侧**（形态漂移或人已离岗弹回列表，两假说并列，修复轮 4/E；
      靠 `no_jd_empty_urls` 与 C 段栈分辨），票面「不降则独立开票」的判据就读在这里；
    · `no_jd_code_nonzero_by_class`＝带非 0 详情返回码的 no_jd **按归因类交叉计数**
      （修复轮 4/B：三类恒在、0 也写出，类别缺失进 unknown 桶）——非 0 只回答「接口自己报了错」，
      **属于哪一类、是不是同一个容器**必须由交叉桶回答：同一容器可先后被写 `data`（code=0 命中）
      与 `nonzero`（随后一次非 0），扁平总数把这些形态读成同一个数，旧注释还断言
      「两者类别相同」——一句对实现没有依据的承诺。按类交叉后读报告即可分流。
    读数只统计 no_jd 条目；旧结果条目/桩读不出形态时落 unknown，不静默丢、也不猜成零命中。"""
    shapes = dict.fromkeys(NO_JD_DOM_SHAPES, 0)
    nonzero_by_class = dict.fromkeys(NO_JD_CLASSES, 0)
    for r in results:
        if r.get("status") != "no_jd":
            continue
        shape = r.get("dom_shape")
        if not isinstance(shape, str) or not shape:
            shape = DOM_SHAPE_UNKNOWN
        shapes[shape] = shapes.get(shape, 0) + 1
        if isinstance(r.get("detail_code"), dict):
            cls = r.get("jd_class")
            if not isinstance(cls, str) or not cls:
                cls = "unknown"
            nonzero_by_class[cls] = nonzero_by_class.get(cls, 0) + 1
    return shapes, nonzero_by_class


def _no_jd_empty_urls(results):
    """no_jd 落空时刻的 URL 读数（修复轮 4/E，进 report.json）：`_record_empty_detail` 已把
    落空时刻的页面 URL 写进容器、`_no_jd_signal_readings` 已把它带进结果条目——report 再给一条
    **一眼可见**的读数，人工判读不必翻 results 数组逐个对照。为什么是 url：#33 第八轮既定事实
    （分栏视图 `/web/geek/jobs?query=…` 的 URL 不含 job_detail，人已离岗被弹回列表同 URL），
    `zero_hits` 的两假说要靠它（与 C 段楔死栈）事后分辨。只收非空 url；老形态容器 / 取证缺口
    不臆造条目（url 空就空着，不拿占位符冒充位置）。"""
    out = []
    for r in results:
        if r.get("status") != "no_jd":
            continue
        url = r.get("empty_url")
        if isinstance(url, str) and url:
            out.append({"idx": r.get("idx"), "job": r.get("job"), "dom_shape": r.get("dom_shape"), "url": url})
    return out
