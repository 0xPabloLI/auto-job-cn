#!/usr/bin/env python3
"""_llm 共享底座的行为测试（原在 test_gen_greetings.py，2026-09-10 gen_greetings
删除本地 LLM drafter 路径后迁出；_llm 仍服务 repo_survey/build_kb/industry_research/
twitter_survey 等调研脚本）。"""

import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("_llm", Path(__file__).resolve().parent / "_llm.py")
assert _spec is not None and _spec.loader is not None, "importlib spec 加载失败（模块文件缺失？）"
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def test_llm_config_defaults_and_override():
    cfg = mod.llm_config(env={})
    assert cfg["base_url"] == "http://127.0.0.1:11434/v1"
    assert cfg["timeout"] == 120, "默认超时 120s 保持现状"
    cfg2 = mod.llm_config(
        env={
            "GREETING_LLM_BASE_URL": "https://api.x.cn/v1/",
            "GREETING_LLM_MODEL": "glm-4",
            "GREETING_LLM_API_KEY": "sk-1",
        }
    )
    assert cfg2["base_url"] == "https://api.x.cn/v1"
    assert cfg2["model"] == "glm-4"
    assert cfg2["api_key"] == "sk-1"


def test_llm_config_timeout_override(monkeypatch):
    """大模型（27b+）单条生成可超 120s（#9 E2E 实测 1.1s/token）——超时须可配。"""
    cfg = mod.llm_config(env={"GREETING_LLM_TIMEOUT": "900"})
    assert cfg["timeout"] == 900
    bad = mod.llm_config(env={"GREETING_LLM_TIMEOUT": "abc"})
    assert bad["timeout"] == 120, "非法值回退默认"
    for bad_val in ("0", "-5"):
        assert mod.llm_config(env={"GREETING_LLM_TIMEOUT": bad_val})["timeout"] == 120, (
            "非正数回退默认（0 或负值等于没配）"
        )


def test_call_llm_default_transport_uses_cfg_timeout(monkeypatch):
    """默认 transport 必须把 cfg['timeout'] 传给 urlopen（超时可配的接线点）。"""
    captured = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"choices": [{"message": {"content": "ok"}}]}'

    def fake_urlopen(req, timeout):
        captured["timeout"] = timeout
        return _FakeResp()

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = mod.call_llm(
        [{"role": "user", "content": "hi"}],
        {"base_url": "http://127.0.0.1:11434/v1", "model": "m", "api_key": "", "timeout": 600},
    )
    assert out == "ok"
    assert captured["timeout"] == 600


def test_call_llm_builds_openai_compatible_request():
    captured = {}

    def transport(url, payload, headers):
        captured.update(url=url, payload=payload, headers=headers)
        return {"choices": [{"message": {"content": '{"greeting": "hi"}'}}]}

    cfg = {"base_url": "http://127.0.0.1:11434/v1", "model": "qwen3:8b", "api_key": "ollama"}
    out = mod.call_llm([{"role": "user", "content": "hi"}], cfg, transport=transport)
    assert out == '{"greeting": "hi"}'
    assert captured["url"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert captured["payload"]["model"] == "qwen3:8b"
    assert captured["payload"]["messages"] == [{"role": "user", "content": "hi"}]
    assert captured["headers"]["Authorization"] == "Bearer ollama"


def test_call_llm_falls_back_to_reasoning_when_content_empty():
    """qwen3 思考模式实测：兼容端点 content 为空、思考文本在 reasoning 字段（#9）。"""

    def transport(url, payload, headers):
        return {"choices": [{"message": {"content": "", "reasoning": '思考...{"greeting": "r"}'}}]}

    out = mod.call_llm(
        [{"role": "user", "content": "hi"}], {"base_url": "x", "model": "m", "api_key": ""}, transport=transport
    )
    assert out == '思考...{"greeting": "r"}'


# --- JSON 提取（call_llm 消费方的公共出口） ---


def test_extract_json_plain():
    assert mod.extract_json('{"greeting": "你好"}') == {"greeting": "你好"}


def test_extract_json_fenced():
    text = '```json\n{"greeting": "7年Solidity", "level": "高"}\n```'
    assert mod.extract_json(text)["level"] == "高"


def test_extract_json_with_prose():
    assert mod.extract_json('好的，结果如下：{"verdict": "pass"} 请查收') == {"verdict": "pass"}


def test_extract_json_nested():
    obj = mod.extract_json('前缀 {"a": {"b": "含}花括号"}, "c": 1} 后缀')
    assert obj == {"a": {"b": "含}花括号"}, "c": 1}


def test_extract_json_invalid_raises():
    with pytest.raises(ValueError):
        mod.extract_json("没有任何 JSON")
