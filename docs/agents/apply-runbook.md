# 投递 Runbook（prompt 驱动）

> 触发词：「投简历」「实时投简历」「开始投简历」（可带时长，如「跑 1 小时」）→ 实时投递。
> 这是**唯一的投递路径**（用户约定 2026-09-10）。
> 步骤由 agent 自己执行——脚本由 agent 起、日志由 agent 读，不让用户代跑。
>
> 架构前提：草稿由 Agent 大模型直接撰写（本地 LLM drafter 已退役，gen_greetings 的
> generate 命令已删除）；脚本只负责机械部分（浏览/打字/发送/回写）。
> 限速与静默时段是防风控实现，保留在脚本内。

## 阅读路径（context pointer）

本文件是「操作序列 + 按需查阅的 reference」混编，按下列路径进入，不要从头通读：

- **每轮投递必读**：步骤 1–6 —— 操作主干，按序执行。
- **写 reply_N 时到达**：步骤 2 下的「起草规范」「称呼规范」 —— 起草那一刻才需要。
- **跑入站回复时到达**：「入站回复」小节（操作）+ 步骤 2 下的「回复起草规范」（文体）——
  只有 `scripts/reply_inbound.py` 这一条链路需要，投递轮不必读。
- **决定是否 skip 时到达**：「边界」的定位条款。
- **会话被看门狗击杀、或 `report.json` 出现 `wedge_ledger` / `browser_exit` 时到达**：
  `docs/agents/apply-forensics.md` —— 卡死（代码里叫 wedge）的三份证据怎么读、四格判读、
  跳岗台账与那几个开关。
- **要偏离默认节奏或调档时到达**：`docs/agents/apply-tuning.md` —— 会话级开关与日上限阶梯
  （默认值本身现取，见步骤 1，本文不抄副本）。
- 每步的**完成判据**已内联在该步骤末尾（「完成：…」），是「这一步是否真的做完」的唯一判据。

## 实时投递（`scripts/live_apply.py`）

浏览会话中实时看 JD → 实时起草 → 当场发送。无预爬、无预写队列，岗位从列表**点卡片**进入（不再直跳 URL）。动作截图序列（滚动/点击/打字瞬间）逐岗落 `shots/`，供用户事后回看。

岗位有**两个来源**（#33）：**推荐列表**（顶栏「职位」页的无 query 模式；默认开启）+ **搜索流**（按 profile `queries` 逐词搜索）。**顺序（#61 方案 A，2026-10-06）：推荐流一轮在关键词轮之前跑**——旧顺序把它排在所有搜索轮之后、且开轮条件含「未停批」，于是任何停批信号都会让第二来源整段缺席（实测会话 `20261005-215607` 的 `recommend_listing=null`）。**代价（本文件只在这里写一遍）**：推荐流从「用剩余额度」变成「先吃额度」，`_collect_target` 随已投数收缩，后面的关键词轮候选目标数变小、额度吃满时不再有投递额度；退路是 `LIVE_RECOMMEND=off`（回到纯搜索流）。**推荐轮另有消费配额（#65）**：采集照旧滚到底，只消费排在前面的若干候选；**它管「吃多少个」、`LIVE_COLLECT_CAP` 管「看多少条」是分开的两件事，怎么调配比与代价只写在 `docs/agents/apply-tuning.md` 的 `LIVE_RECOMMEND_CAP` 那一条**。**两个来源共用同一个页面** `/web/geek/jobs`，都靠滚动加载，实现会**持续滚到底累积**。两来源合并后按 `encryptJobId` 统一去重，同一岗位全局只投一次。`report.json` 的 `direct_fallback` 是**两类混在一起**的计数（`live_apply` 的 `_enter_and_inspect_job` 记账）：`direct_no_card`＝列表页没有 DOM 锚点、按正常数据态直跳（**非零属预期内**，锚点覆盖率随页面版本变化），`direct_click_failed`＝点了卡片却没成功、直跳是兜底（这才是 #50 熔断的分母，占比超 `DIRECT_FALLBACK_STOP` 即停批）。**汇报时按 `card_clicks` / `card_click_fails` / `direct_no_card` 分开报**，别把 `direct_fallback` 一个数当成「真实退化率」。形态、端点、到底判据与采集深度见 `内部调研笔记目录（未随本仓分发）boss-recommend-card-shape.md` §10（探测护栏见 §9.5）。

1. **启动会话**：`python3 scripts/live_apply.py > /tmp/live_$(date +%H%M).log 2>&1` 后台运行
   （脚本自带行缓冲，重定向下也能实时读到 `[队列] JD 已写 request_N.json`，无需 `-u`）。
   启动会打印 `[profile] <name>`（解析链：`AUTOJOB_PROFILE` env > 注册表 default > 唯一 profile，
#28）——投谁的简历、用哪个账号由它决定；搜索词来自该 profile JSON 的 `queries` 字段（可选 `query_weights` 高权加轮，#29 单一真源；
   缺 queries 时投递拒绝启动并给修复片段——不静默回落）。**默认值与阈值一律现取，本文不抄
   副本**（抄了就会烂）：`python3 -c "import sys;sys.path.insert(0,'scripts');from _limits
   import load_config;print(load_config({}))"`，布尔 env 的取值表真源在 `_limits.py` 的
   「布尔 env 开关」小节。每轮都可能用到的是两条：`LIVE_PACE=fast`（温和提速预设）与
   `LIVE_QUERIES="词1,词2"`（临时覆盖本次搜索词；设置时按原样、不带 `query_weights` 加轮）。
   **其余开关**（改起草超时 / 关推荐流 / 调采集上限 / 调保留天数，以及日上限降档判据）
   → `docs/agents/apply-tuning.md`；**会话被看门狗击杀，或 `report.json` 出现 `wedge_ledger` /
   `browser_exit`**（止损开关与取证阈值都在那）→ `docs/agents/apply-forensics.md`。
   采集目标另受剩余额度约束（剩余额度 × 3）；「投 100 封」的量就从这里来，日上限仍由
   `APPLY_DAILY_LIMIT` / 自动阶梯封顶（余量系数与阶梯的真源在 `_limits.py`，要调档读
   `docs/agents/apply-tuning.md`）。**注意**：单来源采集到的条数 ≠ 会投那么多封——候选还要过
   相关性 gate、跨来源去重与简历公司过滤。
   脚本自带启动抖动/静默检查/自动阶梯日上限/会话节奏，无需调参。
   完成：日志已打出 `[profile] <name>` **且** `[启动] 随机延迟 Ns 后开始`（默认抖动上界以
   `load_config({})` 现取，`LIVE_PACE=fast` 会压低它）——**首条 `request_N` 不是本步的判据**：它可能十几分钟才出现，
   等它会把人误导成「没启动成功」而重跑一场（同一段目录里两个进程会顶锁）。
