---
name: triage
description: 对仓库 issue 做 triage：打类别/优先级/领域/状态标签，ready-for-agent 前补齐验收标准。用户要求 triage、整理 issue，或新 issue 进来时使用。
---

# Triage

标签全集与流转规则见 `docs/agents/triage-labels.md`；`gh` CLI 操作契约见 `docs/agents/issue-tracker.md`。本 skill 只写执行步骤，不复制标签定义。

## 步骤

1. 读票：`gh issue view <N> --comments`，正文与评论都读（Body authority：结论落正文）。
2. 打类别标签：`bug` / `enhancement`（恰一个）。
3. 打优先级标签：`P0`-`P3`，按 triage-labels.md 的定义档位。
4. 打领域标签：`area:pipeline` / `area:platforms` / `area:agent-harness` / `area:webui` / `area:docs`（恰一个）。
5. 定状态：
   - 信息不足 → `needs-info`，评论里点名要什么；
   - 可交给 agent → 补齐可机械核验的验收标准后打 `ready-for-agent`；
   - 只能人做 → `ready-for-human`；
   - 拒绝 → `wontfix` 并留解释评论后关票；
   - 拿不准 → 保持 `needs-triage`。

## 完成标准

- 票上恰好：1 类别 + 1 优先级 + 1 领域 + 1 状态（`wontfix` 例外：只留类别）。
- `ready-for-agent` 的票必须有验收标准；补不出就退回 `needs-info`。
- 修订写进 issue 正文，评论只留过程（Body authority）。
