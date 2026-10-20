#!/usr/bin/env python3
"""
一键编排（#14 T1）：调研 → 知识库 → HITL 草稿审核 → 反馈 → learn。
串联 7 个阶段名（twitter_survey 多 handle 展开为多次子进程），HITL 卡点只有草稿审核；
loop 机制（效果差回 build_kb/agent 起草）属 T7，本脚本不含。
草稿由 agent 在投递会话按 apply-runbook 起草写入 greeting_drafts.json
（本地 LLM drafter 已退役，generate 阶段已删除——2026-09-10 用户约定），
卡点1 会拦住空队列。爬岗与盲投阶段已随 #44 物理清理：岗位采集与投递由
live_apply（agent 会话驱动的实时浏览投递）承担，不再有 crawl/apply 编排阶段。

用法：
  python3 scripts/orchestrate.py                      # 全链路（含 HITL 卡点）
  python3 scripts/orchestrate.py --stages promote,followup
  python3 scripts/orchestrate.py --stages promote --skip learn
  python3 scripts/orchestrate.py --list-stages
  python3 scripts/orchestrate.py --yes                # 跳过全部 HITL 卡点（CI/复跑）

阶段清单（--list-stages 查看；顺序固定，--stages 只选子集不重排）：
  repo_survey → build_kb → industry_research → twitter_survey → promote →
  followup → learn
  （promote 之前的草稿由 agent 在投递会话按 apply-runbook 起草写入
  greeting_drafts.json，status=pending_review）
  卡点1（草稿审核）：promote 之前。提示编辑 private/data/greeting_drafts.json，
    把通过的草稿 status 改为 approved，回车继续。
  投递不在编排内：promote 出队列后由 agent 会话跑 live_apply（实时浏览投递）。

config.yaml（repo 根，可选；不存在则全部用各脚本默认值跑）。支持键（映射均按各脚本
真实 CLI/env 核对过）：
  repos:             本地 repo 路径列表 → 写临时文件传 repo_survey.py --repos-file
  industry_query:    字符串 → industry_research.py --query
  twitter_handles:   handle 列表 → twitter_survey.py --handle（每 handle 一次；
                     未配置则跳过该阶段——twitter_survey 的 --handle 必填）
  其他键忽略并打印告警。注意：没有 crawl_url / apply_limit 键——爬岗与盲投
  阶段已随 #44 清理，这些键无映射对象，配置了也不会生效。

config 解析器支持的极小 YAML 子集（自写，不引第三方 yaml 库）：
  - 顶层 `key: value` 与键下的 `- 列表项` 两级；列表项缩进不敏感，不解析更深的嵌套；
  - 整行 # 注释；行内 ` #` 注释（# 前需有空白；引号内的 # 原样保留）；
  - 标量一律按字符串读（apply_limit 在使用处转 int）；值可用成对引号包住
    （引号内冒号/# 原样保留；闭合引号后只允许空白或行内注释，否则报错）；
    重复键后值覆盖前值；
  - 不支持：嵌套 mapping、多行字符串、锚点/引用、带引号的键。

执行语义：
  - 每阶段 subprocess.run([sys.executable, script, ...])，stdout/stderr 实时透传；
  - 阶段退出码非 0 → 立即中止，打印失败阶段名与排查建议，以该码退出；
  - Ctrl+C（SIGINT）→ 打印 [中止] 用户中断，以 130 退出（已完成阶段产物保留）；
  - 卡点 input() 遇 EOFError（非交互环境）→ 中止并提示需交互或用 --yes；
  - --yes 跳过全部卡点：草稿未经人工审核，风险自担（CI/复跑场景）；
  - build_kb 完成后打印人工合并提醒（不阻塞）；industry_research / twitter_survey
    的草稿合并指引由各脚本自身打印。

注入式测试：runner（subprocess 替身）/input_fn/config_loader/时钟均可注入
（见 test_orchestrate.py），测试不打真网真 LLM、不跑真子进程。
"""

import argparse
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
CONFIG_PATH = ROOT / "config.yaml"  # legacy 默认；main() 按解析重算为 config[.<name>].yaml（#28 triage）
DRAFTS_PATH = ROOT / "private/data/greeting_drafts.json"
GREETINGS_PATH = ROOT / "private/data/greetings.json"

