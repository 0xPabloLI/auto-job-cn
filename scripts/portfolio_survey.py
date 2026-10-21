#!/usr/bin/env python3
"""
非 repo 复杂材料调研引擎（#26，#22 Phase 3）：作品集 URL/PDF/文本材料 → 采集 → 结构化档案。

用法：
  python3 scripts/portfolio_survey.py [--sources A,B,C | --sources-file FILE]
                                      [--output FILE] [--dry-run] [--force]
  不带 --sources/--sources-file 时默认读 private/data/portfolio_sources.txt
  （每行一个 URL 或本地路径，# 开头为注释，空行忽略；文件不存在时报错退出）。
  --sources 优先于 --sources-file。

输入（v1 范围，技术路径依据 内部调研笔记目录（未随本仓分发）portfolio-survey-research.md）：
  - http(s) URL：静态抓取（urllib+lxml，剔除 script/style）→ 正文 < 500 字符判
    「疑似 JS 渲染」自动走 Camoufox headless 渲染降级（lazy import）→ 仍过薄显式报错
  - 本地 .pdf：pdfplumber 文本层提取（lazy import）；无文本层（疑似扫描件）显式报错，
    OCR 不在 v1 范围
  - 本地 .txt/.md：直接读取
  - 其他格式（docx/pptx/图片/设计稿）：显式报错——多模态材料走 onboarding 步骤 1 的
    会话内 agent 阅读路径，脚本不假装能处理

输出：JSON 数组 upsert 写入 private/data/portfolio_archive.json（--output 可覆盖）。
每条档案字段：source / type(url|pdf|text) / status(ok|error) / title / text_chars /
extracted_text（截断至 MAX_TEXT_CHARS）/ note / error / collected_at。
失败材料记录 error 字段后继续下一条件（显式标注，不静默跳过）；upsert 语义与
repos_detail.json 一致——本批失败结果不覆盖已有好档案。--force 丢弃已有内容只写本批。
--dry-run 只打印将采集的来源清单，不抓取不写文件。

LLM：不做——综合由会话 agent 代行（2026-09-09 #9 架构决策），脚本只做确定性采集。
消费方：会话 agent 读档案后按 docs/kb-sop.md 人工审核合并进知识库。
"""

import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _atomic_io import write_json_atomic

ROOT = Path(__file__).resolve().parent.parent
# 输入清单与产物都是「谁的简历材料」（#28）：main() 按解析前缀重算，常量仅 legacy 默认
DEFAULT_SOURCES_LIST = ROOT / "private/data/portfolio_sources.txt"
DEFAULT_OUTPUT = ROOT / "private/data/portfolio_archive.json"

from _profile_paths import (  # noqa: E402  #28：简历材料类产物按 profile 隔离
    private_path,
    profile_line,
    resolve_active_profile,
)


MAX_TEXT_CHARS = 20000  # 档案单条正文上限（与 repo_survey 片段总量同量级）
JS_TEXT_MIN = 500  # 静态提取低于此值判疑似 JS 渲染（真实样本 43/2977 字符，推导见调研文档）
TEXT_EXTS = {".txt", ".md", ".markdown"}
PDF_EXTS = {".pdf"}
FETCH_TIMEOUT = 30
RENDER_TIMEOUT = 30000
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def html_text(raw):
    """HTML → (title, 正文文本)：剔除 script/style/noscript，空白折叠。
    lxml 为 camoufox/python-pptx 间接依赖，零新增安装。"""
    from lxml import html as lh

    doc = lh.fromstring(raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw)
    for tag in doc.xpath("//script|//style|//noscript"):
        tag.getparent().remove(tag)
    title = (doc.xpath("//title/text()") or [""]).pop().strip()
    text = re.sub(r"\s+", " ", doc.text_content()).strip()
    return title, text


def fetch_static(url, timeout=FETCH_TIMEOUT):
    """静态抓取：GET → (原始 HTML, 最终 URL)。网络失败抛异常由 survey_one 捕获落 error。"""
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.geturl()


