#!/usr/bin/env python3
"""twitter_survey 的行为测试：handle 规范化、nitter 实例解析、nitter 页解析、
x.com SSR 文本挖掘、抓取防御、多策略编排（1→2→3 fallback）、素材组装、
LLM 综合 prompt、草稿渲染、run 主流程（注入式，不打真网真 LLM）。"""

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

_spec = importlib.util.spec_from_file_location("twitter_survey", Path(__file__).resolve().parent / "twitter_survey.py")
assert _spec is not None and _spec.loader is not None, "importlib spec 加载失败（模块文件缺失？）"
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

NOW = datetime(2026, 9, 7, 12, 0, 0)
NOW_S = "2026-09-07T12:00:00"


def raises(msg):
    raise AssertionError(msg)


def boom(msg):
    def fn(*a, **k):
        raise RuntimeError(msg)

    return fn


def make_tweet(i):
    return {"date": f"2026-09-0{i}", "text": f"tweet{i}", "url": f"https://x.com/a/status/{i}"}


def make_data(handle="alice"):
    return {
        "handle": handle,
        "fetched_at": NOW_S,
        "strategy_used": "nitter",
        "tweets": [make_tweet(1), make_tweet(2)],
        "errors": [],
    }


# --- handle 规范化 ---


def test_normalize_handle_strips_at_and_whitespace():
    assert mod.normalize_handle(" @Alice ") == "Alice"
    assert mod.normalize_handle("@@bob") == "bob"


def test_normalize_handle_empty_raises():
    with pytest.raises(ValueError):
        mod.normalize_handle("  @  ")


def test_normalize_handle_rejects_invalid_characters():
    for bad in ("al!ce", "a-b", "白居易", "x" * 16):
        with pytest.raises(ValueError):
            mod.normalize_handle(bad), "仅允许 [A-Za-z0-9_]{1,15}"
    assert mod.normalize_handle("_a_1") == "_a_1", "下划线合法"
    assert mod.normalize_handle("a" * 15) == "a" * 15, "15 位上限合法"


def test_main_invalid_handle_is_argparse_error(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["twitter_survey.py", "--handle", "bad handle!", "--no-llm"])
    with pytest.raises(SystemExit) as ei:
        mod.main()
    assert ei.value.code == 2
    assert "无效 handle" in capsys.readouterr().err, "非法字符走 argparse error"


# --- nitter 实例列表解析 ---


def test_nitter_instances_defaults_to_constant():
    assert mod.nitter_instances() == mod.NITTER_INSTANCES


def test_nitter_instances_env_override_splits_and_filters():
    env = {"TWITTER_NITTER_INSTANCES": "a.com, b.com ,, c.com"}
    assert mod.nitter_instances(env=env) == ["a.com", "b.com", "c.com"]


def test_nitter_instances_env_blank_falls_back_to_constant():
    assert mod.nitter_instances(env={"TWITTER_NITTER_INSTANCES": "  "}) == mod.NITTER_INSTANCES


def test_nitter_instances_param_wins_over_env():
    assert mod.nitter_instances(["x.com"], env={"TWITTER_NITTER_INSTANCES": "a.com"}) == ["x.com"]


# --- nitter 页解析 ---

# 真实 nitter 标记形状：tweet-date 是 <span>（位于 tweet-body > tweet-header >
# tweet-name-row 内），日期链接 href 带 #m 片段；quote 内嵌自己的 tweet-date
NITTER_HTML = """
<div class="timeline">
  <div class="timeline-item">
    <a class="tweet-link" href="/alice/status/1001"></a>
    <div class="tweet-body">
      <div class="tweet-header">
        <div class="tweet-name-row">
          <span class="tweet-date"><a href="/alice/status/1001#m" title="Sep 1, 2026 · 10:00 AM UTC">Sep 1</a></span>
        </div>
      </div>
      <div class="tweet-content media-body">First tweet <a href="/alice/status/1001">text</a><br>Second line</div>
    </div>
  </div>
  <div class="timeline-item">
    <a class="tweet-link" href="/alice/status/1002"></a>
    <div class="tweet-body">
      <div class="tweet-header">
        <div class="tweet-name-row">
          <span class="tweet-date"><a href="/alice/status/1002#m">Sep 2</a></span>
        </div>
      </div>
      <div class="tweet-content media-body">Second main</div>
      <div class="quote">
        <a class="quote-link" href="/bob/status/2002"></a>
        <div class="tweet-content media-body">quoted text</div>
        <span class="tweet-date"><a href="/bob/status/2002#m" title="Jun 1, 2025 · 08:00 AM UTC">Jun 1, 2025</a></span>
      </div>
    </div>
  </div>
  <div class="timeline-item">
    <a class="tweet-link" href="/alice/status/1003"></a>
    <div class="tweet-body">
      <div class="tweet-header">
        <div class="tweet-name-row">
          <span class="tweet-date"><a href="/alice/status/1003#m" title="Sep 3, 2026 · 09:00 AM UTC">Sep 3</a></span>
        </div>
      </div>
      <div class="tweet-media"><img src="/pic.jpg"></div>
    </div>
  </div>
</div>
"""


