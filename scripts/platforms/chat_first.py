"""站内平台实现（chat-first）：DOM 选择器与页面流程自 crawl_jds.py / batch_apply_v2.py /
followup_tracker.py 平移（行为不变）。编排逻辑（限速、每日上限、重登驱动）留在脚本侧。"""

import random
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from . import PlatformBase
from _forensics import (  # noqa: E402  #56 取证 A：handler 往返计数的登记与往返前后的拍点（状态归 `_forensics`）
    # 与「造拍一律由调用方注入 `beat_fn`」那条分层口径（见 `_no_beat`）的关系——**这条口径从本批起
    # 只对业务拍点成立**：`beat_fn` 默认 None = 业务节拍一条没多，一字未动；而 `enter`/`exit` 各自
    # 那一拍是**取证拍点**，由 `_forensics` 自己落，不走注入。为什么不能等 `beat_fn`：卡死时主线程
    # 再也不拍点，「进入往返之前那一刻」必须由还活着的 handler 自己写下来（口径见
    # `_forensics.handler_roundtrip_enter`）——绕一圈经调用方注入等于把这条取证做成不可用。
    # 两条链路的差别只在有没有会话目录：`followup_tracker.py` 从不 import live_apply、也就从不
    # `_heartbeat_bind` ⇒ `_HEARTBEAT_DIR` 为 None ⇒ 那两拍直接 no-op（`_heartbeat_beat` 吞掉一切），
    # 既不写文件也不抛，计数照记在本进程里；live_apply 会话内的收尾采集则会真写这两拍。
    # 解析可用已由实测确认：`platforms/__init__.py` 先把 scripts/ 挂进 sys.path，本模块才被导入。
    handler_roundtrip_enter,
    handler_roundtrip_exit,
    listener_attached,
    listener_detached,
)

LOGIN_DIALOG_SELECTOR = ".boss-login-dialog, .boss-login-dialog-mask"
# 整页登录变体（2026-10-20 实测）：掉登录时会被导到 `https://www.zhipin.com/web/user/`，
# 页面标题「BOSS直聘注册登录」——**这个形态没有 `.boss-login-dialog` 元素**，只查弹窗选择器
# 会漏判：上层把「未登录」当成正常页面，于是搜索拿不到结果、会话列表采到 0 条，
# 再以「站点抖动/采集残缺」的名义降级。两个探针（搜索框诊断、入站覆盖）都因此产出过假读数。
LOGIN_PAGE_URL_FRAG = "/web/user"
LOGIN_PAGE_TITLE_MARKERS = ("注册登录",)


def is_logged_out(page, login_dialog_selector=LOGIN_DIALOG_SELECTOR):
    """登录态判据（三条并判，单一真源）：
    ① 登录弹窗（旧形态，保持既有语义）；
    ② URL 落在 `/web/user`（整页登录，弹窗元素不存在）；
    ③ 页面标题含「注册登录」（URL 形态再漂移时的兜底）。
    读页面属性抛异常时退回只判①（不把一次读失败伪装成「登录过期」）。"""
    try:
        if page.query_selector(login_dialog_selector):
            return True
        url = page.url or ""
        title = page.title() or ""
        return LOGIN_PAGE_URL_FRAG in url or any(m in title for m in LOGIN_PAGE_TITLE_MARKERS)
    except Exception:
        return False


JOB_SEARCH_URL = "https://www.zhipin.com/web/geek/job"
JOB_DETAIL_URL = "https://www.zhipin.com/job_detail/{eid}.html"
CHAT_URL = "https://www.zhipin.com/web/geek/chat"


def chat_url_for(user_id):
    """聊天页深链（#73）：`/web/geek/chat?id=<招聘者 id>`。

    `?id=` 收的是**招聘者**（会话对端）的 id，不是 encryptJobId：`live_apply._in_chat_form`
    里 #4 峰云岗的实测形态就是 `/web/geek/chat?id=<招聘者用户 id>`。调用方传 wapi 条目的
    `encryptUid`（缺失时退 `encryptBossId`）——这两种是 #73 采用的深链来源；数字 `uid` 不参与
    判据（真机 2026-10-21：站内导航后 SPA 会把 query 规范化掉，落地 URL 连 `?id=` 都不留，
    「打开的是谁」由 `reply_inbound` 读右侧会话头判）。
    id 是站点给的串（可能含 `/`、`+`、`=`），必须整体 quote：裸拼会把 query 拆坏，落地成
    另一个人的会话或空页，而那种失败在页面上看起来与「这条对话不存在」一模一样。"""
    return f"{CHAT_URL}?id={quote(str(user_id), safe='')}"


# 会话数据源（#30/#21 探测结论，内部调研笔记目录（未随本仓分发）boss-chat-card-shape.md §3）：
# 聊天页加载即请求的 wapi——拦截响应拿结构化会话列表（含 encryptJobId/毫秒时间戳/fromId）
FRIEND_LIST_URL_FRAG = "/wapi/zprelation/friend/getGeekFriendList.json"
USER_INFO_URL_FRAG = "/wapi/zpuser/wap/getUserInfo.json"

# --- `followup()` 的等待算术（#59 修复轮 R4-1：这四个常量是**取证阈值的对表输入**，不是节奏参数）---
# 聊天页 goto 上界：本仓列表页/详情页的 goto 一律 30000（见 `live_apply.search_list` /
# `enter_detail`），聊天页按「比列表页重」给 2 倍余量取 60000——90000 会让「goto 上界 + 固定停顿」
# 这一段合法静默（96s）顶穿 `_forensics.WEDGE_STACK_SECONDS`（90s），健康但慢的页面给自己落假栈。
CHAT_GOTO_TIMEOUT_MS = 60000
FOLLOWUP_POST_GOTO_SLEEP_SECONDS = 6  # goto 后等页面水合 + wapi 首批的固定停顿（在两次 goto 拍点之间）
FRIEND_POLL_ROUNDS = 50  # 采集窗口轮数（扫满不定稿，见 followup 内注释）
FRIEND_POLL_INTERVAL_MS = 300  # 每轮等待间隔：逐轮拍点把这段的无心跳上界压到间隔本身

# 崩溃/异常现场存档（#9 E2E：点击超时无证据可查）：截图 + HTML + URL，供人工/大模型复核
CRASH_DIR = Path(__file__).resolve().parent.parent.parent / "private/data/crash"


def _no_beat(_label):
    """beat_fn 的默认替身（#56 A3）：不注入节拍时每次拍点就是一次纯函数调用——
    「默认 None = 零行为变更」这条保证的实现体。平台层不自己造**业务**拍（#56 取证 A 起本文件确实
    import 了 `_forensics`，但那只负责 handler 往返前后的两拍**取证**拍点，与本替身无关：
    两条线的分工与「为什么取证拍点不能也走注入」写在文件顶部那条 import 的注释里）。"""


def _resp_json_with_forensics(resp, url_frag):
    """一次 `resp.json()` 取体，前后各包一拍 #56 取证 A 的 enter/exit（口径见
    `_forensics.handler_roundtrip_enter`：卡死时「最后一次写盘的状态」必须是进入往返之前那一刻）。
    为什么包成函数而不是在两个端点分支里各展开一遍 try/finally：`followup()` 的 handler 里
    friend/list 与 user/info 各取一次体，展开会把 `followup` 的语句数顶到 ruff 的 50 上限附近，
    而包一层让「只求值一次」这条铁律在形状上就看得见（第二次取体会多一次往返=多一次楔死机会）。
    异常**照原样外抛**：调用方那个 `except Exception: pass` 的口径一字不动。"""
    handler_roundtrip_enter(url_frag)
    try:
        return resp.json()
    finally:
        handler_roundtrip_exit()


