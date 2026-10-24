#!/usr/bin/env python3
"""platforms 包的行为测试：抽象接口、工厂/配置、平台解析纯函数、stub 平台、
#56 A3 的细颗粒节拍（beat_fn 注入点）。"""

import random
import re
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import platforms
from platforms import PlatformBase
from platforms import chat_first as chat_mod

# #56 取证 A：注册表/计数状态归 `_forensics` 所有，读数与复位都打在**本体**上——
# patch 或读 `chat_mod` 顶部那几个别名只会拿到导入时的快照（静默 no-op，本仓有专门钉它的用例）。
import _forensics as fx  # noqa: E402


# --- 抽象接口与工厂 ---


def test_base_is_abstract():
    with pytest.raises(TypeError):
        PlatformBase()


def test_get_platform_chat_first():
    p = platforms.get_platform("chat-first")
    assert isinstance(p, PlatformBase)
    assert p.name == "chat-first"
    assert "boss-login-dialog" in p.login_dialog_selector


def test_get_platform_unknown_raises():
    with pytest.raises(ValueError):
        platforms.get_platform("linkedin")


def test_get_platform_name_env_override():
    assert platforms.get_platform_name(env={"PLATFORM": " Lagou "}) == "lagou"


def test_get_platform_name_config_file(tmp_path):
    cfg = tmp_path / "platform.json"
    cfg.write_text('{"platform": "liepin"}', encoding="utf-8")
    assert platforms.get_platform_name(env={}, config_path=str(cfg)) == "liepin"
    assert platforms.get_platform_name(env={"PLATFORM": "chat-first"}, config_path=str(cfg)) == "chat-first"


def test_get_platform_name_defaults():
    assert platforms.get_platform_name(env={}, config_path="/nonexistent/platform.json") == "chat-first"


# --- 拉勾/猎聘：接口占位，未实现 ---


@pytest.mark.parametrize("name", ["lagou", "liepin"])
def test_stub_platforms_not_implemented(name):
    p = platforms.get_platform(name)
    assert isinstance(p, PlatformBase)
    with pytest.raises(NotImplementedError):
        p.crawl_jds(None, [], 0, set())
    with pytest.raises(NotImplementedError):
        p.apply(None, {}, "x")
    with pytest.raises(NotImplementedError):
        p.followup(None, [])


# --- ChatFirstPlatform 纯函数：followup 对话解析（#30 修复：字段来源从整卡 innerText 猜行
# --- 改为 wapi 结构化直取 + 结构化 DOM 降级；fixture 为 2026-09-12 真实登录探针 dump，
# --- 见 内部调研笔记目录（未随本仓分发）boss-chat-card-shape.md。旧合成串 fixture 与解析器假设同源，已废。） ---

PLATFORM = platforms.get_platform("chat-first")

# wapi getGeekFriendList.json 条目（真实响应节选，仅裁剪 securityId/avatar 等无关长字段）
WAPI_MINE_READ = {  # 我方最后发言且对方已读——旧解析把这条误判成 hr_replied（[已读]≠回复）
    "name": "壬先生",
    "brandName": "煦珩人力",
    "title": "猎头顾问",
    "lastMsg": "壬先生你好，我有7年Solidity智能合约开发经验，深耕DeFi衍生品协议设计。",
    "lastTime": "昨天",
    "lastTS": 1789097252186,
    "lastMessageInfo": {"fromId": 780045835, "toId": 626134785, "status": 2, "msgTime": 1789097252186},
    "encryptJobId": "45167fe4a07eec100nF93Ni1EVBQ",
}
WAPI_MINE_DELIVERED = {  # 我方最后发言未读（[送达]）
    "name": "邝强",
    "brandName": "上海布沁网络科技",
    "title": "集团人力资源主管",
    "lastMsg": "李总您好，我有7年Web3全栈经验，RWA 和稳定币方向我有一手研究。",
    "lastTime": "昨天",
    "lastTS": 1789092602966,
    "lastMessageInfo": {"fromId": 780045835, "toId": 27243113, "status": 1, "msgTime": 1789092602966},
    "encryptJobId": "40cde3336736a7a30nF93Ni1EVBQ",
}
WAPI_HR_REPLY = {  # HR 最后发言（真实回复文本）
    "name": "砚女士",
    "brandName": "煦辰",
    "title": "招聘者",
    "lastMsg": "对你的简历很感兴趣，期待深入交流。",
    "lastTime": "09月09日",
    "lastTS": 1788931229176,
    "lastMessageInfo": {"fromId": 740868320, "toId": 780045835, "status": 0, "msgTime": 1788931229176},
    "encryptJobId": "187886fb6ba7eff90nJ_3ti7",
}

# 结构化 DOM 卡片（真实 dump：li[role=listitem] 下的 .time/.name-text/.message-status/.last-msg-text）
DOM_CHIP = {  # 带状态徽标 = 我方最后发言
    "time": "昨天",
    "name": "壬先生",
    "name_box": "壬先生煦珩人力猎头顾问",
    "status": "[已读]",
    "status_read": True,
    "last_msg": "壬先生你好，我有7年Solidity智能合约开发经验，深耕DeFi衍生品协议设计。",
}
DOM_NO_CHIP = {  # 无徽标 = HR 最后发言
    "time": "09月09日",
    "name": "砚女士",
    "name_box": "砚女士煦辰招聘者",
    "status": None,
    "status_read": False,
    "last_msg": "对你的简历很感兴趣，期待深入交流。",
}
DOM_NO_CHIP_2 = {  # 无徽标，品牌 4 字（match_brand min_len 下限之上，可回填）
    "time": "09月08日",
    "name": "翠女士",
    "name_box": "翠女士昀洵网络人事专员",
    "status": None,
    "status_read": False,
    "last_msg": "简历已收到，合适会联系您[抱拳]",
}


def test_parse_friend_entries_field_mapping_and_compat_company():
    out = chat_mod.parse_friend_entries([WAPI_MINE_READ], my_user_id=780045835)
    c = out[0]
    assert c["name"] == "壬先生", "name 是联系人，不再是时间样式（#30 验收 1）"
    assert c["company"] == "壬先生煦珩人力猎头顾问", "company 沿用旧粘连语义（reply_annotations 匹配键不变）"
    assert c["brandName"] == "煦珩人力"
    assert c["title"] == "猎头顾问"
    assert c["summary"].startswith("壬先生你好")
    assert c["time"].startswith("2026-09-11"), "time 是真实时间戳，不再是消息正文（#30 验收 2）"
    assert c["lastTime"] == "昨天"
    assert c["encryptJobId"] == "45167fe4a07eec100nF93Ni1EVBQ"
    assert c["source"] == "wapi"


def test_parse_friend_entries_carries_deeplink_ids_and_last_ts():
    """#73：聊天页深链与发送后的 wapi 回读都靠这几个字段，必须从原始条目**原样透传**——
    `encryptUid`（深链首选）、`encryptBossId`（缺失时兜底）、数字 `uid`（落地 URL 校验的
    第三形态）、`lastTS`（回读时判「时间前进了」的对照点）、`lastMessageInfo.fromId`（判末条
    是不是我们发的）。卡片渲染与否与它们无关，这也是本条链路能绕开 40/68 渲染上限的原因。"""
    entry = {
        **WAPI_HR_REPLY,
        "encryptUid": "enc-uid-1",
        "encryptBossId": "enc-boss-1",
        "uid": 740868320,
    }
    c = chat_mod.parse_friend_entries([entry], my_user_id=780045835)[0]
    assert c["encryptUid"] == "enc-uid-1"
    assert c["encryptBossId"] == "enc-boss-1"
    assert c["bossUid"] == 740868320, "数字 uid 单独一个键，不许和两个加密 id 混用"
    assert c["lastTS"] == WAPI_HR_REPLY["lastTS"], "原始毫秒时间戳，不取展示串 `time`"
    assert c["lastFromId"] == 740868320


def test_parse_friend_entries_zero_last_ts_is_passed_through():
    """`lastTS=0` 原样透传（回读时的对照点可以是 0，不必洗成 None 之外的怪值）。"""
    c = chat_mod.parse_friend_entries([{**WAPI_HR_REPLY, "lastTS": 0}])[0]
    assert c["lastTS"] == 0


def test_parse_friend_entries_missing_message_info_has_no_from_id():
    no_payload = {"name": "己女士", "encryptJobId": "eid1", "lastMsg": None, "lastMessageInfo": None}
    c = chat_mod.parse_friend_entries([no_payload], my_user_id=780045835)[0]
    assert c["lastFromId"] is None, "没有消息体就没有 fromId，不猜成我方"


def test_parse_dom_cards_have_no_deeplink_fields():
    """DOM 降级路径没有加密 id / 原始时间戳（卡片只有展示串），一律 None——不许拿文案编一个。"""
    c = chat_mod.parse_dom_cards([DOM_NO_CHIP], applied=[])[0]
    assert c["encryptUid"] is None
    assert c["encryptBossId"] is None
    assert c["bossUid"] is None
    assert c["lastTS"] is None
    assert c["lastFromId"] is None


def test_parse_friend_entries_read_is_not_reply():
    out = chat_mod.parse_friend_entries([WAPI_MINE_READ, WAPI_MINE_DELIVERED, WAPI_HR_REPLY], my_user_id=780045835)
    mine_read, mine_sent, hr = out
    assert mine_read["status"] == "awaiting", "[已读] 绝不算回复，归 awaiting（#30 开放问题裁决）"
    assert mine_read["is_read"] is True, "已读标记保留供独立分析"
    assert mine_sent["status"] == "awaiting" and mine_sent["is_read"] is False
    assert hr["status"] == "hr_replied" and hr["is_read"] is False


def test_parse_friend_entries_status_fallback_without_uid():
    # userId 不可用时按 lastMessageInfo.status 兜底：0=对方消息，1/2=我方消息
    assert chat_mod.parse_friend_entries([WAPI_HR_REPLY], my_user_id=None)[0]["status"] == "hr_replied"
    assert chat_mod.parse_friend_entries([WAPI_MINE_DELIVERED], my_user_id=None)[0]["status"] == "awaiting"


def test_parse_friend_entries_missing_message_info_is_awaiting():
    """2026-09-24 修复：lastMessageInfo 整体缺失（wapi 未 hydrate，实测 3 条）⇒ 无「对方
    回复」证据，保守归 awaiting——旧逻辑空体默认 False → hr_replied，假阳性污染 replied 统计。"""
    no_payload = {
        "name": "己女士",
        "brandName": "煦昱金创",
        "title": "HRBP",
        "encryptJobId": "eid1",
        "lastMsg": None,
        "lastMessageInfo": None,
    }
    out = chat_mod.parse_friend_entries([no_payload], my_user_id=780045835)
    assert out[0]["status"] == "awaiting"
    assert out[0]["lastMsg"] == ""
    # my_user_id 不可用路径同样保守
    assert chat_mod.parse_friend_entries([no_payload], my_user_id=None)[0]["status"] == "awaiting"


def test_parse_friend_entries_applied_job_by_eid():
    applied = [{"encryptJobId": WAPI_HR_REPLY["encryptJobId"], "jobName": "AI产品经理", "brandName": "别的公司"}]
    out = chat_mod.parse_friend_entries([WAPI_HR_REPLY], my_user_id=780045835, applied=applied)
    assert out[0]["appliedJob"] == "AI产品经理", "encryptJobId 精确回查优先于品牌匹配"


def test_parse_dom_cards_semantics():
    out = chat_mod.parse_dom_cards([DOM_CHIP, DOM_NO_CHIP], applied=[])
    mine, hr = out
    assert mine["status"] == "awaiting" and mine["is_read"] is True
    assert mine["company"] == "壬先生煦珩人力猎头顾问"
    assert mine["name"] == "壬先生"
    assert mine["time"] == "昨天", "DOM 路径只有展示串时间"
    assert mine["source"] == "dom"
    assert hr["status"] == "hr_replied" and hr["is_read"] is False
    assert hr["summary"].startswith("对你的简历很感兴趣")
    assert hr["appliedJob"] is None, "无 applied 或品牌多义时不回填"


def test_parse_dom_cards_unique_brand_match_backfills_job():
    applied = [{"brandName": "昀洵网络", "jobName": "Web3安全工程师", "encryptJobId": "e1"}]
    out = chat_mod.parse_dom_cards([DOM_NO_CHIP_2], applied=applied)
    assert out[0]["appliedJob"] == "Web3安全工程师"
    assert out[0]["encryptJobId"] == "e1"


# --- #66 阶段 2：读态三格（有未读 / 无未读标记 / 未知）进读数 ---
# 注：10-21 实测该字段在真机上恒 0（用户确认有未读时也是 0），所以它给不了「该读哪几条」；
# 下面的用例钉的是**三态不混**（未知不得并入 0），不是钉它可用。
# 硬约束来源（用户 2026-10-20）：链路不得消耗未读标记，所以未读数必须是**只读拿到的**，
# 而不是点开之后剩下的。据此，`unreadMsgCount` 的三态不能压成布尔：
# 0＝确实无未读（已读），>0＝还没读，**None＝这一条的来源根本没给**（DOM 降级、或 wapi
# 形态漂移）。把 None 当 0 会把「不知道」谎报成「都读过了」——正是用户要避免的那种损失。


def test_parse_friend_entries_carries_unread_count():
    """wapi 条目自带 `unreadMsgCount`（2026-09-12 快照 61/61 条都有这个键，当时全为 0）。
    非零照实落，0 照实落，**键缺失落 None 而不是 0**。"""
    base = dict(WAPI_HR_REPLY)
    assert chat_mod.parse_friend_entries([{**base, "unreadMsgCount": 3}])[0]["unreadMsgCount"] == 3
    assert chat_mod.parse_friend_entries([{**base, "unreadMsgCount": 0}])[0]["unreadMsgCount"] == 0
    no_key = {k: v for k, v in base.items() if k != "unreadMsgCount"}
    assert chat_mod.parse_friend_entries([no_key])[0]["unreadMsgCount"] is None, "键缺失=读态未知，不许当已读"


def test_parse_dom_cards_unread_state_is_unknown():
    """DOM 降级路径没有未读数（卡片只有时间/姓名/徽标/正文四样），必须报未知。"""
    out = chat_mod.parse_dom_cards([DOM_NO_CHIP], applied=[])
    assert out[0]["unreadMsgCount"] is None


def test_build_report_splits_inbound_waiting_by_read_state():
    """入站且在等回话的那批按读态分桶，且**三桶相加等于 unlinkedReplied**——
    否则「有未读 N / 无未读标记 M」的读数不可核对，未知被静默吞进某一桶。"""
    convs = [
        {"status": "hr_replied", "appliedJob": None, "unreadMsgCount": 2},  # 未读
        {"status": "hr_replied", "appliedJob": None, "unreadMsgCount": 0},  # 无未读标记
        {"status": "hr_replied", "appliedJob": None, "unreadMsgCount": None},  # 未知
        {"status": "awaiting", "appliedJob": None, "unreadMsgCount": 5},  # 末条我方，不入分桶
        {"status": "hr_replied", "appliedJob": "投过的岗", "unreadMsgCount": 7},  # 已关联，不入分桶
    ]
    report = chat_mod.build_report(convs, generated_at="2026-10-20 12:00:00")
    assert report["unlinkedReplied"] == 3
    assert report["unlinkedRepliedUnread"] == 1
    assert report["unlinkedRepliedRead"] == 1
    assert report["unlinkedRepliedReadUnknown"] == 1
    assert (
        report["unlinkedRepliedUnread"] + report["unlinkedRepliedRead"] + report["unlinkedRepliedReadUnknown"]
        == report["unlinkedReplied"]
    )
    assert report["unreadConversations"] == 3, "含我方末条那条（unread=5）也算未读，分桶只看有没有未读"


def test_build_report_read_state_keys_always_present():
    """老会话记录没这个键（历史 followup_report.json 回放）不得 KeyError——恒在且全 0/未知。"""
    report = chat_mod.build_report([{"status": "awaiting", "appliedJob": "A"}], generated_at="2026-10-20 00:00:00")
    assert report["unreadConversations"] == 0
    assert report["unlinkedRepliedUnread"] == 0
    assert report["unlinkedRepliedRead"] == 0
    assert report["unlinkedRepliedReadUnknown"] == 0


def test_build_report_carries_my_user_id():
    """#73：发送后的 wapi 回读要判「末条 fromId 是不是我方」，而 userId 只有采集那一刻知道
    （`getUserInfo` 响应）⇒ 必须跟着报告一起出来，不能靠回读时的 ctx 再去猜。"""
    assert chat_mod.build_report([], generated_at="2026-10-20 00:00:00", my_user_id=780045835)["myUserId"] == 780045835
    assert chat_mod.build_report([], generated_at="2026-10-20 00:00:00")["myUserId"] is None


def test_chat_url_for_quotes_the_id():
    """#73 深链：`/web/geek/chat?id=<招聘者加密 id>`。id 是站点给的串（可能含 `/`、`+`、`=`），
    必须整体 quote——裸拼会把 query 拆坏，落地成另一个人的会话或空页。"""
    assert chat_mod.chat_url_for("45167fe4a07eec100nF93Ni1EVBQ") == (
        "https://www.zhipin.com/web/geek/chat?id=45167fe4a07eec100nF93Ni1EVBQ"
    )
    assert chat_mod.chat_url_for("a b/c&d=e") == "https://www.zhipin.com/web/geek/chat?id=a%20b%2Fc%26d%3De"
    assert chat_mod.chat_url_for(740868320) == "https://www.zhipin.com/web/geek/chat?id=740868320", (
        "函数只做编码；数字 id 能否作为深链由站点决定，本链路不拿它当来源"
    )


