#!/usr/bin/env python3
"""
推特调研标准化（#14 T5）：多策略抓取目标 handle 的推文 → 原始数据 JSON + LLM 综合调研草稿。

用法：
  python3 scripts/twitter_survey.py --handle <handle> [--max-tweets 30] [--no-llm]
                                   [--instances "a.com,b.com"] [--output-dir DIR]
                                   [--draft-dir DIR]

产出（HITL 边界与 T3/T4 一致：不写正式层）：
  private/data/twitter/<handle>.json          结构化原始数据
                                              {handle, fetched_at, strategy_used,
                                               tweets: [{date, text, url}], errors}
  private/kb/draft/twitter-<handle>-draft.md  LLM 综合调研草稿（活跃度/关注话题/技术观点/
                                              近期动态/可引用锚点），供人工审核并入知识库
                                              （gen_greetings.py 的 sources 字段认可"推特推文日期"）

抓取策略（按序 fallback，多策略防反爬；每策略失败原因记入 errors）：
  1. nitter 镜像 SSR（urllib；实例列表 NITTER_INSTANCES 模块常量，可被 env
     TWITTER_NITTER_INSTANCES 或 --instances 覆盖；逐个尝试，超时/失败/空结果换下一个）；
  2. x.com SSR（urllib 带浏览器 UA；未登录 SSR 反爬强，大概率拿不到，记录在案的策略）；
  3. camoufox 无头浏览器抓 x.com（懒加载 import，测试环境无强依赖；等页面渲染后提取推文 DOM；
     env TWITTER_CAMOUFOX_PROXY 非空时以显式 proxy 启动——Firefox 系不读 https_proxy）。
  策略之间礼貌间隔随机 2-5s（风控礼貌）。

JSON 快照语义：原始数据是"当前快照"，重跑默认**覆盖**旧文件（非增量、不合并）。

LLM 综合（--no-llm 跳过，只落 JSON）：推文素材（带日期+链接）→ 调研草稿。
  禁推测约束（进 prompt）：只综合素材、不得编造，关键结论标注推文日期；
  素材不足的板块写「素材不足，需补充调研」。

LLM：OpenAI 兼容 /v1/chat/completions。默认本地 Ollama（qwen3:8b），
     GREETING_LLM_BASE_URL / GREETING_LLM_MODEL / GREETING_LLM_API_KEY 可换任意兼容服务。

注入式测试：fetch/parse/camoufox/sleep/now/LLM 均可注入（见 test_twitter_survey.py），
测试不打真网真 LLM；策略编排（1→2→3 fallback、全失败 exit 1）是测试重点。
"""

import argparse
import ipaddress
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _atomic_io import write_json_atomic, write_text_atomic
from _llm import call_llm, extract_json, llm_config

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = ROOT / "private/data/twitter"
DEFAULT_DRAFT_DIR = ROOT / "private/kb/draft"
MAX_TWEETS = 30
FETCH_TIMEOUT = 30
CAMOUFOX_SETTLE_SECONDS = 5  # camoufox 打开 x.com 后等推文列表渲染的秒数（x.com 重 JS 渲染）

# 素材总预算（对齐本地 qwen3:8b 上下文）：做法参照 industry_research.py 的 MAX_MATERIAL_CHARS。
MAX_MATERIAL_CHARS = 24000

# nitter 公开实例（大多不稳定，按当前可用性排前；可被 env TWITTER_NITTER_INSTANCES /
# --instances 覆盖，全部失败自动回退策略2/3）
NITTER_INSTANCES = ["xcancel.com", "nitter.net", "lightbrd.com", "nitter.poast.org", "nitter.tiekoetter.com"]
X_PROFILE_URL = "https://x.com/{handle}"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 抓取前防御：私网/环回网段不抓（参照 industry_research.py 同名防御）
_PRIVATE_NETS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
)

# camoufox 在 x.com 页面内提取推文：article[data-testid=tweet] 的正文/日期/链接
_X_DOM_EXTRACT_JS = """() => {
  const out = [];
  document.querySelectorAll('article[data-testid="tweet"]').forEach((el) => {
    const textEl = el.querySelector('[data-testid="tweetText"]');
    const timeEl = el.querySelector('time[datetime]');
    const linkEl = el.querySelector('a[href*="/status/"]');
    out.push({
      date: timeEl ? timeEl.getAttribute('datetime') : '',
      text: textEl ? textEl.innerText : '',
      url: linkEl ? linkEl.href : ''
    });
  });
  return out;
}"""