2. **agent 实时起草**：Glob 监听 `private/data/live_session/<ts>/request_*.json` →
   Read 岗位上下文 + JD 全文 → 按 `private/profile_background.md` + `private/kb/` 起草招呼语 →
   **自检四道 gate 全 CLEAN**（`gen_greetings.py` 的 `ai_fingerprint_scan`/`internal_marker_scan`/
   `runtime_state_scan`/`rule_scan`，whitelist 用 `load_whitelist_file('private/kb/fact_cards.md')`）→
   Write `reply_N.json`：`{"send_text": "...", "level": "高|中|初", "notes": "§1 §14 repo:aaveapy"}`
   （level 供 #19 eval 按岗位级别分组，随投递成功回写台账；notes 备注区**必填**——
   发送前机械核验第 4 项，见下）。**脚本在发送前会机械核验同样四道 gate + level +
   notes 备注区（`live_apply.pre_send_gate`，2026-09-10 用户约定：确定性不靠 agent 自评；
   notes 核验为 #27 项 3，2026-09-11）——违规草稿
   被拦截不发送（status=gate_blocked），agent 自检是快路径，脚本是硬门**；
   岗位与求职定位不匹配（实习/零工/方向偏离，见「边界」的定位条款）则写 `{"skip": true}` 秒跳，
   **原因写进 reason 字段**（会话结束按 reason_stats 分类反馈，用户约定 2026-09-10）。
   **起草规范**（起草时逐条遵守；**当前投递路径以本节为唯一现行规范**——`docs/greeting-prompt-template.md`
   是退役 drafter 时代的模板，与本节同源但已滞后（它内部长度两处不一：100-200 与 120-150），
   只作历史参考）：
   - **认岗再落盘（2026-09-29 实证，#93 加固）**：写 `reply_N.json` 前先在本轮 `Read`/`cat` 出 `request_N.json`
     的 `jobName`/`brandName`/`bossName`，抬头与正文按那份 JD 写——脚本按序号取草稿。两道机械兜底已就位
     （#93 A 一号一岗：补投轮与搜索/推荐轮从同一处续号；#93 C 来历核验：`reply_N` 的落盘时刻早于
     本岗 `request_N` 就判为陈旧草稿、拒收后继续等），兜底只兜住「错发」这一半，代价是该岗等到
     `agent_timeout`；**先读到 request 再写 reply** 仍是正路，`request_N` 还没落盘时不写 `reply_N`，宁可等
   - **决策问题不占用草稿窗口（2026-10-05 实证）**：会话进行中**不要**把「要不要/怎么选」这类
     需要用户回话的问题抛出去然后等答复——每个岗位的草稿窗口只有 `LIVE_REPLY_TIMEOUT`（默认值
     现取 `live_apply.REPLY_TIMEOUT_DEFAULT`，本文不抄）加 90s 宽限复查，等到用户回话时岗位早已
     `agent_timeout` 作废（实测一场连丢 5 个岗，全靠 #55 的跨会话台账兜住才没白投）。要问就
     **先把手上那一岗的草稿落盘再问**，或把问题攒到收尾（步骤 6 汇报）；实在需要中断时用
     `CLOSE` 哨兵优雅停批（走完整收尾），别让岗位干等。
     判断依据写进 `reason` 字段，用户按 `reason_stats` 事后裁量——「跳过还是投」的自由文本
     本就供用户复核，不需要当场问。
   - **先 gate 后落盘（2026-09-25 实证）**：正文定稿后先本地跑四道 gate 自检（`ai_fingerprint_scan`
     /`internal_marker_scan`/`runtime_state_scan`/`rule_scan`，全 CLEAN），**再**写 `reply_N.json`——
     脚本在文件出现的瞬间就消费它，gate 拦截即该岗作废（不重读修复稿）；某岗 #11 就这样丢的
   - 形态：发给 HR 的站内聊天框对话体，口语化短句——对方每天收几十条，模板感=被略过
   - 开头公式：前 15 字内「年限+成果」（例：「我有 <N> 年 <核心技能> 经验，…」；
     年限白名单取自该 profile 的 `fact_cards`，本仓不写死任何人的年限），禁套路开头
   - 语法：每句主谓完整，首句以「我」开头；禁裸名词短语成句（「<N>年<技能>，主网部署过…」是病句）
   - 敬语：指代用「您」；禁「你们/你公司」
   - 结尾：给一个具体下一步（发**简历／网站／repo**这一类对方一眼能看懂的材料）并把材料锚点写进
     正文；**不再提审计报告（用户约定 2026-10-16：对收件人没有意义，招聘侧看不懂它代表什么）**；
     禁客套结尾（期待聊聊/不胜感激/期待您的回复类）
   - **结尾只带软问题（用户约定 2026-10-16）**：软问题＝对方查自己的收件箱和日程就能答的
     （「方便的话我把简历发您看？」「这岗还在看人吗？」）。知识型提问禁——判据是
     「对面那个人答不答得上来」，不是「这个问题跟岗位相不相关」。历史会话样本实测：
     对面多数是 HR／猎头／招聘经理，以技术 scope 二选一收尾的问句基本拿不到回复；
     而对方主动发起的对话，开口句式全是零知识成本的动作确认。脚本逐问句核验并
     **默认报、软问题句式白名单放行**（`live_apply.question_warnings`），命中打 `[warn 提问]`
     不阻断，计入 report.json 的 `results[].question_warns`，汇报见第 ⑤ 项
   - 事实纪律：只能引用 profile_background.md + fact_cards 确认过的事实；每条 claim 的来源
     （§N/repo/链上/推文日期）记入 reply_N.json 的 **notes 字段**（备注区，与 send_text/level
     同级——缺失或引用不存在的 §编号会被发送前 gate 拦截，#27 项 3）——来源标记只在
     notes 这一侧出现，正文保持收件人读起来就是一句普通对话的事实陈述。
     **时态纪律（#34，2026-09-12 真实事故后加）**：知识库里的项目状态是**部署快照**
     （"2022 年部署到主网并已验证"），正文只能用完成时/过去时；写现在时的运行时状态断言
     （"运行至今/持续运行/在线运行/在跑/至今活跃"）即声称业务此刻仍在跑，须在 reply_N.json 的
     **`state_evidence` 字段**记 ≤7 天内的实测（含日期，如 `链上 polygonscan 2026-09-14 调用确认`）
     才能过门；没有实测就不写那句，改成快照式表述。链表述用通用的"主网"，
     不把自己框死在单一链名（"Polygon 主网" → "主网"）；
     起草后 `ai_fingerprint_scan`（痕迹词表见 gen_greetings.py 的 AI_FINGERPRINT_WORDS）/
     `rule_scan`/`internal_marker_scan`/`runtime_state_scan` 四道 gate 必 CLEAN
   - 参考：正例池**确定性注入**（#37 拍板 Q4）——起草前跑
     `python3 scripts/gen_greetings.py match-examples --addressee <addressee> --level <level>`
     （从 `private/kb/winning-examples.md` 按标签匹配 top-2，无匹配不强行注入），
     输出即「参考成功范式」素材，模仿结构不抄内容（池内条目晋升于本约定之前、多以知识型问句
     收尾，借结构不借收尾）；
     **同品牌再触达**（brandName 已在 applied_history / greeting_memory 出现）时先读
     `private/data/greeting_memory.json` 该品牌 `intent` 列表定向引用历史意向
     （如「上次您提到想看简历/约面」——#21 约定，不全局注入其他品牌）
   **回复起草规范（入站对话，#66 阶段 2）**：写 `reply_inbound.py` 会话目录里的 `reply_N.json` 时读这段。
   四道 gate、称呼规范、结尾只带软问题、事实纪律与 notes/state_evidence **全额适用**——走的是同一次
   `pre_send_gate` 调用，回复侧没有豁免分支。**认岗再落盘 / 先 gate 后落盘 / 决策问题不占用草稿窗口**
   三条通用纪律同样适用（同名联系人与同品牌对话可能并存，序号错配就是把 A 的回话发给 B）。
   下面只写与首封不同的几处：
   - **接话，不重新开场**：第一句直接接住 `request_N.json` 里 `theirLastMessage` 那件事——他问简历就
     说简历怎么给，他问时间就给时间。「前 15 字年限+成果」的开头公式是冷启动自我介绍的装备，
     而这条对话里对方已经在跟你说话，接着说即可。
   - **一到两句 + 一个下一步**：对话已经开着，把整份招呼语再贴一遍是噪音。
   - **材料只以文字给**（链接、「发您邮箱」这类说法）：面板上的「交换简历/发附件/加微信」那类控件
     本链路不点——它们的形态与副作用都未核实。对方就要那个时写
     `{"skip": true, "reason": "要附件/要微信"}` 交用户处置。岗位不对口、招聘已结束、群发模板同理，
     原因进 report 的 `agent_skip` 桶。
   - **原文不是人话就 skip**（10-21 真机 16 条入站里有 4 条属这类，且全落在「可回」桶里）：
     `theirLastMessage` 为空（附件/名片类消息，wapi 的 `lastMsg` 给空串，2 条）、平台系统通知
     （「对方已同意，您的附件简历已发送给对方」，1 条）、内嵌复制标记的联系方式
     （`翊女士的微信号：<copy>…</copy>`，1 条——顺带说明 `lastMsg` 里会带 HTML 残留，别当纯文本照抄）。
     这三类没有可接的话头，硬起草只会得到一句自说自话。
   - 正例池 `match-examples` 是首封素材，回复不跑它（那条命令注入的是开场范式）。
   **称呼规范（用户约定 2026-09-10 v3）**：招聘者名（request_N.json 的 bossName）+ 身份（bossTitle）
   - 已带敬称（先生/女士/总/老师/经理）→ 原样保留
   - **猎头/人才经纪人**（bossTitle 含「猎头」「经纪人」）不叫「总」：名字已带先生/女士直接用；
     纯中文姓名按名字性别特征用「姓+先生/女士」，无把握才兜底「姓+总」
   - 公司招聘者中文姓名 → 姓+总，不叫全名
   - 纯英文名 → 不加「您好」，英文抬头（Peter→「Hi Peter，」），猎头同样适用
   - 无姓名 → 「您好，」
   - **称呼软拦截（#27 项 1，2026-09-11）**：脚本对确定性子集（纯英文名禁您好/中文姓名
     禁全名/公司招聘者姓+总/已带敬称原样保留）打 `[warn 称呼]` 不阻断发送并计入
     report.json 的 salutation_warns；名字性别推测不可代码化，
     仍按本规范人工把握；猎头分支不校验期望形——票面「猎头不叫总」含合法兜底
     （无把握才姓+总），确定性校验无法判其为错，故放行
   完成：每个落盘的 `request_N.json` 都有一个 `reply_N.json`（投 or `skip` + `reason`，二者必居其一，
   且 reply 是**读到该岗 JD 之后**写的），并且这些 reply 里**没有出现在 report 的 `gate_blocked`**——
   写了却被拦等于该岗没投，光「有对应文件」不算做完。
   终端出现「陈旧草稿」那一行（#93 C 的来历核验）＝这个号的 `reply_N` 早于本岗 `request_N`，脚本已拒收
   并继续等本岗起草。A 之后它只在撞号或会话目录被预先写入时才可能亮 ⇒ 连同步骤 3 的编号账一起按缺陷报；
   投递轮里该岗始终没等到本岗草稿就走 `agent_timeout` 进补投台账（不是丢岗）。一次等待只报一行。