def render_page(url, timeout=RENDER_TIMEOUT):
    """Camoufox headless 渲染 SPA 页（静态过薄时降级）。lazy import：无浏览器环境
    不 import 就不炸，报错显式可见。"""
    try:
        from camoufox.sync_api import Camoufox
    except ImportError as e:
        raise RuntimeError(f"Camoufox 不可用，无法渲染 JS 页面（pip install camoufox）: {e}")
    with Camoufox(humanize=True, geoip=True, os="macos", headless=True) as browser:
        page = browser.new_page()
        page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)  # SPA 挂载留白
        return page.content()


def _entry(source, type_, now, **fields):
    return {
        "source": source,
        "type": type_,
        "status": "ok",
        "title": "",
        "text_chars": 0,
        "extracted_text": "",
        "note": "",
        "collected_at": (now or datetime.now()).isoformat(timespec="seconds"),
        **fields,
    }


def survey_url(url, fetch_fn=None, render_fn=None, now=None, js_text_min=JS_TEXT_MIN):
    """URL 两级策略：静态抓取 → 过薄（< js_text_min）走渲染降级 → 仍薄/失败显式报错。
    阈值可注入与 fetch_fn/timeout 同族（测试用小阈值，免夹具凑字数）。"""
    fetch_fn = fetch_fn or fetch_static
    try:
        raw, final_url = fetch_fn(url)
        title, text = html_text(raw)
    except Exception as e:
        return _entry(url, "url", now, status="error", error=f"静态抓取失败: {e}")
    if len(text) >= js_text_min:
        return _entry(url, "url", now, title=title, text_chars=len(text), extracted_text=text[:MAX_TEXT_CHARS])
    if render_fn is None:
        render_fn = render_page
    try:
        raw = render_fn(url)
        title, text = html_text(raw)
    except Exception as e:
        return _entry(
            url,
            "url",
            now,
            status="error",
            error=f"静态抓取仅 {len(text)} 字符（< {js_text_min}，疑似 JS 渲染），渲染降级失败: {e}",
        )
    if len(text) < js_text_min:
        return _entry(
            url,
            "url",
            now,
            status="error",
            error=f"渲染后正文仍仅 {len(text)} 字符（< {js_text_min}）——页面可能要求登录或为空壳，显式失败不入档案",
        )
    return _entry(
        url,
        "url",
        now,
        title=title,
        text_chars=len(text),
        extracted_text=text[:MAX_TEXT_CHARS],
        note="静态抓取过薄，Camoufox 渲染降级",
    )


def survey_pdf(path, now=None):
    """PDF 文本层提取（pdfplumber，lazy import）；损坏/无文本层显式报错（OCR 不在 v1）。"""
    try:
        import pdfplumber
    except ImportError as e:
        return _entry(path, "pdf", now, status="error", error=f"pdfplumber 不可用（pip install pdfplumber）: {e}")
    try:
        with pdfplumber.open(path) as pdf:
            text = "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as e:
        return _entry(path, "pdf", now, status="error", error=f"PDF 解析失败: {e}")
    if not text.strip():
        return _entry(
            path, "pdf", now, status="error", error="PDF 无文本层（疑似扫描件）——OCR 不在 v1 范围，显式失败不入档案"
        )
    return _entry(path, "pdf", now, title=Path(path).stem, text_chars=len(text), extracted_text=text[:MAX_TEXT_CHARS])


def survey_text(path, now=None):
    """本地文本材料直读（txt/md/markdown）。"""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return _entry(path, "text", now, status="error", error=f"读取失败: {e}")
    note = "" if len(text) <= MAX_TEXT_CHARS else "超长截断至 MAX_TEXT_CHARS"
    return _entry(
        path, "text", now, title=Path(path).stem, text_chars=len(text), extracted_text=text[:MAX_TEXT_CHARS], note=note
    )


def survey_one(source, fetch_fn=None, render_fn=None, now=None):
    """单来源采集：任何失败落 error 字段继续，绝不抛出中断整批（失败显式不静默）。
    格式判定先于存在性——不支持的后缀即使路径存在也给出同样的格式指引。"""
    s = source.strip()
    if s.startswith(("http://", "https://")):
        return survey_url(s, fetch_fn=fetch_fn, render_fn=render_fn, now=now)
    p = Path(s).expanduser()
    suffix = p.suffix.lower()
    if suffix not in PDF_EXTS | TEXT_EXTS:
        return _entry(
            s,
            "unsupported",
            now,
            status="error",
            error=f"v1 不支持该格式（支持 pdf/txt/md/URL；多模态材料走 onboarding 会话内阅读路径）: {s}",
        )
    if not p.exists():
        return _entry(s, "unknown", now, status="error", error=f"路径不存在: {s}")
    if suffix in PDF_EXTS:
        return survey_pdf(str(p), now=now)
    return survey_text(str(p), now=now)


