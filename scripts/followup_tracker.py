#!/usr/bin/env python3
"""
效果追踪（平台化）：打开站内消息页拿会话列表，判断 HR 回复情况。
- 状态判定单一真源 = conv.status（#30 修复后）：最后一条消息 fromId 是我方 = awaiting
  （[已读]/[送达] 都不算回复），对方发言 = hr_replied——不再按摘要前缀猜
- 数据源：wapi getGeekFriendList 拦截为主（结构化全量，带 encryptJobId/毫秒时间戳），
  结构化 DOM 降级；探测结论 内部调研笔记目录（未随本仓分发）boss-chat-card-shape.md
- 解析与统计逻辑在 scripts/platforms/chat_first.py；本脚本只做编排、标注持久化与展示
- 水位线读数（#75「先看哪几条」）：报告里的 `newSinceWatermark` / `watermark` 与终端那行
  `[水位线]` 都来自 `scripts/_inbound_watermark.py`（经 `wm.attach_readings` 落键，与
  `reply_inbound` / `live_apply` 同一个函数）。**本脚本只读消费者**——读基准、报差值，
  绝不推进它（推进只发生在 `reply_inbound.py --mark-read`，语义是用户当面确认看过这次读到的整批）。
  Boss 的 `unreadMsgCount` 已被实测否证（场次与证据见 `内部调研笔记目录（未随本仓分发）boss-chat-card-shape.md` §7.3），
  所以这里也不拿它分类。

标注模式（#19 Loop Evaluation）：
  python3 scripts/followup_tracker.py annotate --annotations FILE
  followup 环节由 session agent 阅读 HR 回复原文后按固定分类法标注
  （interested/tech_question/rejected/no_reply + replyLatencyHours），
  本脚本只做校验与持久化——标注存 reply_annotations.json（跨 followup 轮次存活，
  followup_report.json 每次全量重写会丢字段），并回填当前报告供 eval 直接消费。
"""

import argparse
import json
import time
from camoufox.sync_api import Camoufox

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from platforms import get_platform
from platforms.chat_first import (  # noqa: E402  #判据与中文标签都在 平台侧（单一真源），本脚本只做编排与展示
    STATE_LABELS,
    inbound_waiting,
    read_state,
)
import _logging  # noqa: E402  #38 机读日志旁路（双轨制：print 人读层与测试契约不动）
import _inbound_watermark as wm  # noqa: E402  #75 水位线判据在单一真源里，本脚本只读、只报、不推进
from _profile_paths import (  # noqa: E402  #28：私有路径唯一出口
    browser_profile_dir,
    private_path,
    profile_line,
    resolve_active_profile,
)

ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = str(ROOT / "private/.camoufox_profile")  # legacy 默认；run() 按解析重算
HISTORY = str(ROOT / "private/data/applied_history.json")
OUT = str(ROOT / "private/data/followup_report.json")
ANNOTATIONS = str(ROOT / "private/data/reply_annotations.json")

# 回复分类法（#19 triage 拍板）：followup 环节 agent 标注，eval 层只消费这四个值
REPLY_CATEGORIES = {"interested", "tech_question", "rejected", "no_reply"}

# intent_sub 二级标签（#21 修订方案 §2）：挂在 interested 大类下的强意向细分，
# 主四分类契约不变；rejected 等其他主分类的细分枚举待定义后再扩，当前不允许携带。
INTERESTED_INTENT_SUBS = {"interview", "resume", "salary"}


def validate_annotation(ann):
    """标注合法性：company/appliedJob/encryptJobId 至少一项非空（匹配键，全空 = 永远无法
    配对的死数据，#27 项 2；encryptJobId 为 #32 段 2 增设的优先键，防 company 解析漂移）；
    replyCategory 必须在固定分类法内，replyLatencyHours 须为非负数值
    （坏数据在入口拦下，不进统计——eval 层排序遇混型会晚爆成 TypeError）；
    intent_sub 可选，仅允许挂在 interested 下且取值受枚举约束（#21）。"""
    if not (
        str(ann.get("company") or "").strip()
        or str(ann.get("appliedJob") or "").strip()
        or str(ann.get("encryptJobId") or "").strip()
    ):
        raise ValueError("company/appliedJob/encryptJobId 至少一项非空（标注的匹配键，全空则永远无法配对）")
    if ann.get("replyCategory") not in REPLY_CATEGORIES:
        raise ValueError(f"replyCategory 必须是 {sorted(REPLY_CATEGORIES)} 之一，得到: {ann.get('replyCategory')!r}")
    lat = ann.get("replyLatencyHours")
    if lat is not None and (isinstance(lat, bool) or not isinstance(lat, (int, float)) or lat < 0):
        raise ValueError(f"replyLatencyHours 必须是非负数值（小时），得到: {lat!r}")
    sub = ann.get("intent_sub")
    if sub is not None:
        if ann.get("replyCategory") != "interested":
            raise ValueError(
                f"intent_sub 仅允许挂在 replyCategory=interested 下（#21），得到主分类: {ann.get('replyCategory')!r}"
            )
        if sub not in INTERESTED_INTENT_SUBS:
            raise ValueError(f"intent_sub 必须是 {sorted(INTERESTED_INTENT_SUBS)} 之一，得到: {sub!r}")
    return True


