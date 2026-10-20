# Onboarding Runbook（prompt 驱动）

> 触发词：用户说「初始化 / Setup / Onboarding / 帮我配置」→ 按本 runbook 引导新 clone 的用户从零建立可用 Profile。
> 断点续跑：任何一步中断后重新进入，先跑下面的前置盘点，已达标产物直接跳过——每步幂等。
> 隐性触发（非阻断）：会话中发现用户请求涉及投递/生成/KB 操作、而 `private/profile_background.md` 不存在或仍含模板占位文本（「填你自己的」）、或 `private/kb/profiles/` 下没有任何 profile JSON 时，附一句友好建议（「可说『初始化』进入 onboarding 引导」）后**继续按用户原请求工作**——不拦截、同会话不重复提醒。

## 前置盘点（每次进入先跑，产物齐则逐项跳过）

> **机械盘点**：`python3 scripts/onboarding_check.py`（只读）——盘点表全部代码化，
> 每步完成后重跑，**完成与否以脚本退出码为准，不靠 agent 自评**（用户约定 2026-09-10：
> onboarding 要有代码确定性）。下表是各项的语义说明，判据以脚本为准。

| 产物 | 完成标准 |
| --- | --- |
| `config.yaml` | 可选（材料全为非 repo 形式时可缺）；配置了则 repos 指向真实本地路径 |
| `private/profiles.json` | 注册表：`default` + 每 profile 的简历路径（简历**任意命名/任意位置**，注册表记一行；旧 `private/resume.*` 仍兼容，#28） |
| `private/profile_background.md` | §1-§3 已填真实事实，无「填你自己的」占位 |
| `private/kb/profiles/<行业>.json` | industry 字段指向存在的行业层文件；`queries` 为非空搜索词数组（#29 门禁） |
| `private/kb/industries/<行业>.md` | 行业层文件（可为最简骨架） |
| `private/kb/fact_cards.md` | 卡片 + 文末白名单 JSON，`gen_greetings.py doctor` 无缺口 |
| `private/data/excluded_companies.json` | 排除公司关键词数组（没有可排除的公司就是空数组 `[]`） |

## 步骤

0. **仓库就位（用户不会克隆也从这里开始）**：先确认当前工作目录是本仓库根
   （`.git/` 与 `scripts/live_apply.py` 都存在）。不是则引导用户完成克隆：
   问用户想把仓库装在哪个目录 → 代会话目录（agent 自己执行，不让用户手敲命令）：
   `git clone https://github.com/0xPabloLI/auto-job-cn.git <目标目录>` →
   之后所有步骤切到仓库根进行。机器没有 git：给用户两个选项（装 git 命令行，或在
   GitHub 页面 Code → Download ZIP 解压后告诉 agent 解压路径，ZIP 版后续更新靠重新下载）。
   完成：仓库根下 `.git/` 与 `scripts/live_apply.py` 存在，agent 工作目录已切过去。
1. **资料收集**：向用户收集简历（格式不限，原样存 `private/resume.*`，
   agent 自行读取提取，不要求先转任何格式）与**作品与材料**：本地 git repo 绝对路径列表
   （写入 `config.yaml` repos，格式见 `config.example.yaml` 注释；没有本地 repo 可不配置）、
   个人网站/作品集 URL、设计稿、产品文档等——**材料形式不限**，agent 会话内直接阅读后
   按 `docs/kb-sop.md` 纳入知识库（`repo_survey` 脚本只吃本地 git 路径，其余材料用不上它）。
   URL/PDF/文本材料多、想批量脚本化采集时用 `scripts/portfolio_survey.py`（#26：产出
   结构化档案后仍按 kb-sop 走审核合并；docx/pptx/多模态材料它显式报错，仍走会话内阅读）。
   顺带确认目标行业意向与**要排除的公司**（老东家/自家公司等，写入
   `private/data/excluded_companies.json` 关键词数组——apply-runbook「边界」：
   brandName 命中即跳过不投）。
   完成：`onboarding_check.py` 的 config/repos、简历、排除公司各项通过
   （用户没有本地 repo 则 config 项显示「可选未配置」也算过）。
2. **个人层**：按 `docs/templates/profile_background.example.md` 结构，从简历 + repo 事实填
   `private/profile_background.md` §1-§3（锚点/硬事实/禁写清单）；§4-§6 可选，无调研结论留空。
   每条事实带来源，无来源不写（禁推测约束，模板 §7）。
   完成：全文无「填你自己的」占位；§2 每条硬事实回溯到 §1 小节或外部来源。
3. **行业归类 + 行业背景调研**：从简历 + 材料推断所属行业，向用户**显式确认**（「你的背景归类为
   X 行业，对吗」）；多重背景置信度不足时升级问人，不代猜。确认后问用户**理想岗位/方向**，
   据此做行业背景调研（Web Deep Research：这个行业怎么招人、招聘语境与黑话、量价与公司坐标系），
   生成 `private/kb/industries/<行业>.md` 草稿——**含「行业入门资料」小节（带来源链接，
   遵守 fact-verification 约定）**，走 HITL 审核合并。确认后写 `private/kb/profiles/<行业>.json`
   （照 `blockchain.json` 结构；目标岗位的投递搜索词**必填**写进 profile 的 `queries`
   字段——搜索自动跟随行业（#22 Phase 2），缺了投递会拒绝启动而不是静默按别人的
   关键词跑（#29 门禁；onboarding_check 会在「profile/行业层」项报缺并给修复片段）。
   可选 `query_weights`：`{"关键词": 2}` 让高权词追加一轮搜索）。
   完成：profile JSON 的 industry 字段指向存在的行业层文件、queries 为非空字符串数组；
   行业层含入门资料小节或声明为空。