# --- #66 阶段 2 归属守卫：入站对话绑的 encryptJobId 现在到底还是不是一个岗位页 ---
# 判据形状全部取自今日实测（`private/data/probe/inbound_coverage_v2_20261020-120316.json`，
# 16 条入站逐条读详情页 document.title），不是凭空列的词。


class _FakeDetailPage:
    """详情页替身：只实现 `job_identity` 真正会碰的四件事（goto / title / url / query_selector）。

    `titles` 是逐次返回的标题序列，模拟「title 水合前是空串」那段轮询。"""

    def __init__(
        self, titles=("", "综合管理岗招聘_某公司招聘-BOSS直聘"), url="https://www.zhipin.com/job_detail/e1.html"
    ):
        self._titles = list(titles)
        self.url = url
        self.goto_urls = []
        self.waited = []

    def goto(self, url, **kwargs):
        self.goto_urls.append(url)

    def title(self):
        return self._titles.pop(0) if len(self._titles) > 1 else (self._titles[0] if self._titles else "")

    def wait_for_timeout(self, ms):
        self.waited.append(ms)

    def query_selector(self, sel):
        return None


def test_clean_job_title_strips_only_the_shell():
    """BOSS 的 title 真形是 `「岗位名」_品牌名招聘-BOSS直聘`（今日 16 条逐条 dump 出来的形状，
    不是设想）。只去壳（引号类符号、站名后缀、分隔符后的品牌段），**不动岗位名本身的字**——
    「招聘」是标题的一部分，留着才能拿去搜/比对。"""
    assert (
        chat_mod.clean_job_title("「区块链-上市公司-六险一金-双休招聘」_四方精创招聘-BOSS直聘")
        == "区块链-上市公司-六险一金-双休招聘"
    )
    assert (
        chat_mod.clean_job_title(
            "产品经理-数币/智能合约/银行支付工作内容_岑淞覃邝2026年产品经理-数币/智能合约/银行支付工作要求-BOSS直聘"
        )
        == "产品经理-数币/智能合约/银行支付"
    )
    assert chat_mod.clean_job_title("「综合管理岗招聘」_宓昱闾笪招聘-BOSS直聘") == "综合管理岗招聘"
    assert chat_mod.clean_job_title("   ") == ""


def test_clean_job_title_underscore_in_title_is_known_loss():
    """已知的**去壳副作用**：标题本身含下划线时，按 `_` 切会把它切残——今日实测
    `「上海___部门管理招聘」_旻珀笪招聘-BOSS直聘` 洗出「上海」。这是**诚实的未知**，
    归 unresolved（认不出是哪个岗 ⇒ 不回、只呈报），不是把它当失效岗。钉住这条是为了
    下一个人别再以为去壳规则是无损的。"""
    assert chat_mod.clean_job_title("「上海___部门管理招聘」_旻珀笪招聘-BOSS直聘") == "上海"
    assert chat_mod.classify_job_identity("「上海___部门管理招聘」_旻珀笪招聘-BOSS直聘")[1] == "unresolved"


def test_classify_job_identity_real_dead_shapes_from_today():
    """今日 16 条里那 4 条「页面活着但已经不是岗位页」的真实形状，逐条必须判 dead。"""
    for raw in (
        "您访问的页面不存在-BOSS直聘",
        "Golang是什么职位_笪曜珀闾2026年Golang前景待遇-BOSS直聘",
        "事业部管理—双休是做什么的_昀覃岑2026年事业部管理—双休岗位职责-BOSS直聘",
        "「什么是外企-朝九晚五-周末双休-955」昱笪旻曜曜旻笪昱笪旻2026年外企-朝九晚五-周末双休-955岗位职责-BOSS直聘",
    ):
        assert chat_mod.classify_job_identity(raw)[1] == "dead", f"{raw} 应判失效岗"


def test_classify_job_identity_ok_and_generic_is_not_dead():
    assert chat_mod.classify_job_identity("「综合管理岗招聘」_宓昱闾笪招聘-BOSS直聘")[1] == "ok"
    # 泛化标题**不是失效**——归属守卫只问「这还是不是一个岗位页」，不问「搜不搜得到」
    # （#66 票面那条「泛化标题 45 条深度搜不到」是搜索覆盖问题，与能不能回这条对话无关）
    assert chat_mod.classify_job_identity("「项目负责人招聘」_知不知咨询有限公司招聘-BOSS直聘")[1] == "ok"
    assert chat_mod.classify_job_identity("「爱博家园兼职服务员招聘」_淞邝煦昀昀煦邝淞邝招聘-BOSS直聘")[1] == "ok"
    assert chat_mod.classify_job_identity("")[1] == "unresolved", "title 取不到＝身份未知，不许猜成 ok"


def test_job_identity_polls_title_and_reports_state():
    page = _FakeDetailPage(
        titles=("", "", "「诚聘升学职业规划师，助力青年规划未来招聘」_昀覃岑煦煦岑覃昀覃岑招聘-BOSS直聘")
    )
    out = chat_mod.job_identity(page, "eid-abc")
    assert page.goto_urls == [chat_mod.JOB_DETAIL_URL.format(eid="eid-abc")], "复用 JOB_DETAIL_URL，不另拼一份 URL"
    assert out["state"] == "ok"
    assert out["jobTitle"] == "诚聘升学职业规划师，助力青年规划未来招聘"
    assert out["encryptJobId"] == "eid-abc"
    assert page.waited == [chat_mod.IDENTITY_POLL_INTERVAL_MS] * 2, (
        "前两次空 title 必须真的等过水合（不轮询也能过＝空过用例）"
    )


def test_job_identity_login_redirect_is_not_dead():
    """掉登录时详情页整页变体是 `/web/user/…`（9-12 探针因此产出过「15 岗全搜不到」的假读数）。
    这种必须报 login，**不能报 dead**——报 dead 会把登录问题洗成「岗已失效」，
    归属守卫据此把整批入站判成不可回。"""
    page = _FakeDetailPage(titles=("注册登录 - BOSS直聘",), url="https://www.zhipin.com/web/user/?src=job")
    out = chat_mod.job_identity(page, "eid-abc")
    assert out["state"] == "login", out
    assert out["jobTitle"] == ""


def test_job_identity_survives_goto_failure():
    """跳转抛异常（网络/协议）⇒ state=unresolved + error 串，不往外抛。
    调用方拿到的是一格读数，不是一枚把整场回复流程打断的异常——#38 观测层不新增失败面。"""

    class _Boom(_FakeDetailPage):
        def goto(self, url, **kwargs):
            raise RuntimeError("net::ERR_CONNECTION_RESET")

    out = chat_mod.job_identity(_Boom(), "eid-abc")
    assert out["state"] == "unresolved"
    assert "ERR_CONNECTION_RESET" in out["error"]


def test_job_identity_survives_poll_failure():
    """goto 成功但**等待那一下**抛（tab 已关/协议断）同样落 unresolved：docstring 承诺的是
    「本函数不往外抛」，漏包这一处就会穿透 resolve_identities → _drive，整场崩掉且报告不落盘。"""

    class _PollBoom(_FakeDetailPage):
        def wait_for_timeout(self, ms):
            raise RuntimeError("Target page, context or browser has been closed")

    out = chat_mod.job_identity(_PollBoom(titles=("", "")), "eid-abc")
    assert out["state"] == "unresolved"
    assert "has been closed" in out["error"]


class _FakeReq:
    """`Response.request` 的替身：只带采集侧要数的 `friendIds` 表单体。"""

    def __init__(self, post_data):
        self.post_data = post_data


class _FakeResp:
    """形状必须跟生产一致：Playwright 的 `Response.request` 是**属性**不是方法。#63 真机踩过——
    按方法调会抛 `TypeError: Request object is not callable`，被采集侧兜底吃成
    `requestedIds=None`，而当时替身给的正是一个可调用的 `request()`，于是全套用例全绿。"""

    def __init__(self, url, body, post_data=None, request_raises=False, request_callable=False):
        self.url = url
        self._b = body
        self._request_raises = request_raises
        self._request_callable = request_callable
        self._req = None if (post_data is None and not request_callable) else _FakeReq(post_data)

    def json(self):
        return self._b

    @property
    def request(self):
        if self._request_raises:
            raise RuntimeError("request 这一格在这个现场读不出来")
        if self._request_callable:
            # 老替身（错误形状）：`request` 给的是可调用对象 ⇒ 取 `.post_data` 必抛 AttributeError
            return lambda: self._req
        return self._req


class _FakeChatPage:
    """followup 编排测试替身：response 监听注册后由 goto 派发（模拟事件到达）。"""

    def __init__(self, wapi=True, login_selector_hit=False, url="https://www.zhipin.com/web/geek/chat", title="消息"):
        self.handlers = {}
        self.evaluated = []
        self._wapi = wapi
        self._login = login_selector_hit
        self.url = url
        self._title = title

    def title(self):
        return self._title

    def on(self, ev, h):
        self.handlers.setdefault(ev, []).append(h)

    def remove_listener(self, ev, h):
        self.handlers.get(ev, []).remove(h)

    def goto(self, url, **kw):
        if not self._wapi:
            return
        for h in self.handlers.get("response", []):
            h(
                _FakeResp(
                    "https://www.zhipin.com/wapi/zpuser/wap/getUserInfo.json",
                    {"code": 0, "zpData": {"userId": 780045835}},
                )
            )
            h(
                _FakeResp(
                    "https://www.zhipin.com/wapi/zprelation/friend/getGeekFriendList.json",
                    {"code": 0, "zpData": {"result": [WAPI_MINE_READ]}},
                )
            )

    def query_selector(self, sel):
        return True if self._login else None

    def evaluate(self, js):
        self.evaluated.append(js)
        return [DOM_CHIP, DOM_NO_CHIP]

    def wait_for_timeout(self, ms):
        pass


# --- 整页登录变体的判据（2026-10-20 实测：掉登录时导到 /web/user/，无 #login-dialog 元素）---


class _PageStub:
    def __init__(self, url="https://www.zhipin.com/web/geek/jobs", title="上海招聘", dialog=False):
        self.url = url
        self._title = title
        self._dialog = dialog

    def title(self):
        return self._title

    def query_selector(self, sel):
        return object() if self._dialog else None


def test_is_logged_out_three_signals():
    """三条并判：弹窗 / 整页登录 URL / 标题「注册登录」。任一命中即判掉登录。"""
    assert chat_mod.is_logged_out(_PageStub(url="https://www.zhipin.com/web/user/"))
    assert chat_mod.is_logged_out(_PageStub(title="【BOSS直聘注册登录】boss直聘在线注册登录-BOSS直聘"))
    assert chat_mod.is_logged_out(_PageStub(dialog=True))
    assert not chat_mod.is_logged_out(_PageStub())


def test_is_logged_out_survives_unreadable_page():
    """页面属性读不到时不误判成「登录过期」（只保留弹窗判据）。"""

    class _Broken:
        def query_selector(self, sel):
            return None

    assert not chat_mod.is_logged_out(_Broken())


def test_followup_returns_none_on_full_page_login(monkeypatch, capsys):
    """整页登录（无弹窗元素）必须判成掉登录并返回 None——否则上层会把未登录
    当成「采到 0 条对话」走残缺降级（2026-10-20 实测形态）。"""
    monkeypatch.setattr(chat_mod.time, "sleep", lambda *_: None)
    page = _FakeChatPage(wapi=False, url="https://www.zhipin.com/web/user/", title="【BOSS直聘注册登录】")
    assert chat_mod.ChatFirstPlatform().followup(page, applied=[]) is None
    assert "登录态过期" in capsys.readouterr().out


def test_followup_prefers_wapi_interception(monkeypatch):
    monkeypatch.setattr(chat_mod.time, "sleep", lambda *_: None)
    page = _FakeChatPage()
    report = chat_mod.ChatFirstPlatform().followup(page, applied=[])
    assert report["totalConversations"] == 1
    assert report["myUserId"] == 780045835, "#73：回读判据要用我方 userId，必须随报告出来"
    conv = report["conversations"][0]
    assert conv["source"] == "wapi"
    assert conv["status"] == "awaiting" and conv["is_read"] is True
    assert not page.evaluated, "wapi 命中时不走 DOM evaluate"


def test_followup_falls_back_to_structured_dom(monkeypatch):
    monkeypatch.setattr(chat_mod.time, "sleep", lambda *_: None)
    page = _FakeChatPage(wapi=False)
    report = chat_mod.ChatFirstPlatform().followup(page, applied=[])
    assert report["totalConversations"] == 2
    assert [c["source"] for c in report["conversations"]] == ["dom", "dom"]
    assert page.evaluated, "wapi 未命中时降级 DOM evaluate"


# --- #59 轮询出口：wapi 响应**可能**分批到达（实测形态是一次调用返回全量，见
#     内部调研笔记目录（未随本仓分发）boss-chat-card-shape.md §3），「第一个非空响应」会照单全收首屏 ---


FRIEND_LIST_URL = "https://www.zhipin.com/wapi/zprelation/friend/getGeekFriendList.json"


def _friend_batch(n, tag="司"):
    """n 条 wapi 会话条目（字段形态够 parse_friend_entries 计数即可，不测解析）。

    **每条必须带自己的 `encryptJobId`**：真实响应里会话键逐条唯一（#63 切片的实物是 09-12
    那份 61 个 id 回 61 条）。批次若共用同一个键，「按会话键取并集」会把整批塌成 1 条，
    用例就绿在错的地方——这条不是风格问题，是并集判据的唯一防线。"""
    return [
        dict(WAPI_MINE_READ, name=f"{tag}{i}", brandName=f"{tag}公司{i}", encryptJobId=f"{tag}eid{i}") for i in range(n)
    ]


class _FakeIncrementalChatPage:
    """分批到达替身：goto 派发首批（first_batch=None 表示 wapi 整场没响应），其后每次
    wait_for_timeout 派发脚本队列的下一批。**这是对到达时序的防御性建模，不是站点实测结论**：
    实测到的是「一次调用返回全部」（各批互为超集，09-12 那份 61 个 friendIds 回 61 条）。
    出口因此是按会话键取**并集**（#63 切片 A）：超集形态下并集＝最大批（下面两条用例把这条
    等价性钉住），互不包含的增量形态下并集不再丢批——旧「取最大批」出口在这种形态下会静默
    丢掉整批，那是 chat_first.py 里登记过、当时没有出口的那条风险。"""

    def __init__(self, first_batch, later_batches=(), post_data=None, request_raises=False):
        self.handlers = {}
        self._first = None if first_batch is None else list(first_batch)
        self._queue = [list(b) for b in later_batches]
        self._post_data = post_data
        self._request_raises = request_raises
        self.waits = 0
        self.evaluated = []

    def on(self, ev, h):
        self.handlers.setdefault(ev, []).append(h)

    def _dispatch(self, batch):
        for h in self.handlers.get("response", []):
            h(
                _FakeResp(
                    FRIEND_LIST_URL,
                    {"code": 0, "zpData": {"result": batch}},
                    post_data=self._post_data,
                    request_raises=self._request_raises,
                )
            )

    def goto(self, url, **kw):
        if self._first is not None:
            self._dispatch(self._first)

    def query_selector(self, sel):
        return None

    def evaluate(self, js):
        self.evaluated.append(js)
        return []

    def wait_for_timeout(self, ms):
        self.waits += 1
        if self._queue:
            self._dispatch(self._queue.pop(0))


def test_followup_union_of_superset_batches_equals_the_largest_batch(monkeypatch):
    """#59 验收 3 的形态：首屏 3 条、随后两批各 50 条，且后批**包含**前批（站点实测形态＝
    一次调用返回全量，各批互为超集）⇒ 并集就是那 50 条。旧出口「取最大批」在这里同样给 50，
    这条用例钉的是**改了出口不许把这种形态做坏**（#63 切片 A 的行为不变侧）。
    旧版在这里取到 3 条是另一场事故（20260929-202514 的 3/100 覆盖掉全量报告），防线是扫满窗口。"""
    monkeypatch.setattr(chat_mod.time, "sleep", lambda *_: None)
    page = _FakeIncrementalChatPage(_friend_batch(3), [_friend_batch(50), _friend_batch(50)])
    report = chat_mod.ChatFirstPlatform().followup(page, applied=[])
    assert report["totalConversations"] == 50
    assert [c["name"] for c in report["conversations"]] == [f"司{i}" for i in range(50)], (
        "并集顺序必须仍是站点给的新→旧"
    )


def test_followup_union_of_shrinking_superset_batches_keeps_the_full_one(monkeypatch):
    """递减形态 100 → 3 → 3（后批是前批的前缀）⇒ 返回 **100**（双轴评审的反例：上一版
    「条数连续两轮不变即返回」在这里返回 3，比旧实现「第一个非空即返回」更差）。并集出口同样
    不返回「最后一次读数」，因为那 3 条的键已经在那 100 条里。"""
    monkeypatch.setattr(chat_mod.time, "sleep", lambda *_: None)
    page = _FakeIncrementalChatPage(_friend_batch(100), [_friend_batch(3), _friend_batch(3)])
    report = chat_mod.ChatFirstPlatform().followup(page, applied=[])
    assert report["totalConversations"] == 100