def save_crash_evidence(page, tag, job=None):
    """页面异常现场存档：PNG 截图 + 完整 HTML + 当前 URL。
    任何一步失败都不影响主流程（吞异常，只打印路径）。返回证据文件前缀路径；
    一件都没落盘时返回 None。消息逐件列出实际落盘文件——20260926 实测浏览器被外部
    终结时截图/HTML 全失败，旧版无条件打「现场已存: prefix.png/.html」谎报。"""
    try:
        CRASH_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        brand = re.sub(r"[^\w一-鿿]+", "-", str((job or {}).get("brandName") or "unknown"))[:20]
        prefix = CRASH_DIR / f"{stamp}-{brand}-{tag}"
        saved = []
        try:
            page.screenshot(path=str(prefix) + ".png", full_page=False, timeout=8000)
            saved.append(str(prefix) + ".png")
        except Exception as e:
            print(f"    [证据] 截图失败: {e}")
        try:
            html = page.content()
            Path(str(prefix) + ".html").write_text(html, encoding="utf-8")  # #43：str 调 .write_text 曾必失败
            saved.append(str(prefix) + ".html")
        except Exception as e:
            print(f"    [证据] HTML 存档失败: {e}")
        try:
            Path(str(prefix) + ".url.txt").write_text(page.url, encoding="utf-8")  # #43 同根因
            saved.append(str(prefix) + ".url.txt")
        except Exception:
            pass
        if saved:
            print(f"    [证据] 现场已存 {len(saved)} 件: {', '.join(saved)}（URL: {page.url}）")
            return str(prefix)
        print("    [证据] 现场未存档（截图/HTML/URL 全部失败）")
        return None
    except Exception as e:
        print(f"    [证据] 存档失败: {e}")
        return None


def dismiss_overlays(page):
    """关闭遮挡沟通按钮的弹窗/遮罩（风控提示、引导浮层等）：点可见的关闭按钮。
    登录框不在此处理（由上层 login_expired 状态驱动重登）。
    #56 修复轮 3 R4（节拍口径，**只改注释不改行为**）：每个选择器各自包在 try/except 里，
    异常在本函数内部就被吞掉、控制流回到 for——所以调用方 `click_with_retry` 的
    `click:dismiss-overlays:pre/post` **没有**、也不该有配套的 `:error` 拍点（加了会改动与本票
    无关的遮罩层语义）。C 段判栈时这一段这样读：
    · `pre` + `post` 只证明「这一段走完了」，**不证明**关掉过任何遮罩（内部全失败也照样走完，
      返回值 `closed` 才是关掉了几个）；
    · `pre` 无 `post` 只剩两种可能：卡在里面的裸奔 `querySelectorAll`（timeout=None，楔死指纹），
      或 BaseException（如 KeyboardInterrupt）逸出——「普通抛异常」这一种在本函数里已被吞掉，
      不会走到这里。这条残余二义是**已知缺口**（本轮不扩范围去补拍点），实测锚见
      test_platforms 的 `test_dismiss_overlays_swallows_internally_so_its_beat_has_no_error_exit`。"""
    closed = 0
    for sel in [
        ".dialog-close",
        ".modal-close",
        "[class*='dialog'] .close",
        "[class*='modal'] .close",
        ".boss-popup__close",
        ".icon-close",
    ]:
        try:
            for el in page.query_selector_all(sel):
                if el.is_visible():
                    el.click(timeout=2000)
                    closed += 1
                    time.sleep(0.5)
        except Exception:
            pass
    return closed


def human_pointer_walk(page, btn, rng=None, beat_fn=None):
    """点击前指针分段走近（反侦察 #44）：Playwright 的 elementHandle.click 会把鼠标
    **瞬移**到目标中心——真人鼠标是连续轨迹。这里先分 3-6 段走近到目标附近 ±6px
    （每段 40-120ms + 微抖），之后 handle.click 只需最后一小步。
    起点用上一次轨迹的落点（会话内连续），首次随机屏内起点。轨迹失败不阻断。
    #56 A3：`btn.bounding_box()` 与每一次 `page.mouse.move()` 都是 timeout=None 的裸奔
    协议调用（票面机制口径 §1），各调用前后经 beat_fn 拍一次点（label 里显式带 pre/post），
    使「停在哪个协议调用上」不靠栈也能判定。beat_fn 由调用方注入（平台层不知道心跳为何物），
    默认 None = 零行为变更；契约上由**注入方**负责吞异常，本层不为观测层兜底、也不因拍点改变动作。
    #56 修复轮 F4①：整段包在 try/except 里＝有一个「吞掉异常的出口」，过去它留下的 label 与
    「调用没回来」完全一样（都只有 pre）。加一条 `pointer:error` 把两种形态分开——
    C 段判栈时「普通失败」与「楔死」不是一回事。
    #56 修复轮 4（D）按实测把边界写清（`test_human_pointer_walk_beats_its_swallowed_exception`
    钉死序列）：本函数内异常出口不拍 post——抛在哪一步，那一步就只有 `pre` + `pointer:error`
    （post 只证明「该调用正常返回了」）；这条结论**别外推**到 `coordinate_click`——
    那边 scroll evaluate 的异常出口 error 之后 post 照落（见该函数 docstring），两处各按各的读。
    返回值与控制流一字未动。"""
    beat = beat_fn or _no_beat
    rng = rng or random
    try:
        beat("pointer:bounding-box:pre")
        box = btn.bounding_box()
        beat("pointer:bounding-box:post")
        if not box:
            return
        tx = box["x"] + box["width"] / 2 + rng.uniform(-6, 6)
        ty = box["y"] + box["height"] / 2 + rng.uniform(-6, 6)
        sx, sy = _LAST_MOUSE["x"], _LAST_MOUSE["y"]
        if sx is None:
            sx, sy = rng.uniform(100, 900), rng.uniform(100, 500)
        steps = rng.randint(3, 6)
        for i in range(1, steps + 1):
            t = i / steps
            jx = sx + (tx - sx) * t + rng.uniform(-8, 8) * (1 - t)
            jy = sy + (ty - sy) * t + rng.uniform(-8, 8) * (1 - t)
            beat(f"pointer:mouse-move:{i}:pre")
            page.mouse.move(jx, jy)
            beat(f"pointer:mouse-move:{i}:post")
            time.sleep(rng.uniform(0.04, 0.12))
        beat("pointer:mouse-move:aim:pre")
        page.mouse.move(tx, ty)
        beat("pointer:mouse-move:aim:post")
        _LAST_MOUSE["x"], _LAST_MOUSE["y"] = tx, ty
        time.sleep(rng.uniform(0.08, 0.25))  # 真人瞄准后到按下之间有个小停顿
    except Exception:
        # 指针轨迹属锦上添花，任何失败都让位给正常点击（吞异常出口，修复轮 F4①留名）
        beat("pointer:error")


def coordinate_click(page, el, beat_fn=None):
    """#44（2026-09-24 实测定性）：列表分栏视图上 `elementHandle.click` 的 actionability
    序列（scrollIntoViewIfNeeded）**永不返回** → 普通 click 与 force click 都稳定超时，
    而元素几何完全正常（inVp=true、fromPoint 落点是自己、animations=0）。
    实测同张卡片 `page.mouse.click(中心坐标)` **33ms 点动**（事件监听证实 mousedown/
    mouseup/click 全部到达）——坐标点击完全绕开该序列。
    视口外/无 box/异常一律 False 交调用方兜底（对视口外坐标 click 会点到别处，宁可不点）。
    #56 A3：本函数是楔死区间里最密集的一串裸奔调用——两个 `page.evaluate`
    （evaluateExpression / evaluateExpressionHandle，timeout=None）+ `bounding_box` +
    `mouse.click`，逐个前后拍点。中途 bail（无 box / 视口外）不拍后面的点，
    于是「最后一条 label」与「实际走到哪一步」严格一致。beat_fn 契约同 `human_pointer_walk`。
    #56 修复轮 F4①：两个 except 出口各补一条 `*:error` 拍点——「pre 有 post 无」过去同时
    表示「卡在这个调用里」与「这个调用抛了异常、走吞异常的出口」，判据二义会误导 C 段判栈。
    #56 修复轮 4（D）按实测把两个出口写清（`test_coordinate_click_beats_both_error_exits` 钉死）：
    · scroll evaluate 的异常出口＝`pre` + `error` + `post` **三拍全落**——异常在这段内部被吞掉后
      本段继续，所以 `click:evaluate-scroll:post` 只证明「这段执行完了（可能刚抛过异常）」，
      不证明 evaluate 成功、更不证明滚到了位置；
    · 外层 except（→ `click:error` → return False）不补在途子调用的 post——如
      `click:mouse-click:pre` 之后抛异常，直接 `click:error` 收尾（`pre` 无 `post` 时读 error 落没落）。
    bail（无 box / 视口外）是正常出口，**不**落 error 拍点；返回值与控制流一字未动。"""
    beat = beat_fn or _no_beat
    beat("click:evaluate-scroll:pre")
    try:
        page.evaluate("el => el.scrollIntoView({block: 'center'})", el)  # handle.evaluate 实测可用
    except Exception:
        beat("click:evaluate-scroll:error")  # 滚动失败原本就吞掉继续（修复轮 F4①留名）
    beat("click:evaluate-scroll:post")
    try:
        beat("click:bounding-box:pre")
        box = el.bounding_box()
        beat("click:bounding-box:post")
        if not box:
            return False
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        beat("click:evaluate-viewport:pre")
        view = page.evaluate("({w: innerWidth, h: innerHeight})")
        beat("click:evaluate-viewport:post")
        if not (0 <= cx <= view["w"] and 0 <= cy <= view["h"]):
            return False
        beat("click:mouse-click:pre")
        page.mouse.click(cx, cy)
        beat("click:mouse-click:post")
        return True
    except Exception:
        beat("click:error")  # 外层异常出口（返回 False 交调用方兜底）
        return False


