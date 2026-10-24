#!/usr/bin/env python3
"""_logging 的行为测试（#38 结构化日志与日志脱敏）：凭证类 PII 掩码、递归脱敏、
JSONL 落盘开关、sink 路径解析、观测层永不抛异常。

拍板口径（issue #38 评论 2026-09-17 / triage 2026-09-16）：
- 双轨制：print 人读层与测试契约原样保留，本模块是独立机读旁路，严禁替代 print；
- 脱敏只掩码凭证类 PII（手机号/邮箱/身份证/token/cookie），公司/岗位/HR 名严禁脱敏
  （排障核心字段，数据已落 private/ 不入库）；
- 默认 ERROR 阈值（正常投递零额外 I/O）；AUTOJOB_STRUCTURED_LOG=1 → INFO，
  AUTOJOB_LOG_LEVEL 显式定级优先；AUTOJOB_LOG_PATH 覆盖落盘路径（测试/调试用）。
"""

import importlib.util
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location("_logging", Path(__file__).resolve().parent / "_logging.py")
assert _spec is not None and _spec.loader is not None, "importlib spec 加载失败（模块文件缺失？）"
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_lg := mod)


def _read_lines(p):
    """读回 JSONL 落盘内容（逐行 json 解析）。"""
    return [json.loads(line) for line in Path(p).read_text(encoding="utf-8").splitlines() if line.strip()]


# 脱敏器夹具：形状与真实手机号/身份证号一致，只在此集中声明一次（行内 pii-ok 供边界门禁放行）
FAKE_MOBILE = "13812345678"  # pii-ok
FAKE_ID = "610101199003071234"  # pii-ok

# --- 脱敏：凭证类 PII ---


def test_scrub_phone_partial_mask():
    """11 位手机号掩码保留首 3 末 4，便于排障辨认。"""
    assert _lg.scrub_text(f"联系方式 {FAKE_MOBILE}，下午聊") == "联系方式 138****5678，下午聊"


def test_epoch_ms_and_short_digits_not_masked():
    """13 位毫秒时间戳 / 非 11 位数字不动——脱敏不得破坏时间线等观测字段。"""
    assert _lg.scrub_text("ts=1726848000000") == "ts=1726848000000"
    assert _lg.scrub_text("code=37 count=12") == "code=37 count=12"


def test_scrub_email_keeps_domain():
    """邮箱保留首字符与域名（域名不是凭证）。"""
    assert _lg.scrub_text("邮件 a.b@example.com 收到") == "邮件 a***@example.com 收到"


def test_scrub_id_card_only_18():
    """18 位身份证掩码；17 位编号不误伤。"""
    assert _lg.scrub_text(f"证件 {FAKE_ID}") == "证件 6101**********1234"
    assert _lg.scrub_text("编号 61010119900307123 保留") == "编号 61010119900307123 保留"


def test_scrub_token_kv_and_bearer():
    """token/cookie/authorization 等键值与 Bearer 头全掩码（凭证不留尾）。"""
    assert _lg.scrub_text("token=abc123def") == "token=******"
    assert _lg.scrub_text("Cookie: sessionid=xyz789; path=/") == "Cookie=******; path=/"
    assert _lg.scrub_text("Authorization: Bearer eyJhbGci.abc-123") == "Authorization: Bearer ******"


def test_company_and_hr_names_not_scrubbed():
    """公司名/HR 称谓/岗位名是排障核心字段——triage 拍板严禁脱敏。"""
    s = "灼识科技 涓女士 RAG 平台工程师（可远程）"
    assert _lg.scrub_text(s) == s


def test_scrub_value_masks_sensitive_keys():
    """字段键是凭证名（token/cookie/authorization 等）→ 整值掩码——结构化日志携带
    凭证的正道（真实冒烟 2026-09-20 踩中：kv 正则只覆盖自由文本，漏掉字段键形态）。"""
    v = _lg.scrub_value({"token": "abc123", "Cookie": "sid=xyz", "brand": "灼识科技", "contact": "涓女士"})
    assert v == {"token": "******", "Cookie": "******", "brand": "灼识科技", "contact": "涓女士"}


