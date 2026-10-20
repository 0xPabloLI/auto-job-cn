# 知识库构建 SOP（分层文档 + 草稿审核）

> 知识库是 gen_greetings.py 的唯一事实来源，分两层维护。本 SOP 描述从 repo 调研到知识库更新的完整流程；
> 设计背景见 docs/specs/pipeline-spec.md（#14），全部文档索引见 docs/DOCS-INDEX.md。

## 分层结构

| 层 | 文件 | 内容 | 谁读 |
|----|------|------|------|
| 精炼层 | `private/profile_background.md` | 招呼语直接引用的锚点、硬事实清单、禁写清单、禁推测约束、行业语境结论 | gen_greetings.py 全量注入 |
| detail 层 | `private/kb/repos_detail.md` | 每仓完整档案（定位/技术栈/架构/硬事实/风险），保留"未读到"标注 | 人工与 agent 会话按需查阅 |

材料分配规则：

- 单仓完整事实（逐条硬事实、文件级佐证、疑点）→ detail 层，每仓一节；
- 跨仓提炼的结论（项目组定位、招呼语锚点、行业坐标系对照）→ 精炼层对应章节；
- 行业语境/推特等外部调研全文放 L2 research 文档（按 DOCS-INDEX 索引查找），精炼层只留带来源注记的结论章节；
- 一条事实只维护在一处：detail 层放全量，精炼层只放提炼后的引用；改事实先改 detail 层，再同步精炼层。

## 构建流程（脚本段）

1. `python3 scripts/repo_survey.py` → `private/data/repos_detail.json`（逐仓调研档案，失败条目带 error）
2. `python3 scripts/build_kb.py` → `private/kb/draft/repos_detail.draft.md`（草稿：detail 条目 + 精炼摘要建议 + 待重跑清单）
3. 完成标志：stdout 打印合并指引，草稿文件内容可读、待重跑清单为空或已确认。

## 人工审核合并（HITL 卡点）

草稿只是建议稿，build_kb.py 不直接改写正式知识库。逐节核对后手动合并：

1. **并入 detail 层**：核对草稿每节事实与 repos_detail.json 字段一致（数字/链名/版本逐字核对）→
   按主题归组并入 `private/kb/repos_detail.md`（编号与分组按正式文档顺序调整）。
   完成标志：草稿里每节要么已并入正式文档，要么已列入待重跑。
2. **并入精炼层**：把「精炼层摘要建议」并入 `private/profile_background.md` 对应章节
   （项目组小节与硬事实清单），不要整段覆盖；与禁写清单冲突时以正式文档为准。
   完成标志：新事实进 `private/kb/fact_cards.md`（含白名单区，跑 `gen_greetings.py doctor`
   核对）后，agent 起草即可引用。
3. **清理**：合并完成后删除草稿（或留作本轮变更记录）。

## 禁推测约束怎么守

- 每条事实标注来源：repo 文件名 / 链上地址 / 实测 HTTP 状态；来源不足的内容标「未读到」或直接不写。
- 调研与综合只引用输入字段里已有的事实，LLM 产出（repo_survey 档案、build_kb 草稿）必须经上节人工核对才能进入正式层。
- 招呼语里出现知识库没有的说法时：把该说法追加进精炼层禁写清单，再补调研，让约束沉淀回知识库。

## 白名单同步（#25）

规则扫描 gate 的白名单（`private/kb/fact_cards.md` 文末 JSON 块）由 `doctor --sync` 从卡片编译维护，
无需手改 JSON：新审事实写进卡片 → `python3 scripts/gen_greetings.py doctor` 看 diff（年限/实体候选
+ 知识库缺口）→ 核对后加 `--sync` 自动入表。表仍是 gate 唯一来源：推导只提议、入表必须显式确认；
否定标注片段（✗/❌/不可直接主张/缺口/过气等）不产出候选；时间段（如 2011-2015）自动编译出可用年限
（span 与 span-1）。手工增删条目仍允许（--sync 做 union 合并，不回退手工内容）。

## 何时重跑

- repo 新增或大改（新语言栈、新部署形态）→ 重跑 repo_survey.py（按 repo 名 upsert）→ build_kb.py；
- loop 信号（followup 回复率低于阈值）由 `scripts/loop.py` 触发，loop 后人工审核 → orchestrate --stages promote,apply，回 build_kb 补知识库；
- 行业语境/推特调研更新 → 直接人工更新精炼层对应章节（不经 build_kb.py，它只处理 repo 调研档案）；
- 非 repo 材料（作品集 URL/PDF/文本）新增或更新 → `scripts/portfolio_survey.py`（#26，按 source upsert
  出 `private/data/portfolio_archive.json` 结构化档案）→ 会话 agent 读档案，走上面「人工审核合并」
  卡点并入精炼层（不经 build_kb.py；多模态材料它显式报错，仍走会话内直接阅读）。