def test_followup_unions_disjoint_batches_instead_of_dropping_one(monkeypatch):
    """#63 切片 A 的承重用例（RED 在 baseline 上取的）：两批**互不包含**——站点若改成增量
    分页，这是它必然给出的形态。旧出口「取最大批」在这里只留下最后那 3 条，**整批 3 条被静默
    丢掉**；并集出口给 6 条。chat_first.py 原注释把这条登记成「那时正确的出口是按 encryptJobId 取
    并集」，本用例就是那条假定的兑现，不是修今天已经在发生的丢批。"""
    monkeypatch.setattr(chat_mod.time, "sleep", lambda *_: None)
    page = _FakeIncrementalChatPage(_friend_batch(3, tag="甲"), [_friend_batch(3, tag="乙")])
    report = chat_mod.ChatFirstPlatform().followup(page, applied=[])
    assert report["totalConversations"] == 6, "增量分页形态下丢了一整批（旧「取最大批」出口的形态）"
    assert sorted(c["name"] for c in report["conversations"]) == sorted(["甲0", "甲1", "甲2", "乙0", "乙1", "乙2"])


def test_followup_union_matches_previous_exit_when_a_later_batch_shares_a_key(monkeypatch):
    """换键形态（#75 review 条目 I 的同一个坑）：同一条会话这场只有 `encryptUid`、下场两个键
    都有 ⇒ 「任一已知键相同」就是同一条，不许因为它换了键就念成两条新会话（虚增条数会喂给
    #63 的残缺判据一个假增长）。"""
    monkeypatch.setattr(chat_mod.time, "sleep", lambda *_: None)
    first = [dict(WAPI_MINE_READ, name="孙先生", encryptJobId="", encryptUid="740868320")]
    later = [dict(WAPI_MINE_READ, name="孙先生", encryptJobId="eid-new", encryptUid="740868320")]
    page = _FakeIncrementalChatPage(first, [later])
    report = chat_mod.ChatFirstPlatform().followup(page, applied=[])
    assert report["totalConversations"] == 1, "同一条会话换键被念成两条"
    assert report["followupCollection"]["unkeyed"] == 0


def test_followup_union_consumes_the_shared_session_key_rule(monkeypatch):
    """并集去重**必须**走 `_inbound_watermark.session_keys`（一条会话的全部可用键），不许在
    平台层再写一份键规则。这条用 monkeypatch 把那个函数换成「什么都不返回」的桩：键全空 ⇒
    两条同 eid 的条目都留下、`unkeyed` 变成 4——读数跟着变才证明判据真的出自那个函数，
    而不是本文件里另抄了一份（自证式断言挡住的是「以后有人另抄一份」）。
    两批各新建一个页面替身：`_FakeIncrementalChatPage` 的批次队列会被第一次 followup 消耗掉，
    复用一个实例只会派发首批（我第一版就栽在这里，得到的是「批次 1」而不是「键没被消费」）。"""
    monkeypatch.setattr(chat_mod.time, "sleep", lambda *_: None)
    batch = _friend_batch(2)

    def collect():
        page = _FakeIncrementalChatPage(batch, [batch])
        return chat_mod.ChatFirstPlatform().followup(page, applied=[])

    assert collect()["totalConversations"] == 2, "同一键的两批本就该并成 2 条（对照侧）"
    monkeypatch.setattr(chat_mod, "session_keys", lambda conv: ())
    report = collect()
    assert report["totalConversations"] == 4, "键规则没被消费：并集段其实还在自己判"
    assert report["followupCollection"]["unkeyed"] == 4


def test_followup_collection_readings_are_present_on_every_exit(monkeypatch):
    """三条出口（wapi 命中 / 命中但空批降级 / 整场没命中降级）都必须带同一组采集形态键——
    键恒在、值可为 None（口径同 #62 `no_send_reasons`、#65 `recommend_*`）。缺键会让 #63 的
    残缺判据在 DOM 那条路上读不到东西，正是 #75 review 条目 B 抓过的形态（恒在恰在用户真读
    的那条路上破了）。"""
    monkeypatch.setattr(chat_mod.time, "sleep", lambda *_: None)
    keys = {"path", "batches", "batchSizes", "maxBatch", "requestedIds", "union", "unkeyed"}
    cases = {
        "wapi": _FakeIncrementalChatPage(_friend_batch(4)),
        "空批": _FakeIncrementalChatPage([], [[]]),
        "整场没命中": _FakeIncrementalChatPage(None),
    }
    for label, page in cases.items():
        report = chat_mod.ChatFirstPlatform().followup(page, applied=[])
        reading = report["followupCollection"]
        assert set(reading) == keys, f"{label} 出口少了/多了采集形态键：{sorted(set(reading) ^ keys)}"
        assert reading["path"] == ("wapi" if label == "wapi" else "dom"), label


def test_followup_reading_distinguishes_no_hit_from_empty_batch(monkeypatch):
    """「接口一次都没回」与「回了但每批都是空列表」是两种现场（前者像登录态/导航问题，后者
    像站点给了空号），`batches` 把两者分开：0 与 2。降级路径两条都走，别把它们读成同一种。"""
    monkeypatch.setattr(chat_mod.time, "sleep", lambda *_: None)
    never = chat_mod.ChatFirstPlatform().followup(_FakeIncrementalChatPage(None), applied=[])
    empty = chat_mod.ChatFirstPlatform().followup(_FakeIncrementalChatPage([], [[]]), applied=[])
    assert never["followupCollection"]["batches"] == 0
    assert empty["followupCollection"]["batches"] == 2
    assert empty["followupCollection"]["batchSizes"] == [0, 0]
    assert empty["followupCollection"]["maxBatch"] == 0
    assert never["followupCollection"]["maxBatch"] is None, "一批都没回时「最大批」是「无从可算」，不是 0"


def test_followup_counts_requested_ids_from_the_response_request_body(monkeypatch):
    """`friendIds` 的个数是「前端到底下发了多少条会话」这一格的唯一来源（票面 1 号假设的实物
    就是 09-12 那份 61 个 id 回 61 条）。它与并集条数一起念，才分得开「站点就只有这些」与
    「接口回得比请求少」。"""
    monkeypatch.setattr(chat_mod.time, "sleep", lambda *_: None)
    page = _FakeIncrementalChatPage(_friend_batch(3), post_data="friendIds=a%2Cb%2Cc&other=1")
    report = chat_mod.ChatFirstPlatform().followup(page, applied=[])
    assert report["followupCollection"]["requestedIds"] == 3


def test_followup_reads_the_request_as_a_property_not_a_call(monkeypatch, capsys):
    """#63 真机回归（`requestedIds` 在真实浏览器上恒 `None` 的根因）：`Response.request` 是属性。
    按方法调会抛 `TypeError`，被兜底吃成 `None` ⇒ 那一格永远问不出东西，而报告与终端长得
    和「站点没下发 id」一模一样。夹具用生产的属性形状；代码若退回 `resp.request()`，
    既要红、又必须当场喊出访问形态不对（不静默）。"""
    monkeypatch.setattr(chat_mod.time, "sleep", lambda *_: None)
    page = _FakeIncrementalChatPage(_friend_batch(3), post_data="friendIds=a%2Cb%2Cc")
    report = chat_mod.ChatFirstPlatform().followup(page, applied=[])
    assert report["followupCollection"]["requestedIds"] == 3, report["followupCollection"]
    assert "[warn]" not in capsys.readouterr().out, "属性形状被读成了异常现场"

    # 反向：把响应做成「request 是方法」的旧替身形状 —— 生产码不许静默给出 None
    old_shaped = _FakeResp(
        FRIEND_LIST_URL,
        {"code": 0, "zpData": {"result": _friend_batch(2)}},
        post_data="friendIds=x%2Cy",
        request_callable=True,
    )

    class _OldShapedPage(_FakeIncrementalChatPage):
        def goto(self, url, **kw):
            for h in self.handlers.get("response", []):
                h(old_shaped)

    report2 = chat_mod.ChatFirstPlatform().followup(_OldShapedPage([]), applied=[])
    term = capsys.readouterr().out
    assert report2["followupCollection"]["requestedIds"] is None, "访问形态错时不许编出条数"
    assert "[warn]" in term and "request" in term, f"访问形态错了却静默成 None：{term}"


def test_followup_requested_ids_stays_none_when_the_request_is_unreadable(monkeypatch):
    """取不到请求体（真浏览器给不出、或读取抛异常）⇒ `requestedIds` 落 **None**，
    不许落 0——0 会被读成「前端一条会话都没下发」，而那是这条读数最想回答的问题（本仓
    `null ≠ 0` 口径，见 #61/#65）。"""
    monkeypatch.setattr(chat_mod.time, "sleep", lambda *_: None)
    for label, page in (
        ("没有 request", _FakeIncrementalChatPage(_friend_batch(2))),
        ("request() 抛", _FakeIncrementalChatPage(_friend_batch(2), request_raises=True)),
        ("表单体读不出", _FakeIncrementalChatPage(_friend_batch(2), post_data="")),
    ):
        report = chat_mod.ChatFirstPlatform().followup(page, applied=[])
        assert report["followupCollection"]["requestedIds"] is None, label
        assert report["totalConversations"] == 2, f"{label} 时采集本身不许受影响"


def test_followup_keeps_keyless_entries_and_counts_them(monkeypatch):
    """没有加密 id 的条目（DOM 降级卡片就是这个形态）在并集段**不静默丢**：条目照留，
    `unkeyed` 单独数一条（#75 的 `unkeyed` 纪律）。两个键都空的同一条目来两次也不合并——
    没有任何依据说它们是同一条。"""
    monkeypatch.setattr(chat_mod.time, "sleep", lambda *_: None)
    uid_missing = dict(WAPI_MINE_READ, name="只有岗位键", encryptJobId="eid-1", encryptUid=None)
    no_keys = dict(WAPI_MINE_READ, name="两个键都没有", encryptJobId="", encryptUid=None)
    for probe, want_unkeyed in ((uid_missing, 0), (no_keys, 1)):
        page = _FakeIncrementalChatPage([probe])
        report = chat_mod.ChatFirstPlatform().followup(page, applied=[])
        assert report["totalConversations"] == 1, probe
        assert report["followupCollection"]["unkeyed"] == want_unkeyed, probe
    both_keyless_twice = _FakeIncrementalChatPage([no_keys], [[no_keys]])
    report = chat_mod.ChatFirstPlatform().followup(both_keyless_twice, applied=[])
    assert report["totalConversations"] == 2, "无键条目被当成同一条合并掉了"
    assert report["followupCollection"]["unkeyed"] == 2


def test_followup_union_keeps_the_freshest_version_of_a_repeated_conversation(monkeypatch):
    """同一条会话在两批里都出现——超集形态天天如此（后批是同一批 id 的**新读数**）。并集必须
    **保留位置、取最新内容**：取首次出现的那条会把 15s 窗口里已经刷新过的 `lastMsg`/`lastTS`
    停在旧值上，而 #75 的水位线判据正是拿 `lastTS` 比线、`reply_inbound` 的回读拿 `lastMsg`
    全等判「末条是不是我方」——停在旧值等于把这两条判据喂陈旧数据。"""
    monkeypatch.setattr(chat_mod.time, "sleep", lambda *_: None)
    old = dict(
        WAPI_MINE_READ,
        name="辛女士",
        encryptJobId="eid-fresh-check",
        lastMsg="旧的一条",
        lastTS=1789000000000,
        lastMessageInfo={"fromId": 780045835, "toId": 626134785, "status": 2, "msgTime": 1789000000000},
    )
    new = dict(
        old,
        lastMsg="新的一条",
        lastTS=1789000900000,
        lastMessageInfo={"fromId": 780045835, "toId": 626134785, "status": 2, "msgTime": 1789000900000},
    )
    page = _FakeIncrementalChatPage([old], [[new]])
    report = chat_mod.ChatFirstPlatform().followup(page, applied=[])
    assert report["totalConversations"] == 1, "同一条会话被并成两条"
    assert report["conversations"][0]["lastMsg"] == "新的一条", "并集取了首次出现的旧值"
    # 反方向也要钉住：先到的是新值时，后到的旧值不许把它盖回去（到达顺序不等于时间顺序，
    # 站点在窗口里可能重放一次更早的读数）——取 lastTS 大的那条。
    page2 = _FakeIncrementalChatPage([new], [[old]])
    report2 = chat_mod.ChatFirstPlatform().followup(page2, applied=[])
    assert report2["conversations"][0]["lastMsg"] == "新的一条", "后到的陈旧读数把新值盖回去了"


def test_followup_union_collapses_a_key_bridge_into_one_entry(monkeypatch):
    """两条已收条目被第三条的键连成同一条会话时（先只带岗位键、再只带 uid、第三条两个都有），
    并集要塌成**一条**、位置取靠前那个、内容取 `lastTS` 大的那条。留下两条就是让 #63 的残缺
    判据读到一次假的增长——条数虚高那一侧不会有人报警，被冻住的报告才是后果。"""
    monkeypatch.setattr(chat_mod.time, "sleep", lambda *_: None)
    by_job = dict(WAPI_MINE_READ, name="桥", encryptJobId="E1", encryptUid=None)
    other = dict(WAPI_MINE_READ, name="旁", brandName="旁公司", encryptJobId="E9", encryptUid="U9")
    by_uid = dict(WAPI_MINE_READ, name="桥", brandName="桥公司", encryptJobId="E2", encryptUid="U1")
    both = dict(
        WAPI_MINE_READ,
        name="桥",
        brandName="桥公司",
        encryptJobId="E1",
        encryptUid="U1",
        lastMsg="桥接后的新一条",
        lastTS=1789099999999,
        lastMessageInfo={"fromId": 780045835, "toId": 626134785, "status": 2, "msgTime": 1789099999999},
    )
    page = _FakeIncrementalChatPage([by_job, other, by_uid], [[both]])
    report = chat_mod.ChatFirstPlatform().followup(page, applied=[])
    assert report["totalConversations"] == 2, "一条会话被桥接成两条"
    assert [c["name"] for c in report["conversations"]] == ["桥", "旁"], "位置该取桥接前靠前那一条"
    assert report["conversations"][0]["lastMsg"] == "桥接后的新一条", "内容该取 lastTS 大的那条"
    assert report["followupCollection"]["union"] == 2


def test_followup_union_keeps_new_to_old_order_for_a_head_superset(monkeypatch):
    """超集的第二种形状：后批在**头部**多一条新会话（窗口里来了新消息就是这样）。
    并集必须仍然是新→旧，而且与「最大批」（也就是那 4 条那一批）逐字同序同内容——
    这条不变量是 `_followup_shape.compare` 位置签名判据的前提：按首次出现排会把最新那条
    甩到列表末尾，下一场的首页截断就被读成 `scattered_lost`、否决腿当场失效
    （双轴 review 查出，/tmp 里用 97+1 复现过 `veto=False`）。"""

    def entry(name, ts):
        return dict(
            WAPI_MINE_READ,
            name=name,
            brandName=f"{name}公司",
            encryptJobId=f"eid-{name}",
            lastTS=ts,
            lastMessageInfo={"fromId": 780045835, "toId": 626134785, "status": 2, "msgTime": ts},
        )

    older = [entry("p0", 1000), entry("p1", 900), entry("p2", 800)]
    head_superset = [entry("NEW", 2000)] + older
    monkeypatch.setattr(chat_mod.time, "sleep", lambda *_: None)
    report = chat_mod.ChatFirstPlatform().followup(_FakeIncrementalChatPage(older, [head_superset]), applied=[])
    names = [c["name"] for c in report["conversations"]]
    assert names == ["NEW", "p0", "p1", "p2"], f"并集序被打乱：{names}"
    assert report["followupCollection"]["union"] == 4
    assert report["followupCollection"]["maxBatch"] == 4
    # 排序发生在原始条目层（报告条目已被 parse 换掉时间字段），所以直接验那一层
    ordered = chat_mod._sorted_newest_first(head_superset)
    assert [e["name"] for e in ordered] == ["NEW", "p0", "p1", "p2"]
    assert chat_mod._sorted_newest_first([entry("无时间", None), entry("p0", 1000)])[0]["name"] == "p0", (
        "读不到 lastTS 的条目排末尾——不冒充最新"
    )


def test_followup_single_batch_still_scans_full_window(monkeypatch):
    """只来一轮 50 条 ⇒ 仍返回 50：扫满窗口是改判据的固定代价（≈15s），不因「看起来已稳定」
    提前定稿——提前定稿正是上一版被证伪的那条。"""
    monkeypatch.setattr(chat_mod.time, "sleep", lambda *_: None)
    page = _FakeIncrementalChatPage(_friend_batch(50))
    report = chat_mod.ChatFirstPlatform().followup(page, applied=[])
    assert report["totalConversations"] == 50
    assert page.waits == 50, "扫满窗口是固定代价（真实语义：等满才定稿）"


def test_followup_empty_batch_hit_falls_back_to_dom_after_full_window(monkeypatch):
    """命中但整场都是空批（code=0 而 result 为空）⇒ 与「wapi 整场没命中」同一条路：
    扫满 50 轮后走 DOM 降级，不把空批当成「拿到数据」就地定稿。"""
    monkeypatch.setattr(chat_mod.time, "sleep", lambda *_: None)
    page = _FakeIncrementalChatPage([], [[]])
    report = chat_mod.ChatFirstPlatform().followup(page, applied=[])
    assert page.waits == 50, "空批也在扫满窗口之后才降级"
    assert page.evaluated, "空批降级 DOM evaluate"
    assert report["totalConversations"] == 0


def test_followup_wapi_miss_still_waits_full_rounds_for_dom(monkeypatch):
    """wapi 整场没响应 ⇒ 仍等满 50 轮再降级 DOM（与空批降级同一条路，两者等待时长一致）。"""
    monkeypatch.setattr(chat_mod.time, "sleep", lambda *_: None)
    page = _FakeIncrementalChatPage(None)
    report = chat_mod.ChatFirstPlatform().followup(page, applied=[])
    assert page.waits == 50, "无捕获与空批一样：扫满窗口才降级"
    assert page.evaluated, "wapi 未命中时降级 DOM evaluate"
    assert report["totalConversations"] == 0


