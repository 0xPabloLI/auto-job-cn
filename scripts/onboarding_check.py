#!/usr/bin/env python3
"""Onboarding 前置盘点的机械核验（onboarding.md 前置盘点表的代码化，2026-09-10）。

用户约定：onboarding 要有代码确定性——agent 每步完成后重跑本脚本，完成与否以
退出码为准，不靠 agent 自评；用户也可以自己直接跑。doctor 项复用 gen_greetings
的同一判定（extract_whitelist / doctor_report），保证与起草 gate 语义一致。

用法：python3 scripts/onboarding_check.py [--root <repo根>]（默认本仓库根）。
只读检查：不写任何文件。退出码：0 = 盘点全过（onboarding 就绪）；1 = 有未达标项。
"""

import argparse
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS))
import gen_greetings as g  # noqa: E402  复用白名单/doctor 判定，与 gate 同一语义
import _profile_paths as pp  # noqa: E402  #28 注册表/简历/profile 解析


def _read(p):
    return Path(p).read_text(encoding="utf-8")


def check_config(root):
    """config.yaml 可选（live-only 流程不依赖它；材料全为非 repo 形式时可缺）；
    配置了则 repos 必须指向真实本地路径（极小 YAML 子集：顶层 repos: + '- 路径'）。"""
    p = root / "config.yaml"
    if not p.exists():
        return True, "config.yaml 未配置（可选——材料全为非 repo 形式时不需要；需要时从 config.example.yaml 复制）"
    repos, in_repos = [], False
    for line in _read(p).splitlines():
        s = line.split("#", 1)[0].rstrip()
        if not s.strip():
            continue
        if not s.startswith((" ", "\t", "-")) and s.endswith(":"):
            in_repos = s[:-1].strip() == "repos"
            continue
        if in_repos and s.strip().startswith("- "):
            repos.append(s.strip()[2:].strip().strip("\"'"))
    if not repos:
        return False, (
            "config.yaml 没有 repos 条目（材料收集步应写入本地 repo 路径；"
            "不需要 repo 材料时可直接删除 config.yaml，该项转为可选通过）"
        )
    missing = [r for r in repos if not (Path(r).expanduser().is_dir() or (root / r).is_dir())]
    if missing:
        return False, f"repos 路径不存在: {missing}"
    return True, f"repos {len(repos)} 条路径全部存在"


def check_resume(root, name=None, prefix=""):
    """简历原文落盘（#28）：注册表声明的简历路径优先（任意命名/任意位置，存在且非空）；
    旧 private/resume.* glob 保留为兼容兜底。"""
    if not name:
        try:
            name, prefix = pp.resolve_active_profile(root=root)
        except SystemExit as e:
            return False, str(e)
    if name:
        declared = pp.resume_path(name, root=root)
        if declared:
            if declared.is_file() and declared.stat().st_size > 0:
                return True, f"简历已落盘（注册表声明）: {declared}"
            return False, (f"注册表声明的简历不存在或为空: {declared}——核对 private/profiles.json 或放入文件")
    priv = root / "private"
    hits = [p for p in priv.glob("resume.*") if p.is_file() and p.stat().st_size > 0] if priv.exists() else []
    if not hits:
        return False, (
            "private/ 下没有简历文件（注册表 private/profiles.json 声明路径，"
            "或旧约定 resume.<任意格式>；用户提供、agent 不代写）"
        )
    return True, f"简历已落盘: {hits[0].name}"


def check_profile_background(root, name=None, prefix=""):
    """个人层（#28：按解析前缀取 ai.profile_background.md 等）：存在且无模板占位。"""
    p = pp.private_path("profile_background.md", prefix, root=root)
    if not p.exists():
        label = f"（profile={name}）" if name else ""
        return False, (
            f"private/{'ai.' if prefix else ''}profile_background.md 不存在{label}"
            "（个人层未建——不能沿用其他 profile 的个人层）"
        )
    if "填你自己的" in _read(p):
        return False, f"{p.name} 仍含「填你自己的」模板占位"
    return True, f"个人层已填（无占位，{p.name}）"


def check_profile(root, name=None, prefix=""):
    """≥1 个 profile JSON：industry 指向存在的行业层；queries 必填（#29 P0——搜索词
    门禁，缺失时投递会静默按硬编码词跑，onboarding 全绿却投错工种）。报缺附可粘贴
    的修复片段，新人无需查文档。"""
    pdir = root / "private/kb/profiles"
    profiles = sorted(pdir.glob("*.json")) if pdir.exists() else []
    if not profiles:
        return False, "private/kb/profiles/ 下没有 profile JSON（行业归类步未完成）"
    if name and f"{name}.json" not in {x.name for x in profiles}:
        return False, (
            f"private/kb/profiles/{name}.json 不存在（profile={name} 的行业归类步"
            "未完成——不能借用其他 profile 的行业层）"
        )
    bad = []
    for p in profiles:
        try:
            data = json.loads(_read(p))
        except json.JSONDecodeError:
            bad.append(f"{p.name}(坏 JSON)")
            continue
        data = data or {}
        industry = data.get("industry")
        if not industry or not (root / "private/kb/industries" / f"{industry}.md").exists():
            bad.append(f"{p.name}(industry={industry!r} 无对应行业层)")
        queries = data.get("queries")
        if not (isinstance(queries, list) and queries and all(isinstance(q, str) and q.strip() for q in queries)):
            bad.append(
                f"{p.name}(queries 缺失或非法——搜索词门禁 #29。修复：在 {p.name} 加 "
                f'"queries": ["岗位关键词1", "岗位关键词2"]，可选 "query_weights": {{"岗位关键词1": 2}})'
            )
    if bad:
        return False, "; ".join(bad)
    return True, f"{len(profiles)} 个 profile 全部通过（行业层 + queries 搜索词门禁）"


