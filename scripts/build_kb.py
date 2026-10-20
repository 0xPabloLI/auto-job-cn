#!/usr/bin/env python3
"""
知识库构建（#14 T3）：读 repo_survey.py 的调研档案 → LLM 综合 → 知识库草稿（HITL 人工审核合并）。

用法：
  python3 scripts/build_kb.py [--input FILE] [--output FILE]
  默认读 private/data/repos_detail.json（repo_survey.py 产物，字段 schema 见其 docstring），
  产出草稿 private/kb/draft/repos_detail.draft.md（--output 可覆盖路径）。

输出（草稿，不直接改写正式知识库——知识库是 gen_greetings.py 的唯一事实来源）：
  1. detail 层条目：每仓一节（定位/技术栈/架构/亮点/风险），人工审核后并入 private/kb/repos_detail.md；
  2. 精炼层摘要建议：几行，人工审核后并入 private/profile_background.md 对应章节
     （禁写清单/禁推测约束继续生效）。
  调研失败（error）档案跳过并列入草稿"待重跑"节；单仓综合失败不中断整批。
  结束时打印人工合并指引；分层文档流程见 docs/kb-sop.md。

LLM：OpenAI 兼容 /v1/chat/completions。默认本地 Ollama（qwen3:8b），
     GREETING_LLM_BASE_URL / GREETING_LLM_MODEL / GREETING_LLM_API_KEY 可换任意兼容服务。

禁推测约束（进 prompt）：只能引用输入 JSON 字段事实，不得编造未给出的指标/数据/时间线。
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _atomic_io import write_text_atomic
from _llm import call_llm, extract_json, llm_config

ROOT = Path(__file__).resolve().parent.parent
# 输入/输出是「谁的简历材料」（#28）：main() 按解析前缀重算，常量仅 legacy 默认
DEFAULT_INPUT = ROOT / "private/data/repos_detail.json"
DEFAULT_OUTPUT = ROOT / "private/kb/draft/repos_detail.draft.md"

from _profile_paths import (  # noqa: E402  #28：简历材料类产物按 profile 隔离
    private_path,
    profile_line,
    resolve_active_profile,
)


DETAIL_KB = "private/kb/repos_detail.md"
REFINED_KB = "private/profile_background.md"

SECTION_SYSTEM = (
    "你是知识库编辑。只依据输入 JSON 档案的字段事实整理该仓库的 detail 层条目："
    "逐字保留数字、链名、版本号、地址等事实，不得编造 JSON 未给出的指标、数据、时间线或部署状态；"
    "字段不足以确认的内容标「未读到」或不写。"
    '输出 JSON：{"section": "条目 Markdown 正文（不含仓库标题）"}，正文按以下结构：\n'
    "- **定位**：…\n"
    "- **技术栈**：…\n"
    "- **架构**：…\n"
    "- **亮点**：（逐条列表）\n"
    "- **风险**：（逐条列表，无则写「未读到」） /no_think"
)
SUMMARY_SYSTEM = (
    "你是知识库编辑，正在为精炼层（招呼语生成的唯一事实来源）起草摘要建议。"
    "只依据输入档案的字段事实，不得编造未给出的指标、数据、时间线或部署状态；事实不足的仓库不写。"
    "按主题归组（如旗舰项目组/合约组/工具组），输出几行精炼摘要，每行聚焦一个主题或项目组。"
    '输出 JSON：{"summary": "几行 Markdown 摘要（无标题、无编号）"} /no_think'
)


def load_entries(input_path):
    """读调研档案 JSON；文件缺失/损坏/不是数组时立即报错退出（fail-fast，避免 LLM 白跑）。"""
    p = Path(input_path)
    if not p.exists():
        print(f"[错误] 调研档案不存在: {p}\n请先运行 repo_survey.py 生成，或用 --input 指定其他档案路径。")
        sys.exit(1)
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"[错误] 调研档案不是合法 JSON: {p}（{e}）\n请先修复或重跑 repo_survey.py。")
        sys.exit(1)
    if not isinstance(data, list):
        print(f"[错误] 调研档案格式不对（应为数组）: {p}")
        sys.exit(1)
    return data


def split_entries(entries):
    """按 error 字段分成（有效档案, 调研失败档案）；失败档案跳过综合、列待重跑。"""
    valid = [e for e in entries if not e.get("error")]
    skipped = [e for e in entries if e.get("error")]
    return valid, skipped


def build_section_messages(entry):
    user = f"""# 仓库档案 JSON

{json.dumps(entry, ensure_ascii=False, indent=1)}

只依据以上字段事实输出条目 JSON。"""
    return [{"role": "system", "content": SECTION_SYSTEM}, {"role": "user", "content": user}]


def build_summary_messages(entries):
    user = f"""# 待综合的仓库档案 JSON（共 {len(entries)} 仓）

{json.dumps(entries, ensure_ascii=False, indent=1)}

