#!/usr/bin/env python3
"""会话取证本体（#56 A 段的心跳 / 取栈 / 节拍；#57 第二轮自 `live_apply.py` 拆出，纯搬迁行为不变）。

本模块写出的文件形状，就是**进程外**看门狗与击杀取证的读入契约（判读口径见
`docs/agents/apply-forensics.md` 与 #56 C 段——改形状等于改那份判读，两边要一起改）：
- `SESSION_HEARTBEAT`（heartbeat.json）：主线程每走一个机械节点刷新一次，十键固定形状 =
  `HEARTBEAT_FIELDS`；看门狗拿它的 mtime 差答「正常等待」还是「协议楔死」。#56 取证 A 起，
  末三键是「现场挂着几个事件处理器 / 几个往返在途」——faulthandler 拍不到 greenlet（事件
  处理器不在线程栈里），这三个数是嫌疑①（我方 handler 的往返挂住）唯一的正面读数，
  判读口径见 `handler_roundtrip_enter`。写者也不再只有主线程：往返前后那一拍跑在
  dispatcher 的 greenlet 上，形状与文件位置完全相同。
- `SESSION_DONE`（session_done）：报告写盘后落位的退场哨兵——不杀「等用户 CLOSE」。
- `WEDGE_STACK`（wedge-stack.txt）：`faulthandler` 在「无心跳满 `WEDGE_STACK_SECONDS` 秒」时落的
  全线程栈。SIGKILL 只能止损、答不出「停在哪个调用上」，而楔死时进程内不可能再执行 Python
  代码，所以唯一能把栈留下来的时机是「还活着的那些节拍」——它是心跳的副产品。
- `WATCHDOG_KILLED`（WATCHDOG_KILLED.json）：由 `_selfguard` 的**两代**看门狗在**进程外**写入
  （`_watchdog_main` 护起飞前自检、`_heartbeat_watchdog_main` 护会话中途），靠 `MARKER_PHASE_KEY`
  区分是哪一代动的手；本模块只供给它字段名（`MARKER_IDENTITY_KEYS` 身份五键、`MARKER_HB_MIRROR`
  取证三键、`NET_PROBE_MARKER_KEY` 出网探针字段、相位与时限两键）与会话目录名形状（`_TS_DIR_RE`），
  不写它。

跨进程取证字段名的单一真源在本模块（仓内先例 `live_apply._RETRY_SNAPSHOT_FIELDS`）：
`_heartbeat_payload` 的十键、看门狗写 marker 的身份五键 + 镜像三键 + 探针字段名、
`_wedge_ledger._wedge_entry_from_marker` 读 marker 的键，三处引用同一份映射。漂移的后果不是报错
而是**静默少字段**——B1 读不到 `in_job` / `last_eid` 就整场不入账。

状态所有权：`_HEARTBEAT_DIR` / `_HEARTBEAT_JOB`（以及取栈句柄 `_WEDGE_FH` / `_WEDGE_FH_DIR` /
`_WEDGE_DUMP_BROKEN`、监听器与往返状态 `_HANDLER_REGISTRY` / `_HANDLER_INFLIGHT` /
`_HANDLER_LAST_ENTER`）是**模块级单例，归本模块**——形态同 `platforms/chat_first._LAST_MOUSE`（一进程
一会话，#57 正文方案 2 选的就是这条路，不引入 session 对象）。`shot()` / `scroll_collect` /
`wait_reply` 这些没有 session 形参的老签名借 `_HEARTBEAT_DIR` 找到心跳文件。
**因此调用方与测试都不许把 `live_apply` 顶部的别名当状态入口**：`from _forensics import
_HEARTBEAT_DIR` 绑的是导入那一刻的快照，而写者（`_heartbeat_bind` / `_touch_heartbeat`）读写的是
本模块自己的变量——patch 别名是**静默 no-op**（隔离失效却可能照常绿）。函数别名可以照旧从
`live_apply` 取（同一个函数对象），可变状态与 `WEDGE_STACK_SECONDS` 这类「被函数体读、被用例
monkeypatch 的数值」一律打在本模块上。

搬迁口径：函数体、签名默认值、docstring 与段落注释自 `live_apply.py` 逐字平移，不改写、
不精简、不重排、不改名；注释里的 `live_apply.py:NNNN` 行号指针改成函数名指针（行号随本次
搬迁全部失效，函数名不会）。
"""

import faulthandler
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _atomic_io import write_json_atomic  # noqa: E402
from _limits import BOOL_ENV_TWO_OFF, env_bool_switch  # noqa: E402  #57 第二轮：布尔 env 取值表单一真源

# 击杀取证与会话目录形状（#57 第二轮第 5 点：按职责从 `_wedge_ledger.py` 归位到取证段——
# 前者是「谁死了」的文件名，后者是「哪一场」的目录名，都属于取证本体而不是台账本体）。
# `live_apply.py`（呈报 `_surface_prior_wedge` 与看门狗 spawn 参数）与 `_wedge_ledger.py`（回灌扫描）
# 都从这里引用同一份字面量，不留第二份。
WATCHDOG_KILLED = "WATCHDOG_KILLED.json"  # 会话目录内：看门狗击杀取证（下一场启动时呈报）
_TS_DIR_RE = re.compile(r"^\d{8}-\d{6}$")  # 会话目录名形状（回灌扫描与保留期清理共用）