def test_parse_nitter_tweets_extracts_text_date_and_url():
    tweets = mod.parse_nitter_tweets(NITTER_HTML, "https://xcancel.com")
    assert tweets[0] == {
        "date": "Sep 1, 2026 · 10:00 AM UTC",
        "text": "First tweet text Second line",
        "url": "https://x.com/alice/status/1001",
    }


def test_parse_nitter_tweets_quote_text_not_merged_and_date_falls_back_to_anchor_text():
    tweets = mod.parse_nitter_tweets(NITTER_HTML, "https://xcancel.com")
    assert tweets[1]["text"] == "Second main", "引用推文的正文不得混入主推文"
    assert tweets[1]["date"] == "Sep 2", "date 链接无 title 时回退用链接文本"
    assert tweets[1]["url"] == "https://x.com/alice/status/1002"


def test_parse_nitter_tweets_quote_date_does_not_override_main_span_date():
    """真实标记形状（span.tweet-date 在 tweet-header > tweet-name-row 内，无 tweet-link，
    日期链接是唯一链接来源）：主推文日期取自 span 内 <a> 的 title，
    quote 自带 tweet-date（有 title）不得覆盖主推文日期与链接。"""
    html = (
        '<div class="timeline-item"><div class="tweet-body">'
        '<div class="tweet-header"><div class="tweet-name-row">'
        '<span class="tweet-date"><a href="/alice/status/1001#m" '
        'title="Sep 1, 2026 · 10:00 AM UTC">Sep 1</a></span>'
        "</div></div>"
        '<div class="tweet-content media-body">Main text</div>'
        '<div class="quote">'
        '<span class="tweet-date"><a href="/bob/status/2002#m" '
        'title="Jun 1, 2025 · 08:00 AM UTC">Jun 1, 2025</a></span>'
        '<div class="tweet-content media-body">quoted text</div>'
        "</div></div></div>"
    )
    tweets = mod.parse_nitter_tweets(html, "https://nitter.example")
    assert len(tweets) == 1
    assert tweets[0]["date"] == "Sep 1, 2026 · 10:00 AM UTC", "主推文日期来自 tweet-header 内 span 的 title"
    assert tweets[0]["url"] == "https://x.com/alice/status/1001", (
        "quote 的日期链接不得覆盖主推文链接；#m 剥离后归一化为 x.com canonical"
    )
    assert tweets[0]["text"] == "Main text", "quote 正文不得混入主推文"


def test_parse_nitter_tweets_skips_empty_text_items():
    assert len(mod.parse_nitter_tweets(NITTER_HTML, "https://xcancel.com")) == 2, "纯媒体（无正文）推文丢弃"


def test_parse_nitter_tweets_dedupes_by_url():
    html = "".join(
        '<div class="timeline-item"><a class="tweet-link" href="/a/status/1"></a>'
        '<div class="tweet-content">Same</div>'
        '<div class="tweet-date"><a href="/a/status/1" title="d1">t</a></div></div>'
        for _ in range(2)
    )
    assert len(mod.parse_nitter_tweets(html, "https://x.example")) == 1


def test_parse_nitter_tweets_anti_scrape_page_returns_empty():
    assert mod.parse_nitter_tweets("<html><body>只是验证页</body></html>") == []


