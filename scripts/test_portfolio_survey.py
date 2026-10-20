#!/usr/bin/env python3
"""portfolio_survey 采集引擎测试（#26）：URL/PDF/文本 → 结构化档案，失败显式不静默。"""

import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "portfolio_survey", Path(__file__).resolve().parent / "portfolio_survey.py"
)
assert _spec is not None and _spec.loader is not None, "importlib spec 加载失败（模块文件缺失？）"
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


RICH_HTML = """<html><head><title>作品集</title></head><body>
<script>var tracking = "noise";</script><style>.x{color:red}</style>
<main><h1>我的项目</h1><p>DeFi 数据平台，覆盖 21 条链的收益聚合与模拟器，服务于零知识
证明开发者社区。后端 Node 20 monorepo，Aave V3/V4 双官方 SDK，PG 5 分钟快照 + R2 备份。
LLM 混入数据管道，带自建准确率基准与优雅降级。前端 React 19 + TS + Vite，四语言 i18n。
合约侧 Solidity 0.8 交付过主网生产 ERC1155 与 EIP712 元交易，经历过第三方审计。
其他作品包括 NFT 平台全栈（ERC721A 工厂 + Merkle 预售 + 荷兰拍 + Chainlink VRF）、
zkRollup 应用迁移（Noir 合约 + WebGL 客户端全量移植）、AI 内容生产流水线等。
另维护开源工具库若干：合约测试覆盖率报告、多链部署脚本模板、链上数据导出工具。
写作方面长期输出 Web3 行业观察与技术长文，社区频道订户过万，内容覆盖协议解读、
安全事件复盘与开发者生态动态，是多个海外协议在中国的社区推动者。
联系方式与履历细节见随附简历，所有项目均可现场演示或提供只读实例。</p></main></body></html>"""


def _entry_of(**kw):
    base = {
        "source": "x",
        "type": "text",
        "status": "ok",
        "title": "t",
        "text_chars": 1,
        "extracted_text": "x",
        "collected_at": "2026-09-11T00:00:00",
    }
    base.update(kw)
    return base


# --- 输入分类：不支持的格式显式报错，不静默跳过 ---


def test_unsupported_suffix_is_explicit_error():
    e = mod.survey_one("deck.pptx", now=None)
    assert e["status"] == "error"
    assert "v1 不支持" in e["error"]


def test_missing_path_is_explicit_error():
    e = mod.survey_one("/no/such/file.pdf", now=None)
    assert e["status"] == "error"
    assert e["error"]


# --- 本地文本材料 ---


def test_text_file_extracted(tmp_path):
    p = tmp_path / "portfolio.md"
    p.write_text("# 作品集\nDeFi 数据平台。", encoding="utf-8")
    e = mod.survey_one(str(p), now=None)
    assert e["status"] == "ok"
    assert e["type"] == "text"
    assert "DeFi 数据平台" in e["extracted_text"]
    assert e["text_chars"] > 0


def test_extracted_text_truncated_to_cap(tmp_path):
    p = tmp_path / "big.md"
    p.write_text("长" * (mod.MAX_TEXT_CHARS + 500), encoding="utf-8")
    e = mod.survey_one(str(p), now=None)
    assert e["status"] == "ok"
    assert len(e["extracted_text"]) == mod.MAX_TEXT_CHARS
    assert "截断" in (e.get("note") or "")


# --- HTML 正文提取：脚本/样式剔除 ---


def test_html_text_strips_script_and_style():
    title, text = mod.html_text(RICH_HTML)
    assert title == "作品集"
    assert "noise" not in text
    assert "DeFi 数据平台" in text


# --- URL 两级策略：静态优先，过薄触发渲染降级 ---


def test_survey_url_static_ok():
    def fetch(u, timeout=30):
        return RICH_HTML, u

    e = mod.survey_url("https://example.com", fetch_fn=fetch, render_fn=None, now=None, js_text_min=100)
    assert e["status"] == "ok"
    assert e["type"] == "url"
    assert "DeFi 数据平台" in e["extracted_text"]


def test_survey_url_thin_triggers_render_fallback():
    thin = "<html><head><title>App</title></head><body><div id=root></div></body></html>"

    def fetch(u, timeout=30):
        return thin, u

    def render(u, timeout=30000):
        return RICH_HTML

    e = mod.survey_url("https://spa.example.com", fetch_fn=fetch, render_fn=render, now=None, js_text_min=100)
    assert e["status"] == "ok"
    assert "渲染降级" in (e.get("note") or "")
    assert "DeFi 数据平台" in e["extracted_text"]


