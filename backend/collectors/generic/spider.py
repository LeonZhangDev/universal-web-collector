from ..browser import BrowserCollector
from .. import register


@register("generic")
class GenericSpider:
    """通用采集器: 任意 URL, 抓全部媒体资源 + 页面正文文本。"""

    def crawl(self, url):
        resources = BrowserCollector().capture(url)
        resources.append(
            {"type": "text", "url": url, "headers": {}, "source": "page"}
        )
        return resources