4. **事实卡片 + 白名单**：按 `docs/kb-sop.md` 卡片规则从个人层生成 `private/kb/fact_cards.md`
   （事实逐字来自原文、每条附 §编号、只收正面事实），白名单由
   `python3 scripts/gen_greetings.py doctor --sync` 从卡片 diff 编译入表（#25，无需手改 JSON；
   表是 #18 规则扫描 gate 的唯一来源）。跑 `python3 scripts/gen_greetings.py doctor` 核对同步。
   完成：doctor 无缺口；有缺口先补卡片再 --sync 重跑，确认无碍才可用 `--yes` 放行。
5. **Skills 安装校验**（幂等：重复执行 `npx skills` 对已装 skill 就地更新、不叠加副本，
   `npx skills update` 同语义——见 implementation-workflow §2）：所需 skill 全部来自
   **Matt Pocock 公共 skill 集**，`npx skills` 一键安装（落到 `.agents/skills/` 或本机
   skills 目录任一位置即可）；**没有需要手工复制进 repo 的自研 skill**——项目特有约定
   全在仓库文档里（implementation-workflow §2 的项目覆写层）。安装后核对
   `docs/agents/implementation-workflow.md` §2 清单齐全（model-invoked 8 个 + user-invoked 6 个）。
   **无 Node 环境降级**：`npx` 不可用时说明「skills 校验跳过，决策/实施阶段由主 agent 直接执行对应步骤
   （implementation-workflow §2 降级条款）」，继续后续步骤，不阻塞 onboarding。
   完成：清单核对通过，或降级说明已告知用户。
6. **浏览器登录态（可选，可留到首次投递）**：问用户「现在登录还是留到首次投递」。
   建议用新注册的平台账号（2026-09-10 约定：老账号历史行为画像与拟人节奏不符，更易触发
   风控；新账号从第一封起就是同一套行为）。
   现在登录：agent 起 `python3 scripts/test_camoufox_login.py --login`，用户在弹出的
   Camoufox 窗口完成扫码/验证，登录态落 `private/.camoufox_profile/`（不入 git）；
   留到首次投递：投递脚本检测到登录框会自动唤起登录窗（#1 relogin 链路），不阻断 onboarding。
   完成：登录态文件存在，或用户明确选择延后。
7. **跑通验证**：agent 会话内按三层装配（fact_cards + 个人层 + 行业层，同 `load_kb_context`
   语义）起草一条招呼语，按 `apply-runbook.md`「实时投递」的「起草规范」自检四道 gate
   （`ai_fingerprint_scan` / `internal_marker_scan` / `runtime_state_scan` / `rule_scan`，#34 起
   第四道查未实测的运行时状态词；#9 架构决策：
   起草由 agent 驱动，gen_greetings 的 generate 命令已删除）。
   完成：草稿引用 §2 硬事实而非模板占位；三道扫描全 CLEAN；最后重跑
   `python3 scripts/onboarding_check.py` 退出码 0（收尾盘点，用户约定 2026-09-10）。

## 边界

- 本 runbook 只写 `private/` 与根目录 `config.yaml`；简历与个人数据不入 git。
- 需要用户输入的步骤（提供简历、确认行业、HITL 审核）挂起等回复，不代填。
- 多行业并存（同一份简历）：用户说「加个 XX 行业」→ 重跑步骤 3-4 生成新行业 profile，
  投递搜索词跟随该 profile 的 `queries`，不覆盖已有行业层。
- 多 Profile（**不同简历/不同账号**，#28）：用户说「加个 XX 的 Profile / 换份简历」→ agent 幂等建骨架：
  `private/profiles.json` 注册表加条目（含简历路径）+ 新行业层骨架 + 新浏览器登录态目录
  `.camoufox_profile.<name>` + 空台账惰性生成；用户把简历/材料丢进 `private/` 后走步骤 2-7。
  在位（default）profile 沿用现有文件名零迁移，其余产物文件名加 `<name>.` 前缀——路径解析
  一律走 `scripts/_profile_paths.py`，脚本启动会打印 `[profile] <name>`。
  **Agent 反幻觉规则**：用户指名的 profile 先查注册表，查不到就问，不代猜；未指名用 default；
  多 profile 且未设 default 时脚本会拒绝启动（fail-loud），提示设置注册表 `default` 或
  `AUTOJOB_PROFILE` env。
- onboarding 完成不等于投递就绪：真实投递按 `docs/agents/apply-runbook.md` 触发词走
  （用户说「投简历 / 实时投简历 / 开始投简历」才是投递授权）。
