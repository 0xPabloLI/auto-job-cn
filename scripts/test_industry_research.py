#!/usr/bin/env python3
"""industry_research 的行为测试：DDG 解析、正文提取、缓存、多策略搜索编排、
页面抓取、素材组装、LLM 综合 prompt、草稿渲染、run 主流程（注入式，不打真网真 LLM）。"""

import email.message
import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "industry_research", Path(__file__).resolve().parent / "industry_research.py"
)
assert _spec is not None and _spec.loader is not None, "importlib spec 加载失败（模块文件缺失？）"
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

NOW = datetime(2026, 9, 7, 12, 0, 0)
NOW_S = "2026-09-07T12:00:00"

DDG_HTML = """
<div class="result results_links results_links_deep web-result">
  <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fweb3-jobs&amp;rut=abc">Web3 招聘 <b>岗位</b>要求 2026</a>
</div>
<div class="result result--ad">
  <a class="result__a" href="https://duckduckgo.com/y.js?ad_provider=x">广告位</a>
</div>
<a class="result__a" href="javascript:void(0)">js 链接</a>
<a class="result__a" href="/l/?uddg=https%3A%2F%2Fdirect.example.com%2Fa">直链结果</a>
"""


def raises(msg):
    raise AssertionError(msg)


# --- DDG 搜索结果解析 ---


def test_parse_ddg_results_decodes_uddg_and_extracts_title():
    results = mod.parse_ddg_results(DDG_HTML)
    by_url = {r["url"]: r["title"] for r in results}
    assert by_url.get("https://example.com/web3-jobs") == "Web3 招聘 岗位要求 2026"
    assert by_url.get("https://direct.example.com/a") == "直链结果"


def test_parse_ddg_results_skips_ads_and_internal_links():
    results = mod.parse_ddg_results(DDG_HTML)
    urls = [r["url"] for r in results]
    assert all("duckduckgo.com" not in u for u in urls), "DDG 内部链接（广告 y.js 等）要过滤"
    assert all(u.startswith("https://") for u in urls), "javascript:/相对路径链接不进结果"
    assert len(urls) == len(set(urls)), "重复 URL 去重"


def test_parse_ddg_results_empty_page_returns_empty_list():
    assert mod.parse_ddg_results("<html><body>被反爬验证码拦截</body></html>") == []


# --- 正文提取 ---


def test_extract_text_strips_tags_script_style_and_comment():
    html = (
        "<html><head><script>var tracking=1;</script><style>.a{color:red}</style></head>"
        "<body><!-- 注释 --><h1>标题一</h1><p>正文内容 首段</p></body></html>"
    )
    text = mod.extract_text(html)
    assert "标题一" in text and "正文内容" in text
    assert "tracking" not in text and "color:red" not in text and "注释" not in text


def test_extract_text_collapses_whitespace():
    text = mod.extract_text("<p>a</p>  \n\t <p>b</p>")
    assert text == "a b"


def test_extract_text_truncates_to_page_chars():
    text = mod.extract_text("<p>" + "长" * 9000 + "</p>")
    assert len(text) == mod.PAGE_CHARS


def test_extract_text_inserts_spaces_between_block_elements():
    assert mod.extract_text("<li>a</li><li>b</li>") == "a b", "块级元素间补空格防英文粘词"
    assert mod.extract_text("<p>Salary</p><p>Remote</p>") == "Salary Remote"


# --- 缓存 ---


def test_cache_path_hashes_query_stably():
    p1, p2, p3 = (mod.cache_path("/c", "Web3 招聘"), mod.cache_path("/c", "Web3 招聘"), mod.cache_path("/c", "别的词"))
    assert p1 == p2 and p1 != p3
    assert p1.suffix == ".json" and p1.parent == Path("/c")


def test_cache_roundtrip(tmp_path):
    cp = tmp_path / "sub" / "h.json"
    pages = [{"url": "https://a", "title": "t", "text": "x", "fetched_at": NOW_S}]
    mod.write_cache(cp, "q", pages)
    assert mod.load_cache(cp) == pages