def click_with_retry(page, btn, action="点击按钮", beat_fn=None):
    """坐标点击（#44 主路径）→ 普通点击 → 关遮罩重试 → force click 兜底。
    全部失败**返回 False** 交调用方按返回值存证（`live_apply` 里 `no_btn` / `no_send` 那一处）；
    本函数自己不外抛——三步 handle click 的异常都在 except 里收口，只有 BaseException
    （如 KeyboardInterrupt）才逸出。首次点击前做指针分段轨迹（human_pointer_walk）。
    #56 A3：beat_fn 透传给下层（`bounding_box` / `mouse.move` / `mouse.click` 那些裸奔
    调用在那两层里拍），本层只补坐标之外的三步——handle click 一族虽然带 timeout，
    但同样在楔死区间内；而 `dismiss_overlays` 里的 `querySelectorAll` 是 timeout=None
    的裸奔调用，不标出来就只能读到「死在 click_with_retry 里」而答不出哪一步。
    #56 修复轮 F4① 起的 error 拍点覆盖范围（修复轮 3 R4 把口径收窄到与实现一致）：
    · **有** `*:error` 的出口 = 本层三个 handle click（`click:handle:error` /
      `click:handle-retry:error` / `click:handle-force:error`）。这三步带 timeout，
      「抛异常」是它们的**正常出口**（点不动就走下一步），补拍后与「调用没回来」分得开；
    · **没有** `*:error` 的出口 = 只有 `dismiss_overlays` 这一段（异常在它内部逐选择器吞掉，
      本层看到的永远是正常返回，见该函数 docstring；本轮明确**不**给它加拍点，那会改动与本票
      无关的遮罩层语义）。下层 `human_pointer_walk`（`pointer:error`）与 `coordinate_click`
      （`click:evaluate-scroll:error` / `click:error`）各有自己的 error 拍点，其 bail
      （无几何 / 视口外 / 点不动）是正常出口、不落 error，都不计入本层。
    残余二义（已知缺口，不扩范围）：`click:dismiss-overlays:pre` 无 `post` 现在只可能是
    「卡在里面的裸奔调用」或 BaseException 逸出，**不再**与「抛异常被内层吞掉」混在一起
    ——那种形态 `post` 照样会落，读不出来只能靠 `closed` 返回值（当前调用方未消费）。
    阶梯顺序、返回值与 print 一字未动。"""
    beat = beat_fn or _no_beat
    human_pointer_walk(page, btn, beat_fn=beat)
    if coordinate_click(page, btn, beat_fn=beat):
        return True
    beat("click:handle:pre")
    try:
        btn.click(timeout=10000)
        beat("click:handle:post")
        return True
    except Exception:
        beat("click:handle:error")
    beat("click:dismiss-overlays:pre")
    dismiss_overlays(page)
    beat("click:dismiss-overlays:post")
    beat("click:handle-retry:pre")
    try:
        btn.click(timeout=10000)
        beat("click:handle-retry:post")
        return True
    except Exception:
        beat("click:handle-retry:error")
    beat("click:handle-force:pre")
    try:
        btn.click(timeout=8000, force=True)  # 遮挡层仍在但元素可见时强制点
        beat("click:handle-force:post")
        print(f"    [重试] {action} 经 force click 成功")
        return True
    except Exception as e:
        beat("click:handle-force:error")
        print(f"    [失败] {action} 三次尝试均超时: {str(e)[:80]}")
        return False


# issue #36：text=立即沟通 是文本模糊匹配，会命中页面里所有含该文案的元素
# （导航/推荐区/meta），query_selector 取首个——登录态变化后首个命中可能
# 是非交互节点，click 3×8s 超时致台账 0 入账。查找改为专属 class 优先 +
# button/a.btn 文本约束回退（可见性过滤；「继续沟通」是已投递信号，由调用方处理）。
#
# issue #33（第八轮探针实测）：站点职位页现有**两种详情形态，按钮 DOM 完全不同**——
#   ① 独立详情页 `/job_detail/{eid}.html`：`<a class="btn btn-startchat">`，祖先 `div.job-op`
#      （旧版组件，`.job-op .btn-startchat` 实测命中）
#   ② 新版职位页的「左列表 + 右栏详情」分栏视图 `/web/geek/jobs?...`（**URL 不含 job_detail**）：
#      `<a class="op-btn op-btn-chat">`，祖先 `div.job-detail-op` ← `div.job-detail-header`
#      —— `.job-op` / `.btn-startchat` / `[class*='startchat']` 在该形态下**命中数全为 0**，
#      这就是 09-20 起 `no_btn` 的直接原因（只认旧版组件）。
# 两套并存，顺序仍是「专属 class 优先」（两组都是专属 class，非文本兜底）。
COMM_BTN_SELECTORS = [
    ".job-op .btn-startchat",  # ① 独立详情页（旧版组件）
    "a.btn.btn-startchat:has-text('立即沟通')",
    "[class*='btn-startchat']",
    ".job-detail-header .op-btn-chat",  # ② 分栏视图右栏
    "a.op-btn.op-btn-chat",
    "[class*='op-btn-chat']",
]


def find_comm_button(page, beat_fn=None):
    """找可点击的「立即沟通」按钮（#36）：专属 class 优先，回退 button/a.btn 文本约束。

    class 组命中也要求非空文本（挡空文本装饰节点误点）；命中后
    scroll_into_view_if_needed 前置（防 lazy-load 按钮未入视口），滚动失败
    不阻断（click_with_retry 内有 force 兜底）。返回 ElementHandle 或 None。
    #56 修复轮 F4②（A3 漏接点）：每个选择器一次 `query_selector_all`（timeout=None 的裸奔
    协议调用，票面机制口径 §1）前后各拍一次点——查找段过去一个拍都没有，楔死在这里时
    最后一条 label 只能停在上一段的 post。beat_fn 契约同 `human_pointer_walk`（注入方吞异常）；
    默认 None = 零行为变更，**不新增重试、不改选择器顺序**。
    #56 修复轮 4（A5/A6）：① 外层 `except: continue` 的吞异常出口补 `find:comm-btn:query:{n}:error`
    ——「pre 有 post 无」不再同时表示「卡在这个调用里」与「这个调用抛了异常被吞」；
    ② 命中后那次 `page.evaluate(scrollIntoView)`（evaluateExpressionHandle 同族，
    `coordinate_click` 同型调用已拍点）补 pre/error/post：异常出口 error 也落、post 照落
    （滚动失败原本就吞掉继续，post 只证明「这段执行完了」）。
    返回值、查找顺序、重试语义一字未动。"""
    beat = beat_fn or _no_beat
    for n, sel in enumerate(COMM_BTN_SELECTORS + ["button", "a.btn"], 1):
        is_text_fallback = sel in ("button", "a.btn")
        try:
            beat(f"find:comm-btn:query:{n}:pre")
            els = page.query_selector_all(sel)
            beat(f"find:comm-btn:query:{n}:post")
            for el in els:
                if not el.is_visible():
                    continue
                try:
                    text = el.inner_text() or ""
                except Exception:
                    text = ""
                if "继续沟通" in text:
                    continue
                if is_text_fallback:
                    if "立即沟通" not in text:
                        continue
                elif not text.strip():
                    continue
                beat(f"find:comm-btn:scroll:{n}:pre")
                try:
                    # #44（2026-09-24）：scroll_into_view_if_needed 走 Juggler 的
                    # actionability 序列，实测在该页面上会**无超时挂起**（§13.4 根因：
                    # 列表页 >9min 不返回）。改用页面内 JS 滚动——同样达成「让 lazy-load
                    # 按钮入视口」的目的，且异常可按 Playwright 默认超时返回而不卡死。
                    page.evaluate("el => el.scrollIntoView({block: 'center'})", el)
                except Exception:
                    beat(f"find:comm-btn:scroll:{n}:error")  # 滚动失败原本就吞掉继续（修复轮 4 A6）
                beat(f"find:comm-btn:scroll:{n}:post")
                return el
        except Exception:
            beat(f"find:comm-btn:query:{n}:error")
            continue
    return None


