#!/usr/bin/env python3
"""
行业语境调研脚本化（#14 T4，封装 web-deep-research 流程）：
多策略搜索 → 页面抓取（带缓存）→ LLM 综合 → 两份行业调研 Markdown 草稿（HITL 人工审核）。

用法：
  python3 scripts/industry_research.py [--query "..."] [--docs both|hiring|tech]
                                       [--max-pages N] [--no-cache] [--dry-run]
                                       [--output-dir DIR] [--cache-dir DIR]

产出（默认 private/kb/draft/，不写正式层，人工审核后手动并入正式 KB）：
  web3-hiring-context.draft.md   招聘语境：目标岗位画像 / 薪资语境 / 常见要求 / 招聘渠道
  web3-tech-coordinate.draft.md  技术坐标：技术栈坐标 / 生态格局 / 信号源

搜索策略（主备，多策略防反爬）：
  1. DuckDuckGo HTML SSR（urllib + UA，默认）；
  2. 失败/被反爬/无结果时回退 camoufox 浏览器（懒加载 import，测试环境无强依赖）。
  搜索请求之间随机间隔 2-5s（风控礼貌）。

页面抓取：对 top --max-pages（默认 5）个结果 urllib 抓 HTML → 去标签取正文，
  每页截断 ~8000 字符，抓取失败/空正文跳过该页。
  抓取缓存按搜索词哈希落 private/data/industry_cache/<query-hash>.json
  （URL+标题+正文+抓取时间）；同 query 重跑命中缓存不重抓（跳过搜索与抓取），
  --no-cache 强制重抓并刷新缓存。

LLM 综合：素材（多页正文摘录，带来源 URL 与抓取时间）→ 综合成 Markdown 草稿。
  禁推测约束（进 prompt）：只综合素材里有的信息，不得编造或推测；
  每条关键结论标注来源 URL；素材不足的板块标「素材不足，需补充调研」。
  单份文档综合失败不中断另一份；全部失败报错退出。

LLM：OpenAI 兼容 /v1/chat/completions。默认本地 Ollama（qwen3:8b），
     GREETING_LLM_BASE_URL / GREETING_LLM_MODEL / GREETING_LLM_API_KEY 可换任意兼容服务。

注入式测试：LLM / 搜索 / 页面抓取 / sleep 均可注入（见 test_industry_research.py），
测试不打真网、不打真 LLM。
"""

import argparse
import hashlib
import ipaddress
import json
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
DEFAULT_OUTPUT_DIR = ROOT / "private/kb/draft"
DEFAULT_CACHE_DIR = ROOT / "private/data/industry_cache"
DEFAULT_QUERY = "Web3 区块链 招聘 岗位要求 2026"
DEFAULT_MAX_PAGES = 5
PAGE_CHARS = 8000  # 每页正文截断字符数
# 素材总预算（对齐本地 qwen3:8b 上下文）：多页素材拼超预算按页序截断，
# 做法参照 repo_survey.py 的 MAX_SNIPPET_CHARS（总量上限 + 按序截断）。
MAX_MATERIAL_CHARS = 24000

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
FETCH_TIMEOUT = 30
CAMOUFOX_SETTLE_SECONDS = 3  # camoufox 打开结果页后等渲染的秒数

# 抓取前防御：私网/环回网段不抓（调研只抓公网页面，防把请求打到本机/内网）
_PRIVATE_NETS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
)