def test_followup_beats_land_around_goto_and_on_every_poll_round(monkeypatch):
    """#59 修复轮 R4-1（平台层侧）：注入 beat_fn 后 goto 前后各一拍、轮询每一轮一拍——
    「聊天页加载慢」不再等于「无心跳」。label 用 `followup:wait-friends:{n}` 一族，与
    `search:wait-xhr:{n}` / `jd:dom-poll:{n}:*` 同族同风格（轮次进 label，停在第几轮读得出来）。"""
    monkeypatch.setattr(chat_mod.time, "sleep", lambda *_: None)
    beats = []
    page = _FakeIncrementalChatPage(_friend_batch(2))
    report = chat_mod.ChatFirstPlatform().followup(page, applied=[], beat_fn=beats.append)
    assert report["totalConversations"] == 2, "拍点是观测旁路，采集语义一字不动"
    assert beats[:2] == ["followup:goto-chat:pre", "followup:goto-chat:post"]
    assert beats[2:] == [f"followup:wait-friends:{i}" for i in range(1, chat_mod.FRIEND_POLL_ROUNDS + 1)]


def test_followup_waits_come_from_the_pinned_constants(monkeypatch):
    """本段等待的**来源**必须是那四个模块常量，而不是散在函数体里的字面量——阈值护栏用例
    `test_followup_chat_page_silence_ceiling_sits_below_both_thresholds` 读的就是这些名字，
    有人把 goto 的 timeout 改回内联数字时，护栏就变成读空气（这条先红）。"""
    slept = []
    monkeypatch.setattr(chat_mod.time, "sleep", lambda s, *_: slept.append(s))

    class _ProbePage(_FakeIncrementalChatPage):
        """记录真实调用参数的页面替身：goto 的 timeout、每轮 wait 的毫秒数。"""

        def __init__(self):
            super().__init__(_friend_batch(1))
            self.goto_timeouts = []
            self.wait_ms = []

        def goto(self, url, **kw):
            self.goto_timeouts.append(kw.get("timeout"))
            super().goto(url, **kw)

        def wait_for_timeout(self, ms):
            self.wait_ms.append(ms)
            super().wait_for_timeout(ms)

    page = _ProbePage()
    chat_mod.ChatFirstPlatform().followup(page, applied=[])
    assert page.goto_timeouts == [chat_mod.CHAT_GOTO_TIMEOUT_MS]
    assert slept == [chat_mod.FOLLOWUP_POST_GOTO_SLEEP_SECONDS]
    assert page.wait_ms == [chat_mod.FRIEND_POLL_INTERVAL_MS] * chat_mod.FRIEND_POLL_ROUNDS


# --- #56 取证 A（平台层侧）：`followup` 的 handler 与两次取体进同一份往返计数 ---


def test_resp_json_with_forensics_reads_body_once_and_balances_the_counter():
    """包装体本身：`resp.json()` **只求值一次**（多一次取体=多一次无超时往返=多一次楔死机会，
    与 `live_apply.capture_xhr` 那条 R3 铁律同源），且 enter/exit 在异常路径上也平衡——
    计数漂了就等于把「下一次卡死」的读数提前作废。"""
    fx._reset_handler_forensics()

    class _Counting:
        def __init__(self):
            self.calls = 0

        def json(self):
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("第二次求值不该发生")
            return {"code": 0}

    resp = _Counting()
    assert chat_mod._resp_json_with_forensics(resp, chat_mod.FRIEND_LIST_URL_FRAG) == {"code": 0}
    assert resp.calls == 1, "取体只求值一次（enter/exit 包的是同一条语句）"
    assert fx.handler_forensics() == {
        "handlers_live": 0,
        "handlers_inflight": 0,
        "handler_last_frag": chat_mod.FRIEND_LIST_URL_FRAG,
    }

    class _Boom:
        def json(self):
            raise RuntimeError("Timeout 30000ms exceeded while reading response body")

    # 异常**照原样外抛**：`followup` 那个 `except Exception: pass` 的口径不许被包装体改掉
    with pytest.raises(RuntimeError):
        chat_mod._resp_json_with_forensics(_Boom(), chat_mod.USER_INFO_URL_FRAG)
    assert fx.handler_forensics()["handlers_inflight"] == 0, "finally 保证配对，异常路径也一样平衡"


def test_followup_registers_its_response_handler(monkeypatch):
    """接线证明：`page.on("response", h)` 之后 handler 进了注册表，两次派发（user/info 与
    friend/list）各留下一对 enter/exit、收尾平衡。
    **默认路径的 handler 没有注销点**（followup 走会话末尾的聊天页，页面随后整体关闭），所以
    这一项会一直留在 `handlers_live` 里——判读时记得收尾之后的读数天然偏高 1，它不是逐岗泄漏
    （出处见 `chat_first.py` 里 `listener_attached(h)` 那条注释）。同页重复读用 `detach=True`，
    另见下一条用例。"""
    fx._reset_handler_forensics()
    monkeypatch.setattr(chat_mod.time, "sleep", lambda *_: None)
    page = _FakeChatPage()
    report = chat_mod.ChatFirstPlatform().followup(page, applied=[])
    assert report["totalConversations"] == 1, "取证接线不改采集语义"
    reading = fx.handler_forensics()
    assert reading["handlers_live"] == 1, reading
    assert reading["handlers_inflight"] == 0, reading
    # `_FakeChatPage.goto` 先派 user/info、后派 friend/list ⇒ 最后进入的那次往返是 friend list
    assert reading["handler_last_frag"] == chat_mod.FRIEND_LIST_URL_FRAG, reading


def test_followup_detach_removes_its_handler(monkeypatch):
    """`detach=True` 给同页重复读用（#73 发送后回读）：出口必须注销自己挂的 handler，
    否则同一页第 2、3 次 followup 会把 `handlers_live` 抬到 2、3，「偏高 1」的判读失效。"""
    fx._reset_handler_forensics()
    monkeypatch.setattr(chat_mod.time, "sleep", lambda *_: None)
    page = _FakeChatPage()
    report = chat_mod.ChatFirstPlatform().followup(page, applied=[], detach=True)
    assert report["totalConversations"] == 1, "注销不影响采集结果"
    assert page.handlers["response"] == []
    assert fx.handler_forensics()["handlers_live"] == 0


def test_build_report_counts():
    results = [
        {"status": "hr_replied", "appliedJob": "A"},
        {"status": "awaiting", "appliedJob": "A"},
        {"status": "hr_replied", "appliedJob": None},
        {"status": "awaiting", "appliedJob": None},
    ]
    report = chat_mod.build_report(results, generated_at="2026-09-06 12:00:00")
    assert report["totalConversations"] == 4
    assert report["hrReplied"] == 2
    assert report["awaiting"] == 2
    assert report["replyRate"] == "50%"
    assert report["linkedToApplied"] == 2
    assert report["linkedReplied"] == 1
    # 入站线索读数（2026-10-16）：未关联＝归不到我们的投递记录；其中末条是对方发言＝在等回话
    assert report["unlinkedConversations"] == 2
    assert report["unlinkedReplied"] == 1
    assert report["generatedAt"] == "2026-09-06 12:00:00"


def test_build_report_unlinked_readings_are_always_present():
    """全关联场景下两键仍恒在（0 也写出来，口径同 #62 no_send_reasons）——旧报告读者按缺键
    回落，但新报告不许缺键。"""
    report = chat_mod.build_report([{"status": "awaiting", "appliedJob": "A"}], generated_at="2026-10-16 00:00:00")
    assert report["unlinkedConversations"] == 0 and report["unlinkedReplied"] == 0


# --- ChatFirstPlatform 纯函数：JD 去重收集（自 crawl_jds.py 平移） ---


def test_collect_new_jds_dedups_and_excludes():
    listing = [
        {"encryptJobId": "1", "jobName": "A"},
        {"encryptJobId": "2", "jobName": "B"},
        {"encryptJobId": None, "jobName": "C"},
        {"encryptJobId": "1", "jobName": "A2"},
    ]
    collected = [{"encryptJobId": "2"}]  # 已在本轮收过
    new = chat_mod.collect_new_jds(listing, collected, applied_ids={"9"})
    assert [j["encryptJobId"] for j in new] == ["1"], "排除已投递与本轮重复，跳过无 id"
    collected.extend(new)
    again = chat_mod.collect_new_jds(listing, collected, applied_ids={"1"})
    assert again == []


# --- issue #20：拟人浏览/打字/风控检测 ---


class _Rng:
    """确定性随机：uniform 取中值、randint 取中值、random 可配、choice 取首项。"""

    def __init__(self, random_val=0.5):
        self.random_val = random_val

    def uniform(self, lo, hi):
        return (lo + hi) / 2

    def randint(self, lo, hi):
        return (lo + hi) // 2

    def random(self):
        return self.random_val

    def choice(self, seq):
        return seq[0]


class _Keyboard:
    def __init__(self):
        self.typed = []
        self.pressed = []

    def type(self, text, delay=0):
        self.typed.append(text)

    def press(self, key):
        self.pressed.append(key)


class _Mouse:
    def __init__(self, wheel_raises=False):
        self.wheels = []
        self.clicks = []
        self._raises = wheel_raises

    def wheel(self, dx, dy):
        self.wheels.append((dx, dy))
        if self._raises:
            raise RuntimeError("boom")

    def click(self, x, y, **kw):
        self.clicks.append((x, y))


class _BoxEl:
    """coordinate_click 用：只有 bounding_box。"""

    def __init__(self, box):
        self._box = box

    def bounding_box(self):
        return self._box


class _ViewPage:
    """coordinate_click 用：evaluate 返回视口尺寸，mouse.click 可记录。"""

    def __init__(self, vw=1280, vh=800):
        self.mouse = _Mouse()
        self.evaluated = []
        self._view = {"w": vw, "h": vh}

    def evaluate(self, js, *args):
        self.evaluated.append(js)
        return self._view


class _El:
    def __init__(self, visible=True, click_raises=False):
        self._visible = visible
        self._click_raises = click_raises

    def is_visible(self):
        return self._visible

    def click(self, **kw):
        if self._click_raises:
            raise RuntimeError("click timeout")


class _FakePage:
    """最小 page 替身：query_selector 按映射返回；记录 goto/evaluate/keyboard/mouse。"""

    def __init__(self, sel_map=None, evaluate_raises=False):
        self.keyboard = _Keyboard()
        self.mouse = _Mouse()
        self._sel = sel_map or {}
        self.gotos = []
        self.went_back = False
        self.evaluated = []
        self._evaluate_raises = evaluate_raises

    def query_selector(self, sel):
        return self._sel.get(sel)

    def evaluate(self, js, *args):
        self.evaluated.append(js)
        if self._evaluate_raises:
            raise RuntimeError("boom")

    def goto(self, url, **kw):
        self.gotos.append(url)

    def go_back(self, **kw):
        self.went_back = True


def test_detect_risk_signal_selector_hit():
    page = _FakePage({"[class*='captcha']": _El()})
    sig = chat_mod.detect_risk_signal(page)
    assert sig and "风控" in sig


def test_detect_risk_signal_text_hit():
    page = _FakePage({"text=安全验证": _El()})
    sig = chat_mod.detect_risk_signal(page)
    assert sig and "安全验证" in sig


def test_coordinate_click_hits_center_and_skips_out_of_viewport():
    """#44：坐标点击打元素中心；视口外一律不点（会误点别处）——交调用方兜底。"""
    el = _BoxEl({"x": 300, "y": 200, "width": 100, "height": 40})
    page = _ViewPage()
    assert chat_mod.coordinate_click(page, el) is True
    assert page.mouse.clicks == [(350.0, 220.0)], "坐标点击落在元素中心"

    off = _BoxEl({"x": -500, "y": 200, "width": 100, "height": 40})
    page2 = _ViewPage()
    assert chat_mod.coordinate_click(page2, off) is False, "视口外宁可不点"
    assert page2.mouse.clicks == []


def test_detect_risk_signal_none_and_hidden_element():
    assert chat_mod.detect_risk_signal(_FakePage()) is None
    page = _FakePage({".nc-container": _El(visible=False)})
    assert chat_mod.detect_risk_signal(page) is None, "隐藏元素不算风控信号"


def test_human_read_scrolls_and_swallows_errors(monkeypatch):
    monkeypatch.setattr(chat_mod.time, "sleep", lambda *_: None)
    page = _FakePage()
    chat_mod.human_read(page, lo=0, hi=0, rng=_Rng())  # n_scroll=randint(1,3) 中值=2
    # #44 反侦察：一段滚动拆 2~4 个小步 ⇒ 2 次滚动 = 4~8 次 evaluate scrollBy（非 wheel）
    assert 4 <= len(page.evaluated) <= 8, "停留期间 2 次滚动，每次拆 2-4 小步"
    assert all("scrollBy" in js for js in page.evaluated)
    bad = _FakePage(evaluate_raises=True)
    chat_mod.human_read(bad, lo=0, hi=0, rng=_Rng())  # 滚动异常不抛出


def test_human_type_char_by_char_no_typo():
    page = _FakePage()
    n, typos = chat_mod.human_type(page, "您好", delay_lo=1, delay_hi=2, typo_prob=0.0, rng=_Rng())
    assert page.keyboard.typed == ["您", "好"], "逐字键入，非整段 fill"
    assert page.keyboard.pressed == []
    assert (n, typos) == (2, 0)


def test_human_type_typo_then_backspace_once():
    page = _FakePage()
    n, typos = chat_mod.human_type(page, "好", delay_lo=1, delay_hi=2, typo_prob=1.0, rng=_Rng(random_val=0.0))
    assert typos == 1 and n == 1
    assert page.keyboard.pressed == ["Backspace"], "错字后退格修正"
    assert page.keyboard.typed[-1] == "好", "退格后补上正确字"
    assert len(page.keyboard.typed) == 2, "错字 1 次 + 正字 1 次"


def test_human_type_typo_at_most_once(monkeypatch):
    monkeypatch.setattr(chat_mod.time, "sleep", lambda *_: None)
    page = _FakePage()
    _, typos = chat_mod.human_type(page, "甲乙丙", typo_prob=1.0, rng=_Rng(random_val=0.0))
    assert typos == 1, "一条消息最多 1 次错字（避免过度拟真反而显眼）"


def test_search_via_input_types_and_enters():
    page = _FakePage({"input[placeholder*='搜索']": _El()})
    assert chat_mod.search_via_input(page, "Solidity", rng=_Rng()) is True
    assert page.keyboard.typed == ["Solidity"]
    assert page.keyboard.pressed == ["Enter"]


def test_search_via_input_no_box_returns_false():
    assert chat_mod.search_via_input(_FakePage(), "Solidity", rng=_Rng()) is False


def test_search_via_input_hidden_box_skips_to_fallback():
    page = _FakePage({"input[placeholder*='搜索']": _El(visible=False)})
    assert chat_mod.search_via_input(page, "Solidity", rng=_Rng()) is False
    assert page.keyboard.typed == [], "隐藏输入框不键入"


# --- issue #36：沟通按钮查找（专属 class 优先 + 文本约束回退 + 可见性过滤） ---


class _CommEl:
    """按钮替身：可见性 + inner_text + scroll 记录。"""

    def __init__(self, text="立即沟通", visible=True, scroll_raises=False):
        self._text = text
        self._visible = visible
        self._scroll_raises = scroll_raises
        self.scrolled = False

    def is_visible(self):
        return self._visible

    def inner_text(self):
        return self._text

    def scroll_into_view_if_needed(self, **kw):
        if self._scroll_raises:
            raise RuntimeError("scroll timeout")
        self.scrolled = True


class _CommPage:
    """query_selector / query_selector_all 均按映射返回（值可为 list 或单元素）。"""

    def __init__(self, sel_map=None):
        self._sel = sel_map or {}

    def query_selector(self, sel):
        return self._sel.get(sel)

    def query_selector_all(self, sel):
        v = self._sel.get(sel, [])
        return v if isinstance(v, list) else [v]

    def evaluate(self, js, *args):
        # find_comm_button 现走页面内 JS scrollIntoView（#44：Juggler 的
        # scroll_into_view_if_needed 会无超时挂起，2026-09-24 改）；按真页面语义
        # 把滚动记到元素上、_scroll_raises 时抛异常（验证不阻断查找）。
        el = args[0] if args else None
        if el is None:
            return None
        if el._scroll_raises:
            raise RuntimeError("scroll timeout")
        el.scrolled = True
        return None


def test_find_comm_button_prefers_startchat_class():
    btn = _CommEl()
    page = _CommPage({".job-op .btn-startchat": [btn]})
    assert chat_mod.find_comm_button(page) is btn
    assert btn.scrolled, "点击前页面内 JS scrollIntoView 前置（#44：不用 Juggler 版）"


def test_find_comm_button_class_wildcard_fallback():
    btn = _CommEl()
    page = _CommPage({"[class*='btn-startchat']": [btn]})
    assert chat_mod.find_comm_button(page) is btn


def test_find_comm_button_falls_back_to_button_text():
    btn = _CommEl()
    page = _CommPage({"button": [btn], "a.btn": []})
    assert chat_mod.find_comm_button(page) is btn


def test_find_comm_button_skips_hidden_and_continue():
    cont = _CommEl(text="继续沟通")
    hidden = _CommEl(visible=False)
    ok = _CommEl()
    page = _CommPage({".job-op .btn-startchat": [cont], "button": [hidden, ok]})
    assert chat_mod.find_comm_button(page) is ok, "「继续沟通」是已投递信号；隐藏按钮不可点"