def find_continue_button(page):
    """找「继续沟通」按钮（已投递信号）。#36 同型收紧：class + 文本双重约束，
    替代 text= 模糊匹配——误报 skipped 会静默漏投。返回 ElementHandle 或 None。"""
    try:
        el = page.query_selector("[class*='btn-startchat']:has-text('继续沟通')")
    except Exception:
        el = None
    if el and el.is_visible():
        return el
    return None


# --- issue #20：拟人浏览/打字/风控检测 ---

# 风控/验证码弹窗信号：命中即熔断停批（选择器 + 可见文本两路检测）
RISK_SIGNAL_SELECTORS = [
    "[class*='captcha']",
    "[class*='verify-dialog']",
    "[class*='security-check']",
    ".nc-container",
    ".slider-verify",
]
RISK_SIGNAL_TEXTS = ["安全验证", "拖动滑块", "完成验证", "滑块验证", "请完成安全验证"]

# 打字模拟错字池（常用汉字，仅用于退格修正的拟真，不会留在输入框）
_TYPO_POOL = "的一是不了人我在有他这中来上国个到说们为子和你地出道也时年就那要下看"


def detect_risk_signal(page):
    """检测验证码/安全验证弹窗。命中返回描述字符串，否则 None。"""
    for sel in RISK_SIGNAL_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return f"风控弹窗（选择器 {sel}）"
        except Exception:
            pass
    for txt in RISK_SIGNAL_TEXTS:
        try:
            el = page.query_selector(f"text={txt}")
            if el and el.is_visible():
                return f"安全验证提示（{txt}）"
        except Exception:
            pass
    return None


_LAST_MOUSE = {"x": None, "y": None}  # 会话内指针连续性（human_pointer_walk 起点）


def human_scroll(page, px):
    """拟人滚动一段像素：把整段拆成 2-4 个小步（每步 40~200ms 随机停顿），
    模拟滚轮的离散节奏——单次 scrollBy 一把到位是脚本的典型签名（反侦察 #44）。
    仍走 evaluate scrollBy：mouse.wheel 无超时参数，页面 renderer 挂起时会永久阻塞
    （2026-09-12 实测两次死锁，主线程 kevent 挂死零产出）；evaluate 走 Playwright
    默认 30s 超时，超时抛异常由调用方捕获恢复，绝不卡死投递主流程。"""
    steps = random.randint(2, 4)
    done = 0
    for i in range(steps):
        left = px - done
        chunk = left if i == steps - 1 else int(left * random.uniform(0.3, 0.5))
        if chunk > 0:
            page.evaluate(f"window.scrollBy(0, {chunk})")
            done += chunk
            if i < steps - 1:
                time.sleep(random.uniform(0.04, 0.2))


def human_read(page, lo=10, hi=35, rng=None):
    """详情页模拟读 JD：随机停留 lo-hi 秒，期间 1-3 次随机滚动。
    任何滚动失败都不影响主流程。"""
    rng = rng or random
    total = rng.uniform(lo, hi)
    n_scroll = rng.randint(1, 3)
    chunk = total / (n_scroll + 1)
    print(f"    [浏览] 停留 {total:.0f}s，滚动 {n_scroll} 次模拟阅读 JD")
    try:
        for _ in range(n_scroll):
            time.sleep(chunk * 0.6)
            human_scroll(page, rng.randint(300, 900))
            time.sleep(chunk * 0.4)
        time.sleep(chunk)
    except Exception as e:
        print(f"    [浏览] 滚动异常（不影响投递）: {str(e)[:60]}")
        time.sleep(min(total, 5))


def human_type(page, text, delay_lo=40, delay_hi=120, typo_prob=0.05, rng=None):
    """逐字键入（真键盘事件序列，非 fill 整段灌入）：随机键间延迟 + 低概率错字退格修正。
    调用前需已 focus 输入框。返回 (键入字符数, 模拟错字次数)。"""
    rng = rng or random
    typos = 0
    for ch in text:
        if typos == 0 and rng.random() < typo_prob:
            wrong = rng.choice(_TYPO_POOL)
            page.keyboard.type(wrong, delay=rng.randint(delay_lo, delay_hi))
            time.sleep(rng.uniform(0.15, 0.4))  # 发现打错的停顿
            page.keyboard.press("Backspace")
            typos += 1
        page.keyboard.type(ch, delay=rng.randint(delay_lo, delay_hi))
    if typos:
        print(f"    [打字] {len(text)} 字（含 {typos} 次错字退格修正）")
    return len(text), typos


def search_via_input(page, query, rng=None):
    """#20：搜索框逐字输入替代 URL 直拼 query。成功返回 True；
    找不到输入框或输入失败返回 False（调用方回退到 URL 直拼）。"""
    rng = rng or random
    for sel in [
        "input[placeholder*='搜索']",
        ".search-input input",
        "input[name='query']",
        ".ipt-search",
        ".search-box input",
        ".search-input-box input",
    ]:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click(timeout=3000)
                page.keyboard.type(query, delay=rng.randint(60, 180))
                page.keyboard.press("Enter")
                print(f"    [浏览] 搜索框逐字输入 '{query}'")
                return True
        except Exception:
            continue
    return False


# #56 A5：落空时的一次性选择器命中探测（**只在失败退出前跑一次**，成功路径零成本）。
# 为什么轮询答不出、必须另探一次：DOM 轮询的判据是「有没有 >20 字的文本」，它只能告诉
# 「没拿到正文」，告诉不了「选择器根本不存在」还是「选择器存在但正文为空」——而这两者正是
# 票面分流的两条假说（详情页形态漂移 vs 站点往返/内容缺失）。成本口径同 #56 A3 实测
# （一次原子写 ~0.47ms）：一次 evaluate、不进轮询循环，取证不做成性能回归。
_DETAIL_PROBE_JS = """(sels) => sels.map((s) => {
    const out = {selector: s, hits: 0, nonempty: 0};
    try {
        for (const n of document.querySelectorAll(s)) {
            out.hits += 1;
            const t = (n.innerText || '').trim();
            if (t.length > 20) out.nonempty += 1;
        }
    } catch (e) {}
    return out;
})"""


def _record_empty_detail(page, detail_data, selectors, beat, timings):
    """grab_detail 落空 exit 前落证据：一次选择器命中探测 + 等待时长，写进调用方的
    详情容器（`detail_data["empty_outcome"]`）。
    为什么归因走容器字段而不是改返回类型：`grab_detail` 的返回值（str）已被 `_acquire_and_draft_greeting`
    与既有 8 个用例按「空串=没拿到」消费，加第二个出参会把「返回契约一字未动」这条 A3 已经
    守住的边界再破一次；容器本来就是监听器与消费方共享的可变信道（`data`/`errors` 同例）。
    类别判定不在此处做——它在编排层 `classify_no_jd`（平台层不该懂得 no_jd 的台账语义）。
    整体吞异常：取证失败最多退回「零可观测」，绝不能把落空出口变成抛异常的地方（#38 同口径）。"""
    try:
        beat("jd:dom-probe:pre")
        probe = page.evaluate(_DETAIL_PROBE_JS, selectors)
        beat("jd:dom-probe:post")
        if not isinstance(probe, list):
            # 探测结果形态异常时**不写命中数**：`hit_total: 0` 会被下游读成「选择器全落空」
            # 这一「双空」判据的 DOM 侧证据（编排层 `classify_no_jd` 只认有证据的零命中），
            # 而实际只是取证没拿到东西——宁缺毋造（#43/#53 诚实性同族）。
            raise TypeError(f"探测返回形态异常（{type(probe).__name__}），不落命中数")
        hits = [h for h in probe if isinstance(h, dict) and h.get("hits")]
        outcome = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "hit_total": int(sum(int(h.get("hits") or 0) for h in hits)),
            "nonempty_total": int(sum(int(h.get("nonempty") or 0) for h in hits)),
            "hit_selectors": [str(h.get("selector")) for h in hits],
            "selectors": list(probe),
            **timings,
        }
        try:
            outcome["url"] = str(page.url)[:300]  # 落空时刻的 URL（能拿到就写，页面已关则空）
        except Exception as e:  # noqa: BLE001
            outcome["url"] = ""
            outcome["url_error"] = str(e)[:120]
        detail_data["empty_outcome"] = outcome
    except Exception as e:  # noqa: BLE001
        try:
            detail_data["empty_outcome"] = {"probe_error": str(e)[:200], **timings}
        except Exception:  # noqa: BLE001  # 容器不可写（形态诡异）时放弃取证，不往上抛
            pass