def test_cache_load_missing_or_corrupt_returns_none(tmp_path):
    assert mod.load_cache(tmp_path / "missing.json") is None
    cp = tmp_path / "bad.json"
    cp.write_text("not json", encoding="utf-8")
    assert mod.load_cache(cp) is None
    cp.write_text('{"no_pages_key": 1}', encoding="utf-8")
    assert mod.load_cache(cp) is None


def test_load_cache_skips_bad_page_records(tmp_path):
    good = {"url": "https://a", "title": "t", "text": "x", "fetched_at": NOW_S}
    cp = tmp_path / "pages.json"
    cp.write_text(
        json.dumps(
            {
                "query": "q",
                "pages": [
                    good,
                    "not-a-dict",
                    42,
                    {"title": "缺 url 键"},
                    {"url": "https://b", "text": "y"},
                ],
            }
        ),
        encoding="utf-8",
    )
    assert mod.load_cache(cp) == [good, {"url": "https://b", "text": "y"}]


# --- 多策略搜索编排 ---


def test_search_strategy1_success_skips_fallback_and_sleep():
    out = mod.search(
        "q",
        ddg_fn=lambda q: [{"url": "u1", "title": "t1"}],
        camoufox_fn=lambda q: raises("策略1 成功不应回退"),
        sleep_fn=lambda: raises("策略1 成功不应 sleep"),
    )
    assert out == [{"url": "u1", "title": "t1"}]


def test_search_falls_back_on_exception_after_polite_sleep():
    order = []

    def ddg(q):
        raise RuntimeError("被反爬")

    out = mod.search(
        "q",
        ddg_fn=ddg,
        camoufox_fn=lambda q: (order.append("camo"), [{"url": "u2", "title": "t2"}])[1],
        sleep_fn=lambda: order.append("sleep"),
    )
    assert out == [{"url": "u2", "title": "t2"}]
    assert order == ["sleep", "camo"], "回退前应先做礼貌间隔"


def test_search_falls_back_on_empty_results():
    out = mod.search(
        "q", ddg_fn=lambda q: [], camoufox_fn=lambda q: [{"url": "u2", "title": "t2"}], sleep_fn=lambda: None
    )
    assert out and out[0]["url"] == "u2"


def test_search_both_strategies_fail_raises_last_error():
    def ddg(q):
        raise RuntimeError("ddg 挂了")

    def camo(q):
        raise RuntimeError("camo 也挂了")

    with pytest.raises(RuntimeError, match="camo 也挂了"):
        mod.search("q", ddg_fn=ddg, camoufox_fn=camo, sleep_fn=lambda: None)


# --- 页面抓取 ---


def test_fetch_pages_records_url_title_truncated_text_and_time():
    def fetch(url):
        return "字" * 9000 if url.endswith("big") else "正文 " + url

    results = [
        {"url": "https://s/big", "title": "T0"},
        {"url": "https://s/1", "title": "T1"},
        {"url": "https://s/2", "title": "T2"},
    ]
    pages = mod.fetch_pages(results, max_pages=2, fetch_fn=fetch, now=NOW)
    assert [p["url"] for p in pages] == ["https://s/big", "https://s/1"]
    assert pages[0]["title"] == "T0"
    assert pages[0]["text"] == "字" * mod.PAGE_CHARS, "正文截到 PAGE_CHARS"
    assert pages[1]["text"] == "正文 https://s/1"
    assert pages[0]["fetched_at"] == NOW_S


def test_fetch_pages_skips_failure_and_empty_text():
    def fetch(url):
        if url.endswith("bad"):
            raise RuntimeError("超时")
        if url.endswith("empty"):
            return "   "
        return "ok"

    results = [{"url": f"https://s/{k}", "title": k} for k in ("bad", "empty", "good")]
    pages = mod.fetch_pages(results, 5, fetch_fn=fetch, now=NOW)
    assert [(p["url"], p["text"]) for p in pages] == [("https://s/good", "ok")]


