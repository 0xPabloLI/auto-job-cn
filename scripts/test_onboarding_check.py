"""onboarding_check.py 测试：前置盘点的机械核验（用户约定 2026-09-10：
onboarding 完成与否以代码退出码为准，不靠 agent 自评）。"""

import importlib.util
import json
from pathlib import Path


_SCRIPTS = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("onboarding_check", _SCRIPTS / "onboarding_check.py")
assert _spec is not None and _spec.loader is not None, "importlib spec 加载失败（模块文件缺失？）"
oc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oc)


# ---------- fixture 组装 ----------

_BG_OK = """# 个人背景

## 1. 时间线与锚点
2018 至今：某公司 Solidity 工程师。

## 8. 招呼语可用硬事实清单
7年Solidity，主网部署过3个协议

## 9. 禁写
不要写年龄。
"""

_CARDS_OK = """# 事实卡片

### [C1] 年限
- 7 年 Solidity 经验（来源：§1）

## 白名单（机器读，勿手改——gen_greetings.py doctor --sync 编译）

```json
{"years": [7], "entities": ["Solidity"]}
```
"""


def _mk_repo(
    tmp_path, *, config=True, repos_ok=True, resume=True, background=True, profile=True, cards=True, excluded=True
):
    root = tmp_path / "repo"
    priv = root / "private"
    (priv / "kb" / "profiles").mkdir(parents=True)
    (priv / "kb" / "industries").mkdir(parents=True)
    (priv / "data").mkdir(parents=True)
    if config:
        repo_path = tmp_path / "myrepo" if repos_ok else tmp_path / "nope"
        if repos_ok:
            repo_path.mkdir()
        (root / "config.yaml").write_text(f"repos:\n  - {repo_path}\n", encoding="utf-8")
    if resume:
        (priv / "resume.md").write_text("# 我的简历", encoding="utf-8")
    if background:
        (priv / "profile_background.md").write_text(_BG_OK, encoding="utf-8")
    if profile:
        (priv / "kb" / "profiles" / "web3.json").write_text(
            json.dumps({"industry": "web3", "queries": ["Solidity", "Web3"]}, ensure_ascii=False), encoding="utf-8"
        )
        (priv / "kb" / "industries" / "web3.md").write_text("# Web3 行业层\n", encoding="utf-8")
    if cards:
        (priv / "kb" / "fact_cards.md").write_text(_CARDS_OK, encoding="utf-8")
    if excluded:
        (priv / "data" / "excluded_companies.json").write_text('["老东家"]', encoding="utf-8")
    return root


# ---------- 全过路径 ----------


def test_all_ok(tmp_path):
    root = _mk_repo(tmp_path)
    results = dict((name, ok) for name, ok, _ in oc.run_checks(root))
    assert all(results.values()), results
    assert oc.main(["--root", str(root)]) == 0


def test_all_ok_empty_excluded_list(tmp_path):
    """用户没有要排除的公司：空数组也算就绪（onboarding 步骤 1 约定）。"""
    root = _mk_repo(tmp_path)
    (root / "private/data/excluded_companies.json").write_text("[]", encoding="utf-8")
    name, ok, _ = next(r for r in oc.run_checks(root) if r[0] == "排除公司清单")
    assert ok


# ---------- 单项失败 ----------


def test_config_optional_when_absent(tmp_path):
    """config.yaml 可选：材料全为非 repo 形式（作品集 URL/设计稿/文档）时不需要。"""
    root = _mk_repo(tmp_path, config=False)
    name, ok, detail = next(r for r in oc.run_checks(root) if r[0] == "config repos")
    assert ok and "可选" in detail


def test_repo_path_not_exist(tmp_path):
    root = _mk_repo(tmp_path, repos_ok=False)
    name, ok, detail = next(r for r in oc.run_checks(root) if r[0] == "config repos")
    assert not ok and "nope" in detail


def test_no_resume(tmp_path):
    root = _mk_repo(tmp_path, resume=False)
    name, ok, detail = next(r for r in oc.run_checks(root) if r[0] == "简历原文")
    assert not ok and "简历" in detail


def test_background_placeholder(tmp_path):
    root = _mk_repo(tmp_path)
    bg = root / "private/profile_background.md"
    bg.write_text(bg.read_text(encoding="utf-8").replace("某公司", "填你自己的"), encoding="utf-8")
    name, ok, detail = next(r for r in oc.run_checks(root) if r[0] == "个人层 profile_background")
    assert not ok and "填你自己的" in detail


def test_no_profile(tmp_path):
    root = _mk_repo(tmp_path, profile=False)
    name, ok, detail = next(r for r in oc.run_checks(root) if r[0] == "profile/行业层")
    assert not ok and "profile" in detail