HIRING_SYSTEM = (
    "你是行业招聘调研编辑，正在起草「Web3 合约工程师招聘行业语境」调研报告草稿。"
    "只依据给出的搜索素材（多页网页正文摘录）综合，不得编造或推测素材里没有的信息；"
    "每条关键结论句末标注来源 URL（Markdown 链接 [来源](URL)）；"
    "素材不足以支撑的板块整节写「素材不足，需补充调研」，不要推测填充。"
    "按以下小节结构输出 Markdown 正文（不含一级标题）：\n"
    "## 目标岗位画像\n## 薪资语境\n## 常见要求\n## 招聘渠道\n"
    '输出 JSON：{"markdown": "Markdown 正文"} /no_think'
)
TECH_SYSTEM = (
    "你是行业技术调研编辑，正在起草「Web3 行业技术坐标系」调研报告草稿。"
    "只依据给出的搜索素材（多页网页正文摘录）综合，不得编造或推测素材里没有的信息；"
    "每条关键结论句末标注来源 URL（Markdown 链接 [来源](URL)）；"
    "素材不足以支撑的板块整节写「素材不足，需补充调研」，不要推测填充。"
    "按以下小节结构输出 Markdown 正文（不含一级标题）：\n"
    "## 技术栈坐标\n## 生态格局\n## 信号源\n"
    '输出 JSON：{"markdown": "Markdown 正文"} /no_think'
)

DOCS = {
    "hiring": {
        "file": "web3-hiring-context.draft.md",
        "title": "Web3 合约工程师招聘行业语境",
        "system": HIRING_SYSTEM,
    },
    "tech": {
        "file": "web3-tech-coordinate.draft.md",
        "title": "Web3 行业技术坐标系",
        "system": TECH_SYSTEM,
    },
}

MERGE_GUIDE = """[合并指引] 草稿仅供人工审核（HITL），本脚本不写正式层：
  1. 逐条核对结论与来源 URL 是否一致（禁推测约束由 prompt 承担，LLM 仍可能出错）；
  2. 标「素材不足，需补充调研」的板块需人工补充调研后再定稿；
  3. 审核通过后手动并入正式 KB（如 private/profile_background.md 行业语境节），可删本草稿。"""


# --- 搜索：DDG HTML 解析 ---


def clean_ddg_url(href):
    """清洗 DDG 结果链接：解 /l/?uddg= 跳转参数，过滤 DDG 内部链接（广告 y.js 等）与非 http 链接。"""
    if not href:
        return None
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    query = urllib.parse.parse_qs(parsed.query)
    if parsed.path.startswith("/l/") and query.get("uddg"):
        href = query["uddg"][0]
        parsed = urllib.parse.urlparse(href)
    if not href.startswith("http") or not parsed.netloc:
        return None
    if "duckduckgo.com" in parsed.netloc:  # 跳转未解出或 DDG 内部链接（广告等）
        return None
    return href


