# 集成/E2E 冒烟（投递链路）

> 什么时候到这里：人工触发 #40 集成冒烟、Wave 准出门禁要求「真实链路留证」、或 live_apply 主链路改动后需要回归验证。
> 单元测试全 mock（CI 默认跳过 `integration` 标记），真实链路的登录态/风控/页面形态只有这里能验。

## 两种模式

| 模式 | 发送 | 名额消耗 | 可重复 | 证据 |
| --- | --- | --- | --- | --- |
| **A dry-run**（本票新增） | 否——发送前 gate 截停 | 零 | 随时 | `report.json`（`dry_run: true`）+ 截图 |
| **B 真实冒烟** | 是 | 1-2 个 | 不建议频繁 | 台账新增记录 + 退出码 + 截图 |

## 前置

- **有头浏览器在本机可跑**（2026-09-21 `1 passed in 136s`；2026-09-22 08:06 推荐流只读探测再验通过）。
  `live_apply` 用有头浏览器（`headless=False` 硬编码），起不来时按下方条目逐条核对：第一条是
  录像链路时代的**史料**（已随 #105 下线，现行链路不会再触发）；现行两类**是缺依赖、不是链路缺陷**，
  失败形态各不相同（数分钟超时 vs 数秒失败——后者的样本在下面那条史料里），看**失败耗时**就能分流：
  - **Playwright 自带 ffmpeg 在位**（`~/Library/Caches/ms-playwright/ffmpeg-1011/`）——**环境史料**：该条目的
    触发面是投递录像链路，已随 #105 于 2026-10-23 整体下线；其他链路（faulthandler 取栈、出网探针）都不经过
    ffmpeg。保留只为历史排查参考：当年录像链路开着时，缺 ffmpeg 会让 driver 进程直接退出，Python 侧只看到
    `BrowserType.launch_persistent_context: Connection closed while reading from the driver`
    （**数秒内失败，不是超时**）。装法（历史）：`python3 -m playwright install ffmpeg`（约 1 MiB）。
  - **首跑辅助资源已下完**（UBO 插件 + GeoLite2 库）：走代理可能极慢，叠加 Playwright 180s 启动超时，表现为
    「终端几分钟没动静，最后 `Timeout 180000ms`」。
  - **⚠️ 2026-09-23 更新：agent 会话内起有头 camoufox，先带上 `MOZ_DISABLE_*` 那组变量**（本文档此前称
    `sandbox initialization failed` 是「无害噪音、别去关沙箱」）。三条对照见下方「第四种形态」：沙箱内败、
    `dangerouslyDisableSandbox` 也败、加 `MOZ_DISABLE_*` 成功；已排除下载混淆项（GeoLite2 于 09-21 已缓存）。
    **但成功样本 n=1、未做撤变量反向复测** ⇒ 先按这条试，复测前不上升为铁律（技能
    `browser-launch-env-repair` 里并列保留了 09-21 的相反记录）。
- **依赖安装（本机无固定 venv 时）**：`uv run --no-project --python 3.12 --with "camoufox[geoip]==0.5.5"
  --with "playwright==1.60.0" …`。三个坑：① 必须 `--no-project`——项目 `requires-python>=3.9` 与 camoufox
  要求的 `>=3.10` 冲突，带项目解析必然报 unsatisfiable；② 必须带 `[geoip]`——生产与 `open_browser.py` 都是
  `geoip=True`，缺该 extra 会**数秒内**抛 `NotInstalledGeoIPExtra`（与下面的超时形态靠耗时就能分开）；
  ③ 代理不稳（pypi `unsuccessful tunnel`）时加 `--offline` 命中 uv 缓存。
