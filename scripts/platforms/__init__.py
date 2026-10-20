"""平台抽象层（issue #2）：PlatformBase 接口 + 平台工厂 + 配置选择。

平台选择优先级：环境变量 PLATFORM > private/platform.json 的 {"platform": ...} > 默认 chat-first。
新平台实现 PlatformBase 三个方法后，在 get_platform 的注册处加一行即可。
"""

import abc
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))  # noqa: E402  平台包独立入口也挂 scripts
from _profile_paths import private_path, resolve_active_profile  # noqa: E402  #28

CONFIG = ROOT / "private/platform.json"  # legacy 默认；运行时按 profile 前缀重算


def platform_config_path(prefix=""):
    """platform.json 的 profile 化路径（#28：平台/账号配置跟人走）。"""
    return private_path("platform.json", prefix)


class PlatformBase(abc.ABC):
    """单平台的三类能力：爬 JD、投递、回复追踪。所有方法接收已登录的 Playwright page。"""

    name = "base"
    login_dialog_selector = ""

    @abc.abstractmethod
    def crawl_jds(self, page, queries, target, exclude_ids):
        """搜索并爬取岗位列表+详情，排除 exclude_ids，返回 raw JD 列表。"""

    @abc.abstractmethod
    def apply(self, page, job, send_text, opts=None):
        """对单个岗位执行投递（进详情页→阅读浏览→沟通→逐字填招呼语→发送）。
        opts：拟人参数（dwell/type_delay/typo_prob，见 _limits.load_config）。
        返回 {"status": ...}：captured（含 fa 响应）/ login_expired / skipped / no_btn /
        no_input / no_send / no_capture / page_error（页面异常，证据已存档）/
        risk_blocked（验证码/安全验证，熔断停批，证据已存档）。"""

    @abc.abstractmethod
    def followup(self, page, applied, beat_fn=None):
        """遍历消息页对话，返回 followup 报告 dict。
        beat_fn：#56 A3 的节拍注入器（`followup:goto-chat:*` / `followup:wait-friends:{n}`），
        由调用方注入并自行吞异常；默认 None = 零行为变更——平台层不知道心跳为何物。"""

    def save_evidence(self, page, tag, job=None):
        """页面异常现场存档（截图/HTML）；不支持的平台默认空实现。"""
        return None


def get_platform_name(env=None, config_path=None):
    env = os.environ if env is None else env
    name = env.get("PLATFORM", "")
    if name:
        return name.strip().lower()
    if config_path:
        cfg_path = Path(config_path)
    else:
        _n, prefix = resolve_active_profile(env=env)
        cfg_path = platform_config_path(prefix)
    if cfg_path.exists():
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        name = data.get("platform", "")
        if name:
            return name.strip().lower()
    return "chat-first"


def get_platform(name=None, env=None, config_path=None):
    key = (name or get_platform_name(env, config_path)).lower()
    if key == "chat-first":
        from .chat_first import ChatFirstPlatform

        return ChatFirstPlatform()
    if key == "lagou":
        from .lagou import LagouPlatform

        return LagouPlatform()
    if key == "liepin":
        from .liepin import LiepinPlatform

        return LiepinPlatform()
    raise ValueError(f"未知平台: {key!r}（支持: chat-first/lagou/liepin）")