def merge_annotations(report, annotations):
    """把标注贴到对话上；未匹配的对话不动。匹配语义唯一实现在
    gen_greetings.annotation_for_conv（company+appliedJob 精确优先，company 兜底），
    eval 侧 join 复用同一函数，避免双实现漂移。"""
    from gen_greetings import annotation_for_conv

    for ann in annotations:
        validate_annotation(ann)
    for conv in report.get("conversations", []):
        ann = annotation_for_conv(conv, annotations)
        if ann:
            conv["replyCategory"] = ann["replyCategory"]
            if ann.get("replyLatencyHours") is not None:
                conv["replyLatencyHours"] = ann["replyLatencyHours"]
            if ann.get("intent_sub"):
                conv["intent_sub"] = ann["intent_sub"]
    return report


def link_applied_ids(report, applied):
    """对话 ↔ 投递记录回填 encryptJobId（品牌互为子串 + 岗位名一致，复用 gen_greetings.match_brand）。"""
    from gen_greetings import match_brand

    for conv in report.get("conversations", []):
        for a in applied:
            if a.get("jobName") == conv.get("appliedJob") and match_brand(
                conv.get("company", ""), a.get("brandName", "")
            ):
                conv["encryptJobId"] = a["encryptJobId"]
                break
    return report


def load_annotations(path):
    p = Path(path)
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, list):  # agent 产出的标注文件两种形态都收：裸 list 或 {"annotations": [...]}
        return data
    if isinstance(data, dict) and "annotations" in data:
        return data.get("annotations") or []
    raise ValueError('顶层须是标注数组或 {"annotations": [...]}')


def _blocked(msg):
    """坏标注统一拒绝出口（#27 项 2）：[blocked] + 指明错误 + 提示重写 + 退出码 1，
    不裸抛 traceback（runbook 步骤 5 的执行者是会话 agent，退出码为准）。"""
    print(f"[blocked] {msg}")
    print("        修正标注文件后重写重跑；本次未写任何文件")
    sys.exit(1)