def upsert_archive(existing, new):
    """本批结果按 source upsert 进已有档案：同源替换、新源追加；
    新条目带 error 而已有同源档案无 error 时保留旧档案（失败不覆盖好档案）。"""
    by_source = {e["source"]: e for e in new}
    out, seen = [], set()
    for e in existing:
        s = e.get("source")
        if s in by_source:
            if by_source[s].get("status") == "error" and e.get("status") != "error":
                out.append(e)  # 本批失败结果不覆盖已有好档案
            else:
                out.append(by_source[s])
            seen.add(s)
        else:
            out.append(e)
    for e in new:
        if e["source"] not in seen:
            out.append(e)
    return out


def load_sources(sources_arg, sources_file_arg, default_list=None):
    """解析输入：--sources 逗号分隔优先；否则 --sources-file；否则默认列表文件
    （缺失报错退出，与 repo_survey.load_repo_paths 同语义）。"""
    if sources_arg:
        return [s.strip() for s in sources_arg.split(",") if s.strip()]
    path = Path(sources_file_arg) if sources_file_arg else (default_list or DEFAULT_SOURCES_LIST)
    if not path.exists():
        print(
            f"[错误] 材料清单文件不存在: {path}\n"
            "请创建它（每行一个 URL 或本地路径，# 开头为注释），"
            "或用 --sources A,B,C / --sources-file FILE 指定。"
        )
        sys.exit(1)
    return [
        ln.strip()
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def run(sources, output_path, force=False, dry_run=False, fetch_fn=None, render_fn=None, now=None):
    """整批主流程：逐来源采集 → upsert 落盘。dry-run 只打印采集计划。
    返回本批档案列表。"""
    if dry_run:
        for s in sources:
            print(f"[dry-run] 将采集: {s}")
        print("[done] dry-run 结束：未抓取、未写文件")
        return []
    existing = [] if force else _load_json(output_path)
    results = [survey_one(s, fetch_fn=fetch_fn, render_fn=render_fn, now=now) for s in sources]
    ok = sum(1 for e in results if e["status"] == "ok")
    if not results:
        print("[done] 没有产出档案，输出文件保持不变")
        return results
    merged = upsert_archive(existing, results)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(out, merged)
    print(
        f"[done] 本批 {len(results)} 来源（成功 {ok} / 失败 {len(results) - ok}）"
        f"→ {out}（累计 {len(merged)} 条）；档案交会话 agent 按 kb-sop 审核合并"
    )
    return results


def _load_json(path):
    """读已有档案；存在但损坏时立即报错退出（fail-fast，避免整批跑完才发现读不了）。"""
    p = Path(path)
    if not p.exists():
        return []
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(
            f"[错误] 已有档案文件不是合法 JSON，无法 upsert: {p}（{e}）\n"
            "请先修复或删除该文件（或改用 --force 覆盖）后重试。"
        )
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="非 repo 材料调研引擎：URL/PDF/文本 → 结构化档案（#26）")
    parser.add_argument("--sources", default="", help="逗号分隔的 URL 或本地路径（优先于 --sources-file）")
    parser.add_argument(
        "--sources-file", default=None, help="每行一个来源的文本文件（默认 private/data/portfolio_sources.txt）"
    )
    parser.add_argument("--output", default=None, help="输出 JSON 路径（默认 private/data/portfolio_archive.json）")
    parser.add_argument("--force", action="store_true", help="覆盖输出文件（默认按 source upsert）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将采集的来源清单，不抓取不写文件")
    args = parser.parse_args()
    _name, _prefix = resolve_active_profile()
    print(profile_line(_name, _prefix))
    sources_list = private_path("data/portfolio_sources.txt", _prefix)
    output = Path(args.output) if args.output else private_path("data/portfolio_archive.json", _prefix)
    sources = load_sources(args.sources, args.sources_file, default_list=sources_list)
    run(sources, output, force=args.force, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