# --- 跨进程取证字段名（单一真源；漂移的代价是静默少字段，见模块 docstring）---
# 心跳 payload 的十键：`_heartbeat_payload` 按这个顺序构造，看门狗与 B1 按固定键读、不做形态猜测。
# 前七键是 #56 A 段的原形状，顺序不许动（历史会话目录里的 heartbeat.json 要照常读得出来）；
# 末三键（#56 取证 A）把「挂着几个 handler / 几个往返在途 / 最后进入的是哪个端点」送进
# 楔死现场的最后一拍——判读口径见 `handler_roundtrip_enter`。
HEARTBEAT_FIELDS = (
    "ts",
    "label",
    "eid",
    "job",
    "brand",
    "idx",
    "in_job",
    "handlers_live",
    "handlers_inflight",
    "handler_last_frag",
)
# marker 侧身份字段名 ↔ heartbeat 侧字段名（#56 A2 的 last_* 前缀就是「这是死前最后一拍的身份」）。
# 看门狗写 marker 用 `**{k: hb.get(v) for k, v in MARKER_IDENTITY_KEYS.items()}`，
# `_wedge_entry_from_marker` 读 marker 用下面由本映射派生的五个名字——两处都不再写字面量。
MARKER_IDENTITY_KEYS = {
    "last_eid": "eid",
    "last_job": "job",
    "last_brand": "brand",
    "in_job": "in_job",
    "last_label": "label",
}
# 上面映射的**键**（marker 侧读取名），按声明序解包；dict 迭代保序（py3.7+），
# 形状由 `test_heartbeat_and_marker_field_names_are_the_single_source` 双侧钉住。
MARKER_LAST_EID, MARKER_LAST_JOB, MARKER_LAST_BRAND, MARKER_IN_JOB, MARKER_LAST_LABEL = MARKER_IDENTITY_KEYS
# 心跳 payload 的键名取自 `HEARTBEAT_FIELDS`（同 positional 解包），于是「payload 的键」与
# 「那份元组」不可能各走各的；`_HEARTBEAT_JOB` 内部字典用的正是 eid/job/brand/idx 四个同名键。
# 末三个是**键名**，与下面那三个**状态**变量（`_HANDLER_REGISTRY` / `_HANDLER_INFLIGHT` /
# `_HANDLER_LAST_ENTER`）刻意不同名——同一份形状的两半若同名，500 行的取证模块里「读计数」和
# 「读键名」会互相串门，而串门的症状正好是取证 A 要防的那种：读数看着正常、其实是键名字符串。
(
    _TS,
    _LABEL,
    _EID,
    _JOB,
    _BRAND,
    _IDX,
    _IN_JOB,
    _HANDLERS_LIVE,
    _HANDLERS_INFLIGHT,
    _HANDLER_LAST_FRAG,
) = HEARTBEAT_FIELDS
# #56 取证 A 的 marker 镜像：三键**与心跳侧同名**（不像身份五键要加 `last_` 前缀——那五键表达的是
# 「死前的岗位身份」，这三键表达的是「被杀那一刻进程里还挂着什么」，抄过去不需要改名）。
# 与 `MARKER_IDENTITY_KEYS` 分成两份映射是**职责边界**而不是省事：`_wedge_ledger._wedge_entry_from_marker`
# 的入账判据只吃身份五键，合并成一份就等于把往返计数喂进跳岗判据——那是行为变更。
MARKER_HB_MIRROR = {
    _HANDLERS_LIVE: _HANDLERS_LIVE,
    _HANDLERS_INFLIGHT: _HANDLERS_INFLIGHT,
    _HANDLER_LAST_FRAG: _HANDLER_LAST_FRAG,
}

# marker 里记「哪一代看门狗动的手」。为什么必须有这一格：两代看门狗在**同一段**（起飞前自检）并存
# ——`live_apply._preflight_guarded` 既拉按时限判的哨兵版（`LIVE_PREFLIGHT_WATCHDOG`），又
# `rebind=True` 武装按停摆判的心跳版（`LIVE_WATCHDOG`），先到期者留证，于是「被杀」这件事本身答不出
# 死在哪条判据上：把时限击杀误读成停摆击杀，就会把「停在起飞前那一次点击」当成「会话中途卡死」，
# #56 C 段的定性跟着走错方向（阈值本身只认代码常量，这里不复述数值）。
MARKER_PHASE_KEY = "watchdog_phase"
MARKER_PHASE_PREFLIGHT = "preflight"  # `_watchdog_main`：按**时限**判，没有停摆这个概念
MARKER_PHASE_SESSION = "session"  # `_heartbeat_watchdog_main`：按**心跳停摆**判
# preflight 版自己的那条时限（env `LIVE_PREFLIGHT_WATCHDOG`），会话版按停摆判、无此概念故落 None。
# 为什么不塞进 `stall_seconds`：那个键的含义是「停摆阈值」，两种语义共用一键，判读就得先猜这是哪种。
MARKER_TIMEOUT_KEY = "timeout_seconds"