def upsert_annotations(path, annotations, now=None):
    """标注按 encryptJobId（#32 段 2）/ company+appliedJob 幂等覆盖——重复标注原位更新不堆积。"""
    p = Path(path)
    old = []
    if p.exists():
        old = json.loads(p.read_text(encoding="utf-8")).get("annotations", [])
    for ann in annotations:
        validate_annotation(ann)

    def key(a):
        eid = str(a.get("encryptJobId") or "").strip()
        if eid:
            return ("@eid", eid)  # 带 eid 的标注独立成组，不与同名 company 条目互吞
        return (a.get("company", ""), a.get("appliedJob", ""))

    merged = {key(a): a for a in old}
    for a in annotations:
        merged[key(a)] = a
    data = {"updated_at": (now or time.strftime("%Y-%m-%dT%H:%M:%S")), "annotations": list(merged.values())}
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def run():
    name, prefix = resolve_active_profile()
    print(profile_line(name, prefix))
    history_path = private_path("data/applied_history.json", prefix)
    out_path = private_path("data/followup_report.json", prefix)
    # 水位线基准按 profile 隔离（#28 扁平命名）。本脚本是**只读消费者**：读它、报它，一个字都不写
    # ——推进只发生在 `reply_inbound.py --mark-read`（用户当面确认「这批我看过了」那一刻）。
    watermark_path = private_path(wm.WATERMARK_FILE, prefix)
    platform = get_platform()
    with open(history_path, encoding="utf-8") as f:
        applied = json.load(f).get("applied", [])

    with Camoufox(
        humanize=True,
        geoip=True,
        os="macos",
        headless=False,
        persistent_context=True,
        user_data_dir=browser_profile_dir(name),
    ) as context:
        page = context.new_page()
        # 不注 beat_fn（#59 修复轮 R4-1 核对过）：本脚本是 standalone 复盘，没有会话心跳文件、
        # 也没有看门狗与 faulthandler 武装，拍点在此无处可落——采集侧的多花墙钟（≈21s 等待窗口）
        # 是它自己的既有代价，与 live_apply 收尾那条阈值对表线无关。
        report = platform.followup(page, applied)

    if report is None:
        return

    if not report["conversations"]:
        # 登录过期/掉线采集到 0 对话（192616 实测）：空报告会把历史数据静默抹成 0，
        # 与 live_apply._collect_fresh_followup 同一条防线——沿用上次报告，不覆盖。
        print(f"[warn] 采集到 0 条对话（疑似登录态过期/掉线）——沿用上次报告，不覆盖 {out_path}")
        return

    # 水位线读数（#75）：拿本场只读快照与上次基准比，两个恒在键就地落进 report。
    # 判据、形态、措辞与「怎么落键」都在 `_inbound_watermark.attach_readings`（条目 B：三个消费者
    # 共用一个函数，免得各自接一遍接漏），这里只接线 + 打印。无基准时 N 是 None（**不是**全部条数）。
    base, due = wm.attach_readings(report, profile=name, path=watermark_path)

    replied = [r for r in report["conversations"] if r["status"] == "hr_replied"]
    awaiting = [r for r in report["conversations"] if r["status"] == "awaiting"]
    # 入站且在等回话：末条是对方发言、又归不到我们的投递记录（对方主动来撩的那批）
    unlinked_replied = inbound_waiting(report["conversations"])

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(
        f"[统计] 对话 {report['totalConversations']} | HR已回复 {report['hrReplied']} | "
        f"待回复 {report['awaiting']} | 回复率 {report['replyRate']}"
    )
    _logging.log_event(  # #38：followup 统计进机读旁路（人读 print 原样）
        "followup_report",
        total=report["totalConversations"],
        hr_replied=report["hrReplied"],
        awaiting=report["awaiting"],
        reply_rate=report["replyRate"],
        linked=report["linkedToApplied"],
        linked_replied=report["linkedReplied"],
        unlinked=report["unlinkedConversations"],
        unlinked_replied=report["unlinkedReplied"],
        unread_conversations=report["unreadConversations"],
        unlinked_replied_unread=report["unlinkedRepliedUnread"],
        unlinked_replied_read=report["unlinkedRepliedRead"],
        unlinked_replied_read_unknown=report["unlinkedRepliedReadUnknown"],
        new_since_watermark=report["newSinceWatermark"],
        watermark_state=report["watermark"]["state"],
    )
    print(f"[关联投递记录] {report['linkedToApplied']} 个，其中已回复 {report['linkedReplied']}")
    print(
        f"[入站线索] 归不到投递记录 {report['unlinkedConversations']} 个，"
        f"其中 {report['unlinkedReplied']} 个末条是对方发言（在等回话；要回这批走"
        " docs/agents/apply-runbook.md 的「入站回复」小节）"
    )
    # 读态分桶恒打（0 也打）：未读数**只能靠 wapi 只读字段拿**——链路不许为了分类去点开会话，
    # 点开会话就把未读消耗掉，而这列的全部价值就是告诉人「该去读哪几条」（用户 2026-10-20 硬约束）。
    # 三桶相加 = 上面的 unlinkedReplied；unknown 非零说明来源没给这个键（DOM 降级 / 字段漂移），
    # 此时不要把「无未读标记」那桶读成「都看过了」。
    # 更硬的一条：该字段在实测快照里恒 0（而其中有场次用户确认有未读——场次与证据见
    # `内部调研笔记目录（未随本仓分发）boss-chat-card-shape.md` §7.3，本脚本不自己数）⇒ **这桶基本等于整批**，
    # 它给不了「该读哪几条」；那件事只有 Boss 的未读徽标知道，要人去看（reply_inbound.read_state 注释）。
    print(
        f"        其中有未读标记 {report['unlinkedRepliedUnread']} / 无未读标记 {report['unlinkedRepliedRead']} / "
        f"读态未知 {report['unlinkedRepliedReadUnknown']}"
    )
    # 上面那三桶给不了「先看哪几条」，这条才是（#75）：自己记的水位线，只读数之间的差。
    print(wm.headline(base, due))
    # 全列不设上限、长文行尾补 `…`（#66 验收 1）：行数必须等于上面的 `unlinkedReplied`——这批就是
    # 待办清单本身，截 15 行会静默丢掉等回话的线索，而静默截断又会被读成「对方就说完了」。
    window = 46
    for r in unlinked_replied:
        sm = r["summary"] or "（无摘要）"
        # 「｜线上新」只挂在**线上**那几条末尾（不前移徽标、不改 `…` 的位置——那两样是 #66 的用例契约）。
        above = " ｜线上新" if wm.above_line(r, base, report.get("myUserId")) else ""
        print(
            f"  ▸ [{STATE_LABELS[read_state(r)]}] {r['company']} | {r['title']} | {sm[:window]}"
            f"{'…' if len(sm) > window else ''}{above}"
        )
    print()
    print("=== HR 已回复的对话 ===")
    for r in replied:
        print(f"  {r['company']} | {r['name']} | {r['summary'][:50]} | {r['appliedJob'] or '未关联'}")
    print("\n=== 待回复 ===")
    for r in awaiting[:10]:
        print(f"  {r['company']} | {r['summary'][:40]}")
    print(f"\n[done] 报告存 {out_path}，可运行 gen_greetings.py learn 回写 growing memory")
    print("[next] agent 阅读 HR 回复原文后：followup_tracker.py annotate --annotations <file> 落标注（#19）")