3. **等脚本跑完**：脚本依次点卡片→浏览→入队→粘贴/逐字打字（默认 70% 粘贴 30% 打字混合，
   `APPLY_PASTE_PROB` 可调）→发送→go_back，全程截图落盘
   `private/data/live_session/<ts>/shots/`。
   队列耗尽前后有两段**超时补投**（#55，自动行为）：开跑先补投跨会话台账
   `private/data/[<profile>.]retry_pending.json` 里的历史超时岗位（attempts 累计封顶 3 次，
   投出/已跳过即移出；`no_jd` **按证据分流**——只有「详情接口 code=0 而正文为空」这种
   「岗位无内容」的正向证据才移出，站点抖动型（往返失败 / 超时未回）保留补投机会；
   旧口径「岗位疑已失效即移出」会静默误杀可补投岗，#56 A5 已废）；收尾前对本场 agent
   起草超时的岗位直跳重试一次（起草超时本身先经 90s 宽限复查救迟到草稿）。
   两段都受日上限/风控熔断/excluded 清单/wedge 台账同链约束，dry-run 会话不补投不写台账。
   完成：`[done]` 分桶播报行出现，**且** EDD 那一行**三种形态都算**（快照追加了才渲染
   `## EDD 趋势跟踪` 块，否则打 `[EDD] 跳过趋势快照（原因）`——幂等去重与残缺采集都走后者，
   实测 `20261005-215607` 就是跳过那支；第三种 `[EDD] 趋势块未渲染（…未带 append 前的趋势历史）`
   是 #82 的 fail-closed 分支，生产路径正常到不了——它一出现就说明接缝又退化成写后读，按缺陷查），
   所以**不要把「EDD 块没出现」读成「还没跑完」**；
   再核一条编号账（不变量 #93 A：一场会话内**一号一岗**）：Glob 出的 `request_*.json` 编号集合与
   `report.json` 里 `results[].idx` 集合对得上（skip/timeout 也占编号），没有悬空的 request，
   **且 `results[].idx` 去掉无编号条目（excluded_company / wedge_skipped / 起飞前自检）后逐号唯一**。
   撞号＝续号链退化，出现就把两份 `request_N.json` 的 `jobName` 一起呈用户并按缺陷报。
   `[done]` 没出现就是还在跑——
   **提前跑步骤 4 会漏掉本轮发出后才收到的新回复**。