def detail_post_description(payload):
    """详情响应 → JD 正文。**这条取值链是全仓唯一真源**（#56 修复轮 3 R2）：
    `grab_detail` 的 XHR 兜底与编排层 `classify_no_jd` 的「双空」判据共用它，编排层不再自己
    重猜 key——「这份响应到底有没有 JD 正文」只允许有一个答案的来源。

    形态不苛求，取不到**字符串正文**一律返回 ""：`zpData` / `jobInfo` 可能是 null
    （岗位失效时 站内真实回 `{"code": 0, "zpData": null}`），`postDescription` 可能缺失或
    不是字符串。旧写法 `data.get("zpData", {}).get(...)` 在 `zpData: null` 上直接
    AttributeError，从 `grab_detail` 逸出（把「返回类型恒为 str」这条 A5 守住的边界破在半路），
    而非字符串正文（如 `123`）会被当成正文返回给起草侧。

    语义（与 `grab_detail` docstring 同一条）：返回 "" ⇔ 这条链上没有可用正文。"""
    if not isinstance(payload, dict):
        return ""
    zdata = payload.get("zpData")
    if not isinstance(zdata, dict):
        return ""
    job_info = zdata.get("jobInfo")
    if not isinstance(job_info, dict):
        return ""
    body = job_info.get("postDescription")
    return body if isinstance(body, str) else ""


def grab_detail(page, detail_data, beat_fn=None):
    """详情 JD：优先 DOM（多选择器+轮询），fallback XHR。
    #56 A3：两段轮询里的 `page.evaluate`（evaluateExpression，timeout=None）与
    `wait_for_timeout` 是票面点名的同族无超时/长停站点，逐轮前后拍点（label 带轮次），
    使「死在 DOM 轮询第几轮 / XHR 空等第几轮」不靠栈也读得出。
    #56 A5：**返回类型仍是 str、契约一字未动**；只在返回空串的那一个出口前落一次
    选择器命中探测与等待时长进容器（`empty_outcome`，见 `_record_empty_detail`），
    把「DOM 轮询 5s 全落空 + XHR 空等 15s」的干等变成有证据的一次退出。
    #56 修复轮 3 R2（口径统一）：XHR 侧「有没有拿到正文」只问 `detail_post_description`
    这一条链，**不再拿「外层 data 非空」冒充「拿到正文」**——于是「返回空串」与
    「XHR 里有可用正文」互斥，落空出口必然带 `empty_outcome` 取证，调用方
    （`_acquire_and_draft_greeting`）不可能收到一份没有 JD 正文的 request_N.json。
    beat_fn 契约同 `human_pointer_walk`——由调用方注入并自行吞异常，默认 None = 零行为变更。"""
    beat = beat_fn or _no_beat
    dom_js = """(sels) => {
        for (const s of sels) {
            const e = document.querySelector(s);
            if (e && e.innerText.trim().length > 20) return e.innerText.trim();
        }
        return '';
    }"""
    selectors = [
        ".job-sec-text",
        ".job-detail .text",
        ".job-detail .job-sec",
        ".job-sec .text",
        ".job-detail .describe",
        ".job-detail-content",
        '[class*="job-sec-text"]',
        '[class*="job-detail"] [class*="text"]',
    ]
    t0 = time.time()
    dom_rounds = 0
    while time.time() - t0 < 5:
        dom_rounds += 1
        beat(f"jd:dom-poll:{dom_rounds}:pre")
        dom = page.evaluate(dom_js, selectors)
        beat(f"jd:dom-poll:{dom_rounds}:post")
        if dom:
            return dom
        beat(f"jd:dom-wait:{dom_rounds}:pre")
        page.wait_for_timeout(500)
        beat(f"jd:dom-wait:{dom_rounds}:post")
    dom_seconds = round(time.time() - t0, 1)
    t0 = time.time()
    xhr_rounds = 0
    while detail_data["data"] is None and time.time() - t0 < 15:
        xhr_rounds += 1
        beat(f"jd:xhr-wait:{xhr_rounds}:pre")
        page.wait_for_timeout(300)
        beat(f"jd:xhr-wait:{xhr_rounds}:post")
    # #56 修复轮 3 R2：判「拿到正文」的唯一口径（旧写法先真值判断外层 data、再在链上落空，
    # 于是 `{"code": 0, "zpData": null}` 当场 AttributeError、非字符串正文被当成正文）。
    jd = detail_post_description(detail_data["data"])
    if jd:
        return jd
    # #56 A5：只有落到这里才是「没拿到正文」——此前这一行是裸 `return ""`，
    # 干等 20s 却一个字都不留（票面 A5 点名的静默面）。
    xhr_seconds = round(time.time() - t0, 1)
    _record_empty_detail(
        page,
        detail_data,
        selectors,
        beat,
        {
            "dom_rounds": dom_rounds,
            "xhr_rounds": xhr_rounds,
            "dom_seconds": dom_seconds,
            "xhr_seconds": xhr_seconds,
            # 两段各自实测后相加，不再多取一次时钟：total 与分项必须严格自洽
            # （读报告的人会把 seconds 与 dom_seconds/xhr_seconds 对账，对不上就成了假证据）。
            "seconds": round(dom_seconds + xhr_seconds, 1),
        },
    )
    return ""


# --- #66 阶段 2 归属守卫：入站对话绑的那个岗，现在还是不是一个岗位页 ----------------
# 判据形状不靠设想：`private/data/probe/inbound_coverage_v2_20261020-120316.json` 把当日 16 条
# 入站逐条 dump 过 `document.title`，12 条是正常岗位页、4 条已经不是岗位页（1 条 404 文案 +
# 3 条被 Boss 换成「是什么职位 / 是做什么的 / …岗位职责」这类问答/SEO 内容页）。
JOB_DEAD_MARKERS = ("您访问的页面不存在",)
# 内容页（问答/SEO）与 404 分开列：处置相同（都不回），但归因不同——读数是给人看的。
JOB_CONTENT_PAGE_MARKERS = ("是什么职位", "是做什么的", "什么是", "前景待遇", "岗位职责")
JOB_TITLE_SHELL_RE = r"工作内容|职位描述|招聘信息|_|-?\s*BOSS直聘"
# 短到这个长度就认不出是个岗位名（实测残渣形如「上海」——那是标题自带下划线被去壳规则切残的副产物，
# 见 test_clean_job_title_underscore_in_title_is_known_loss）。保守方向：unresolved ⇒ 不回、只呈报，
# 因为「回给一个认不出的岗」不可撤回，而"少回一条"下一场还在池子里。
JOB_TITLE_MIN_CHARS = 6
IDENTITY_GOTO_TIMEOUT_MS = 45000  # 与探针同款（详情页比列表页重，30000 实测会误判成取不到）
IDENTITY_POLL_SECONDS = 12
IDENTITY_POLL_INTERVAL_MS = 400


def clean_job_title(raw_title):
    """详情页 `document.title` → 岗位名。真形是 `「岗位名」_品牌名招聘-BOSS直聘`。
    只去壳（分隔符之后的品牌段、站名后缀、「」【】引号），**不动岗位名本身的字**——
    「招聘」是标题的一部分，留着才能拿去搜/比对。"""
    head = re.split(JOB_TITLE_SHELL_RE, (raw_title or "").strip())[0].strip()
    return re.sub(r"[「」【】\[\]]", "", head).strip()