只依据以上字段事实输出精炼摘要建议 JSON。"""
    return [{"role": "system", "content": SUMMARY_SYSTEM}, {"role": "user", "content": user}]


def render_draft(sections, summary, skipped, failed, input_path, now=None):
    """纯组装草稿文本。sections: [(repo, surveyed_at, body)]；failed: [(repo, error)]。"""
    now_s = (now or datetime.now()).isoformat(timespec="seconds")
    valid_n = len(sections) + len(failed)
    lines = [
        "# 知识库草稿：全仓调研档案（build_kb.py 自动生成）",
        "",
        f"> 生成时间：{now_s}；来源：{input_path}",
        f"> 有效档案 {valid_n} 条：综合成功 {len(sections)} / 综合失败 {len(failed)}；"
        f"另有 {len(skipped)} 条调研失败档案被跳过。",
        "> 本文件是**草稿**：人工审核后手动并入正式知识库，build_kb.py 不直接改写 detail/精炼层。",
        f"> 分层文档流程见 docs/kb-sop.md；禁写清单与禁推测约束见 {REFINED_KB}。",
        "",
        f"## detail 层草稿（每仓一节，审核后并入 {DETAIL_KB}）",
        "",
    ]
    for i, (repo, surveyed_at, body) in enumerate(sections, 1):
        title = f"### {i}. {repo}"
        if surveyed_at:
            title += f"（调研于 {surveyed_at}）"
        lines += [title, "", body.strip(), ""]
    lines += [
        f"## 精炼层摘要建议（审核后并入 {REFINED_KB} 对应章节）",
        "",
        summary.strip() if summary else "（精炼摘要生成失败，请重跑 build_kb.py）",
        "",
    ]
    if skipped or failed:
        lines += ["## 待重跑（本次未进草稿）", ""]
        for e in skipped:
            err_flat = e["error"].replace("\n", " ")  # 反斜杠转义不能进 f-string（3.9 兼容）
            lines.append(
                f"- {e.get('repo', '（未知名）')}（调研失败）：{err_flat} → 修复后重跑 repo_survey.py，再跑 build_kb.py"
            )
        for repo, err in failed:
            err_flat = err.replace("\n", " ")
            lines.append(f"- {repo}（综合失败）：{err_flat} → 重跑 build_kb.py")
        lines.append("")
    return "\n".join(lines)


MERGE_GUIDE = f"""[合并指引] 草稿仅是建议稿，人工审核后手动合并（详见 docs/kb-sop.md）：
  1. detail 层：逐节核对事实与调研档案 JSON 一致 → 按主题归组并入 {DETAIL_KB}
     （编号/分组按正式文档顺序调整）。
  2. 精炼层：把「精炼层摘要建议」核对后并入 {REFINED_KB} 对应章节
     （不要整段覆盖；禁写清单与禁推测约束继续生效，冲突以正式文档为准）。
  3. 合并完成后可删除本草稿（或留作本轮变更记录）。"""


def run(input_path, output_path, llm_call=None, cfg=None, now=None):
    """主流程：读档案（fail-fast）→ 逐仓综合 detail 节 → 综合精炼摘要 → 草稿落盘 + 打印合并指引。
    LLM 可注入；单仓/摘要失败不中断整批（失败仓库列入草稿待重跑节）。
    全部仓库综合失败时无草稿可交，报错退出。返回草稿文本。"""
    if llm_call is None:

        def llm_call(msgs, c):
            return call_llm(msgs, c)

    cfg = cfg or {}
    entries = load_entries(input_path)
    valid, skipped = split_entries(entries)
    if not valid:
        print(
            f"[错误] {input_path} 里没有有效档案（共 {len(entries)} 条，全部带 error 或为空）。\n"
            "请先修复并重跑 repo_survey.py。"
        )
        sys.exit(1)
    print(f"[build] {input_path}：{len(entries)} 条档案（有效 {len(valid)} / 调研失败跳过 {len(skipped)}）")
    sections, failed, sectioned_entries = [], [], []
    for e in valid:
        repo = e.get("repo", "（未知名）")
        print(f"[section] {repo} ...")
        try:
            out = extract_json(llm_call(build_section_messages(e), cfg))
            body = (out.get("section") or "").strip()
            if not body:
                raise ValueError("section 为空")
        except Exception as exc:
            print(f"  [失败] {repo}: {exc}")
            failed.append((repo, str(exc)))
            continue
        sections.append((repo, e.get("surveyed_at", ""), body))
        sectioned_entries.append(e)
    if not sections:
        print("[错误] 所有仓库综合失败，无草稿可交。请检查 LLM 服务后重跑。")
        sys.exit(1)
    summary = None
    print(f"[summary] 综合 {len(sectioned_entries)} 仓 → 精炼摘要建议 ...")
    try:
        out = extract_json(llm_call(build_summary_messages(sectioned_entries), cfg))
        summary = (out.get("summary") or "").strip() or None
        if summary is None:
            print("  [警告] 精炼摘要为空，草稿留占位说明")
    except Exception as exc:
        print(f"  [警告] 精炼摘要生成失败（detail 节不受影响）: {exc}")
    text = render_draft(sections, summary, skipped, failed, str(input_path), now=now)
    write_text_atomic(output_path, text)
    print(f"[done] 草稿 → {output_path}（detail {len(sections)} 节 / 综合失败 {len(failed)}）")
    print(MERGE_GUIDE)
    return text


def main():
    parser = argparse.ArgumentParser(
        description="知识库构建：调研档案 → detail 层条目 + 精炼层摘要草稿（#14 T3，草稿供人工审核合并）"
    )
    parser.add_argument(
        "--input", default=None, help="调研档案 JSON 路径（默认 private/data/repos_detail.json，repo_survey.py 产物）"
    )
    parser.add_argument(
        "--output", default=None, help="草稿输出路径（默认 private/kb/draft/repos_detail.draft.md；重跑覆盖已有草稿）"
    )
    args = parser.parse_args()
    _name, _prefix = resolve_active_profile()
    print(profile_line(_name, _prefix))
    input_path = Path(args.input) if args.input else private_path("data/repos_detail.json", _prefix)
    output_path = Path(args.output) if args.output else private_path("kb/draft/repos_detail.draft.md", _prefix)
    run(input_path, output_path, cfg=llm_config())


if __name__ == "__main__":
    main()