def test_find_comm_button_skips_blank_text_class_hit():
    blank = _CommEl(text="  \n ")
    ok = _CommEl()
    page = _CommPage({".job-op .btn-startchat": [blank], "button": [ok]})
    assert chat_mod.find_comm_button(page) is ok, "class 命中的空文本节点不可点"


def test_find_continue_button_class_constrained():
    cont = _CommEl()
    page = _CommPage({"[class*='btn-startchat']:has-text('继续沟通')": cont})
    assert chat_mod.find_continue_button(page) is cont
    assert chat_mod.find_continue_button(_CommPage()) is None


def test_find_comm_button_scroll_failure_not_blocking():
    btn = _CommEl(scroll_raises=True)
    page = _CommPage({".job-op .btn-startchat": [btn]})
    assert chat_mod.find_comm_button(page) is btn, "滚动失败不阻断查找"


def test_find_comm_button_absent_returns_none():
    assert chat_mod.find_comm_button(_CommPage()) is None


# --- #43：save_crash_evidence 三件套落盘（str 路径调 .write_text 曾让 html/url.txt 必失败） ---


class _CrashPage:
    """存证替身：screenshot/content/url 最小面。"""

    def __init__(
        self,
        url="https://www.zhipin.com/job_detail/e1.html",
        content_raises=False,
        screenshot_raises=False,
        url_raises=False,
    ):
        self._url = url
        self._content_raises = content_raises
        self._screenshot_raises = screenshot_raises
        self._url_raises = url_raises

    @property
    def url(self):
        if self._url_raises:
            raise RuntimeError("target closed")
        return self._url

    def screenshot(self, **kw):
        if self._screenshot_raises:
            raise RuntimeError("screenshot: target closed")
        Path(kw["path"]).write_bytes(b"png-bytes")

    def content(self):
        if self._content_raises:
            raise RuntimeError("content timeout")
        return "<html><body>崩溃现场</body></html>"


def test_save_crash_evidence_writes_all_three_files(tmp_path, monkeypatch):
    """#43 根因：`(str(prefix) + ".html").write_text(...)` 对 str 调 Path 方法必抛
    AttributeError 且被吞——crash 目录 11 天 0 个 html/url.txt。修复后三件齐落。"""
    monkeypatch.setattr(chat_mod, "CRASH_DIR", tmp_path)
    page = _CrashPage()
    prefix = chat_mod.save_crash_evidence(page, "no-input", {"brandName": "某基金"})
    assert prefix is not None
    for ext in (".png", ".html", ".url.txt"):
        assert Path(str(prefix) + ext).exists(), f"{ext} 未落盘（#43：html/url.txt 曾必失败）"
    assert Path(str(prefix) + ".html").read_text(encoding="utf-8") == "<html><body>崩溃现场</body></html>"
    assert Path(str(prefix) + ".url.txt").read_text(encoding="utf-8") == page.url


def test_save_crash_evidence_failure_isolation(tmp_path, monkeypatch):
    """单件失败不炸整体：content() 抛异常时 PNG/url.txt 照落，且不向调用方抛出
    （观测层不阻断投递主链路，#43 triage 拍板）。"""
    monkeypatch.setattr(chat_mod, "CRASH_DIR", tmp_path)
    page = _CrashPage(content_raises=True)
    prefix = chat_mod.save_crash_evidence(page, "no-btn", {"brandName": "某基金"})
    assert prefix is not None
    assert Path(str(prefix) + ".png").exists()
    assert Path(str(prefix) + ".url.txt").exists()
    assert not Path(str(prefix) + ".html").exists()


# --- 20260926 实测：浏览器被外部终结时截图/HTML 全失败，日志仍打「现场已存 .png/.html」谎报
# （#43 red 阶段已观察到该谎报，当时范围只限写盘根因）——消息必须逐件列出实际落盘文件 ---


def test_save_crash_evidence_all_failed_reports_truthfully(tmp_path, monkeypatch, capsys):
    """截图/HTML 全失败、仅 URL 可读：只落 url.txt，消息不得谎报 png/html。"""
    monkeypatch.setattr(chat_mod, "CRASH_DIR", tmp_path)
    page = _CrashPage(
        url="https://www.zhipin.com/job_detail/e1",  # URL 不带 .html，避免污染断言
        screenshot_raises=True,
        content_raises=True,
    )
    prefix = chat_mod.save_crash_evidence(page, "page_error", {"brandName": "某基金"})
    out = capsys.readouterr().out
    assert prefix is not None
    assert Path(str(prefix) + ".url.txt").exists()
    assert "现场已存 1 件" in out and ".url.txt" in out
    assert ".png" not in out and ".html" not in out


def test_save_crash_evidence_message_lists_saved_files(tmp_path, monkeypatch, capsys):
    """成功路径：消息逐件列出实际落盘文件，替代「.png/.html」缩写。"""
    monkeypatch.setattr(chat_mod, "CRASH_DIR", tmp_path)
    page = _CrashPage()
    prefix = chat_mod.save_crash_evidence(page, "no-input", {"brandName": "某基金"})
    out = capsys.readouterr().out
    for ext in (".png", ".html", ".url.txt"):
        assert str(prefix) + ext in out


def test_save_crash_evidence_nothing_saved_returns_none(tmp_path, monkeypatch, capsys):
    """连 URL 都读不出（page.url 抛）：零落盘返回 None，消息如实报未存档。"""
    monkeypatch.setattr(chat_mod, "CRASH_DIR", tmp_path)
    page = _CrashPage(screenshot_raises=True, content_raises=True, url_raises=True)
    assert chat_mod.save_crash_evidence(page, "page_error", {"brandName": "某基金"}) is None
    out = capsys.readouterr().out
    assert "未存档" in out


# --- #56 A3：细颗粒节拍（beat_fn 注入点，使「停在哪个协议调用」无需栈即可判定）---
#
# 票面机制口径 §1：querySelector / querySelectorAll / boundingBox / evaluateExpression /
# mouse* / keyboard* 在 playwright 1.60 里全是 timeout=None 的裸奔调用。5 场楔死的最后一拍
# 都停在岗头与 detail-open 之间，答不出停在哪一个调用上——所以这些调用前后各拍一次点，
# label 里显式写 pre/post：只有 pre = 进了这个调用没出来；pre+post = 出来了，死在下一步。
# 平台层只收一个可调用的 beat_fn（默认 None = 零行为变更），不知道心跳为何物。


class _BeatRecorder:
    """beat_fn 替身：按顺序记录 label（楔死现场读的正是这个顺序）。"""

    def __init__(self):
        self.labels = []

    def __call__(self, label):
        self.labels.append(label)


class _BeatMouse:
    def __init__(self):
        self.moves = []
        self.clicks = []

    def move(self, x, y):
        self.moves.append((x, y))

    def click(self, x, y, **kw):
        self.clicks.append((x, y))


class _BeatPage:
    """节拍用例的 page 替身：evaluate / wait_for_timeout / mouse 全可观察。
    `on_wait` 钩子在第 N 次等待时改变条件（模拟详情响应到达），免得测试真等 15s。"""

    def __init__(self, evaluate_seq=None, view=None, on_wait=None):
        self.mouse = _BeatMouse()
        self.evaluated = []
        self.waits = []
        self._seq = list(evaluate_seq or [])
        self._view = view if view is not None else {"w": 1280, "h": 800}
        self._on_wait = on_wait

    def evaluate(self, js, *args):
        self.evaluated.append(js)
        return self._seq.pop(0) if self._seq else self._view

    def wait_for_timeout(self, ms):
        self.waits.append(ms)
        if self._on_wait:
            self._on_wait(len(self.waits), ms)

    def query_selector_all(self, sel):
        return []


class _BeatEl:
    """元素替身：bounding_box 可编排（None = 拿不到几何），click 记录 kwargs。"""

    def __init__(self, box=None, click_raises=False):
        self._box = box
        self._click_raises = click_raises
        self.clicks = []

    def bounding_box(self):
        return self._box

    def click(self, **kw):
        self.clicks.append(kw)
        if self._click_raises:
            raise RuntimeError("Timeout 10000ms exceeded.")


class _FakeClock:
    """替换 chat_mod 命名空间里的 time 名字（不动 stdlib time 模块本身）：
    `time()` 每次前进 step 秒、`sleep()` 只记录。grab_detail 的 5s/15s 轮询与
    human_pointer_walk 的 40-120ms 停顿都不该在测试里真等。"""

    def __init__(self, start=0.0, step=0.0):
        self.now = start
        self.step = step
        self.slept = []

    def time(self):
        v = self.now
        self.now += self.step
        return v

    def sleep(self, seconds):
        self.slept.append(seconds)


def test_human_pointer_walk_beats_around_bounding_box_and_each_move(monkeypatch):
    """#56 验收 1：`bounding_box()` 与**每一次** `mouse.move()` 前后各一拍（票面点名的位点）。
    步数由 rng 决定，故断言「成对 + 从 1 递增 + 末尾那次瞄准落点」，不锁死步数。"""
    page, el, beat = _BeatPage(), _BeatEl(box={"x": 100, "y": 100, "width": 40, "height": 20}), _BeatRecorder()
    monkeypatch.setattr(chat_mod, "time", _FakeClock())
    chat_mod.human_pointer_walk(page, el, rng=random.Random(7), beat_fn=beat)
    assert beat.labels[:2] == ["pointer:bounding-box:pre", "pointer:bounding-box:post"]
    moves = [lbl for lbl in beat.labels if lbl.startswith("pointer:mouse-move:")]
    assert len(moves) == 2 * len(page.mouse.moves), "每个 mouse.move 前后各一拍（成对）"
    pres = [lbl for lbl in moves if lbl.endswith(":pre")]
    assert pres == [f"pointer:mouse-move:{i}:pre" for i in range(1, len(pres))] + ["pointer:mouse-move:aim:pre"], (
        "分段轨迹按轮次编号，落点那一次单独可辨"
    )


def test_human_pointer_walk_beats_stop_at_the_call_that_hung(monkeypatch):
    """楔死现场读法（A3 的判据本体）：bounding_box 不返回时最后一条节拍是
    `pointer:bounding-box:pre` 而不是 post——没有栈也能指认停在哪个调用上。
    #56 修复轮 F4①补一条 `pointer:error` 把它与「抛了异常被吞掉」分开：本用例用的是
    「异常代替楔死」的替身，所以两条都在；真楔死时 error 拍点也出不来（进程停在那里），
    「只有 pre、既无 post 也无 error」才是楔死指纹。"""

    class _HangEl:
        def bounding_box(self):
            raise RuntimeError("never returns（本用例用异常代替楔死，机制同一条 label）")

    beat = _BeatRecorder()
    chat_mod.human_pointer_walk(_BeatPage(), _HangEl(), rng=random.Random(1), beat_fn=beat)
    assert beat.labels == ["pointer:bounding-box:pre", "pointer:error"], "没出来就不拍 post"


def test_coordinate_click_beats_every_bare_call_in_order(monkeypatch):
    """坐标点击里两个 `page.evaluate`（scrollIntoView / innerWidth）都是 evaluateExpression
    的裸奔调用，加上 bounding_box 与 mouse.click——整段顺序必须完全可读。"""
    el = _BeatEl(box={"x": 300, "y": 200, "width": 100, "height": 40})
    beat = _BeatRecorder()
    assert chat_mod.coordinate_click(_BeatPage(), el, beat_fn=beat) is True
    assert beat.labels == [
        "click:evaluate-scroll:pre",
        "click:evaluate-scroll:post",
        "click:bounding-box:pre",
        "click:bounding-box:post",
        "click:evaluate-viewport:pre",
        "click:evaluate-viewport:post",
        "click:mouse-click:pre",
        "click:mouse-click:post",
    ]


def test_coordinate_click_does_not_beat_calls_it_never_makes():
    """视口外宁可不点：`click:mouse-click:pre` 不得出现——节拍跟着实际控制流走，
    不是无条件成对，否则 label 流会谎报「进了那个调用」。"""
    beat = _BeatRecorder()
    off = _BeatEl(box={"x": -500, "y": 200, "width": 100, "height": 40})
    assert chat_mod.coordinate_click(_BeatPage(), off, beat_fn=beat) is False
    assert beat.labels[-1] == "click:evaluate-viewport:post"
    assert "click:mouse-click:pre" not in beat.labels


def test_click_with_retry_beats_cover_the_whole_retry_ladder():
    """重试阶梯（坐标 → handle click → 关遮罩 → force）逐步有拍：阶梯本身就是
    「同一段动作里的多个停摆窗口」，最后一条 label 要能指到具体哪一步。
    三次 handle.click 全抛 ⇒ 三处都是「pre + error、无 post」（修复轮 F4①之后 error 收尾）。"""
    page, beat = _BeatPage(), _BeatRecorder()
    el = _BeatEl(box=None, click_raises=True)  # 无几何 ⇒ 坐标段直接 False；handle 三次全抛
    assert chat_mod.click_with_retry(page, el, action="立即沟通按钮", beat_fn=beat) is False
    for lbl in (
        "click:handle:pre",
        "click:dismiss-overlays:pre",
        "click:dismiss-overlays:post",
        "click:handle-retry:pre",
        "click:handle-force:pre",
    ):
        assert lbl in beat.labels, f"阶梯缺拍点 {lbl}"
    assert beat.labels[-1] == "click:handle-force:error", "失败出口停在最后一次点击的异常出口，不谎报返回"
    for not_run in ("click:handle:post", "click:handle-retry:post", "click:handle-force:post"):
        assert not_run not in beat.labels


def test_click_with_retry_passes_the_beat_down_to_its_helpers(monkeypatch):
    """click_with_retry 自己只懂动作、不懂心跳：它把同一个 beat_fn 透传给
    human_pointer_walk 与 coordinate_click，否则下层裸奔调用点仍然无名。"""
    monkeypatch.setattr(chat_mod, "time", _FakeClock())  # 轨迹里的 40-120ms 停顿不真等
    beat = _BeatRecorder()
    el = _BeatEl(box={"x": 300, "y": 200, "width": 100, "height": 40})
    chat_mod.click_with_retry(_BeatPage(), el, action="职位卡片", beat_fn=beat)
    assert "pointer:bounding-box:pre" in beat.labels, "指针轨迹段的拍必须由上层透下来"
    assert "click:mouse-click:pre" in beat.labels, "坐标点击段的拍同理"


def test_grab_detail_beats_dom_poll_rounds(monkeypatch):
    """#56 验收 1：DOM 轮询的 `page.evaluate`（evaluateExpression 裸奔）与随后的
    `wait_for_timeout` 逐轮有拍，轮次编号进 label——「死在第几轮」直接读得出来。"""
    page = _BeatPage(evaluate_seq=["", "JD 全文：负责测试"])
    beat = _BeatRecorder()
    monkeypatch.setattr(chat_mod, "time", _FakeClock(step=0.5))
    got = chat_mod.grab_detail(page, {"data": None}, beat_fn=beat)
    assert got == "JD 全文：负责测试"
    assert beat.labels == [
        "jd:dom-poll:1:pre",
        "jd:dom-poll:1:post",
        "jd:dom-wait:1:pre",
        "jd:dom-wait:1:post",
        "jd:dom-poll:2:pre",
        "jd:dom-poll:2:post",
    ]


def test_grab_detail_beats_xhr_wait_rounds(monkeypatch):
    """DOM 全落空后转 XHR 空等（票面点名的同族站点）：那一段每轮一拍，
    并在详情响应到达后收口——楔死在这里时最后一条 label 是 `jd:xhr-wait:N:*`。"""
    detail = {"data": None}

    def arrive(n, ms):
        if ms == 300:  # 空等段第一次等待后响应到达，测试不必真等 15s
            detail["data"] = {"zpData": {"jobInfo": {"postDescription": "JD：XHR 兜底"}}}

    page = _BeatPage(evaluate_seq=["", "", "", ""], on_wait=arrive)
    beat = _BeatRecorder()
    monkeypatch.setattr(chat_mod, "time", _FakeClock(step=1.0))
    assert chat_mod.grab_detail(page, detail, beat_fn=beat) == "JD：XHR 兜底"
    assert beat.labels[:4] == [
        "jd:dom-poll:1:pre",
        "jd:dom-poll:1:post",
        "jd:dom-wait:1:pre",
        "jd:dom-wait:1:post",
    ]
    assert beat.labels[-2:] == ["jd:xhr-wait:1:pre", "jd:xhr-wait:1:post"]
    assert "jd:dom-poll:4:post" in beat.labels, "5s 预算按 1s/轮 = 4 轮 DOM 轮询"


# --- #56 修复轮 F4①：吞掉异常的出口要有独立拍点 ---
#
# 「pre 有 post 无」过去同时表示两件事：① 卡在这个调用里（楔死，C 段要判栈的那一种）；
# ② 这个调用抛了异常、走了吞掉异常的出口（普通失败，与楔死无关）。判据二义会让 C 段把
# 一次正常的 handle click 超时读成「停在无超时调用上」。修法：在吞异常的出口补一条
# 同命名空间的 `*:error` 拍点——**返回值与控制流一字不动**（拍点异常由注入方吞）。