SURVEY_SYSTEM = (
    "你是求职调研编辑，正在起草某推特（X）账号的调研档案草稿。"
    "只依据给出的推文素材综合，不得编造或推测素材里没有的信息；"
    "每条关键结论句末标注来源推文的日期（如（2026-09-01））；"
    "素材不足以支撑的板块整节写「素材不足，需补充调研」，不要推测填充。"
    "按以下小节结构输出 Markdown 正文（不含一级标题）：\n"
    "## 活跃度\n## 关注话题\n## 技术观点\n## 近期动态\n## 可引用锚点\n"
    "其中「可引用锚点」要点明哪条推文（带日期与链接）适合在求职招呼语中引用、为什么。"
    '输出 JSON：{"markdown": "Markdown 正文"} /no_think'
)

MERGE_GUIDE = """[合并指引] 草稿仅供人工审核（HITL），本脚本不写正式层：
  1. 逐条核对结论与素材推文（private/data/twitter/<handle>.json 里的链接/日期）是否一致
     （禁推测约束由 prompt 承担，LLM 仍可能出错）；
  2. 标「素材不足，需补充调研」的板块需人工补充调研后再定稿；
  3. 审核通过后手动并入正式 KB，可删本草稿；并入时保留推文日期
     （gen_greetings.py 的 sources 字段认可"推特推文日期"）。"""


# --- 输入规范化 ---

# twitter handle 规则：仅字母/数字/下划线，最长 15 位
_HANDLE_RE = re.compile(r"[A-Za-z0-9_]{1,15}")


def normalize_handle(handle):
    """去首尾空白与 @ 前缀（约定 handle 不带 @，带 @ 自动剥离）；空 handle 报错。
    handle 字符按 twitter 规则校验（仅 [A-Za-z0-9_]，1-15 位），不合法报 ValueError
    （main 里转 argparse error）。"""
    h = (handle or "").strip().lstrip("@").strip()
    if not h:
        raise ValueError("handle 不能为空")
    if not _HANDLE_RE.fullmatch(h):
        raise ValueError(f"无效 handle: {h!r}（仅允许字母/数字/下划线，1-15 位）")
    return h


def nitter_instances(instances=None, env=None):
    """nitter 实例列表：参数 > env TWITTER_NITTER_INSTANCES（逗号分隔）> 内置常量列表。"""
    env = os.environ if env is None else env
    if instances:
        return [s.strip() for s in instances if s and s.strip()]
    raw = env.get("TWITTER_NITTER_INSTANCES", "")
    if raw.strip():
        return [s.strip() for s in raw.split(",") if s.strip()]
    return list(NITTER_INSTANCES)


# --- 抓取与解析 ---


def fetch_url(url):
    """urllib 抓 URL → HTML 文本（浏览器 UA；nitter SSR 与 x.com SSR 共用）。
    抓取前防御：主机为私网/环回地址（127/8、10/8、172.16/12、192.168/16、
    169.254/16、localhost、::1）不抓，返回空文本。响应体最多读 2MB 再 decode。"""
    host = urllib.parse.urlparse(url).hostname
    if not host or host.lower() == "localhost":
        return ""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        pass  # 域名，放行交给正常抓取
    else:
        if any(addr in net for net in _PRIVATE_NETS):
            return ""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read(2 * 1024 * 1024).decode(charset, errors="replace")