# --- #56 取证 B：击杀**之前**那一次有界出网探测的形状常量 ---
# 为什么常量放这里、实现放 `_selfguard.py`：探测跑在进程外的看门狗子进程里，它要打的地址、
# 花的时间上界、以及写进 WATCHDOG_KILLED.json 的那个字段名，必须和主进程侧的 marker 形状出自
# **同一份真源**——否则「写侧改了字段名、读侧（人工判读 + runbook）还在猜老名字」这种静默少字段
# 又会只以「取证缺项」的形态露出来。`_selfguard` 的加载纪律是「只 import 标准库 + `_forensics`」，
# 常量顺着那条 import 过来正好合规，探测本体用的 `socket` 与 `http.client` 也都是标准库。
NET_PROBE_ENV = "LIVE_NET_PROBE"  # 开关：默认开；关只认 0/off（解析见 `_net_probe_enabled`）
NET_PROBE_HOST = "www.zhipin.com"  # 探测目标是投递实际打的那个域，不是通用连通性检查
NET_PROBE_PORT = 443
NET_PROBE_TIMEOUT_SECONDS = 3  # TCP 段的上界；`getaddrinfo` 不吃超时参数（见 `_selfguard._probe_egress`）
NET_PROBE_MARKER_KEY = "net_probe"  # WATCHDOG_KILLED.json 里那个嵌套字段的键名
NET_PROBE_MAX_ADDRESSES = 5  # 解析地址落盘上限——取证文件不是 DNS 转储
# **本机环境特征，不是通用事实**（换机器 / 换代理方案要重新逐条对照，别把它写进通用结论）。
# 这台机器的浏览器与看门狗都走系统代理（`scutil --proxy` 的 HTTP/HTTPS = 127.0.0.1:7890），该代理
# 处于 **fake-IP 模式**：它给每个域名分配一个 198.18.0.0/15（198.18./198.19. 开头，RFC 8153 的
# benchmarking 保留段）里的假地址，真正的转发在 **TCP 层**由代理劫持完成。
# ⇒ **命中这一段的含义是「代理正在管这条路」（正常工作态），不命中才是「直连公网」**；
#   它回答的是「探针这条读数是走哪条路拿到的」这个**归因**问题，本身不是故障证据。
# 实测依据（2026-10-09 09:41 那场被击杀的同一刻、同一台机器复测）：
#   `getaddrinfo("www.zhipin.com", 443)` 仍返回 `198.18.0.116`，而
#   `urlopen("https://www.zhipin.com/")` 拿到 **HTTP 200 / 538056 字节 / 630ms**
#   ⇒ 落在这一段照样取得到站点内容。旧注释（写于 2026-10-05，那时还没有真机对照）把这个段判成
#   「代理黑洞＝出网断」、旧字段名也带着那个比喻，方向都是反的——2026-10-09 按上面这组实测纠正。
NET_PROBE_FAKE_IP_PREFIXES = ("198.18.", "198.19.")
# --- 探针读数的键名（`_selfguard._probe_egress` 的返回形状 = marker 里 `net_probe` 的形状）---
# 为什么键名放这里：写侧是**进程外**的看门狗子进程、读侧是人工判读与 runbook，两边隔着一份 JSON，
# 漂移的症状不是报错而是「读侧静默缺字段」（本模块顶部那条单一真源口径，先例 `NET_PROBE_MARKER_KEY`）。
NET_PROBE_FAKE_IP_KEY = "resolved_via_proxy_fake_ip"  # 解析地址是否落进上面那个 fake-IP 段（归因，非故障）
NET_PROBE_HTTP_KEY = "http_ok"  # 是否真拿到过一次响应状态码——只答「路通不通」，**不答「取到的是不是内容」**
# 状态码；**任何**已收到的状态码都算往返成功（含 4xx/5xx：这格答「路通不通」不是「页面好不好」）。
# 2026-10-11 那场把 `http_ok=true` 读成「取得到站点内容」是**错的**：探针当时打的是首页，首页常年
# 回 302 引导——路通与「那一个岗的正文取回来了」是两件事，故下面两键各答一半，别合并解读。
NET_PROBE_STATUS_KEY = "http_status"
# 跳转目标与压缩方式：`http.client` **不跟 302、不解压**，所以这两格是 `blocked_shape` 的两条
# 「为什么可能没检出」的解释来源——`location` 非空＝被引导（通道②的证据），`content_encoding` 非空
# ＝body 判据当场没有发言权。恒在，缺信息时落 `None`。
NET_PROBE_LOCATION_KEY = "http_location"
NET_PROBE_ENCODING_KEY = "http_content_encoding"
NET_PROBE_TOTAL_MS_KEY = "http_total_ms"  # HTTP 段自己的耗时（发起请求 → 读完那 4096 字节）
NET_PROBE_HTTP_TIMEOUT_SECONDS = 5  # HTTP 段上界，与 TCP 段的 3s 各自计（代价见 `_probe_egress` docstring）
# 探的是**哪一条 path**：会话卡在详情页往返上，探测就该问同形状的端点（形状由击杀那一刻心跳里的
# eid 决定，缺 eid 回落首页 `/`）。**TCP 都没通时它是意图值**（那一趟请求压根没发出去），读侧别把
# 它读成「这条 path 真发出去过」——配合 `tcp_ok` 一起看才知道是发过还是本想发。
NET_PROBE_PATH_KEY = "probe_path"
# 插页形态判据（**两条通道**，缺一不可）：① 响应体命中文案特征；② 3xx 且 `Location` 命中站点的
# 认证/安全路径。第二条不是补充而是主通道——2026-10-15 按生产探针自己的形状（`http.client`、
# 无 cookie、非浏览器 UA、不跟跳转）实测那条详情页：`status=302`、`Location=/web/passport/zp/
# security.html?seed=…`、`Content-Length: 0`、**body 是空的**——插页信号写在响应头里，只读 body 的
# 判据在它自己的 motivating 现场上会答 False。仍**只认命中**：False 只等于「没检出特征」，不等于
# 「取到了岗位内容」（`http.client` 不解压，gzip 见 `http_content_encoding`；且探针是匿名非浏览器
# UA，站点给插页本就是正常 bot 检测——这一格**分不清**「IP 被风控」与「匿名本来就不给看」）。
NET_PROBE_BLOCKED_KEY = "blocked_shape"
NET_PROBE_DETAIL_PATH_TMPL = "/job_detail/{eid}.html"  # 与浏览器那一刻的请求同形状的端点
# 插页特征词：两组都是本仓实测现场，不是猜的——`安全验证` / `完成验证后即可正常使用` / `verify.html`
# 来自 2026-10-11 四条 `/job_detail/{eid}.html` 全部被引导到的那张页（含当场已成功投出的那个岗）；
# `请稍候` 来自 2026-10-15 复测同一条 URL 时的 `<title>请稍候 - BOSS直聘</title>`（不再跳 verify.html、
# status 仍是 200，取的依旧不是正文——正是这条让「只换探测端点」不够、必须读 body 判形态）。
NET_PROBE_INTERSTITIAL_MARKERS = ("安全验证", "完成验证后即可正常使用", "verify.html", "请稍候")
# 3xx 的 `Location` 命中这些片段＝被引导进认证/安全流程（通道②）。`/web/passport/zp/` 是两条实测
# 落地页（10-11 的 `verify.html`、10-15 的 `security.html`）的共同前缀，所以按前缀认家族而不是只认
# 那一个文件名——站点在同一条路上换文件名不是意外，是常态。
NET_PROBE_INTERSTITIAL_LOCATION_MARKERS = ("/web/passport/zp/", "security.html", "verify.html")

