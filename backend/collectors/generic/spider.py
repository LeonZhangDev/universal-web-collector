from urllib.parse import urlparse

from ..scores import SCORE_GENERIC
from ..browser import BrowserCollector
from .. import register


@register("generic")
class GenericSpider:
    """通用采集器: 任意 URL, 抓全部媒体资源 + 页面正文文本。"""

    @classmethod
    def match_score(cls, url):
        """垫底候选: 任何 http(s) 链接都能"试着抓"。

        ⚠️ 非 URL 输入(纯图集 ID)**必须返回 None**。请求 `6aa5136f606fe`
        会在 DNS 层失败, 与其让用户看"DNS 解析失败", 不如让这条输入
        落到图集采集器那里拿到"可接受哪些 URL 形态"的可读错误。
        """
        try:
            scheme = urlparse((url or "").strip()).scheme
        except ValueError:
            return None
        return SCORE_GENERIC if scheme in ("http", "https") else None

    def crawl(self, url):
        resources = BrowserCollector().capture(url)
        resources.append(
            {"type": "text", "url": url, "headers": {}, "source": "page"}
        )
        return resources
