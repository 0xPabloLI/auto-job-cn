#!/usr/bin/env python3
"""
scripts 共享 LLM 底座：gen_greetings / repo_survey / build_kb 共用的三件套
（llm_config / extract_json / call_llm），后续 LLM 消费方也从这里导入。

env 约定：OpenAI 兼容 /v1/chat/completions。默认本地 Ollama（qwen3:8b），
GREETING_LLM_BASE_URL / GREETING_LLM_MODEL / GREETING_LLM_API_KEY / GREETING_LLM_TIMEOUT 可换任意兼容服务。

导入方式（scripts/ 目录内平铺，参照 crawl_jds.py 导入 platforms 的处理）：
  sys.path.insert(0, str(Path(__file__).resolve().parent))
  from _llm import call_llm, extract_json, llm_config
"""

import json
import os
import urllib.request


_DEFAULT_TIMEOUT = 120


def llm_config(env=None):
    env = os.environ if env is None else env
    try:
        timeout = int(env.get("GREETING_LLM_TIMEOUT") or 0) or _DEFAULT_TIMEOUT
    except ValueError:
        timeout = _DEFAULT_TIMEOUT
    if timeout <= 0:
        timeout = _DEFAULT_TIMEOUT
    return {
        "base_url": env.get("GREETING_LLM_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/"),
        "api_key": env.get("GREETING_LLM_API_KEY", "ollama"),
        "model": env.get("GREETING_LLM_MODEL", "qwen3:8b"),
        # 大模型单条生成可远超 120s（#9 E2E 实测 27b ~1.1s/token），超时须可配
        "timeout": timeout,
    }


def extract_json(text):
    """从 LLM 输出提取第一个 JSON 对象（容忍 ```json 围栏与前后杂文）。"""
    text = text.strip()
    if "```" in text:
        for part in text.split("```"):
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                text = part
                break
    start = text.find("{")
    if start == -1:
        raise ValueError(f"输出中没有 JSON 对象: {text[:80]!r}")
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
        return obj
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 解析失败: {e}: {text[:120]!r}")


def call_llm(messages, cfg, transport=None):
    """OpenAI 兼容 /chat/completions；transport 可注入测试。返回 assistant 文本。"""
    if transport is None:

        def transport(url, payload, headers):
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
            with urllib.request.urlopen(req, timeout=cfg.get("timeout", 120)) as resp:
                return json.loads(resp.read().decode("utf-8"))

    url = f"{cfg['base_url']}/chat/completions"
    payload = {"model": cfg["model"], "messages": messages, "temperature": 0.7}
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    data = transport(url, payload, headers)
    msg = data["choices"][0]["message"]
    # qwen3 思考模式在兼容端点会把输出放进 reasoning 字段、content 为空——回退兜底
    return msg.get("content") or msg.get("reasoning") or ""