class _ResultParser(HTMLParser):
    """收集 class=result__a 的 <a>（DDG HTML 结果条目），产出 (清洗后 URL, 标题文本)。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self._in_anchor = False
        self._href = None
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        a = dict(attrs)
        if "result__a" in a.get("class", "").split():
            self._in_anchor = True
            self._href = a.get("href", "")
            self._buf = []

    def handle_data(self, data):
        if self._in_anchor:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._in_anchor:
            title = "".join(self._buf).strip()
            url = clean_ddg_url(self._href)
            if url and title:
                self.results.append({"url": url, "title": title})
            self._in_anchor = False


def parse_ddg_results(html):
    """从 DDG HTML 结果页解析（url, title）列表：按出现顺序、URL 去重。"""
    parser = _ResultParser()
    parser.feed(html)
    seen, out = set(), []
    for r in parser.results:
        if r["url"] not in seen:
            seen.add(r["url"])
            out.append(r)
    return out


def ddg_search(query):
    """策略1：DuckDuckGo HTML SSR（urllib）。返回结果列表，被反爬时抛异常或空列表。"""
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    return parse_ddg_results(html)


def camoufox_search(query):
    """策略2（fallback）：camoufox 浏览器抓 DDG 结果页。懒加载 import，测试环境不装也不受影响。"""
    from camoufox.sync_api import Camoufox

    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(query)
    with Camoufox(humanize=True, geoip=True, os="macos", headless=True) as browser:
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(CAMOUFOX_SETTLE_SECONDS)  # 等结果渲染（参照 crawl_jds.py 的等待做法）
        html = page.content()
    return parse_ddg_results(html)


def default_sleep():
    """搜索请求之间的风控礼貌间隔：随机 2-5s。"""
    time.sleep(random.uniform(2, 5))


def search(query, ddg_fn=None, camoufox_fn=None, sleep_fn=None):
    """多策略搜索编排：策略1（默认 DDG SSR）失败/无结果 → 策略2（camoufox）。
    回退前 sleep_fn()（默认随机 2-5s）；两策略均失败时抛出最后的异常。"""
    if ddg_fn is None:
        ddg_fn = ddg_search
    if camoufox_fn is None:
        camoufox_fn = camoufox_search
    if sleep_fn is None:
        sleep_fn = default_sleep
    try:
        results = ddg_fn(query)
        if results:
            print(f"[搜索] 策略1 DuckDuckGo SSR 命中 {len(results)} 条")
            return results
        print("[搜索] 策略1 无结果，启用策略2（camoufox 浏览器）")
    except Exception as e:
        print(f"[搜索] 策略1 失败: {e}；启用策略2（camoufox 浏览器）")
    sleep_fn()
    return camoufox_fn(query)


# --- 页面抓取与缓存 ---


class _TextExtractor(HTMLParser):
    """去标签取正文：跳过 script/style/noscript；块级标签前后补空格，防英文粘词。"""

    SKIP = {"script", "style", "noscript"}
    BLOCK = {
        "p",
        "div",
        "li",
        "br",
        "ul",
        "ol",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "tr",
        "table",
        "section",
        "article",
        "blockquote",
        "pre",
        "header",
        "footer",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag in self.BLOCK:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in self.BLOCK:
            self.parts.append(" ")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self.parts.append(data)


def extract_text(html, limit=PAGE_CHARS):
    """HTML → 纯文本（去标签/注释/script/style，块级标签处补空格，空白折叠），截断 limit 字符。"""
    parser = _TextExtractor()
    parser.feed(html)
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip()[:limit]


def fetch_url_text(url):
    """urllib 抓 URL → 去标签正文文本（供 fetch_pages 默认使用，可被注入替换）。
    抓取前防御：主机为私网/环回地址（127/8、10/8、172.16/12、192.168/16、
    169.254/16、localhost、::1）不抓；响应 Content-Type 非 text/html 不解析。
    两者都返回空文本，由 fetch_pages 按空正文跳过该页。响应体最多读 2MB 再 decode。"""
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
        if resp.headers.get_content_type() != "text/html":
            return ""
        charset = resp.headers.get_content_charset() or "utf-8"
        html = resp.read(2 * 1024 * 1024).decode(charset, errors="replace")
    return extract_text(html)


def fetch_pages(results, max_pages, fetch_fn=None, now=None):
    """抓前 max_pages 个结果的正文，产出页记录 [{url,title,text,fetched_at}]；
    抓取失败/空正文跳过该页，每页截断 PAGE_CHARS 字符。"""
    if fetch_fn is None:
        fetch_fn = fetch_url_text
    now_dt = now or datetime.now()
    pages = []
    for r in results[:max_pages]:
        url = r.get("url", "")
        print(f"[抓取] {url} ...")
        try:
            text = fetch_fn(url)
        except Exception as e:
            print(f"  [跳过] {url}: {e}")
            continue
        text = (text or "").strip()[:PAGE_CHARS]
        if not text:
            print(f"  [跳过] {url}: 正文为空")
            continue
        pages.append(
            {"url": url, "title": r.get("title", ""), "text": text, "fetched_at": now_dt.isoformat(timespec="seconds")}
        )
    return pages


def cache_path(cache_dir, query):
    """query → 缓存文件路径 <cache_dir>/<query-hash>.json（sha256 前 16 位）。"""
    h = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
    return Path(cache_dir) / f"{h}.json"


def load_cache(path):
    """读抓取缓存；不存在/损坏/结构不对返回 None（视为未命中，重抓）。
    页记录非 dict 或缺 url 键的条目按页跳过（容错坏页记录，不因单页坏档崩掉整份缓存）。"""
    p = Path(path)
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    pages = data.get("pages") if isinstance(data, dict) else None
    if not isinstance(pages, list):
        return None
    return [pg for pg in pages if isinstance(pg, dict) and "url" in pg]


def write_cache(path, query, pages):
    """写抓取缓存 JSON（{"query": ..., "pages": [...]}; 原子写委托 scripts/_atomic_io.py）。"""
    write_json_atomic(path, {"query": query, "pages": pages})


# --- LLM 综合 ---


def build_material(pages):
    """多页正文 → 素材文本：每页一节，带来源 URL 与抓取时间（供 LLM 标注来源）。
    总量超 MAX_MATERIAL_CHARS 预算时按页序截断：来源头完整保留，正文按剩余预算截断
    （最后一页可部分截断），放不下一整节来源头时后续页整页丢弃；
    对齐本地 qwen3:8b 上下文（做法参照 repo_survey.py 的 MAX_SNIPPET_CHARS）。"""
    parts, total = [], 0
    for i, p in enumerate(pages, 1):
        header = f"## 素材{i}：{p.get('title', '')}（来源: {p['url']}，抓取于 {p.get('fetched_at', '')}）\n\n"
        sep = "\n\n" if parts else ""
        remaining = MAX_MATERIAL_CHARS - total - len(sep)
        if remaining <= len(header):
            break
        parts.append(sep + header + p["text"][: remaining - len(header)])
        total += len(parts[-1])
    return "".join(parts)


def synthesize(doc_key, material, query, llm_call, cfg):
    """单份文档综合：禁推测 system prompt + 素材 → LLM → Markdown 正文。"""
    user = f"""# 调研主题

