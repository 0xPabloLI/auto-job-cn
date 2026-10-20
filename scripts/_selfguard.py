#!/usr/bin/env python3
"""启动期自守护（live_apply 专用；只 import 标准库与一个纯标准库的本地模块，子进程加载照旧轻）。

2026-09-30 #57 第二轮：本文件多了一条 `from _forensics import MARKER_IDENTITY_KEYS`——击杀取证
的字段名必须与心跳 payload 同一份真源（漂移的代价是下一场**静默少字段**、整场不入账）。
`_forensics` 及其依赖（`_atomic_io` / `_limits`）全是标准库，不引 camoufox/lxml 那类重依赖，
`python -c` 子进程的加载开销只多了三个本地小模块。进程外契约、SIGKILL 行为与降级铁律一字未动。

2026-10 #56 取证 A/B：这条 import 又多了几样东西——marker 的镜像三键名（`MARKER_HB_MIRROR`）、
出网探针的主机/端口/两段超时/字段名/本机代理 fake-IP 段（`NET_PROBE_*`）与开关解析 `_net_probe_enabled`，
探测本体 `_probe_egress` 落在本文件（新增的 `socket` 与 `http.client` 都是标准库，轻加载纪律不变）。
SIGKILL 行为照旧：**探测只加字段，不改击杀**——判定完成后才探，任何探测异常都兜住并照常杀。
代价如实写在这里：击杀前多花最多 `NET_PROBE_TIMEOUT_SECONDS`（TCP 3s）+ `NET_PROBE_HTTP_TIMEOUT_SECONDS`
（HTTPS 往返 5s），而看门狗本来就是 240s 才动手；那一格往返换来的正是「那一刻取不取到站点内容」——
TCP 握手答不出这个问题（判读见 `docs/agents/apply-forensics.md` 的四格，2026-10-09 实测纠正）。
测试侧注意：**两代**看门狗（`_watchdog_main` 与 `_heartbeat_watchdog_main`）的默认探测都会真打
DNS/TCP/HTTPS——它们自本次起走同一份击杀取证 `_write_kill_evidence`，故 `scripts/conftest.py` 的 autouse
夹具把 `LIVE_NET_PROBE` 默认关（形态同它已有的 `LIVE_WEDGE_DUMP`），要验探测的用例自行开开关并注入假件。

2026-09-25 会话 `20260925-215522` 实测：Playwright 同步协议链路楔死——python 主线程
卡在不可中断的 C 层 read（SIGINT 亦无法打断），进程内 signal 看门狗同样不可靠
（信号标志永远没机会被检查）。唯一可靠的看门狗在进程外。

`_watchdog_main` 由 live_apply 以 `python -c` 子进程方式拉起：在 timeout 秒内每 2s
检查一次「哨兵文件是否出现 / 父进程是否还活着」；超时且父进程仍存活则先落击杀取证
（读心跳 + 出网探测 + 写 `WATCHDOG_KILLED.json`，与会话中途版同一份 `_write_kill_evidence`），
再从叶子到根 SIGKILL 整棵进程树（浏览器 + driver + 父进程），退出码 3。楔死会话零投递零产物，
强杀即确定性止损。取证是 2026-10-07 补的口：那一场的卡死发生在起飞前自检的点击上，180s 的①先到期
把整树杀了、240s 的②根本没机会动手，于是只有栈、没有 marker、没有网络读数——两代看门狗在 preflight
段**本来就并存**（`live_apply._preflight_guarded` 会 `_arm_session_watchdog(rebind=True)`），
问题是赛跑中快的那个不留证据，所以现在**先到期者留证**、marker 里用 `watchdog_phase` 记下是谁掐的。

`_heartbeat_watchdog_main` 是同一止损思路的**会话中途**版（20260927 三场楔死实测：
楔点在 enter_detail 协议链路，preflight 看门狗只护起飞前）：主进程每走一个机械节点
刷新心跳文件，看门狗发现心跳停摆超阈值且父进程仍活即整树击杀。退出码 0=父进程死亡
或 done 哨兵出现（report 已写盘、进入用户节奏的 CLOSE 等待），3=心跳停摆击杀。
"""

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _forensics import (  # noqa: E402  #57 第二轮：跨进程取证字段名的单一真源
    # #56 取证 A/B：marker 的镜像三键名、出网探针的主机/端口/两段超时/字段名/fake-IP 特征段与开关解析——
    # 全部是**常量与纯标准库函数**，`_selfguard` 的轻加载纪律（只 import 标准库 + `_forensics`）不变。
    MARKER_HB_MIRROR,
    MARKER_IDENTITY_KEYS,
    # 相位两键 + 时限键：两代看门狗写同一份 marker，读的人靠它们认出「这一场是哪一条掐的」。
    MARKER_PHASE_KEY,
    MARKER_PHASE_PREFLIGHT,
    MARKER_PHASE_SESSION,
    MARKER_LAST_EID,
    MARKER_TIMEOUT_KEY,
    NET_PROBE_BLOCKED_KEY,
    NET_PROBE_DETAIL_PATH_TMPL,
    NET_PROBE_FAKE_IP_KEY,
    NET_PROBE_FAKE_IP_PREFIXES,
    NET_PROBE_HOST,
    NET_PROBE_ENCODING_KEY,
    NET_PROBE_HTTP_KEY,
    NET_PROBE_HTTP_TIMEOUT_SECONDS,
    NET_PROBE_INTERSTITIAL_LOCATION_MARKERS,
    NET_PROBE_INTERSTITIAL_MARKERS,
    NET_PROBE_LOCATION_KEY,
    NET_PROBE_MARKER_KEY,
    NET_PROBE_MAX_ADDRESSES,
    NET_PROBE_PATH_KEY,
    NET_PROBE_PORT,
    NET_PROBE_STATUS_KEY,
    NET_PROBE_TIMEOUT_SECONDS,
    NET_PROBE_TOTAL_MS_KEY,
    _net_probe_enabled,
)


