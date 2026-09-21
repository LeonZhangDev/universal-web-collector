"""XChina 视频页采集器: 粘贴视频页 URL, 自动过 Cloudflare 并捕获带签名的 m3u8。

为什么需要专门的采集器
====================
视频页 `https://xchina.co/video/id-{gid}.html` 是 Cloudflare 挑战页
(匿名 GET 直接 403), 而且播放器真正请求的 m3u8 带一次性签名
(`?expires=...&md5=...`, 实测约 30 分钟有效)。这个 URL **不在 HTML 里、
也不在 JS 状态变量里**, 只有播放器发起网络请求时才现身。所以"下载视频"不能像
图集那样走纯 HTTP 枚举, 必须开一次无头浏览器, 监听网络响应, 抓那个带签名的
m3u8, 然后交回通用的 HLS 下载器(它已经会校验签名、拦截占位、解密 AES-128)。

关键风险(已在 downloaders/video.py 的预检里兜住)
=============================================
签名过期/无效时站点**不报错**, 而是回一个语法合法、指向占位分片的 m3u8,
ffmpeg 会一路畅通地下完并报告 success, 用户只拿到几十秒占位画面。所以本采集器
在返回 m3u8 之前先跑一遍 `collectors.hls.inspect_playlist`: 凭证还活着、分片是真的、
密钥拿得到, 才放行; 否则响亮地失败并告诉用户"去视频页重新拿链接"。

CF 策略与 album_meta 一致: 匿名优先(陈旧 cf_clearance 会被永久拒绝),
带登录态被拒即隔离, 失败降级可见。

测试性
======
`_capture` 的浏览器运行器是注入点(`browser_runner`), 单测里换一个假运行器即可,
不必真的开 Chromium。
"""
import re
import time

from collectors.hls import estimate_size, inspect_playlist
from collectors.scores import (
    SCORE_ALBUM_PAGE,
    SCORE_RESOURCE_URL,
)
from core.config import settings
from core.naming import clean_segment
from .. import register

# 视频页 URL 形如 https://xchina.co/video/id-6aaa517d3f106.html
_VIDEO_PAGE_RE = re.compile(
    r"(?:https?://)?(?:[^/]+\.)?xchina\.(?:co|io)/video/id-([0-9A-Za-z_-]{6,})",
    re.I,
)
# 视频直链(若用户直接拿到带签名的 m3u8)
_VIDEO_M3U8_RE = re.compile(r"(?:https?://)?[^/]*xchina[^/]*\.download/.+\.m3u8", re.I)


def parse_video_gid(raw):
    """从视频页 URL 提取图集 ID; 不认别的形态(让图集采集器去认裸 ID)。

    故意**只认 `/video/` 路径**: 裸 gid 同时像图集又像视频, 留给我们无法判断,
    交给已经能处理裸 gid 的图集采集器更稳妥。替用户做决定的场合一律用最严规则。
    """
    if not raw:
        return None
    m = _VIDEO_PAGE_RE.search(raw)
    if m:
        return m.group(1)
    return None