4. **收尾（不关窗）**：脚本结束后浏览器保持打开，**报告写盘前现场采集一次最新
   followup**（#49，2026-09-24：`[followup] 现场采集 N 条对话` 行，report.json 的
   `followup_fresh` 字段为真）——收尾 EDD 快照与密度判定（`## EDD 趋势跟踪` 块）因此
   吃到本场投递后的真实回复状态；采集失败**或采到残缺列表**（残缺判据＝消失项构成上一份末尾
   连续一段**且**跌幅过半，#59 立、#63 收成 `_followup_shape.decide` 一处）都只
   warn 降级（`followup_fresh=false`，沿用上一份报告），不阻断收尾——汇报时要说明本场 eval 吃的
   是上一次复盘的数，别把旧读数当新读数念。**散落形态不降级**：那是站点侧真把会话移走了
   （实物见 `内部调研笔记目录（未随本仓分发）boss-chat-card-shape.md` §8），照常覆盖、样本基数随之变小，
   这种情况终端打 `[清单签名]` 行并点名三个数字（本场／上一份／消失条数）。
   浏览器**保持打开**（结束不关窗，#39）：`[done]` 之后浏览器窗口保持打开，终端打
   `[保持] 浏览器保持打开；写 <session_dir>/CLOSE 结束保持（Ctrl+C 亦可）`；用户确认后写
   `private/data/live_session/<ts>/CLOSE` 空文件结束保持（脚本轮询 CLOSE 哨兵），Ctrl+C 亦可直接退出。
   完成：三段各给一句结论——① 现场采集读数已明确：`report.json` 的 `followup_fresh` **是 true 还是
   false 都要说出来**（false＝走了残缺降级，本场 eval 吃的是上一次复盘的数；缺这个键＝这场没采集，
   按缺陷报）；② `CLOSE` 已写入；③ 收尾读数只有两类——逐岗截图行 `    [录像] 截图 <文件名>`
   （含失败变体 `[录像] 截图失败`）照常打出、`shots/` 照常产出（这个前缀是**截图**日志，与已下线的
   录像无关，一直都有）；除此之外全程不出现任何录像/定格/保持段读数行：`[录像] 视频已落盘`／
   `[剪辑]`／`[保持] 已保持`／`[保持] 保持段录像` 都不该再出现（录像链路已随 #105 下线，出现了按
   缺陷报）；CLOSE 之后终端收在 `[保持] 收到 CLOSE 信号`。
5. **followup + 回写**：收尾虽已采集（#49），本步的 followup 重跑仍需要——**标注要读
   HR 回复原文**：`python3 scripts/followup_tracker.py`（重新生成 followup_report.json）→
   Agent 阅读 HR 回复原文，按固定分类法（interested/tech_question/rejected/no_reply +
   intent_sub 二级标签 + replyLatencyHours 小时数）写标注 JSON，跑
   `python3 scripts/followup_tracker.py annotate --annotations <file>` 落盘（#19/#21）→
   `python3 scripts/gen_greetings.py learn`（HR 反馈回写 greeting_memory.json）。
   完成：learn 输出 `[done] growing memory 已更新`；annotate 输出 `[done] 标注 N 条`。
   followup 终端还须读出 `[入站线索]` 这一行（`followup_report.json` 的 `unlinkedConversations` /
   `unlinkedReplied`，2026-10-16）——归不到投递记录、且末条是对方发言的对话＝**对方主动来撩、正在等
   我们回话**（逐条打 `▸ [读态] 公司｜职位｜原文`：全列不设行数上限，行数必须等于上面那句读数；
   原文超 46 字行尾带 `…`，全文在 `followup_report.json`；这里的「职位」是对方的抬头，
   岗位名只在 `reply_inbound` 的候选表里）。紧跟的那行 `其中有未读标记 N /
   无未读标记 M / 读态未知 K` 是同一批的第二层分法（来自 wapi 的 `unreadMsgCount`，**只读拿到、
   不靠点开**）：三桶相加＝`unlinkedReplied`。**但这列在真机上已被测出是空的**——2026-10-21 09:05 与
   09:54 两次快照 68/68 条全为 0，而 09:54 那次用户确认 Boss 上**有未读会话**、两次之间 `hrReplied`
   还从 18 涨到 20（新消息确实在进来）；9-12 那份 61 条同样全 0。⇒ geek 侧这个字段不携带未读态，
   `M` 只代表「站点没给未读标记」，**不代表「我们看过没回」**，更答不了「该去读哪几条」——那件事
   只有 Boss 的未读徽标知道，要人去看。呈报时把这列当**原始字段**念，别当判据念。
   要回这批见「入站回复」小节（`scripts/reply_inbound.py`）；**没跑过它就是未回**，呈报时不得把这批
   说成「已处理」。它们也不进 eval 分母（配对从招呼语台账侧驱动），所以对话级 `replyRate` 远高于
   台账级 `overallRate` 是正常差异，差值主要就是这批入站对话。
   annotate 入口机械校验（#27 项 2）：坏 JSON/坏标注（枚举外分类、匹配键 company/appliedJob
   全空、负时延、intent_sub 非法）打 `[blocked]` 并退出码 1 不落盘——按报错指明的条目重写标注
   文件后重跑。
   样本达标与否都跑 `python3 scripts/gen_greetings.py eval --trend`（#37：**每次都跑**——
   充分性与观测性解耦，样本不足也输出趋势与距达标缺口；趋势快照自动落
   `private/eval_trend.jsonl`，live_apply 会话收尾已自动记过一次会话快照，同内容幂等去重）；
   eval 产出的正/反例候选自动进待审队列 `private/kb/pending_promotions.json`（30 天未审自动
   废弃，下次产出重新入队）；回写需用户人工确认（`promote-winners --group <key> --yes`）。
   正例池超 25 条上限时 promote-winners 拒绝写入并列出建议淘汰项（FIFO+回复率降权）——
   加 `--retire <片段>` 点名淘汰（片段须恰好命中 1 条）后 --yes 一并执行，绝不静默删除。
