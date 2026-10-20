#!/usr/bin/env python3
"""
repo 全仓调研脚本化（#14 T2）：遍历本地已 clone 的仓库 → LLM 逐仓总结 → repos_detail.json（全仓调研档案）。

用法：
  python3 scripts/repo_survey.py [--repos A,B,C | --repos-file FILE] [--output FILE]
                                 [--max-files N] [--dry-run] [--force]
  不带 --repos/--repos-file 时默认读 private/data/repos_list.txt
  （每行一个本地路径，# 开头为注释，空行忽略；文件不存在时报错退出）。
  --repos 优先于 --repos-file。

输出：JSON 数组写入 private/data/repos_detail.json（--output 可覆盖路径）。
每仓一条档案，字段：repo / path / positioning / tech_stack / architecture_summary /
highlights / red_flags / files_sampled / surveyed_at；单仓失败（超时/坏 JSON）记录
error 字段后继续下一仓。已有输出文件时默认按 repo 名 upsert（同名单仓替换、新仓
追加；新条目带 error 而旧档案无 error 时保留旧档案不替换，失败不覆盖好档案），
--force 丢弃已有内容只写本批。已有输出文件在调 LLM 前先读取，损坏 JSON 立即报错退出。

LLM：OpenAI 兼容 /v1/chat/completions。默认本地 Ollama（qwen3:8b），
     GREETING_LLM_BASE_URL / GREETING_LLM_MODEL / GREETING_LLM_API_KEY 可换任意兼容服务。

采样规则：跳过 .git/node_modules/__pycache__/dist/build 目录、二进制扩展名、
>200KB 单文件；每仓最多 --max-files（默认 60）个文件，README/包清单/CI 配置等
优先入选，前若干个关键文件带片段进 prompt（片段总量超限截断）。
--dry-run 只打印将采样的仓库与文件数，不调 LLM、不写文件。

消费方：build_kb.py 读本档案生成知识库分层文档（对应手写档案 private/kb/repos_detail.md）。

v1 范围：仅支持本地仓库路径（用户自己 clone 好）；不做 GitHub API 抓取与远端 clone。
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _atomic_io import write_json_atomic
from _llm import call_llm, extract_json, llm_config

ROOT = Path(__file__).resolve().parent.parent
# 输入清单与产物都是「谁的简历材料」（#28）：main() 按解析前缀重算，常量仅 legacy 默认
DEFAULT_REPOS_LIST = ROOT / "private/data/repos_list.txt"
DEFAULT_OUTPUT = ROOT / "private/data/repos_detail.json"

from _profile_paths import (  # noqa: E402  #28：简历材料类产物按 profile 隔离
    private_path,
    profile_line,
    resolve_active_profile,
)


MAX_FILES = 60
MAX_FILE_BYTES = 200 * 1024
SNIPPET_FILES = 10
SNIPPET_PER_FILE = 1500
MAX_SNIPPET_CHARS = 20000

# IDE/agent 配置目录：缓存第三方 skill/会话临时文件，挤占采样名额（#16）
SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    ".agents",
    ".cursor",
    ".codeartsdoer",
    ".claude",
    ".opencode",
    ".amp",
}
BINARY_EXTS = {
    # 图片
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".icns",
    ".bmp",
    ".tiff",
    ".svg",
    # 文档
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    # 压缩包
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".7z",
    ".rar",
    # 字体
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    # 音视频
    ".mp3",
    ".mp4",
    ".mov",
    ".avi",
    ".wav",
    ".flac",
    ".ogg",
    ".webm",
    # 编译产物/二进制
    ".so",
    ".dylib",
    ".dll",
    ".exe",
    ".bin",
    ".o",
    ".a",
    ".class",
    ".wasm",
    ".pyc",
    ".pyo",
    # 数据/模型
    ".db",
    ".sqlite",
    ".sqlite3",
    ".onnx",
    ".pt",
    ".pth",
    ".safetensors",
    ".gguf",
    ".pb",
}
# 优先入选采样清单的清单/说明文件（最能代表技术栈与架构），含 CI 配置（文件名精确匹配）
PRIORITY_FILES = {
    "readme",
    "readme.md",
    "readme.rst",
    "readme.txt",
    "package.json",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "composer.json",
    "gemfile",
    "dockerfile",
    "docker-compose.yml",
    "makefile",
    ".gitlab-ci.yml",
    ".travis.yml",
    ".drone.yml",
    ".woodpecker.yml",
    "jenkinsfile",
    "azure-pipelines.yml",
    "cloudbuild.yaml",
}
# CI 工作流目录（路径匹配）：其下文件名各异无法枚举（如 .github/workflows/*.yml）
CI_DIRS = (".github/workflows", ".circleci")

SURVEY_SYSTEM = (
    "你是代码仓库调研 agent。只依据给出的采样文件清单与关键文件片段总结该仓库，"
    '不得编造未出现的技术栈或功能；片段不足以确认的事实写入 red_flags（标注"未读到"）。'
    '输出 JSON：{"positioning": "一句话定位", "tech_stack": "语言/框架/关键依赖（含版本号）", '
    '"architecture_summary": "目录组织与架构要点", "highlights": ["硬事实逐条"], '
    '"red_flags": ["疑点/风险/未读到逐条"]} /no_think'
)


def file_priority(rel_path):
    if rel_path.name.lower() in PRIORITY_FILES:
        return 0
    parent = "/".join(rel_path.parts[:-1]).lower()  # CI 工作流目录按路径匹配
    return 0 if any(d in parent for d in CI_DIRS) else 1


def sample_files(repo_path, max_files=MAX_FILES):
    """按采样规则遍历仓库，返回相对路径列表（优先文件在前，最多 max_files 个）。"""
    repo = Path(repo_path)
    candidates = []
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        base = Path(dirpath)
        for name in filenames:
            if name in (".DS_Store", ".git"):  # .git 在 worktree 里是指针文件而非目录
                continue
            p = base / name
            if p.suffix.lower() in BINARY_EXTS:
                continue
            try:
                if p.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            candidates.append(p.relative_to(repo))
    candidates.sort(key=lambda rp: (file_priority(rp), str(rp).lower()))
    return candidates[:max_files]


def build_snippets(repo_path, files):
    """取优先文件前若干个的正文片段拼 prompt 素材，总量超 MAX_SNIPPET_CHARS 截断。"""
    repo = Path(repo_path)
    parts, total = [], 0
    for rel in files[:SNIPPET_FILES]:
        try:
            text = (repo / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        remaining = MAX_SNIPPET_CHARS - total
        if remaining <= 0:
            break
        snippet = text[:SNIPPET_PER_FILE][:remaining]
        total += len(snippet)
        parts.append(f"## {rel}\n{snippet}")
    return "\n\n".join(parts)


def build_survey_messages(repo_name, repo_path, files, snippet_text):
    file_list = "\n".join(f"- {f}" for f in files) if files else "（无）"
    user = f"""# 仓库
