"""猎聘平台：接口占位。真实实现需要猎聘账号实测其反爬与页面结构，
按 docs/agents/proposal-review.md 完成调研后再落地。"""

from . import PlatformBase


class LiepinPlatform(PlatformBase):
    name = "liepin"
    login_dialog_selector = ""

    def crawl_jds(self, page, queries, target, exclude_ids):
        raise NotImplementedError("猎聘爬取未实现：需账号实测页面结构与反爬强度")

    def apply(self, page, job, send_text, opts=None):
        raise NotImplementedError("猎聘投递未实现：需账号实测沟通流程")

    def followup(self, page, applied):
        raise NotImplementedError("猎聘回复追踪未实现：需账号实测消息页结构")