6. **汇报 + 分类反馈（用户约定 2026-09-10）**：除投递/跳过/熔断统计 + `direct_fallback`
   （直跳回退次数）+ 截图路径（`shots/`）外，按 report.json 的 `reason_stats` 向用户反馈
   **哪类岗位投了、哪类没投、为什么**（关键词误中/硬语言缺失/薪资段位/非工程岗/领域不符…），
   让用户知道漏了哪类；gate_blocked（发送前机械核验拦截，2026-09-10）也须如实计入——
   被拦了几条、什么违规；salutation_warns（称呼软提醒，#27 项 1）条数同样呈现；
   `keyword_skips`（关键词轮导航重试后仍失败被整轮跳过，#35）也须列出，并说明是网络
   抖动还是环境问题——这些关键词一个岗位都没投，是漏量来源；**EDD 趋势呈现（#37）**：
   会话收尾终端的 `## EDD 趋势跟踪` 块与 report.json 的 `edd` 字段须纳入汇报（样本进度 +
   距达标缺口，样本不足也如实呈现）；出现 `💡 [EDD 批次审阅]` 提示（样本新增 ≥10 或
   超 3 天有新回复）时，agent 直接跑 `eval --trend` 并把趋势读数呈报（与步骤 5 的「每次都跑」
   同一口径，别让同一个命令在两份文档里一会儿你跑一会儿他跑），只有 promote-winners 交用户确认；
   **密度阈值提示制（#45，2026-09-24 拍板）**：任一 eval 分组组内回复 ≥5 时，会话收尾
   （edd_wrapup）与 eval 命令会主动发 macOS 系统通知并打 `💡 [范式审阅]` 提示块（附
   pending_promotions 候选清单与 promote-winners 指引）——此时 agent 当轮把候选清单呈给用户
   拍板（确认晋升或明确推迟），并在同会话处理 #27 项 4 的分组评估（两者数据门槛同源，
   triage #45 约定）；低密度期不出现该提示，属正常静默不扰，不得代用户跑 promote-winners；
   **熔断事件开票（#51）**：会话收尾出现 `💡 [开票提示]`（本次会话有熔断/停批事件，草稿
   已随 report.json 的 `issue_drafts` 落盘）时，agent 当轮把拟建票的标题清单呈给用户授权——
   远端建票是外部动作（AGENTS.md 硬门禁），确认后执行
   `python3 scripts/_issue_reporter.py --report <report.json 路径>` 建票并汇报返回的
   issue URL；未获授权则如实汇报「待确认」，不得代跑；`AUTOJOB_AUTO_ISSUE=1` 的无人值守
   会话改为直接汇报已自动开出的票（防重台账 `reported_issues.json` 保证同会话同熔断器
   只开一张；gh 失败只 warn 不阻断收尾，可稍后用同一命令重试）；
   跑长时每投满 5 条左右也可主动给一次小结。
   完成：汇报逐项覆盖下列十五项，缺任一项都是没汇报完——① 投递/跳过/熔断三类计数，投递数并按
   `applied_by_source` 报**来源分布**（#61：`search` / `recommend` / `retry` / `unknown`；逐条看
   `results[].source`，**`null` 只表示无法归属，不许说成「来自搜索」**——归属按「编号区间 + results
   切片」双判据；#93 A 后一场会话一号一岗、补投轮与搜索/推荐轮的号段不再重叠，来源仍只按
   `results[].source` 念（不从编号反推：切片判据不依赖编号唯一，回读 A 之前的历史 report 也读得对）；
   同一项里报推荐轮的**候选 N / 消费 M**（#65 消费配额：`recommend_candidates` / `recommend_consumed`，
   差值 N-M 是本场没轮到、不投也不进台账的岗数；M 个里 0 命中而后面的搜索轮有命中时，按 `apply-tuning.md` 调档）；
   ② `direct_fallback` 计数；③ `reason_stats` 分类反馈（哪类投了/哪类没投/为什么）；
   ④ 截图路径（`shots/`）；⑤ `gate_blocked` 与 `salutation_warns`、`question_warns`（2026-10-16
   「结尾只带软问题」约定的软提醒命中，非零＝仍有草稿在问对方答不上来的事）、`keyword_skips` 明细
   （含网络抖动还是环境问题的归因）；补投统计（`[done]` 行「历史超时补投 N 个 /
   本场新增超时 M 个」，#55）出现时纳入——两层补投是自动兜底，汇报让用户知道
   超时岗位的去向（宽限救回/补投成功/进台账下轮再投/封顶放弃）；⑥ `## EDD 趋势跟踪` 块（样本进度 + 距达标缺口，
   样本不足也如实呈现）——核对口径（#59）：该块样本数与随后 `eval --trend` 的样本数应同量级，差值只应来自
   本场新增；出现「0 样本 vs 历史非 0」按 #59 的读数缺陷报告，不当成真实退化。⑦ 出现 `💡 [范式审阅]` /
   `💡 [EDD 批次审阅]` 时，把
   pending_promotions 候选清单摆给用户拍板（晋升还是推迟）；低密度期无提示属正常静默；
   ⑧ 出现 `💡 [开票提示]` 时，草稿呈用户授权后建票并汇报 issue URL
   （`AUTOJOB_AUTO_ISSUE=1` 则直接汇报已开票结果）；
   ⑨ report.json 出现 `browser_exit`（浏览器退出分类，#54）时，如实呈现分类与 evidence
   （pid 探活结果 / 崩溃报告路径），并按「边界」的「浏览器进程消失归因硬规则」处置；
   ⑩ report.json 的 `no_jd_classes` / `no_jd_dom_shapes` / `no_jd_code_nonzero_by_class` /
   `no_jd_empty_urls`（#56 A5 分流口径）、`wedge_ledger`（#56 B1 跳岗台账）与 `no_jd_reentries`
   （#67 三键恒在，重进 0 次也是一条读数；逐岗看 **no_jd 条目**的 `reentered` 与 `jd_chars`）
   出现时一并呈报——`no_jd` 的归类分布是「站点往返失败/挂起」与「详情页形态
   漂移」两假说的分流依据，`still_no_jd` 那一支的归因照旧由 `no_jd_classes` 说（重进读数不替它
   定性，也不许拿它压低 no_jd 计数）；
   只报事实与读数，不替用户定性（同 ⑨ 的硬规则）；
   ⑪ report.json 的 `no_send_reasons`（#62 四桶：`off_chat` / `send_btn_missing` /
   `send_click_failed` / `other`，恒在）有非零桶时呈报，并说清两类去向——`off_chat`
   是打字**前**的形态守卫拦下的岗（一个字都没进页面，已落 #55 的 `retry_pending`
   台账，下一场自动捞回）；后两桶是字**已打进**沟通输入框之后才落空的岗，按票面拍板
   口径不自动补投（可能已实际发出，重投有双发风险），这条读数就是它们唯一的可复核
   出口——逐岗列出 `results[]` 里对应的岗名与 evidence 路径，只报事实与读数（同 ⑨）；
   ⑫ report.json 的 `greeting_ledger`（#64 三键：`added` 入库 / `overwritten` 覆盖 /
   `skipped` 写盘失败，恒在；与 `[done]` 行「招呼语台账」段同源）——`overwritten` 非零＝
   这些岗库里原有一条被本次真投出的文案更新掉（原条目可能只是旧链路「起草过、从未发出」的稿，
   更新之后 EDD/learn 才归因到真发出去的那份），报数即可，旧稿内容在会话日志
   `[台账] 招呼语覆盖旧稿` 行带前 40 字，要复核逐岗去 grep 那行；`skipped` 非零＝有文案没进库
   （写盘失败被台账层吞下，投递本身不受影响），按缺陷呈报；
   ⑬ report.json 的 `greeting_ledger_migration`（#64 追加验收 5/6 四键，恒在：`legacy_undated` /
   `legacy_draft_only` 两个历史遗留桶的普查数、`applied_without_greeting` 投过但库里查不到文案的
   缺口数、`tagged` 本场新打标条数）与 `gen_greetings.py eval` 的
   `[eval] 排除历史遗留招呼语 N 条` 一行出现时呈报。三条读数的去向各不相同——`tagged` 非零通常只在
   **第一次**跑真机的会话出现（历史条目就此脱离按文案分组，之后恒为 0，非零不是缺陷）；之后再
   非零＝库里新进了没有 `date` 的条目，如实报一句；
   `applied_without_greeting` 是**只普查不回填**的缺口（票面口径：不给历史岗位伪造文案），报数、
   不据此判定投递有漏；`legacyExcluded` 让 `sampleSize` 比台账条数少一截，这是排除而非丢数据，
   别读成「样本退化」——同 ⑥ 的 #59 口径一样先对齐分母再说话；
   ⑭ followup 的 `[入站线索]`（`unlinkedConversations` / `unlinkedReplied` + 有未读标记/无未读标记/读态未知
   三桶，2026-10-16／#66 阶段 2）恒呈报——`unlinkedReplied` 非零就是「有人主动来找我们、末条是他发的、
   还在等回话」，逐条给公司／职位／原文／读态。这批**只在跑过「入站回复」小节后**才可能已回，
   没跑过就是未回；本小节内不进 eval 分母，所以只报事实、不代用户决定要不要回，更不许说成「已处理」
   （口径见步骤 5）
   ⑮ report.json 的 `followupLedger`（#63 六键恒在：`collected` 本场采到 / `previous` 上一份 /
   `missing` 上一份有本场没有 / `appeared` 本场新增 / `shape` 位置签名 / `followupCollection`
   采集形态：接口回了几批各几条、前端下发了多少 id、并集剩几条）与终端 `[清单签名]` 行、`[done]`
   行同段出现时呈报，并把**三个数字一起念**——它们互不相等就是缺陷信号（票面验收 5）。两个签名
   的去向相反：`tail_truncated`（消失项是上一份末尾连续一段）＝我们这侧读数残缺，跌幅过半时脚本
   沿用上一份、本场 eval 不吃这份数据；`scattered_lost`（消失项散在列表中间）＝站点侧移走了会话，
   照常覆盖，**样本基数因此变小**，念 `sampleSize` 与回复率必须连着念它，别读成「回复率退化」
   （同 ⑥ 的 #59 分母口径）；`unknown`＝没有可比基准，报「本场只建立、不比对」。趋势里新增的
   `denominator{conversations,shape}` 与状态 `appended_decline`（样本数低于最新一条，仍然落盘）
   就是同一件事在时序上的留痕，`eval --trend` 打出「已追加（样本数低于最新一条…）」时如实报。