def test_scrub_value_recursive_and_types():
    """递归脱敏嵌套结构；int/bool/None 原样（时间戳等非字符串零风险）。"""
    v = _lg.scrub_value({"a": [FAKE_MOBILE], "b": {"c": "x@y.com"}, "n": 1726848000000, "t": True, "none": None})
    assert v == {"a": ["138****5678"], "b": {"c": "x***@y.com"}, "n": 1726848000000, "t": True, "none": None}


# --- 落盘开关与 JSONL ---


def test_log_event_writes_jsonl_when_enabled(tmp_path, monkeypatch):
    """AUTOJOB_STRUCTURED_LOG=1 → JSONL 落盘：字段脱敏但公司名保留，ts/level 在位。"""
    p = tmp_path / "app.log"
    monkeypatch.setenv("AUTOJOB_LOG_PATH", str(p))
    monkeypatch.setenv("AUTOJOB_STRUCTURED_LOG", "1")
    _lg.log_event("greeting_applied", job="RAG 平台工程师", brand="灼识科技", contact=FAKE_MOBILE)
    recs = _read_lines(p)
    assert len(recs) == 1
    assert recs[0]["event"] == "greeting_applied"
    assert recs[0]["level"] == "INFO"
    assert recs[0]["ts"]
    assert recs[0]["contact"] == "138****5678"
    assert recs[0]["brand"] == "灼识科技"
    assert recs[0]["job"] == "RAG 平台工程师"


def test_log_event_default_silent_for_info(tmp_path, monkeypatch):
    """默认阈值 ERROR：info 事件零 I/O（正常投递不产生日志文件）。"""
    p = tmp_path / "app.log"
    monkeypatch.setenv("AUTOJOB_LOG_PATH", str(p))
    monkeypatch.delenv("AUTOJOB_STRUCTURED_LOG", raising=False)
    monkeypatch.delenv("AUTOJOB_LOG_LEVEL", raising=False)
    _lg.log_event("greeting_applied", job="x")
    assert not p.exists()


def test_log_event_level_env_debug(tmp_path, monkeypatch):
    """AUTOJOB_LOG_LEVEL=DEBUG 显式定级：debug 事件落盘。"""
    p = tmp_path / "app.log"
    monkeypatch.setenv("AUTOJOB_LOG_PATH", str(p))
    monkeypatch.setenv("AUTOJOB_LOG_LEVEL", "DEBUG")
    _lg.log_event("detail_probe", level="debug", depth=3)
    recs = _read_lines(p)
    assert recs[0]["level"] == "DEBUG"
    assert recs[0]["depth"] == 3


def test_log_event_invalid_level_env_falls_back(tmp_path, monkeypatch):
    """非法定级回落结构化档再回落 ERROR——不猜、不崩。"""
    p = tmp_path / "app.log"
    monkeypatch.setenv("AUTOJOB_LOG_PATH", str(p))
    monkeypatch.setenv("AUTOJOB_LOG_LEVEL", "LOUD")
    _lg.log_event("greeting_applied")
    assert not p.exists()
    monkeypatch.setenv("AUTOJOB_STRUCTURED_LOG", "1")
    _lg.log_event("greeting_applied")
    assert p.exists()


def test_log_event_switches_sink_on_path_change(tmp_path, monkeypatch):
    """AUTOJOB_LOG_PATH 变更 → handler 重建，各自路径只收各自事件。"""
    p1, p2 = tmp_path / "a.log", tmp_path / "b.log"
    monkeypatch.setenv("AUTOJOB_STRUCTURED_LOG", "1")
    monkeypatch.setenv("AUTOJOB_LOG_PATH", str(p1))
    _lg.log_event("e1")
    monkeypatch.setenv("AUTOJOB_LOG_PATH", str(p2))
    _lg.log_event("e2")
    assert [r["event"] for r in _read_lines(p1)] == ["e1"]
    assert [r["event"] for r in _read_lines(p2)] == ["e2"]