def test_human_pointer_walk_beats_its_swallowed_exception(monkeypatch):
    """`human_pointer_walk` 整段包在 try/except 里（轨迹属锦上添花，任何失败都让位给正常点击）：
    旧写法下它抛异常与它楔住留下的 label 完全一样。"""

    class _BoomEl:
        def bounding_box(self):
            raise RuntimeError("Timeout 30000ms exceeded")

    beat = _BeatRecorder()
    monkeypatch.setattr(chat_mod, "time", _FakeClock())
    assert chat_mod.human_pointer_walk(_BeatPage(), _BoomEl(), rng=random.Random(1), beat_fn=beat) is None
    assert beat.labels == ["pointer:bounding-box:pre", "pointer:error"], "pre + error＝抛异常，不是没出来"
    assert "pointer:bounding-box:post" not in beat.labels


def test_coordinate_click_beats_both_error_exits():
    """`coordinate_click` 的两个 except 出口（滚动 evaluate 吞掉后继续 / 整段异常返回 False）
    各留一条 label；bail（无 box、视口外）不是异常，不得冒充 error 拍点。"""

    class _EvalBoomPage(_BeatPage):
        def evaluate(self, js, *args):
            self.evaluated.append(js)
            raise RuntimeError("evaluateExpression never returns / blows up")

    beat = _BeatRecorder()
    el = _BeatEl(box={"x": 300, "y": 200, "width": 100, "height": 40})
    assert chat_mod.coordinate_click(_EvalBoomPage(), el, beat_fn=beat) is False
    assert beat.labels == [
        "click:evaluate-scroll:pre",
        "click:evaluate-scroll:error",
        "click:evaluate-scroll:post",
        "click:bounding-box:pre",
        "click:bounding-box:post",
        "click:evaluate-viewport:pre",
        "click:error",
    ], "滚动那次异常吞掉后照原样继续，视口那次异常从外层出口返回 False"
    assert "click:mouse-click:pre" not in beat.labels, "异常出口之后不得再拍后续调用（节拍跟着实际控制流）"


def test_coordinate_click_bail_paths_emit_no_error_beat():
    """无 box / 视口外是**正常 bail**（返回 False），不是吞掉的异常：不得落 error 拍点，
    否则 C 段会把「宁可不点」读成一次协议故障。"""
    beat = _BeatRecorder()
    assert chat_mod.coordinate_click(_BeatPage(), _BeatEl(box=None), beat_fn=beat) is False
    assert beat.labels == [
        "click:evaluate-scroll:pre",
        "click:evaluate-scroll:post",
        "click:bounding-box:pre",
        "click:bounding-box:post",
    ]
    assert not [lbl for lbl in beat.labels if lbl.endswith(":error")]


def test_error_post_docstrings_match_the_pinned_sequences():
    """修复轮 4（D）：error/post 的边界以实测为准（两条序列分别由
    `test_human_pointer_walk_beats_its_swallowed_exception` 与
    `test_coordinate_click_beats_both_error_exits` 钉死），两处 docstring 各自写明
    「覆盖到哪、post 证明什么」——`human_pointer_walk` 的「异常出口不拍 post」只限本函数，
    `coordinate_click` 的 scroll evaluate 异常出口 error 后 post 照落。这条文档契约防止
    再把一处的结论外推到另一处（旧注释正是这么被读错的）。"""
    doc_walk = chat_mod.human_pointer_walk.__doc__ or ""
    doc_click = chat_mod.coordinate_click.__doc__ or ""
    assert "本函数内异常出口不拍 post" in doc_walk, "边界必须显式限定在本函数（post＝该调用正常返回）"
    assert "别外推" in doc_walk, "不得被读成同族函数的通用规则"
    assert "三拍全落" in doc_click, "scroll evaluate 的异常出口序列按实测写明（pre+error+post）"
    assert "不证明 evaluate 成功" in doc_click, "post 的真实含义要写明：返回了 ≠ 成功了"


def test_click_with_retry_beats_its_three_handle_error_exits(monkeypatch):
    """handle click 族的三个 except 出口（首点 / 关遮罩后重试 / force）都要有独立 error 拍点：
    这三步带 timeout，异常是它们的**正常出口**（点不动就下一步），旧写法只留一个 pre。
    阶梯顺序与返回值不变——最后一条 label 现在指向失败出口本身。"""
    monkeypatch.setattr(chat_mod, "time", _FakeClock())
    beat = _BeatRecorder()
    el = _BeatEl(box=None, click_raises=True)
    assert chat_mod.click_with_retry(_BeatPage(), el, action="立即沟通按钮", beat_fn=beat) is False
    for lbl in ("click:handle:error", "click:handle-retry:error", "click:handle-force:error"):
        assert lbl in beat.labels, f"异常出口缺拍点 {lbl}"
    assert beat.labels[-1] == "click:handle-force:error"
    assert beat.labels.count("click:handle:post") == 0


def test_click_with_retry_success_paths_emit_no_error_beat(monkeypatch):
    """成功路径零 error 拍点（取证不谎报）：坐标点击点中就直接返回 True。"""
    monkeypatch.setattr(chat_mod, "time", _FakeClock())
    beat = _BeatRecorder()
    el = _BeatEl(box={"x": 300, "y": 200, "width": 100, "height": 40})
    assert chat_mod.click_with_retry(_BeatPage(), el, action="职位卡片", beat_fn=beat) is True
    assert not [lbl for lbl in beat.labels if lbl.endswith(":error")]


def test_dismiss_overlays_swallows_internally_so_its_beat_has_no_error_exit(monkeypatch):
    """#56 修复轮 3 R4（注释口径的实测锚）：`dismiss_overlays` 里每个选择器各自包在
    try/except 里，异常**在它自己那一层就被吞掉**、控制流回到 for——`click_with_retry`
    因此永远看不到异常，`click:dismiss-overlays:pre` 也就**没有**配套的 `:error` 拍点
    （给它加拍点会改动与本票无关的遮罩层语义，本轮明确不做）。

    于是本层的出口分两类，C 段判栈时按这个读：
    · 有 `*:error` 的三个 handle click（首点 / 关遮罩后重试 / force）——「抛异常」与
      「调用没回来」读得出来；
    · 没有 `*:error` 的 `dismiss-overlays` 这一段：`pre`+`post` 只证明「走完了」，**不证明**
      关掉过任何遮罩（内部异常照样走完）；`pre` 无 `post` 只剩两种可能——卡在里面的
      裸奔 `querySelectorAll` 上（楔死指纹），或 BaseException（如 KeyboardInterrupt）逸出。
    """
    monkeypatch.setattr(chat_mod, "time", _FakeClock())

    class _QueryBoomPage(_BeatPage):
        def query_selector_all(self, sel):
            raise RuntimeError("querySelectorAll 抛异常（本用例用异常代替「遮罩一个都没关掉」）")

    beat = _BeatRecorder()
    el = _BeatEl(box=None, click_raises=True)
    assert chat_mod.click_with_retry(_QueryBoomPage(), el, action="立即沟通按钮", beat_fn=beat) is False
    assert "click:dismiss-overlays:pre" in beat.labels and "click:dismiss-overlays:post" in beat.labels, (
        "内部异常不改变上层控制流：pre 之后仍照常 post"
    )
    assert not [lbl for lbl in beat.labels if "dismiss-overlays" in lbl and lbl.endswith(":error")], (
        "已知缺口（不是漏接）：遮罩层没有 error 拍点，见 docstring 的两类出口"
    )
    assert chat_mod.dismiss_overlays(_QueryBoomPage()) == 0, "异常在内部吞掉：返回 0、不外抛"


# --- #56 修复轮 F4②：A3 漏接的「同族无超时站点」---


class _FindPage:
    """查找段替身：`query_selector_all` 恒空、`query_selector` 恒 None（全部选择器落空），
    并记录询问顺序——接拍点时「不新增重试、不改查找顺序」就靠这两条断言把住。"""

    def __init__(self):
        self.asks = []

    def query_selector_all(self, sel):
        self.asks.append(sel)
        return []

    def query_selector(self, sel):
        self.asks.append(sel)
        return None


def test_find_comm_button_beats_each_query_selector_all():
    """`find_comm_button` 的 `query_selector_all` 是 timeout=None 的裸奔调用，A3 段里
    查找前一个拍都没有——楔死在找按钮这一段时，最后一条 label 只能停在上一段的 post。"""
    page, beat = _FindPage(), _BeatRecorder()
    assert chat_mod.find_comm_button(page, beat_fn=beat) is None
    pres = [lbl for lbl in beat.labels if lbl.endswith(":pre")]
    posts = [lbl for lbl in beat.labels if lbl.endswith(":post")]
    assert len(pres) == len(posts) == len(page.asks) == len(chat_mod.COMM_BTN_SELECTORS) + 2, (
        "每个选择器一次前后拍：顺序与次数都照原样（不新增重试）"
    )
    assert pres[0] == "find:comm-btn:query:1:pre" and pres[-1] == f"find:comm-btn:query:{len(pres)}:pre"
    assert page.asks == chat_mod.COMM_BTN_SELECTORS + ["button", "a.btn"], "查找顺序一字未动"


def test_find_comm_button_default_beat_fn_is_noop():
    """默认 None = 零行为变更（A3 的另一半保证）：不注入节拍时照常查完全部选择器、一个拍不发。"""
    page = _FindPage()
    assert chat_mod.find_comm_button(page) is None
    assert len(page.asks) == len(chat_mod.COMM_BTN_SELECTORS) + 2


def test_find_comm_button_beats_the_swallowed_query_exception():
    """修复轮 4（A5）：外层 `except: continue` 的出口补 `find:comm-btn:query:{n}:error`——
    补点前「pre 有 post 无」同时表示「卡在这个调用里」与「这个调用抛了异常被吞」，
    C 段判栈会二义。异常不改变查找顺序与次数（照原样继续下一个选择器）。"""

    class _FirstBoomPage(_FindPage):
        def query_selector_all(self, sel):
            self.asks.append(sel)
            if len(self.asks) == 1:
                raise RuntimeError("querySelectorAll 挂了")
            return []

    page, beat = _FirstBoomPage(), _BeatRecorder()
    assert chat_mod.find_comm_button(page, beat_fn=beat) is None, "异常被吞：返回值仍是 None"
    assert beat.labels[0] == "find:comm-btn:query:1:pre"
    assert "find:comm-btn:query:1:error" in beat.labels
    assert "find:comm-btn:query:1:post" not in beat.labels, "抛出后该调用没有 post"
    assert len(page.asks) == len(chat_mod.COMM_BTN_SELECTORS) + 2, "异常不改变查找顺序与次数"


class _FindHitPage(_FindPage):
    """命中替身：第一个选择器返回一个可见按钮 → 触发逐元素循环里那次 scrollIntoView。"""

    def __init__(self, scroll_boom=False):
        super().__init__()
        self.scroll_boom = scroll_boom

    def query_selector_all(self, sel):
        self.asks.append(sel)
        return [self._Btn()] if len(self.asks) == 1 else []

    def evaluate(self, js, *args):
        if self.scroll_boom:
            raise RuntimeError("evaluateExpression 挂了")
        return None

    class _Btn:
        def is_visible(self):
            return True

        def inner_text(self):
            return "立即沟通"


def test_find_comm_button_beats_the_scroll_evaluate():
    """修复轮 4（A6，票面 §1）：命中后那次 `page.evaluate(scrollIntoView)` 是
    evaluateExpressionHandle 同族（coordinate_click 同型调用已拍点），此处补 pre/post/error——
    正常出口 pre+post；异常出口 error 也落（滚动失败原本就吞掉继续），post＝这段执行完了。"""
    page, beat = _FindHitPage(), _BeatRecorder()
    assert chat_mod.find_comm_button(page, beat_fn=beat) is not None
    assert beat.labels == [
        "find:comm-btn:query:1:pre",
        "find:comm-btn:query:1:post",
        "find:comm-btn:scroll:1:pre",
        "find:comm-btn:scroll:1:post",
    ]
    page, beat = _FindHitPage(scroll_boom=True), _BeatRecorder()
    assert chat_mod.find_comm_button(page, beat_fn=beat) is not None, "滚动失败不阻断（异常在内部吞掉）"
    assert beat.labels == [
        "find:comm-btn:query:1:pre",
        "find:comm-btn:query:1:post",
        "find:comm-btn:scroll:1:pre",
        "find:comm-btn:scroll:1:error",
        "find:comm-btn:scroll:1:post",
    ]


def test_platform_beat_sites_are_silent_without_beat_fn(monkeypatch):
    """默认 None = 零行为变更（A3 的另一半保证）：平台层不注入时照常动作、一个拍都不发。
    chat_first.py 是平台层，本就不该知道心跳存在——节拍由调用方（live_apply）注入。"""
    clock = _FakeClock()
    monkeypatch.setattr(chat_mod, "time", clock)
    page = _BeatPage()
    el = _BeatEl(box={"x": 300, "y": 200, "width": 100, "height": 40})
    assert chat_mod.human_pointer_walk(page, el, rng=random.Random(3)) is None
    assert page.mouse.moves, "不注入节拍时指针轨迹照常走"
    assert chat_mod.coordinate_click(page, el) is True
    assert page.mouse.clicks == [(350.0, 220.0)]
    assert chat_mod.grab_detail(_BeatPage(evaluate_seq=["JD"]), {"data": None}) == "JD"
    assert clock.slept, "轨迹内的 time.sleep 未被跳过（节拍不改变动作节奏）"


# --- #56 A5①：grab_detail 落空出口的可观测化（一次性选择器命中探测 + 等待时长进容器）---
#
# 失败基线（改前实测，`.venv/bin/python` 直接跑现版 grab_detail，DOM 全落空 + XHR 空等）：
#   返回 '' / 真实耗时 20.05s（5s DOM 轮询 + 15s XHR 空等）/ 容器原样 {'data': None}
#   —— 什么都没落：既没有「等了多久」，也没有「选择器命中数」，no_jd 因此完全不可归因。
# 本段锁的就是这一处：改后同一出口必须把四件事里的两件（等了多久、DOM 命中数）连同
# 岗位详情页 URL 落进容器，且**返回类型仍是 str**（空串＝没拿到，契约一字未动）。


class _JdPage:
    """grab_detail 专用页面替身：DOM 轮询恒落空，探测 JS 返回编排好的逐选择器命中数。
    以 JS 里有没有 `querySelectorAll` 区分「轮询」与「探测」——轮询 JS 只用 `querySelector`
    （A5 的探测是新增的第二段 JS，本用例同时钉住「探测只在失败出口跑一次」）。"""

    def __init__(self, probe=None, probe_raises=False, url="https://www.zhipin.com/job_detail/e1.html"):
        self.probe = probe if probe is not None else []
        self.probe_raises = probe_raises
        self.poll_calls = 0
        self.probe_calls = 0
        self.waits = []
        self.url = url

    def evaluate(self, js, *args):
        # 与 _BeatPage 同形态收下第二个位置参数（真实 page.evaluate 的 arg），不用它
        if "querySelectorAll" in js:
            self.probe_calls += 1
            if self.probe_raises:
                raise RuntimeError("Target page, context or browser has been closed")
            return self.probe
        self.poll_calls += 1
        return ""

    def wait_for_timeout(self, ms):
        self.waits.append(ms)


_PROBE_HIT = {"selector": ".job-sec-text", "hits": 2, "nonempty": 0}
_PROBE_MISS = {"selector": ".job-detail .text", "hits": 0, "nonempty": 0}


def test_grab_detail_empty_exit_lands_probe_evidence(monkeypatch):
    """#56 验收 1（A5 侧）：落空出口把「等了多久 + DOM 选择器命中数 + URL」写进容器，
    返回值仍是空串（合同不变）。轮询轮数与真实 evaluate 次数一一对得上——取证不能编数。"""
    monkeypatch.setattr(chat_mod, "time", _FakeClock(step=1.0))
    page = _JdPage(probe=[_PROBE_HIT, _PROBE_MISS])
    container = {"data": None}
    assert chat_mod.grab_detail(page, container) == "", "返回契约：仍是 str，空串=没拿到"
    out = container["empty_outcome"]
    assert out["url"] == page.url
    assert out["hit_total"] == 2 and out["nonempty_total"] == 0
    assert out["hit_selectors"] == [".job-sec-text"], "只报有命中的选择器（命中数为 0 的不进短名单）"
    assert out["selectors"] == [_PROBE_HIT, _PROBE_MISS], "逐选择器原样留存：形态漂移假说要靠这份对照"
    assert out["dom_rounds"] == page.poll_calls, "落盘的轮数必须等于真实 evaluate 次数"
    assert out["xhr_rounds"] == sum(1 for w in page.waits if w == 300), "XHR 空等轮数同口径"
    assert out["seconds"] == round(out["dom_seconds"] + out["xhr_seconds"], 1)


def test_grab_detail_probe_runs_once_and_never_on_success(monkeypatch):
    """成本口径（A3 实测：一次原子写 ~0.47ms，取证不能做成性能回归）：探测**只在失败出口
    跑一次**——不进轮询循环、成功路径零次。票面「DOM 选择器命中数」这个新字段的全部代价
    就是这一条 evaluate。"""
    monkeypatch.setattr(chat_mod, "time", _FakeClock(step=1.0))
    page = _JdPage(probe=[_PROBE_MISS])
    chat_mod.grab_detail(page, {"data": None})
    assert page.probe_calls == 1, "失败出口一次，不多不少（逐轮探测会把 20s 干等放大成上百次往返）"
    assert page.poll_calls > 1, "轮询确实用了多次 evaluate，探测不与它们混算"

    hit = _BeatPage(evaluate_seq=["JD 全文：负责测试"])
    container = {"data": None}
    assert chat_mod.grab_detail(hit, container) == "JD 全文：负责测试"
    assert "empty_outcome" not in container, "成功路径不写取证字段（也不该多跑一次协议往返）"