{query}

# 素材（多页网页正文摘录，每页带来源 URL 与抓取时间）

{material}

只依据以上素材综合输出 JSON（每条关键结论标注来源 URL）。"""
    out = extract_json(
        llm_call([{"role": "system", "content": DOCS[doc_key]["system"]}, {"role": "user", "content": user}], cfg)
    )
    md = (out.get("markdown") or "").strip()
    if not md:
        raise ValueError("markdown 为空")
    return md


def render_draft(doc_key, markdown, query, n_pages, now=None):
    """组装草稿文本：标题 + 元信息（生成时间/搜索词/素材页数）+ 草稿边界声明 + 正文。"""
    meta = DOCS[doc_key]
    now_s = (now or datetime.now()).isoformat(timespec="seconds")
    return "\n".join(
        [
            f"# 行业调研草稿：{meta['title']}（industry_research.py 自动生成）",
            "",
            f"> 生成时间：{now_s}；搜索词：{query}；素材：{n_pages} 页（来源 URL 标注于正文结论处）",
            "> 本文件是**草稿**：LLM 综合公开搜索素材而成，可能有错漏；"
            "人工审核、核对来源后手动并入正式 KB，本脚本不写正式层。",
            "",
            markdown.strip(),
            "",
        ]
    )


# --- 主流程 ---


def run(
    query,
    docs="both",
    max_pages=DEFAULT_MAX_PAGES,
    no_cache=False,
    dry_run=False,
    output_dir=DEFAULT_OUTPUT_DIR,
    cache_dir=DEFAULT_CACHE_DIR,
    llm_call=None,
    cfg=None,
    search_fn=None,
    fetch_fn=None,
    sleep_fn=None,
    now=None,
):
    """主流程：缓存 → 搜索（策略1/2）→ 抓取 → LLM 综合 → 草稿落盘 + 合并指引。
    llm/search/fetch/sleep 均可注入；单份文档综合失败不中断另一份，
    全部失败或无素材时 sys.exit(1)。返回 {doc_key: 草稿文本}。"""
    if llm_call is None:

        def llm_call(msgs, c):
            return call_llm(msgs, c)

    cfg = cfg or {}
    now_dt = now or datetime.now()
    out_dir = Path(output_dir)
    doc_keys = ["hiring", "tech"] if docs == "both" else [docs]

    if dry_run:
        print(
            f"[dry-run] 搜索词: {query} | 文档: {docs} | max-pages: {max_pages} | "
            f"缓存: {cache_path(cache_dir, query)} | 输出目录: {out_dir}"
        )
        print("[dry-run] 不搜索、不抓取、不调 LLM、不写文件")
        return {}

    # 1. 缓存：同 query 命中则跳过搜索与抓取（--no-cache 强制重抓）
    cp = cache_path(cache_dir, query)
    pages = None if no_cache else load_cache(cp)
    if pages is not None:
        print(f"[cache] 命中 {len(pages)} 页（{cp}），跳过搜索与抓取")
    else:
        # 2. 搜索（策略1 DDG SSR → 策略2 camoufox）
        try:
            results = search_fn(query) if search_fn else search(query, sleep_fn=sleep_fn)
        except Exception as e:
            print(f"[错误] 搜索失败（两个策略均未拿到结果）: {e}")
            sys.exit(1)
        print(f"[搜索] 共 {len(results)} 条结果，抓取前 {min(max_pages, len(results))} 页 ...")
        # 3. 抓取
        pages = fetch_pages(results, max_pages, fetch_fn=fetch_fn, now=now_dt)
        if not pages:
            print("[错误] 所有页面抓取失败，没有可用素材（可换 --query 或稍后重试）")
            sys.exit(1)
        write_cache(cp, query, pages)
        print(f"[cache] 已写 {cp}（{len(pages)} 页；同 query 重跑命中缓存，--no-cache 强制重抓）")

    # 4. LLM 综合（单份失败不中断另一份）
    material = build_material(pages)
    drafts, failed = {}, []
    for key in doc_keys:
        name = DOCS[key]["file"]
        print(f"[综合] {name} ...")
        try:
            md = synthesize(key, material, query, llm_call, cfg)
        except Exception as exc:
            print(f"  [失败] {name}: {exc}")
            failed.append(name)
            continue
        text = render_draft(key, md, query, len(pages), now=now_dt)
        write_text_atomic(out_dir / name, text)
        drafts[key] = text
        print(f"  [草稿] {out_dir / name}")
    if not drafts:
        print("[错误] 所有文档综合失败，无草稿可交。请检查 LLM 服务后重跑。")
        sys.exit(1)
    print(f"[done] 产出 {len(drafts)} 份草稿（失败 {len(failed)}）→ {out_dir}")
    print(MERGE_GUIDE)
    return drafts


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
        description="行业语境调研：搜索 + 页面抓取 + LLM 综合 → 行业调研草稿（#14 T4，草稿供人工审核合并）"
    )
    parser.add_argument("--query", default=DEFAULT_QUERY, help=f"搜索词（默认 {DEFAULT_QUERY!r}）")
    parser.add_argument("--docs", choices=["both", "hiring", "tech"], default="both", help="产出哪些草稿（默认 both）")
    parser.add_argument(
        "--max-pages",
        type=_positive_int,
        default=DEFAULT_MAX_PAGES,
        help=f"抓取的搜索结果页数上限（默认 {DEFAULT_MAX_PAGES}，必须 >= 1；变更后请 --no-cache 重抓）",
    )
    parser.add_argument("--no-cache", action="store_true", help="忽略已有缓存强制重抓（抓完刷新缓存）")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="草稿输出目录（默认 private/kb/draft）")
    parser.add_argument(
        "--cache-dir", default=str(DEFAULT_CACHE_DIR), help="抓取缓存目录（默认 private/data/industry_cache）"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只打印调研计划（搜索词/文档/缓存路径），不搜索不抓取不调 LLM 不写文件"
    )
    args = parser.parse_args()
    run(
        args.query,
        docs=args.docs,
        max_pages=args.max_pages,
        no_cache=args.no_cache,
        dry_run=args.dry_run,
        output_dir=args.output_dir,
        cache_dir=args.cache_dir,
        cfg=llm_config(),
    )


if __name__ == "__main__":
    main()