# --- 会话中途看门狗的取证形状与取栈状态（原 `live_apply.py` 取证段）---

# 默认 240s。**这个阈值的含义是「合法无心跳上界的函数」，不是固定延迟**（#58 T1 改口径）：
# 合法无心跳上界由 `_limits.PACE_BEAT_INTERVAL`（30s）决定——会话内所有经由 `SessionPacer`
# 的长等待都被分段睡、段间拍一次点（`pace:intra` / `pace:break` / `pace:dismiss`），
# 于是 30s × 8 拍 = 240s 是纯余量（约 8 倍），agent 起草等待（默认 1200s）另由
# `wait_reply` 逐拍心跳豁免。
# 票面 #58 对照表（旧注释「最长合法无心跳 ~90s：打字 400 字最坏 ~80s / 限速等待 ~40s /
# goto 30s」错在哪：它把限速等待按 ~40s 估，而实际取值是——）：
#   · intra 60-150s **× 时段系数**（9-17 点 1.0、12-13 点 1.5、其余 2.0）→ 最坏 300s
#   · 连投满会话后的长休息 break **480-1500s（8-25 分钟）且不乘系数**（session_min/max=2/4）
#   · `search_list` 等首批 XHR 的轮询 → 60s 纯 `wait_for_timeout`
# 三者都曾经**零拍点**：480s > 240s 会把一场健康会话整树 SIGKILL（票面后果 2）。
# 复核结论（票面 T1 第三条）：分段节拍落地后「下限 120s」的建议值仍成立（120 = 4×30），
# 但**不得低于 4 倍 PACE_BEAT_INTERVAL**，否则一次调度抖动就误杀；数值护栏用例
# `test_legitimate_silence_ceiling_sits_below_both_thresholds` 锁着这条不等式。
SESSION_WATCHDOG_DEFAULT = 240
SESSION_HEARTBEAT = "heartbeat.json"  # 会话目录内：`HEARTBEAT_FIELDS` 十键，逐节点刷新（含往返前后那两拍）
SESSION_DONE = "session_done"  # 会话目录内：报告写盘后落位，看门狗据此退场（不杀「等用户 CLOSE」）
# 击杀取证文件名 `WATCHDOG_KILLED` 与会话目录名形状 `_TS_DIR_RE` 定义在本模块顶部（#57 第二轮
# 第 5 点：按取证职责从 `_wedge_ledger.py` 归位过来）——呈报（`live_apply._surface_prior_wedge`）、
# 看门狗 spawn 参数与回灌扫描认同一份名字，不留第二份字面量。
# #56 A4：楔死现场的全线程栈（会话目录内）。SIGKILL 只能止损、答不出「停在哪个调用上」，
# 而楔死时进程内不可能再执行任何 Python 代码（票面机制口径 §2）——唯一能把栈留下来的
# 时机是「还活着的那些节拍」，所以它是心跳的副产品：正常会话里这份文件恒为空。
WEDGE_STACK = "wedge-stack.txt"
# 无心跳多久落栈：节拍即武装点，故这是**两拍之间的最坏上界**而非固定延迟。
# #58 T1 之后合法上界 = `_limits.PACE_BEAT_INTERVAL`（30s，分段节拍的段长），90s 留 3 倍余量。
# 改前它被合法区间击穿（票面 20260929-145750 的三份假栈，栈顶证据逐字照抄；行号指针已随
# #57 的拆分失效，改按函数名指——`_limits.rnd_sleep <- _intra <- _record_apply_outcome` 与
# `live_apply.search_list <- keyword_round` 两处调用关系不变）：
#   dump 1/3 `_limits rnd_sleep <- _intra <- _record_apply_outcome`（[限速] 125.4s / 143.5s）
#   dump 2   `live_apply search_list <- keyword_round`（等首批 XHR 的 60s 轮询）
# 也就是说：一份非空栈不再等于「楔过」，C 段（根因定性）的判读前提被污染。T1 把节拍铺进
# 这两类等待后，「非空=停在某个协议调用上」才重新成立（判读口径见 runbook，T3 回写）。
WEDGE_STACK_SECONDS = 90
WEDGE_DUMP_ENV = "LIVE_WEDGE_DUMP"  # 逃生开关：默认开；0/off 关（解析见 `_wedge_dump_enabled`）
_HEARTBEAT_DIR = None  # 会话级单例（一进程一会话，同 platforms/chat_first._LAST_MOUSE 风格）
# #56 A2：当前岗位上下文单例（eid/job/brand/idx），由 apply_live 岗头设置、单岗末尾清除。
# None = 不在任何岗位内（滚动轮、wait_reply 之外的会话级节拍）→ 心跳带 in_job=False、
# eid=None。击杀取证靠这个相位区分「死在这个岗上」与「死在两岗之间」，B1 才敢据 eid 跳岗。
_HEARTBEAT_JOB = None
# #56 A4 的取栈状态：faulthandler 的 later 定时器是全进程单例，且 `file=` 用的是文件
# 描述符——打开的文件对象必须长期持有引用，否则它被 GC 掉、fd 关闭，到点的栈就落进了一个
# 已经关掉的描述符（等于什么都没落）。_WEDGE_FH_DIR 记着这个句柄属于哪个会话目录，
# 目录被重绑时据此换文件；_WEDGE_DUMP_BROKEN 是「武装失败过一次就不再逐拍重试」的闸。
_WEDGE_FH = None
_WEDGE_FH_DIR = None
_WEDGE_DUMP_BROKEN = False
# #56 取证 A 的监听器 / 往返状态（模块级单例，归属与「patch 别名是 no-op」的同款告诫见 docstring）。
# 注册表按 **handler 对象身份**存而不是按事件名计数：同一张 page 上那几个闭包的事件名全都是
# "response"，只有对象身份答得出「哪个 handler 还挂着」。
# 为什么状态不放 `live_apply`：`page.on` 与 `remove_listener` 的调用点散在 live_apply 四处 finally
# 加平台层，状态一分为二就必然漂（注销了但计数还在），而这份计数唯一的用处就是楔死定性。
_HANDLER_REGISTRY: set = set()  # 已挂上、尚未注销的 response handler 对象（`set` 标注是 mypy 对空字面量的要求，非风格）
_HANDLER_INFLIGHT = 0  # 正在进行的「handler 内协议往返」数（下限 0，理由见 `handler_roundtrip_exit`）
_HANDLER_LAST_ENTER = None  # 最后一次进入往返时传入的 url_frag（端点片段，不是完整 URL）


