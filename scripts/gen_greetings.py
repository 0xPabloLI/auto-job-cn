#!/usr/bin/env python3
"""
招呼语工具（issue #4/#18/#19/#34）：四道确定性 gate + 草稿晋级 + growing memory + 回复率评估。

用法：
  python3 scripts/gen_greetings.py promote
      把 greeting_drafts.json 中 status=approved 的草稿合并进 greetings.json，
      供 live_apply 投递队列消费。人工审核 = 直接编辑 drafts 文件改 status/greeting。
  python3 scripts/gen_greetings.py learn
      读 followup_report.json + applied_history.json，把 HR 回复情况回写 greeting_memory.json。
  python3 scripts/gen_greetings.py eval
      followup 报告 + 回复标注 + 招呼语台账 → 分组回复率评估（按岗位级别/长度/结构变体），
      产出 greeting_evaluation.json；样本不足（组 <5 / 总量 <30）显式不出结论（#19）。
  python3 scripts/gen_greetings.py promote-winners --group <key> [--group <key>…] --yes
      评估候选 → private/kb/winning-examples.md（正例 few-shot 池 + 避坑清单）。
      人工 gate：先看 eval 报告，显式点名候选组并 --yes 才写盘；正例池供 agent 起草参考
      （runbook 模式 A「起草规范」）。
  python3 scripts/gen_greetings.py doctor [--sync]
      知识库硬事实 vs 白名单同步检查（缺口默认拦截投递出口）；--sync 把 fact_cards
      已审事实 diff 核对后编译进文末白名单 JSON 块（#25：新审卡片无需手改 JSON）。

知识库：private/profile_background.md（唯一事实来源，含禁写清单，防编造）；
     private/kb/fact_cards.md 事实卡片常驻前置（#18 M1），其白名单区供规则扫描 gate
     （M6②：年限/实体确定性比对，命中标记进 review_issues，人工裁决）。
草稿撰写：由 agent 大模型按 runbook 模式 A「起草规范」直接写入 drafts（本地 LLM drafter
     已退役，generate 命令已删除——2026-09-10 用户约定，见 #25 关联的管线决策）。
"""

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _atomic_io import write_text_atomic  # noqa: E402  （scripts 平铺导入，同 _llm.py）
from _edd import (  # noqa: E402  #37 EDD 趋势观测；stamp/parse_ts 别名复用（review J1 去重）
    TREND_APPENDED,  # #59 修复轮 R4-5：append_trend 的返回状态（判序只有一份，长在咽喉里）
    TREND_DUPLICATE,
    TREND_FALSE_ZERO,
    append_trend,
    density_due,  # #45 密度提示判定（record_session_snapshot 透传 + eval 命令接线）
    load_pending_candidates,  # #45：候选审阅建议清单
    load_trend,
    parse_ts as _parse_stamp,
    render_density_prompt,  # #45：密度达标提示块
    render_trend_table,
    review_due,
    stamp as _stamp,
    trend_snapshot,
)
from _limits import notify  # noqa: E402  #45：密度达标的 macOS 系统通知（复用，不自造通知通道）
from _profile_paths import (  # noqa: E402  #28：私有路径唯一出口，全函数化
    private_path,
    profile_line,
    resolve_active_profile,
    resolve_kb_profile,
)

ROOT = Path(__file__).resolve().parent.parent
KB = ROOT / "private/profile_background.md"
FACT_CARDS = ROOT / "private/kb/fact_cards.md"  # 事实卡片（#18 M1）：白名单唯一来源，规则扫描 gate 读这里
INDUSTRIES = ROOT / "private/kb/industries"  # 行业层：按 profile 选用（blockchain/ai…），不含个人信息可随团队共享
PROFILES = ROOT / "private/kb/profiles"  # profile 配置：一份简历配一个行业背景（blockchain.json / ai.json）
DRAFTS = ROOT / "private/data/greeting_drafts.json"
GREETINGS = ROOT / "private/data/greetings.json"
MEMORY = ROOT / "private/data/greeting_memory.json"
FOLLOWUP = ROOT / "private/data/followup_report.json"
HISTORY = ROOT / "private/data/applied_history.json"
ANNOTATIONS = ROOT / "private/data/reply_annotations.json"  # #19：回复分类标注（followup_tracker 落盘，跨轮次存活）
EVALUATION = ROOT / "private/data/greeting_evaluation.json"  # #19：分组回复率评估报告
WINNING = ROOT / "private/kb/winning-examples.md"  # #19：few-shot 正例池 + 避坑清单（promote-winners 人工确认后写入）

# 样本量门槛（#19 triage）：组内 < MIN_GROUP 条不出结论；总量 < MIN_TOTAL 不产正/反例候选
MIN_GROUP = 5
MIN_TOTAL = 30

# #64：招呼语台账 source 词表单一真源（live_apply 经 `import gen_greetings as g` 取用）。
# legacy 打标由 live_apply.migrate_greeting_ledger 在会话开跑时落，新增投递一律
# source="live_apply" + 真实 date。
LEGACY_UNDATED = "legacy_undated"
LEGACY_DRAFT_ONLY = "legacy_draft_only"
LEGACY_SOURCES = (LEGACY_UNDATED, LEGACY_DRAFT_ONLY)


def is_legacy_greeting(entry):
    """#64：source 落在 legacy 词表＝历史遗留条目（正文与实发可能不一致），不参与按文案分组。"""
    return entry.get("source") in LEGACY_SOURCES


# AI 痕迹词表（issue #7 启示 7，来源：cover-letter-automation-research.md 启示 7）：
# 海投腔/一眼假的措辞，生成后确定性扫描，命中条目并入 review_issues 供人工审核。
AI_FINGERPRINT_WORDS = [
    "贵公司",
    "贵司",
    "本人具备",
    "本人精通",
    "您好，我非常希望",
    "不胜感激",
    "殷切期盼",
    "精通",
    "主导",
    "带领",
    "顶级",
    "资深",
    "期待有机会",
    "期待您的回复",
]


def ai_fingerprint_scan(text):
    """返回命中的痕迹词列表；干净文本返回空列表。"""
    return [w for w in AI_FINGERPRINT_WORDS if w in text]


# 领域通用词：不构成能力/经历主张，规则扫描豁免（#18）
_SCAN_ALLOWED_TOKENS = {
    "web3",
    "defi",
    "nft",
    "dao",
    "dapp",
    "ai",
    "llm",
    "api",
    "json",
    "http",
    "https",
    "remote",
    "e2e",
    "ci",
    "ui",
    "ux",
    "jd",  # 输入侧词（#9 E2E）：匹配 JD 的某某要求，永不构成候选人人设主张
    # 白名单实体的描述组成词/通用格式词（#9 E2E 第二轮）：audit PDF、MetaTx 免 Gas、
    # VRF reveal、Aave V4 Hub-Spoke、CI workflows——都是机制/格式词，不构成人设主张
    "pdf",
    "gas",
    "metatx",
    "reveal",
    "hub",
    "spoke",
    "workflows",
    # 行业通用词/JD 原词（2026-09-15 用户确认：GameFi 行业词条、bot 为 JD 原词——
    # 引用 JD 提到的词回应岗位期待不构成捏造经历，误拦导致可投岗位丢失）
    "gamefi",
    "bot",
    "bots",
}
# 同一技术栈的全称/异写 → 白名单规范名（2026-09-15 误拦教训：草稿写全称 TypeScript/
# Node.js 而白名单只有缩写 TS/Node，精确比对拦截了本可发送的 Web3 全栈岗）。
# 比对前先归一，白名单 JSON 无需为写法差异重复收录。
_ENTITY_ALIASES = {
    "typescript": "ts",
    "nodejs": "node",
    "node.js": "node",
    "web3.js": "web3",
    "postgres": "pg",
    "postgresql": "pg",
    "kubernetes": "k8s",
}
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 卡片机器区（白名单 JSON 块）的唯一解析入口：extract_whitelist / 推导剥离 / --sync 编译共用
_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)


def _entity_keys(tok):
    """实体 token 的两种归一键（原形 + 去内连字符），扫描比对 / 推导 / --sync 登记共用。"""
    t = str(tok).rstrip(".-").lower()
    return t, t.replace("-", "")