class _NitterTimelineParser(HTMLParser):
    """收集 <div class="timeline-item"> 内的推文：正文（tweet-content）、
    日期（tweet-date 内 <a> 的 title，缺省回退链接文本）、链接（tweet-link 或日期链接 href）。
    真实 nitter 渲染 <span class="tweet-date">（位于 tweet-header > tweet-name-row 内），
    div 写法也兼容。span 不改变 div 深度，date_at 记录当前 div 深度、span 闭合即复位；
    引用推文（quote）内嵌的 tweet-content 不并入主推文正文，quote 内日期/链接
    因 cur["date"]/cur["url"] 已置值也不会覆盖主推文的。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tweets = []
        self.depth = 0  # 当前 div 嵌套深度
        self.item_at = None  # timeline-item 起始的 div 深度（None=不在 item 内）
        self.cur = None
        self.content_at = None  # tweet-content 起始的 div 深度
        self.content_done = False  # 主推文正文已采集（防引用推文正文混入）
        self.date_at = None  # tweet-date 生效的 div 深度

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        if tag == "div":
            self.depth += 1
            if self.item_at is None and "timeline-item" in cls:
                self.item_at = self.depth
                self.cur = {"date": "", "text": "", "url": ""}
                self.content_done = False
            elif self.item_at is not None:
                if "tweet-content" in cls and self.content_at is None and not self.content_done:
                    self.content_at = self.depth
                elif "tweet-date" in cls and self.date_at is None:
                    self.date_at = self.depth
            return
        if self.item_at is None:
            return
        if tag == "span" and "tweet-date" in cls and self.date_at is None:
            self.date_at = self.depth  # span 不改 div 深度，记当前 div 深度
            return
        if tag == "br" and self.content_at is not None:
            self.cur["text"] += " "  # 换行补空格，防前后词粘连
        elif tag == "a":
            href = a.get("href", "")
            if "tweet-link" in cls and href and not self.cur["url"]:
                self.cur["url"] = href
            elif self.date_at is not None and self.depth >= self.date_at:
                if a.get("title") and not self.cur["date"]:
                    self.cur["date"] = a["title"]
                if href and "/status/" in href and not self.cur["url"]:
                    self.cur["url"] = href

    def handle_data(self, data):
        if self.item_at is None:
            return
        if self.content_at is not None and self.depth >= self.content_at:
            self.cur["text"] += data
        elif self.date_at is not None and self.depth >= self.date_at and not self.cur["date"]:
            d = data.strip()
            if d:
                self.cur["date"] = d  # 回退：日期链接文本（如 "Sep 1"）

    def handle_endtag(self, tag):
        if tag == "span" and self.date_at is not None and self.depth == self.date_at:
            self.date_at = None  # span.tweet-date 闭合复位（span 不影响 div 深度）
        if tag != "div" or self.depth <= 0:
            return
        if self.content_at is not None and self.depth == self.content_at:
            self.content_at = None
            self.content_done = True
        if self.date_at is not None and self.depth == self.date_at:
            self.date_at = None
        if self.item_at is not None and self.depth == self.item_at:
            self._flush()
            self.item_at = None
        self.depth -= 1

    def _flush(self):
        c, self.cur = self.cur, None
        if not c:
            return
        text = re.sub(r"\s+", " ", c.get("text", "")).strip()
        if text:
            self.tweets.append({"date": (c.get("date") or "").strip(), "text": text, "url": c.get("url") or ""})


# 真实 nitter 的日期链接带 #m 片段（如 /alice/status/1001#m）；
# 快照 URL 统一归一化为 x.com canonical（与策略3 camoufox 原生链接一致）
_STATUS_PATH_RE = re.compile(r"/([^/?#]+)/status/(\d+)")


def canonical_status_url(url):
    """推文 URL 归一化：剥 #m 等 fragment，路径为 /{user}/status/{id} 时改写为
    canonical https://x.com/{user}/status/{id}；解析不出 status id 的原样返回（不硬造）。"""
    if not url:
        return url
    url = urllib.parse.urldefrag(url).url
    m = _STATUS_PATH_RE.fullmatch(urllib.parse.urlparse(url).path)
    if not m:
        return url
    return f"https://x.com/{m.group(1)}/status/{m.group(2)}"


def parse_nitter_tweets(html, base_url=""):
    """nitter 用户页 HTML → 推文列表 [{date,text,url}]：相对链接以 base_url 补全，
    URL 剥 #m 片段后归一化为 https://x.com/{user}/status/{id} canonical（与策略3
    camoufox 原生链接统一），按 URL（无链接则按正文）去重，无正文条目丢弃。
    非推文页/被反爬页返回 []。"""
    parser = _NitterTimelineParser()
    parser.feed(html or "")
    out, seen = [], set()
    for t in parser.tweets:
        url = t["url"]
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            url = base_url.rstrip("/") + url
        url = canonical_status_url(url)
        key = url or t["text"]
        if key in seen:
            continue
        seen.add(key)
        out.append({"date": t["date"], "text": t["text"], "url": url})
    return out