def _pgrep_children(pid):
    """pgrep -P 取直接子进程 pid 列表；pgrep 缺失/异常返回空列表（降级为只杀父进程）。"""
    try:
        proc = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=10)
    except Exception:
        return []
    children = []
    for token in proc.stdout.split():
        try:
            children.append(int(token))
        except ValueError:
            continue
    return children


def _descendants(pid, pgrep_fn=None):
    """递归收集 pid 的全部后代；叶子在前，便于先杀叶子再杀祖先。"""
    pgrep_fn = pgrep_fn or _pgrep_children
    out = []
    for child in pgrep_fn(pid):
        out.extend(_descendants(child, pgrep_fn))
        out.append(child)
    return out


def _probe_alive(pid):
    """os.kill(pid, 0) 存在性探测：死=False，活/无权限杀但存在=True。"""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _kill9(pid):
    os.kill(pid, signal.SIGKILL)


_POLL_INTERVAL = 2  # 秒；哨兵/父进程存活轮询间隔


def _kill_tree(parent, pgrep_fn=None, kill_fn=None):
    """从叶子到根 SIGKILL 整棵进程树（两代看门狗共用的止损出口）。"""
    victims = _descendants(parent, pgrep_fn)
    victims.append(parent)
    for pid in victims:
        try:
            kill_fn(pid)
        except OSError:
            pass  # 击杀竞态（进程已自行退出）不算失败