def _heartbeat_bind(session_dir):
    """绑定当前会话目录（shot/scroll_collect 等无 session 形参的老签名借它找到心跳文件）。"""
    global _HEARTBEAT_DIR
    _HEARTBEAT_DIR = Path(session_dir)


def _heartbeat_job_start(flow):
    """#56 A2：把本岗身份写入心跳单例，之后每一拍都带 eid/岗位名/公司/序号 + in_job=True。

    必须在**岗头心跳之前**调用：5/5 场楔死的最后一拍就是 `job:{idx}`（楔点在岗头→
    detail-open 之间），上下文晚设一步，击杀取证里的 eid 就恒为 None。
    清除见 `_heartbeat_job_end`（与 A1 的监听器注销共用 `_apply_job` 的 finally）。"""
    global _HEARTBEAT_JOB
    job = flow.job or {}
    _HEARTBEAT_JOB = {
        "eid": job.get("encryptJobId"),
        "job": job.get("jobName"),
        "brand": job.get("brandName"),
        "idx": flow.idx,
    }


def _heartbeat_job_end():
    """#56 A2：单岗末尾清除岗位上下文——之后的会话级节拍（滚动轮 / 下一岗之前）
    必须落回 in_job=False，否则两岗之间的楔死会被 B1 错记到上一岗头上。"""
    global _HEARTBEAT_JOB
    _HEARTBEAT_JOB = None


def _heartbeat_payload(label):
    """心跳 payload（#56 A2 + 取证 A）：{ts, label} + 当前岗位上下文 + 监听器/往返读数。
    不在岗位内时 eid/job/brand/idx 为 None、in_job=False（键恒在——看门狗与 B1 都按固定键读，
    不做形态猜测）。

    键名与顺序都取自 `HEARTBEAT_FIELDS`（#57 第二轮：跨进程取证字段名的单一真源，先例
    `live_apply._RETRY_SNAPSHOT_FIELDS`）——输出的十键形状与本元组不可能各走各的。
    `_HEARTBEAT_JOB` 里存的正是 eid/job/brand/idx 四个同名键，故岗位上下文直接按同名取。
    末三键走 `handler_forensics()`：那份形状只有**一个**生产者，payload 与现场读法就不会
    各造一份键名——取证 A 的全部价值建立在「楔死那一拍写出来的就是这三项」上。"""
    jctx = _HEARTBEAT_JOB or {}
    return {
        _TS: datetime.now().isoformat(timespec="seconds"),
        _LABEL: label,
        _EID: jctx.get(_EID),
        _JOB: jctx.get(_JOB),
        _BRAND: jctx.get(_BRAND),
        _IDX: jctx.get(_IDX),
        _IN_JOB: _HEARTBEAT_JOB is not None,
        **handler_forensics(),
    }