def test_grab_detail_both_empty_case_still_records(monkeypatch):
    """票面第三类的证据形态：详情响应**到了**但 postDescription 为空 + DOM 零命中。
    这是唯一能说「这个岗位没内容」的形态，故必须留下 hit_total=0 的探测结果供归因。"""
    monkeypatch.setattr(chat_mod, "time", _FakeClock(step=1.0))
    detail = {"data": {"code": 0, "zpData": {"jobInfo": {"postDescription": ""}}}}
    page = _JdPage(probe=[_PROBE_MISS, dict(_PROBE_MISS, selector=".job-detail-content")])
    assert chat_mod.grab_detail(page, detail) == ""
    out = page.probe  # 探测确实跑过
    assert out and all(h["hits"] == 0 for h in out)
    assert detail["empty_outcome"]["hit_total"] == 0 and detail["empty_outcome"]["hit_selectors"] == []


# --- #56 修复轮 3 R2：`grab_detail` 的「有 data」口径统一到 postDescription 取值链 ---
#
# 旧写法 `if detail_data["data"]:` 先拿**外层 dict 的真值**冒充「拿到正文」，再在取值链上
# `.get("zpData", {}).get("jobInfo", {}).get("postDescription", "")`：
#   ① `{"code": 0, "zpData": null}`（岗位失效时 站内真实回这个形态）当场 AttributeError
#      从 grab_detail 逸出——「返回类型恒为 str、契约一字未动」这条 A5 守住的边界被破在半路；
#   ② 链上取到非字符串（如 `postDescription: 123`）时被当正文返回，编排层据此产出一份
#      `jdText` 不是 JD 正文的 request_N.json，读起来像一次正常起草。
# 现在两侧共用 `detail_post_description` 这一条链（编排层 `classify_no_jd` 引用同一个函数，
# 单一真源）：取不到**非空字符串正文**就走落空出口，「返回空串」与「XHR 有可用正文」互斥。


def test_detail_post_description_is_form_tolerant():
    """取值链本体：逐层形态不苛求（缺失 / null / 非字典 / 正文非字符串）一律返回 ""，
    只有真拿到字符串正文才返回它（空串正文也算没有正文）。"""
    assert chat_mod.detail_post_description({"zpData": {"jobInfo": {"postDescription": "JD 全文"}}}) == "JD 全文"
    assert chat_mod.detail_post_description({"zpData": {"jobInfo": {"postDescription": ""}}}) == ""
    for bad in (
        None,
        [],
        "不是响应",
        {},
        {"zpData": None},
        {"zpData": {"jobInfo": None}},
        {"zpData": {"jobInfo": {}}},
        {"zpData": {"jobInfo": {"postDescription": None}}},
        {"zpData": {"jobInfo": {"postDescription": 123}}},
    ):
        assert chat_mod.detail_post_description(bad) == "", f"{bad!r} 取不到正文却不算空？"


def test_grab_detail_nonempty_data_dict_without_body_takes_the_empty_exit(monkeypatch):
    """R2 反例①：`data` 是非空 dict 但链上没有正文（`zpData: null`）→ 返回 ""、照常落
    `empty_outcome` 取证，而不是让 AttributeError 逸出破坏返回契约。"""
    monkeypatch.setattr(chat_mod, "time", _FakeClock(step=1.0))
    page = _JdPage(probe=[_PROBE_MISS])
    container = {"data": {"code": 0, "zpData": None, "message": "岗位已下线"}}
    assert chat_mod.grab_detail(page, container) == ""
    assert container["empty_outcome"]["hit_total"] == 0, "落空出口照常取证（不改返回契约的同一处）"
    assert page.probe_calls == 1


def test_grab_detail_never_returns_a_non_string_as_the_body(monkeypatch):
    """R2 反例②：链上取到非字符串（`postDescription: 123`）不算正文——否则 `_acquire_and_draft_greeting`
    会产出一份 jdText=123 的 request_N.json，「拿不到正文」被伪装成「拿到了」。"""
    monkeypatch.setattr(chat_mod, "time", _FakeClock(step=1.0))
    container = {"data": {"code": 0, "zpData": {"jobInfo": {"postDescription": 123}}}}
    assert chat_mod.grab_detail(_JdPage(probe=[_PROBE_MISS]), container) == ""
    assert container["empty_outcome"], "同样走落空出口"

    body = {"data": {"code": 0, "zpData": {"jobInfo": {"postDescription": "JD：XHR 兜底正文"}}}}
    assert chat_mod.grab_detail(_JdPage(probe=[_PROBE_MISS]), body) == "JD：XHR 兜底正文"


def test_grab_detail_probe_failure_keeps_str_contract(monkeypatch):
    """探测本身失败（页面已关是最现实的形态）时：返回仍是 ""、异常不外抛、
    并如实留下 probe_error——取证缺口不粉饰成「命中数为 0」，也不反噬投递。"""
    monkeypatch.setattr(chat_mod, "time", _FakeClock(step=1.0))
    container = {"data": None}
    page = _JdPage(probe_raises=True)
    assert chat_mod.grab_detail(page, container) == ""
    out = container["empty_outcome"]
    assert "probe_error" in out and "closed" in out["probe_error"]
    assert "hit_total" not in out, "没探到就不写命中数（0 会被读成「选择器全落空」的假证据）"
    assert out["dom_rounds"] == page.poll_calls, "等待时长与轮数仍是实测值"


def test_grab_detail_beats_the_probe_on_empty_exit(monkeypatch):
    """#56 A3 × A5 的交点：探测也是一次 evaluateExpression 裸奔调用，必须有名有姓；
    探测抛出时只有 pre 没有 post——最后一条 label 依旧与实际控制流严格一致。"""
    monkeypatch.setattr(chat_mod, "time", _FakeClock(step=1.0))
    beat = _BeatRecorder()
    chat_mod.grab_detail(_JdPage(probe=[_PROBE_MISS]), {"data": None}, beat_fn=beat)
    assert beat.labels[-2:] == ["jd:dom-probe:pre", "jd:dom-probe:post"]

    beat2 = _BeatRecorder()
    chat_mod.grab_detail(_JdPage(probe_raises=True), {"data": None}, beat_fn=beat2)
    assert beat2.labels[-1] == "jd:dom-probe:pre", "探测没出来不谎报 post"
    assert "jd:dom-probe:post" not in beat2.labels


def test_grab_detail_records_url_failure_instead_of_losing_evidence(monkeypatch):
    """`page.url` 在页面已关时会抛——取证不能让这一抛把整个 empty_outcome 带走：
    等待时长与命中数照落，url 留空并写 url_error（缺口本身也要留痕）。"""
    monkeypatch.setattr(chat_mod, "time", _FakeClock(step=1.0))

    class _ClosedUrl:
        """除 `url` 外全部委托给内层替身（`url` 是类上的 data descriptor，
        所以不能用子类赋值覆盖——内层 `__init__` 里的 `self.url = url` 会直接报错）。"""

        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        @property
        def url(self):
            raise RuntimeError("Target page, context or browser has been closed")

    inner = _JdPage(probe=[_PROBE_MISS])
    container = {"data": None}
    assert chat_mod.grab_detail(_ClosedUrl(inner), container) == ""
    out = container["empty_outcome"]
    assert out["url"] == "" and "closed" in out["url_error"]
    assert out["dom_rounds"] == inner.poll_calls and out["hit_total"] == 0


def test_grab_detail_unwritable_container_does_not_raise(monkeypatch):
    """容器形态诡异（不可写）时放弃取证而不是把落空出口变成抛异常的地方——
    返回契约仍是 ""（观测层绝不新增失败面，#38 同口径）。"""
    monkeypatch.setattr(chat_mod, "time", _FakeClock(step=1.0))

    class _Frozen(dict):
        def __setitem__(self, key, value):
            raise RuntimeError("container is frozen")

    page = _JdPage(probe=[_PROBE_MISS])
    assert chat_mod.grab_detail(page, _Frozen({"data": None})) == ""
    assert page.probe_calls == 1, "探测照跑（只是结果落不下），不改控制流"


def test_grab_detail_bad_probe_shape_does_not_become_zero_hits(monkeypatch):
    """探测返回形态异常（JS 侧改动 / evaluate 返回非列表）时**宁缺毋造**：
    只落 probe_error，不落 `hit_total: 0`——0 命中是「DOM 与 XHR 双空」判据的 DOM 侧正向证据形态，
    拿取证缺口去冒充它，等于把岗位判成无内容（票面点名的静默误杀）。"""
    monkeypatch.setattr(chat_mod, "time", _FakeClock(step=1.0))
    page = _BeatPage(evaluate_seq=["", "", "", ""])  # 探测落到默认 view（dict），不是列表
    container = {"data": None}
    assert chat_mod.grab_detail(page, container) == ""
    out = container["empty_outcome"]
    assert "hit_total" not in out and "hit_selectors" not in out
    assert "形态异常" in out["probe_error"] and out["dom_rounds"] == 4


# --- #94 验收 4：「抓取用哪组选择器」与「探针用哪组选择器」必须是同一份真源 ---
#
# 票面的盲区口径：`zero_hits` 这个读数的全部效力，取决于「探针探的那组选择器」是不是
# 「抓取用的那组」——两边各修一半时，探针报出来的「全落空」描述的是一套抓取永远不吃到的
# 选择器，读数从证据降级成假证据。今天两处靠 `grab_detail` 里的同一个局部变量维系，
# 谁另起一份列表就会静默断开。本用例走**真实调用面**：记录每次 evaluate 实际收到的
# selector 实参，断言两侧逐项相等且等于模块常量当前值——任一侧单独加/删一条即变红。


class _SelectorSpyPage:
    """落空路径上的 evaluate 实参记录器：按 JS 段分栏收下第二个位置参数（真实 page.evaluate
    的 arg），其余形态照 `boss.grab_detail` 的两段 JS 各自契约给：
    - 抓取轮（JS 只用 `querySelector`）恒返回空串（=本轮没拿到正文）；
    - 命中探测（JS 用 `querySelectorAll`）按**收到的那份 selector** 逐条回
      `{selector, hits, nonempty}`——与 `_DETAIL_PROBE_JS` 的真实回包同形状（#63 的教训：
      替身形状必须与生产一致，否则用例绿而生产崩）。"""

    def __init__(self, url="https://www.zhipin.com/web/geek/jobs"):
        self.url = url
        self.grab_selectors = []  # DOM 抓取轮每次收到的实参（轮询会收到多次）
        self.probe_selectors = []  # 落空探针收到的实参（失败出口一次）
        self.waits = []

    def evaluate(self, js, *args):
        received = list(args[0]) if args and isinstance(args[0], (list, tuple)) else []
        if "querySelectorAll" in js:
            self.probe_selectors.append(received)
            return [{"selector": s, "hits": 0, "nonempty": 0} for s in received]
        self.grab_selectors.append(received)
        return ""

    def wait_for_timeout(self, ms):
        self.waits.append(ms)


def test_grab_detail_dom_side_and_probe_share_one_selector_source(monkeypatch):
    """#94 验收 4 本体：落空这一次运行里，DOM 抓取轮与命中探针收到的 selector 实参
    **逐项相等**，且都等于模块常量 `DETAIL_JD_SELECTORS` 的当前值（本用例只钉「同一份」；
    扩进来的那条排末尾、带作用域，由切片 B 的形态用例与钉顺序那条各自守住）。"""
    monkeypatch.setattr(chat_mod, "time", _FakeClock(step=1.0))
    page = _SelectorSpyPage()
    container = {"data": None}
    assert chat_mod.grab_detail(page, container) == "", "返回契约不动（#56 A3/A5）"
    expected = list(chat_mod.DETAIL_JD_SELECTORS)
    assert page.grab_selectors, "DOM 抓取轮确实把选择器列表送进了 evaluate（没有它本轮判据无从谈起）"
    assert len(page.probe_selectors) == 1, "落空探针一次（成本口径同 #56 A5）"
    for label, rounds in (("抓取轮", page.grab_selectors), ("落空探针", page.probe_selectors)):
        for i, got in enumerate(rounds, start=1):
            assert got == expected, f"{label}第 {i} 次收到的选择器与模块常量不一致（两边各修一半）"


def test_empty_detail_probe_report_lines_up_with_the_grab_source(monkeypatch):
    """同一份真源的读法护栏：探针回包里的 `selector` 字段（进 `empty_outcome.selectors`、
    再被编排层 `detail_empty_outcome` 事件原样落盘）必须与抓取轮吃的那组一一对应——
    事后翻台账的人拿这份对照判断「形态漂移还是内容缺失」，错位即假证据。"""
    monkeypatch.setattr(chat_mod, "time", _FakeClock(step=1.0))
    page = _SelectorSpyPage()
    container = {"data": None}
    assert chat_mod.grab_detail(page, container) == ""
    outcome = container["empty_outcome"]
    assert [s["selector"] for s in outcome["selectors"]] == list(chat_mod.DETAIL_JD_SELECTORS)
    assert outcome["selectors"] == [{"selector": s, "hits": 0, "nonempty": 0} for s in chat_mod.DETAIL_JD_SELECTORS]


# --- #94 切片 B：分栏视图右栏的 JD 容器（`.job-detail-body .desc`）---
#
# 形态读数来自两个只读探针（2026-10-23，未点沟通、未投递；全量记录见
# `内部调研笔记目录（未随本仓分发）boss-recommend-card-shape.md` §15）：`scripts/oneshot/probe_jd_splitview.py`
# 量出「点左栏卡片后右栏渲染详情」这个形态里，**现有 8 条选择器在 4/4 个现场样本上 hits 全为 0**，
# JD 正文容器是 `div.job-detail-body > p.desc`；`scripts/oneshot/probe_jd_watermark.py` 量出同一容器
# `innerText` 205 字 / `textContent` 793 字（后者含 Boss 的反爬水印与整段内联 CSS）。
#
# 为什么这几条用例要让替身自己走一遍选择器匹配，而不是像上面几段那样编排好 evaluate 的返回值
# （#63 的教训：替身比生产宽松 ⇒ 用例绿而生产崩）：本票的风险恰好是**命中的是哪个节点**，而生产
# 取法 `document.querySelector(s)` 只取文档序首个命中——这个差别只有在文档序真摆着同名促销节点的
# 替身上才读得出来。同名节点有几个、各是什么标签与文案长度：§15.4 记一份，这里不抄副本。


class _DomEl:
    """替身页的节点：tag / class 属性原串 / 子节点 / 文本，外加「上不上屏」。
    `rendered=False` 对应浏览器里不渲染的节点——Boss 的反爬水印 span 与 `p.desc` 里那段内联 `<style>`：
    它们进 `textContent`、不进 `innerText`，这正是「水印不外泄」这条判据的物理依据。"""

    def __init__(self, tag, cls="", children=(), text="", rendered=True):
        self.tag = tag
        self.cls = cls
        self.text = text
        self.rendered = rendered
        self.children = list(children)
        self.parent = None
        for child in self.children:
            child.parent = self

    def document_order(self):
        """前序遍历，与浏览器 `querySelectorAll` 的返回序（文档序）一致。"""
        yield self
        for child in self.children:
            yield from child.document_order()

    def inner_text(self):
        if not self.rendered:
            return ""
        return self.text + "".join(child.inner_text() for child in self.children)

    def text_content(self):
        return self.text + "".join(child.text_content() for child in self.children)


def _el(tag, cls="", *children, rendered=True, text=""):
    return _DomEl(tag, cls, children, text=text, rendered=rendered)


def _text_node(s):
    """文本节点：无 tag、无 class ⇒ 永不被选择器命中，只贡献文本。"""
    return _DomEl("", "", (), text=s)


_TAG_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*")
_CLASS_TOKEN_RE = re.compile(r"\.([A-Za-z0-9_-]+)")
_CLASS_SUBSTRING_RE = re.compile(r'\[class\*="([^"]*)"\]')


def _parse_simple(part):
    """.job-detail-body / p.desc / [class*="job-sec-text"] → (tag, 必需 class token, 必需 class 子串)。
    认不出的写法**抛**而不是算 0 命中：替身静默不命中的话，将来扩选择器的人只会看到一条绿用例，
    而生产那边根本没人验过。"""
    tag = None
    head = _TAG_NAME_RE.match(part)
    if head:
        tag, part = head.group(0), part[head.end() :]
    tokens, substrings = [], []
    while part:
        found = _CLASS_TOKEN_RE.match(part) or _CLASS_SUBSTRING_RE.match(part)
        if not found:
            raise AssertionError(f"替身读不懂这个选择器片段：{part!r}（要么扩展替身，要么别加这条选择器）")
        if found.re.pattern == _CLASS_TOKEN_RE.pattern:
            tokens.append(found.group(1))
        else:
            substrings.append(found.group(1))
        part = part[found.end() :]
    return tag, tokens, substrings


def _parse_selector(selector):
    """只认现场用到的「空格 = 后代」组合器；`>` / `,` / `:` 一律抛（理由同上：宁红不静默）。"""
    if any(ch in selector for ch in ">,:"):
        raise AssertionError(f"替身不支持这个选择器形态：{selector!r}")
    return [_parse_simple(part) for part in selector.split()]


def _simple_matches(node, parsed):
    tag, tokens, substrings = parsed
    if tag and node.tag != tag:
        return False
    class_tokens = node.cls.split()
    return all(t in class_tokens for t in tokens) and all(s in node.cls for s in substrings)


def _matches(node, chain):
    """链的末段量节点自己，前面的段自下而上找祖先（后代组合器的右起匹配）。"""
    if not _simple_matches(node, chain[-1]):
        return False
    index = len(chain) - 2
    ancestor = node.parent
    while index >= 0:
        if ancestor is None:
            return False
        if _simple_matches(ancestor, chain[index]):
            index -= 1
        ancestor = ancestor.parent
    return True