def test_fetch_url_text_skips_private_and_loopback_hosts(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("私网/环回地址不应发起请求")

    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
    for url in (
        "http://127.0.0.1/x",
        "http://10.1.2.3/x",
        "http://172.16.0.9/x",
        "http://192.168.1.1/x",
        "http://169.254.1.1/x",
        "http://localhost/x",
        "http://LOCALHOST:8080/x",
        "http://[::1]/x",
    ):
        assert mod.fetch_url_text(url) == "", url


class _FakeResp:
    def __init__(self, headers):
        self.headers = headers

    def read(self, n=-1):
        return b"<p>should-not-parse</p>"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_fetch_url_text_skips_non_html_content_type(monkeypatch):
    msg = email.message.Message()
    msg["Content-Type"] = "application/json"
    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda req, timeout=None: _FakeResp(msg))
    assert mod.fetch_url_text("https://example.com/data.json") == ""


# --- 素材组装 ---


def test_build_material_carries_source_url_and_fetch_date():
    pages = [{"url": "https://a", "title": "A 页", "text": "正文A", "fetched_at": NOW_S}]
    m = mod.build_material(pages)
    assert "A 页" in m and "https://a" in m and "正文A" in m and NOW_S in m


def test_build_material_truncates_to_budget_and_keeps_source_headers():
    pages = [{"url": f"https://s/{i}", "title": f"页{i}", "text": "字" * 8000, "fetched_at": NOW_S} for i in range(10)]
    m = mod.build_material(pages)
    assert len(m) <= mod.MAX_MATERIAL_CHARS, "素材总量不超预算（对齐 qwen3:8b 上下文）"
    assert m.count("字" * 8000) == 2, "预算内的页正文完整保留"
    assert "https://s/2" in m, "最后一页可部分截断，来源头仍保留"
    assert "https://s/3" not in m, "预算耗尽后后续页整页丢弃"


# --- LLM 综合 ---


def test_system_prompts_carry_no_speculation_and_no_think():
    for key in ("hiring", "tech"):
        s = mod.DOCS[key]["system"]
        assert "不得编造" in s or "不得推测" in s, "禁推测约束必须进 prompt"
        assert "素材不足" in s, "素材不足的处置约定必须进 prompt"
        assert "来源" in s, "来源标注要求必须进 prompt"
        assert s.rstrip().endswith("/no_think")


def test_doc_file_names_match_spec():
    assert mod.DOCS["hiring"]["file"] == "web3-hiring-context.draft.md"
    assert mod.DOCS["tech"]["file"] == "web3-tech-coordinate.draft.md"


def test_synthesize_sends_system_and_material_and_returns_markdown():
    captured = {}

    def llm(messages, cfg):
        captured.update(messages=messages)
        return '{"markdown": "## 综合结论"}'

    md = mod.synthesize("hiring", "素材文本", "搜索词X", llm, {})
    assert md == "## 综合结论"
    system, user = captured["messages"][0]["content"], captured["messages"][1]["content"]
    assert system == mod.DOCS["hiring"]["system"]
    assert "搜索词X" in user and "素材文本" in user


def test_synthesize_empty_markdown_raises():
    with pytest.raises(ValueError):
        mod.synthesize("hiring", "m", "q", lambda m, c: '{"markdown": "  "}', {})


# --- 草稿渲染 ---


def test_render_draft_marks_draft_with_meta_and_body():
    text = mod.render_draft("hiring", "## 正文", "搜索词Q", 5, now=NOW)
    assert text.startswith("# 行业调研草稿")
    assert "草稿" in text and "人工审核" in text, "草稿边界声明必须在"
    assert "搜索词Q" in text and "5 页" in text and NOW_S in text
    assert "## 正文" in text


# --- 主流程（注入式） ---


def make_run_kwargs(tmp_path, llm=None, search=None, fetch=None, docs="both", **kw):
    d = dict(
        docs=docs,
        max_pages=5,
        no_cache=False,
        dry_run=False,
        output_dir=str(tmp_path / "draft"),
        cache_dir=str(tmp_path / "cache"),
        llm_call=llm or (lambda m, c: '{"markdown": "## 综合结论"}'),
        cfg={},
        search_fn=search or (lambda q: [{"url": "https://a", "title": "A"}]),
        fetch_fn=fetch or (lambda u: "页面正文"),
        sleep_fn=lambda: None,
        now=NOW,
    )
    d.update(kw)
    return d