> 日上限阶梯（首日 20、每日随机 +2-5、40 封顶、±5 抖动）由脚本自动推进，**默认不用管**；
> 要退档或提量、以及「什么算风控信号」的判据见 `docs/agents/apply-tuning.md`。

## 入站回复（`scripts/reply_inbound.py`，#66 阶段 2）

> 触发：用户说「回复入站」「回那批来找我们的」，或步骤 5/6 报出的 `unlinkedReplied` 非零、用户决定处理。
> 对象就是 `[入站线索]` 那批：对方主动来撩、末条是他发的、归不到我们的投递记录。

动作面与投递分开，三条都是刻意的：**不占** `APPLY_DAILY_LIMIT` 名额、**不写** `applied_history`、
**不进** `greetings.json`（#19 eval 的配对从招呼语台账侧驱动，混进回复会污染回复率分母）。
自己的台账是 `private/data/[profile.]inbound_replies.json`，按 `encryptJobId` 幂等。
**没有新的数字上限**：额度治理的既有结论是「阶梯 + 风控信号驱动升降」（`docs/agents/apply-tuning.md`），
本链路复用投递那套形态防护（突发节奏、时段系数、静默时段、验证码即停），当日累计只作读数呈报
（`repliedToday`），不拿来拦。

候选资格三条按序，都不回、只呈报：没有 `encryptJobId`（DOM 降级路径绑不上岗）→ 已回过（同一条
对话不自动化第二句，防把对话往来变成聊天机器人）→ 详情页已不是岗位页（404 或被 Boss 换成问答/SEO
内容页＝**下架岗不回**，2026-10-20 用户定）。

1. **只读跑一遍（默认）**：`python3 scripts/reply_inbound.py > /tmp/inbound_$(date +%H%M).log 2>&1`
   采会话快照（只读消息页，**不点任何会话**）→ 逐条反查岗位身份 → 打印候选清单。
   完成：日志有 `[会话目录] <路径>`（后面几步都在这个目录里）、`[快照] … 入站等回话 N 条`、
   `[身份 i/N] <eid> → ok|dead|unresolved|login` 每岗一行，候选逐条
   `▸ [可回/跳过：原因] <eid> 公司｜岗位｜读态｜线上新|线以下｜对方原文`（**步骤 2 要的那个 eid 就在这一行**；
   全列不设行数上限，原文超 52 字行尾带 `…`、全文在 `report.json`），
   清单之后一行 `[水位线] 较上次读数新增 N 条（水位线 <时刻>（K 键）…）`（念法见步骤 5）；
   该目录 `report.json` 的 `mode` 是 `dry_run`。
   dry-run 不点会话不是风格问题：点开就把对方那条标成已读，而**未读是「该去读哪几条」的唯一线索**，
   消耗掉补不回来。dry-run 落的 `request_N.json` 只用于预先决定「回不回、由谁起草」——真发那一场
   另起一个会话目录重新写，**草稿写在 dry-run 目录里没人读**。
