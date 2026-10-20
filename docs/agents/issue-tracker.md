# Issue tracker: GitHub

Issues and specs for this repo live as GitHub issues. Use the `gh` CLI for all operations.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`. Use a heredoc for multi-line bodies.
- **Read an issue**: `gh issue view <number> --comments`, filtering comments by `jq` and also fetching labels.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with appropriate `--label` and `--state` filters.
- **Comment on an issue**: `gh issue comment <number> --body "..."`
- **Apply / remove labels**: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **Close**: `gh issue close <number> --comment "..."`
- **Commit 不动票状态（自动关票陷阱，2026-09-28 #56 实测）**：GitHub 的 closing keyword 会匹配 **commit 标题/正文**里 `关键词 + 票号` 的形态，push 到默认分支即置 `CLOSED / stateReason=COMPLETED`。本仓标题惯例「`fix:` 紧跟 `#<票号>`」正好命中（`fix` 是关键词、冒号后紧跟票号），结果与验收进度无关，且**跳过下条标签清扫**——已闭票上还挂着 `ready-for-agent`/`P0`/`area:*`，污染 open 过滤器。**本文件当初写这条规则时，正文里就带着那个字面串，于是记录规则的 commit 又把票关了一次**（2026-09-29 二次踩中）——所以说明陷阱时用占位写法，别把「关键词 + 真实票号」写进正文。规则：关票只由 `gh issue close` 做（配「交付记录」标记）；commit 里引用票用裸 `#56` 或 `refs #56`，`fix:`/`feat:` 前缀后先写描述再带票号（`fix: 双轴 review 修复轮（#56）…`）。误关后 `gh issue reopen` + 一条说明评论（写明 timeline 里的 `closed` 事件 commit_id，归因不留猜）。
- **Body authority**: the issue body is the authoritative spec. When a plan, scope, or acceptance criteria is revised, edit the body (fold the superseded version into a `<details>` block) and leave a date-stamped comment as the process record. An agent that reads only the body must still get the current plan — never leave a revision only in comments.
- **Delivery-record marker**: 关票交付评论以固定标记开头（`交付记录` / `Delivery record`）——标记是机器可检的唯一部分，记录本身写给人类读。（关票无标记的 CI 提醒属可选机器兜底，本仓未装。）
- **关票标签清扫**：关票时保留 category 标签，剥掉全部 state 标签、`P0`–`P3` 和 area 标签——优先级/领域标签只服务于 open backlog 的取票顺序，留在已闭票上只会污染过滤器。已闭票永远不是取票候选，取票只从 `gh issue list --state open` 出发。
- **GraphQL timeout workaround**: `gh` 的 GraphQL 调用（`gh issue view/edit/close` 用到）经本地代理会间歇超时。改走 REST：读用 `gh api repos/<owner>/<repo>/issues/<num>`；标签变更用 `gh api .../issues/<num>/labels -X PUT` 覆盖全量集合（DELETE 同样会超时，用 PUT 覆盖代替）；关票用 `gh api .../issues/<num> -X PATCH -f state=closed`。
- **Align decision history before claiming**: 认领票前对齐模型/工具裁决史——票面推荐的模型、工具或方案可能已被后续裁决降级或否决。开工前到该名字的调研/决策文档（roadmap 的 Design Decisions 段、`内部调研笔记目录（未随本仓分发）`）查现行裁决（✅ 在用 / 备选 / ❌ 否决），不能只验证环境可行性。

Infer the repo from `git remote -v` — `gh` does this automatically when run inside a clone.

## Pull requests as a triage surface

**PRs as a request surface: no.** _(Set to `yes` if this repo treats external PRs as feature requests; `/triage` reads this flag.)_

When set to `yes`, PRs run through the same labels and states as issues, using the `gh pr` equivalents:

- **Read a PR**: `gh pr view <number> --comments` and `gh pr diff <number>` for the diff.
- **List external PRs for triage**: `gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments` then keep only `authorAssociation` of `CONTRIBUTOR`, `FIRST_TIME_CONTRIBUTOR`, or `NONE` (drop `OWNER`/`MEMBER`/`COLLABORATOR`).
- **Comment / label / close**: `gh pr comment`, `gh pr edit --add-label`/`--remove-label`, `gh pr close`.

GitHub shares one number space across issues and PRs, so a bare `#42` may be either — resolve with `gh pr view 42` and fall back to `gh issue view 42`.

## When a skill says "publish to the issue tracker"

Create a GitHub issue.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> --comments`.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a single issue with **child** issues as tickets.

- **Map**: a single issue labelled `wayfinder:map`, holding the Notes / Decisions-so-far / Fog body. `gh issue create --label wayfinder:map`.
- **Child ticket**: an issue linked to the map as a GitHub sub-issue (`gh api` on the sub-issues endpoint). Where sub-issues aren't enabled, add the child to a task list in the map body and put `Part of #<map>` at the top of the child body. Labels: `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`). Once claimed, the ticket is assigned to the driving dev.
- **Blocking**: GitHub's **native issue dependencies** — the canonical, UI-visible representation. Add an edge with `gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`, where `<blocker-db-id>` is the blocker's numeric **database id** (`gh api repos/<owner>/<repo>/issues/<n> --jq .id`, _not_ the `#number` or `node_id`). GitHub reports `issue_dependencies_summary.blocked_by` (open blockers only — the live gate). Where dependencies aren't available, fall back to a `Blocked by: #<n>, #<n>` line at the top of the child body. A ticket is unblocked when every blocker is closed.
- **Frontier query**: list the map's open children (`gh issue list --state open`, scoped to the map's sub-issues / task list), drop any with an open blocker (`issue_dependencies_summary.blocked_by > 0`, or an open issue in the `Blocked by` line) or an assignee; first in map order wins.
- **Claim**: `gh issue edit <n> --add-assignee @me` — the session's first write.
- **Resolve**: `gh issue comment <n> --body "<answer>"`, then `gh issue close <n>`, then append a context pointer (gist + link) to the map's Decisions-so-far.