- **起不来的第三种形态：进程启动即退出 / 启动超时**（`SIGABRT` 秒退、`TargetClosedError: … has been
  closed`、或 `Timeout …ms exceeded`）。**排查顺序**（①②③ 全过再考虑移交普通终端）：
  1. **清 profile 残留 `.parentlock`**——崩溃后残留的 0 字节锁会让新实例判定「profile 正被占用」。
     用改名而非删除（可回滚）：`mv private/.camoufox_profile/.parentlock{,.bak-$(date +%H%M%S)}`。
  2. **确认无残留进程占同一 `user_data_dir`**：`pgrep -fl -i camoufox`。`persistent_context` 的
     `user_data_dir` 同一时刻只有一个持有者，上一个崩溃实例没退干净会顶掉新实例。
  3. **确认内存宽裕**：启动峰值实测吃 1.6GB 以上。2026-09-22 凌晨 32GB 机上启动瞬间系统 free 掉到
     63MB、swap 已用 2.78G/4G，同一 profile 连败 6 次；次晨内存宽裕时**首次即成功**。跑之前先关掉
     不必要的大内存应用。
  已确认**与结果无关**的变量（无需重试）：`--headless` 开关、`geoip`/`humanize` 开关。
  **agent 沙箱开关无效**（`dangerouslyDisableSandbox` 开与不开都同样失败，见下）。
- **起不来的第四种形态（2026-09-23 实证，agent 会话内特有）**：`Timeout 180000ms exceeded`，日志里
  `sandbox initialization failed: Operation not permitted` 之后紧跟
  `[Parent <pid>, IPC I/O Parent] WARNING: process <cpid> exited on signal 11`。
  **判据**：`sandbox_init()` 失败 **+ 内容进程 SIGSEGV(11)** 同时出现 ⇒ 就是它，不是内存、不是锁。
  **疑似根因**（待反向复测坐实）：agent 会话内是**嵌套 macOS 沙箱**，Firefox 内容进程再 `sandbox_init()`
  被拒（父进程已被限制），内容进程随即段错误 → 主进程等不到就绪 → 180s 超时。
  **解法**：起进程前导出（Firefox 会跳过对应的 `sandbox_init`）：

  ```bash
  export MOZ_DISABLE_CONTENT_SANDBOX=1 MOZ_DISABLE_GMP_SANDBOX=1 MOZ_DISABLE_RDD_SANDBOX=1 \
         MOZ_DISABLE_SOCKET_PROCESS_SANDBOX=1 MOZ_DISABLE_UTILITY_SANDBOX=1
  ```

  **对照组（2026-09-23 00:47→00:57，同 profile、同命令、同一 `152.0.4-beta.30`）**：
  ① 沙箱内 → 180s 超时；② `dangerouslyDisableSandbox=true` → **同样** 180s 超时（证明不是工具级沙箱）；
  ③ 加 `MOZ_DISABLE_*` → **7 个进程存活、打印「浏览器已打开」**。三次内存水位相当（free 174MB / 66MB / 185MB，
  swap 均 ≈2.0G/3G）⇒ **内存不是本次的差异项**，别再拿内存解释这个形态。
  区分「浏览器起不来」与「脚本有 bug」：用最小启动脚本（只起浏览器开 `about:blank`）先验一遍。
- **已跑通记录（2026-09-22 08:06）**：同 profile / 同命令 / 同一浏览器二进制（`152.0.4-beta.30`），
  清 `.parentlock` 后**推荐流只读探测首次即成功**（`title: BOSS直聘`、XHR 39 条、`job_detail` 链接 12+）。
  故「agent 会话内可直接跑」成立，前提是浏览器进程能起来。
- **一次误判的教训（2026-09-22 凌晨）**：worktree 上出现过一个「不是我写的」commit，当时据此判定有
  并行 session 抢 profile。事后用 `git reflog` + Session-Id 核对，那是本 session 第一次 commit 的记录
  （hook 提示被误读成拦截），**并无第二个会话**。归因前先查 reflog，别凭「这条不是我写的」下结论。
- 本机已 onboarding：`private/profiles.json` 注册表 + 浏览器登录态（步骤见 `docs/agents/onboarding.md`）。
- camoufox 浏览器二进制在位：`camoufox path` 有输出、`installed_verstr()` 不抛 `CamoufoxNotInstalled`。
  缺失时用 `camoufox fetch`（本机代理可能中途 `IncompleteRead`；失败可 `curl -C -` 续传官方 zip 后按
  `browsers/<repo>/<version>-<build>-<sha8>/` 布局解压，并补 `version.json` + `config.json.active_version`
  + `.0.5_FLAG`——缺 `.0.5_FLAG` 时 pkgman 会把整个 `INSTALL_DIR` 当旧数据清掉）。
