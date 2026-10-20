# Implementation Workflow

> 代码、脚本、迁移、测试基础设施或 Agent 行为规则的实施入口。核心原则：**Planning Scale 决定是否创建 Spec/Tickets，Risk 决定验证强度。**

## 阅读路径（context pointer）

- **每次实施必读**：§1 授权与自动推进、§3 Planning Scale、§4 Risk——两次分流定整条路线。
- **走到对应阶段才读**：§5 决策阶段、§6 Implement 与 TDD、§7 Verification Gate、§8 Review。
- **收尾与排障时读**：§9 收尾顺序、§10 Gate 语义（判 N/A 或 Exception 时）、§11 Context Recovery（compact/handoff 后）。
- §2 的 Skill 调用契约在第一次需要 execute-by-reference 时读。

## 1. 授权与自动推进

1. 用户明确要求实施、修改、修复或开始后，才可改文件。
2. 用户只要求分析、review、调研或方案时，停在实施前。
3. 获得实施授权后，Agent 自动完成后续阶段，不要求用户逐条输入 Matt Pocock slash command。
4. 仅在以下情况暂停：
   - 关键产品决策仍有多个合理答案；
   - prototype 需要用户选择；
   - 工作流到达规定的 HITL 检查点；
   - 即将执行不可逆操作或 consequential external action；
   - 环境、凭据或缺失信息导致无法继续。

## 2. Matt Pocock Skill 调用契约

本项目使用 Matt Pocock `skills` v1.2.3 的调用分类。

### Model-invoked

`grilling`、`domain-modeling`、`prototype`、`diagnosing-bugs`、`research`、`tdd`、`codebase-design`、`code-review` 可由 Agent 通过 Skill tool 调用。

### User-invoked

`ask-matt`、`grill-with-docs`、`wayfinder`、`to-spec`、`to-tickets`、`implement` 带有 `disable-model-invocation: true`。当本文件要求 Agent 自动执行其中一个阶段时，采用 **execute-by-reference**：

1. 读取已安装的 skill 文件（`.agents/skills/<name>/SKILL.md` 或本机 skills 目录）。
2. 以该文件为方法来源，执行适用步骤和完成标准。
3. 应用本文件明确列出的项目覆写。
4. 如实描述为“按 `<name>` 执行”，不得声称 Skill tool 已调用它。

Matt skill 安装副本保持上游原样，由 `npx skills update` 统一更新；本项目的长期适配只写在本文件这一处。

> **Skill / Subagent 降级**：决策与实施阶段依赖的 skill（`grill-with-docs`、`to-spec`、`to-tickets`、`implement`）或 subagent 不可用时，主 agent 直接自己执行该步骤。判定信号：skill 不在可用列表；subagent 缺少该步骤必需的工具（例：`code-review` 需要 shell 执行 `git diff`，只读型 subagent 拿不到 diff，只能按文件内容推断）；或 subagent 返回的结论是推断而非真实产物。降级后该阶段的完成标准不变——不跳过、不降低要求，并在最终汇报标注「阶段 N 降级为主 agent 执行 + 原因」。（自 ee93a74 迁入，原落在旧 AGENTS.md Step 1）

## 3. 第一次分流：Planning Scale

选择能可靠完成任务的最低等级。代码量和目录不是判定标准。

| 等级                              | 判定                                                  | 路线                                                      |
| --------------------------------- | ----------------------------------------------------- | --------------------------------------------------------- |
| **S0 Read-only**                  | 未获实施授权，或任务仅为解释、review、调查            | 检查并汇报，不改文件                                      |
| **S1 Single-session**             | 决策和实施可在一个 context window 内完成              | 必要时 Grill，随后直接 Implement；可跳过独立 Spec/Tickets |
| **S2 Multi-session, clear route** | 路线已清楚，但实施需多个 context windows              | Grill（按需）→ Spec → Tickets → 每 ticket 独立 Implement  |
| **S3 Multi-session, foggy route** | 目的地可命名，但规划本身跨 session 且存在大量未决路径 | Wayfinder → 路线清晰后进入 S2                             |