名称: {repo_name}
路径: {repo_path}

# 采样文件清单（共 {len(files)} 个）

{file_list}

# 关键文件片段

{snippet_text or "（无片段）"}

只依据以上事实输出 JSON 档案。"""
    return [{"role": "system", "content": SURVEY_SYSTEM}, {"role": "user", "content": user}]


def survey_repo(repo_path, llm_call, cfg, max_files=MAX_FILES, now=None):
    """单仓调研：采样 → LLM 总结 → 档案条目；失败记录 error 字段不抛出。"""
    repo = Path(repo_path).resolve()
    files = sample_files(repo, max_files)
    base = {
        "repo": repo.name,
        "path": str(repo),
        "files_sampled": len(files),
        "surveyed_at": (now or datetime.now()).isoformat(timespec="seconds"),
    }
    if not files:
        return {**base, "error": "没有可采样文件（空仓库或全被过滤）"}
    try:
        out = extract_json(
            llm_call(build_survey_messages(repo.name, str(repo), files, build_snippets(repo, files)), cfg)
        )
    except Exception as e:
        return {**base, "error": str(e)}
    return {
        **base,
        "positioning": out.get("positioning", ""),
        "tech_stack": out.get("tech_stack", ""),
        "architecture_summary": out.get("architecture_summary", ""),
        "highlights": out.get("highlights", []),
        "red_flags": out.get("red_flags", []),
    }


def upsert_repos(existing, new):
    """本批结果按 repo 名 upsert 进已有档案：同名单仓替换（保位次）、新仓追加；
    新条目带 error 而已有同名档案无 error 时保留旧档案不替换（失败不覆盖好档案）。"""
    by_repo = {e["repo"]: e for e in new}
    out, seen = [], set()
    for e in existing:
        r = e.get("repo")
        if r in by_repo:
            if by_repo[r].get("error") and not e.get("error"):
                out.append(e)  # 本批失败结果不覆盖已有好档案
            else:
                out.append(by_repo[r])
            seen.add(r)
        else:
            out.append(e)
    for e in new:
        if e["repo"] not in seen:
            out.append(e)
    return out


def load_repo_paths(repos_arg, repos_file_arg, default_list=None):
    """解析输入：--repos 逗号分隔优先；否则 --repos-file；否则默认列表文件（缺失报错退出）。"""
    if repos_arg:
        return [p.strip() for p in repos_arg.split(",") if p.strip()]
    path = Path(repos_file_arg) if repos_file_arg else (default_list or DEFAULT_REPOS_LIST)
    if not path.exists():
        print(
            f"[错误] repo 列表文件不存在: {path}\n"
            "请创建它（每行一个本地 repo 路径，# 开头为注释），或用 --repos A,B,C / --repos-file FILE 指定。"
        )
        sys.exit(1)
    return [
        ln.strip()
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]


def _load_json(path, default):
    """读已有输出 JSON；存在但损坏时立即报错退出（fail-fast，避免 LLM 整批跑完才发现档案读不了）。"""
    p = Path(path)
    if not p.exists():
        return default
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(
            f"[错误] 已有输出文件不是合法 JSON，无法 upsert: {p}（{e}）\n"
            "请先修复或删除该文件（或改用 --force 覆盖）后重试。"
        )
        sys.exit(1)


def run(repos, output_path, max_files=MAX_FILES, force=False, dry_run=False, llm_call=None, cfg=None, now=None):
    """整批主流程：读已有档案（fail-fast）→ 逐仓调研 → upsert 落盘。dry-run 只打印采样计划。
    本批 repo 重名时打印告警不中断（档案按 repo 名 upsert 会互相覆盖，下游 T3 也按 repo 名消费）。
    返回本批档案列表。"""
    if llm_call is None:

        def llm_call(msgs, c):
            return call_llm(msgs, c)

    cfg = cfg or {}
    # fail-fast：调 LLM 之前先读已有档案（dry-run 不读不写、--force 丢弃已有内容），
    # 损坏 JSON 立即报错退出，避免整批 LLM 白跑
    existing = [] if force or dry_run else _load_json(output_path, [])
    results = []
    seen_names = set()
    for rp in repos:
        p = Path(rp).expanduser().resolve()  # resolve："." 等相对路径也要拿到正确的 repo 名
        if not p.is_dir():
            print(f"[跳过] 路径不存在或不是目录: {rp}")
            continue
        if p.name in seen_names:
            print(f"[警告] 本批存在重名仓库: {p.name}（同名单仓会互相覆盖，下游 T3 也按 repo 名消费）")
        seen_names.add(p.name)
        files = sample_files(p, max_files)
        if dry_run:
            preview = ", ".join(str(f) for f in files[:5])
            print(f"[dry-run] {p.name} | 采样 {len(files)} 个文件 | 前 5: {preview}")
            continue
        print(f"[survey] {p.name}（{len(files)} 个采样文件）...")
        entry = survey_repo(p, llm_call, cfg, max_files=max_files, now=now)
        if entry.get("error"):
            print(f"  [失败] {entry['repo']}: {entry['error']}")
        results.append(entry)
    if dry_run:
        print("[done] dry-run 结束：未调 LLM、未写文件")
        return []
    if not results:
        print("[done] 没有产出档案，输出文件保持不变")
        return results
    merged = results if force else upsert_repos(existing, results)
    write_json_atomic(output_path, merged)
    ok = sum(1 for e in results if "error" not in e)
    print(
        f"[done] 本批 {len(results)} 仓（成功 {ok} / 失败 {len(results) - ok}）→ {output_path}（累计 {len(merged)} 条）"
    )
    return results


def main():
    parser = argparse.ArgumentParser(description="repo 全仓调研：本地仓库逐仓 LLM 总结 → repos_detail.json（#14 T2）")
    parser.add_argument("--repos", default="", help="逗号分隔的本地 repo 路径（优先于 --repos-file）")
    parser.add_argument(
        "--repos-file", default=None, help="每行一个 repo 路径的文本文件（默认 private/data/repos_list.txt）"
    )
    parser.add_argument(
        "--output", default=None, help="输出 JSON 路径（默认按 profile 解析 private/data/[<name>.]repos_detail.json）"
    )
    parser.add_argument("--max-files", type=int, default=MAX_FILES, help=f"每仓最多采样的文件数（默认 {MAX_FILES}）")
    parser.add_argument("--force", action="store_true", help="覆盖输出文件（默认按 repo 名 upsert）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将采样的仓库与文件数，不调 LLM 不写文件")
    args = parser.parse_args()
    _name, _prefix = resolve_active_profile()
    print(profile_line(_name, _prefix))
    repos_list = private_path("data/repos_list.txt", _prefix)
    output = Path(args.output) if args.output else private_path("data/repos_detail.json", _prefix)
    repos = load_repo_paths(args.repos, args.repos_file, default_list=repos_list)
    run(repos, output, max_files=args.max_files, force=args.force, dry_run=args.dry_run, cfg=llm_config())


if __name__ == "__main__":
    main()
