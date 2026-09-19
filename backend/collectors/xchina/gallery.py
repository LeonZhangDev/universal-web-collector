r"""XChina 图集采集: 由图集 ID 枚举出该图集下的全部图片与视频。

本模块只做「站点声明」—— 枚举 / 判定 / 限速 / 镜像 / 命名逻辑都在 gallery_base 里。

站点实测特征(2026-09-17 图片部分 / 2026-09-18 视频与相册页, 结论直接决定实现,
改动前请重新验证)
================================================================================

URL 模板::

    # 图片
    https://img.xchina.io/photos/{gid}/{seq:05d}.jpg          原图
    https://img.xchina.io/photos/{gid}/{seq:05d}_600x0.webp   缩略
    https://img.xchina.io/photos/{gid}/{seq:05d}_800x0.webp
    https://img.xchina.io/photos/{gid}/{seq:05d}_1200x0.webp
    # 视频(与图片同一序号空间, 后缀不同, 互不冲突)
    https://img.xchina.io/photos/{gid}/{seq:05d}.mp4

存在性判定 —— 最关键的坑
------------------------
该站**不返回 404**。越界序号同样返回 HTTP 200, 只是 Content-Type 变成
text/html::

    00001.jpg -> 200 image/jpeg 656713B
    00056.jpg -> 200 image/jpeg 426153B
    00057.jpg -> 200 text/html     <- 越界, 但状态码仍是 200
    00004.mp4 -> 206 video/mp4  (109900697B)
    00005.mp4 -> 200 text/html     <- 视频越界, 同上

因此**只能用 Content-Type 判定存在性, 绝不能用状态码**。
若按 404/403 判定, 枚举会永不停止或第一张就误停。

⚠️ 2026-09-18 复测: **HEAD 是可靠的**。存在/越界分别返回
`200 image/jpeg|video/mp4`(带 Content-Length) 与 `200 text/html`, requests 连测 4/4
全部正常, `probe_size()` 也直接拿到 64.2MB。
曾把它记成"该站 HEAD 不返回任何响应头(只有一行 200 OK)" —— 那是 **curl 经系统
代理时 `-I` 只回 `200 Connection Established`** 造成的假象。若据此以为"每个序号
都要发两次请求", 会去做毫无意义的优化。
`gallery_base._head_status_headers` 的流式 GET 回退作为兜底保留(对真的不返回
Content-Type 的站点有用), 但**本站不需要它兜底**。

URL 形态与 ID 提取 —— 第二个大坑
--------------------------------
同一个图集有多种输入形态, 都必须能解析出 `{id}`::

    https://img.xchina.io/photos/{id}/00001.jpg        图片直链
    https://img.xchina.io/photos/{id}/00001.mp4        视频直链
    https://img.xchina.io/photos/{id}/00001_1200x0.webp
    https://xchina.co/photo/id-{id}.html               相册页
    https://xchina.co/photo/id-{id}/10.html            相册页第 10 页
    https://xchina.co/photoShow.html?id={id}           老式页面

⚠️ 相册页 URL 的末段 `10.html` 是**页码**, 不是图集 ID。
早期 `id_in_path` 只认 `/photos/`, 于是相册页 URL 匹配失败 → 退路取路径末段
→ 拿到 `10` 当 ID → 去枚举 `https://img.xchina.io/photos/10/00001.jpg`。
该站对不存在的图集同样返回 `200 text/html`, 于是 3 个序号全被判"不存在"、
枚举立即停止, 任务**报 success 却有 0 个资源**(2026-09-18 实测事故)。
现在 `page_tail=r"^\d+$"` 会把这种末段识别为页码并直接放弃解析,
宁可让任务明确报错, 也不去枚举一个猜出来的图集。

媒体类型: 图片与视频可以同时存在
--------------------------------
实测相册 `6a3654854fd25`(= `https://xchina.co/photo/id-6a3654854fd25.html`)::

    var videos = [00001.mp4(64M), 00002.mp4(77M), 00003.mp4(5M), 00004.mp4(105M)]
    photo-items: No.1 ~ No.12  (00001.jpg ~ 00012.jpg)

也就是**12 张图 + 4 段视频**, 且 `00001.jpg` 与 `00001.mp4` 同时存在:
两个序号空间一样, 靠扩展名区分, 不会撞名。
因此 `options.media=auto`(默认)在该站会图片与视频一起采:
只采图片会漏掉 260MB 的视频, 只采视频会漏掉整套图。

视频**没有尺寸档位**(只有 .mp4 一条), 所以 `quality` 对视频无意义;
`mirrors` 也因此为空 —— 视频没有备用下载点。

访问条件
--------
`img.xchina.io` 直连即可, 无需登录、无需浏览器、无需代理。
`xchina.co` 的 HTML 页面(相册页)**对普通请求返回 403**(Cloudflare challenge),
所以**不要**指望"用 requests 抓相册页 HTML 再从中提取资源清单"; 纯 HTTP 序号枚举
才是资源发现的主路径。相册页只通过 headless Chromium 读一次, 且仅用于两件事:
取 `<title>` 当输出目录名、判断这个相册有没有视频(见 `collectors/album_meta.py`)。

WAF 校验 Accept 头(项目级坑, 与具体站点无关)
---------------------------------------------
无 Accept 或 `Accept: */*` -> **403**; 含 `image/*` -> 200。
常量在 `core.config.IMAGE_ACCEPT` / `VIDEO_ACCEPT`, 由 gallery_base.probe 统一带上。
⚠️ 但 2026-09-18 实测 `.mp4` 路径**对任何 Accept(含完全不带头)都返回 206 video/mp4**,
说明该站的 Accept 白名单只覆盖部分路径。保留 VIDEO_ACCEPT 是为了对其它站点正确,
不要据此推断"Accept 无关紧要"。

画质档
------
`original` = `.jpg` 原图(实测约 382~656KB), `1200/800/600` = webp 变体
(1200 档实测约 28~119KB)。默认 original 以保持与原行为一致。
"""