_FULL_TEXT_RE = re.compile(r'"full_text"\s*:\s*"((?:[^"\\]|\\.)*)"')


def parse_x_ssr_tweets(html):
    """从 x.com SSR 页面内嵌 JSON 挖推文正文（full_text 字段，解码转义）。
    未登录 SSR 大概率只有应用壳（无推文数据）→ 返回 []；日期/链接壳里不携带，留空。"""
    out, seen = [], set()
    for m in _FULL_TEXT_RE.finditer(html or ""):
        try:
            text = json.loads(f'"{m.group(1)}"').strip()
        except json.JSONDecodeError:
            continue
        if text and text not in seen:
            seen.add(text)
            out.append({"date": "", "text": text, "url": ""})
    return out


def nitter_fetch(handle, instances=None, fetch_fn=None, parse_fn=None, errors=None):
    """策略1：nitter 镜像 SSR。逐实例尝试（超时/失败/空结果换下一个），逐实例失败
    原因以 "nitter(host): 原因" 追加进 errors（可观测，不只 print）；
    命中的实例返回推文列表；全部实例报错则抛最后一个异常，全部空结果返回 []。"""
    if fetch_fn is None:
        fetch_fn = fetch_url
    if parse_fn is None:
        parse_fn = parse_nitter_tweets
    last_err = None
    for host in nitter_instances(instances):
        url = f"https://{host}/{handle}"
        print(f"[nitter] 尝试 {url} ...")
        try:
            html = fetch_fn(url)
        except Exception as e:
            print(f"  [跳过] {host}: {e}")
            if errors is not None:
                errors.append(f"nitter({host}): {e}")
            last_err = e
            continue
        tweets = parse_fn(html, f"https://{host}")
        if tweets:
            print(f"  [命中] {host}: {len(tweets)} 条推文")
            return tweets
        print(f"  [跳过] {host}: 无结果（实例失效或被反爬）")
        if errors is not None:
            errors.append(f"nitter({host}): 无结果")
    if last_err is not None:
        raise last_err
    return []


def x_ssr_fetch(handle, fetch_fn=None, parse_fn=None):
    """策略2：x.com SSR（浏览器 UA）。未登录 SSR 反爬强、大概率拿不到推文，
    记录在案的策略：抓到什么解析什么，无数据返回 [] 自动进策略3。"""
    if fetch_fn is None:
        fetch_fn = fetch_url
    if parse_fn is None:
        parse_fn = parse_x_ssr_tweets
    return parse_fn(fetch_fn(X_PROFILE_URL.format(handle=handle)))


def camoufox_kwargs(env=None):
    """Camoufox 启动参数：env TWITTER_CAMOUFOX_PROXY 非空时附加显式 proxy。"""
    env = os.environ if env is None else env
    kwargs = {"humanize": True, "geoip": True, "os": "macos", "headless": True}
    proxy = env.get("TWITTER_CAMOUFOX_PROXY", "").strip()
    if proxy:
        kwargs["proxy"] = {"server": proxy}
    return kwargs


def camoufox_fetch(handle, max_tweets=MAX_TWEETS):
    """策略3（fallback）：camoufox 无头浏览器抓 x.com。
    懒加载 import（测试环境不装也不受影响，参照 industry_research.py/crawl_jds.py）；
    页面渲染后从 DOM 提取推文正文/日期/链接。"""
    from camoufox.sync_api import Camoufox

    url = X_PROFILE_URL.format(handle=handle)
    with Camoufox(**camoufox_kwargs()) as browser:
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(CAMOUFOX_SETTLE_SECONDS)  # 等推文列表渲染
        items = page.evaluate(_X_DOM_EXTRACT_JS)
    tweets, seen = [], set()
    for it in items or []:
        text = (it.get("text") or "").strip()
        if not text:
            continue
        key = it.get("url") or text
        if key in seen:
            continue
        seen.add(key)
        tweets.append({"date": it.get("date") or "", "text": text, "url": it.get("url") or ""})
        if len(tweets) >= max_tweets:
            break
    return tweets


def default_sleep():
    """策略之间的风控礼貌间隔：随机 2-5s。"""
    time.sleep(random.uniform(2, 5))