# 阶段注册表：顺序即执行顺序（--stages 只选子集不重排）；
# 每项 = (短名, 描述, 无可运行项被跳过时的原因——空串表示不会出现空跑)
STAGES = [
    ("repo_survey", "repo 全仓调研 → private/data/repos_detail.json", ""),
    ("build_kb", "知识库草稿构建 → private/kb/draft/（人工合并另做）", ""),
    ("industry_research", "行业语境调研草稿 → private/kb/draft/web3-*.draft.md", ""),
    (
        "twitter_survey",
        "推特调研（每 handle 一次）→ private/data/twitter/",
        "需在 config.yaml 配置 twitter_handles 列表",
    ),
    ("promote", "approved 草稿晋级 → private/data/greetings.json（卡点1 在此之前）", ""),
    ("followup", "效果追踪 → private/data/followup_report.json", ""),
    ("learn", "followup 反馈回写 growing memory", ""),
]
STAGE_DESC = {name: desc for name, desc, _ in STAGES}
STAGE_SKIP_REASON = {name: reason for name, _desc, reason in STAGES if reason}
SUPPORTED_CONFIG_KEYS = ("repos", "industry_query", "twitter_handles")

# 阶段失败后的人工排查建议（打印在 [建议] 行）
STAGE_HINTS = {
    "repo_survey": "检查 repos 路径 / private/data/repos_list.txt 是否存在、LLM 服务（GREETING_LLM_* 配置）是否可用",
    "build_kb": "确认 private/data/repos_detail.json 存在（先跑 repo_survey）、LLM 服务可用",
    "industry_research": "多为网络/反爬或 LLM 失败，可直接重跑；持续失败检查 GREETING_LLM_* 配置",
    "twitter_survey": "多为 nitter 实例失效或反爬，可直接重跑；核对 handle 是否正确",
    "promote": "greeting_drafts.json 里可能没有 status=approved 的草稿"
    "（草稿由 agent 在投递会话按 apply-runbook 起草，再人工审核）",
    "followup": "多为登录态过期：重新登录后重跑",
    "learn": "确认 private/data/followup_report.json 存在（先跑 followup）",
}


# --- config：极小 YAML 子集解析 ---


def _scalar(value, lineno):
    """标量值处理：成对引号 → 去引号（内部冒号/# 原样）；否则剥行内 ` #` 注释。"""
    if value[:1] in ('"', "'"):
        quote = value[0]
        end = value.find(quote, 1)
        if end == -1:
            raise ValueError(f"config 第 {lineno} 行: 引号未闭合: {value!r}")
        rest = value[end + 1 :]
        if rest.strip() and not (rest[:1].isspace() and rest.lstrip().startswith("#")):
            # 闭合引号后只允许空白或“空白 + # 注释”（# 前需有空白，与行内注释规则一致）
            raise ValueError(f"config 第 {lineno} 行: 引号闭合后仍有无法解析的内容: {value!r}")
        return value[1:end]
    idx = value.find(" #")
    if idx != -1:
        value = value[:idx]
    return value.strip()


def parse_mini_yaml(text):
    """极小 YAML 子集 → dict（标量为 str，列表为 list[str]）；解析不了抛 ValueError。
    只支持顶层 `key: value` + 键下 `- 列表项` 两级，子集与限制见模块 docstring。"""
    data, current = {}, None
    for lineno, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            if current is None or not isinstance(data.get(current), list):
                raise ValueError(
                    f"config 第 {lineno} 行: 列表项缺少所属键（应为 'key:' 空值下的 '- 项'）: {stripped!r}"
                )
            data[current].append(_scalar(stripped[2:], lineno))
            continue
        if ":" not in stripped:
            raise ValueError(f"config 第 {lineno} 行: 无法解析（缺少冒号）: {stripped!r}")
        key, _, rest = stripped.partition(":")
        key = key.strip()
        if not key or " " in key:
            raise ValueError(f"config 第 {lineno} 行: 非法键 {key!r}（键不含空格）")
        rest = rest.strip()
        if rest.startswith("#"):
            rest = ""  # 冒号后直接跟注释，值留空（后接列表项）
        if rest:
            data[key] = _scalar(rest, lineno)
            current = None
        else:
            if key in data and not isinstance(data[key], list):
                raise ValueError(f"config 第 {lineno} 行: 键 {key!r} 重复且类型冲突（原为标量）")
            data[key] = []
            current = key
    return data