def test_parse_nitter_tweets_url_normalized_to_xcom_canonical():
    def item(href):
        return (
            '<div class="timeline-item"><a class="tweet-link" href="%s"></a>'
            '<div class="tweet-content">t</div></div>' % href
        )

    tweets = mod.parse_nitter_tweets(item("/alice/status/1001#m"), "https://xcancel.com")
    assert tweets[0]["url"] == "https://x.com/alice/status/1001", (
        "剥 #m 片段并归一化为 https://x.com/{user}/status/{id} canonical"
    )
    tweets = mod.parse_nitter_tweets(item("https://xcancel.com/alice/status/1002#m"), "https://xcancel.com")
    assert tweets[0]["url"] == "https://x.com/alice/status/1002", "绝对链接同样归一化"
    tweets = mod.parse_nitter_tweets(item("/alice"), "https://xcancel.com")
    assert tweets[0]["url"] == "https://xcancel.com/alice", "解析不出 status id 的链接保持原样，不硬造 canonical"


# --- x.com SSR 文本挖掘 ---

SSR_HTML = r"""<html><script>window.__INITIAL_STATE__=
{"entities":{"tweets":{"1":{"full_text":"Line1\nLine2 #web3"}}},"meta":"text"}</script></html>"""


def test_parse_x_ssr_tweets_mines_full_text_and_unescapes():
    tweets = mod.parse_x_ssr_tweets(SSR_HTML)
    assert tweets == [{"date": "", "text": "Line1\nLine2 #web3", "url": ""}]


def test_parse_x_ssr_tweets_no_data_returns_empty():
    assert mod.parse_x_ssr_tweets("<html>登录墙，没有推文数据</html>") == []


# --- 抓取防御 ---


def test_fetch_url_skips_private_and_loopback_hosts(monkeypatch):
    monkeypatch.setattr(mod.urllib.request, "urlopen", boom("私网/环回地址不应发起请求"))
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
        assert mod.fetch_url(url) == "", url


class _FakeResp:
    """urlopen 注入替身：带 get_content_charset 的 headers + 可截断 read 的字节体。"""

    def __init__(self, payload, charset="utf-8"):
        self._payload = payload
        self.headers = SimpleNamespace(get_content_charset=lambda: charset)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        return self._payload if n is None or n < 0 else self._payload[:n]