def _cn_year_to_int(cn):
    """一~十九/二十/二十X 等简单中文数字解析；解析不了返回 None。"""
    if cn in _CN_NUM:
        return _CN_NUM[cn]
    if "十" in cn:
        tens, _, ones = cn.partition("十")
        if len(cn) <= 3 and (not tens or tens in _CN_NUM) and (not ones or ones in _CN_NUM):
            return (10 if not tens else _CN_NUM[tens] * 10) + (_CN_NUM[ones] if ones else 0)
    return None


def extract_whitelist(cards_text):
    """从事实卡片解析机器白名单区（```json {"years": [...], "entities": [...]} ```）。
    多词实体拆成单词入集合（"Chainlink VRF" 同时放行 Chainlink/VRF）；
    无白名单区或坏 JSON 返回 None（调用方跳过扫描）。"""
    m = _JSON_BLOCK_RE.search(cards_text)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    if "years" not in data and "entities" not in data:
        return None
    entities = set()
    for e in data.get("entities", []):
        for tok in str(e).split():
            entities.add(tok.lower())
            entities.add(tok.lower().replace("-", ""))
    try:
        years = set()
        for y in data.get("years", []):
            if isinstance(y, float) and not y.is_integer():
                return None
            years.add(int(y))
    except (TypeError, ValueError):
        return None
    return {"years": years, "entities": entities}


def load_whitelist_file(path):
    """卡片文件 → 白名单；文件缺失/解析失败返回 None（生成侧跳过扫描并提示）。"""
    path = Path(path)
    if not path.exists():
        return None
    return extract_whitelist(path.read_text(encoding="utf-8"))


def _scan_claims(text, whitelist):
    """rule_scan 的结构化内核（#25）：返回 (kind, value, raw) 元组——
    kind "year"（value 为数值、raw 为原文写法）或 "entity"（value=raw=token 原文）。
    字符串展示（rule_scan）与白名单推导（derive_card_facts）共用同一判定，
    避免把人读格式当数据接口、文案一改推导静默失效。"""
    years, ents = whitelist.get("years", set()), whitelist.get("entities", set())
    out = []
    for n in re.findall(r"(?<![\d.])(\d{1,2}(?:\.\d{1,2})?)\s*年", text):
        if float(n) not in years:
            out.append(("year", float(n) if "." in n else int(n), n))
    for cn in re.findall(r"([一二两三四五六七八九十]{1,3})年", text):
        v = _cn_year_to_int(cn)
        if v is not None and v not in years:
            out.append(("year", v, cn))
    for tok in re.findall(r"[A-Za-z0-9][A-Za-z0-9+#.-]{1,30}", text):
        t, t_nohy = _entity_keys(tok)
        t = _ENTITY_ALIASES.get(t, t)
        t_nohy = _ENTITY_ALIASES.get(t_nohy, t_nohy)
        if len(t) < 2 or t in ents or t_nohy in ents or t in _SCAN_ALLOWED_TOKENS:
            continue
        # 纯数字/版本号（#9 E2E 第二轮：21链、193测试、3.5标注）是标注不是人设主张
        if re.fullmatch(r"(?:v?\d+(?:\.\d+)*)", t):
            continue
        # 连字符/加号组合（#9 E2E 误报：marshmallow-v1、dfpunk-aztec 等词尾带 + 号的组合）：
        # 版本号/纯数字段（v1、21）是标注不是人设主张，剔除后再比对；
        # 剩余每一段都可佐证才放行，任一段未知仍命中
        segs = [s for s in re.split(r"[-+]", t) if s and not re.fullmatch(r"v\d+(\.\d+)*|\d+(\.\d+)*", s)]
        if not segs:
            continue  # 纯版本号组合（doctor 实测：V3+V4）是标注不是人设主张
        if all(s in ents or s in _SCAN_ALLOWED_TOKENS for s in segs):
            continue
        out.append(("entity", tok, tok))
    return out


def rule_scan(text, whitelist):
    """确定性禁推测扫描（#18 M6②）：年限数字与拉丁实体对卡片白名单比对。
    只标记不拦截——命中进 review_issues 由人工裁决。中文公司名等无法确定性提取，
    不在本扫描范围（仍是 reviewer + 人工审核职责）。判定内核见 _scan_claims。"""
    years_sorted = sorted(whitelist.get("years", set()))
    hits = []
    for kind, _val, raw in _scan_claims(text, whitelist):
        if kind == "year":
            hits.append(f"年限越界: {raw}年（白名单 {years_sorted}）")
        else:
            hits.append(f"未佐证实体: {raw}")
    return hits


# 内部标记扫描（#9 E2E 第二轮起迭代）：8b drafter 无视 prompt 约束、reviewer 也拦不住的
# 泄漏——§编号、卡片/条目编号（第四轮变体）、白名单/知识库元数据字样、JD 字样。
# 确定性兜底，命中进 review_issues。
def internal_marker_scan(text):
    """返回正文泄漏的内部标记列表；干净正文返回空列表。"""
    hits = []
    if "§" in text:
        hits.append("§编号引用")
    # 卡片N/条目N（#9 E2E 第四轮）：§ 被压下后的变体——「卡片式的」等普通词义不命中
    if re.search(r"(?:卡片|条目)\s*\d", text):
        hits.append("卡片/条目编号引用")
    if "白名单" in text or "知识库" in text:
        hits.append("白名单字样")
    if re.search(r"(?<![A-Za-z])JD(?![A-Za-z])", text):
        hits.append("JD字样")
    return hits


# 运行时状态断言词（#34 真实事故）：KB 里只有「2022 年部署到主网并已验证」这类**部署快照**，
# 把快照写成「生产运行至今 / 持续运行 / 在跑」是时态滑坡——宣称业务此刻仍在跑，属未实测的
# 现状断言。命中后须有近期实测佐证（reply_N.json 的 state_evidence 备注区）才放行；
# 快照式过去时（「部署过并已验证」）不命中，是规范要求的写法。
STATE_ASSERTION_WORDS = [
    "运行至今",
    "持续运行",
    "在线运行",
    "至今运行",  # 「稳定运行至今」由「运行至今」覆盖
    "在跑",
    "运行中",
    "至今活跃",
    "仍在运行",  # 「至今在跑 / 还在跑 / 仍在跑」由「在跑」覆盖
]
STATE_EVIDENCE_MAX_AGE_DAYS = 7  # 「近期实测」口径：当天到第 7 天算近期
_STATE_EVIDENCE_DATE_RES = (
    re.compile(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})"),  # 2026-09-14 / 2026/9/14
    re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"),  # 2026 年 9 月 14 日
)
# 延续性副词 + 持续副词的组合（#34 方向④）：状态断言不止「运行至今」一种写法——
# 「目前仍在服务 / 至今仍在使用 / 一直在跑」同属未实测现状声称。要求「仍/还/持续/稳定」
# 紧跟延续词，才不会误伤「我目前在寻找新机会」这类正常表述。
_CONTINUITY_STATE_RE = re.compile(r"(?:至今|目前|现在|一直)(?:仍|还|持续|稳定)")


def has_recent_state_evidence(evidence, today=None):
    """state_evidence 备注区是否含近 STATE_EVIDENCE_MAX_AGE_DAYS 天内的实测日期
    （「链上 polygonscan 2026-09-14 调用确认」「链上 2026 年 9 月 14 日复核」都认）。
    坏形态 / 无日期 / 过期 / 未来日期一律 False——机械门默认方向是拦截（fail-closed），
    未来日期不是实测。"""
    if not isinstance(evidence, str) or not evidence.strip():
        return False
    today = today or date.today()
    for pattern in _STATE_EVIDENCE_DATE_RES:
        for y, m, d in pattern.findall(evidence):
            try:
                day = date(int(y), int(m), int(d))
            except ValueError:
                continue
            if 0 <= (today - day).days <= STATE_EVIDENCE_MAX_AGE_DAYS:
                return True
    return False


def runtime_state_scan(text, evidence=None, today=None):
    """运行时状态词扫描（#34）：返回**未获实测佐证**的状态断言（词或延续性短语）；
    干净文本返回 []。只回答两个问题——正文有没有现状断言、有没有近期佐证，不判措辞好坏。"""
    hits = [w for w in STATE_ASSERTION_WORDS if w in text]
    hits += [m.group(0) for m in _CONTINUITY_STATE_RE.finditer(text)]
    if not hits:
        return []
    return [] if has_recent_state_evidence(evidence, today=today) else hits