def survey(
    handle,
    max_tweets=MAX_TWEETS,
    instances=None,
    nitter_fn=None,
    ssr_fn=None,
    camoufox_fn=None,
    sleep_fn=None,
    now=None,
):
    """多策略抓取编排：策略1 nitter → 策略2 x.com SSR → 策略3 camoufox，按序 fallback。
    回退前 sleep_fn()（默认随机 2-5s）；每策略失败原因记入 errors；
    命中即返回 {handle, fetched_at, strategy_used, tweets, errors}（空正文条目过滤、
    按 max_tweets 截断）；全部失败抛 RuntimeError（带全部原因）。"""
    if nitter_fn is None:

        def nitter_fn():
            return nitter_fetch(handle, instances, errors=errors)

    if ssr_fn is None:

        def ssr_fn():
            return x_ssr_fetch(handle)

    if camoufox_fn is None:

        def camoufox_fn():
            return camoufox_fetch(handle, max_tweets)

    if sleep_fn is None:
        sleep_fn = default_sleep
    now_dt = now or datetime.now()

    errors = []
    strategies = [("nitter", nitter_fn), ("x_ssr", ssr_fn), ("camoufox", camoufox_fn)]
    for i, (name, fn) in enumerate(strategies):
        if i > 0:
            sleep_fn()
        try:
            tweets = fn()
        except Exception as e:
            errors.append(f"{name}: {e}")
            print(f"[策略] {name} 失败: {e}")
            continue
        tweets = [t for t in tweets if (t.get("text") or "").strip()][:max_tweets]
        if tweets:
            print(f"[策略] {name} 命中 {len(tweets)} 条推文")
            return {
                "handle": handle,
                "fetched_at": now_dt.isoformat(timespec="seconds"),
                "strategy_used": name,
                "tweets": tweets,
                "errors": errors,
            }
        errors.append(f"{name}: 无结果")
        print(f"[策略] {name} 无结果")
    raise RuntimeError("三种策略均未抓到推文: " + "; ".join(errors))


# --- LLM 综合 ---


def build_material(tweets, budget=MAX_MATERIAL_CHARS):
    """推文列表 → 素材文本：每条一节，带日期与链接（供 LLM 标注推文日期、挑可引用锚点）。
    日期缺失标「日期不明」，无链接不写链接行。总量超 budget 按推文序截断：
    节头（日期+链接）完整保留，正文按剩余预算截断（最后一条可部分截断）。"""
    parts, total = [], 0
    for i, t in enumerate(tweets, 1):
        header = f"### 推文{i}（{t.get('date') or '日期不明'}）\n"
        tail = f"\n链接: {t['url']}" if t.get("url") else ""
        sep = "\n\n" if parts else ""
        remaining = budget - total - len(sep)
        need = len(header) + len(tail)
        if remaining <= need:
            break
        parts.append(sep + header + t["text"][: remaining - need] + tail)
        total += len(parts[-1])
    return "".join(parts)


def synthesize(handle, material, llm_call, cfg):
    """LLM 综合：禁推测 system prompt + 推文素材 → 该 handle 的调研草稿 Markdown 正文。"""
    user = f"""# 目标账号

@{handle}

# 推文素材（每条带日期与链接）

{material}

只依据以上素材综合输出 JSON（每条关键结论标注来源推文的日期）。"""
    out = extract_json(llm_call([{"role": "system", "content": SURVEY_SYSTEM}, {"role": "user", "content": user}], cfg))
    md = (out.get("markdown") or "").strip()
    if not md:
        raise ValueError("markdown 为空")
    return md


def render_draft(handle, markdown, n_tweets, strategy_used, now=None):
    """组装草稿文本：标题 + 元信息（生成时间/抓取策略/推文条数）+ 草稿边界声明 + 正文。"""
    now_s = (now or datetime.now()).isoformat(timespec="seconds")
    return "\n".join(
        [
            f"# 推特调研草稿：@{handle}（twitter_survey.py 自动生成）",
            "",
            f"> 生成时间：{now_s}；抓取策略：{strategy_used}；素材：{n_tweets} 条推文"
            "（原始数据见 private/data/twitter/ 下的 JSON，日期与链接标注于素材与正文）",
            "> 本文件是**草稿**：LLM 综合公开推文素材而成，可能有错漏；"
            "人工审核、核对推文日期后手动并入正式 KB，本脚本不写正式层。",
            "",
            markdown.strip(),
            "",
        ]
    )