def _touch_heartbeat(label):
    """心跳：主线程每走一个机械节点刷新一次——看门狗据此区分「正常等待」与「协议楔死」。
    必须在主线程调用：楔死场景下主线程卡在不可中断 read（GIL 已释放），后台线程照常
    跳动的心跳对看门狗毫无意义。写失败降级为「无看门狗继续跑」：落 done 哨兵让看门狗
    退场（观测通道坏了宁可失保不误杀）+ 大声警告（de98212/c-1 同款可观测性要求）。"""
    global _HEARTBEAT_DIR
    if _HEARTBEAT_DIR is None:
        return False
    try:
        write_json_atomic(_HEARTBEAT_DIR / SESSION_HEARTBEAT, _heartbeat_payload(label))
    except OSError as e:
        print(f"[warn] 心跳写入失败（{e}）——本会话降级为无看门狗运行，楔死需人工介入")
        try:
            (_HEARTBEAT_DIR / SESSION_DONE).touch()
        except OSError:
            print("[warn] done 哨兵也写不进去——看门狗将在停摆阈值点强杀本会话")
        _HEARTBEAT_DIR = None
        return False
    # #56 A4：节拍即武装点——写心跳成功就代表主线程还活着，把「无心跳 90s 落全栈」的
    # 定时器推到下一拍。A3 把拍点铺到每个无超时协议调用前后，武装精度=节拍密度。
    _arm_wedge_dump()
    return True


def _wedge_dump_enabled():
    """LIVE_WEDGE_DUMP：#56 A4 取栈的逃生开关。

    默认开；只有显式 "0" / "off"（`strip()` + `lower()`，即大小写与首尾空白不敏感）关；
    **其它值一律按开**——包括空串和拼错的 "ofs"。理由：关掉取证的代价是「下一次楔死又只剩
    heartbeat 一条线索」（本票 5 场楔死的现状），而写错值把取证留着没有任何代价。
    关闭时零 faulthandler 调用（既不武装也不取消），与真实会话一刀切开。

    这张表与仓内另外几张布尔 env 取值表**各不相同**（#56 修复轮 F2 逐字核对过，旧注释
    「口径照 LIVE_RECORD / LIVE_PREFLIGHT」不成立）；**多张表的逐行对照只有一份真源**，
    在 `_limits.py` 的「布尔 env 开关」小节，这里不再抄一遍。按**行为**钉着这三张表的用例是
    `scripts/test_live_apply.py::test_three_bool_env_switches_have_three_distinct_value_tables`。
    本函数的解析参数逐项就是上面这套口径：strip + lower、关只认 `BOOL_ENV_TWO_OFF`、
    非法值**不**提示（`warn_on_unknown` 不设）——与接线前的 `raw.strip().lower() not in ("0", "off")`
    逐字等价（空串与未设置同样落回「按开」；「默认开」这个方向现在由 `env_bool_switch` 固定实现，
    #59 修复轮 R4-4 删掉了没有调用方的 `default_on=False` 分支）。"""
    return env_bool_switch(os.environ, WEDGE_DUMP_ENV, off_values=BOOL_ENV_TWO_OFF, strip=True, lower=True)


def _net_probe_enabled():
    """LIVE_NET_PROBE：击杀前出网探针（#56 取证 B）的逃生开关。
    取值表与 `_wedge_dump_enabled` **同一张**（strip + lower、关只认 `BOOL_ENV_TWO_OFF`、
    非法值静默按开），逐行对照的真源在 `_limits.py` 的「布尔 env 开关」小节，这里不复述。
    为什么默认开：探针是**唯一**能把嫌疑③（这台机器此刻不通）从另外两个嫌疑里分开的读数，
    写错一个字母把它关了，代价是下一次楔死又只剩「分不清」这三个字。
    为什么要有开关：这台机器的代理/防火墙一旦变化，探测本身可能在击杀前拖满超时——
    留一条不用改代码就能退路出的口子（关 ⇒ 完全不探测，marker 里那个字段落 None）。"""
    return env_bool_switch(os.environ, NET_PROBE_ENV, off_values=BOOL_ENV_TWO_OFF, strip=True, lower=True)


def _close_wedge_file():
    """释放武装句柄并复位句柄状态（不碰 faulthandler）——开关中途被关掉时也要收得掉现场。"""
    global _WEDGE_FH, _WEDGE_FH_DIR
    fh, _WEDGE_FH = _WEDGE_FH, None
    _WEDGE_FH_DIR = None
    if fh is not None:
        try:
            fh.close()
        except Exception:  # noqa: BLE001  # 关不掉只是漏一个 fd，不值得为取证层添错
            pass