def _read_heartbeat(heartbeat):
    """读心跳文件的完整 payload（best-effort）：事后回答「楔死时在干什么、在哪个岗上」。

    #56 A2：原来只取 label，击杀取证因此答不出岗位身份，楔死场永远从同一个岗重新开始。
    形态不挑：文件缺失 / 坏 JSON / 非字典一律返回 {}——marker 照写、缺的字段落 None，
    取证失败绝不阻止击杀（降级铁律，同 #38/#54 观测层口径）。"""
    try:
        with open(heartbeat, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _write_marker(path, payload):
    """取证落盘（best-effort）：止损优先于取证，写不进去也不阻止击杀。"""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


# --- #56 取证 B：击杀**之前**那一次有界出网探测 -----------------------------------------
#
# 三个嫌疑现在分不清的原因：现有取证只有线程栈（`wedge-stack.txt`）与心跳，而
# ①「我方 handler 的往返挂住」由取证 A 的三键负责，③「这台机器那一刻出网不通」只能由
# **进程外**的看门狗自己证——被杀的进程正卡在往返上，写不出任何证词。所以探测放在这里，
# 放在写 marker 之前。
#
# 2026-10-09 的实测把探测从「DNS + TCP」推成「DNS + TCP + 一次真实 HTTPS 往返」：会话
# `20261009-093228` 09:41:31 被击杀，取证里 `tcp_ok=true`、解析地址 `198.18.0.116`，而同一刻同机
# 复测 `urlopen("https://www.zhipin.com/")` = **HTTP 200 / 538056 字节 / 630ms**。对端就是本地代理，
# **握手成功证明不了「浏览器那一刻取得到站点内容」**——旧形状对这个问题一字不答，
# 旧判读表因此只能把那场读成「三嫌疑都无正面证据」。多出来的那一格答的就是它。

_NET_PROBE_BODY_BYTES = 4096  # 只读这么多就够判定「取不取到内容」——实测首页 538056 字节，全灌进内存毫无增益


def _safe_monotonic(monotonic_fn):
    """取一次 `monotonic_fn()` 读数，坏掉返回 None（时钟只是附注，缺读数不许把探测变成异常）。"""
    try:
        return monotonic_fn()
    except Exception:  # noqa: BLE001  # 降级铁律同 `_probe_egress`：读表失败只丢那一格耗时
        return None


def _redirect_is_interstitial(status, location):
    """3xx 且 `Location` 命中站点认证/安全流程＝插页的**通道②**（不是补充，是主通道）。

    为什么必须有这一条：`http.client` 不跟跳转，而实测插页就是这样回话的（302 → `/web/passport/zp/
    security.html`，`Content-Length: 0`）。只读 body 的判据在这个现场必然漏——那正是 2026-10-15
    自查抓出来的失效点：一个声称用来认插页的键，在它自己的 motivating 案例上答 False。
    同通道①**只认命中**：非 3xx、或 Location 指向别处（正常引导）一律 False。"""
    if not isinstance(location, str) or not location:
        return False
    try:
        code = int(status)
    except Exception:  # noqa: BLE001  # 状态码形态怪异：这一格没资格说话，不许把探测变成异常
        return False
    if not 300 <= code < 400:
        return False
    return any(marker in location for marker in NET_PROBE_INTERSTITIAL_LOCATION_MARKERS)


def _probe_path(eid):
    """本次 HTTP 段的请求 path：有岗可指就探**同形状**的详情页 `/job_detail/{eid}.html`，否则回落首页。

    为什么要跟着击杀那一刻的岗走：会话正是卡在某个岗的详情往返上，探首页只能问「这条路通不通」，
    问不出「这一类端点此刻给不给内容」——2026-10-11 那次误读就误在这里（首页常年 302、探它永远通）。
    eid 过 `quote`：它是从心跳文件里读出来的字符串，裸拼进请求行等于让一个带 CR/LF 的坏值变成
    请求头注入——探测自己绝不许成为新的失败面（降级铁律的另一半）。"""
    if isinstance(eid, str) and eid:
        return NET_PROBE_DETAIL_PATH_TMPL.format(eid=quote(eid, safe=""))
    return "/"


def _default_http_fetch(host, timeout, path="/"):
    """生产默认的 HTTP 段：`https://{host}{path}` 发一次 GET，返回一份**小 dict**（状态码 / 读体异常 / 响应体片段 / Location / Content-Encoding）。

    为什么从元组换成 dict：判插页要读的是**响应头**（2026-10-15 用生产探针自己的形状实测那条详情页：
    `302`、`Location=/web/passport/zp/security.html`、`Content-Length: 0`、body 为空），加上 body 与压缩
    方式就是第五格——五元组的调用点读不出谁是谁，而 marker 侧早已按 `http_status` / `http_location` 这类
    名字定名，契约用同名键最直接。

    为什么只读极少量响应体：这一格要回答的是「那一刻这条路取不取得到站点内容」，
    **状态行 + 响应头 + 前 4096 字节**已经把问题答完；实测首页 538056 字节（2026-10-09 09:41 复测），
    读完它只是让击杀前多等几秒、多占几百 KB。**不解压、不解析 HTML**——但 body 不再整个丢掉：
    2026-10-11/10-15 两次实测证明「有状态码」与「取到内容」可以背离（同一时刻详情页回的是
    安全验证页／「请稍候」插页，status 都正常），那 4096 字节里就写着答案，丢掉它等于把这次
    实测重新变成盲区。判形态在 `_body_is_interstitial`，压缩字节读不出特征属**已知假阴性**（见下）。

    代理路径：**不**设 `HTTPS_PROXY`、**不**接 `ProxyHandler`，与 TCP 段同一条路（直连解析到的地址；
    本机代理是 fake-IP 模式，劫持发生在 TCP 层，实测就是这么拿到 HTTP 200 的）。
    读数代表的仍是**看门狗进程视角**，浏览器那条路可能不同（`_probe_egress` 的诚实限制一字未改）。

    抛 vs 返回：拿不到状态码（连接/TLS/超时/协议错误）一律**外抛**，由 `_probe_egress` 兜进 `error`；
    状态码已到手之后的读体失败**不外抛**，而是把异常类名回给调用方记下——路已经证明通了，
    半路截断不该被反着写成「没通」，但那一次截断本身是信号，所以也不能整个吞掉。截断时 `body` 落
    `None`（异常对象里那份残片不取：半截 HTML 里能不能碰上特征词取决于截在哪，拿它判形态会造出
    「同一个现场、两种读数」，比缺这一格更坏）。`location` / `content_encoding` 在读体**之前**就从响应头
    取到了，不受读体失败影响——那正是插页判据的主通道。"""
    conn = http.client.HTTPSConnection(host, timeout=timeout)
    try:
        conn.request("GET", path, headers={"User-Agent": "auto-job-cn-selfguard-net-probe/1.0"})
        resp = conn.getresponse()
        status = resp.status
        # 响应头先读：`http.client` 不跟跳转，302 的答案**只**写在这里（body 是空的）
        location = resp.getheader("Location")
        encoding = resp.getheader("Content-Encoding")
        try:
            body = resp.read(_NET_PROBE_BODY_BYTES)
        except Exception as e:  # noqa: BLE001  # 只影响「读全」不影响「路通」，见 docstring
            return {
                "status": status,
                "error": type(e).__name__,
                "body": None,
                "location": location,
                "content_encoding": encoding,
            }
        return {"status": status, "error": None, "body": body, "location": location, "content_encoding": encoding}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001  # 关不掉只是漏一个 fd，读数已经拿到
            pass


def _body_is_interstitial(body):
    """响应体片段是否命中站点插页特征（`NET_PROBE_INTERSTITIAL_MARKERS`）——**只认命中**。

    `True` 是「那一刻取回来的是拦截/挑战页而不是内容」的正面证据（嫌疑③ 那一格）。
    `False` 的含义只有「没检出特征」：可能是真内容，也可能是 `http.client` 没解压的 gzip/br 字节、
    4096 字节被截在特征词之前、或站点换了新文案——**不许把它读成「取到了岗位内容」**（#54 归因
    硬规则：只报事实，不把缺证据写成结论）。非字节入参一律 `False`：坏形态不该冒判成命中。
    """
    if not isinstance(body, (bytes, bytearray)):
        return False
    try:
        # `ignore` 而非 `strict`：截在半个汉字上时剩下的部分照旧能匹配特征词；
        # 压缩字节走同一分支——解出乱码串、匹配不上、返回 False，正是上面那条已知假阴性。
        text = bytes(body).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001  # 降级铁律：判形态这一步任何失败都不许把探测变成异常
        return False
    return any(marker in text for marker in NET_PROBE_INTERSTITIAL_MARKERS)


def _probe_egress(getaddrinfo_fn=None, create_connection_fn=None, monotonic_fn=None, http_fetch_fn=None, eid=None):
    """`NET_PROBE_HOST` 的解析 + 一次 TCP 连接 + **一次真实 HTTPS 往返**，返回一份**形状固定**的字典。

    诚实的限制（这条决定判读口径，别读反）：看门狗是**另一个进程**，它探到的是**它自己视角**的
    网络；浏览器（Camoufox）可能走不同的代理路径。于是
      · 探针失败 = 嫌疑③ 的正面证据（同机同时刻出网确实不通）；
      · 探针成功 **不能**排除嫌疑③（浏览器那条路可能仍然是断的）——这一格**加不了、也别想靠它减掉**。
    三段各自答各自的一层：`dns_ok` 解析、`tcp_ok` 能不能握手、`http_ok` 拿不拿得到响应状态码。
    其中 **`tcp_ok=true` 而 `http_ok=false` 是嫌疑③ 的正面证据**（连上了对端、内容却取不回来，
    2026-10-09 那场缺的正是这一格）；状态码 **任何**已收到的都算往返成功（含 4xx/5xx），因为这族
    键回答「路通不通」，不回答「页面好不好」。四格判读见 `docs/agents/apply-forensics.md`。
    判读还要配着取证 A 的三键一起看：`handlers_inflight=1` + 探针失败才是「往返挂住且网络也断」，
    `handlers_inflight=1` + 探针成功仍然只是嫌疑①/② 之间二选一。

    **2026-10-11 之后这两句都不够了**（那场 `http_ok=true` 曾被读成「排除③」，是错的）：探的是首页，
    首页常年回 302——路通与「正卡住的那个岗的正文取回来了」根本是两件事。于是本函数多两格：
      · `probe_path`：本次探测的 path（**TCP 都没通时是意图值**，那一趟压根没发出去，配 `tcp_ok` 一起读）。
        会话卡在详情往返上就探同形状的 `/job_detail/{eid}.html`（eid 由击杀取证从同一份心跳传下来），
        缺 eid 回落首页 `/`，读数照旧恒在。
      · `blocked_shape`：插页形态，**两条通道**——body 命中文案特征，或 3xx 且 `Location` 命中认证/安全
        路径（`http_location`）。第二条是主通道：2026-10-15 按生产探针自己的形状实测那条详情页，拿到
        `302 + Location=/web/passport/zp/security.html + Content-Length: 0`，**body 是空的**——只读 body
        会在它自己的 motivating 现场上答 False。判据仍**只认命中**：`False` 只等于「没检出特征」，
        可能是 `http.client` 没解压（见 `http_content_encoding`），也可能只是探针匿名、非浏览器 UA——
        **这一格分不清**「IP 被风控」与「匿名本来就不给看」，所以永远不许反着写成「取到了岗位内容」。
    两格与 `http_ok` 是**并存读数**，不互相吃掉（同 `IncompleteRead` 那格「路通了」与「那次截断」
    并存的口径）：`http_ok=true` + `blocked_shape=true` 读成「路通、取到插页」，不是「路通、排除③」。

    时间上界与代价：TCP 段吃 `NET_PROBE_TIMEOUT_SECONDS`（3s）、HTTP 段吃
    `NET_PROBE_HTTP_TIMEOUT_SECONDS`（5s，最坏多花的就在这 5s 上）；`getaddrinfo` **没有**超时参数，
    最坏由 OS 解析器决定，所以这是「两段有界」而不是「总耗时硬有界」。HTTP 段只在 TCP 通了之后才跑
    （对端都没连上时发请求只是把「连不上」再测一遍，白多等一轮）。
    代价明说：看门狗本来就是 240s 才动手，这 3s+5s 加在**已经判定要杀**的那一场的止损延迟上
    （健康会话一轮都探不到），换回来的是「下一次卡死能一次定性到嫌疑③还是排除它」。

    降级铁律（同 #38/#54 观测层与 `_read_heartbeat`）：**任何**异常都兜在函数内部、只往返回值
    的 `error` 里写，绝不外抛——探测要是把击杀搞没了，就是为取证牺牲了止损。
    异常只记 `type(e).__name__`：文本里常带主机、代理 URL 甚至凭据，取证文件是给旁人读的。
    TCP 失败时 HTTP 段不跑，那三键（`http_ok`/`http_status`/`http_total_ms`）落 `False`/`None`/`0`，
    `blocked_shape` 落 `False`（没读体就没资格判形态）、`probe_path` 落**本次意图的 path**
    ——**键恒在**是这份形状对读侧的硬承诺。"""
    path = _probe_path(eid)
    result = {
        "host": NET_PROBE_HOST,
        "port": NET_PROBE_PORT,
        "timeout_seconds": NET_PROBE_TIMEOUT_SECONDS,
        "dns_ok": False,
        "tcp_ok": False,
        NET_PROBE_HTTP_KEY: False,
        NET_PROBE_STATUS_KEY: None,
        NET_PROBE_TOTAL_MS_KEY: 0,
        # 本次**意图**的 path（TCP 不通时也是它）：读侧要能认出这一场探的是首页还是详情页——
        # 同一个 `http_ok=true` 在这两种形状上答的是两个不同的问题。
        NET_PROBE_PATH_KEY: path,
        NET_PROBE_BLOCKED_KEY: False,
        NET_PROBE_LOCATION_KEY: None,
        NET_PROBE_ENCODING_KEY: None,
        "addresses": [],
        NET_PROBE_FAKE_IP_KEY: False,
        "elapsed_ms": 0,
        "error": None,
    }
    getaddrinfo_fn = getaddrinfo_fn or socket.getaddrinfo
    create_connection_fn = create_connection_fn or socket.create_connection
    monotonic_fn = monotonic_fn or time.monotonic
    http_fetch_fn = http_fetch_fn or _default_http_fetch
    started = _safe_monotonic(monotonic_fn)
    try:
        addresses = []
        for info in getaddrinfo_fn(NET_PROBE_HOST, NET_PROBE_PORT) or []:
            try:
                # 同一个 IP 会按 A/AAAA × SOCK_STREAM/DGRAM 出现好几遍（实测一次解析回 5 条里
                # 只有 2 个不同地址）——去重保序，落盘的才是「解析到了哪几个地址」而不是家族转储。
                addr = str(info[4][0])
                if addr not in addresses:
                    addresses.append(addr)
            except Exception:  # noqa: BLE001  # 一条形态怪异的地址不该废掉整份读数
                continue
        result["dns_ok"] = True
        result["addresses"] = addresses[:NET_PROBE_MAX_ADDRESSES]
        # fake-IP 段判的是**全量**地址而不是截断后的前几条：命中一个就够归因，
        # 上限只约束落盘的条数，不该把布尔读数也跟着截没（这一段的含义是「代理在管这条路」
        # 而不是故障——出处见 `_forensics.NET_PROBE_FAKE_IP_PREFIXES` 的 2026-10-09 实测）。
        result[NET_PROBE_FAKE_IP_KEY] = any(addr.startswith(NET_PROBE_FAKE_IP_PREFIXES) for addr in addresses)
        try:
            conn = create_connection_fn((NET_PROBE_HOST, NET_PROBE_PORT), NET_PROBE_TIMEOUT_SECONDS)
            result["tcp_ok"] = True
            try:
                conn.close()
            except Exception:  # noqa: BLE001  # 连接成败已经记下，关不掉只是漏一个 fd
                pass
        except Exception as e:  # noqa: BLE001  # 解析成功但连不上（拒连/超时）：同样只记类型
            result["error"] = type(e).__name__
        if result["tcp_ok"]:
            # 握手之后的那一次真实往返才是本段的新增价值（TCP 通了却取不回内容 = 嫌疑③ 的正面证据）。
            # `http_fetch_fn` 抛任何异常都只写进 `error`：路不通这件事本身就是要记的读数，不是失败。
            http_started = _safe_monotonic(monotonic_fn)
            try:
                info = http_fetch_fn(NET_PROBE_HOST, NET_PROBE_HTTP_TIMEOUT_SECONDS, path)
                # 一起升：`info["status"]` 取不到或转不了整数（注入件回错形状）就**全都不升**——
                # `http_ok` 由状态码成立，`blocked_shape` 又只在状态码到手之后才有资格说话。
                # `http_ok`（路通不通）与 `blocked_shape`（取到的是不是内容）是**并存**读数，谁也不吃掉谁
                # ——2026-10-11 那场正是把前者读成了后者的答案（探的是常年回 302 的首页）。
                status = int(info["status"])
                result.update(
                    {
                        NET_PROBE_STATUS_KEY: status,
                        NET_PROBE_HTTP_KEY: True,
                        NET_PROBE_LOCATION_KEY: info.get("location"),
                        NET_PROBE_ENCODING_KEY: info.get("content_encoding"),
                        # 两条通道并行：body 命中文案（跟完跳转那种现场）或 3xx 的 Location 命中认证/
                        # 安全路径（body 全空那种现场）。缺任意一条都会在某一类现场上假阴。
                        NET_PROBE_BLOCKED_KEY: _body_is_interstitial(info.get("body"))
                        or _redirect_is_interstitial(status, info.get("location")),
                    }
                )
                if info.get("error"):
                    result["error"] = str(info["error"])  # 已经是类名（`_default_http_fetch` 里转好的），不含文本
            except Exception as e:  # noqa: BLE001  # 整个往返没成 / 注入件形态怪异：只记类型，键照旧恒在
                result["error"] = type(e).__name__
            if http_started is not None:
                try:
                    result[NET_PROBE_TOTAL_MS_KEY] = int((_safe_monotonic(monotonic_fn) - http_started) * 1000)
                except Exception:  # noqa: BLE001  # 含读数取不到时的减法（None - float）：只丢这一格耗时
                    pass
    except Exception as e:  # noqa: BLE001  # 解析失败 / 注入件本身异常
        result["error"] = type(e).__name__
    if started is not None:
        try:
            result["elapsed_ms"] = int((monotonic_fn() - started) * 1000)
        except Exception:  # noqa: BLE001  # 耗时只是附注，缺了不影响成败读数
            pass
    return result


def _default_net_probe(eid=None):
    """生产默认的探测入口：开关（`LIVE_NET_PROBE`）关掉时**完全不探测**、返回 None。
    为什么选「写 None」而不是「不写这个字段」：marker 的键集合恒定（读的人不用先猜这一场
    到底开没开），而 None 与「探了但失败」区分得开——探测真跑过就**一定**返回字典，
    失败也在字典里（`error` 非空 / `tcp_ok=False` / `http_ok=False`），永远不会被读成 None。"""
    if not _net_probe_enabled():
        return None
    return _probe_egress(eid=eid)


def _write_kill_evidence(heartbeat, marker, age, phase, stall, timeout, *, marker_fn=None, net_probe_fn=None):
    """击杀取证（**两代**看门狗共用的那一份）：读心跳 → 出网探测 → 写 marker，全在 SIGKILL 之前。

    为什么只有一份：marker 的键集合是读侧契约（`live_apply._surface_prior_wedge` 与
    `_wedge_ledger._wedge_entry_from_marker` 都按固定键读、不做形态猜测），两代各写一份迟早漂成
    「其中一代静默少字段」——而少字段正是 2026-10-07 那一格的成因（preflight 击杀只留了栈）。
    两代唯一的差别是判据：会话版按**心跳停摆**判（`stall` 有值、`timeout` 为 None），
    preflight 版按**时限**判（反之），故两个语义各占一个键、互不冒充（见 `_forensics.MARKER_TIMEOUT_KEY`）。

    降级铁律（#38/#54 同口径）：探测异常与写盘异常各兜各的，**取证任何一步失败都不许改变击杀决策**
    ——走到这里止损已经判定，为一份读数推迟它，等于把取证做成新的失败面。"""
    hb = _read_heartbeat(heartbeat)  # #56 A2：整包读，岗位身份随之进取证
    marker_fn = marker_fn or _write_marker
    net_probe_fn = net_probe_fn or _default_net_probe
    # 判定已经做完、击杀尚未发生——这是本场唯一还能问一句「这台机器此刻通不通」的窗口
    # （被杀的父进程自己正卡在往返上）。判读口径与「TCP 3s + HTTPS 往返 5s」的代价见 `_probe_egress`。
    # 探测目标跟着**这一场正卡在哪个岗**走（`eid` 来自同一份心跳 `hb`，不开第二份真相）：
    # 首页永远通，详情页却可能正被站点拦成插页——不传就问不出后面那一格。
    # 缺 eid（旧格式心跳/坏文件）时 `net_probe_fn` 自己回落首页，读数照旧恒在。
    try:
        net_probe = net_probe_fn(hb.get(MARKER_IDENTITY_KEYS[MARKER_LAST_EID]))
    except Exception:  # noqa: BLE001  # 探测失败绝不改变击杀决策
        net_probe = None
    try:
        marker_fn(
            marker,
            {
                "killed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                # 哪一代掐的：两代在 preflight 段并存，先到期者留证，读的人不必猜（取值见
                # `_forensics.MARKER_PHASE_*`）。
                MARKER_PHASE_KEY: phase,
                # 停摆阈值（会话版）与时限（preflight 版）分列两键：同一键装两种语义，
                # 判读就得先猜这一场的数是哪一种，而猜错的代价是把健康会话读成楔死或反之。
                "stall_seconds": stall,
                MARKER_TIMEOUT_KEY: timeout,
                "stale_seconds": int(age) if age != float("inf") else -1,
                # 身份五键（last_eid / last_job / last_brand / in_job / last_label）的名字
                # 与「取自心跳哪个键」都由 `_forensics.MARKER_IDENTITY_KEYS` 供给（#57 第二轮
                # 单一真源；读侧是 `_wedge_ledger._wedge_entry_from_marker`）。
                # 心跳里没有这些键（旧格式/坏文件）就落 None——取证缺项
                # 不许改变击杀决策，B1 靠 eid + in_job 判断「是否死在这个岗上」。
                **{k: hb.get(v) for k, v in MARKER_IDENTITY_KEYS.items()},
                # 取证 A 的镜像三键（handlers_live / handlers_inflight / handler_last_frag）：
                # 同一份 `hb.get` 推导，键名映射来自 `MARKER_HB_MIRROR`（与身份五键**分开两份**
                # 是职责边界——B1 的入账判据只吃身份五键，别把它们并成一张表）。
                # 旧格式心跳没有这三键 → 落 None，读法与上面完全同构。
                **{k: hb.get(v) for k, v in MARKER_HB_MIRROR.items()},
                # 取证 B：出网探针的嵌套读数（形状见 `_probe_egress`）。None 只可能是
                # 「开关关掉了」或「注入的探测件自己抛了」，生产默认实现不抛。
                NET_PROBE_MARKER_KEY: net_probe,
            },
        )
    except Exception:  # noqa: BLE001  # 取证失败不阻止止损（默认写盘器已吞 OSError，此处兜注入件）
        pass


def _watchdog_main(
    parent,
    timeout,
    sentinel,
    heartbeat,
    marker,
    *,
    exists_fn=None,
    probe_fn=None,
    kill_fn=None,
    sleep_fn=None,
    now_fn=None,
    pgrep_fn=None,
    mtime_fn=None,
    marker_fn=None,
    net_probe_fn=None,
):
    """起飞前自检看门狗主循环。返回退出码：0=哨兵出现或父进程已死（无需处置）；3=超时击杀进程树。
    所有 IO/时间依赖可注入（单测不拉真子进程）；生产用默认真实实现。
    到点击杀前走同一份 `_write_kill_evidence`（读心跳 + 探测 + 写 marker），相位记 `preflight`；
    哨兵出现/父进程已死这两条退场路径什么都没坏，不写 marker 也不探测。
    `heartbeat` 与 `marker` 是必填位置参数：给默认 None 等于留一条「取证可以被悄悄关掉」的分支，
    而被关掉的正是 2026-10-07 那场唯一缺的那格（只有栈、没有 marker、没有网络读数）。"""
    exists_fn = exists_fn or os.path.exists
    probe_fn = probe_fn or _probe_alive
    kill_fn = kill_fn or _kill9
    sleep_fn = sleep_fn or time.sleep
    now_fn = now_fn or time.time
    mtime_fn = mtime_fn or os.path.getmtime
    deadline = now_fn() + timeout
    while now_fn() < deadline:
        if exists_fn(sentinel):
            return 0
        if not probe_fn(parent):
            return 0
        sleep_fn(_POLL_INTERVAL)
    try:
        age = now_fn() - mtime_fn(heartbeat)
    except OSError:
        age = float("inf")  # 自检段还没拍出心跳（文件缺失/消失）= 没有实测停摆，取证里落 -1
    _write_kill_evidence(
        heartbeat,
        marker,
        age,
        MARKER_PHASE_PREFLIGHT,
        None,
        timeout,
        marker_fn=marker_fn,
        net_probe_fn=net_probe_fn,
    )
    _kill_tree(parent, pgrep_fn, kill_fn)
    return 3


def _heartbeat_watchdog_main(
    parent,
    stall,
    heartbeat,
    done,
    marker,
    *,
    exists_fn=None,
    probe_fn=None,
    kill_fn=None,
    sleep_fn=None,
    now_fn=None,
    pgrep_fn=None,
    mtime_fn=None,
    marker_fn=None,
    net_probe_fn=None,
):
    """会话中途看门狗主循环。心跳 mtime 距今超过 stall 秒且父进程仍活 → 取证 + 整树击杀。
    退出码：0=父进程死亡或 done 哨兵出现；3=心跳停摆击杀。依赖注入约定同 _watchdog_main。
    #56 取证 B：`net_probe_fn` 是击杀前的出网探测（默认 `_default_net_probe`，开关关掉时它自己
    返回 None）。它的异常**单独兜住**——探测件坏掉时字段落 None、marker 其余字段照写、击杀照做，
    不许它把整个 payload 连带止损一起搞没（默认实现自身不抛，见 `_probe_egress` 的降级铁律）。
    取证本身住在 `_write_kill_evidence`（两代共用，payload 键集合因此不可能各走各的）。"""
    exists_fn = exists_fn or os.path.exists
    probe_fn = probe_fn or _probe_alive
    kill_fn = kill_fn or _kill9
    sleep_fn = sleep_fn or time.sleep
    now_fn = now_fn or time.time
    mtime_fn = mtime_fn or os.path.getmtime
    while True:
        if not probe_fn(parent):
            return 0
        if exists_fn(done):
            return 0
        try:
            age = now_fn() - mtime_fn(heartbeat)
        except OSError:
            age = float("inf")  # 心跳文件消失（异常态）= 按永久停摆处置
        if age > stall:
            _write_kill_evidence(
                heartbeat,
                marker,
                age,
                MARKER_PHASE_SESSION,
                stall,
                None,
                marker_fn=marker_fn,
                net_probe_fn=net_probe_fn,
            )
            _kill_tree(parent, pgrep_fn, kill_fn)
            return 3
        sleep_fn(_POLL_INTERVAL)


if __name__ == "__main__":  # 直接执行入口（调试用）；生产路径经 live_apply 的 python -c 拉起
    sys.exit(_watchdog_main(int(sys.argv[1]), float(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]))