### S1 完成标准

- 当前上下文写明 scope、acceptance criteria、test seams 和适用的风险场景。
- 实施、验证、review 和修复循环能在本 session 内完成。
- 即使 Risk 为 R3，也不因形式要求强制创建 Spec/Tickets。

### S2 完成标准

- Spec 是跨 session 的 durable contract，记录已决定行为、seams、风险、out of scope 和完成标准。
- Tickets 是 tracer-bullet vertical slices，每个可独立演示或验证，并适合一个 fresh context。
- 每个 ticket 声明 blocking edges、demo path，以及在基线状态下会失败的 acceptance criteria。

### S3 完成标准

- 先确定有边界的 Destination。
- Map 只保存索引；decision ticket 保存答案；Fog 只保存尚无法准确提问的区域；Frontier 只含未阻塞、未领取的 ticket。
- Wayfinder ticket 解决决策，不实施产品代码。
- `research` ticket 可 AFK；`grilling` 和 `prototype` ticket 是 HITL。
- Map 清晰后先合成 Spec，再拆 implementation tickets；decision tickets 一律经 Spec 转化后进入 Implement。

若规划能在一个 session 内想清楚，使用 S1，不使用 Wayfinder。若路线清楚但实施跨 session，使用 S2，不使用 Wayfinder。

## 4. 第二次分流：Risk

Planning Scale 与 Risk 独立判定。

| 等级             | 判定                                           | Assurance Gate                                                                                                           |
| ---------------- | ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| **R0 Read-only** | 无文件修改                                     | 无实施 gate                                                                                                              |
| **R1 Low**       | 不改变运行时行为、契约、数据流、授权或发布路径 | 定向检查 + diff 自审                                                                                                     |
| **R2 Standard**  | 有界的普通行为变化                             | acceptance scenarios + 可测试行为 TDD + affected tests + review                                                          |
| **R3 High**      | 下列任一高风险触发                             | impact/scenario analysis + observed, testable failure baseline + relevant full verification + runtime/real-data evidence |

### R3 触发

- 认证、授权、角色或 secret 边界；
- migration、数据删除、不可逆状态变化；
- 公共接口、持久化 schema、跨组件或跨 step 契约；
- 发布、外部写入、用户可见生产路径；
- 多消费者共享模块，失败会静默产生错误内容或错误权限。

R3 实现前以真实产物记录失败基线：失败的自动化测试、可重复的 runtime/real-data 复现记录，或对旧状态 fail 的静态/运行时 gate——口头声称“可以变 red”不构成基线。直接复现会破坏真实数据或触发不可逆操作时，用隔离 fixture、dry-run 或既有失败 artifact 提供同等证据。

## 5. 决策阶段

### Grill

仅当需求、边界、seam 或产品行为仍未决定时，按 `grill-with-docs` execute-by-reference：

1. 调用 `grilling` 与 `domain-modeling`。
2. 事实由 Agent 查询，决策交给用户。
3. 使用 round-based frontier，一轮只问当前可回答的问题并附推荐答案。
4. R2/R3 按 `docs/conventions/scenario-enumeration-checklist.md` 检查适用场景。
5. 用户确认 shared understanding 后结束。

路线已由 issue、明确 acceptance criteria 或刚完成的讨论充分决定时，直接进入下一阶段（Spec 或 Implement）。

### Prototype Detour

只有谈话无法回答状态模型或视觉选择时调用 `prototype`。原型必须放在隔离 worktree 的 `prototype/<name>` 分支，遵守 `docs/agents/git-workflow.md`，不得在当前工作目录切分支。

### Spec

只在 S2/S3 执行 `to-spec` by reference。项目覆写：