def _arm_wedge_dump():
    """武装「无心跳 WEDGE_STACK_SECONDS 秒 → 自动 dump 全线程栈到会话目录/wedge-stack.txt」。

    为什么只能这样取栈：楔死时主线程停在无超时的协议往返上（querySelector / boundingBox /
    evaluateExpression / mouse* / keyboard* 全是 timeout=None，票面机制口径 §1），进程内
    不可能再执行任何 Python 代码（§2）——事后调 dump_traceback 的口子是关的。
    faulthandler 的 later 定时器跑在解释器层，到点直接写栈，不需要主线程回到字节码。
    弃「等 py-spy + root」：同一取证三次没采到，它不是可依赖路径。

    先 cancel 再 arm：这一拍成功=线程还活着=旧定时器作废。不先 cancel，旧那台的 90s 会在
    当前拍之后不足 90s 的某一刻到期，给一场健康会话落一份假栈（C 段判据就此被污染）。
    exit=False：杀人不是取证层的活——SIGKILL 归进程外看门狗，这里只负责在死之前把栈留下。

    降级铁律（#38/#54 同口径）：任何异常都吞掉、打一行 warn 后本会话不再尝试。
    观测/取证层绝不新增失败面：心跳照写、看门狗照杀，只是少一份栈。"""
    global _WEDGE_FH, _WEDGE_FH_DIR, _WEDGE_DUMP_BROKEN
    if _WEDGE_DUMP_BROKEN or not _wedge_dump_enabled():
        return
    try:
        faulthandler.cancel_dump_traceback_later()
        if _HEARTBEAT_DIR is None:
            return  # 无处落盘就不武装（取消照做，不留悬着的定时器）
        if _WEDGE_FH is not None and _WEDGE_FH_DIR != _HEARTBEAT_DIR:
            _close_wedge_file()  # 会话目录被重绑：句柄跟着换，否则栈落进上一场的目录
        if _WEDGE_FH is None:
            _WEDGE_FH = open(_HEARTBEAT_DIR / WEDGE_STACK, "a", encoding="utf-8")
            _WEDGE_FH_DIR = _HEARTBEAT_DIR
        faulthandler.dump_traceback_later(WEDGE_STACK_SECONDS, file=_WEDGE_FH, exit=False)
    except Exception as e:  # noqa: BLE001
        _WEDGE_DUMP_BROKEN = True
        _close_wedge_file()
        print(f"[warn] 楔死取栈武装失败（{str(e)[:60]}）——本会话不再尝试，楔死时只剩心跳取证")


def _cancel_wedge_dump():
    """收尾取消武装——调用点必须在 done 那一拍**之后**：done 之前取消的话，用户 CLOSE 的
    长等待（合法无心跳，见 `wait_close_sentinel`）会落一份「正常收尾」的假栈。

    幂等：未武装时 `cancel_dump_traceback_later()` 不抛（py3.12.9 实测），故无条件先取消。
    开关关闭时零 faulthandler 调用，但句柄照常释放。"""
    if not _wedge_dump_enabled():
        _close_wedge_file()
        return
    try:
        faulthandler.cancel_dump_traceback_later()
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 楔死取栈取消失败（{str(e)[:60]}）——收尾等待期间可能落一份假栈")
    finally:
        _close_wedge_file()


def _reset_wedge_dump_state():
    """取栈状态整体复位：取消定时器 + 释放句柄 + 清掉武装失败标记。
    测试用例之间必须复位（faulthandler 是全进程单例，一个用例武装的定时器会串进后续
    800+ 用例，在随机时刻往别的会话目录落假栈）；生产路径上每个会话由 `_cancel_wedge_dump`
    收尾，这个函数是它的「无条件版」——不看开关、不看是否已武装，也不抛。"""
    global _WEDGE_DUMP_BROKEN
    _WEDGE_DUMP_BROKEN = False
    _close_wedge_file()
    try:
        faulthandler.cancel_dump_traceback_later()
    except Exception:  # noqa: BLE001  # 复位兜底：取消失败也不能把 teardown 变成抛异常的地方
        pass


def _heartbeat_beat(label):
    """#56 A3 的注入形态：把协议调用前后的拍点直接落成心跳节拍（`beat_fn` 契约的实现）。

    为什么薄封装要吞掉一切异常：被注入的一方是**动作代码**——`human_pointer_walk` 整段包在
    try/except 里、`coordinate_click` 的异常等价于「不点」。拍点若抛出去，指针轨迹会静默消失、
    坐标点击退回 handle 点击，那就把取证做成了行为变更（A 段口径：除 A1 外零行为变更）。
    `_touch_heartbeat` 自己只兜 OSError（那是它降级为「无看门狗」的分支），其余异常在这里兜。

    与 A4 的关系：拍点即武装点——细颗粒节拍把 `_touch_heartbeat` 的调用密度提上去，
    faulthandler 的取栈定时器随之每一拍重武装，楔死落栈的延迟上界从「距上一次节点 90s」
    收紧到「距上一个协议调用 90s」。"""
    try:
        _touch_heartbeat(label)
    except Exception:  # noqa: BLE001  # 观测层绝不新增失败面（#38/#54 同口径）
        pass


# --- #56 取证 A：「挂着几个监听器 / 几个往返在途」上心跳 ---
#
# 为什么现有取证分不开三个嫌疑：`wedge-stack.txt` 是 faulthandler 拍的**线程栈**，而 Playwright
# 的 `page.on("response")` 处理器跑在 dispatcher 切换的 greenlet 上——它**不出现在线程栈里**。
# 于是「我方 handler 里那次 `resp.json()` 挂住」（嫌疑①）与「网站/浏览器把 renderer 占死」（嫌疑②）
# 在栈文件里长得一模一样，只能另找一条不依赖线程栈的读数。这三键就是那条读数。


