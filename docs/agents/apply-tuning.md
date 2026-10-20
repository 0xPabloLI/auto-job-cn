# 投递节奏与额度调档

> 什么时候到这里：要改起草超时、关推荐流、关录像、调采集上限或保留天数，或出现「风控
> 信号」之后要不要降档。**默认值以代码为准，本文不抄**（抄的副本会烂）：
>
> ```bash
> python3 -c "import sys;sys.path.insert(0,'scripts');from _limits import load_config;print(load_config({}))"
> ```
>
> 布尔 env 的取值表真源：`scripts/_limits.py` 的「布尔 env 开关」小节（各开关的表**刻意
> 不同**，差异由行为用例钉着）。看门狗/取栈/出网探针/跳岗台账那几个开关在
> `docs/agents/apply-forensics.md`；`LIVE_PACE` 与 `LIVE_QUERIES` 留在
> `docs/agents/apply-runbook.md` 步骤 1（每轮都用得到）。

## 会话级开关
`LIVE_QUERIES` 与 `LIVE_PACE` 的用法在 `docs/agents/apply-runbook.md` 步骤 1（每轮都用得到，
不在这里重复）。剩下这几条是「要偏离默认时才查」的，写法是**读法与代价**，数值以
`load_config({})`（本文顶部那条命令）现取为准：

- `LIVE_REPLY_TIMEOUT`（默认 1200 秒）—— 每个岗位等 agent 草稿的窗口。起草要跑白名单核验加
  四道 gate，600 秒会误杀（2026-09-25 上调）；窗口耗尽另有 90 秒宽限复查救迟到草稿（#55）。
  ⚠️ 会话进行中不要为了等一个决策把这 1200 秒耗干——实测一场因此丢了 5 个岗，纪律写在
  runbook 步骤 2「决策问题不占用草稿窗口」。
- `LIVE_RECORD`（默认开）—— 录屏。关掉就没有 `trimmed_*.webm` 可核（步骤 4 的视频那段判据随之
  不适用）；ffmpeg 缺失或剪辑子进程失败同样只 warn、保留原片。
- `LIVE_RECOMMEND`（默认开）—— 关掉推荐流整轮，退化为纯搜索流。**这是推荐流前置之后的额度配比
  退路之一**（另一条是下面的 `LIVE_RECOMMEND_CAP`：留着但限量）：推荐流先吃额度（这条代价的完整
  叙述在 runbook「两个来源」段，那里是唯一一份），想把额度全留给搜索词就关它。
- `LIVE_COLLECT_CAP`（默认 120 条，0 = 不限）—— **单来源**单次采集上限，推荐流与搜索流各自
  受它管；两个来源合并后再按 `encryptJobId` 去重。旧名 `LIVE_RECOMMEND_PER_ROUND` 仍可用，
  会打一条「已并入」提示。
- `LIVE_RECOMMEND_CAP`（0 = 不限；默认值现取 `live_apply.RECOMMEND_CAP_DEFAULT`，本文不抄）——
  推荐轮的**消费**配额：管「吃多少个」，与上一条管「看多少条」分家，故 `recommend_listing` /
  `recommend_scrolls` 两个采集读数不受它影响。⚠️ 代价（#65）：排在配额之后的对口岗本场不投、
  也不进任何台账——不是超时也不是卡死，是没轮到；改小=省时间但可能漏投一个达标岗，放开写 `=0`，
  整轮关掉用 `LIVE_RECOMMEND=off`。默认值的依据只有 n=1 场（会话 `20261009-104050`：采集 49 条、
  去重后 9 个候选全不对口、投 0），下一场若配额内 0 命中而搜索轮有命中，就把配额往上调一档再观察。
- `LIVE_RETENTION_DAYS`（默认 7 天，0 = off）—— 每次启动顺手清理超期的 `videos/<ts>/` 与
  `live_session/<ts>/shots/`；`report.json` 与队列等会话产物保留。
## 日上限阶梯（live 同样生效，#20 + 用户约定 2026-09-10 自动化）


日上限**自动阶梯爬升**（live 每次启动经 `effective_daily_limit` 推进，脚本内自动处理）：
首日 20，此后每天随机 +2-5，40 封顶（`APPLY_LADDER_CAP` 可调），状态落
`private/data/daily_limit_state.json`（同日幂等；停机数天后跨天一次性追平增量）。
实际发送量再叠加 ±5 抖动（`APPLY_CAP_JITTER`）。
显式设置 `APPLY_DAILY_LIMIT` 即固定值、阶梯关闭——回退安全档用这个。

风控信号出现时降档：把 `APPLY_LADDER_CAP` 调回已验证的安全档（如 30）观察 2-3 天；
信号持续则显式设 `APPLY_DAILY_LIMIT` 固定更低值。需提量到 60/80 时调高 `APPLY_LADDER_CAP`，
同样每档观察 2-3 天。

判定「风控信号」：熔断停批（risk_blocked）、投递成功率骤降、followup 发现账号异常提示。
任何一档出现信号：降回上一档重新观察 2-3 天，不要停留在触发档位硬跑。