def load_config(path=CONFIG_PATH):
    """读 repo 根 config.yaml（可选文件，不存在返回 {}）。"""
    p = Path(path)
    if not p.exists():
        return {}
    return parse_mini_yaml(p.read_text(encoding="utf-8"))


def normalize_config(cfg):
    """校验/转换 config 值：未知键告警忽略；类型错误抛 ValueError（编排以 2 退出）。"""
    unknown = [k for k in cfg if k not in SUPPORTED_CONFIG_KEYS]
    for k in unknown:
        print(f"[警告] config.yaml 键 {k!r} 不受支持，已忽略（支持: {', '.join(SUPPORTED_CONFIG_KEYS)}）")
    out = {}
    repos = cfg.get("repos")
    if repos is not None:
        if not isinstance(repos, list):
            raise ValueError("config.yaml 的 repos 应为列表（'- ' 列表项，每行一个本地路径）")
        out["repos"] = [str(p) for p in repos]
    handles = cfg.get("twitter_handles")
    if handles is not None:
        if not isinstance(handles, list):
            raise ValueError("config.yaml 的 twitter_handles 应为列表（'- ' 列表项，每行一个 handle）")
        out["twitter_handles"] = [str(h) for h in handles]
    query = cfg.get("industry_query")
    if query is not None:
        if not isinstance(query, str):
            raise ValueError("config.yaml 的 industry_query 应为字符串")
        out["industry_query"] = query
    limit = cfg.get("apply_limit")
    if limit is not None:
        try:
            n = int(str(limit).strip())
        except (TypeError, ValueError):
            n = -1
        if n <= 0:
            raise ValueError(f"config.yaml 的 apply_limit 应为正整数，收到: {limit!r}")
        out["apply_limit"] = n
    return out


# --- 阶段计划 ---


def _argv(script):
    return [sys.executable, str(SCRIPTS / script)]