def classify_job_identity(raw_title):
    """`document.title` → `(jobTitle, state)`，state ∈ ok / dead / unresolved。

    先判 dead 再判长度：404 文案「您访问的页面不存在」自身就有 8 个字，反过来先量长度
    会把它错归成"标题太短认不出"，`skippedReasons` 里就丢了三条失效岗的归因。"""
    title = clean_job_title(raw_title)
    if any(m in title for m in JOB_DEAD_MARKERS) or any(m in title for m in JOB_CONTENT_PAGE_MARKERS):
        return title, "dead"
    if len(title) < JOB_TITLE_MIN_CHARS:
        return title, "unresolved"
    return title, "ok"


def job_identity(page, eid, login_dialog_selector=LOGIN_DIALOG_SELECTOR):
    """`encryptJobId` → 岗位身份（详情页 `document.title`），供回复前的归属守卫判「岗还在不在」。

    为什么主源是 title：v2 探针走 `/wapi/zpgeek/job/detail.json` **全数落空**——那条端点是
    分栏视图发的，直跳的 `/job_detail/{eid}.html` 不发它；v1 用 DOM 选择器则串读到页内推荐卡片
    （品牌读成别家）。title 同时含岗位名与公司名，且今日 16 条逐条可读。

    这里**不挂 XHR 监听器**：`capture_xhr` 的 handler 里那次 `resp.json()` → `Response.body()`
    是无超时协议往返，#56 认定的头号楔死源就是它按岗累积；本函数只需要一个 title，不值得为备用
    证据引入一个新的楔死面。

    `state=login` 与 `dead` 必须分开（9-12 那次「15 岗全搜不到」的假读数就是掉登录被读成岗失效）：
    登录问题洗成失效岗，会把整批入站判成不可回。调用方见 login 应整场停，不是跳过这一条。
    跳转抛异常 ⇒ `state=unresolved` + `error` 串，不往外抛（#38 观测/取证层不新增失败面）。"""
    out = {"encryptJobId": eid, "rawTitle": "", "jobTitle": "", "state": "unresolved", "url": ""}
    try:
        page.goto(JOB_DETAIL_URL.format(eid=eid), wait_until="domcontentloaded", timeout=IDENTITY_GOTO_TIMEOUT_MS)
    except Exception as e:  # noqa: BLE001  # 网络/协议失败是一格读数，不是一枚打断整场的异常
        out["error"] = f"{type(e).__name__}: {e}"[:160]
        return out
    raw = ""
    started = time.time()
    while not raw.strip() and time.time() - started < IDENTITY_POLL_SECONDS:
        try:
            raw = (page.title() or "").strip()
        except Exception:  # noqa: BLE001  # 读 title 失败按空处理，交给下面的 unresolved 分支
            raw = ""
        if not raw.strip():
            try:
                page.wait_for_timeout(IDENTITY_POLL_INTERVAL_MS)
            except Exception as e:  # noqa: BLE001  # tab 已关/协议断：等不动就等于拿不到身份，落 unresolved 只呈报，别把整场打断
                out["error"] = f"poll: {type(e).__name__}: {e}"[:160]
                return out
    out["rawTitle"] = raw[:160]
    try:
        out["url"] = (page.url or "")[:200]
    except Exception:  # noqa: BLE001  # url 只是取证字段，取不到不影响判决
        pass
    if is_logged_out(page, login_dialog_selector):
        out["state"] = "login"
        return out
    title, state = classify_job_identity(raw)
    out["jobTitle"] = title
    out["state"] = state
    return out


def collect_new_jds(listing, collected, applied_ids):
    """列表页新岗位收集：排除已投递、本轮重复（含单列表内重复）与无 id 项。"""
    seen = {j.get("encryptJobId") for j in collected}
    new = []
    for j in listing:
        eid = j.get("encryptJobId")
        if eid and eid not in seen and eid not in applied_ids:
            collected.append(j)
            seen.add(eid)
            new.append(j)
    return new


def _unread_count(raw):
    """wapi `unreadMsgCount` 归一化：非负整数照实取，**其余一律 None（读态未知），不取 0**。
    0 与 None 的分别是这条读数的全部价值：0＝确实都读过了，None＝这一条的来源没给
    （DOM 降级路径 / wapi 形态漂移 / 值不是数）。把未知压成 0，等于对外宣称
    「入站那批我们全读过了」，而用户正是靠未读数决定下一步去读哪几条（#66 阶段 2，
    用户 2026-10-20 硬约束：链路不得消耗未读标记，所以未读只能靠只读字段拿）。"""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return None


def parse_friend_entries(entries, my_user_id=None, applied=None):
    """wapi getGeekFriendList 条目 → 结构化对话（#30 修复：字段来源从整卡 innerText 猜行
    改为结构化直取；字段映射裁决见 内部调研笔记目录（未随本仓分发）boss-chat-card-shape.md §5）。
    - name/company 语义：company 沿用旧粘连串「姓名+品牌+抬头」（reply_annotations 匹配键不变），
      干净值另存 brandName/title；
    - status：lastMessageInfo.fromId == 我方 userId ⇒ awaiting（[已读] ≠ 回复，#30 裁决），
      否则 hr_replied；userId 不可用时按 lastMessageInfo.status 兜底（0=对方，1/2=我方）；
      **lastMessageInfo 整体缺失 ⇒ awaiting（2026-09-24 修复：空体默认「非我方」= hr_replied
      是假阳性——实测 3 条 wapi 未 hydrate 消息体的会话被误判回复，污染 replied 统计）；
    - is_read 保留已读标记供独立分析；time 为 lastTS 本地 ISO（lastTime 展示串另存）。"""
    applied = applied or []
    results = []
    for e in entries:
        lmi = e.get("lastMessageInfo") or {}
        from_id = lmi.get("fromId")
        if not lmi:
            mine = True  # 无消息体 ⇒ 无「对方回复」证据，保守归 awaiting（#30 同族纪律）
        elif my_user_id:
            mine = from_id == my_user_id
        else:
            mine = lmi.get("status") in (1, 2)
        ts = e.get("lastTS")
        try:
            time_iso = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
        except (ValueError, OSError, OverflowError):
            time_iso = ""
        results.append(
            _conv_record(
                name=(e.get("name") or "").strip(),
                company="".join(
                    p
                    for p in (
                        (e.get("name") or "").strip(),
                        (e.get("brandName") or "").strip(),
                        (e.get("title") or "").strip(),
                    )
                    if p
                ),
                brand=(e.get("brandName") or "").strip(),
                title=(e.get("title") or "").strip(),
                last_msg=e.get("lastMsg") or "",
                time_iso=time_iso,
                last_time=e.get("lastTime") or "",
                status="awaiting" if mine else "hr_replied",
                is_read=bool(mine and lmi.get("status") == 2),
                applied=applied,
                eid=e.get("encryptJobId"),
                unread=_unread_count(e.get("unreadMsgCount")),
                source="wapi",
                # #73：深链 id 的候选与回读对照点，原样透传（`lastTS=0` 也照原样，不当缺失）。
                encrypt_uid=e.get("encryptUid"),
                encrypt_boss_id=e.get("encryptBossId"),
                boss_uid=e.get("uid"),
                last_ts=ts,
                last_from_id=from_id,
            )
        )
    return results


def parse_dom_cards(cards, applied=None):
    """结构化 DOM 卡片（CONVS_JS 产出）→ 结构化对话（wapi 未命中时的降级路径）。
    带状态徽标（[已读]/[送达]）= 最后一条是我方消息 ⇒ awaiting；无徽标 = HR 回复。
    DOM 无 encryptJobId/精确时间戳：岗位按品牌唯一命中回填，time 只有展示串。"""
    applied = applied or []
    results = []
    for c in cards:
        chip = (c.get("status") or "").strip()
        mine = bool(chip)
        results.append(
            _conv_record(
                name=(c.get("name") or "").strip(),
                company=re.sub(r"\s+", "", c.get("name_box") or ""),
                brand="",
                title="",
                last_msg=c.get("last_msg") or "",
                time_iso="",
                last_time=c.get("time") or "",
                status="awaiting" if mine else "hr_replied",
                is_read=bool(mine and c.get("status_read")),
                applied=applied,
                eid=None,
                unread=None,  # DOM 卡片没有未读数（四样：时间/姓名/徽标/正文）⇒ 读态未知
                source="dom",
            )
        )
    return results


