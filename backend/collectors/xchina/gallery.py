"""XChina 图集采集: 由图集 ID 枚举出该图集下的全部图片。

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

访问条件
--------
直连即可, 无需登录、无需浏览器、无需代理。
Referer 有无均可, 不影响结果。

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
    id_in_path=r"/photos/([0-9A-Za-z_-]{6,})",
)


@register("xchina_gallery")
class XChinaGallerySpider(SequenceGallerySpider):
    """图集 ID -> 该图集全部图片。

    每个资源自带 mirrors(其余尺寸变体), 下载器在主 URL 失败时自动切换下载点。
    站点规则全部封装在本模块与 gallery_base, 下载层不感知站点细节。
    """

    site = XCHINA
