#!/usr/bin/env python3
"""Profile 路径解析基础设施（#28 单仓多 Profile）——私有路径的唯一出口。

约束（#28 正文 + 2026-09-12 审查建议）：
- 扁平命名：默认（在位）profile 沿用现有文件名＝零迁移，其余 profile 文件名加
  「<name>.」前缀；浏览器登录态是目录维度：.camoufox_profile / .camoufox_profile.<name>
  （Camoufox user_data_dir 只收目录）。
- 解析优先级：显式参数（--profile）> AUTOJOB_PROFILE env > 注册表 default > 唯一
  profile 自动采用；多 profile 且无 default → SystemExit（fail-loud，绝不静默选错）。
- 全函数化：路径在调用时求值，不做 import 期静态绑定——运行时 env / CLI 参数即时生效，
  跨模块 import 静态路径常量的模式废除。
- private/profiles.json 注册表只承载「有哪些 profile + default + 简历路径」；
  industry / queries 仍由 private/kb/profiles/<name>.json 持有（#28 triage，不两处维护）。
"""

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BROWSER_DIR_NAME = ".camoufox_profile"


def load_registry(root=None):
    """private/profiles.json → {"default": str|None, "profiles": {name: {...}}}。
    文件缺失 → 空注册表（legacy 单 profile，现有 private/ 零迁移）；
    坏 JSON / 形态非法 → SystemExit（fail-loud，验收 1 的可读报错）。"""
    r = Path(root) if root else ROOT
    p = r / "private" / "profiles.json"
    if not p.exists():
        return {"default": None, "profiles": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as e:
        raise SystemExit(f"[blocked] private/profiles.json 不是合法 JSON（{e}）——修正后再跑（#28）")
    if not isinstance(data, dict):
        raise SystemExit("[blocked] private/profiles.json 顶层须是对象（#28）")
    profiles = data.get("profiles") or {}
    if not isinstance(profiles, dict):
        raise SystemExit('[blocked] private/profiles.json 的 "profiles" 须是对象（#28）')
    default = data.get("default")
    if default is not None and not isinstance(default, str):
        raise SystemExit('[blocked] private/profiles.json 的 "default" 须是字符串或 null（#28）')
    return {"default": default, "profiles": profiles}


def _incumbent(reg):
    """在位 profile（沿用现有文件名的那一个）：注册表 default，否则唯一 profile。"""
    if reg["default"]:
        return reg["default"]
    names = sorted(reg["profiles"])
    return names[0] if len(names) == 1 else None


def resolve_active_profile(explicit=None, env=None, root=None, registry=None):
    """解析当前 profile → (name, prefix)。name 为注册表 profile 名；legacy 单 profile
    /空注册表时 None。prefix 为文件名前缀：在位 profile 为 ""（零迁移），其余 "<name>."。
    优先级：explicit（CLI --profile）> AUTOJOB_PROFILE env > 注册表 default > 唯一
    profile；多 profile 无 default、env/参数指名未注册 profile → SystemExit。"""
    reg = registry or load_registry(root)
    profiles = reg["profiles"]
    names = sorted(profiles)
    env = os.environ if env is None else env
    chosen = None
    named = (explicit or (env or {}).get("AUTOJOB_PROFILE") or "").strip()
    if named:
        if not profiles:
            raise SystemExit(
                f"[blocked] AUTOJOB_PROFILE={named} 但 private/profiles.json "
                "未注册任何 profile——先对 agent 说「初始化」注册（#28）"
            )
        if named not in profiles:
            raise SystemExit(
                f"[blocked] profile「{named}」不在注册表（{', '.join(names)}）——不代猜，确认名字或先注册（#28）"
            )
        chosen = named
    elif reg["default"]:
        if reg["default"] not in profiles:
            raise SystemExit(
                f"[blocked] 注册表 default={reg['default']!r} 不在 profiles 里——修正 private/profiles.json（#28）"
            )
        chosen = reg["default"]
    elif len(names) == 1:
        chosen = names[0]
    elif len(names) > 1:
        raise SystemExit(
            f"[blocked] 多个 profile（{', '.join(names)}）且未设 default——"
            '在 private/profiles.json 设 "default" 或用 AUTOJOB_PROFILE 显式'
            "指定，不静默选错（#28 fail-loud）"
        )
    incumbent = _incumbent(reg)
    prefix = "" if chosen is None or chosen == incumbent else f"{chosen}."
    return chosen, prefix


def private_path(relative, prefix="", root=None):
    """private/<relative> 的 profile 化路径（扁平命名 #28）：前缀只加在文件名段——
    data/greetings.json + prefix "ai." → private/data/ai.greetings.json。"""
    r = Path(root) if root else ROOT
    rel = Path(relative)
    return r / "private" / rel.parent / f"{prefix}{rel.name}"


def browser_profile_dir(name=None, root=None, registry=None):
    """浏览器登录态目录：在位 profile 用原名 .camoufox_profile（零迁移），
    其余 .camoufox_profile.<name>（Camoufox 的 user_data_dir 只收目录，#28）。"""
    r = Path(root) if root else ROOT
    if not name:
        return r / "private" / BROWSER_DIR_NAME
    reg = registry or load_registry(root)
    if name == _incumbent(reg):
        return r / "private" / BROWSER_DIR_NAME
    return r / "private" / f"{BROWSER_DIR_NAME}.{name}"


def resume_path(name, root=None, registry=None):
    """注册表为 profile 声明的简历路径（#28：任意命名/任意位置，注册表记一行）。
    相对路径按仓库根解析；未声明返回 None。"""
    reg = registry or load_registry(root)
    entry = reg["profiles"].get(name or "") or {}
    raw = entry.get("resume")
    if not raw:
        return None
    p = Path(str(raw)).expanduser()
    if p.is_absolute():
        return p
    r = Path(root) if root else ROOT
    return r / p


def resolve_kb_profile(env=None, root=None):
    """kb/profiles/<name>.json 的名字解析——#29 P1 的默认值收敛点（全仓唯一一处
    "blockchain" 字面量默认）。链路：① 注册表非空时走 resolve_active_profile
    （AUTOJOB_PROFILE > default > 唯一，多 profile 无 default 直接 fail-loud——不会
    静默落到 ②）；② 注册表缺席/为空时 legacy env（AUTOJOB_PROFILE > GREETING_PROFILE，
    兼容保留）> kb/profiles 下唯一 JSON > 无任何 profile 时 "blockchain"。
    注册表缺席且 kb/profiles 有多个 JSON 且无 env 指定 → SystemExit（不静默挑）。"""
    env = os.environ if env is None else env
    reg = load_registry(root)
    if reg["profiles"]:  # 注册表为空（legacy）时不走它——AUTOJOB_PROFILE 由下方 legacy 链消费
        name, _prefix = resolve_active_profile(env=env, root=root, registry=reg)
        if name:
            return name
    for key in ("AUTOJOB_PROFILE", "GREETING_PROFILE"):
        v = (env.get(key) or "").strip()
        if v:
            return v
    r = Path(root) if root else ROOT
    pdir = r / "private" / "kb" / "profiles"
    names = sorted(p.stem for p in pdir.glob("*.json")) if pdir.exists() else []
    if len(names) == 1:
        return names[0]
    if not names:
        return "blockchain"
    raise SystemExit(
        f"[blocked] kb/profiles 有多个 profile（{', '.join(names)}）但未指定"
        "——设 AUTOJOB_PROFILE / GREETING_PROFILE 或注册 profiles.json（#28/#29）"
    )


def profile_line(name, prefix=""):
    """启动解析结果统一打印（#28 验收 7）。"""
    base = f"[profile] {name or '默认（未注册，沿用现有文件名）'}"
    return f"{base}（文件前缀 {prefix}）" if prefix else base