def _handler_label_tail(url_frag):
    """url_frag → label 用的端点尾段（`handler:detail.json:enter` 里那截）。
    为什么只取尾段、不塞完整 URL：心跳文件是给人一眼读的，完整 frag 常带路径与 query
    （岗位 id、分页参数），把它拼进 label 会让 `heartbeat.json` 变成 URL 转储，还会挤掉
    同一行里真正重要的相位信息（eid / in_job / 计数）。"""
    try:
        return str(url_frag or "").rstrip("/").rsplit("/", 1)[-1][:40] or "?"
    except Exception:  # noqa: BLE001  # 观测层绝不新增失败面（同 `_heartbeat_beat`）
        return "?"


def listener_attached(handler):
    """登记一个已经 `page.on("response", h)` 挂上的处理器（按对象身份）。
    调用点必须在 `page.on` **成功之后**：`on` 自身抛异常时什么都没挂上，先计数就等于虚报一个泄漏。"""
    try:
        _HANDLER_REGISTRY.add(handler)
    except Exception:  # noqa: BLE001  # 观测层绝不新增失败面（同 `_heartbeat_beat`）
        pass


def listener_detached(handler):
    """注销登记。用 `discard` 而不是 `remove`：重复注销幂等——取证层把「本来就没挂着」
    当成一次失败，等于给业务路径新开一个异常出口（A1 的 `finally` 会跑多次是常态）。"""
    try:
        _HANDLER_REGISTRY.discard(handler)
    except Exception:  # noqa: BLE001  # 观测层绝不新增失败面（同 `_heartbeat_beat`）
        pass


def handler_roundtrip_enter(url_frag):
    """一次「handler 内的协议往返」**开始**：计数 +1，然后立刻自己拍一拍心跳。
    记的是 url_frag 的完整片段（上限 120 字符，取证文件不是 URL 转储），label 只带尾段。

    为什么必须在 +1 之后**立刻落盘**，而不是等下一个正常节拍来读这个计数（本函数的全部设计
    就在这一步）：卡死时主线程再也不会拍点，于是「最后一次写盘的状态」必须是**进入往返之前**
    的那一刻。只在正常节拍里读计数，handler 挂住那一拍的 `inflight=1` 永远来不及落盘，
    取证就白做——看到的还是 `inflight=0`，正好把要抓的东西反着写。
    反过来这条读数也负责证伪：卡死时心跳里 `handlers_inflight=0`，「往返挂住」这个嫌疑就被
    排除，嫌疑移向浏览器（②）或出网（③，见 `_selfguard` 的击杀前探针与 `NET_PROBE_*` 常量）。

    为什么这一拍还写得进去：greenlet 切换不离开主线程，拍点落在挂住的那次往返**之前**，
    磁盘 IO 已经做完；`_touch_heartbeat` 的「必须在主线程调用」口径一字未变。
    （往返成功返回时 `handler_roundtrip_exit` 再拍一拍，健康会话因此每端点多一次写盘——
    这是取证的代价，不是行为变更：投递路径、容器与事件名一字不动。）"""
    global _HANDLER_INFLIGHT, _HANDLER_LAST_ENTER
    try:
        _HANDLER_LAST_ENTER = str(url_frag)[:120]
        _HANDLER_INFLIGHT += 1
        tail = _handler_label_tail(_HANDLER_LAST_ENTER)
    except Exception:  # noqa: BLE001  # 观测层绝不新增失败面（同 `_heartbeat_beat`）
        tail = "?"
    _heartbeat_beat(f"handler:{tail}:enter")


def handler_roundtrip_exit():
    """一次往返**结束**（成功与失败都走这里，靠 `try/finally` 保证平衡）：计数 -1，再拍一拍。
    为什么计数有下限 0：不平衡的读数比零更坏——它会让「下一次卡死时 inflight=1」变成
    永远解释不清的账。异常路径上如果哪里漏了一次 enter 的配对，宁可让计数偏小、也不要它漂移。"""
    global _HANDLER_INFLIGHT
    try:
        if _HANDLER_INFLIGHT > 0:
            _HANDLER_INFLIGHT -= 1
        tail = _handler_label_tail(_HANDLER_LAST_ENTER)
    except Exception:  # noqa: BLE001  # 观测层绝不新增失败面（同 `_heartbeat_beat`）
        tail = "?"
    _heartbeat_beat(f"handler:{tail}:exit")


def handler_forensics():
    """监听器 / 往返三键的当前读数（键名取自 `HEARTBEAT_FIELDS`，与心跳 payload 同一个形状）。
    生产路径由 `_heartbeat_payload` 逐拍调用；单列出来是给「现场还想再看一眼」的读法，
    也让 payload 与这三键只有**一个**生产者。"""
    return {
        _HANDLERS_LIVE: len(_HANDLER_REGISTRY),
        _HANDLERS_INFLIGHT: _HANDLER_INFLIGHT,
        _HANDLER_LAST_FRAG: _HANDLER_LAST_ENTER,
    }


def _reset_handler_forensics():
    """监听器 / 往返状态整体复位（测试用例之间的隔离，形态同 `_reset_wedge_dump_state`）。
    注册表必须**清空**而不是只归零计数：残留的是 handler 闭包，它一路引用着 container 与
    page 替身——既漏内存，也把 `handlers_live` 抬高到不可信（而这正是取证 A 唯一的读数）。"""
    global _HANDLER_INFLIGHT, _HANDLER_LAST_ENTER
    _HANDLER_INFLIGHT = 0
    _HANDLER_LAST_ENTER = None
    try:
        _HANDLER_REGISTRY.clear()
    except Exception:  # noqa: BLE001  # 复位兜底：teardown 不许变成抛异常的地方
        pass