def load_industry_text(profile=None, profiles_dir=PROFILES, industries_dir=INDUSTRIES):
    """profile json → 对应行业层文本（private/kb/industries/<industry>.md）。
    profile 缺省时走 resolve_kb_profile 解析链（#29 P1 默认值收敛点）。
    profile 文件/industry 字段/行业文件任一缺失都返回空串（拼装时静默降级为纯个人层）。"""
    profile = profile or resolve_kb_profile()
    p = Path(profiles_dir) / f"{profile}.json"
    if not p.exists():
        return ""
    try:
        ind = json.loads(p.read_text(encoding="utf-8")).get("industry") or ""
    except ValueError:
        return ""
    f = Path(industries_dir) / f"{ind}.md"
    return f.read_text(encoding="utf-8") if f.exists() else ""


def load_kb_context(kb_path=KB, cards_path=FACT_CARDS, profile=None, profiles_dir=PROFILES, industries_dir=INDUSTRIES):
    """prompt 装配（#9 架构改造）：个人层（profile_background.md，个人专属）+ 行业层（按 profile 选用）
    + 事实卡片。卡片存在时拼在最前（#18 M1 常驻）；返回 (kb_text, whitelist)。
    当前总量 <100KB 全量进 prompt（选型框架 M2① stuffing），涨到塞不下再上 RAG。"""
    kb = Path(kb_path).read_text(encoding="utf-8")
    industry = load_industry_text(profile, profiles_dir, industries_dir)
    parts = [kb, industry] if industry else [kb]
    p = Path(cards_path)
    if p.exists():
        cards = p.read_text(encoding="utf-8")
        return cards + "\n\n" + "\n\n".join(parts), extract_whitelist(cards)
    return "\n\n".join(parts), None


def kb_whitelist_gaps(kb_text, whitelist):
    """doctor（#9 架构改造诉求③）：知识库新增内容 vs 白名单的同步检查。
    对 KB 文本跑 rule_scan 同款比对——新 repo/新年限写进知识库但忘同步
    fact_cards 白名单时，这里先报出来，防止扫描器对新生成内容漏检或误报。"""
    if not whitelist:
        return []
    return sorted(set(rule_scan(kb_text, whitelist)))


def fact_claims_text(kb_text):
    """doctor 取证范围：「招呼语可用硬事实清单」节（## 8. → ## 9.）。
    该节就是招呼语候选 claim 集合，必须全部入白名单；KB 其余叙述（项目详情/行业层）
    不构成招呼语人设主张，全量扫描只会产出 128 条噪声。找不到该节时回退全文。"""
    m = re.search(r"^## 8\..*?(?=^## 9\.)", kb_text, re.M | re.S)
    return m.group(0) if m else kb_text


_DATE_TOKEN = re.compile(r"(?:19|20)\d{2}(?:[-/.]\d{1,2}){0,2}")


def doctor_report(kb_text, whitelist):
    """doctor 主逻辑：硬事实清单 vs 白名单，返回待补条目（日期等标注性 token 剔除）。"""
    gaps = kb_whitelist_gaps(fact_claims_text(kb_text), whitelist)
    out = []
    for g in gaps:
        tok = g.split(": ", 1)[-1]
        if _DATE_TOKEN.fullmatch(tok) or tok.rstrip(".-") in ("0x",):
            continue
        out.append(g)
    return out


# --- #25 白名单自动推导：cards → derive → doctor diff → --sync 入表 ---

# 推导期否定标记：命中片段不产出候选（卡片「只收正面事实」约定的机器执行面）。
# ⚠ 前缀覆盖 ⚠️/⚠️ 变体；行业语境卡的「缺口/过气」段落同样不算主张。
_DERIVE_NEGATION_MARKERS = (
    "✗",
    "❌",
    "⚠",
    "不可直接主张",
    "不作能力证明",
    "不要写",
    "不装懂",
    "缺口",
    "过气",
    "少用",
    "未部署",
)

# 推导期停用词：英文功能词永不构成人设主张，进 diff 只是把审核者淹没在噪声里
# （仅作用于推导候选；rule_scan 行为不变）。
_DERIVE_STOPWORDS = {
    "to",
    "of",
    "me",
    "the",
    "a",
    "an",
    "and",
    "or",
    "for",
    "with",
    "in",
    "on",
    "at",
    "by",
    "is",
    "are",
    "be",
}

_YEAR_RANGE = re.compile(r"\b((?:19|20)\d{2})(?:\.\d{1,2})?\s*[-/~—至]\s*((?:19|20)\d{2})(?:\.\d{1,2})?")


def _strip_card_markup(cards_text):
    """推导前剔除机器区与噪声：JSON 白名单块、标题/blockquote/分隔线、URL 主机名。
    JSON 块是显式表（union 由 --sync 负责），重复计入推导会让 diff 永远不收敛。"""
    text = _JSON_BLOCK_RE.sub(" ", cards_text)
    lines = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith(("#", ">")) and ln.strip() != "---"
    ]
    return re.sub(r"https?://\S+", " ", "\n".join(lines))


def derive_card_facts(cards_text):
    """卡片正文 → 候选事实（#25）：复用 _scan_claims（rule_scan 判定内核）对空白名单
    的命中做候选来源——入表即不再拦，候选与扫描永远同一套判定。年限另加时间段编译
    （span 与 span-1，2011-2015 → 4/3，支持 2011.12-2015.02 精度；裸年份是日期不收）。
    否定标记片段（✗/❌/不可直接主张等）不产出候选。实体返回小写 token 集合。"""
    text = _strip_card_markup(cards_text)
    years, entities = set(), set()
    for seg in re.split(r"[。；，,;\n]", text):
        if not seg.strip() or any(mk in seg for mk in _DERIVE_NEGATION_MARKERS):
            continue
        for kind, val, raw in _scan_claims(seg, {"years": set(), "entities": set()}):
            if kind == "year":
                years.add(val)
                continue
            t, _t_nohy = _entity_keys(raw)
            if t not in _DERIVE_STOPWORDS:
                entities.add(t)
        for y1, y2 in _YEAR_RANGE.findall(seg):
            span = int(y2) - int(y1)
            if 0 < span <= 40:
                years.add(span)
                if span - 1 >= 1:
                    years.add(span - 1)
    return {"years": years, "entities": entities}


def card_gaps(cards_text, whitelist):
    """卡片候选事实 vs 显式白名单表 → 未入表条目（doctor diff 展示，#25）。
    表仍是 gate 唯一来源：推导只提议，入表必须走 --sync 人工确认。"""
    facts = derive_card_facts(cards_text)
    ents = whitelist.get("entities", set())
    gaps = [t for t in sorted(facts["entities"]) if all(k not in ents for k in _entity_keys(t))]
    return {"years": sorted(y for y in facts["years"] if y not in whitelist.get("years", set())), "entities": gaps}


def sync_card_facts(cards_text):
    """卡片 → 白名单 JSON 块编译落盘（#25 --sync 的纯函数面）。已有块做 union
    （既有条目原样保留、新条目按序追加），无块或坏块则原位重建。返回新文件全文，
    写盘由调用方原子落盘；幂等——卡片与表同步后二次 sync 逐字节不变。"""
    facts = derive_card_facts(cards_text)
    m = _JSON_BLOCK_RE.search(cards_text)
    try:
        existing = json.loads(m.group(1)) if m else {}
    except ValueError:
        existing = {}
    years = sorted(set(existing.get("years", [])) | facts["years"], key=float)
    ents = list(existing.get("entities", []))
    lower = set()
    for e in ents:
        for tok in str(e).split():
            lower.update(_entity_keys(tok))
    ents = ents + sorted(t for t in facts["entities"] if all(k not in lower for k in _entity_keys(t)))
    block = f"```json\n{json.dumps({'years': years, 'entities': ents}, ensure_ascii=False)}\n```"
    if m:
        return cards_text[: m.start()] + block + cards_text[m.end() :]
    return cards_text.rstrip("\n") + "\n\n" + block + "\n"


