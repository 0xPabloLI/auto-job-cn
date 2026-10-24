#!/usr/bin/env python3
"""公开边界守卫的行为测试（#108）：denylist 来源解析，与「缺失要显式可见但不拦」的退出码契约。

拍板口径（#108 第二十二轮 triage）：实装 A（提升链给 `git commit` 传 env）+ B（denylist 缺失时
往 stderr 打 WARN），C 归并否决。**缺失不拦**——公开仓 CI 整树扫描天然没有 denylist，
缺失即拦会把 CI 打成必红。所以这里钉的是「可见但不拦」这一对读数，以及 denylist 在场时
只靠姓名/公司名命中的内容必须 rc=1。

夹具全是合成值（一个假姓名、一个假号码），不读任何真实清单；脚本一律先拷进临时仓的
`scripts/` 再跑——它按自身位置推导仓根，直接跑 overlay 里的原件会 chdir 到链目录、扫错树。
"""

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent / "check_public_boundary.py"
_spec = importlib.util.spec_from_file_location("check_public_boundary", SCRIPT)
assert _spec is not None and _spec.loader is not None, "importlib spec 加载失败（守卫脚本缺失？）"
cb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cb)

NAME = "张三丰"
MOBILE = "13800001111"  # 合成夹具，专门用来触形态层 # pii-ok


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _run(repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """跑临时仓里那份副本，退出码与两条流都是真产物。"""
    return subprocess.run(
        [sys.executable, str(repo / "scripts" / "check_public_boundary.py"), "--staged"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """一条 staged 记录文本的临时 git 仓；`PII_DENYLIST` 一律先摘掉。"""
    monkeypatch.delenv("PII_DENYLIST", raising=False)
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, root / "scripts" / "check_public_boundary.py")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "note.md").write_text(f"对接人 {NAME}\n", encoding="utf-8")
    _git(root, "add", "note.md")
    return root


# ---------- 来源解析：路径与「为什么盲」共用一份算法 ----------


def test_source_prefers_env_over_local_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("PII_DENYLIST", "/somewhere/pii.txt")
    assert cb.denylist_source(tmp_path) == (Path("/somewhere/pii.txt"), "env")


def test_source_falls_back_to_repo_private(tmp_path, monkeypatch):
    monkeypatch.delenv("PII_DENYLIST", raising=False)
    assert cb.denylist_source(tmp_path) == (tmp_path / "private/pii_denylist.txt", "fallback")


def test_load_drops_comments_short_terms_and_sorts_longest_first(tmp_path, monkeypatch):
    listing = tmp_path / "pii.txt"
    listing.write_text("# 注释行\nab\n李四\n王小明\n张大魔王\n", encoding="utf-8")
    monkeypatch.setenv("PII_DENYLIST", str(listing))
    assert cb.load_denylist(tmp_path) == ["张大魔王", "王小明"]


def test_present_but_zero_term_list_counts_as_not_loaded(tmp_path, monkeypatch):
    """钉住那个残留：清单文件在 ≠ 那一层在跑。改成「文件在就算加载」这条用例必须红。"""
    listing = tmp_path / "pii.txt"
    listing.write_text("# 只有注释，一条可用词条都没有\n", encoding="utf-8")
    monkeypatch.setenv("PII_DENYLIST", str(listing))
    assert cb.load_denylist(tmp_path) == []


# ---------- CLI 契约：缺失可见（三种理由分开说）、但不拦 ----------


def test_missing_denylist_warns_but_exits_zero(repo):
    p = _run(repo, dict(os.environ))
    assert "[boundary] WARN 仅形态扫描" in p.stderr, p.stdout + p.stderr
    assert "未设 `PII_DENYLIST`" in p.stderr
    assert "仅形态扫描" in p.stdout  # clean 行同步说清只跑了形态层
    assert p.returncode == 0, f"缺失不拦是拍板边界：{p.stdout}"


def test_env_pointing_to_missing_file_says_so(repo, tmp_path):
    p = _run(repo, dict(os.environ, PII_DENYLIST=str(tmp_path / "nope.txt")))
    assert "`PII_DENYLIST` 指向的" in p.stderr and "不存在" in p.stderr
    assert "未设" not in p.stderr  # 说成「未设」会把设了却指错的形态盖掉
    assert p.returncode == 0


def test_present_but_empty_list_says_so(repo, tmp_path):
    listing = tmp_path / "pii.txt"
    listing.write_text("# 全是注释\n", encoding="utf-8")
    p = _run(repo, dict(os.environ, PII_DENYLIST=str(listing)))
    assert "没有可用词条" in p.stderr
    assert p.returncode == 0


def test_loaded_denylist_blocks_name_hit_and_stays_quiet(repo, tmp_path):
    listing = tmp_path / "pii.txt"
    listing.write_text(f"{NAME}\n", encoding="utf-8")
    p = _run(repo, dict(os.environ, PII_DENYLIST=str(listing)))
    assert p.returncode == 1, p.stdout + p.stderr
    assert "[denylist]" in p.stdout
    assert "WARN" not in p.stderr  # 加载态报失明 = 假警报，会把这行训成噪音


def test_blocked_reading_still_discloses_blindness(repo):
    """失明行在 BLOCKED 之前也要打：只放 clean 分支的话，BLOCKED 那份读数分不清是哪层拦的。"""
    (repo / "note.md").write_text(f"对接人电话 {MOBILE}\n", encoding="utf-8")
    _git(repo, "add", "note.md")
    p = _run(repo, dict(os.environ))
    assert "[cn_mobile]" in p.stdout
    assert "[boundary] WARN 仅形态扫描" in p.stderr
    assert p.returncode == 1