def test_log_event_creates_missing_parent_dirs(tmp_path, monkeypatch):
    """sink 父目录缺失（新 clone/worktree 无 private/）→ 自建目录落盘，不静默丢事件
    （真实冒烟 2026-09-20 踩中：fresh worktree 无 private/ 导致 FileHandler 丢弃）。"""
    p = tmp_path / "fresh" / "private" / "data" / "app.log"
    monkeypatch.setenv("AUTOJOB_LOG_PATH", str(p))
    monkeypatch.setenv("AUTOJOB_STRUCTURED_LOG", "1")
    _lg.log_event("greeting_applied", job="x")
    assert [r["event"] for r in _read_lines(p)] == ["greeting_applied"]


def test_log_event_never_raises(tmp_path, monkeypatch):
    """观测层永不影响业务：不可序列化对象 repr 兜底；路径不可写静默丢弃。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("普通文件，不是目录", encoding="utf-8")
    p = blocker / "app.log"  # 父路径是文件 → mkdir 必败
    monkeypatch.setenv("AUTOJOB_LOG_PATH", str(p))
    monkeypatch.setenv("AUTOJOB_STRUCTURED_LOG", "1")
    _lg.log_event("evt", obj=object(), nested={"weird": {1, 2}})
    assert not p.exists()  # 落盘失败不冒泡


def test_log_event_swallows_profile_resolution_failure(monkeypatch):
    """profile 解析失败（如多 profile 无 default 的 SystemExit）→ 事件丢弃不崩业务
    ——不代猜路径，与 #28 fail-loud 同语义。"""

    def boom(**kwargs):
        raise SystemExit("blocked: multi profile no default")

    monkeypatch.setattr(_lg, "log_path", boom)
    _lg.log_event("greeting_applied", job="x")  # 不抛即通过


def test_get_logger_shares_sink_and_respects_switch(tmp_path, monkeypatch):
    """get_logger(name)：评审点名 API——子 logger 挂同一 JSONL sink，info 阈值下落盘。
    stdlib 惯用口：消息原样落盘，行结构由调用方负责（schema 化走 log_event）。"""
    p = tmp_path / "app.log"
    monkeypatch.setenv("AUTOJOB_LOG_PATH", str(p))
    monkeypatch.setenv("AUTOJOB_STRUCTURED_LOG", "1")
    lg = _lg.get_logger("smoke-test")
    lg.info("hello %s", "world")
    assert p.read_text(encoding="utf-8").splitlines() == ["hello world"]


def test_get_logger_default_threshold_silent(tmp_path, monkeypatch):
    """get_logger 同受默认 ERROR 阈值约束：info 不落盘。"""
    p = tmp_path / "app.log"
    monkeypatch.setenv("AUTOJOB_LOG_PATH", str(p))
    monkeypatch.delenv("AUTOJOB_STRUCTURED_LOG", raising=False)
    monkeypatch.delenv("AUTOJOB_LOG_LEVEL", raising=False)
    _lg.get_logger("smoke-test").info("quiet")
    assert not p.exists()


# --- sink 路径解析 ---


def test_log_path_env_override(tmp_path):
    """AUTOJOB_LOG_PATH 显式覆盖优先于 profile 解析（测试/调试入口）。"""
    assert _lg.log_path(env={"AUTOJOB_LOG_PATH": str(tmp_path / "x.log")}) == tmp_path / "x.log"


def test_log_path_legacy_and_profile_prefix(tmp_path):
    """无注册表 → private/data/app.log；指名非在位 profile → <name>. 前缀（#28 扁平命名）。"""
    (tmp_path / "private" / "data").mkdir(parents=True)
    assert _lg.log_path(root=tmp_path, env={}) == tmp_path / "private" / "data" / "app.log"
    (tmp_path / "private" / "profiles.json").write_text(
        json.dumps({"default": "ai", "profiles": {"ai": {"resume": "r.docx"}, "b": {"resume": "r2.docx"}}}),
        encoding="utf-8",
    )
    assert _lg.log_path(root=tmp_path, env={"AUTOJOB_PROFILE": "b"}) == tmp_path / "private" / "data" / "b.app.log"