def promote_drafts(drafts, greetings):
    """approved 且有正文的草稿并入 greetings；同 encryptJobId 原位替换，新岗位追加。
    已投出条目（source=live_apply）不被草稿顶掉（#64）；替换时旧条目的 date/source
    原样带过去——草稿晋级不改变「有没有发出去过」。返回 (new_list, promoted, kept_sent)，
    promoted 按 eid 逐条实数（同 eid 多份 approved 只落 1 条，不虚报晋级数）。"""
    approved = [d for d in drafts if d.get("status") == "approved" and d.get("greeting")]
    by_eid = {
        d["encryptJobId"]: {
            "encryptJobId": d["encryptJobId"],
            "jobName": d.get("jobName"),
            "brandName": d.get("brandName"),
            "level": d.get("level"),
            "greeting": d["greeting"],
            "addressee": d.get("addressee", "hr"),
            "bossName": d.get("bossName"),
            "bossTitle": d.get("bossTitle"),
        }
        for d in approved
    }
    out, seen, kept_sent, promoted = [], set(), 0, 0
    for g in greetings:
        eid = g.get("encryptJobId")
        if eid in by_eid:
            seen.add(eid)
            if g.get("source") == "live_apply":
                out.append(g)
                kept_sent += 1
                continue
            entry = dict(by_eid[eid])
            for k in ("date", "source"):
                if g.get(k) is not None:
                    entry[k] = g[k]
            out.append(entry)
            promoted += 1
        else:
            out.append(g)
    for eid, entry in by_eid.items():
        if eid not in seen:
            out.append(entry)
            promoted += 1
    return out, promoted, kept_sent


def match_brand(company, brand, min_len=4):
    """followup 的 company 字符串与投递历史 brandName 互为子串即视为同一家（含身份后缀噪声）。"""
    if not company or not brand:
        return False
    if min(len(company), len(brand)) < min_len:
        return False
    return brand in company or company in brand


def build_memory(followup, history, annotations=None, now=None):
    """followup_report + applied_history → growing memory（按品牌聚合回复情况）。
    annotations（#21）：interested 对话的 intent_sub 聚合进品牌 intent 列表——
    同品牌再次起草时定向激活历史意向（不全局注入 prompt，膨胀面不存在）。"""
    applied_brands = {a.get("brandName", "") for a in history.get("applied", [])}
    brands, replied = {}, 0
    for conv in followup.get("conversations", []):
        company = conv.get("company", "")
        # replied 单一真源 = status（#30：[已读] 归 awaiting，summary 是消息正文）
        is_replied = conv.get("status") == "hr_replied"
        matched = next((b for b in applied_brands if match_brand(company, b)), company)
        brands.setdefault(matched, {"conversations": 0, "replied": 0})
        brands[matched]["conversations"] += 1
        if is_replied:
            brands[matched]["replied"] += 1
            replied += 1
            ann = annotation_for_conv(conv, annotations or [])
            sub = ann.get("intent_sub") if ann else None
            if sub:
                brands[matched].setdefault("intent", []).append({"job": conv.get("appliedJob"), "sub": sub})
    return {
        "updated_at": (now or datetime.now()).isoformat(timespec="seconds"),
        "global": {
            "conversations": followup.get("totalConversations", 0),
            "hr_replied": followup.get("hrReplied", replied),
        },
        "brands": brands,
    }


def merge_memory(old, new):
    """新记忆覆盖 global 与同品牌条目，保留 old 中新报告未覆盖的品牌历史；
    同品牌 intent 历史合并去重（job+sub 键）——新报告只含当前对话，不能抹掉旧意向。"""
    old = old or {}
    brands = dict(old.get("brands", {}))
    for name, b in new["brands"].items():
        old_intent = (brands.get(name) or {}).get("intent", [])
        if old_intent:
            seen = {json.dumps(i, sort_keys=True) for i in b.get("intent", [])}
            b = dict(
                b, intent=b.get("intent", []) + [i for i in old_intent if json.dumps(i, sort_keys=True) not in seen]
            )
        brands[name] = b
    return {"updated_at": new["updated_at"], "global": new["global"], "brands": brands}


# --- #19 Loop Evaluation：分组回复率评估与正/反例回写 ---


def pattern_tags(text):
    """招呼语结构变体（确定性规则，#19 triage）：提问式 CTA / 链接式 / 随时到岗。
    一条招呼语可同时命中多个标签；全不命中归入 plain 组。
    #19 前置 B 拍板（2026-09-10）：不再产出 year_opener——#13 文案标准强制年限开头，
    该标签零方差恒单桶，无区分力；长度同理不再作为结论维度（见 eval_greetings）。"""
    tags = []
    if "？" in text or "?" in text:
        tags.append("question_cta")
    if "http" in text.lower():
        tags.append("has_link")
    if "到岗" in text:
        tags.append("availability")
    return tags


def length_bucket(text):
    n = len(text)
    if n <= 100:
        return "≤100"
    if n <= 150:
        return "101-150"
    return ">150"


def annotation_for_conv(conv, annotations):
    """一条对话的最佳标注（#19 匹配语义唯一实现，followup_tracker.merge_annotations 同源复用）：
    encryptJobId 精确优先（#32 段 2：company 串两轮采集间会漂移，同 eid 失配实发作），
    company+appliedJob 精确次之，company 兜底；无匹配返回 None。"""
    anns = annotations or []
    eid = conv.get("encryptJobId")
    if eid:
        eid_hits = [a for a in anns if a.get("encryptJobId") == eid]
        if eid_hits:
            return eid_hits[0]
    cands = [a for a in anns if a.get("company") == conv.get("company")]
    exact = [a for a in cands if a.get("appliedJob") and a.get("appliedJob") == conv.get("appliedJob")]
    broad = [a for a in cands if not a.get("appliedJob")]
    return (exact or broad or [None])[0]


# 评估维度注册表（#37 拍板 Q5 轻量插件化）：每项 = (维度名, record → 标签列表)。
# Phase 1 基准维在位；Phase 2 时段维（by_time_slot，#27 项 4）/Phase 3 活跃度维
# （by_recruiter_active_status，#32）数据到位后在 EVAL_DIMENSIONS 注册即可，eval_greetings
# 自动产出分组，不改函数体。skip_decision_quality 已拍板暂缓（无回复真值反馈）。
def _dim_level(r):
    return [r["level"] or "未知"]


def _dim_addressee(r):
    return [r["addressee"] or "未知"]


def _dim_pattern(r):
    return r["tags"] or ["plain"]


EVAL_DIMENSIONS = [
    ("by_level", _dim_level),
    ("by_addressee", _dim_addressee),
    ("by_pattern", _dim_pattern),
]


def join_eval_records(report, greetings, annotations):
    """对话 ↔ 招呼语关联（1:1 消耗，2026-09-12 审查修复）。双侧 encryptJobId 相等时
    精确配对（#30 后 wapi 常态，天然 1:1）；任一侧缺 id 才回退 match_brand 品牌匹配，
    且已配对的对话/招呼语不再参与后续兜底——否则同品牌多岗位多对话会产生笛卡尔积
    重复计数。双侧 id 都在但不相等绝不跨岗配对（原语义保留）。
    replied 单一真源 = conv.status == "hr_replied"（#30：[已读] 归 awaiting，
    summary 是消息正文，不再承担状态语义）。未匹配到招呼语的对话不进 records
    （整体口径见报告 conversations 字段）。"""
    convs = report.get("conversations", [])
    used_convs, used_greetings = set(), set()
    records = []

    def record(conv, g):
        ann = annotation_for_conv(conv, annotations)
        text = g.get("greeting", "")
        return {
            "encryptJobId": g.get("encryptJobId"),
            "brandName": g.get("brandName"),
            "level": g.get("level"),
            "addressee": g.get("addressee"),
            "greeting": text,
            "length": len(text),
            "lengthBucket": length_bucket(text),
            "tags": pattern_tags(text),
            "replied": conv.get("status") == "hr_replied",
            "category": conv.get("replyCategory") or (ann or {}).get("replyCategory"),
            "latency": conv.get("replyLatencyHours", (ann or {}).get("replyLatencyHours")),
        }

    # 第一遍：encryptJobId 精确配对
    conv_by_eid = {c.get("encryptJobId"): i for i, c in enumerate(convs) if c.get("encryptJobId")}
    for gi, g in enumerate(greetings):
        ci = conv_by_eid.get(g.get("encryptJobId"))
        if ci is not None and ci not in used_convs:
            records.append(record(convs[ci], g))
            used_convs.add(ci)
            used_greetings.add(gi)
    # 第二遍：品牌兜底（任一侧缺 eid 才允许；1:1 消耗，先到先得）
    for gi, g in enumerate(greetings):
        if gi in used_greetings:
            continue
        for ci, conv in enumerate(convs):
            if ci in used_convs:
                continue
            if g.get("encryptJobId") and conv.get("encryptJobId"):
                continue  # 双侧 id 都在但不相等：绝不跨岗配对
            if match_brand(conv.get("company", ""), g.get("brandName", "")):
                records.append(record(conv, g))
                used_convs.add(ci)
                used_greetings.add(gi)
                break
    return records


