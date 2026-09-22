r"""Pexels 图库采集器 —— 序号枚举型站点的**第二个实例**。

为什么把它加进来
================
在 Pexels 之前, `gallery_base` 这套声明式契约只有一个真实使用者(xchina)。
"只有一个使用者的抽象"没法证明它是抽象还是刚好合身 —— 加第二个站点是唯一
可靠的检验。本次接入**没有改动 gallery_base 的任何逻辑**, 只写了一份
`GallerySite` 声明 + 一个 `@register` 装饰的类, 这本身就说明契约是可复用的。

站点实测特征(2026-09-22)
========================

URL 模板::

    # 图片(原图)
    https://images.pexels.com/photos/{pid}/pexels-photo-{pid}.jpeg
    # 尺寸档: 站点用 query 参数表达, 而不是路径后缀
    https://images.pexels.com/photos/{pid}/pexels-photo-{pid}.jpeg?w=1200
    https://images.pexels.com/photos/{pid}/pexels-photo-{pid}.jpeg?w=800
    # 相册页(用于取标题 / 判断有无视频)
    https://www.pexels.com/photo/{slug}-{pid}/

⚠️ 与 xchina 的两处**根本不同**, 也正是这两点让接入有价值:

1. **文件名里嵌了 ID, 且 ID 不是末段的一部分**。
   xchina 是 `/photos/{gid}/00001.jpg` —— gid 在目录层。pexels 是
   `/photos/{pid}/pexels-photo-{pid}.jpeg` —— pid 同时出现在目录名和文件名里。
   因此 `id_patterns` 必须从完整路径里抓, 不能依赖"末段前的目录"这个假设。

2. **"相册"的语义不同**。xchina 一个 gid 下有 N 张按序号排的图;
   pexels 一个 photo 页就是**一张图**, 没有序号。所以本站的"相册"是
   **集合/搜索页**, 由 `SequenceSeriesSpider`(见 spider.py)处理 ——
   它复用序号枚举的限速/重试/镜像机制, 但资源来自页面解析而非序号探测。

存在性判定
----------
与 xchina 相同的坑: 越界或不存在的 id 同样可能返回 200。本站走**探测**
(官方 JSON API 优先, 失败回退页面 `<meta property="og:image">`), 不用序号
枚举, 所以判定收敛在 `spider.py` 的 `crawl()` 里, 每张图一次探测。

访问条件
--------
`images.pexels.com` 直连即可, 无需登录、无需浏览器、无需代理。
`www.pexels.com` 的 HTML 页对普通请求**可能返回 403/Cloudflare challenge**,
因此资源发现优先走官方 API(`api.pexels.com`), 页面解析只作兜底。

画质档
------
pexels 用 query 参数表达尺寸, 所以 `variants` 里写的是**带 query 的后缀** ——
`GallerySite.url_for` 会把变体拼在文件名之后, query 语法正好成立::

    base/{pid}/pexels-photo-{pid}.jpeg?w=1200

`original` 档不带参数(返回站点给的原图), 其余档自动成为 mirrors。
"""

from .. import register
from ..gallery_base import GallerySite, SequenceGallerySpider

PEXELS = GallerySite(
    name="pexels",
    # ⚠️ 注意 base 里**不含** {pid}: pid 同时出现在目录名与文件名里,
    # 由 id_patterns 从完整 URL 中抓出, 再交给 seq_format 之外的模板拼装。
    base="https://images.pexels.com/photos",
    variants=[
        "",                     # 原图(不带 query)
        "?auto=compress&w=1200",
        "?auto=compress&w=800",
        "?auto=compress&w=600",
    ],
    quality_map={
        "original": "",
        "1200": "?auto=compress&w=1200",
        "800": "?auto=compress&w=800",
        "600": "?auto=compress&w=600",
    },
    # 本站单张图 = 一个 photo, **没有序号枚举**, 所以 seq_format 用不到序号;
    # 保留默认值以通过 check_site 的渲染自检。
    seq_format="{seq:05d}",
    # 无视频声明: pexels 的视频是独立资源类型, 与照片不同 id 空间,
    # 混在一起枚举会静默采空某个空间。需要视频时另开采集器, 不在这里硬塞。
    video_variants=[],
    id_patterns=[
        # 原图/尺寸图: https://images.pexels.com/photos/1234567/pexels-photo-1234567.jpeg?...
        #   ⚠️ 必须把"照片页"和"图片直链"分开抓: 两者域名不同, 混成一条会在
        #   跨站跳转时取到错的段。这里锚定到 /photos/{digits}/ 这一段。
        r"images\.pexels\.com/photos/(\d+)/",
        # 相册/照片页: https://www.pexels.com/photo/{slug}-1234567/
        #   末段形如 "a-cat-sitting-1234567", id 是**结尾的数字**,
        #   前面的 slug 是可变文本。不锚定结尾就会把 slug 里的数字也抓进来。
        r"pexels\.com/photo/[^/?#]*?(\d+)/?(?:[?#]|$)",
        # 纯数字 ID: 用户在界面上直接粘一个 photo id 也应能采。
        r"^(\d{4,})$",
    ],
    # 实测 photo id 是 4~9 位十进制数(早期图库 id 较短, 新图 7 位)。
    # 自动识别时用它挡掉把 slug 里的年份当 id 的情况。
    gid_shape=r"\d{4,10}",
    # 末段是纯数字时它更可能是页码/序号而不是 id —— 与本站 id 形态冲突,
    # 所以**不启用** page_tail 的"页码"特殊处理, 直接沿用默认的"纯数字即页码"
    # 会让纯 id 输入被拒。这里关掉, 改由 gid_shape 把关。
    page_tail=r"^(?!)",          # 永不匹配: 纯数字一律当 id, 配合 gid_shape 兜底
    id_samples=[
        ("1234567", "1234567"),
        ("https://images.pexels.com/photos/1234567/pexels-photo-1234567.jpeg",
         "1234567"),
        ("https://images.pexels.com/photos/1234567/pexels-photo-1234567.jpeg?auto=compress&w=800",
         "1234567"),
        ("https://www.pexels.com/photo/a-cat-sitting-1234567/", "1234567"),
        ("https://www.pexels.com/photo/1234567/", "1234567"),
    ],
    input_forms=[
        "图片直链 https://images.pexels.com/photos/{id}/pexels-photo-{id}.jpeg",
        "照片页 https://www.pexels.com/photo/{slug}-{id}/",
        "照片 ID 本身(如 1234567)",
        # 集合/搜索页由独立采集器处理(见 spider.py), 不属于本站声明的输入形态:
        # 它的资源来自页面解析, 不是序号枚举。
    ],
    album_url_template="https://www.pexels.com/photo/{gid}/",
    # pexels 的 <title> 形如 "Free stock photo of ..." / "A Cat Sitting · Free Stock Photo"
    title_split=r"\s+[·|]\s+",
)

# ⚠️ 本站**没有**注册成 SequenceGallerySpider 的子类 —— 那会要求"一个 id 下有
# 序号递增的多个资源", 而 pexels 一个 id 只有一张图。缺少爬取逻辑的声明只是
# 一个数据常量, 这里显式留着供 spider.py 复用, 也说明"声明与爬取可以分离"。