def _match_applied(eid, company, applied):
    """岗位回查：encryptJobId 精确匹配优先；无 id（DOM 路径）或 id 不在投递台账时
    品牌互为子串（match_brand，与 eval/learn 同一判定）且唯一命中才回填——多义不猜。"""
    if eid:
        for a in applied:
            if a.get("encryptJobId") == eid:
                return a
    from gen_greetings import match_brand  # scripts 平铺导入，单一判定源

    hits = [a for a in applied if match_brand(company, a.get("brandName", ""))]
    return hits[0] if len(hits) == 1 else None


def _conv_record(
    name,
    company,
    brand,
    title,
    last_msg,
    time_iso,
    last_time,
    status,
    is_read,
    applied,
    eid,
    source,
    unread=None,
    encrypt_uid=None,
    encrypt_boss_id=None,
    boss_uid=None,
    last_ts=None,
    last_from_id=None,
):
    matched = _match_applied(eid, company, applied)
    return {
        "name": name,
        "company": company,
        "brandName": brand,
        "title": title,
        "summary": (last_msg or "")[:80],
        "lastMsg": last_msg or "",
        "time": time_iso or last_time,
        "lastTime": last_time,
        "status": status,
        "is_read": is_read,
        # 未读条数（#66 阶段 2）。注意与 `is_read` 不是一回事、也不可互相代替：`is_read` 说的是
        # **我方发出去的那条被对方读了没**（lastMessageInfo.status==2），`unreadMsgCount` 说的是
        # **对方发我们的还有几条没读**。后者本来想用来分「还没读 / 读了没回」，但 10-21 实测它分不出来（见 `read_state`）。
        "unreadMsgCount": unread,
        "appliedJob": matched.get("jobName") if matched else None,
        "encryptJobId": eid or (matched or {}).get("encryptJobId"),
        "source": source,
        # #73 深链与发送回读的地址/对照字段：wapi 条目原样透传，DOM 降级路径一律 None
        # （卡片只有时间/姓名/徽标/正文四样，没有这些）。
        "encryptUid": encrypt_uid,
        "encryptBossId": encrypt_boss_id,
        "bossUid": boss_uid,
        "lastTS": last_ts,
        "lastFromId": last_from_id,
    }


def inbound_waiting(convs):
    """入站、且球在我们这侧的那批对话（#66 阶段 2 的动作面对象）。

    判据两条：末条是对方发言（`status == hr_replied`）+ 归不到我们的投递记录
    （`appliedJob` 空）。这是单一实现处——`followup_tracker` 的 `[入站线索]` 清单和
    `reply_inbound` 的候选都走它；各写一遍迟早漂，漂了以后呈报的数与回复面的数对不上。"""
    return [c for c in convs if c.get("status") == "hr_replied" and not c.get("appliedJob")]


# 读态的中文标签（呈报侧唯一一份）：`followup_tracker` 的 `[入站线索]` 清单与
# `reply_inbound` 的候选清单都从这里取——判据是 `read_state`，措辞也必须同源，
# 否则两处改词会让同一个人在两份读数里看到不同的名字。
STATE_LABELS = {"unread": "有未读", "read": "无未读标记", "unknown": "读态未知"}


def read_state(rec):
    """一条对话的读态 → `unread` / `read` / `unknown`（#66 阶段 2，判据唯一实现处）。

    未知**不并入已读**：历史报告回放、DOM 降级路径、wapi 少给这个键，都只能报未知。
    把它读成「都看过了」等于对用户谎报，而那正是用户要靠这列判断的事。

    **但这条判据在真机上是空的（2026-10-21 两场测量）**：09:05 与 09:54 两次快照里 68/68 条
    `unreadMsgCount` 全为 0，而 09:54 那一次用户确认 Boss 上**有未读会话**、且两次之间 hr_replied
    还从 18 涨到 20（新消息确实在进来）。⇒ geek 侧这个字段不携带未读态。所以 `read` 这一格只能
    读成「站点没给未读标记」，**不能**读成「我们看过没回」；标签因此改名成「无未读标记」。
    「该去读哪几条」这个问题这列答不了——只有 Boss 自己的未读徽标答得了，而那要人去看。
    `bool` 不是计数（`True` 会满足 `> 0`），显式排除。展示侧（followup_tracker 的
    `[入站线索]` 清单）与本文件的分桶计数都走这里，不各写一份。"""
    u = rec.get("unreadMsgCount")
    if isinstance(u, bool) or not isinstance(u, int):
        return "unknown"
    if u > 0:
        return "unread"
    # 0 才算已读；负数（生产路径到不了——`_unread_count` 已洗成 None，但历史报告回放与别的
    # caller 可能直接构造）不并入已读，否则又回到「把不知道读成都看过」那个错误方向。
    return "read" if u == 0 else "unknown"


def build_report(results, generated_at, my_user_id=None):
    """结构化对话 → 统计报告。`myUserId` 恒在（#73）：发送后的 wapi 回读要判「末条 fromId
    是不是我方」，而 userId 只有采集那一刻从 `getUserInfo` 响应里拿得到，不跟着报告出来，
    回读侧就只能靠猜或再发一次请求。采集时没拿到就是 None（回读侧据此 fail-closed）。"""
    replied = [r for r in results if r["status"] == "hr_replied"]
    awaiting = [r for r in results if r["status"] == "awaiting"]
    total = len(results)
    linked = [r for r in results if r["appliedJob"]]
    linked_replied = [r for r in linked if r["status"] == "hr_replied"]
    # 入站线索（用户点名的盲区，2026-10-16）：appliedJob 为空＝归不到我们投出去的招呼语，
    # 其中绝大多数是对方主动来撩（少量是我方投出但关联失败——#64 的 applied_without_greeting 那一族）。
    # 末条是对方发言（hr_replied）的那些**球在我们这侧、正在等回话**，而现有回复率读数看不到它们：
    # eval 的配对从招呼语台账侧驱动，台账里没有的岗根本进不了 records
    # （实测 81 条对话 16 条未关联，15 条已回复里 13 条出自这批）。两键恒在（0 也写出来）。
    unlinked = [r for r in results if not r["appliedJob"]]
    unlinked_replied = inbound_waiting(results)
    # 等回话那批再按读态分三桶（恒在）。三桶相加＝unlinkedReplied，由 test_platforms 钉住——
    # 分母不等于该桶的读数会被读成「漏了几条」。
    waiting_states = [read_state(r) for r in unlinked_replied]
    return {
        "generatedAt": generated_at,
        "myUserId": my_user_id,
        "totalConversations": total,
        "hrReplied": len(replied),
        "awaiting": len(awaiting),
        "replyRate": f"{len(replied) / total * 100:.0f}%" if total else "0%",
        "linkedToApplied": len(linked),
        "linkedReplied": len(linked_replied),
        "unlinkedConversations": len(unlinked),
        "unlinkedReplied": len(unlinked_replied),
        "unreadConversations": sum(1 for r in results if read_state(r) == "unread"),
        "unlinkedRepliedUnread": waiting_states.count("unread"),
        "unlinkedRepliedRead": waiting_states.count("read"),
        "unlinkedRepliedReadUnknown": waiting_states.count("unknown"),
        "conversations": results,
    }


CONVS_JS = """() => {
    const lis = [...document.querySelectorAll('li[role="listitem"]')];
    const out = [];
    for (const li of lis) {
        const rect = li.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) continue;
        const q = (sel) => li.querySelector(sel);
        const msg = q('.last-msg-text');
        if (!msg) continue;
        const status = q('.message-status');
        const timeEl = q('.time');
        const nameEl = q('.name-text');
        const box = q('.name-box');
        out.push({
            time: timeEl ? timeEl.innerText.trim() : '',
            name: nameEl ? nameEl.innerText.trim() : '',
            name_box: box ? box.innerText.replace(/\\s+/g, '') : '',
            status: status ? status.innerText.trim() : '',
            status_read: !!(status && /status-read/.test(status.className || '')),
            last_msg: msg.innerText.trim(),
        });
    }
    return out;
}"""


