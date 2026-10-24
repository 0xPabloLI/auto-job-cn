# AGENTS.md

## Project

- **Repo**: auto-job-cn — 公开版求职助手框架（引擎 + 文档 + 脚本）。个人数据与平台适配笔记在 `private/`，不入库。
- **Domain language**: read `CONTEXT.md`（懒创建，尚不存在则静默跳过）；domain-document layout and ADR rules are in `docs/agents/domain.md`.
- **Agent harness**: 组件即 `docs/agents/` 下各文档 + `.githooks/` + `scripts/session-launcher.sh`；session-pilot 状态流见 `docs/agents/git-workflow.md` §8 与 hook 头部注释。

## Workflow Router

1. **Read-only work**: reviews, explanations, diagnosis and research do not modify files unless the user also requests implementation.
2. **Implementation authorization**: do not change files until the user explicitly asks to implement, modify, fix or begin. Once authorized, proceed automatically except at a hard gate, following `docs/agents/implementation-workflow.md`.
3. **Agent documents**: before creating, moving, deleting, renaming or structurally changing `AGENTS.md`, `docs/`, a skill or any Agent-reached document, load `writing-for-agents` and apply `docs/DOCS-INDEX.md` placement rules.
4. **Proposals and external facts**: before recommending a code change, architecture, tool, service or model, follow `docs/agents/proposal-review.md`.
5. **Git actions**: before staging, committing, pushing, opening a PR, working across branches or changing context with unfinished work, follow `docs/agents/git-workflow.md`.
6. **Onboarding**: 用户说「初始化 / Setup / Onboarding / 帮我配置」时按 `docs/agents/onboarding.md` 引导新 clone 用户建 Profile；缺 profile 的隐性触发是非阻断提示，不拦截用户请求。

## Hard Safety Gates

- Treat existing tracked and untracked changes as user work. Never overwrite, restore, move, delete, clean or include unrelated work.
- Never switch branches in the current working directory. Use a worktree for cross-branch work.
- Never run stash commands without explicit confirmation in the current chat.
- Keep secrets out of code, logs and responses.
- Consequential external actions, including publishing, messaging, deployment and remote Issue changes, require user authorization for that action.
- **Public boundary**: this repo is published. Personal facts (name, résumé, employers,
  salary floor, target industry, real company/HR names, platform probing notes) live only
  under `private/` and never get committed, logged or quoted. Before any commit or release,
  run `python3 scripts/check_public_boundary.py` (also wired into `.githooks/pre-commit`).
- Public repo is generated, not edited: promote with `python3 scripts/publish/promote_public.py`
  （私有主线 → 脱敏快照 → 全闸门 → 用户确认 → push）。绝不手改公开 checkout，也不把私有提交直接推 public。
  改 `scripts/publish/` 或公开侧守卫前读 `内部发布链文档（未随本仓分发）`。

## Collaboration Preferences

- Prefer Chinese for collaboration text.
- Prefer evidence from logs, APIs, tests and runtime artifacts over speculation.
- If the user requests a plan first, stop after the plan until approval.
- Keep implementation scoped and avoid unrelated refactors.

## Agent skills

### Issue tracker

本仓是公开发布面，**不开 public issue**：票面与评论极易夹带个人数据（真实公司名、HR 称谓、招呼语原文、会话样本）。任务记录写在本地 `private/notes/`（不入库）；缺陷与安全报告走 `SECURITY.md` 的私下渠道。历史票务留在私有主线仓，不迁移、不引用其编号开工。

### Triage labels

Uses the five default triage labels (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout — one `CONTEXT.md` + `docs/adr/` at the repo root, created lazily by `/domain-modeling`. See `docs/agents/domain.md`.

### Profile（多简历/多账号，#28）

多 Profile 隔离由 `scripts/_profile_paths.py` 统一解析：`AUTOJOB_PROFILE` env > `private/profiles.json`
注册表 `default` > 唯一 profile；多 profile 无 default 脚本拒绝启动。Agent 侧规则：**用户指名的
profile 先查注册表，查不到就问，不代猜；未指名用 default**；跨 profile 事实由 `pre_send_gate`
白名单机械兜底。新 Profile 走 onboarding（「加个 XX 的 Profile」）。

### 投递流程（prompt 驱动）

用户说「投简历 / 实时投简历 / 开始投简历」都走 live_apply 实时浏览投递——这是唯一的投递路径
（2026-09-10 用户约定：挂机 backup 链路已从流程移除，不再有挂机模式）。均按 runbook 执行
（→ followup+learn → 汇报）。See `docs/agents/apply-runbook.md`.
用户说「回复入站 / 回那批来找我们的」走同一份 runbook 的「入站回复」小节
（`scripts/reply_inbound.py`，#66 阶段 2：默认 dry-run 零点击，发送需当次授权）。