def annotate(annotations_path, out_path=None, history_path=None, store_path=None):
    """标注持久化：upsert 到 reply_annotations.json，并回填当前 followup_report + encryptJobId。
    入口机械校验（#27 项 2）：坏 JSON / 坏形态 / 坏标注拒绝落盘——指明哪条错、提示重写、
    退出码 1，不裸抛 traceback。路径缺省按当前 profile 运行时解析（#28，不做静态绑定）；
    显式传路径不触发解析（多 profile 无 default 时也能对指定文件操作）。"""
    _name, prefix = resolve_active_profile()
    out_path = out_path or private_path("data/followup_report.json", prefix)
    history_path = history_path or private_path("data/applied_history.json", prefix)
    store_path = store_path or private_path("data/reply_annotations.json", prefix)
    try:
        anns = load_annotations(annotations_path)
    except json.JSONDecodeError as e:
        _blocked(f"{annotations_path} 不是合法 JSON（{e}）——修正标注文件后重跑")
    except ValueError as e:
        _blocked(f"{annotations_path} 形态不合法：{e}")
    if not isinstance(anns, list) or not all(isinstance(a, dict) for a in anns):
        _blocked(f"{annotations_path} 须是标注对象数组（元素为 dict）")
    if not anns:
        print(f"[跳过] {annotations_path} 无标注")
        return
    for i, ann in enumerate(anns, 1):
        try:
            validate_annotation(ann)
        except ValueError as e:
            ident = "/".join(
                filter(
                    None,
                    (
                        str(ann.get("company") or "").strip(),
                        str(ann.get("appliedJob") or "").strip(),
                        str(ann.get("encryptJobId") or "").strip(),
                    ),
                )
            )
            _blocked(f"标注不合法（{ident or f'第 {i} 条'}）：{e}")
    missing = [p for p in (out_path, history_path) if not Path(p).exists()]
    if missing:
        print(f"[blocked] 缺少 {'、'.join(map(str, missing))}——先运行 followup_tracker.py 生成报告")
        return
    data = upsert_annotations(store_path, anns)
    report = link_applied_ids(
        merge_annotations(json.loads(Path(out_path).read_text(encoding="utf-8")), data["annotations"]),
        json.loads(Path(history_path).read_text(encoding="utf-8")).get("applied", []),
    )
    Path(out_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[done] 标注 {len(data['annotations'])} 条 → {store_path}，已回填 {out_path}")


def main():
    parser = argparse.ArgumentParser(description="效果追踪 + 回复标注持久化（#19）")
    sub = parser.add_subparsers(dest="cmd")
    p_ann = sub.add_parser("annotate", help="replyCategory/replyLatencyHours 标注落盘并回填当前报告")
    p_ann.add_argument("--annotations", required=True, help="标注 JSON 文件（agent 在 followup 环节产出）")
    args = parser.parse_args()
    if args.cmd == "annotate":
        annotate(args.annotations)
    else:
        run()  # 裸调用 = 跑浏览器 followup（orchestrate.py 无参调用依赖此行为）


if __name__ == "__main__":
    main()
