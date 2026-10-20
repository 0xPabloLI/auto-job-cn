# Pipeline 化 Spec（#14）

> **历史留档（L3）**：本 spec 完成使命后归档，不再维护。文中 `gen_greetings.py generate`
> 阶段已退役（本地 LLM drafter 删除，草稿由 agent 按 `apply-runbook.md`「起草规范」撰写，
> 2026-09-10 用户约定）；挂机链路（crawl_jds/batch_apply_v2）已从流程与文档移除，
> 唯一投递路径是 live_apply（#20/#23，2026-09-10 用户约定）。
> 现行架构以 `docs/agents/apply-runbook.md` 与 README 为准；下文描述退役时的历史形态。

## 背景

当前 auto-job-cn 已有链路：`crawl_jds.py → gen_greetings.py(generate→promote) → batch_apply_v2.py → followup_tracker.py → gen_greetings.py learn`。但前置环节（repo 调研/知识库构建/行业语境/推特）是 agent 会话内做的，无脚本；缺一键编排和可复用性。

## 目标

- 一键 `python3 scripts/orchestrate.py` 跑全链路（调研→知识库→匹配→生成→审核→投递→反馈→learn）
- HITL 只在草稿审核 + 投递前确认两个卡点
- 效果不好时 loop 回知识库/生成环节
- 别人 clone 后填自己的 repo/知识库/平台凭据就能跑

## 当前链路（已有）

| 环节 | 脚本 | 输入 | 输出 | 状态 |
|------|------|------|------|------|
| 爬 JD | `crawl_jds.py` | 站点搜索 URL | `raw_jds.json` | ✅ |
| 生成草稿 | `gen_greetings.py generate` | `raw_jds.json` + 知识库 | `greeting_drafts.json`（pending_review） | ✅ |
| 草稿审核 | 人工 | `greeting_drafts.json` | status=approved | ✅ HITL |
| promote | `gen_greetings.py promote` | approved 草稿 | `greetings.json` | ✅ |
| 投递 | `batch_apply_v2.py` | `greetings.json` | `apply_log.jsonl` | ✅ |
| 跟进 | `followup_tracker.py` | 投递历史 | `followup_report.json` | ✅ |
| learn | `gen_greetings.py learn` | followup + history | `greeting_memory.json` | ✅ |

## 缺口（待补）

| 缺口 | 说明 |
|------|------|
| 前置调研脚本化 | repo 全仓调研 / 行业语境 / 推特调研当前在 agent 会话内做，无脚本 |
| 一键编排 | `orchestrate.py` 串联全链路 + HITL 卡点暂停 |
| 推特调研标准化 | 多策略（x.com SSR / nitter / CDP）+ 反爬 + JSON 输出 |
| 知识库构建 SOP | 分层文档（精炼层 + detail）构建流程文档化 |
| RAG 演进决策点 | KB 增长后全量注入 vs embedding RAG 的决策（归 #15 T1） |

## 目标链路

```
[调研阶段]
  repo_survey.py      → repos_detail.json（全仓调研档案）
  industry_research.py → web3-hiring-context.md + web3-tech-coordinate.md
  twitter_survey.py   → twitter_profile.json
[知识库阶段]
  build_kb.py         → repos_detail.draft.md（草稿，禁推测约束）→ 人工合并 → 精炼层/detail 层
[匹配生成阶段]
  crawl_jds.py        → raw_jds.json
  gen_greetings.py generate → greeting_drafts.json（pending_review）
  --- HITL 卡点 1：草稿审核 → status=approved ---
  gen_greetings.py promote → greetings.json
  --- HITL 卡点 2：投递前确认 ---
[投递阶段]
  batch_apply_v2.py   → apply_log.jsonl
[反馈阶段]
  followup_tracker.py → followup_report.json
  gen_greetings.py learn → greeting_memory.json
[loop]
  效果不好 → 回 build_kb / generate（带 memory）
```

## HITL 卡点

1. **草稿审核**：`gen_greetings.py generate` 后暂停，人工审核 `greeting_drafts.json` 改 status=approved/reject，然后继续
2. **投递前确认**：`promote` 后暂停，人工确认 `greetings.json` 内容 + 投递数量，然后 `batch_apply`

`orchestrate.py` 在卡点处 `input("审核完成按回车继续...")` 暂停。

## loop 机制

- followup 回复率 < 阈值 → 触出 loop 信号
- loop 回 `build_kb`（补知识库）/ `generate`（调 prompt，带 memory 避重复表达）
- loop 次数上限防死循环

## 可复用性

- `config.example.yaml`：repo 列表 / 行业关键词 / 平台凭据 / 投递参数模板
- `README.md` 补"clone 后 3 步跑起来"：填 config → 跑 orchestrate → HITL 审核
- 知识库占位：`docs/templates/profile_background.example.md` 模板

## 依赖

- #13 文案标准已定（8cfef7f）
- #9 小批量投递已验证（pipeline 的端到端测试）✅
- #15 T1 RAG 方案（KB 增长后的演进决策，可不阻塞 v1）

## Tickets 拆分

> 2026-09-07 用户拍板：T4/T5 v1 范围为**完整自动抓取**（脚本内置 CDP/多策略抓取 + LLM 综合），非"素材→综合"半自动方案。

| Ticket | 内容 | 依赖 |
|--------|------|------|
| T1 | `orchestrate.py` 一键编排 + HITL 卡点暂停 | T2-T6 |
| T2 | `repo_survey.py` repo 全仓调研脚本化（封装 agent 调研流程） | — |
| T3 | `build_kb.py` + 知识库构建 SOP 文档（分层文档流程） | T2 |
| T4 | `industry_research.py` 行业语境调研脚本化（封装 web-deep-research） | — |
| T5 | `twitter_survey.py` 推特调研标准化（多策略 + JSON 输出） | — |
| T6 | `config.example.yaml` + README 可复用性 | T1-T5 |
| T7 | loop 机制（followup 回复率 → 回 build_kb/generate） | T1, followup_tracker |

v1 不含 RAG（#15 T1），全量注入知识库；KB 增长后再评估。