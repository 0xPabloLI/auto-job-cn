#!/usr/bin/env python3
"""结构化日志旁路（#38）——机读 JSONL 层；print 人读层原样保留（双轨制）。

拍板口径（triage 2026-09-16 + 深度 review 评论 2026-09-17）：
- **双轨制**：print 是 agent 消费接口（README「对 agent 说话即可」）与 9 个测试文件的
  capsys 断言契约——严禁替换或改措辞；本模块是独立旁路，只追加不替代。
- **零新依赖**：stdlib logging（structlog 与零依赖基调冲突）。
- **脱敏边界**：只掩码凭证类 PII（手机号/邮箱/身份证/token/cookie）；公司/岗位/HR 名
  严禁脱敏——排障核心可观测性字段，且数据已落 private/ 隔离区不入库。
- **默认安静**：阈值 ERROR（正常投递零额外 I/O）；AUTOJOB_STRUCTURED_LOG=1 → INFO
  全量；AUTOJOB_LOG_LEVEL（DEBUG/INFO/WARNING/ERROR）显式定级优先，非法值回落
  结构化档再回落 ERROR，不猜不崩；AUTOJOB_LOG_PATH 覆盖落盘路径（测试/调试用）。
- **落盘**：private/data/app.log（profile 化走 #28 扁平命名：非在位 profile 为
  app.<name>.log）；JSONL 每行一条事件。profile 解析失败 → 事件丢弃不代猜路径
  （与 #28 fail-loud 同语义）；观测层任何故障都不冒泡到业务脚本。
"""

import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _profile_paths import private_path, resolve_active_profile  # noqa: E402

_LOGGER_NAME = "autojob"
_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING, "ERROR": logging.ERROR}

# --- 脱敏正则（先凭证键值后号码，掩码保留可辨认片段； (?<!\d)/(?!\d) 防止命中
#     更长数字串的子串——毫秒时间戳/长编号零误伤） ---

_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ID_RE = re.compile(r"(?<!\d)\d{17}(?:\d|X|x)(?!\d)")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_KV_RE = re.compile(
    r"(?i)\b(access[_-]?token|refresh[_-]?token|api[_-]?key|apikey|secret|password|passwd|token|cookie|authorization)"
    r"\b\s*[=:：]\s*[\"']?(?!bearer\b)([^\s\"'&,;，；]+)"
)
# 字段键形态的凭证（log_event(token=...) 是结构化日志携带凭证的正道）：键全匹配才掩值，
# 子串匹配会误伤 tokenize_count 之类计数字段
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:access[_-]?token|refresh[_-]?token|api[_-]?key|apikey|secret|password|passwd|token|cookies?)"
)


def _mask_phone(m):
    return f"{m.group(0)[:3]}****{m.group(0)[-4:]}"


def _mask_email(m):
    local, domain = m.group(0).split("@", 1)
    return f"{local[0]}***@{domain}"


def _mask_id(m):
    return f"{m.group(0)[:4]}**********{m.group(0)[-4:]}"


def _mask_bearer(m):
    return f"{m.group(0).split()[0]} ******"


def _mask_kv(m):
    return f"{m.group(1)}=******"


def scrub_text(text):
    """字符串值脱敏：凭证类 PII 掩码；公司/岗位/HR 名原样（triage 拍板）。"""
    text = _BEARER_RE.sub(_mask_bearer, text)
    text = _SECRET_KV_RE.sub(_mask_kv, text)
    text = _PHONE_RE.sub(_mask_phone, text)
    text = _EMAIL_RE.sub(_mask_email, text)
    text = _ID_RE.sub(_mask_id, text)
    return text


def scrub_value(value):
    """递归脱敏：字符串值走 scrub_text；dict 的凭证名键（token/cookie 等）整值掩码；
    int/bool/None 等非字符串零风险原样。"""
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {
            k: ("******" if isinstance(k, str) and _SENSITIVE_KEY_RE.fullmatch(k.strip()) else scrub_value(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(scrub_value(v) for v in value)
    return value


def log_path(root=None, env=None):
    """sink 路径：AUTOJOB_LOG_PATH 覆盖 > private/data/app[.<profile>].log（#28 扁平命名）。
    root/env 参数供测试注入；生产默认走真实 ROOT 与 os.environ。
    profile 解析失败（多 profile 无 default 等）原样抛 SystemExit——log_event 接住丢弃。"""
    env = os.environ if env is None else env
    override = (env.get("AUTOJOB_LOG_PATH") or "").strip()
    if override:
        return Path(override)
    _name, prefix = resolve_active_profile(env=env, root=root)
    return private_path("data/app.log", prefix, root=root)


def _env_level(env):
    """阈值：AUTOJOB_LOG_LEVEL 显式定级（非法回落）> AUTOJOB_STRUCTURED_LOG=1 → INFO
    > 默认 ERROR（正常投递零额外 I/O）。"""
    raw = (env.get("AUTOJOB_LOG_LEVEL") or "").strip().upper()
    if raw in _LEVELS:
        return _LEVELS[raw]
    if (env.get("AUTOJOB_STRUCTURED_LOG") or "").strip() == "1":
        return logging.INFO
    return logging.ERROR


def _ensure_handler(logger, path):
    """按需挂 FileHandler；AUTOJOB_LOG_PATH 变更时重建（测试/调试切路径即时生效）。
    delay=True：文件只在首次真实写入时创建——默认 ERROR 阈值下零文件产生；
    父目录缺失自建（新 clone/worktree 无 private/ 时 FileHandler 会静默丢事件，
    真实冒烟 2026-09-20 踩中）。"""
    want = os.path.abspath(str(path))
    for h in list(logger.handlers):
        if getattr(h, "_autojob", False):
            if h.baseFilename == want:
                return
            logger.removeHandler(h)
            h.close()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    h = logging.FileHandler(path, encoding="utf-8", delay=True)
    h.setFormatter(logging.Formatter("%(message)s"))
    h._autojob = True
    logger.addHandler(h)


def get_logger(name):
    """评审点名的 stdlib 惯用入口：返回 autojob 层级下的子 logger（挂同一 JSONL sink，
    propagate=False 不冒泡）。阈值在取用时刻采样 env——进程内改 env 后重新调用即刷新；
    事件化路径 log_event 每次调用都刷新，仍是一般脚本的推荐入口。"""
    lg = logging.getLogger(f"autojob.{name}")
    lg.propagate = False
    lg.setLevel(_env_level(os.environ))
    _ensure_handler(lg, log_path())
    return lg


def log_event(event, level="info", **fields):
    """追加一条结构化事件（JSONL）。观测层永不影响业务：任何异常（含 profile 解析
    SystemExit）静默丢弃事件；不可序列化字段 repr 兜底。"""
    try:
        lv = str(level).upper()
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "level": lv, "event": event}
        rec.update(scrub_value(fields))
        line = json.dumps(rec, ensure_ascii=False, default=repr)
        lg = logging.getLogger(_LOGGER_NAME)
        lg.propagate = False  # 不向 root 冒泡：机读层与人读层/pytest caplog 完全隔离
        lg.setLevel(_env_level(os.environ))
        _ensure_handler(lg, log_path())
        lg.log(_LEVELS.get(lv, logging.INFO), "%s", line)
    except BaseException:  # noqa: S110  观测层故障静默——业务脚本绝不被日志拖崩
        return None


# emit 失败（磁盘满/权限等）也不打印到 stderr 污染人读层——机读层的自我要求
logging.raiseExceptions = False
