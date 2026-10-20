#!/usr/bin/env python3
"""Public-boundary guard.

这个仓库是公开发布面。本脚本拒绝个人数据入库：private/ 路径、简历与媒体文件、
PII 形态（手机号/证件号/邮箱/绝对本地路径/硬编码年限），以及本机可选的
`private/pii_denylist.txt`（一行一个已知个人标识，不入库）。

用法：
    python3 scripts/check_public_boundary.py            # 扫全部 tracked 文件
    python3 scripts/check_public_boundary.py --staged    # 只扫 staged（pre-commit）

单行确认为合成夹具时，在该行加注释 `pii-ok`；不要用白名单常量把真实数据留在仓里。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

MARKER = "pii-ok"

BANNED_PATHS = [
    re.compile(r"^private/"),
    re.compile(r"^scripts/oneshot/"),
    re.compile(r"(?i)\.(pdf|docx?|xlsx?|pptx?|png|jpe?g|gif|webp|heic|mp4|webm|mov|avi|sqlite3?|zip|tar\.gz)$"),
    re.compile(r"(?i)(^|/)(app|apply_log)\.log$"),
    re.compile(r"^\.env(\.|$)"),
    re.compile(r"^\.coverage$"),
    re.compile(r"(?i)(^|/)(__pycache__|\.mypy_cache|\.pytest_cache|\.ruff_cache|node_modules|\.venv)/"),
    re.compile(r"\.camoufox_profile"),
]

FORBIDDEN_DOCS = [
    "docs/issues-roadmap.md",
    "docs/archive/",
    "docs/research/",
    "docs/agents/harness.md",
]

CONTENT_SCANS = [
    ("cn_mobile", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    (
        "cn_id_card",
        re.compile(r"(?<![\dXx])[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![\dXx])"),
    ),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z]{2,6})+")),
    ("local_path", re.compile(r"(?:/Users/|/home/)([A-Za-z0-9._-]+)")),
    ("resume_file", re.compile(r"[\w\-一-鿿]*[Rr]esume[\w\-一-鿿]*\.(pdf|docx?)", re.IGNORECASE)),
    ("hardcoded_years", re.compile(r"years=\[\d+\]")),
]

SAFE_EMAIL_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "localhost",
    "y.com",
    "users.noreply.github.com",
    "t.local",
    "example",
}
SAFE_PATH_USERS = {"example", "user", "runner", "pablo", "<user>", "name"}
SAFE_MOBILE = {"13812345678", "13800138000", "13900139000", "15000000000", "18888888888"}


def mask(value: str) -> str:
    if len(value) <= 4:
        return value[:1] + "***"
    return f"{value[:2]}***{value[-2:]}"


def git(*args: str) -> list[str]:
    proc = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line]


SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
}


def files_to_scan(staged: bool) -> list[str]:
    if staged:
        return git("diff", "--cached", "--name-only", "--diff-filter=ACM")
    tracked = git("ls-files")
    if tracked:
        return tracked
    # 尚未 git init 的导出目录：整棵树逐文件判定（private/ 等仍会被路径规则拦下）
    root = Path(".")
    found = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        parts = set(path.relative_to(root).parts[:-1])
        if parts & SKIP_DIRS:
            continue
        found.append(path.relative_to(root).as_posix())
    return found


def load_denylist(root: Path) -> list[str]:
    """已知个人标识清单：默认读本地 private/，可用 PII_DENYLIST 指向别处（都不入库）。"""
    import os

    env = os.environ.get("PII_DENYLIST", "").strip()
    path = Path(env) if env else root / "private/pii_denylist.txt"
    if not path.is_file():
        return []
    terms = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        term = line.strip()
        if term and not term.startswith("#") and len(term) >= 3:
            terms.append(term)
    return sorted(terms, key=len, reverse=True)


def check_paths(files: list[str], findings: list[str]) -> None:
    for rel in files:
        for rx in BANNED_PATHS:
            if rx.search(rel):
                findings.append(f"{rel}: 路径本身就不该入库（个人数据/媒体/缓存）")
                break
        for prefix in FORBIDDEN_DOCS:
            if rel == prefix or rel.startswith(prefix):
                findings.append(f"{rel}: 属于个人/内部文档，不随公开仓分发")
                break


def check_content(files: list[str], denylist: list[str], findings: list[str]) -> None:
    for rel in files:
        path = Path(rel)
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            findings.append(f"{rel}: 二进制/不可读文件，公开仓不允许")
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if MARKER in line:
                continue
            for kind, rx in CONTENT_SCANS:
                for match in rx.finditer(line):
                    value = match.group(0)
                    if kind == "resume_file" and value.lower().startswith(("sample_", "example_")):
                        continue
                    if kind == "cn_mobile" and value in SAFE_MOBILE:
                        continue
                    if kind == "email" and value.split("@", 1)[1].lower() in SAFE_EMAIL_DOMAINS:
                        continue
                    if kind == "local_path":
                        user = (match.group(1) or "").lower()
                        if user in SAFE_PATH_USERS or user.startswith(("runner", "vsts")):
                            continue
                    findings.append(f"{rel}:{lineno} [{kind}] {mask(value)}")
            for term in denylist:
                if term in line:
                    findings.append(f"{rel}:{lineno} [denylist] {mask(term)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="只扫 staged 文件")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    import os

    os.chdir(root)

    files = files_to_scan(args.staged)
    findings: list[str] = []
    check_paths(files, findings)
    check_content(files, load_denylist(root), findings)

    scope = "staged" if args.staged else "tracked"
    if findings:
        print(f"[boundary] BLOCKED：{len(findings)} 处个人数据命中（范围={scope}）")
        for line in findings[:60]:
            print(f"  - {line}")
        if len(findings) > 60:
            print(f"  - … 另 {len(findings) - 60} 处")
        print("\n处理方式：删除该内容或改用占位符；确认为合成夹具的行在该行加 `pii-ok`。")
        return 1

    print(
        f"[boundary] clean（{len(files)} 个 {scope} 文件；denylist 已加载）"
        if load_denylist(root)
        else f"[boundary] clean（{len(files)} 个 {scope} 文件；未找到 private/pii_denylist.txt，仅形态扫描）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