def check_fact_cards(root, name=None, prefix=""):
    """事实卡片 + 白名单 JSON 块可解析（起草 gate 的唯一事实来源；#28 按解析前缀取）。"""
    p = pp.private_path("kb/fact_cards.md", prefix, root=root)
    if not p.exists():
        label = f"（profile={name}）" if name else ""
        return False, (
            f"private/kb/{p.name} 不存在{label}（事实卡片步未完成——白名单必须 per-profile，不能共用其他 profile 的）"
        )
    if g.extract_whitelist(_read(p)) is None:
        return False, f"{p.name} 无白名单区或 JSON 块损坏——跑 gen_greetings.py doctor --sync 编译建表"
    return True, f"卡片与白名单 JSON 可解析（{p.name}）"


def check_excluded_companies(root, name=None, prefix=""):
    """排除公司清单存在且为 JSON 数组（用户没有可排除公司时是空数组；#28 按解析前缀取）。"""
    p = pp.private_path("data/excluded_companies.json", prefix, root=root)
    if not p.exists():
        return False, (f"private/data/{p.name} 不存在（投递边界「命中不投」依赖它；没有可排除公司就建空数组 []）")
    try:
        data = json.loads(_read(p))
    except json.JSONDecodeError:
        return False, "excluded_companies.json 不是合法 JSON"
    if not isinstance(data, list):
        return False, "excluded_companies.json 应为关键词数组"
    return True, f"排除公司清单就绪（{len(data)} 条关键词）"


def resolve_profile(root):
    """doctor 核对用 profile：#29 P1 默认值收敛——委托 _profile_paths.resolve_kb_profile
    （注册表 > AUTOJOB_PROFILE > GREETING_PROFILE > 唯一 kb/profiles JSON > blockchain）。"""
    try:
        return pp.resolve_kb_profile(root=root)
    except SystemExit:
        return None  # 多 profile 未指定：不代猜（fail-loud 语义交 check_doctor 报缺）


def check_doctor(root, name=None, prefix=""):
    """doctor 同步核验：个人层 + 行业层硬事实 vs 白名单无缺口（与 gen_greetings doctor 同判定；
    #28 全部按解析前缀取，profile 名用解析结果）。"""
    bg = pp.private_path("profile_background.md", prefix, root=root)
    cards = pp.private_path("kb/fact_cards.md", prefix, root=root)
    if not bg.exists() or not cards.exists():
        return False, "个人层或事实卡片缺失，无法核对"
    whitelist = g.extract_whitelist(_read(cards))
    if whitelist is None:
        return False, "白名单不可解析，先跑 gen_greetings.py doctor --sync"
    profile = name or resolve_profile(root)
    if profile is None:
        return False, ("多个 profile 但未指定——在 private/profiles.json 设 default 或设 AUTOJOB_PROFILE 后重跑（#28）")
    industry = g.load_industry_text(
        profile, profiles_dir=root / "private/kb/profiles", industries_dir=root / "private/kb/industries"
    )
    gaps = g.doctor_report(_read(bg) + ("\n\n" + industry if industry else ""), whitelist)
    if gaps:
        return False, (f"硬事实有 {len(gaps)} 条未入白名单（如 {gaps[0]}）——补卡片后 gen_greetings.py doctor --sync")
    return True, f"知识库（profile={profile}）硬事实与白名单同步，无缺口"


CHECKS = [
    ("config repos", check_config),
    ("简历原文", check_resume),
    ("个人层 profile_background", check_profile_background),
    ("profile/行业层", check_profile),
    ("事实卡片白名单", check_fact_cards),
    ("排除公司清单", check_excluded_companies),
    ("doctor 同步", check_doctor),
]


def run_checks(root, name=None, prefix=None):
    """逐项核验，返回 (名称, 是否通过, 详情) 列表——测试直接消费。
    #28：先解析当前 profile（env/注册表），各检查项按其前缀取文件——
    其他 profile 的个人层/白名单/台账不能替本 profile 混过盘点。"""
    if prefix is None:
        try:
            name, prefix = pp.resolve_active_profile(root=root)
        except SystemExit as e:
            return [("profile 解析", False, str(e))]
    return [
        (cname, *fn(root)) if cname == "config repos" else (cname, *fn(root, name=name, prefix=prefix))
        for cname, fn in CHECKS
    ]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Onboarding 前置盘点机械核验（只读）")
    ap.add_argument("--root", default=str(_SCRIPTS.parent), help="仓库根目录（默认本仓库）")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        _name, _prefix = pp.resolve_active_profile(root=root)
    except SystemExit as e:
        print(f"== onboarding 盘点（root={root}）==")
        print(f"  [缺] profile 解析 —— {e}")
        print("未就绪：1 项未达标（对应 onboarding.md 步骤；对 agent 说「初始化」继续）")
        return 1
    print(f"== onboarding 盘点（root={root}，{pp.profile_line(_name, _prefix)}）==")
    failed = 0
    for name, ok, detail in run_checks(root, name=_name, prefix=_prefix):
        failed += not ok
        print(f"  {'[ok]' if ok else '[缺]'} {name} —— {detail}")
    if failed:
        print(f"未就绪：{failed} 项未达标（对应 onboarding.md 步骤；对 agent 说「初始化」继续）")
        return 1
    print("onboarding 就绪：盘点全过，可投递")
    return 0


if __name__ == "__main__":
    sys.exit(main())