def test_run_writes_both_drafts_and_cache(tmp_path):
    llm_calls = []

    def llm(messages, cfg):
        llm_calls.append(messages)
        return '{"markdown": "## 综合结论"}'

    out = mod.run("Web3 招聘", **make_run_kwargs(tmp_path, llm=llm))
    d = tmp_path / "draft"
    assert set(out) == {"hiring", "tech"}
    for name in ("web3-hiring-context.draft.md", "web3-tech-coordinate.draft.md"):
        text = (d / name).read_text(encoding="utf-8")
        assert "## 综合结论" in text and "草稿" in text
    assert len(llm_calls) == 2, "两份文档各调一次 LLM"
    assert mod.load_cache(mod.cache_path(tmp_path / "cache", "Web3 招聘")) is not None


def test_run_docs_option_single_file(tmp_path):
    mod.run("q", **make_run_kwargs(tmp_path, docs="tech"))
    d = tmp_path / "draft"
    assert (d / "web3-tech-coordinate.draft.md").exists()
    assert not (d / "web3-hiring-context.draft.md").exists()


def test_run_cache_hit_skips_search_and_fetch(tmp_path):
    kw = make_run_kwargs(tmp_path)
    mod.run("q", **kw)
    hits = []
    mod.run(
        "q",
        **{
            **kw,
            "search_fn": lambda q: raises("缓存命中不应再搜索"),
            "fetch_fn": lambda u: hits.append(u),
            "llm_call": lambda m, c: '{"markdown": "第二轮"}',
        },
    )
    text = (tmp_path / "draft" / "web3-hiring-context.draft.md").read_text(encoding="utf-8")
    assert "第二轮" in text and not hits


def test_run_no_cache_forces_refetch(tmp_path):
    kw = make_run_kwargs(tmp_path)
    mod.run("q", **kw)
    calls = []
    mod.run(
        "q",
        **{
            **kw,
            "no_cache": True,
            "search_fn": lambda q: (calls.append(q), [{"url": "https://b", "title": "B"}])[1],
            "fetch_fn": lambda u: "新正文",
        },
    )
    assert calls == ["q"], "--no-cache 应强制重搜重抓"


def test_run_llm_failure_isolated_between_docs(tmp_path):
    def llm(messages, cfg):
        if messages[0]["content"] == mod.DOCS["hiring"]["system"]:
            raise ValueError("LLM 超时")
        return '{"markdown": "tech ok"}'

    out = mod.run("q", **make_run_kwargs(tmp_path, llm=llm))
    d = tmp_path / "draft"
    assert set(out) == {"tech"}, "单份失败不中断另一份"
    assert "tech ok" in (d / "web3-tech-coordinate.draft.md").read_text(encoding="utf-8")
    assert not (d / "web3-hiring-context.draft.md").exists(), "失败的文档不落半成品"


def test_run_all_llm_failures_exits(tmp_path):
    with pytest.raises(SystemExit):
        mod.run("q", **make_run_kwargs(tmp_path, llm=lambda m, c: raises("LLM 全挂")))


def test_run_no_material_exits(tmp_path):
    with pytest.raises(SystemExit):
        mod.run("q", **make_run_kwargs(tmp_path, fetch=lambda u: raises("抓取全挂")))


def test_run_search_failure_exits(tmp_path):
    with pytest.raises(SystemExit):
        mod.run("q", **make_run_kwargs(tmp_path, search=lambda q: raises("两策略均挂")))


def test_run_dry_run_no_network_no_llm_no_write(tmp_path, capsys):
    boom = lambda *a, **k: raises("dry-run 不应有任何外部动作")  # noqa: E731
    out = mod.run("q", **make_run_kwargs(tmp_path, llm=boom, search=boom, fetch=boom, sleep_fn=boom, dry_run=True))
    assert out == {}
    assert not (tmp_path / "draft").exists() and not (tmp_path / "cache").exists()
    captured = capsys.readouterr()
    assert "dry-run" in captured.out


def test_run_passes_query_to_search(tmp_path):
    seen = []
    mod.run(
        "我的搜索词", **make_run_kwargs(tmp_path, search=lambda q: (seen.append(q), [{"url": "u", "title": "t"}])[1])
    )
    assert seen == ["我的搜索词"]