class _SplitViewPage:
    """分栏视图页的替身：把 `grab_detail` 的两段 JS 在 Python 侧照原样跑一遍。
    抓取轮逐条选择器取 `querySelector` 的**首个**命中、按 `>20 字` 判据决定继续还是返回；
    命中探测（`_DETAIL_PROBE_JS`）逐条回 `{selector, hits, nonempty}`。
    **从生产 JS 文本里读的**：两条分支的长度判据（`_length_gate`）与抓取分支的取值属性名
    （`_text_attr`）——生产改了口径，这里要么跟着变、要么红着问（写死一份就是「替身比生产宽松」
    的另一副面孔）。**替身自己钉住的**：探针分支的取值属性写死 `inner_text`。这条分支只回计数
    （命中几个、其中几个过判据），不产出任何正文，把它的取法读成 `textContent` 也只会让 nonempty
    偏高、不会把内容喂给下游，所以不值得一份第二判据；代价是探针那句 JS 真要换成别的取法，
    本文件不会自动跟上，得人来对齐——这是替身的已知边界，如实写在这里。"""

    def __init__(self, root, url="https://www.zhipin.com/web/geek/jobs"):
        self.root = root
        self.url = url
        self.grab_selectors = []
        self.probe_selectors = []
        self.waits = []

    def wait_for_timeout(self, ms):
        self.waits.append(ms)

    def hits(self, selector):
        """用例用它核实自己钉的那条前提：这条选择器在替身里按文档序命中了哪些节点。"""
        chain = _parse_selector(selector)
        return [node for node in self.root.document_order() if _matches(node, chain)]

    @staticmethod
    def _text_attr(js):
        for name, attr in (("innerText", "inner_text"), ("textContent", "text_content")):
            if name in js:
                return attr
        raise AssertionError(f"取值属性读不出来（既没写 innerText 也没写 textContent）：{js!r}")

    @staticmethod
    def _length_gate(js):
        gate = re.search(r"length > (\d+)", js)
        if not gate:
            raise AssertionError(f"长度判据读不出来：{js!r}")
        return int(gate.group(1))

    def evaluate(self, js, *args):
        selectors = list(args[0]) if args and isinstance(args[0], (list, tuple)) else []
        gate = self._length_gate(js)
        if "querySelectorAll" in js:  # 落空探针：命中数 + 非空数，取值口径照生产那句 innerText
            self.probe_selectors.append(selectors)
            out = []
            for selector in selectors:
                nodes = self.hits(selector)
                out.append(
                    {
                        "selector": selector,
                        "hits": len(nodes),
                        "nonempty": sum(1 for n in nodes if len(n.inner_text().strip()) > gate),
                    }
                )
            return out
        self.grab_selectors.append(selectors)
        attr = self._text_attr(js)
        for selector in selectors:
            nodes = self.hits(selector)
            if not nodes:
                continue
            text = getattr(nodes[0], attr)().strip()  # querySelector 只取首个命中，落空才换下一条
            if len(text) > gate:
                return text
        return ""


# 探针实测的那 205 字正文（`p.desc` 的 innerText；含 Boss 用来打乱爬虫的两个部首字「⾏」「⼯」，
# 以及被单独包了一层**可见** span 的「计」字——替身必须把可见 span 算进正文）。
_SPLIT_VIEW_JD = (
    "1.负责DRAM/HBM/NAND等存储芯片探针卡的整体开发能力建设以及配套团队能力建设；\n"
    "2.负责相关产品的研究、开发、设计、评估等工作，进⾏产品论证、可⾏性研究、盈利性评价等；\n"
    "3.研究、设计产品的功能和特性，负责产品结构设计、仿真、测试，以满⾜用户需求；\n"
    "4.负责生产⼯艺设计&测试&改善、整卡装配、生产检测、加工工艺的相关数据收集、分析、改善和标准化；\n"
    "5.以产品为核心，以量产为目标的体系构建和改善。"
)

_WATERMARK_CSS = (
    ".AJefFhDf{display:none!important;}"
    ".PEbKRnF{display:none!important;}"
    ".CnRPxMibcfN{display:inline-block;font-size:0!important;width:1em;height:1em;visibility:hidden;line-height:0;}"
)
# `p.desc` 里子节点的文档序，照存盘 HTML 那一段逐段抄：正文文本 / 隐藏水印 span / 可见 span。
# 水印是 Boss 插在正文中间的 4 个不上屏 span（直聘 / kanzhun / BOSS直聘 / 来自BOSS直聘），
# 而「计」那个 span 看着同族却**上屏**——它是正文的一部分，替身不能一并裁掉。
_JD_PIECES = (
    ("text", "1."),
    ("mark", "直聘"),
    ("text", "负责DRAM/HBM/NAND等存储芯片探针卡的"),
    ("mark", "kanzhun"),
    ("text", "整体开"),
    ("mark", "BOSS直聘"),
    ("text", "发能力建设以及配套团队能力建设；\n2.负责相关产品的研"),
    ("mark", "来自BOSS直聘"),
    ("text", "究、开发、设"),
    ("span", "计"),
    (
        "text",
        "、评估等工作，进⾏产品论证、可⾏性研究、盈利性评价等；\n3.研究、设计产品的功能和特性，"
        "负责产品结构设计、仿真、测试，以满⾜用户需求；\n4.负责生产⼯艺设计&测试&改善、整卡装配、"
        "生产检测、加工工艺的相关数据收集、分析、改善和标准化；\n5.以产品为核心，以量产为目标的体系构建和改善。",
    ),
)


def _jd_desc_element():
    """JD 正文容器 `<p class="desc" style="user-select: none;">`：一个内联 `<style>` + 上面那 11 段。
    现场读数（§15.3 取法探针）：`innerText` 205 字、`textContent` 793 字（整段 CSS 与 4 个水印都算正文）。
    替身实测：`innerText` 同样 205 字（正文那段照抄存盘 HTML），`textContent` 405 字——这里的 `<style>`
    只抄了 3 条规则，长度不与现场对齐；判据看的是「换成 textContent 就脏」的内容，不是字数。"""
    children = [_el("style", "", rendered=False, text=_WATERMARK_CSS)]
    for kind, piece in _JD_PIECES:
        if kind == "mark":
            children.append(_el("span", "PEbKRnF", _text_node(piece), rendered=False))
        elif kind == "span":
            children.append(_el("span", "ArXQyiSfHx", _text_node(piece)))
        else:
            children.append(_text_node(piece))
    return _el("p", "desc", *children)


def _vip_promo_element(tag="p"):
    """页头 VIP 促销卡里那个带 `class="desc"` 的节点。现场它是个 span、文案也短（形态读数只在 §15.4
    记一份）；这里按票面口径把它写成 `<p class="desc">` 并撑到 20 字以上——替身比现场更严，钉的是
    「不许裸写 `p.desc`」：那层同名节点一换成 p、文案一长过判据，生产就不该跟着把促销文案当 JD 起草。"""
    return _el(
        "a",
        "vip-info",
        _el("span", "status", _text_node("升级VIP")),
        _el(tag, "desc", _text_node("尊享8大求职权益 曝光提升 一键投递 去升级")),
        _el("span", "link", _text_node("去升级")),
    )


def _split_view_page(with_jd=True, promo_tag="p"):
    """分栏视图 `/web/geek/jobs` 点左栏卡片后的右栏形态，文档序照存盘 HTML：
    页头（含 VIP 促销）→ 右栏详情（`recommend-result-inner > … > job-detail-container > job-detail-box
    > job-detail-body`，里面依次是岗位头、「举报 / 微信扫码分享」、`h3.title`「职位描述」、职位标签，
    最后才是正文那个 `p.desc`）→ 侧栏促销与热门职位。
    `with_jd=False` 是「这个岗真没填正文」：右栏标题与标签照常渲染，就是没有 `.job-detail-body .desc`。"""
    detail_children = [
        _el("div", "", _text_node("存储探针卡产品部资深经理\n30-50K\n上海")),
        _el(
            "div",
            "",
            _el("a", "report-new", _text_node("举报")),
            _el("div", "wechat-share", _el("a", "link-wechat-share", _text_node("微信扫码分享"))),
        ),
        _el("h3", "title", _text_node("职位描述")),
        _el(
            "ul",
            "job-label-list",
            _el("li", "", _text_node("B端产品")),
            _el("li", "", _text_node("其他行业")),
        ),
    ]
    if with_jd:
        detail_children.append(_jd_desc_element())
    root = _el(
        "div",
        "page-jobs has-header",
        _el("div", "header-v2", _el("div", "dropdown", _vip_promo_element(promo_tag))),
        _el(
            "div",
            "recommend-result-inner",
            _el(
                "div",
                "recommend-result-job clearfix",
                _el(
                    "div",
                    "job-detail-container",
                    _el("div", "job-detail-box", _el("div", "job-detail-body", *detail_children)),
                ),
            ),
        ),
        _el(
            "div",
            "sider-box banner-item",
            _el("div", "title", _text_node("升级VIP")),
            _el("div", "desc", _text_node("尊享8大特权提升求职效率")),
            _el("ul", "hot-link-tab clearfix", _el("li", "active", _text_node("热门职位"))),
        ),
    )
    return _SplitViewPage(root)


def test_grab_detail_split_view_right_panel_body_is_grabbed(monkeypatch):
    """#94 验收 2 本体（改前红）：分栏形态下右栏职位描述清晰在屏，而旧 8 条选择器一条都不中 ⇒
    改前 `grab_detail` 对这个形态返回空串、岗位被误判 `no_jd`。摘掉追加的那条选择器，本用例即红。"""
    monkeypatch.setattr(chat_mod, "time", _FakeClock(step=1.0))
    page = _split_view_page(promo_tag="span")  # 现场形态：促销那个是 span，不是 p
    container = {"data": None}
    assert chat_mod.grab_detail(page, container) == _SPLIT_VIEW_JD, "拿到的必须是那 205 字正文本身"
    assert len(_SPLIT_VIEW_JD) == 205, "替身正文必须与探针实测的 innerText 等长（现场 205 字）"
    assert "empty_outcome" not in container, "成功路径不落取证（也不该多跑一次探针）"
    # 负对照：旧 8 条在这个形态上确实全落空——否则本用例只证明「新选择器能用」，证明不了改前为什么红
    probe = page.evaluate(chat_mod._DETAIL_PROBE_JS, list(chat_mod.DETAIL_JD_SELECTORS))
    assert [row["hits"] for row in probe[:-1]] == [0] * (len(probe) - 1), f"旧选择器应当在分栏形态全落空：{probe}"
    assert probe[-1] == {"selector": chat_mod.DETAIL_JD_SELECTORS[-1], "hits": 1, "nonempty": 1}


def test_grab_detail_split_view_scoped_selector_beats_the_promo_desc(monkeypatch):
    """对抗输入（票面非目标条款点名的「误抓别的容器 = 把不相干文本当 JD 起草」）：促销卡那个
    `desc` 节点写成 p、文案撑到 20 字以上，并摆在文档序**前面**。把追加那条改成裸 `p.desc`，本用例
    即红；为什么这条必须带 `.job-detail-body` 作用域见 §15.4（形态读数只在那儿记一份），这里不重述。"""
    monkeypatch.setattr(chat_mod, "time", _FakeClock(step=1.0))
    page = _split_view_page()
    container = {"data": None}
    got = chat_mod.grab_detail(page, container)
    assert got == _SPLIT_VIEW_JD
    assert "尊享" not in got and "求职权益" not in got
    # 上面两条断言钉得住的前提是：裸 `p.desc` 在替身里的首个命中确实是那张促销卡——替身要是把促销
    # 摆到 JD 之后，它们就什么都没钉，所以这里核实文档序，而不是只信构造代码写过。
    assert "尊享" in page.hits("p.desc")[0].inner_text()


def test_grab_detail_split_view_watermark_stays_out_of_the_body(monkeypatch):
    """水印不外泄：Boss 在分栏视图的 JD 里塞隐藏 span（直聘 / kanzhun / BOSS直聘 / 来自BOSS直聘）
    和一段内联 `<style>` ⇒ **取值属性必须是 innerText**。现场（§15.3）同一个 `p.desc`：innerText 205 字、
    textContent 793 字；替身实测：innerText 205 字（与现场一致）、textContent 405 字（`<style>` 只抄
    3 条规则，不必与现场等长，脏不脏看内容）。生产改成 textContent 就把水印和 CSS 喂进起草端，本用例即红。
    同时钉住「抓 `p.desc` 而不是外层 `.job-detail-body`」：外层比正文容器胖——现场 innerText 291 字、
    替身实测 247 字，多出来的就是举报 / 微信扫码分享 / 职位描述 / B端产品 这类上屏噪声（逐项拆分数见
    下面那条断言的注释）。"""
    monkeypatch.setattr(chat_mod, "time", _FakeClock(step=1.0))
    page = _split_view_page(promo_tag="span")
    got = chat_mod.grab_detail(page, {"data": None})
    assert got == _SPLIT_VIEW_JD, "可见 span 里的「计」也必须算进正文（不能连正文一起裁掉）"
    for leak in ("直聘", "kanzhun", "BOSS", "display:none", "visibility:hidden"):
        assert leak not in got, f"{leak} 泄进正文了——这是 textContent 取法或外层容器的指纹"
    for noise in ("举报", "微信扫码分享", "职位描述", "B端产品"):
        assert noise not in got, f"{noise} 是外层 `.job-detail-body` 的噪声，不属于正文容器"
    # 上面那串「噪声不在正文里」的断言要有钉子，前提是外层里确实摆着那些噪声。现场 `.job-detail-body`
    # 的 innerText 291 字＝那 205 字正文 + 86 字上屏噪声（举报 / 微信扫码分享 / 职位描述 / B端产品 /
    # 其他行业 / 岗位头，§15.3）；替身实测 247 字＝205 + 42（岗位头那段 22 字 + 上面这些标签 20 字）。
    # 替身不摆噪声的话，抓错一层的退化照样绿。
    outer = page.hits(".job-detail-body")[0].inner_text()
    assert "举报" in outer and "微信扫码分享" in outer and "B端产品" in outer
    assert len(outer) > len(_SPLIT_VIEW_JD), "外层比正文容器胖——抓错一层就是把噪声当 JD 起草"
    # 同理，水印那串断言的前提是：替身里同一个节点换成 textContent 就是脏的——不脏的话它永远绿。
    dirty = page.hits(".job-detail-body .desc")[0].text_content()
    assert "直聘" in dirty and "kanzhun" in dirty and "display:none" in dirty


def test_grab_detail_split_view_without_jd_body_still_lands_empty_exit(monkeypatch):
    """负例：分栏形态、右栏标题照常渲染，但这个岗**真没填正文** ⇒ 仍返回空串、仍走 XHR 兜底与
    `empty_outcome` 取证，探针如实报「全条落空」。不许退而抓促销那个 `p.desc`——那会把
    「岗位没内容」读成「拿到了正文」并送去起草，比 `no_jd` 代价更高。"""
    monkeypatch.setattr(chat_mod, "time", _FakeClock(step=1.0))
    page = _split_view_page(with_jd=False)
    container = {"data": None}
    assert chat_mod.grab_detail(page, container) == ""
    outcome = container["empty_outcome"]
    assert outcome["hit_total"] == 0 and outcome["hit_selectors"] == [], "落空取证必须如实报零命中"
    assert [row["selector"] for row in outcome["selectors"]] == list(chat_mod.DETAIL_JD_SELECTORS)
    assert len(page.probe_selectors) == 1, "探针只在失败出口跑一次（成本口径同 #56 A5）"
    # 「不许退而抓促销」这条断言的前提：促销那个 p.desc 确实还留在替身的文档里，裸写就会抓到它。
    assert "尊享" in page.hits("p.desc")[0].inner_text()


def test_detail_jd_selectors_keep_legacy_eight_then_append_scoped_one():
    """#94 验收 2 的「并存」口径（#44 与 §11.5 按钮选择器的先例）：旧 8 条**一字不改、顺序不动**，
    新的那条只许挂在末尾——独立详情页的旧形态要继续优先按原选择器取，新条目只在旧 8 条全落空时
    才有机会命中，这样对已验证形态零行为变更。作用域（`.job-detail-body`）也在这一条钉住。"""
    assert chat_mod.DETAIL_JD_SELECTORS[:8] == [
        ".job-sec-text",
        ".job-detail .text",
        ".job-detail .job-sec",
        ".job-sec .text",
        ".job-detail .describe",
        ".job-detail-content",
        '[class*="job-sec-text"]',
        '[class*="job-detail"] [class*="text"]',
    ], "旧 8 条是独立详情页形态的既有取法，顺序与字面都不动"
    assert chat_mod.DETAIL_JD_SELECTORS[8:] == [".job-detail-body .desc"], "分栏视图那条挂在末尾"


def test_read_state_negative_count_is_unknown_not_already_read():
    """负数不并入已读：生产解析器 `_unread_count` 已把负数洗成 None，这条管的是**绕过解析器**
    直接构造的记录（历史报告回放、别的 caller）——「不知道」读成「都看过」是本读数唯一的致命错误方向。"""
    assert chat_mod.read_state({"unreadMsgCount": -1}) == "unknown"
    report = chat_mod.build_report(
        [{"status": "hr_replied", "appliedJob": None, "unreadMsgCount": -1}], generated_at="2026-10-20 00:00:00"
    )
    assert report["unlinkedRepliedRead"] == 0
    assert report["unlinkedRepliedReadUnknown"] == 1