from .. import register
from ..gallery_base import GallerySite, SequenceGallerySpider

XCHINA = GallerySite(
    name="xchina_gallery",
    base="https://img.xchina.io/photos",
    # 该站会按相册把资源分到 photos/photos2/photos3 等不同子路径: 枚举前自动试探命中
    base_candidates=[
        "https://img.xchina.io/photos",
        "https://img.xchina.io/photos2",
        "https://img.xchina.io/photos3",
    ],
    # 按画质从高到低
    variants=[".jpg", "_1200x0.webp", "_800x0.webp", "_600x0.webp"],
    quality_map={
        "original": ".jpg",
        "1200": "_1200x0.webp",
        "800": "_800x0.webp",
        "600": "_600x0.webp",
    },
    # 视频: 同一 gid 下的 00001.mp4 起顺序编号, 没有尺寸档位
    video_variants=[".mp4"],
    video_quality_map={"original": ".mp4"},
    id_patterns=[
        # 图片/视频直链: https://img.xchina.io/photos/{id}/00001.jpg|.mp4
        #   (站点可能把不同相册分到 photos/photos2/photos3 等不同子路径, 见 base_candidates)
        r"/photos\d*/([0-9A-Za-z_-]{6,})",
        # 相册页: https://xchina.co/photo/id-{id}.html 或 /photo/id-{id}/10.html
        #        (末段是**页码**, 不是 ID)
        r"/photo/id-([0-9A-Za-z_-]{4,})",
        # 老式: https://xchina.co/photoShow.html?id={id}
        r"/photoShow\.html\?id=([0-9A-Za-z_-]{4,})",
    ],
    page_tail=r"^\d+$",
    input_forms=[
        "图片/视频直链 https://img.xchina.io/photos/{id}/00001.jpg|.mp4",
        "相册页 https://xchina.co/photo/id-{id}.html",
        "图集 ID 本身(如 6aa5136f606fe)",
    ],
    # 相册页(经 headless 浏览器)用于取 <title> 作目录名 + 判断有无视频
    album_url_template="https://xchina.co/photo/id-{gid}.html",
    title_split=r"\s+-\s+",
)


@register("xchina_gallery")
class XChinaGallerySpider(SequenceGallerySpider):
    """图集 ID -> 该图集全部图片与视频。

    每个图片资源自带 mirrors(其余尺寸变体), 下载器在主 URL 失败时自动切换下载点。
    站点规则全部封装在本模块与 gallery_base, 下载层不感知站点细节。

    输出目录名默认取相册页 `<title>`(去掉 " - 分类 - 站名" 尾巴), 拿不到时回退
    图集 ID; 可用 `options.album_title` 切到完整标题或图集 ID。
    """

    site = XCHINA