def test_survey_url_thin_render_unavailable_is_explicit_error():
    """渲染器不可用（如无 camoufox 环境）也显式报错，且不得静默当成功。"""
    thin = "<html><body><div id=root></div></body></html>"

    def fetch(u, timeout=30):
        return thin, u

    def render(u, timeout=30000):
        raise RuntimeError("Camoufox 不可用，无法渲染 JS 页面（pip install camoufox）")

    e = mod.survey_url("https://spa.example.com", fetch_fn=fetch, render_fn=render, now=None, js_text_min=100)
    assert e["status"] == "error"
    assert "渲染" in e["error"] and "100" in e["error"]


def test_survey_url_render_failure_is_explicit_error():
    thin = "<html><body><div id=root></div></body></html>"

    def fetch(u, timeout=30):
        return thin, u

    def render(u, timeout=30000):
        raise RuntimeError("browser gone")

    e = mod.survey_url("https://spa.example.com", fetch_fn=fetch, render_fn=render, now=None, js_text_min=100)
    assert e["status"] == "error"
    assert "browser gone" in e["error"]


def test_survey_url_network_failure_is_explicit_error():
    def fetch(u, timeout=30):
        raise OSError("unreachable")

    e = mod.survey_url("https://down.example.com", fetch_fn=fetch, render_fn=None, now=None)
    assert e["status"] == "error"
    assert "unreachable" in e["error"]


# --- PDF：文本层提取；损坏文件显式报错不崩批 ---


def test_pdf_corrupt_bytes_is_explicit_error(tmp_path):
    p = tmp_path / "broken.pdf"
    p.write_bytes(b"not-a-pdf")
    e = mod.survey_one(str(p), now=None)
    assert e["status"] == "error"
    assert "pdfplumber" in e["error"] or "PDF" in e["error"]


# --- 档案 upsert：失败结果不覆盖好档案（与 repos_detail 同语义） ---


def test_upsert_error_does_not_overwrite_good_entry():
    good = _entry_of(source="https://example.com", status="ok")
    bad = _entry_of(source="https://example.com", status="error", error="timeout")
    merged = mod.upsert_archive([good], [bad])
    assert len(merged) == 1
    assert merged[0]["status"] == "ok", "本批失败不覆盖已有好档案"
    fresh = [_entry_of(source="https://new.example.com")]
    merged = mod.upsert_archive([good], fresh)
    assert [e["source"] for e in merged] == ["https://example.com", "https://new.example.com"]


# --- 批量 run：混合成败、dry-run、缺清单退出 ---


def test_run_mixed_batch_writes_archive(tmp_path):
    good = tmp_path / "a.md"
    good.write_text("作品材料正文", encoding="utf-8")
    out = tmp_path / "archive.json"
    results = mod.run([str(good), "deck.pptx"], str(out), now=None)
    assert len(results) == 2
    statuses = {e["status"] for e in results}
    assert statuses == {"ok", "error"}, "失败材料显式标注不静默"
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert len(saved) == 2


def test_run_dry_run_writes_nothing(tmp_path):
    good = tmp_path / "a.md"
    good.write_text("正文", encoding="utf-8")
    out = tmp_path / "archive.json"
    results = mod.run([str(good)], str(out), dry_run=True)
    assert results == []
    assert not out.exists()


def test_run_upserts_into_existing_archive(tmp_path):
    good = tmp_path / "a.md"
    good.write_text("正文", encoding="utf-8")
    out = tmp_path / "archive.json"
    mod.run([str(good)], str(out), now=None)
    other = tmp_path / "b.md"
    other.write_text("另份材料", encoding="utf-8")
    mod.run([str(other)], str(out), now=None)
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert [Path(e["source"]).name for e in saved] == ["a.md", "b.md"]


def test_load_sources_missing_default_file_exits():
    with pytest.raises(SystemExit):
        mod.load_sources("", "/no/such/sources.txt")


def test_load_sources_comma_separated_wins():
    assert mod.load_sources("a.md, b.md", None) == ["a.md", "b.md"]


def test_load_sources_file_skips_comments_and_blanks(tmp_path):
    f = tmp_path / "sources.txt"
    f.write_text("# 注释\n\nhttps://example.com\n", encoding="utf-8")
    assert mod.load_sources("", str(f)) == ["https://example.com"]