def _group_stats(items):
    n = len(items)
    rep = sum(1 for r in items if r["replied"])
    if n < MIN_GROUP:
        return {"sample": n, "replied": rep, "replyRate": None, "conclusion": f"样本不足（{n}<{MIN_GROUP}），不出结论"}
    return {"sample": n, "replied": rep, "replyRate": rep / n, "conclusion": "ok"}


def eval_greetings(report, greetings, annotations, now=None):
    """followup 报告 + 招呼语台账 → 分组回复率评估（#19）。
    维度由 EVAL_DIMENSIONS 注册表驱动（#37 拍板 Q5 轻量插件化）：Phase 1 基准维
    （岗位级别 / 称呼对象 / 结构变体，标签可重叠）在位；Phase 2 时段维（#27 项 4）、
    Phase 3 活跃度维（#32）注册即扩展。by_length 与 year_opener 移除——#13 统一文案标准
    消灭了长度与开头变体，两维度零区分力；为 A/B 故意写低标准文案的代价不对等，
    行业/岗位关键词维度待台账携带行业字段后再加。records 仍保留 length/lengthBucket
    供后续离线分析。样本门槛：总量 < MIN_TOTAL → sufficient=False，不出任何正/反例候选；
    组内 < MIN_GROUP → 该组 replyRate=None。"""
    # #64 验收 7：legacy_* 历史遗留条目的正文与实发内容可能不一致（正是本票报的病），
    # 不进配对、不参与按文案分组的回复率；被排除条数走 legacyExcluded 如实呈报。
    paired = [e for e in greetings if not is_legacy_greeting(e)]
    legacy_excluded = len(greetings) - len(paired)
    records = join_eval_records(report, paired, annotations)
    total = len(records)
    replied = sum(1 for r in records if r["replied"])
    overall_rate = replied / total if total else None
    latencies = sorted(r["latency"] for r in records if r.get("latency") is not None)
    buckets = {name: {} for name, _fn in EVAL_DIMENSIONS}
    for r in records:
        for name, fn in EVAL_DIMENSIONS:
            for tag in fn(r):
                buckets[name].setdefault(tag, []).append(r)
    groups = {dim: {k: _group_stats(v) for k, v in keys.items()} for dim, keys in buckets.items()}
    sufficient = total >= MIN_TOTAL
    candidates = {"winning": [], "losing": []}
    if sufficient and overall_rate is not None:
        for dim, keys in buckets.items():
            for k, items in keys.items():
                s = groups[dim][k]
                if s["replyRate"] is None or s["replyRate"] == overall_rate:
                    continue
                kind = "winning" if s["replyRate"] > overall_rate else "losing"
                exs = (
                    [r["greeting"] for r in items if r["replied"]]
                    if kind == "winning"
                    else [r["greeting"] for r in items]
                )
                candidates[kind].append(
                    {
                        "key": f"{dim}:{k}",
                        "kind": kind,
                        "sample": s["sample"],
                        "replied": s["replied"],
                        "replyRate": s["replyRate"],
                        "examples": (exs if kind == "winning" else exs)[:3],
                    }
                )
    return {
        "generatedAt": _stamp(now),
        "minGroup": MIN_GROUP,
        "minTotal": MIN_TOTAL,
        "sufficient": sufficient,
        "sampleSize": total,
        "legacyExcluded": legacy_excluded,
        "replied": replied,
        "overallRate": overall_rate,
        "latency": {"sample": len(latencies), "medianHours": latencies[len(latencies) // 2] if latencies else None},
        "groups": groups,
        "candidates": candidates,
    }


def load_winning_examples(path):
    """winning-examples.md → {"winners": [...], "avoid": [...]}（各节下的 - 列表行）。"""
    p = Path(path)
    if not p.exists():
        return {"winners": [], "avoid": []}
    out, section = {"winners": [], "avoid": []}, None
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            section = "winners" if "正例" in line else ("avoid" if "避坑" in line else None)
        elif section and line.startswith("- "):
            out[section].append(line[2:].strip())
    return out


# 正例池总数硬上限（#37 拍板 Q3：每类称呼/风格 5~8 条为经验值，总数 ≤25 兜底防膨胀）
WINNING_MAX_TOTAL = 25
_ENTRY_PREFIX_RE = re.compile(r"^(?:- )?\[[^\]]*\]\s*")
_ENTRY_KEY_RE = re.compile(r"^\[([^\]|]+?)\s*\|")


def match_examples(winning_path, addressee=None, level=None, limit=2):
    """起草端确定性正例匹配（#37 拍板 Q4；声明性偏离：拍板所指 compose_greeting_prompt 已随
    drafter 退役，注入点改为本工具 + runbook 接线——agent 起草时按当前岗位 addressee/level
    调用，确定性标签匹配取 top-N 注入 prompt，杜绝向量 RAG）。匹配优先级：addressee(+3) >
    level(+2) > pattern(+1)；只取正例节，避坑不入选；无匹配返回空（不强行注入）。"""
    entries = load_winning_examples(winning_path)["winners"]
    scored = []
    for entry in entries:
        m = _ENTRY_KEY_RE.match(entry)
        key = m.group(1).strip() if m else ""
        base = 0
        if addressee and f"by_addressee:{addressee}" in key:
            base += 3
        if level and f"by_level:{level}" in key:
            base += 2
        if base > 0:
            tiebreak = 1 if "by_pattern:" in key else 0
            scored.append((base + tiebreak, key, _ENTRY_PREFIX_RE.sub("", entry)))
    scored.sort(key=lambda x: -x[0])
    return [{"key": k, "score": s, "text": t} for s, k, t in scored[:limit]]


def suggest_retirements(entries, overflow):
    """超限建议排序（拍板 Q3）：FIFO 结合回复率降权——最旧优先、同旧看低回复率；
    前缀解析失败的坏条目视为无凭据，归入最旧组末尾（仍在建议内，便于人工一并清理，
    且占掉较新条目的建议位——无凭据条目比"较新但低效"更该优先清理）。"""
    dated = []
    bad = []
    for ln in entries:
        m = re.search(r"回复率 (\d+)% \| 样本 \d+ \| (\d{4}-\d{2}-\d{2})", ln)
        if m:
            dated.append((m.group(2), int(m.group(1)), ln))
        else:
            bad.append(ln)
    dated.sort(key=lambda x: (x[0], x[1]))
    if bad and dated:
        oldest = dated[0][0]
        merged = list(dated) + [(oldest, float("inf"), ln) for ln in bad]
        merged.sort(key=lambda x: (x[0], x[1]))
        ordered = [ln for _, _, ln in merged]
    else:
        ordered = [ln for _, _, ln in dated] + bad
    return [_ENTRY_PREFIX_RE.sub("", ln) for ln in ordered[:overflow]]


def _resolve_retire_lines(existing, retire):
    """--retire 片段 → {"winners": [...], "avoid": [...]}：片段在正例+避坑**两节合并**中
    须恰好命中 1 条（人工看文本点名，不须知道条目在哪节），零命中/多命中报 ValueError
    （绝不静默删除）。"""
    out = {"winners": [], "avoid": []}
    for frag in retire or []:
        hits = [("winners", ln) for ln in existing["winners"] if frag in ln]
        hits += [("avoid", ln) for ln in existing["avoid"] if frag in ln]
        if len(hits) != 1:
            raise ValueError(f"淘汰片段 {frag!r} 命中 {len(hits)} 条（须恰好 1 条）")
        section, ln = hits[0]
        out[section].append(ln)
    return out


def promote_winners(evaluation, groups, out_path=None, yes=False, now=None, retire=None):
    """评估候选 → few-shot 正例池/避坑清单。人工 gate：--group 显式点名候选组 + --yes 确认才写文件。
    #37 拍板 Q3：正例池总数硬上限 WINNING_MAX_TOTAL——超限且未点名淘汰（--retire）时拒绝写入
    并列出建议淘汰项（FIFO 结合回复率降权），绝不静默删除；--retire 片段须恰好命中 1 条。"""
    out_path = Path(out_path or WINNING)
    cands = {
        c["key"]: c
        for c in evaluation.get("candidates", {}).get("winning", [])
        + evaluation.get("candidates", {}).get("losing", [])
    }
    selected = [cands[k] for k in groups if k in cands]
    missing = [k for k in groups if k not in cands]
    if missing or not selected:
        print(f"[blocked] 候选组不存在: {missing or groups}——先运行 eval，候选 key 见 {EVALUATION}")
        return False
    if not yes:
        print("[blocked] 人工确认 gate——核对以下回写内容后加 --yes 执行：")
        for c in selected:
            print(f"  [{c['key']} | 回复率 {c['replyRate']:.0%} | 样本 {c['sample']}] {c['examples']}")
        return False
    stamp = _stamp(now)[:10]
    existing = load_winning_examples(out_path)
    try:
        retired = _resolve_retire_lines(existing, retire)
    except ValueError as e:
        print(f"[blocked] {e}")
        return False
    new = {"winners": [], "avoid": []}
    retired_set = {s: set(rows) for s, rows in retired.items()}
    for c in selected:
        section = "winners" if c.get("kind", "winning") == "winning" else "avoid"
        for ex in c["examples"]:
            entry = f"[{c['key']} | 回复率 {c['replyRate']:.0%} | 样本 {c['sample']} | {stamp}] {ex}"
            if entry not in existing[section] and entry not in new[section]:
                new[section].append(entry)
    kept = {}
    for section, label in (("winners", "正例"), ("avoid", "避坑")):
        kept[section] = [ln for ln in existing[section] if ln not in retired_set[section]]
        total = len(kept[section]) + len(new[section])
        overflow = total - WINNING_MAX_TOTAL
        if overflow > 0:
            print(
                f"[blocked] {label}池超上限（{total}>{WINNING_MAX_TOTAL}）——绝不静默删除，"
                f"加 --retire <片段> 点名淘汰后重跑："
            )
            for s in suggest_retirements(kept[section] + new[section], overflow):
                print(f"  Suggested retirement: {s}")
            return False
    text = (
        "# 招呼语效果回写（#19 Loop Evaluation：eval 产出候选，人工 --yes 确认后由 promote-winners 写入）\n\n"
        "## 正例（few-shot）\n\n"
        + ("\n".join(f"- {line}" for line in kept["winners"] + new["winners"]) or "（暂无）")
        + "\n\n## 避坑（低回复模式）\n\n"
        + ("\n".join(f"- {line}" for line in kept["avoid"] + new["avoid"]) or "（暂无）")
        + "\n"
    )
    out_path.write_text(text, encoding="utf-8")
    retired_n = len(retired["winners"]) + len(retired["avoid"])
    print(
        f"[done] 正例 {len(kept['winners']) + len(new['winners'])} 条 / 避坑 {len(kept['avoid']) + len(new['avoid'])} 条"
        + (f"（淘汰 {retired_n} 条）" if retired_n else "")
        + f" → {out_path}"
        "（agent 起草时按 runbook「起草规范」引用）"
    )
    return True


def _load_json(path, default):
    p = Path(path)
    if not p.exists():
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# --- #37 EDD 持续化：候选待审队列 + eval 产物持久化 ---

PENDING_MAX_AGE_DAYS = 30  # 拍板 Q8：超 30 天未审阅的候选自动废弃（下次 eval 重新产出会再入队）


def pending_upsert(existing, candidates, now=None):
    """候选入待审队列（拍板 Q8）：按 key 幂等——同 key 原位覆盖并刷新 last_seen，
    **created 保留首次入队时间**（review C1：30 天「未审阅」过期以首见起算，
    持续产出不能无限续期）；新 key 追加。"""
    merged = {c["key"]: c for c in (existing or {}).get("candidates", [])}
    stamp = _stamp(now)
    for c in candidates or []:
        old = merged.get(c["key"]) or {}
        merged[c["key"]] = dict(c, created=old.get("created") or stamp, last_seen=stamp)
    return {"updated_at": stamp, "candidates": list(merged.values())}


def pending_prune(data, now=None, max_age_days=PENDING_MAX_AGE_DAYS):
    """过滤超龄候选（created 距 now > max_age_days 天）；created 坏形态按过期处理
    （无凭据的时间不保留——与 state_evidence 同一 fail-closed 方向）。"""
    now_dt = now if isinstance(now, datetime) else (_parse_stamp(_stamp(now)) or datetime.now())
    keep = []
    for c in (data or {}).get("candidates", []):
        created = _parse_stamp(c.get("created"))
        if created is None or (now_dt - created).total_seconds() > max_age_days * 86400:
            continue
        keep.append(c)
    return {"updated_at": (data or {}).get("updated_at"), "candidates": keep}


def persist_eval_output(out, evaluation_path, trend_path, pending_path, profile, now=None):
    """eval 产物持久化组合（#37）：评估报告写盘 + 趋势快照追加（无论 sufficient 与否，
    拍板 Q7；0 样本假零由 _edd.append_trend 咽喉拦下）+ 非空候选入待审队列（幂等去重 +
    超龄清理）。
    返回 (待审条数, 本次快照, 趋势写入状态)——状态就是 `_edd.append_trend` 的返回值
    （`TREND_APPENDED` / `TREND_DUPLICATE` / `TREND_FALSE_ZERO`）。
    #59 诚实性：`eval --trend` 过去无条件打「趋势快照已追加」，被拒时说的是假话——追加结果必须传出来。
    #59 修复轮 R4-5：第 3 项从 bool 改成状态字符串。旧形态为了区分「被拒的两类原因」在这里
    `false_zero_snapshot(load_trend(trend_path), snapshot)` 自己又判一遍，判序因此在咽喉与调用方
    各存一份：① 「幂等跳过」与「假零」同时成立时，这里报的是假零而咽喉实际按幂等跳过处理；
    ② 两次读 trend 之间没有锁，理论上能读到不同内容。现在调用方只读状态，判序只有一份。"""
    _write_json(evaluation_path, out)
    snapshot = trend_snapshot(out, profile=profile, now=now)
    trend_status = append_trend(trend_path, snapshot)
    candidates = out.get("candidates", {}).get("winning", []) + out.get("candidates", {}).get("losing", [])
    if candidates:
        queue = pending_upsert(_load_json(pending_path, {"candidates": []}), candidates, now=now)
        queue = pending_prune(queue, now=now)
        _write_json(pending_path, queue)
    else:
        queue = _load_json(pending_path, None)
        if queue is not None:
            _write_json(pending_path, pending_prune(queue, now=now))
    return len(queue["candidates"]) if queue else 0, snapshot, trend_status


# `_edd.append_trend` 的返回状态 → `eval --trend` 终端措辞（动词 + 括注）。三个取值都在这儿列全：
# 咽喉拒写的两类原因不同（幂等跳过 ≠ 疑似残缺采集），把它们混成一句「未追加」就又回到
# #59 修之前的谎报。未知状态由调用点兜底成「未追加（未知状态 …）」，不谎报「已追加」。
_TREND_STATUS_WORDING = {
    TREND_APPENDED: ("已追加", "（#37）"),
    TREND_FALSE_ZERO: ("未追加", "（0 样本而历史已有非 0 点，疑似残缺采集——不往趋势里写假零点，#59）"),
    TREND_DUPLICATE: ("未追加", "（与最新一条内容相同，幂等跳过）"),
}


def record_session_snapshot(paths, profile, now=None):
    """投递会话收尾的 EDD 轻量快照（#37 拍板 Q1 会话级）：基于 followup 报告 +
    招呼语台账 + 标注跑 eval → 追加趋势快照 → 批次级审阅提示 + 密度判定（#45）。
    纯本地计算（<0.5s），不跑浏览器——#49 起收尾已在报告写盘前现场采集过一次最新
    followup（本函数通常吃到本场数据），从未复盘过的全新账号则无数据跳过快照。
    #59 防线 2：配对样本为 0 而趋势历史已有非 0 点 ⇒ 判疑似残缺采集、跳过追加
    （宁可这一场没快照，不往趋势里写假零点）。判定与判序都在 `_edd.append_trend` 咽喉里，
    本函数只读它的返回状态出可读 reason 给 report.json 的 edd 字段（R4-5：不再自己重读 trend
    判第二遍）。
    **返回体形状（R4-2）**：跑过 eval 之后的两个出口（追加成功 / 被咽喉拒写）都带
    `snapshot` + `review_due` + `density_due` 三键——`live_apply.edd_wrapup` 对后两键只做
    `.get(...) or {}`，缺键时那一整块提示判定在消费侧直接消失，而消费方分辨不了「没有键」与
    「判定为否」。两个早退出口（无 followup / 空报告）在 eval 之前，没有 eval 输出可判，
    因此不带这两键（既有语义，`edd_wrapup` 按缺键走）。
    异常不在此吞（调用方 live_apply 收尾 try/except 兜底，保证故障可感知）。"""
    followup_data = _load_json(paths["followup"], {}) if Path(paths["followup"]).exists() else None
    if followup_data is None:
        return {"trend_appended": False, "reason": "no followup data"}
    if not followup_data.get("conversations"):
        # 登录过期/掉线采集到的空报告（192616 实测）：0 对话 eval 出 0 样本快照会把趋势
        # 稀释成假象，直接跳过快照（live_apply 侧 _collect_fresh_followup 已拦截覆盖，
        # 这里是独立入口/兜底场景的防线）。
        return {"trend_appended": False, "reason": "empty followup report"}
    annotations = _load_json(paths["annotations"], {}).get("annotations", [])
    out = eval_greetings(followup_data, _load_json(paths["greetings"], []), annotations, now=now)
    snapshot = trend_snapshot(out, profile=profile, now=now)
    # 追加**前**读一次历史，只喂两件事：① 拒写分支的 `review_due`（此刻快照没入账，按既有语义
    # 吃历史算），② 假零 reason 里的「最新样本数」。判定不在这里做（判序只有一份，在咽喉里）。
    history = load_trend(paths["trend"])
    trend_status = append_trend(paths["trend"], snapshot)
    # `density_due` 吃的是**本次 eval 输出 `out`**、与趋势有没有写入无关——被拒的那一场照样有
    # 完整 eval 结果，所以 `review_due` / `density_due` 两键照常在。旧版假零分支只回 reason +
    # snapshot，注释却写着「density_due 不受本防线影响」，而那个返回体里键根本不存在：
    # `live_apply.edd_wrapup` 对两键只做 `.get(...) or {}`，缺键时整块提示判定从流程里消失
    # （该形态下多半 due=False，但「消费方拿不到键」与「判定为否」是两件事——消费侧分辨不了，
    # 注释更不能声称不受影响）。
    density = density_due(out)
    if trend_status != TREND_APPENDED:
        latest_size = (history[-1] or {}).get("sampleSize") if history else None
        reason = (
            f"0 样本而趋势历史已有非 0 点（最新 {latest_size} 样本）——疑似残缺采集，不追加趋势以免写进假零点"
            if trend_status == TREND_FALSE_ZERO
            else "与最新一条快照内容相同（除 ts）——幂等跳过，不重复追加"
        )
        return {
            "trend_appended": False,
            "reason": reason,
            "snapshot": snapshot,
            "review_due": review_due(snapshot, history, now=now),
            "density_due": density,
        }
    verdict = review_due(snapshot, load_trend(paths["trend"]), now=now)
    return {"trend_appended": True, "snapshot": snapshot, "review_due": verdict, "density_due": density}


def promote_gate(gaps, yes=False):
    """promote 前置 gate（#9 架构改造诉求③自动化）：KB 更新后忘跑 doctor，
    就在投递出口自动兜住——白名单有缺口默认拦截，--yes 确认放行。"""
    if not gaps:
        return True
    if yes:
        print("[warn] 白名单缺口已确认放行（--yes）")
        return True
    print(
        "[blocked] 知识库硬事实与白名单不同步——先跑 `gen_greetings.py doctor` 看缺口"
        "（卡片新事实用 doctor --sync 入表），或加 --yes 强制："
    )
    for g in gaps[:10]:
        print(f"  - {g}")
    return False


def resolve_paths(explicit=None, env=None):
    """入口路径束（#28）：显式 --profile > AUTOJOB_PROFILE env > 注册表 default > 唯一
    profile（多 profile 无 default fail-loud）。在位 profile 沿用现有文件名（零迁移），
    其余加 <name>. 前缀；行业层目录 kb/industries、profile 注册目录 kb/profiles 共享不隔离。"""
    name, prefix = resolve_active_profile(explicit=explicit, env=env)
    return {
        "name": name,
        "prefix": prefix,
        "kb": private_path("profile_background.md", prefix),
        "fact_cards": private_path("kb/fact_cards.md", prefix),
        "drafts": private_path("data/greeting_drafts.json", prefix),
        "greetings": private_path("data/greetings.json", prefix),
        "memory": private_path("data/greeting_memory.json", prefix),
        "followup": private_path("data/followup_report.json", prefix),
        "history": private_path("data/applied_history.json", prefix),
        "annotations": private_path("data/reply_annotations.json", prefix),
        "evaluation": private_path("data/greeting_evaluation.json", prefix),
        "winning": private_path("kb/winning-examples.md", prefix),
        "trend": private_path("eval_trend.jsonl", prefix),  # #37：EDD 趋势时序（拍板 Q2 定名 private/eval_trend.jsonl）
        "pending": private_path("kb/pending_promotions.json", prefix),  # #37：候选待审队列（拍板 Q8）
    }


def _cmd_doctor(args, paths, ind_profile):
    cards_path = paths["fact_cards"]
    print(f"[doctor] 目标卡片文件：{cards_path}")
    _, whitelist = load_kb_context(kb_path=paths["kb"], cards_path=cards_path, profile=ind_profile)
    if whitelist is None:
        if args.sync:
            cards_text = cards_path.read_text(encoding="utf-8")
            if _JSON_BLOCK_RE.search(cards_text):
                print("[warn] 白名单 JSON 块损坏（解析失败）——--sync 将原位重建，坏块内容不保留")
            write_text_atomic(cards_path, sync_card_facts(cards_text))
            print(f"[done] 白名单区已从卡片编译生成 → {cards_path}（#25），重跑 doctor 核对")
        else:
            print(f"[提示] {cards_path} 无白名单区——跑 `doctor --sync` 从卡片编译建表")
        return
    cards_text = cards_path.read_text(encoding="utf-8")
    personal = paths["kb"].read_text(encoding="utf-8")
    industry = load_industry_text(ind_profile)
    gaps = doctor_report(personal + ("\n\n" + industry if industry else ""), whitelist)
    if not gaps:
        print(f"[ok] 知识库（profile={ind_profile}）硬事实与白名单同步，无缺口")
    else:
        print(f"[warn] 硬事实清单提到但白名单未收录（{len(gaps)} 条，写入新实体后请同步 fact_cards 白名单）：")
        for g in gaps[:25]:
            print(f"  - {g}")
    diff = card_gaps(cards_text, whitelist)
    if diff["years"] or diff["entities"]:
        print(
            f"[derive] 卡片已审事实未入表（#25）：年限 {diff['years']}"
            f"、实体 {diff['entities'][:15]}{'…' if len(diff['entities']) > 15 else ''}"
            "——核对后 `gen_greetings.py doctor --sync` 自动入表"
        )
    if args.sync:
        write_text_atomic(cards_path, sync_card_facts(cards_text))
        print(f"[done] 白名单已从卡片编译入表：+{len(diff['years'])} 年限 +{len(diff['entities'])} 实体（原子写）")


def _cmd_promote(args, paths, ind_profile):
    _, wl = load_kb_context(kb_path=paths["kb"], cards_path=paths["fact_cards"], profile=ind_profile)
    if wl is not None:
        personal = paths["kb"].read_text(encoding="utf-8")
        industry = load_industry_text(ind_profile)
        gaps = doctor_report(personal + ("\n\n" + industry if industry else ""), wl)
        if not promote_gate(gaps, yes=args.yes):
            return
    else:
        print("[提示] 白名单缺失——规则扫描 gate 不在位，投递内容未经确定性核查")
    drafts_path = Path(args.drafts) if args.drafts else paths["drafts"]
    greetings, n, kept_sent = promote_drafts(_load_json(drafts_path, []), _load_json(paths["greetings"], []))
    _write_json(paths["greetings"], greetings)
    print(f"[done] 晋级 {n} 条 → {paths['greetings']}（投递队列，live_apply 会话消费）")
    if kept_sent:
        print(f"  [promote] 另有 {kept_sent} 条已投出的招呼语未被草稿覆盖（source=live_apply 优先，#64）")


def _cmd_eval(args, paths):
    if not paths["followup"].exists():
        print(f"[跳过] {paths['followup']} 不存在——先运行 followup_tracker.py")
        return
    annotations = _load_json(paths["annotations"], {}).get("annotations", [])
    out = eval_greetings(_load_json(paths["followup"], {}), _load_json(paths["greetings"], []), annotations)
    pending_n, snapshot, trend_status = persist_eval_output(
        out,
        evaluation_path=paths["evaluation"],
        trend_path=paths["trend"],
        pending_path=paths["pending"],
        profile=paths["name"] or "default",
    )
    # #59 诚实性：措辞跟着 append_trend 的**返回状态**走，不再一律打「已追加」，也不再在这里
    # 自己重判一遍假零（R4-5：判序只有咽喉那一份——旧写法在「幂等跳过 + 假零」同时成立时报的是
    # 假零，而咽喉实际按幂等跳过处理）。未知状态不谎报「已追加」。
    verb, why = _TREND_STATUS_WORDING.get(trend_status, ("未追加", f"（未知状态 {trend_status!r}，未落盘）"))
    print(f"[done] 评估报告 → {paths['evaluation']}；趋势快照{verb} → {paths['trend']}{why}")
    rate = f"{out['overallRate']:.0%}" if out["overallRate"] is not None else "N/A"
    print(f"  样本 {out['sampleSize']}（门槛 {MIN_TOTAL}）| 回复 {out['replied']} | 总体回复率 {rate}")
    if out["legacyExcluded"]:
        print(
            f"  [eval] 排除历史遗留招呼语 {out['legacyExcluded']} 条（source 属 legacy 词表：正文与实发可能不一致，#64）"
        )
    if not out["sufficient"]:
        print("  [样本不足] 总量未达门槛，不出结论——继续投递积累样本（趋势照常记录）")
    if args.trend:
        print(render_trend_table(snapshot, load_trend(paths["trend"])))
    for c in out["candidates"]["winning"]:
        print(f"  [正例候选] {c['key']} | {c['replyRate']:.0%}（样本 {c['sample']}）")
    for c in out["candidates"]["losing"]:
        print(f"  [避坑候选] {c['key']} | {c['replyRate']:.0%}（样本 {c['sample']}）")
    if pending_n:
        print(
            f"💡 发现 {pending_n} 个候选优化范式待人工审阅，请执行: "
            f"python scripts/gen_greetings.py promote-winners --group <key>（候选清单 {paths['pending']}）"
        )
    # #45 密度阈值提示制（拍板 2026-09-24）：任一分组组内回复 ≥5 时主动叫人审范式——
    # 系统通知 + 提示块附候选清单；低密度静默不扰（避免回复寥寥就弹窗的噪声）。决策权在用户，
    # promote-winners 的人工 gate 原样保留，本命令不做任何自动晋级。
    density = density_due(out)
    if density["due"]:
        print(render_density_prompt(density, load_pending_candidates(paths["pending"])))
        notify("招呼语范式审阅提醒", f"分组回复密度达标（{'；'.join(density['groups'])}）——跑 eval 看候选清单")


def _cmd_promote_winners(args, paths):
    evaluation = _load_json(paths["evaluation"], None)
    if evaluation is None:
        print(f"[blocked] {paths['evaluation']} 不存在——先运行 gen_greetings.py eval")
        return
    promote_winners(evaluation, args.group, yes=args.yes, out_path=paths["winning"], retire=args.retire)


def _cmd_learn(args, paths):
    if not paths["followup"].exists():
        print(f"[跳过] {paths['followup']} 不存在，growing memory 保持不变")
        return
    # 标注库是 {"updated_at":…, "annotations":[…]} dict（upsert_annotations 落盘形态），
    # 须取 annotations 列表——整包传下去会迭代出字符串 key（2026-09-12 真实数据 learn 崩溃）
    annotations = _load_json(paths["annotations"], {})
    if isinstance(annotations, dict):
        annotations = annotations.get("annotations", [])
    new = build_memory(
        _load_json(paths["followup"], {}), _load_json(paths["history"], {"applied": []}), annotations=annotations
    )
    merged = merge_memory(_load_json(paths["memory"], {}), new)
    _write_json(paths["memory"], merged)
    print(f"[done] growing memory 已更新 → {paths['memory']}（品牌 {len(merged['brands'])} 个）")


def _cmd_match_examples(args, paths):
    picks = match_examples(paths["winning"], addressee=args.addressee, level=args.level, limit=args.limit)
    if not picks:
        print(f"[跳过] 正例池无匹配（addressee={args.addressee}, level={args.level}）——不强行注入")
        return
    print(f"[done] 匹配 {len(picks)} 条正例范式（供起草 [参考成功范式] 块注入，#37）：")
    for p in picks:
        print(f"  [{p['key']} | 匹配分 {p['score']}] {p['text']}")


def main():
    parser = argparse.ArgumentParser(
        description="招呼语工具：草稿晋级 + growing memory + 回复率评估（起草由 agent 按 runbook 完成）"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def _add_profile(sp):
        sp.add_argument(
            "--profile", default=None, help="显式指定 profile（默认按 AUTOJOB_PROFILE env > 注册表解析，#28）"
        )

    p_pro = sub.add_parser("promote", help="approved 草稿并入 greetings.json")
    p_pro.add_argument("--drafts", default=None, help="默认解析当前 profile 的 drafts 文件")
    p_pro.add_argument("--yes", action="store_true", help="白名单有缺口时强制放行")

    p_eval = sub.add_parser("eval", help="分组回复率评估 → greeting_evaluation.json（#19）")
    _add_profile(p_eval)
    p_eval.add_argument(
        "--trend", action="store_true", help="追加输出趋势对比表格（baseline/previous/current 及 delta，#37）"
    )
    p_pw = sub.add_parser("promote-winners", help="评估候选 → few-shot 正例池/避坑清单（人工 gate）")
    _add_profile(p_pw)
    p_pw.add_argument(
        "--group", action="append", required=True, help="候选组 key（如 by_pattern:question_cta），可重复"
    )
    p_pw.add_argument("--yes", action="store_true", help="确认写入（人工 gate）")
    p_pw.add_argument(
        "--retire",
        action="append",
        help="淘汰条目的唯一片段（命中须恰好 1 条，可重复；正例池超上限时须先淘汰，#37）",
    )

    p_learn = sub.add_parser("learn", help="followup 反馈回写 growing memory")
    _add_profile(p_learn)
    p_match = sub.add_parser(
        "match-examples", help="按 addressee/level 确定性匹配正例池 top-N 供起草注入（#37 拍板 Q4）"
    )
    _add_profile(p_match)
    p_match.add_argument("--addressee", default=None, help="目标受众（hr/leader/ceo）")
    p_match.add_argument("--level", default=None, help="岗位级别（高/中/初）")
    p_match.add_argument("--limit", type=int, default=2, help="返回条数（默认 2）")
    p_doctor = sub.add_parser("doctor", help="检查知识库新增内容 vs 白名单的同步情况")
    _add_profile(p_doctor)
    p_doctor.add_argument(
        "--sync", action="store_true", help="把卡片已审事实（diff 审核后）编译进白名单 JSON 块（#25）"
    )

    args = parser.parse_args()
    paths = resolve_paths(explicit=getattr(args, "profile", None))
    print(profile_line(paths["name"], paths["prefix"]))
    ind_profile = paths["name"] or resolve_kb_profile()  # 行业层 profile 名（注册名 = kb/profiles 名，#28）
    if args.cmd == "doctor":
        _cmd_doctor(args, paths, ind_profile)
    elif args.cmd == "promote":
        _cmd_promote(args, paths, ind_profile)
    elif args.cmd == "eval":
        _cmd_eval(args, paths)
    elif args.cmd == "promote-winners":
        _cmd_promote_winners(args, paths)
    elif args.cmd == "learn":
        _cmd_learn(args, paths)
    elif args.cmd == "match-examples":
        _cmd_match_examples(args, paths)


if __name__ == "__main__":
    main()