@register("xchina_video")
class XChinaVideoSpider:
    """视频页采集器: 捕获带签名 m3u8 -> 一个 video 资源。"""

    # --- 自动识别 ---
    @staticmethod
    def match_score(url):
        if parse_video_gid(url):
            return SCORE_ALBUM_PAGE  # 与图集页同级: 明确的站点+明确形态
        if _VIDEO_M3U8_RE.search(url or ""):
            return SCORE_RESOURCE_URL
        return None

    # --- 浏览器运行器(默认 Playwright, 测试可注入) ---
    def _capture(self, url, log=None, browser_runner=None, session=None):
        """加载视频页, 捕获带签名的 m3u8。

        返回 (m3u8_url, title, info): info 是 inspect_playlist 的校验结果。
        browser_runner 签名: (url, on_response) -> dict(可含 title);
        默认用真实 Playwright。session 透传给 inspect_playlist(测试注入假响应用)。
        """
        if browser_runner is None:
            browser_runner = _playwright_capture
        captured = {}

        def on_response(resp):
            u = (resp.url or "").lower()
            # 只关心真正的播放列表; 别把 .key / .ts 当成目标。
            # ⚠️ 真实 m3u8 带签名查询串(`.m3u8?expires=..&md5=..`),
            #   不能用 endswith, 否则永远匹配不上
            if ".m3u8" in u:
                captured.setdefault("m3u8", resp.url)

        meta = browser_runner(url, on_response) or {}
        m3u8 = captured.get("m3u8")
        if not m3u8:
            raise RuntimeError(
                "未能从视频页捕获到播放列表(m3u8)。可能 Cloudflare 仍在拦截、"
                "播放器未加载, 或该视频需要登录态 —— 请先确认浏览器能正常打开此页"
            )

        # 预检: 凭证还活着、不是占位、密钥可达? 不通过就别往下交
        info = inspect_playlist(m3u8, session=session, log=log)
        if not info["ok"]:
            raise RuntimeError(f"捕获到的播放列表未通过校验: {info['reason']}")
        return m3u8, meta.get("title", ""), info

    # --- 资源发现 ---
    def crawl(self, url, log=None, session=None, browser_runner=None):
        m3u8, title, info = self._capture(
            url, log=log, session=session, browser_runner=browser_runner
        )
        gid = parse_video_gid(url) or "video"
        base = clean_segment(title) if title else gid
        size = None
        if info.get("segments"):
            # 用「单片体积 x 分片数」估整段大小, 让 min_size/max_size 过滤对 HLS 有效
            # (.m3u8 的 Content-Length 只有几 KB, 直接比会误杀整段视频)
            try:
                size = estimate_size(info["segments"], session=session, log=log)
            except Exception:
                size = None
        return [{
            "type": "video",
            "url": m3u8,
            "headers": {"referer": url},
            "mirrors": [],
            "size": size,
            # ⚠️ 文件名用 **gid**, 不用页面标题: 视频是平铺在下载根目录的
            # (见 core/layout.py), 那一层没有相册文件夹可以依赖, 名字必须自带
            # 唯一性 —— 两个不同视频的标题一模一样是常事, 撞名只能靠 `xx(2).mp4`
            # 兜底, 越兜越乱。gid 是站点自己的标识(它的播放列表就叫
            # `{gid}.m3u8`), 既是"网站中的文件名", 也稳定且唯一。
            "filename": f"{gid}.mp4",
            # 标题仍然带出去: 任务列表拿它做展示名(见 _infer_name), 撞名时也拿它
            # 当区分词 —— 文件名里看不到标题, 至少别让信息在这里丢掉。
            "album": base,
        }]

    # --- 创建前预览 ---
    def preview(self, url, options=None, log=None, max_items=12, session=None,
                browser_runner=None):
        m3u8, title, info = self._capture(
            url, log=log, session=session, browser_runner=browser_runner
        )
        gid = parse_video_gid(url) or "video"
        base = clean_segment(title) if title else gid
        variants = info.get("variants") or []
        height = max((v.get("height") or 0) for v in variants) if variants else 0
        return {
            "gid": gid,
            "title": title,
            "group": base,
            "media": ["video"],
            "photos": 0,
            "videos": 1,
            "video_bytes": info.get("duration") and None or info.get("key_bytes") or 0,
            "duration": info.get("duration") or 0,
            "quality": f"{height}p" if height else ("AES-128" if info.get("encrypted") else "未知"),
            "encrypted": info.get("encrypted", False),
            "sampled": False,
            "page": None,
            "tags": [],
            "maker": None,
            # 示例名与实际落盘一致(视频平铺在下载根目录, 名字是 gid, 见 crawl)
            "sample_files": [f"{gid}.mp4"],
            "video_items": [{"name": f"{gid}.mp4", "url": m3u8,
                             "size": info.get("key_bytes") or 0}],
            "logs": [],
        }


def _playwright_capture(url, on_response, log=None):
    """真实浏览器运行器: 过 Cloudflare, 监听网络响应抓 m3u8。

    on_response(resp): 对每条网络响应回调, resp 需有 .url 属性。
    返回 dict(可含 title)。
    """
    from urllib.parse import urlparse

    from playwright.sync_api import sync_playwright

    from collectors.album_meta import _storage_state

    domain = urlparse(url).netloc
    state = _storage_state(url)  # 已保存的登录态路径, 没有则 None

    meta = {}
    attempts = [None]
    if state:
        attempts.append(state)

    last_err = None
    for i, storage in enumerate(attempts):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                ctx_kwargs = {"user_agent": settings.user_agent}
                if storage:
                    ctx_kwargs["storage_state"] = storage
                if settings.proxy:
                    ctx_kwargs["proxy"] = {"server": settings.proxy}
                context = browser.new_context(**ctx_kwargs)
                page = context.new_page()
                page.on("response", on_response)
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    last_err = e
                # 轮询: 不靠 networkidle(挑战页可能永远 idle 不了),
                # 而是等播放器真正发出 m3u8 请求
                deadline = time.time() + 25
                while time.time() < deadline:
                    try:
                        if page.title():
                            meta["title"] = page.title()
                    except Exception:
                        pass
                    time.sleep(0.5)
                try:
                    meta.setdefault("title", page.title())
                except Exception:
                    pass
                try:
                    if storage:
                        context.storage_state(path=str(state))
                except Exception:
                    pass
                browser.close()
            if i and log:
                log("视频页: 匿名读取未拿到播放列表, 带登录态重试成功")
            return meta
        except Exception as e:
            last_err = e
            if log:
                log(f"视频页读取失败: {type(e).__name__}: {e}")
    if last_err:
        raise last_err
    return meta
