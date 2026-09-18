from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

from core.config import settings
from .parsers import (
    APIDetector,
    DOMParser,
    JSStateParser,
    NetworkParser,
    merge_by_priority,
    select_quality,
)


class BrowserCollector:
    """通用浏览器采集: 单次页面加载运行四个解析器, 持久化浏览器状态。

    与具体站点无关, 各 collector 插件复用。
    """

    wait_timeout = 8000

    def state_file(self, domain):
        settings.browser_state_dir.mkdir(parents=True, exist_ok=True)
        return settings.browser_state_dir / f"{domain}.json"

    def capture_detailed(self, url):
        """返回 {source: [resource, ...]} 原始解析结果(未合并)。"""
        domain = urlparse(url).netloc
        state = self.state_file(domain)
        storage = str(state) if state.exists() else None

        network = NetworkParser()
        api = APIDetector()
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx_kwargs = {"user_agent": settings.user_agent}
            if storage:
                ctx_kwargs["storage_state"] = storage
            if settings.proxy:
                ctx_kwargs["proxy"] = {"server": settings.proxy}
            context = browser.new_context(**ctx_kwargs)
            page = context.new_page()

            for handler in (network.on_response, api.on_response):
                page.on("response", handler)

            try:
                page.goto(url, wait_until="networkidle", timeout=30000)
            except PWTimeoutError:
                page.wait_for_timeout(self.wait_timeout)
            page.wait_for_timeout(self.wait_timeout)

            try:
                html = page.content()
            except Exception:
                html = ""
            js = JSStateParser(url)
            js.parse(html)
            dom = DOMParser(url)
            dom.parse(page)

            try:
                context.storage_state(path=str(state))
            except Exception:
                pass

            browser.close()

        return {"api": api.resources, "network": network.resources,
                "js": js.resources, "dom": dom.resources}

    def capture(self, url):
        """优先级合并 + 质量选择后的 resource 列表。"""
        results = self.capture_detailed(url)
        return select_quality(merge_by_priority(results))