class ChatFirstPlatform(PlatformBase):
    name = "chat-first"
    login_dialog_selector = LOGIN_DIALOG_SELECTOR

    def crawl_jds(self, page, queries, target, exclude_ids):
        """预爬链路已废弃（#44）：唯一投递路径 live_apply 自带搜索流/推荐流采集
        （search_list/recommend_list），不再经过平台层预爬。"""
        raise NotImplementedError("crawl_jds 随挂机盲投链路清理（#44）；岗位采集由 live_apply 承担")

    def save_evidence(self, page, tag, job=None):
        return save_crash_evidence(page, tag, job)

    def apply(self, page, job, send_text, opts=None):
        """盲投已废弃（#44）：唯一投递路径 live_apply 承担全部 DOM 投递流程
        （进详情→人读→沟通→起草→gate→键入发送）。"""
        raise NotImplementedError("apply 随挂机盲投链路清理（#44）；投递由 live_apply 承担")

    def followup(self, page, applied, beat_fn=None, detach=False):
        """打开消息页拿会话列表（#30 修复）：wapi 拦截为主链路（getGeekFriendList 结构化
        全量，带 encryptJobId 与毫秒时间戳），结构化 DOM 降级；字段映射见
        内部调研笔记目录（未随本仓分发）boss-chat-card-shape.md。探测结论：聊天实时流走 WS
        （wss://ws6.zhipin.com/chatws），page.on("response") 抓不到，但不逆向——
        会话列表数据 wapi 已完整覆盖。
        #59 修复轮 R4-1：beat_fn 形参（#56 A3 同形态，`grab_detail` / `find_comm_button` 一致）——
        平台层不知道心跳为何物，由调用方注入并自行吞异常，默认 None = 零行为变更。本段的
        「goto 上界 + 固定停顿」是会话内最长的合法静默区间之一，拍点让它落在取栈阈值之下。
        `detach=True` 给**会话中途的重复读**用（#73 发送后回读）：默认路径停在会话末尾的聊天页、
        页面随后整体关闭，留一个 handler 不影响判读；但同一页面上重复调用会把 `handlers_live`
        抬高成 2、3…，那时「偏高 1」的既有判读就不成立了。所以正常出口（含登录过期早退）都注销
        自己挂的那个；中途异常时保持挂着（诚实读数），由会话收尾关闭页面。注销失败时计数同样
        保持挂着并打警告——重复读的代价只是多一个解析器。"""
        beat = beat_fn or _no_beat
        captured = {"friends": None, "uid": None}

        def h(resp):
            try:
                url = resp.url
                if FRIEND_LIST_URL_FRAG in url:
                    j = _resp_json_with_forensics(resp, FRIEND_LIST_URL_FRAG)
                    if j.get("code") == 0:
                        captured["friends"] = j.get("zpData", {}).get("result") or []
                elif USER_INFO_URL_FRAG in url:
                    j = _resp_json_with_forensics(resp, USER_INFO_URL_FRAG)
                    if j.get("code") == 0:
                        captured["uid"] = j.get("zpData", {}).get("userId")
            except Exception:
                pass

        # 监听必须在 goto 前注册：friend list 由页面加载即请求
        page.on("response", h)
        # #56 取证 A：登记 handler（`on` 成功后才登记）。默认路径停在会话末尾的聊天页、页面
        # 随后整体关闭，所以这一项会一直留在 `handlers_live` 里——收尾采集之后的读数天然偏高 1，
        # 它不是泄漏（A1 管的是投递期逐岗累积）。`detach=True` 的重复读则必须在出口注销自己。
        listener_attached(h)

        def _detach():
            if not detach:
                return
            try:
                page.remove_listener("response", h)
            except Exception as e:  # noqa: BLE001  # 注销失败保持计数挂着，是诚实读数
                print(f"[warn] followup 监听器注销失败（{type(e).__name__}: {str(e)[:60]}）——handlers_live 保持挂着")
            else:
                listener_detached(h)

        beat("followup:goto-chat:pre")
        page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=CHAT_GOTO_TIMEOUT_MS)
        beat("followup:goto-chat:post")
        time.sleep(FOLLOWUP_POST_GOTO_SLEEP_SECONDS)

        if is_logged_out(page, self.login_dialog_selector):
            print("[停] 登录态过期")
            _detach()
            return None

        # wapi 响应可能晚于 goto 返回：把这段轮询窗口扫满（FRIEND_POLL_ROUNDS ×
        # FRIEND_POLL_INTERVAL_MS = 50 × 300ms ≈ 15s，轮数与间隔一字不改），期间保留**条数最多的
        # 那一批**，窗口结束后写回 captured["friends"]。
        # #59（二轮）：出口既不是「第一个非空响应」（照单全收首屏）也不是上一版实现的
        # 「条数连续两轮不再增长」——那还是在猜到达时序，实测两个反例：① 下一批推迟到第 3 个
        # 等待节拍 ⇒ 提前定稿只返回 3 条（阈值没跟真实到达间隔对表）；② 递减形态 100 → 3 ⇒
        # 返回 3，比旧实现更差。扫满窗口取最大值不新增任何可调阈值，读数不再取决于「哪一次
        # 响应被当成终点」。
        # **「取最大批＝拿全」是假定，不是实测结论**：实测到的形态是站点**一次调用返回全量**
        # （内部调研笔记目录（未随本仓分发）boss-chat-card-shape.md §3「一次调用返回全部 61 条，观测窗口内滚动未触发
        # 分页调用」），于是各批之间互为**超集**，最大批即全量。风险留在这里：若站点改成
        # **增量分页**（每批是不同页、互不包含），取最大批会**静默丢批**——那时正确的出口是按
        # encryptJobId 取**并集**。本函数与 `test_platforms.py` 的桩序列（3 → 50 → 50、100 → 3）
        # 建模的都是「超集到达」这一种形态，不构成对增量形态的覆盖，别把它读成既成事实。
        # 静默时长与阈值对表（`docs/agents/implementation-workflow.md` §7 的专门义务）：两次拍点之间
        # 最长的合法静默 = 聊天页 goto 上界 60s + 固定停顿 6s = **66s**（轮询段本身逐轮拍点，
        # 无心跳上界 = 间隔 300ms）；66 < `_forensics.WEDGE_STACK_SECONDS`（90s）<
        # `SESSION_WATCHDOG_DEFAULT`（240s），所以一场**健康但慢**的聊天页不会给自己落非空的
        # `wedge-stack.txt`——runbook「栈文件为空＝本场没楔过」的判读才成立（#58 那批假栈的
        # 复发形态正是「合法静默顶穿取栈阈值」）。上一版这里写的「合计 ≈21s 静默」漏算了
        # goto 的 90s 上界（真实最坏 111s > 90s），算术现在由常量供给、由
        # `test_live_apply.test_followup_chat_page_silence_ceiling_sits_below_both_thresholds` 钉住。
        # 一致性：wapi 整场没命中（captured["friends"] is None）与命中但最大批为空，**都**在
        # 扫满窗口后走下面的 DOM 降级——空批不当成「拿到了数据」，两条路径的等待时长相同。
        # （取的是各等待节拍上读到的最大值，采样密度与上一版一致，未改成按响应到达计数。）
        best_batch = None
        for round_no in range(1, FRIEND_POLL_ROUNDS + 1):
            beat(f"followup:wait-friends:{round_no}")
            friends = captured["friends"]
            if friends is not None and (best_batch is None or len(friends) > len(best_batch)):
                best_batch = friends
            page.wait_for_timeout(FRIEND_POLL_INTERVAL_MS)
        if best_batch is not None:
            captured["friends"] = best_batch

        if captured["friends"]:
            print(
                f"[会话列表] wapi 拿到 {len(captured['friends'])} 条"
                + (f"（userId {captured['uid']}）" if captured["uid"] else "（userId 未捕获，status 兜底判定）")
            )
            results = parse_friend_entries(captured["friends"], captured["uid"], applied)
        else:
            print("[会话列表] wapi 未命中，降级结构化 DOM 抓取")
            cards = page.evaluate(CONVS_JS)
            print(f"[对话列表] {len(cards)} 个")
            results = parse_dom_cards(cards, applied)
        _detach()
        return build_report(results, generated_at=time.strftime("%Y-%m-%d %H:%M:%S"), my_user_id=captured["uid"])
