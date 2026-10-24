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
import os
import re
import subprocess
import sys
from pathlib import Path

MARKER = "pii-ok"

BANNED_PATHS = [
    re.compile(r"^private/"),
    re.compile(r"^private$"),
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
    # `(?:\+?86[-\s]?)?` 让 +86 连写也算命中（旧写法 `(?<!\d)` 会把 `+8613800001111` 判成  # pii-ok
    # 「前面是数字」而漏掉）；两侧 lookaround 保留，长数字串里不误报。
    # 「前面是数字」而漏掉）；两侧 lookaround 保留，长数字串里不误报。
    ("cn_mobile", re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)")),
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

# 带分隔符的 3-4-4 写法（连字符或空格分组）单独一条，且只在同一行出现语境词时才生效：
# 无条件放开会把时间戳、坐标、长数字串全卷进来，误报会把这条守卫训成噪声。
MOBILE_SEPARATED = re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d[-\s]\d{4}[-\s]\d{4}(?!\d)")
PHONE_CONTEXT = re.compile(r"(?i)(tel|phone|mobile|手机|电话|联系|号码)")
# 刻意不设「安全号码常量」：按值放行是全仓生效的洞；合成夹具用行内标记，只豁免那一行。


PLACEHOLDER_MARKS = ("sample", "example", "示例", "占位", "test_")


def _is_placeholder(value: str) -> bool:
    """我们自己生成的占位文件名（`Resume_示例用户.pdf`）不是个人信息，但长得像简历名。

    真实姓名这一层由 denylist 单独命中，放宽形态判断不会让那一层失效。
    """
    low = value.lower()
    return any(mark in low for mark in PLACEHOLDER_MARKS)


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


def denylist_source(root: Path) -> tuple[Path, str]:
    """清单来源的唯一算法：显式 `PII_DENYLIST` 优先，否则回退本地 private/（都不入库）。

    返回 `(路径, 来源)`，来源参与「为什么盲」的措辞——设了却指错与压根没设，补救动作不一样。
    """
    env = os.environ.get("PII_DENYLIST", "").strip()
    return (Path(env), "env") if env else (root / "private/pii_denylist.txt", "fallback")


def usable_terms(path: Path) -> list[str]:
    """从一份清单文件数出可用词条：一行一个已知个人标识，短于 3 字与注释行不计。

    「可用词条」的定义只有这一处（#113）——提升链的真门经 importlib 载入本模块调它，
    别在 promote 里另写一套 ≥3 字/非注释规则。
    """
    if not path.is_file():
        return []
    terms = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        term = line.strip()
        if term and not term.startswith("#") and len(term) >= 3:
            terms.append(term)
    return sorted(terms, key=len, reverse=True)


def load_denylist(root: Path) -> list[str]:
    """已知个人标识清单：解析来源路径后交给 `usable_terms()`（一份定义）。"""
    path, _ = denylist_source(root)
    return usable_terms(path)


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


def tracked_symlinks(staged: bool) -> list[str]:
    """被跟踪的符号链接。

    公开仓里的软链只会指向仓外（本机数据目录），它的 target 就是一串绝对路径，
    而且 `private/` 这类 ignore 规则按目录匹配、根本挡不住同名软链——所以直接禁。
    """
    if staged:
        rows = git("diff", "--cached", "--raw", "--diff-filter=ACM")
        out = []
        for row in rows:
            fields = row.split()
            # raw 格式 `:<旧模式> <新模式> <旧sha> <新sha> <状态>\t<路径>`——新模式是第 2 个字段
            if len(fields) >= 5 and fields[1] == "120000":
                out.append(fields[-1].split("->")[-1])
        return out
    return [r.split("\t", 1)[1] for r in git("ls-files", "-s") if r.startswith("120000 ")]


def check_symlinks(files: list[str], findings: list[str]) -> None:
    for rel in files:
        target = ""
        try:
            target = os.readlink(rel)
        except OSError:
            pass
        findings.append(f"{rel}: 公开仓不得跟踪符号链接（target={target or '读不出'}）")


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
                    if kind == "resume_file" and _is_placeholder(value):
                        continue
                    if kind == "email" and value.split("@", 1)[1].lower() in SAFE_EMAIL_DOMAINS:
                        continue
                    if kind == "local_path":
                        user = (match.group(1) or "").lower()
                        if user in SAFE_PATH_USERS or user.startswith(("runner", "vsts")):
                            continue
                    findings.append(f"{rel}:{lineno} [{kind}] {mask(value)}")
            if PHONE_CONTEXT.search(line):
                for match in MOBILE_SEPARATED.finditer(line):
                    findings.append(f"{rel}:{lineno} [cn_mobile] {mask(match.group(0))}")
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
    check_symlinks(tracked_symlinks(args.staged), findings)
    denylist = load_denylist(root)
    check_content(files, denylist, findings)

    scope = "staged" if args.staged else "tracked"

    if not denylist:
        # 缺失不拦（公开仓 CI 整树扫描天然没有 denylist，缺失即拦会把 CI 打成必红），
        # 但失明必须显式可见，且在「拦下」与「放行」两种结局里都要说得出——只放在 clean 分支，
        # BLOCKED 那份读数就分不清是带清单拦的还是盲拦。打 stderr，钩子与闸门日志都留这行。
        path, kind = denylist_source(root)
        if path.is_file():
            why = f"清单 `{path}` 在，但没有可用词条（每行需 ≥3 字且非注释）"
        elif kind == "env":
            why = f"`PII_DENYLIST` 指向的 `{path}` 不存在"
        else:
            why = f"未设 `PII_DENYLIST`，回退路径 `{path}` 也不存在"
        print(
            f"[boundary] WARN 仅形态扫描：denylist 未加载（{why}），"
            "姓名/公司名这一层是失明的——形态规则抓不到真实姓名。"
            "补救：给运行本脚本的进程设 `PII_DENYLIST=<清单文件>`，"
            "或在本仓根放 `private/pii_denylist.txt`。",
            file=sys.stderr,
        )

    if findings:
        print(f"[boundary] BLOCKED：{len(findings)} 处个人数据命中（范围={scope}）")
        for line in findings[:60]:
            print(f"  - {line}")
        if len(findings) > 60:
            print(f"  - … 另 {len(findings) - 60} 处")
        print("\n处理方式：删除该内容或改用占位符；确认为合成夹具的行在该行加 `pii-ok`。")
        return 1

    # 「denylist 已加载」与「仅形态扫描」是跨文件契约：promote_public.hook_boundary_reading
    # 按这两个子串判读钩子那一侧的姓名/公司名层在不在跑，改文案要连着改那一处。
    print(
        f"[boundary] clean（{len(files)} 个 {scope} 文件；denylist 已加载）"
        if denylist
        else f"[boundary] clean（{len(files)} 个 {scope} 文件；仅形态扫描）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