2. **真发（外部动作，当次授权，且必须点名）**：`python3 scripts/reply_inbound.py --send --only <encryptJobId>`
   ——**裸 `--send` 启动即被拒**，`--only` 给两个也拒（真发只接受恰好一个）。这不是省事：Boss 的未读字段
   已被实测否证（见步骤 5 那段），机器判不出「这条你读过没有」，所以"读过没"只能由**你点名单条**这个
   动作来表达；一次裸跑会连开十几条你并没打算读的会话，未读全烧掉、补不回来。深链打开与
   「打开→发送→wapi 回读」这一整跳已在真机单条通过（2026-10-21，`20261021-191140`），但仍按
   一次只发一条、发一条验一条走——那是授权粒度，不是形态未定。
   脚本会**按 `encryptUid` 深链打开**那条会话（`CHAT_URL?id=<招聘者加密 id>`，缺失时退
   `encryptBossId`）——不是回消息页点卡片，所以**不依赖列表卡片是否渲染**（真机 68 条会话只渲染
   40 张，目标那条在整页 HTML 里 0 命中）。**打开的是不是这条**看右侧会话头
   （`.base-info` 去空白 = wapi 的 `company`；`.user-info` 是父层，会连「更多」按钮一起读进来），
   不看 URL：真机从岗位详情页
   导航过去时 SPA 会把 `?id=` 规范化掉。打开后写 `request_N.json` 并等草稿（窗口
   `LIVE_REPLY_TIMEOUT`，默认值现取 `live_apply.REPLY_TIMEOUT_DEFAULT`，本文不抄）：这期间按
   「实时投递」步骤 2 下的「回复起草规范」往**这一场的会话目录**写 `reply_N.json`；不值得回就写
   `{"skip": true, "reason": …}`，原因会带进报告供裁量。
   这一条链路复用投递那套 `wait_reply`，所以「陈旧草稿」那道来历核验同样在（#93 C）：终端报了那一行
   ＝脚本判这份 `reply_N` 早于本条 `request_N`、已拒收继续等；入站没有补投台账，等不到就按
   `agent_timeout` 计，不重试。
   发送成功的确认来自**重新读 wapi 会话列表**（`getGeekFriendList`，复用 `ChatFirstPlatform.followup`），
   不是读卡片末条：按 `encryptJobId` 唯一命中同一条，要求末条 `fromId` 是我方 userId、`lastMsg` 等于
   我们发的那句，且 `lastTS` 相对点开前前进。发送动作触发后，`outcomes[]` 恒带一对对照：
   `wapiBefore` 来自本场快照（不是点开瞬间的新读，`snapshotAt` 标明它属于哪一场），`wapiAfter`
   是发送后的新回读（`readAt`）；回读没命中时 `wapiAfter` 为 `null`，原因看 `wapiReadbackError`。
   完成：本场每条 `replied` 都能在 `inbound_replies.json` 里按 `encryptJobId` 查到条目——台账是
   **跨会话按岗累计**的，条数与本场 `replied` 本来就不该相等，别拿它当对账口径。
   `outcomes[].ledger` 为 `skipped` ⇒ 那条已发出但没入库，如实呈报。`send_unconfirmed` ⇒ 那条没进
   台账、且无法判断到底发出去没有：**不要重跑 `--send`**（双发不可撤回），把岗名、`wapiBefore` /
   `wapiAfter` / `wapiReadbackError` 与 `shots/` 截图呈给用户裁量。
3. **读数出口**：`<会话目录>/report.json`——`mode` / `inbound` / `eligible` / `replied` /
   `repliedToday`（只报不拦）/ `skippedReasons`（全桶恒在＝逐条为什么没回）/ `outcomes[]`
   （对方原文、发出的文案、notes、`skip_reason`、称呼与软问题命中；发送动作触发后另有
   `wapiBefore` / `wapiAfter` / `wapiReadbackError`，其中 `wapiAfter` 无命中为 `null`）；
   截图与 `deeplink-mismatch-<idx>.json` 在同目录。
   `snapshotAt` 与 `myUserId` 一并报：#63（会话列表分页未取全）没修之前这批条数只会偏小；
   `myUserId` 是回读判「末条是不是我方」的锚，空值＝回读侧只能 fail-closed。
   水位线两键同样恒在（`newSinceWatermark` / `watermark`，念法见步骤 5）；`followup_tracker.py`
   写的 `private/data/followup_report.json` 是同一对键、同一个判据（`_inbound_watermark.py`）。
4. **失败读数怎么读（#73，三个容易混的桶）**：
   - `no_deeplink_id`：wapi 条目没给 `encryptUid` / `encryptBossId`，按 id 打不开（形态漂移）。
     这是「没有地址」，不是「这条对话不存在」——报数，别手点。
   - `conv_unreachable`：有 id 但打开失败（导航超时），或打开之后**右侧会话头不是这条对话**。
     落地 URL 的 `?id=` **不参与判决**（真机：站内导航会被 SPA 规范化掉）。现场在
     `<会话目录>/deeplink-mismatch-<idx>.json`（期望 URL / 实际 URL / 期望 company / 读到的会话头 /
     候选 / error）与 `shots/`，呈报时两样一起给。
   - `conv_ambiguous`：`--only` 的 eid 命中多条可回会话（同岗多招聘者），按 id 也分不清该开哪位
     ⇒ 发前就停，一条不开。它不是「列表里没有」，而是「有不止一条，不猜」。
   - `conv_not_found`：**已移除的卡片路径**的历史桶（「列表里没渲染出这条」）。本链路不再产出它；
     报告里见到只可能来自旧报告。**别把 `conv_unreachable` 读成它**——前者是「打开时被换走」，
     后者是「列表里没有」，处置方向相反。