def test_fetch_url_decodes_with_response_charset(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["ua"] = req.headers.get("User-agent")
        return _FakeResp("中文内容".encode("gbk"), charset="gbk")

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    assert mod.fetch_url("https://example.com/x") == "中文内容", "按响应 charset 解码"
    assert captured["ua"] == mod.USER_AGENT, "正常路径带浏览器 UA"


def test_fetch_url_truncates_body_at_2mb(monkeypatch):
    monkeypatch.setattr(
        mod.urllib.request, "urlopen", lambda req, timeout=None: _FakeResp(b"a" * (3 * 1024 * 1024), charset="ascii")
    )
    body = mod.fetch_url("https://example.com/big")
    assert len(body) == 2 * 1024 * 1024, "响应体最多读 2MB 再 decode"


# --- nitter 多实例策略 ---


def test_nitter_fetch_tries_instances_until_hit():
    calls = []

    def fetch(url):
        calls.append(url)
        if "nitter.net" in url:
            raise RuntimeError("超时")
        if "dead.com" in url:
            return "<p>验证页，无推文</p>"
        return NITTER_HTML

    tweets = mod.nitter_fetch("alice", instances=["nitter.net", "dead.com", "xcancel.com"], fetch_fn=fetch)
    assert calls == ["https://nitter.net/alice", "https://dead.com/alice", "https://xcancel.com/alice"], (
        "失败/空结果换下一个实例"
    )
    assert tweets[0]["url"].endswith("/alice/status/1001")


def test_nitter_fetch_all_instances_raise_raises_last_error():
    calls = []

    def fetch(url):
        calls.append(url)
        raise RuntimeError(f"down:{url}")

    with pytest.raises(RuntimeError, match="down:https://b.com/alice"):
        mod.nitter_fetch("alice", instances=["a.com", "b.com"], fetch_fn=fetch)
    assert calls == ["https://a.com/alice", "https://b.com/alice"]


def test_nitter_fetch_all_instances_empty_returns_empty_list():
    assert mod.nitter_fetch("alice", instances=["a.com"], fetch_fn=lambda u: "<p>无推文</p>") == []


def test_nitter_fetch_accumulates_per_instance_errors():
    errors = []

    def fetch(url):
        if "a.com" in url:
            raise RuntimeError("超时")
        return "<p>无推文</p>"

    with pytest.raises(RuntimeError, match="超时"):
        mod.nitter_fetch("alice", instances=["a.com", "b.com"], fetch_fn=fetch, errors=errors)
    assert errors == ["nitter(a.com): 超时", "nitter(b.com): 无结果"], (
        "逐实例失败原因以 nitter(host): 原因 累积进 errors（不只 print）"
    )


def test_nitter_fetch_success_does_not_pollute_errors():
    errors = []
    tweets = mod.nitter_fetch("alice", instances=["a.com"], fetch_fn=lambda u: NITTER_HTML, errors=errors)
    assert tweets and errors == [], "命中实例不往 errors 写无关条目"


def test_survey_records_nitter_per_instance_errors(monkeypatch):
    def fetch(url):
        raise RuntimeError("超时")

    monkeypatch.setattr(mod, "fetch_url", fetch)
    data = mod.survey(
        "alice",
        instances=["a.com"],
        ssr_fn=lambda: [make_tweet(2)],
        camoufox_fn=boom("不应到策略3"),
        sleep_fn=lambda: None,
        now=NOW,
    )
    assert data["strategy_used"] == "x_ssr"
    assert data["errors"] == ["nitter(a.com): 超时", "nitter: 超时"], "nitter 逐实例失败原因随快照 errors 落盘"


def test_x_ssr_fetch_mines_via_injected_fetch_fn():
    tweets = mod.x_ssr_fetch("alice", fetch_fn=lambda u: SSR_HTML)
    assert tweets and tweets[0]["text"] == "Line1\nLine2 #web3"


# --- 多策略编排（测试重点） ---


def test_survey_strategy1_success_no_fallback_no_sleep():
    data = mod.survey(
        "alice",
        nitter_fn=lambda: [make_tweet(1)],
        ssr_fn=boom("策略1 成功不应回退"),
        camoufox_fn=boom("策略1 成功不应回退"),
        sleep_fn=lambda: raises("策略1 成功不应 sleep"),
        now=NOW,
    )
    assert data["handle"] == "alice" and data["fetched_at"] == NOW_S
    assert data["strategy_used"] == "nitter"
    assert data["tweets"] == [make_tweet(1)]
    assert data["errors"] == []


def test_survey_falls_back_to_ssr_after_nitter_failure_with_polite_sleep():
    order = []

    def nitter():
        order.append("nitter")
        raise RuntimeError("全部实例挂了")

    data = mod.survey(
        "alice",
        nitter_fn=nitter,
        ssr_fn=lambda: (order.append("ssr"), [make_tweet(2)])[1],
        camoufox_fn=boom("不应到策略3"),
        sleep_fn=lambda: order.append("sleep"),
        now=NOW,
    )
    assert data["strategy_used"] == "x_ssr"
    assert order == ["nitter", "sleep", "ssr"], "回退前先做礼貌间隔"
    assert data["errors"] == ["nitter: 全部实例挂了"], "前序策略失败原因记入 errors"


def test_survey_falls_back_to_camoufox_after_two_failures():
    order = []

    def nitter():
        order.append("nitter")
        return []

    def ssr():
        order.append("ssr")
        raise RuntimeError("被反爬拦截")

    data = mod.survey(
        "alice",
        nitter_fn=nitter,
        ssr_fn=ssr,
        camoufox_fn=lambda: (order.append("camo"), [make_tweet(3)])[1],
        sleep_fn=lambda: order.append("sleep"),
        now=NOW,
    )
    assert data["strategy_used"] == "camoufox"
    assert order == ["nitter", "sleep", "ssr", "sleep", "camo"], "策略间各一次礼貌间隔"
    assert data["errors"] == ["nitter: 无结果", "x_ssr: 被反爬拦截"]


# --- camoufox 代理配置（#17）---


def test_camoufox_kwargs_defaults_without_proxy():
    kwargs = mod.camoufox_kwargs(env={})
    assert "proxy" not in kwargs, "env 未配置时行为不变"
    assert kwargs["headless"] is True


def test_camoufox_kwargs_env_proxy_passthrough():
    kwargs = mod.camoufox_kwargs(env={"TWITTER_CAMOUFOX_PROXY": " http://127.0.0.1:7890 "})
    assert kwargs["proxy"] == {"server": "http://127.0.0.1:7890"}


def test_camoufox_fetch_starts_with_env_proxy(monkeypatch):
    """camoufox_fetch 把 env 代理显式传给 Camoufox（Firefox 系不读 https_proxy env）。"""
    import types

    calls = {}
    items = [{"date": "2026-09-01", "text": "t1", "url": "https://x.com/a/status/1"}]
    fake_page = SimpleNamespace(goto=lambda *a, **k: None, evaluate=lambda *a, **k: items)

    class FakeBrowser:
        def __init__(self, **kwargs):
            calls["kwargs"] = kwargs

        def __enter__(self):
            return SimpleNamespace(new_page=lambda: fake_page)

        def __exit__(self, *exc):
            return False

    fake_pkg = types.ModuleType("camoufox")
    fake_api = types.ModuleType("camoufox.sync_api")
    fake_api.Camoufox = FakeBrowser
    fake_pkg.sync_api = fake_api
    monkeypatch.setitem(sys.modules, "camoufox", fake_pkg)
    monkeypatch.setitem(sys.modules, "camoufox.sync_api", fake_api)
    monkeypatch.setenv("TWITTER_CAMOUFOX_PROXY", "socks5://127.0.0.1:1080")
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    tweets = mod.camoufox_fetch("alice")
    assert calls["kwargs"]["proxy"] == {"server": "socks5://127.0.0.1:1080"}
    assert tweets[0]["text"] == "t1"


def test_survey_all_strategies_fail_raises_with_all_reasons():
    sleeps = []
    with pytest.raises(RuntimeError) as ei:
        mod.survey(
            "alice",
            nitter_fn=boom("nitter 挂"),
            ssr_fn=boom("ssr 挂"),
            camoufox_fn=boom("camo 挂"),
            sleep_fn=lambda: sleeps.append(1),
            now=NOW,
        )
    msg = str(ei.value)
    for reason in ("nitter 挂", "ssr 挂", "camo 挂"):
        assert reason in msg
    assert len(sleeps) == 2, "三个策略之间共两次礼貌间隔"


def test_survey_filters_empty_text_and_truncates_to_max_tweets():
    tweets = [make_tweet(1), {"date": "", "text": "  ", "url": "u"}, make_tweet(3), make_tweet(4)]
    data = mod.survey(
        "alice",
        nitter_fn=lambda: list(tweets),
        max_tweets=2,
        ssr_fn=boom("不应回退"),
        camoufox_fn=boom("不应回退"),
        sleep_fn=lambda: None,
        now=NOW,
    )
    assert data["tweets"] == [make_tweet(1), make_tweet(3)]


# --- 素材组装 ---


def test_build_material_carries_date_text_and_url():
    m = mod.build_material([make_tweet(1)])
    assert "tweet1" in m and "2026-09-01" in m and "https://x.com/a/status/1" in m


def test_build_material_marks_missing_date_and_skips_missing_url():
    m = mod.build_material([{"date": "", "text": "无日期推文", "url": ""}])
    assert "无日期推文" in m and "日期不明" in m and "链接:" not in m


def test_build_material_truncates_to_budget():
    tweets = [{"date": f"d{i}", "text": "字" * 8000, "url": f"https://x.com/a/status/{i}"} for i in range(10)]
    m = mod.build_material(tweets)
    assert len(m) <= mod.MAX_MATERIAL_CHARS, "素材总量不超预算（对齐 qwen3:8b 上下文）"
    assert m.count("字" * 8000) == 2, "预算内的推文正文完整保留"
    assert "status/2" in m, "最后一篇可部分截断，日期与链接头仍保留"
    assert "status/3" not in m, "预算耗尽后后续推文整条丢弃"


# --- LLM 综合 ---


def test_survey_system_prompt_carries_constraints():
    s = mod.SURVEY_SYSTEM
    assert "不得编造" in s or "不得推测" in s, "禁推测约束必须进 prompt"
    assert "素材不足" in s, "素材不足的处置约定必须进 prompt"
    assert "日期" in s, "关键结论标注推文日期必须进 prompt"
    assert "可引用锚点" in s, "招呼语可引用的推文锚点板块必须进 prompt"
    assert s.rstrip().endswith("/no_think")


def test_synthesize_sends_system_handle_and_material():
    captured = {}

    def llm(messages, cfg):
        captured.update(messages=messages)
        return '{"markdown": "## 综合结论"}'

    md = mod.synthesize("alice", "素材文本", llm, {})
    assert md == "## 综合结论"
    system, user = captured["messages"][0]["content"], captured["messages"][1]["content"]
    assert system == mod.SURVEY_SYSTEM
    assert "alice" in user and "素材文本" in user


def test_synthesize_empty_markdown_raises():
    with pytest.raises(ValueError):
        mod.synthesize("a", "m", lambda m, c: '{"markdown": "  "}', {})


# --- 草稿渲染 ---


def test_render_draft_marks_draft_with_meta_and_body():
    text = mod.render_draft("alice", "## 正文", 12, "nitter", now=NOW)
    assert text.startswith("# 推特调研草稿")
    assert "草稿" in text and "人工审核" in text, "草稿边界声明必须在"
    assert "alice" in text and "nitter" in text and "12 条" in text and NOW_S in text
    assert "## 正文" in text


# --- 主流程（注入式） ---


def make_run_kwargs(tmp_path, data=None, llm=None, **kw):
    d = dict(
        max_tweets=30,
        no_llm=False,
        instances=None,
        output_dir=str(tmp_path / "data"),
        draft_dir=str(tmp_path / "draft"),
        survey_fn=(lambda h: data if data is not None else make_data(h)),
        llm_call=llm or (lambda m, c: '{"markdown": "## 综合结论"}'),
        cfg={},
        sleep_fn=lambda: None,
        now=NOW,
    )
    d.update(kw)
    return d


def test_run_writes_json_and_draft(tmp_path):
    mod.run("alice", **make_run_kwargs(tmp_path))
    saved = json.loads((tmp_path / "data" / "alice.json").read_text(encoding="utf-8"))
    assert saved["handle"] == "alice" and saved["strategy_used"] == "nitter"
    assert saved["tweets"][0] == make_tweet(1) and "errors" in saved
    text = (tmp_path / "draft" / "twitter-alice-draft.md").read_text(encoding="utf-8")
    assert "## 综合结论" in text and "草稿" in text


def test_run_strips_at_prefix_for_survey_and_filenames(tmp_path):
    seen = []
    mod.run("@Bob", **make_run_kwargs(tmp_path, survey_fn=lambda h: (seen.append(h), make_data(h))[1]))
    assert seen == ["Bob"], "@ 前缀自动剥离后才传给调研编排"
    assert (tmp_path / "data" / "Bob.json").exists()
    assert (tmp_path / "draft" / "twitter-Bob-draft.md").exists()


def test_run_no_llm_only_fetches_data(tmp_path):
    mod.run("a", **make_run_kwargs(tmp_path, no_llm=True, llm=lambda m, c: raises("--no-llm 不应调 LLM")))
    assert (tmp_path / "data" / "a.json").exists()
    assert not (tmp_path / "draft" / "twitter-a-draft.md").exists()


def test_run_overwrites_json_snapshot(tmp_path):
    kw = make_run_kwargs(tmp_path)
    mod.run("a", **kw)
    second = make_data("a")
    second["tweets"] = [{"date": "d", "text": "new", "url": "u"}]
    mod.run("a", **{**kw, "survey_fn": lambda h: second, "llm_call": lambda m, c: '{"markdown": "二稿"}'})
    saved = json.loads((tmp_path / "data" / "a.json").read_text(encoding="utf-8"))
    assert len(saved["tweets"]) == 1 and saved["tweets"][0]["text"] == "new", "原始数据快照语义：重跑覆盖旧文件"
    assert "二稿" in (tmp_path / "draft" / "twitter-a-draft.md").read_text(encoding="utf-8")


def test_run_feeds_tweets_with_date_and_url_to_llm(tmp_path):
    msgs = []

    def llm(m, c):
        msgs.append(m)
        return '{"markdown": "## 结论"}'

    mod.run("a", **make_run_kwargs(tmp_path, llm=llm))
    user = msgs[0][1]["content"]
    assert "tweet1" in user and "2026-09-01" in user and "https://x.com/a/status/1" in user, (
        "推文素材必须带日期+链接喂给 LLM"
    )


def test_run_survey_failure_exits_without_writing(tmp_path):
    with pytest.raises(SystemExit):
        mod.run("a", **make_run_kwargs(tmp_path, survey_fn=boom("三种策略均未抓到推文")))
    assert not (tmp_path / "data" / "a.json").exists()


def test_run_llm_failure_exits_but_keeps_json(tmp_path):
    with pytest.raises(SystemExit):
        mod.run("a", **make_run_kwargs(tmp_path, llm=boom("LLM 挂了")))
    assert (tmp_path / "data" / "a.json").exists(), "数据先落盘，LLM 失败后保留"
    assert not (tmp_path / "draft" / "twitter-a-draft.md").exists(), "失败的草稿不落半成品"