def write_repos_file(repos):
    """repos 路径列表 → 临时文本文件（每行一个路径，repo_survey.py --repos-file 格式）。"""
    fd, path = tempfile.mkstemp(prefix="orchestrate_repos_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(repos) + "\n")
    return path


def build_plan(selected, cfg):
    """选中阶段 → 计划：每阶段 {name, desc, runs: [(argv, env)], cleanup: [临时文件]}。"""
    plan = []
    for name in selected:
        runs, cleanup = [], []
        if name == "repo_survey":
            argv = _argv("repo_survey.py")
            if cfg.get("repos"):
                path = write_repos_file(cfg["repos"])
                cleanup.append(path)
                argv += ["--repos-file", path]
            runs.append((argv, None))
        elif name == "build_kb":
            runs.append((_argv("build_kb.py"), None))
        elif name == "industry_research":
            argv = _argv("industry_research.py")
            if cfg.get("industry_query"):
                argv += ["--query", cfg["industry_query"]]
            runs.append((argv, None))
        elif name == "twitter_survey":
            for handle in cfg.get("twitter_handles", []):
                runs.append((_argv("twitter_survey.py") + ["--handle", handle], None))
        elif name == "promote":
            runs.append((_argv("gen_greetings.py") + ["promote"], None))
        elif name == "followup":
            runs.append((_argv("followup_tracker.py"), None))
        elif name == "learn":
            runs.append((_argv("gen_greetings.py") + ["learn"], None))
        plan.append(
            {
                "name": name,
                "desc": STAGE_DESC[name],
                "runs": runs,
                "cleanup": cleanup,
                "skip_reason": STAGE_SKIP_REASON.get(name, ""),
            }
        )
    return plan


def parse_stage_selection(stages, skip):
    """--stages/--skip（逗号分隔子集）→ 选中的阶段名列表（按内置顺序）。
    原始串非空但解析不出阶段名（如全空白/逗号）/ 未知阶段名 / 过滤后为空抛 ValueError。"""

    def parse(s):
        return [t.strip() for t in s.split(",") if t.strip()]

    chosen = parse(stages)
    skipped_list = parse(skip)
    if stages and not chosen:
        raise ValueError(f"--stages={stages!r} 只有空白/逗号，未解析出任何阶段名（全跑请省略该参数，不静默回退全跑）")
    if skip and not skipped_list:
        raise ValueError(f"--skip={skip!r} 只有空白/逗号，未解析出任何阶段名（不排除任何阶段请省略该参数）")
    if not chosen:
        chosen = [name for name, _, _ in STAGES]
    skipped = set(skipped_list)
    unknown = [n for n in chosen + skipped_list if n not in STAGE_DESC]
    if unknown:
        raise ValueError(f"未知阶段名: {', '.join(unknown)}（用 --list-stages 查看全部阶段）")
    selected = [name for name, _, _ in STAGES if name in chosen and name not in skipped]
    if not selected:
        raise ValueError("没有要运行的阶段（--stages/--skip 过滤后为空）")
    return selected


# --- HITL 卡点 ---


def _eof_abort():
    print("\n[中止] 输入流结束（EOF，非交互环境）。本编排含 HITL 卡点，需交互运行；")
    print("CI/复跑场景请加 --yes 跳过卡点（草稿未经人工审核，风险自担）。")


def review_checkpoint(ask, _greetings_path=None):
    """卡点1（草稿审核）：回车继续；EOF → False（中止）。"""
    print("\n" + "─" * 62)
    print(f"[HITL 卡点1] 草稿审核：编辑 {DRAFTS_PATH}，")
    print("把通过审核的草稿 status 改为 approved（不通过的改 rejected），promote 只晋级 approved。")
    try:
        ask("审核完成按回车继续...")
    except EOFError:
        _eof_abort()
        return False
    return True


CHECKPOINTS = {"promote": review_checkpoint}


# --- 执行 ---


def default_runner(argv, env=None):
    """默认 runner：subprocess.run，stdout/stderr 实时透传（不捕获）。"""
    sys.stdout.flush()  # 先冲掉父进程缓冲，防 CI/管道下父输出与子进程输出乱序
    return subprocess.run(argv, env=env).returncode


def _banner(i, total, stage, t0):
    print("\n" + "=" * 62)
    print(f"[{i}/{total}] 阶段 {stage['name']} — {stage['desc']}")
    print(f"开始 {t0:%Y-%m-%d %H:%M:%S}")
    print("=" * 62)


def _summary(durations):
    print("\n" + "=" * 62)
    print("[编排完成] 各阶段耗时:")
    total = 0.0
    for name, d in durations:
        print(f"  {name:<20s} {d:7.1f}s")
        total += d
    print(f"  {'总计':<20s} {total:7.1f}s")


def orchestrate(
    stages="",
    skip="",
    yes=False,
    config_loader=None,
    runner=None,
    input_fn=None,
    now_fn=None,
    monotonic_fn=None,
    greetings_path=GREETINGS_PATH,
):
    """编排主流程。可注入：config_loader()→dict、runner(argv, env)→退出码、
    input_fn(prompt)→str、now_fn（横幅墙钟时间戳）→datetime、
    monotonic_fn（耗时测量单调钟）→float。返回编排退出码（0 成功；2 配置/阶段名错误；
    1 HITL 未通过/EOF；130 用户 Ctrl+C 中断；其他 = 首个失败阶段的退出码）。"""
    loader = config_loader or load_config
    run_cmd = runner or default_runner
    ask = input_fn or input
    wall = now_fn or datetime.now
    mono = monotonic_fn or time.monotonic
    try:
        cfg = normalize_config(loader())
        selected = parse_stage_selection(stages, skip)
    except ValueError as e:
        print(f"[错误] {e}")
        return 2
    plan = build_plan(selected, cfg)
    print(f"[编排] 共 {len(selected)} 个阶段: {' → '.join(selected)}")
    if yes:
        print("[--yes] 跳过全部 HITL 卡点（草稿未审核、投递未经人工确认，风险自担）")
    durations = []
    temp_files = [p for st in plan for p in st["cleanup"]]
    try:
        for i, stage in enumerate(plan, 1):
            if not stage["runs"]:
                reason = stage["skip_reason"]
                extra = f"（{reason}）" if reason else ""
                print(f"\n[跳过] 阶段 {stage['name']}：无可运行项{extra}")
                continue
            if not yes and stage["name"] in CHECKPOINTS:
                if not CHECKPOINTS[stage["name"]](ask, greetings_path):
                    print("[中止] HITL 卡点未通过，编排结束（已完成阶段的产物保留，后续阶段未运行）")
                    return 1
            _banner(i, len(plan), stage, wall())
            t0 = mono()
            for argv, env in stage["runs"]:
                print(f"[运行] {' '.join(argv)}")
                code = run_cmd(argv, env)
                if code != 0:
                    print("\n" + "!" * 62)
                    print(f"[编排中止] 阶段 {stage['name']} 失败（退出码 {code}）")
                    print(f"[建议] {stage['name']}: {STAGE_HINTS.get(stage['name'], '人工查看上方该阶段输出排查')}")
                    return code
            dur = mono() - t0
            durations.append((stage["name"], dur))
            print(f"\n[阶段完成] {stage['name']} 耗时 {dur:.1f}s")
            if stage["name"] == "build_kb":
                print(
                    "[提醒] 知识库草稿（private/kb/draft/）需人工审核合并到正式 KB"
                    "（private/kb/repos_detail.md、private/profile_background.md），"
                    "agent 起草才会用到；本提醒不阻塞编排"
                )
    except KeyboardInterrupt:
        print("\n[中止] 用户中断")
        return 130
    finally:
        for p in temp_files:
            Path(p).unlink(missing_ok=True)
    _summary(durations)
    return 0


def print_stage_list():
    print("可编排阶段（--stages/--skip 用英文短名，逗号分隔；顺序固定）:")
    for name, desc, _ in STAGES:
        print(f"  {name:<20s} {desc}")
    print(
        "\nHITL 卡点: promote 之前（卡点1 草稿审核）；"
        "--yes 跳过卡点（草稿未经人工审核，风险自担）。"
        "投递不在编排内：由 agent 会话跑 live_apply。"
    )
    print(
        f"config.yaml（可选，repo 根）支持键: {', '.join(SUPPORTED_CONFIG_KEYS)}"
        "（无 crawl_url/apply_limit：爬岗与盲投阶段已随 #44 清理）"
    )


def main(argv=None):
    from _profile_paths import private_path as _pp, profile_line as _pl, resolve_active_profile as _rap

    _name, _prefix = _rap()
    print(_pl(_name, _prefix))
    config_path = ROOT / (f"config.{_name}.yaml" if _prefix else "config.yaml")
    if config_path.exists() or _prefix:
        globals()["CONFIG_PATH"] = config_path
    globals()["DRAFTS_PATH"] = _pp("data/greeting_drafts.json", _prefix)
    globals()["GREETINGS_PATH"] = _pp("data/greetings.json", _prefix)
    parser = argparse.ArgumentParser(
        description="一键编排求职链路 + HITL 卡点暂停（#14 T1；loop 机制属 T7 不在本脚本）"
    )
    parser.add_argument(
        "--stages",
        default="",
        help="逗号分隔的阶段子集（如 crawl,promote,apply），默认全跑；执行顺序按内置阶段序，不按参数顺序",
    )
    parser.add_argument("--skip", default="", help="逗号分隔的排除阶段（与 --stages 同语法），从选择中剔除")
    parser.add_argument(
        "--yes", action="store_true", help="跳过全部 HITL 卡点（CI/复跑场景）；草稿未经人工审核，风险自担"
    )
    parser.add_argument("--list-stages", action="store_true", help="打印阶段清单与卡点说明后退出")
    args = parser.parse_args(argv)
    if args.list_stages:
        print_stage_list()
        return 0
    return orchestrate(stages=args.stages, skip=args.skip, yes=args.yes)


if __name__ == "__main__":
    sys.exit(main())