- 默认创建本地 active spec；只有用户明确授权 tracker 写入时才发布 GitHub Issue。
- Spec 必须吸收已决定内容，不得重新开启访谈或发明新要求。
- R3 必须包含 Modified Files Impact 和 Behavioral Scenarios。
- Spec 创建后立即进入 Tickets，不要求额外人工确认，除非出现新决策。

### Tickets

只在 S2/S3 执行 `to-tickets` by reference。项目覆写：

- Agent 先自审 granularity、blocking edges、demo path 和 acceptance criteria。
- 仅当拆分会改变产品范围，或存在多个 materially different 拆法时询问用户。
- 默认使用项目现有 active ticket 文档；发布 GitHub Issues 需要用户明确授权。
- 一个 ticket 一个 fresh context；wide refactor 使用 expand–migrate–contract。

## 6. Implement 与 TDD

按 `implement` execute-by-reference，一次只实施一个 S2/S3 ticket；S1 实施当前已确认 scope。

1. 读取 contract，复述本次 scope，记录 pre-work Git baseline。
2. **bug 任务先执行根因搜索**：搜索相关定义与所有调用方；仅当该不变量属于公共 seam 时在 seam 处修复根因，否则在各调用方分别修复。随后按选择顺序决定实现路径——在读完被改动代码的真实调用流程、并满足已确认行为和验证义务的前提下，依次考虑：无需新增代码、契约匹配的现有实现、标准库或平台原生能力、合适的已装依赖、最小自定义实现。若需新增代码，选择最高、最稳定的 public seam；seam 本身是设计问题时调用 `codebase-design`。不得用少行替代正确性、信任边界校验、防数据丢失的错误处理、安全、可访问性或测试。
3. 对可确定且可自动验证的行为调用 `tdd`：
   - 一个 slice 一次 red → green；
   - red 必须命中本次行为，而不是附近错误；
   - 只写足以变 green 的实现；
   - review 发现设计问题后再 refactor，并保持测试 green。
4. 每个 scenario 都是 verification obligation，但不等于必须各写一个自动化测试；按 `docs/conventions/scenario-matrix.md` 指定可追溯 evidence。
5. **对同一文件的多次编辑必须串行执行，不得放进同一批并行工具调用**：并行调用各自基于同一旧内容读-改-写回，后写者会静默冲掉先写者（2026-09-03 事故：hook 文件的两个编辑并行发出，登记校验块被头部注释编辑覆盖，测试 red 后才暴露）。每轮编辑后核对全部预期改动都落在工作区（grep 关键标记），再进入下一步。
6. 运行定向验证，完成 ticket checklist，然后按 `docs/agents/git-workflow.md` 创建 atomic commit。

难复现 bug 先调用 `diagnosing-bugs` 建立 tight red-capable loop，再进入本节。

## 7. Verification Gate

所有代码改动至少运行 affected tests。已知环境性失败（隔离重跑即绿、缺本地工具、版本漂移）见 `docs/conventions/test-env-baseline.md`——解释失败前先对照该清单。按影响触发以下检查：

| 改动                                        | 必需验证                                                                                       |
| ------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| 仅 Agent 文档                               | `node scripts/lint-doc-hierarchy.mjs`、指针检查、`git diff --check`                             |
| 应用代码 / 配置                             | affected tests、full test suite（`pytest`）、lint                                                              |
| Python 代码                                 | affected Python tests（优先项目既有 `pytest`/runner）；涉及真实数据或外部服务时再加 smoke       |
| Shell 脚本 / git hooks                      | `bash -n` 语法检查 + 在临时 repo 或 dry-run 下走一次关键路径                                    |
| 涉及外部真实数据 / 媒体 / 跨进程边界        | 相应 consumer tests + 真实数据 smoke（投递链：dry-run/单平台限量先行）                          |

找不到真实数据时，明确报告“无真实数据可用”和原因；mock 全绿的结论一律如实标注为 mock 验证，与真实数据验证分开呈现。

