r"""XChina 图集采集: 由图集 ID 枚举出该图集下的全部图片。

本模块只做「站点声明」——枚举 / 判定 / 限速 / 镜像逻辑都在 gallery_base 里。

站点实测特征(2026-09-17, 结论直接决定实现, 改动前请重新验证)
================================================================

URL 模板::

    https://img.xchina.io/photos/{gid}/{seq:05d}.jpg          原图
    https://img.xchina.io/photos/{gid}/{seq:05d}_600x0.webp   缩略
    https://img.xchina.io/photos/{gid}/{seq:05d}_800x0.webp
    https://img.xchina.io/photos/{gid}/{seq:05d}_1200x0.webp

存在性判定 —— 最关键的坑
------------------------
该站**不返回 404**。越界序号同样返回 HTTP 200, 只是 Content-Type 变成
text/html::

    00001.jpg -> 200 image/jpeg 656713B
    00056.jpg -> 200 image/jpeg 426153B
    00057.jpg -> 200 text/html     <- 越界, 但状态码仍是 200

因此**只能用 Content-Type 判定存在性, 绝不能用状态码**。
若按 404/403 判定, 枚举会永不停止或第一张就误停。

URL 形态与 ID 提取 —— 第二个大坑
--------------------------------
同一个图集有四种输入形态, 都必须能解析出 `{id}`::

    https://img.xchina.io/photos/{id}/00001.jpg        图片直链
    https://img.xchina.io/photos/{id}/00001_1200x0.webp
    https://xchina.co/photo/id-{id}/10.html            相册页第 10 页
    https://xchina.co/photoShow.html?id={id}           老式页面

⚠️ 相册页 URL 的末段 `10.html` 是**页码**, 不是图集 ID。
早期 `id_in_path` 只认 `/photos/`, 于是相册页 URL 匹配失败 → 退路取路径末段
→ 拿到 `10` 当 ID → 去枚举 `https://img.xchina.io/photos/10/00001.jpg`。
该站对不存在的图集同样返回 `200 text/html`, 于是 3 个序号全被判"不存在"、
枚举立即停止, 任务**报 success 却有 0 个资源**(2026-09-18 实测事故)。
现在 `page_tail=r"^\d+$"` 会把这种末段识别为页码并直接放弃解析,
宁可让任务明确报错, 也不去枚举一个猜出来的图集。

访问条件
--------
`img.xchina.io` 直连即可, 无需登录、无需浏览器、无需代理。
`xchina.co` 的 HTML 页面对普通请求返回 **403**(Cloudflare), 所以**不要**
指望"抓相册页 HTML 再从中提取图片列表"; 纯 HTTP 序号枚举才是可行路径。

WAF 校验 Accept 头(项目级坑, 与具体站点无关)
---------------------------------------------
无 Accept 或 `Accept: */*` -> **403**; 含 `image/*` -> 200。
常量在 `core.config.IMAGE_ACCEPT`, 由 gallery_base.probe 统一带上。

画质档
------
`original` = `.jpg` 原图(实测约 656KB), `1200/800/600` = webp 变体
(1200 档实测约 100KB 量级)。默认 original 以保持与原行为一致。
"""

from .. import register
from ..gallery_base import GallerySite, SequenceGallerySpider

XCHINA = GallerySite(
    name="xchina_gallery",
    base="https://img.xchina.io/photos",
    # 按画质从高到低
    variants=[".jpg", "_1200x0.webp", "_800x0.webp", "_600x0.webp"],
    quality_map={
        "original": ".jpg",
        "1200": "_1200x0.webp",
        "800": "_800x0.webp",
        "600": "_600x0.webp",
    },
    id_patterns=[
        # 图片直链: https://img.xchina.io/photos/{id}/00001.jpg (含 _1200x0.webp 变体)
        r"/photos/([0-9A-Za-z_-]{6,})",
        # 相册页: https://xchina.co/photo/id-{id}/10.html  (末段是**页码**, 不是 ID)
        r"/photo/id-([0-9A-Za-z_-]{4,})",
        # 老式: https://xchina.co/photoShow.html?id={id}
        r"/photoShow\.html\?id=([0-9A-Za-z_-]{4,})",
    ],
    page_tail=r"^\d+$",
    input_forms=[
        "图片直链 https://img.xchina.io/photos/{id}/00001.jpg",
        "相册页 https://xchina.co/photo/id-{id}/1.html",
        "图集 ID 本身(如 6aa5136f606fe)",
    ],
)


@register("xchina_gallery")
class XChinaGallerySpider(SequenceGallerySpider):
    """图集 ID -> 该图集全部图片。

    每个资源自带 mirrors(其余尺寸变体), 下载器在主 URL 失败时自动切换下载点。
    站点规则全部封装在本模块与 gallery_base, 下载层不感知站点细节。
    """

    site = XCHINA