def test_profile_industry_file_missing(tmp_path):
    root = _mk_repo(tmp_path)
    (root / "private/kb/profiles/web3.json").write_text(
        json.dumps({"industry": "ghost", "queries": ["Solidity"]}), encoding="utf-8"
    )
    name, ok, detail = next(r for r in oc.run_checks(root) if r[0] == "profile/行业层")
    assert not ok and "ghost" in detail


def test_profile_queries_missing_blocked(tmp_path):
    """#29 P0：profile 缺 queries → onboarding 门禁报缺（含可粘贴修复片段），退出码 1。"""
    root = _mk_repo(tmp_path)
    (root / "private/kb/profiles/web3.json").write_text(json.dumps({"industry": "web3"}), encoding="utf-8")
    name, ok, detail = next(r for r in oc.run_checks(root) if r[0] == "profile/行业层")
    assert not ok and "queries" in detail and "修复" in detail
    assert oc.main(["--root", str(root)]) == 1


def test_profile_queries_empty_or_non_string_blocked(tmp_path):
    root = _mk_repo(tmp_path)
    for payload in (
        {"industry": "web3", "queries": []},
        {"industry": "web3", "queries": ["ok", 3]},
        {"industry": "web3", "queries": "Solidity"},
    ):
        (root / "private/kb/profiles/web3.json").write_text(json.dumps(payload), encoding="utf-8")
        name, ok, _ = next(r for r in oc.run_checks(root) if r[0] == "profile/行业层")
        assert not ok, f"{payload} 应报缺"


def test_profile_queries_ok(tmp_path):
    root = _mk_repo(tmp_path)
    name, ok, detail = next(r for r in oc.run_checks(root) if r[0] == "profile/行业层")
    assert ok and "queries" in detail


def test_profile_bad_json(tmp_path):
    root = _mk_repo(tmp_path)
    (root / "private/kb/profiles/web3.json").write_text("{oops", encoding="utf-8")
    name, ok, detail = next(r for r in oc.run_checks(root) if r[0] == "profile/行业层")
    assert not ok and "JSON" in detail


def test_cards_whitelist_missing(tmp_path):
    root = _mk_repo(tmp_path, cards=False)
    results = dict((name, ok) for name, ok, _ in oc.run_checks(root))
    assert not results["事实卡片白名单"]
    assert not results["doctor 同步"]


def test_doctor_gap(tmp_path):
    """§8 硬事实提到白名单外的事实（9年Go）→ doctor 项失败，其余不受影响。"""
    root = _mk_repo(tmp_path)
    bg = root / "private/profile_background.md"
    bg.write_text(_BG_OK.replace("7年Solidity，主网部署过3个协议", "7年Solidity，9年Go"), encoding="utf-8")
    results = dict((name, ok) for name, ok, _ in oc.run_checks(root))
    assert not results["doctor 同步"]
    assert results["个人层 profile_background"]  # 占位检查不受影响
    assert oc.main(["--root", str(root)]) == 1


def test_excluded_missing_and_bad_json(tmp_path):
    root = _mk_repo(tmp_path, excluded=False)
    assert not next(ok for name, ok, _ in oc.run_checks(root) if name == "排除公司清单")
    (root / "private/data/excluded_companies.json").write_text("{oops", encoding="utf-8")
    assert not next(ok for name, ok, _ in oc.run_checks(root) if name == "排除公司清单")


def test_missing_items_fail_main(tmp_path):
    root = _mk_repo(tmp_path, resume=False, excluded=False)
    assert oc.main(["--root", str(root)]) == 1


def test_second_profile_does_not_borrow_default_files(tmp_path, monkeypatch):
    """#28 回归：注册表含两个 profile、只建了 default 的产物时，选中另一个 profile
    盘点必须报缺（个人层/事实卡片/profile JSON/排除公司不能借用 default 的混过盘点）。"""
    root = _mk_repo(tmp_path)  # default（无注册表时在位）= web3 产物齐全
    priv = root / "private"
    (priv / "profiles.json").write_text(
        json.dumps(
            {
                "default": "web3",
                "profiles": {"web3": {"resume": "private/resume.md"}, "ai": {"resume": "private/resume-ai.md"}},
            }
        ),
        encoding="utf-8",
    )
    (priv / "resume-ai.md").write_text("# AI 简历", encoding="utf-8")
    monkeypatch.setenv("AUTOJOB_PROFILE", "ai")
    results = dict((n, (ok, d)) for n, ok, d in oc.run_checks(root))
    assert results["简历原文"][0], "注册表声明的 AI 简历应通过"
    for item in ("个人层 profile_background", "事实卡片白名单", "排除公司清单"):
        assert not results[item][0], f"{item} 不能借用 default profile 的文件混过盘点"
    assert "ai" in results["profile/行业层"][1], "报缺应指明 ai.json 未建"
    monkeypatch.delenv("AUTOJOB_PROFILE")
    results_default = dict((n, ok) for n, ok, _ in oc.run_checks(root))
    assert results_default["个人层 profile_background"], "default profile 盘点不受影响"