# --- 主流程 ---


def run(
    handle,
    max_tweets=MAX_TWEETS,
    no_llm=False,
    instances=None,
    output_dir=DEFAULT_OUTPUT_DIR,
    draft_dir=DEFAULT_DRAFT_DIR,
    survey_fn=None,
    llm_call=None,
    cfg=None,
    sleep_fn=None,
    now=None,
):
    """主流程：handle 规范化（剥 @）→ 多策略抓取 → JSON 快照落盘（覆盖旧文件）→
    LLM 综合草稿。抓取全失败 sys.exit(1)；LLM 失败时数据已保留，同样 sys.exit(1)
    （可重跑重综合）。--no-llm 只抓数据。返回结果 dict。"""
    handle = normalize_handle(handle)
    now_dt = now or datetime.now()
    if llm_call is None:

        def llm_call(msgs, c):
            return call_llm(msgs, c)

    cfg = cfg or {}

    # 1. 多策略抓取
    try:
        if survey_fn is not None:
            data = survey_fn(handle)
        else:
            data = survey(handle, max_tweets=max_tweets, instances=instances, sleep_fn=sleep_fn, now=now_dt)
    except Exception as e:
        print(f"[错误] 推文抓取失败（三种策略均未拿到推文）: {e}")
        sys.exit(1)

    # 2. JSON 快照（原始数据快照语义：重跑覆盖）
    json_path = Path(output_dir) / f"{handle}.json"
    write_json_atomic(json_path, data)
    print(f"[数据] {len(data['tweets'])} 条推文（策略 {data['strategy_used']}）→ {json_path}")

    if no_llm:
        print("[done] --no-llm：只抓原始数据，跳过 LLM 综合")
        return data

    # 3. LLM 综合调研草稿
    print("[综合] 起草调研档案 ...")
    try:
        md = synthesize(handle, build_material(data["tweets"]), llm_call, cfg)
    except Exception as exc:
        print(f"[错误] LLM 综合失败: {exc}（原始数据已存 {json_path}，可重跑重综合）")
        sys.exit(1)
    draft_path = Path(draft_dir) / f"twitter-{handle}-draft.md"
    write_text_atomic(draft_path, render_draft(handle, md, len(data["tweets"]), data["strategy_used"], now=now_dt))
    print(f"[草稿] {draft_path}")
    print(MERGE_GUIDE)
    return data


def _positive_int(text):
    """argparse type：要求 >= 1 的整数（0/负数/非数字报 argparse error）。"""
    try:
        v = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"无效整数: {text!r}")
    if v < 1:
        raise argparse.ArgumentTypeError(f"必须 >= 1，收到: {text!r}")
    return v


def main():
    parser = argparse.ArgumentParser(
        description="推特调研：多策略抓取推文 + LLM 综合 → 原始数据 JSON + 调研草稿（#14 T5，草稿供人工审核合并）"
    )
    parser.add_argument("--handle", required=True, help="目标 handle（不带 @ 前缀；带 @ 自动剥离）")
    parser.add_argument(
        "--max-tweets",
        type=_positive_int,
        default=MAX_TWEETS,
        help=f"保存的推文条数上限（默认 {MAX_TWEETS}，必须 >= 1）",
    )
    parser.add_argument("--no-llm", action="store_true", help="只抓原始数据 JSON，跳过 LLM 综合草稿")
    parser.add_argument(
        "--instances",
        default="",
        help="逗号分隔的 nitter 实例列表覆盖（默认内置常量列表，也可用 env TWITTER_NITTER_INSTANCES 覆盖）",
    )
    parser.add_argument(
        "--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="原始数据输出目录（默认 private/data/twitter）"
    )
    parser.add_argument("--draft-dir", default=str(DEFAULT_DRAFT_DIR), help="草稿输出目录（默认 private/kb/draft）")
    args = parser.parse_args()
    instances = [s.strip() for s in args.instances.split(",") if s.strip()] if args.instances else None
    try:
        run(
            args.handle,
            max_tweets=args.max_tweets,
            no_llm=args.no_llm,
            instances=instances,
            output_dir=args.output_dir,
            draft_dir=args.draft_dir,
            cfg=llm_config(),
        )
    except ValueError as e:
        parser.error(str(e))


if __name__ == "__main__":
    main()
