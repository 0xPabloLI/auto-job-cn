# Documentation Index

> Source of truth for doc ownership. Use it to find any document in one lookup.
>
> 本仓是公开发布面：个人数据、平台探测笔记、历史 ticket/roadmap 不进这里，留在本地 `private/`。新增文档前先问一句「陌生人读到这一页会不会读到我个人」。
>
> _Last inventory: 2026-10-20._

## Canonical Structure

### Layer Definitions

| Layer                       | What goes here                                                                                                       | What does NOT go here                                                                                               | Who reads it                                    |
| --------------------------- | -------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------- |
| **L0: AGENTS.md** (必读)    | Pointers + top-level rules only                                                                                      | No technical details                                                                                                | Agent every session start                       |
| **L1: Execution reference** | Execution instructions: what to do, what params to use, how to configure. Loaded on-demand when doing that workflow  | Research rationale, parameter derivation, multi-source comparison, methodology — push to L2, pointer from L1 bottom | Agent loaded on-demand when doing that workflow |
| **L2: Deep research**       | Research rationale: why this was chosen, how params were derived, what sources were surveyed, methodology comparison | Execution instructions — extract to L1 as pointer targets                                                           | Agent only when deep-diving into specific topic |
| **L3: Archive**             | Completed work: specs, tickets, plans, roadmaps — retained for reference, no longer maintained                       | No active reference material                                                                                        | Historical reference only                       |

### Layer Placement Rules

创建、移动、删除、重命名或结构性修改 `AGENTS.md`、`docs/`、skill 或其他 Agent-reached 文档前，先加载 `writing-for-agents`；随后应用本节的 Layer Placement 检查：

1. **L1/L2 boundary**: 执行文档只写“做什么、用什么参数”；研究依据、参数推导和方法比较放 `内部调研笔记目录（未随本仓分发）`，并由 L1 底部的 Design Decisions 指向。
2. **New document: ask first.** Before writing, ask: "Is this execution instructions or research rationale?" → execution → L1; research → L2.
3. **Modifying an L1 doc: check for L2 intrusion.** If research rationale has crept in, extract it to L2 and add a pointer in the L1 doc bottom.
4. **L2 docs: no execution instructions.** L2 writes "why this was chosen, how params were derived, what was surveyed" — execution instructions belong in L1 as pointer targets.
5. **Sync DOCS-INDEX after changes.** After creating, moving, or deleting any doc, add/remove/rename the corresponding table row here. Run `node scripts/lint-doc-hierarchy.mjs` to verify.
6. **Compression/restructure audit.** Check (a) cross-section and cross-document qualifiers for contradictions, (b) every moved information field has a destination rather than merely an existing pointer, and (c) every changed local pointer resolves to a real file.

### Root — Active reference (AGENTS.md pointers)

| Document                                   | Purpose                                                                     | Referenced by                              |
| ------------------------------------------ | ---------------------------------------------------------------------------- | ------------------------------------------ |
| `docs/agents/apply-runbook.md`             | 投递 Runbook（prompt 驱动）— 「开始投简历」触发的标准执行步骤                  | AGENTS.md / 用户说开始投简历时             |
| `docs/agents/apply-forensics.md`           | 投递取证与跳岗台账（卡死三份证据的读法与四格判读）— 会话被看门狗击杀时才读        | apply-runbook 阅读路径 / `scripts/_forensics.py` |
| `docs/agents/apply-tuning.md`              | 投递节奏与额度调档（会话级开关 + 日上限阶梯）— 要偏离默认或降档时才读             | apply-runbook 阅读路径 / 步骤 1                   |
| `docs/agents/integration-smoke.md`         | 集成/E2E 冒烟步骤 — dry-run 到发送前 gate（零发送，agent 可跑）/ 真实限量冒烟（人工留证）（#40） | #40 人工触发 / Wave 准出门禁 / live_apply 改动回归 |
| `docs/agents/onboarding.md`                | Onboarding Runbook（prompt 驱动）— 「初始化/Setup」触发的 Profile 引导：资料收集→个人层→行业归类→事实卡片→skills 校验→冒烟（#22 Phase 1） | AGENTS.md Workflow Router / 新 clone 首次使用 |
| `docs/greeting-prompt-template.md`         | Greeting prompt 模板（求职自动化产出的问候语模板）                           | scripts 中的打招呼流程                     |
| `docs/specs/pipeline-spec.md`                | Pipeline 化 spec（#14）— 全链路设计 + HITL 卡点 + tickets 拆分（L3）        | #14 implementation                         |
| `docs/kb-sop.md`                             | 知识库构建 SOP — 分层文档流程/detail·精炼层材料分配/草稿审核合并/禁推测约束/重跑时机（L1） | repo_survey → build_kb 流程 / #14 T3       |
| `docs/templates/profile_background.example.md` | 知识库精炼层占位模板 — clone 后复制到 private/profile_background.md 填写（private/ 不入库，模板放 tracked 的 docs/templates/） | README 快速开始 / #14 T6 |

### `agents/` — Agent harness（执行参考，L1）

| Document                                      | Purpose                                                                   | Referenced by                     |
| --------------------------------------------- | -------------------------------------------------------------------------- | --------------------------------- |
| `docs/agents/issue-tracker.md`                | GitHub Issues 约定与 `gh` CLI 操作                                        | to-tickets / triage / wayfinder   |
| `docs/agents/triage-labels.md`                | triage 标签全集（类别/状态/优先级/领域）与流转                | triage                            |
| `docs/agents/domain.md`                       | 领域文档消费规则 + multi-context 布局（CONTEXT-MAP.md）                    | 探索代码前的入口                  |
| `docs/agents/git-workflow.md`                 | git 安全基线、worktree、stash、commit 节奏、Session-Id、并行 session      | AGENTS.md Hard Gates / git 操作前 |
| `docs/agents/git-concurrent-recovery.md`      | 并行 session 竞态应急配方（plumbing，绕过 hooks）                          | git-workflow §9                   |
| `docs/agents/implementation-workflow.md`      | 实施入口 — Planning Scale / Risk 分流、Spec/Tickets/TDD、Verification Gate | 实施授权后                        |
| `docs/agents/proposal-review.md`              | 建议/选型前的证据与影响面检查                                              | AGENTS.md Workflow Router         |

### `conventions/` — 检查与记录约定（L1）

| Document                                        | Purpose                                             | Referenced by                             |
| ------------------------------------------------ | ---------------------------------------------------- | ----------------------------------------- |
| `docs/conventions/fact-verification.md`          | 源码验证链、CLI 确认、定价查询的操作程序             | proposal-review §3                        |
| `docs/conventions/scenario-matrix.md`            | 场景与 evidence 的记录格式                           | implementation-workflow §6                |
| `docs/conventions/scenario-enumeration-checklist.md` | R2/R3 决策阶段的场景类别清单（查什么）          | implementation-workflow §5                |
| `docs/conventions/test-env-baseline.md`         | 已知环境性测试失败的 flaky 清单（先隔离重跑再根因搜索） | implementation-workflow §7                |