- 搜索词有「新鲜岗位」可投：已沟通过/排除公司/已投过的岗位会被跳过，全是旧面孔时冒烟走不到 gate。
- 草稿在手（模式 A 用）：一份能过四道 gate 的招呼语文本——AI 痕迹零、内部标记零、无未实测状态词、实体在 fact_cards 白名单内、level 合法、notes 带来源。dry-run 不会发送，草稿只用于喂 gate；真实起草流程见 `docs/agents/apply-runbook.md`。

## 模式 A：dry-run 冒烟（零发送）

1. **设草稿环境变量**（缺省测试自动 skip，不算失败）：

   ```bash
   export SMOKE_REPLY_TEXT="您好，看到贵司在招区块链开发，我有 7 年合约开发经验，聊过几个同类岗位，方便看下简历吗"
   # 可选：SMOKE_REPLY_LEVEL（默认 中）、SMOKE_REPLY_NOTES（默认占位来源）、LIVE_QUERIES（默认用 profile 的 queries）
   ```

   完成标准：`echo $SMOKE_REPLY_TEXT` 非空。

2. **跑集成测试**（会真开浏览器窗口）：

   ```bash
   APPLY_START_JITTER_MAX=1 python3 -m pytest scripts/test_live_apply_integration.py -m integration -s
   ```

   `APPLY_START_JITTER_MAX` **必须设一个小正整数**：默认 600（0-10 分钟随机延迟）会吃掉本测试 900s 的
   起草看门狗（`WORKER_DEADLINE`），表现为「终端几分钟没动静、最后 `agent_timeout`」。填 `0` 无效——
   `get_int` 只收正整数，会打印 `[配置] … 需为正整数，使用默认 600` 后回落。

   完成标准：浏览器自动打开 → 搜索 → 进详情 → JD 写入 `request_N.json` → 测试线程自动回写 `reply_N.json` → gate 校验 → 打印 `[dry-run]` 截停 → `report.json` 出现后自动写 CLOSE 收尾，退出码 0。

3. **机械核验**（测试断言已覆盖，留证按此对照）：
   - 退出码 0，且 `report.json` 的 `dry_run: true`、`results` 里有 `dry_run_ok` 条目；
   - `applied == 0`、`greetings.json` 条目数不变——名额与台账零消耗；
   - 不点「立即沟通」：站内不会出现新会话。

4. **留证**：会话目录（`private/data/live_session/<ts>/`，含 `report.json` 与 `shots/`）。

已知的无害副作用：会话收尾会按 #37 追加一条 EDD 趋势快照（`applied=0`，指标数值不变）。

**`gate_blocked` 不是故障**：草稿被机械 gate 拦下本身就是 gate 工作的证据。核对 `report.json` 中 `detail` 的违规项，换合规草稿重跑即可。`agent_timeout` 说明测试线程没等到 request（多为搜索词全是旧面孔），换 `LIVE_QUERIES` 重试。

## 模式 B：真实冒烟（人工在环）

步骤 = `docs/agents/apply-runbook.md` 的标准投递流程 + 两条限量约束：

- **限量 1-2 岗**：第 1 岗投成（终端出现 `[ok]` 与台账行）后即写会话目录里的 `CLOSE` 文件优雅停止（或 Ctrl+C）。
- **留证**：`greetings.json` / `apply_log.jsonl` 新增 1-2 条 + 退出码 + 会话目录截图，与模式 A 证据合并归档。

## 边界

- 节奏与日上限规则（时段系数/静默/阶梯）在两种模式下的生效方式不同：**模式 B 完全生效不豁免**（真实发送与正式投递无差别，限量靠 CLOSE）；**模式 A 的集成测试显式设 `APPLY_QUIET_HOURS=off`**——dry-run 零发送、零额度、零对话建立，静默时段防的是发送风控而非读页面，豁免只影响测试何时能跑、不产生任何风控暴露面；人工跑模式 A 若想全按真实节奏，去掉该豁免直接跑也一样（只是 0-7 点会被拒绝启动）。
- 冒烟会话同样受 #34 状态词 gate、#27 备注核验约束：草稿违规被拦是预期行为，不要为过冒烟放宽 gate。
