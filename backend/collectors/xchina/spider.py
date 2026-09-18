from ..browser import BrowserCollector
from .. import register


@register("xchina")
class XChinaSpider:
    """XChina 站点采集: 相册 photoShow.html?id=xxx / 视频页。

    站点特有解析逻辑(未来)在此扩展, 基础能力来自通用 BrowserCollector。
    """

    def crawl(self, url):
        return BrowserCollector().capture(url)