5. **水位线读数怎么念（#75，回答「先看哪几条」）**：Boss 的未读字段给不了这个答案（否证过程与
   结论在 `内部调研笔记目录（未随本仓分发）boss-chat-card-shape.md` §7.3），所以「比上次读数新的那几条」由脚本自己
   记的**基准**给出：两次只读会话列表读数之间做键集合差（`encryptJobId` ∪ `encryptUid`）加时间兜底
   （`lastTS` 越过基准里最大那个毫秒），末条是我方发的那条不算该读。全程只读，一次会话都不打开。
   `newSinceWatermark` 与 `watermark.state` 成对念，五种形态各说各的（两份报告同一对键、同一个判据，
   实现在 `scripts/_inbound_watermark.py`）：
   - `ok`：N 是整数 ⇒ 照念「较上次读数新增 N 条（水位线 `watermark.snapshotAt`）」。
     `newConversations` / `updatedConversations` 是它的拆分（新出现的会话 / 老会话里对方又发了一条）。
     N=0 就报 0，含义是「上次基准之后没动静」。
   - `first_run`：N 是 `null` ⇒ 「还没有历史基准，本场只建立、不比对」（首跑报具体条数就是谎报）。
   - `corrupt`：N 是 `null` ⇒ 基准文件读不出，**原文件保留不覆盖**；要重建请用户自己移走
     `private/data/[profile.]inbound_watermark.json`，删不改写都交给人。
   - `profile_mismatch`：N 是 `null` ⇒ 那份基准记的是别的 profile 的读数（注册表 default 换人时
     两个档会共用同一个文件名），各档各一份，不串档。**出路两条，都要人做**：切回原来那个 profile
     （`AUTOJOB_PROFILE=<名字>` 或注册表 default 改回去）再跑，或者由人工把这份基准移走／改名
     （例如 `mv private/data/[profile.]inbound_watermark.json <它自己的名字>`）让本档重新建立——
     脚本不替人挪别人的读数现场。
   - `no_keys`：N 是 `null` ⇒ 基准文件在、里面却一个会话键都没有（零键读数写进去的那格），
     与**没有基准等价**，念「基准未推进：无可记录的会话键」。它不是排查材料，处置是一次真拿到
     会话键的 `--mark-read`（就地重建），不必移文件。
   `watermark.unkeyed` 非零＝有几条拿不到会话键（DOM 降级路径），它们记不进基准、也不进 N，
   呈报时把这句一起带上，免得「N=0」被读成「都看过了」。

   **`--mark-read` 的时机：只在用户当次说过「这次读到的这批我看过了」之后跑**——
   `python3 scripts/reply_inbound.py --mark-read`。它跑的还是那场只读（零点击、不建截图），
   末尾把**本次读到的整批**（列出的、以及因末条是我方或已归到投递而没列出的，都算看过）的会话键
   与最大 `lastTS` 写成新基准，回显把三个数分开：
   `水位线已从 <旧时刻> 推进到 <新时刻>：本次读到 R 条会话、候选清单列出 M 条、记入 K 个会话键（较上次读数新增 N 条）`。
   别把 N 念成清单条数——N 是「比基准新的那几条」的分臂数，M 才是候选清单的长度，K 是进基准的键数
   （一条会话可能贡献两个键）。除它之外没有任何路径推进基准
   （`followup_tracker`、`live_apply` 收尾采集与 dry-run 都只读只报），因为每跑一次就推进会把「用户没看过的」换成
   「脚本没见过的」，下一场就报「新增 0」而用户一条没看。它与 `--send`／`--only` 互斥：真发那一场
   会点开会话，点名过滤后列出的只是子集，两种都推不出「这批」。
   完成：用户问「先看哪几条」时你给的是候选清单里 `线上新` 那几条（eid／公司／原文）加上水位线时刻，
   并说明 `watermark.state`；每跑一次 `--mark-read` 都对应用户说过一次「看过了」。

## 边界

- 静默时段（默认 0-7 点）脚本会自动暂停；用户要求立即投递时改 `APPLY_QUIET_HOURS=off`，
  但默认保留。
- 熔断：检测到验证码/安全验证（risk_blocked）脚本即停下并存证 `private/data/crash/`——
  当日不要重启投递，汇报中必须展示截图证据并等用户人工处置。
- **浏览器进程消失归因硬规则（#54，2026-09-27）**：遇到 page_error 连环出现、「浏览器
  没了」类事件（`Target page, context or browser has been closed`），agent **只报事实、
  不下定性**——report.json 的 `browser_exit` 分类与 evidence（pid 探活结果 /
  DiagnosticReports 崩溃报告路径、有无风控信号）+ 可能原因清单，一并呈给用户，**用户
  确认后再定性**（2026-09-26 实测：浏览器被外部终结，agent 误按缺陷排查走了一轮）。
  分类字段由脚本机械判定（进程探活 + 崩溃报告检索），`external_kill`（外部终结）保留
  「待确认」语义——最终定性权在用户。
- **卡死被看门狗击杀**（代码与文件名叫 wedge，中文一律说「卡死」）：两代看门狗的开关、
  三份证据的读法（`wedge-stack.txt` / `heartbeat.json` 末三键 / `WATCHDOG_KILLED.json` 的
  `net_probe`，含四格判读与「`null` ≠ `0`」）、跳岗台账与 `LIVE_WEDGE_*` 开关，全部在
  `docs/agents/apply-forensics.md`。agent 遇 `[warn] … 被看门狗强杀` 呈报时**只报事实**
  （击杀相位——起飞前自检还是会话中途 / 停摆时长 / 最后节点 label / 岗位名与 eid），
  根因定性归用户；相位决定下一步往哪儿查，漏报它等于让用户重新猜一遍。被杀会话无 `report.json`
  （零投递零台账），重启本身不构成风控信号。
- 简历出现过的公司不投递（用户约定 2026-09-10）：清单在 `private/data/excluded_companies.json`
  （个人履历信息，不入 git；新 clone 按自己履历填写），brandName 命中即跳过——脚本自动过滤，
  无需 agent 判断。
- 求职定位边界（**定位与阈值属于个人数据，本仓只规定判据的形状**）：
  - 每个人的可投/必跳范围都不同，因此 agent 判定只看这五类：职能方向是否匹配、
    语言与栈是否硬缺失、级别/方向是否对口、薪资是否低于下限、是否命中排除公司清单
    （`private/data/excluded_companies.json`）。
  - 具体取值写在 `private/profile_background.md` 的个人层与行业层里，按当前 profile 读取；
    **本仓不写死任何人的履历、年限、雇主、薪资下限或目标行业**。
  - **存疑时倾向投**：漏投的代价高于错投；分类反馈里如实呈现每类跳过的理由供人裁量。
- 投递是外部动作：每次实际发送前需要用户当次授权（说「开始投简历」即为该次授权）。
- 入站回复也是外部动作：说「回复入站」**不等于**授权发送，它只授权跑到只读候选清单为止；
  `--send` 那一下要用户当次点名（口径见「入站回复」小节步骤 3）。
- 脚本的机械过滤只有三份台账：`applied_history`（同岗投过不重投）、`excluded_companies`
  （简历出现过的品牌）、`wedge_skipped`（卡死跳岗台账）。**`greeting_memory.json` 不参与过滤**——
  它是 `learn` 的回写目标与「同品牌再触达」时引用历史意向的读取源（见步骤 2 引用规范），
  要按 HR 已拒/已联系的历史避开某品牌，得 agent 自己读它，脚本不会替你判。