> **止损/超时类阈值的专门义务**：任何「无心跳 N 秒就击杀/落栈/告警」的阈值，必须与**代码里最长的那段合法静默**对表（限速、长休息、轮询等待、外部等待），而不是与注释里的估算对表。单测全绿 + 覆盖率 94% 也证明不了阈值自洽——它需要一次真实运行，或一个**缩样 A/B 探针**（同一份代码只差被修的那个开关，跑出「修复前被自己的阈值杀掉 / 修复后存活」的对照）。#58 即此条的来由。

## 8. Review 与修复循环

`code-review` 只看两个 fixed points 之间的 committed diff，因此顺序固定为：

1. 记录整个工作开始前的 baseline commit。
2. 每个已验证 ticket 创建 atomic commit。
3. review 开始前立即记录当前任务终点 `taskHead = git rev-parse HEAD`；对 `baseline...taskHead` 调用 `code-review`，分别输出 Standards 与 Spec。不得把会继续移动的裸 `HEAD` 当作 review 终点。
4. 每个 accepted finding 回到 Implement/TDD，修复并重新验证。
5. 修复 commit 后记录新的 `taskHead`，重新审查 `baseline...taskHead` 受影响部分；已经发布的历史只追加 commit。
6. 重复修复、验证与固定终点 review，直到没有阻塞 finding。

Review 报告的每个 finding 都要有 disposition（已修复 / 推迟＋理由 / 驳回＋理由），形成闭环。

## 9. 收尾顺序

1. 所有 ticket acceptance criteria 与 checklist 和实际验证结果一致。
2. 运行最终 Verification Gate。
3. 更新相关执行文档；S2/S3 完成后归档已结束工作的 spec、tickets 和 review，并更新 `docs/DOCS-INDEX.md`。
4. 为归档和文档同步创建 final documentation commit。
5. 只有用户已授权这次 push 时才 push。
6. 只有存在对应 GitHub Issue 且用户已授权 tracker 写入时才更新或关闭 issue。
7. 最终汇报先以**产品视角**讲清这个 issue/feature 原本要什么、用户现在看到什么，再列 completed gates、evidence、remaining blockers 和真正 N/A 的条件分支。

**Session 结束验证**（逐条确认，未完成的当场补做或显式标注「跳过 + 原因」，汇报时不得遗漏）：

- [ ] Grill 完成（有 spec 或对话记录佐证）
- [ ] Spec 完成（有 spec 文件，含 Scenario Matrix；S1 可 N/A）
- [ ] Tickets 完成（S1 可 N/A）
- [ ] TDD 完成（测试 red → green → refactor）
- [ ] Code Review 完成（有审查报告）
- [ ] Runtime Verify 完成（有运行时验证证据：截图 / DOM 检查 / API 响应 / E2E 结果）
- [ ] Commit 完成（有 commit hash）
- [ ] 文档及 Issue 更新完成
- [ ] 产品视角结论完成（有前后行为对照）

## 10. Gate 语义

- **Hard gate**：未满足必须停止，例如实施授权、失败测试、内容发布 HITL、不可逆操作确认。
- **Conditional gate**：触发条件不成立时标为 N/A，不称为“跳过”，例如 S1 的 Spec/Tickets、非 UI 的浏览器验证。
- **Exception**：本应执行但用户明确批准豁免。记录被豁免项、原因和残余风险。

流程步骤的省略只有两种合法形态：Conditional N/A（触发条件不成立，标注 N/A）或用户批准的 Exception（记录豁免项、原因与残余风险）。

## 11. Context Recovery

- S1 默认保持同一 context。
- S2/S3 每个 implementation ticket 使用 fresh context，只加载 ticket、spec、相关 ADR/CONTEXT、Git baseline 和必要代码。
- Spec/ticket、Git 状态/提交和验证输出共同构成 durable source；conversation summary 或 checklist 不能单独证明完成。
- `/compact` 或 handoff 后，先读取 durable sources 并与 `git status`、`git log`、测试结果核对，再继续。
