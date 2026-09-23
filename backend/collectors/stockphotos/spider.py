r"""Pexels 采集器: 单张照片 + 搜索页 + 集合页。

三种输入形态, 三条路径
======================

1. **单张照片**(`/photo/{slug}-{id}/` 或图片直链或纯 id)
   —— 只有一个资源, 走 `crawl_single()`: 一次探测确认存在性, 产出 1 条资源。
   此时**不需要序号枚举**, 也不需要浏览器。

2. **搜索页**(`/search/{query}/`) —— 多张图, 但**没有可枚举的序号**。
   走 `crawl_page()`: 用官方 API(`api.pexels.com/v1/search`) 分页拉取,
   每页 80 张, 直到 `max_items` 或翻完。⚠️ 绝不用"猜 id"的方式枚举 —— 那是
   本项目最贵的一课(见 `gallery_base.parse_gid` 的说明: 猜错 id 是静默失败,
   报 success 而 0 资源)。

3. **集合页**(`/collections/{slug}-{id}/`) —— 同样多张图、同样没有序号。
   走 `_crawl_collection()`: 端点不同(`/v1/collections/{id}`), 且**必须**先从
   slug 里抽出 ID —— 见 `_collection_id` 的说明(拿 slug 去请求会得到一个与
   "集合真不存在"无法区分的 404)。

为什么不用官方 API 处理单张照片
------------------------------
API 要 API key(`PEXELS_API_KEY`), 而单张照片的图片直链是**公开可预测**的:
`images.pexels.com/photos/{id}/pexels-photo-{id}.jpeg`。为一个"只用一次"的
请求引入密钥依赖不划算。所以:
  * 单张 -> 直接构造直链 + probe 确认(零密钥)
  * 列表(搜索/集合) -> 必须走 API(列表内容无法从 id 推导), 此时才要求密钥

缺密钥时的行为
--------------
搜索/集合页输入且没有 `PEXELS_API_KEY` -> **明确报错并说清怎么配**, 而不是静默
返回空列表。空列表会被上层判成 failed, 但用户看不到"为什么" —— 那种
"任务失败但你不知道为什么"是本项目反复想消灭的体验。
"""

import json
import os
import re
from urllib.parse import parse_qs, unquote, urlparse

from core.config import IMAGE_ACCEPT
from collectors.scores import SCORE_AGGREGATE_PAGE, SCORE_ALBUM_PAGE, SCORE_BARE_ID

from .. import register
from ..gallery_base import _match_score, probe, _session
from .pexels import PEXELS

#: 官方 API 的基址与默认分页大小(官方上限 80)
API_BASE = "https://api.pexels.com/v1"
API_PAGE_SIZE = 80
#: 一次采集的硬上限, 防止"翻完整个搜索结果"(可能几万张)
DEFAULT_MAX_ITEMS = 300

_SEARCH_RE = re.compile(r"pexels\.com/(?:search|collections)/([^/?#]+)")
_COLLECTION_RE = re.compile(r"pexels\.com/collections/([^/?#]+)")
_QUERY_RE = re.compile(r"[?&]query=([^&#]+)")
#: 纯 ID 输入的形状 —— 与 PEXELS.gid_shape 一致, 单独提出来是因为它在
#: `match_score` 里要先于通用判据生效(见那里的说明)。
_BARE_ID = re.compile(PEXELS.gid_shape)

#: 站点上**不是**集合 id 的特殊 slug。写在这里是为了把它们与真实 id 明确分开:
#: `featured` 是一个"集合的列表页", 请求它只会得到一个集合数组而不是照片。
_NON_ID_SLUGS = {"featured", "popular", "latest", "search", "all"}


def _collection_id(slug):
    """从集合页 URL 的末段里抽出集合 ID; 抽不出返回 None。

    ⚠️ 站点 URL 给的是 **slug**(可读文字, 形如 `nature-2sx8z9c`), 而官方集合 API
    只认 **ID**。两者不是一回事: 拿 slug 去请求会得到 404, 而"这个集合不存在"与
    "我们猜错了 ID"在界面上长得一模一样 —— 于是用户会以为自己的集合被删了。

    抽取规则: 末段若有 `-`, 取其**最后一段**; 必须含数字且长度 >= 4 才认。
    含数字这一条能把 `nature`、`featured` 这类纯词挡掉 —— 宁可明确报"取不到 ID,
    请改用别的输入形态", 也不要拿一个必然 404 的串去试。
    """
    seg = unquote(str(slug or "")).strip().strip("/")
    if not seg:
        return None
    tail = seg.rsplit("-", 1)[-1] if "-" in seg else seg
    if tail.lower() in _NON_ID_SLUGS:
        return None
    if len(tail) < 4 or not re.search(r"\d", tail):
        return None
    return tail


def _api_key():
    """从环境变量取 API key; 没有返回空串。"""
    return (os.environ.get("PEXELS_API_KEY") or "").strip()


def _photo_url(pid):
    """由 photo id 构造原图直链(公开可预测, 无需密钥)。"""
    return f"https://images.pexels.com/photos/{pid}/pexels-photo-{pid}.jpeg"


def _extract_pid(url):
    """从任意 pexels URL 里抽 photo id; 抽不到返回 None(绝不猜)。"""
    from ..gallery_base import parse_gid

    try:
        return parse_gid(PEXELS, url)
    except Exception:
        return None


@register("pexels")
class PexelsSpider:
    """Pexels: 单张照片 + 搜索/集合页(多图)。

    输入形态与分数:
      * 搜索/集合页 -> SCORE_AGGREGATE_PAGE(多图, 需 API)
      * 照片页 / 图片直链 / 纯 id -> SCORE_ALBUM_PAGE(单图)

    分数不同是有意义的: 同一个 URL 若既能当"照片页"又能当"列表页",
    应当选**资源更多**的那个解释, 否则用户会看到一个本该几百张的结果里
    只有 1 张。
    """

    #: 暴露站点声明, 让它进 `selfcheck_all()` —— 否则 `id_samples` 这些断言
    #: 永远不会被执行, "写下来防回归"就退化成了注释。
    site = PEXELS

    @classmethod
    def match_score(cls, url):
        """⚠️ 认领必须先过站点的**严格**解析与域名比对。

        这里刻意复用 `_match_score` 而不是自己写一遍 `_extract_pid(url)`:

        自己写的版本用 `parse_gid(..., strict=False)`, 而那条**宽松退路**会取
        URL 的末段当 ID —— 于是 `https://xchina.co/photo/id-6aa5136f606fe.html`
        会被解析出 `id-6aa5136f606fe`, 让 Pexels 采集器**抢走 xchina 的 URL**!
        抢走的后果是静默的: 拿一个 13 位十六进制串去请求 pexels 的图片直链,
        站点返回 404/HTML, 采集器报"照片不存在" —— 用户看到的是"这张图没了",
        而不是"选错采集器了"。

        这与 `collectors/__init__.py` 里反复强调的那条是同一件事:
        **认领错了的代价是静默的, 所以判据必须最严**。
        `_match_score` 已经做了 strict 解析 + 候选基址/相册页域名比对, 直接用它。
        """
        raw = (url or "").strip()
        if not raw:
            return None

        # 列表页(搜索/集合)不走 _match_score: 它没有"图集 ID"可解析,
        # 靠路径模板识别。这里必须**独立先判**, 否则搜索页会被判成"无法解析"。
        if _SEARCH_RE.search(raw) or _QUERY_RE.search(raw):
            # ⚠️ 但也要挡掉别的站: 只有 host 真的属于 pexels 才算。
            host = urlparse(raw).netloc.lower()
            if host.endswith("pexels.com"):
                return SCORE_AGGREGATE_PAGE
            return None

        # ⚠️ 纯 ID 输入要额外用 `gid_shape` 把关。
        # `_match_score` 对纯 ID 的判据是通用的 `[0-9A-Za-z_-]{6,}`, 那个正则
        # **什么都能装**: xchina 的 13 位十六进制 gid(`6aa5136f606fe`)也满足它。
        # 两个站点都认领同一个纯 ID 时, 上层按"分数相同则名字字典序"挑 ——
        # `pexels` 排在 `xchina_gallery` 前面, 于是**用户粘一串 xchina 的 ID
        # 会被送去 pexels**, 报"照片不存在"。
        # pexels 的 id 是纯十进制数字, 用本站自己的形状声明把它收紧即可。
        if "://" not in raw and "/" not in raw:
            if not _BARE_ID.fullmatch(raw):
                return None
        return _match_score(PEXELS, raw)

    def crawl(self, url):
        raw = (url or "").strip()
        search = _SEARCH_RE.search(raw)
        if search or _QUERY_RE.search(raw):
            return self._crawl_list(raw, search)
        return self._crawl_single(raw)

    # ---- 单张 ----

    def _crawl_single(self, raw):
        pid = _extract_pid(raw)
        if not pid:
            raise ValueError(
                "无法从该输入解析出 Pexels 照片 ID。可接受的形态:\n"
                "  " + "\n  ".join(PEXELS.input_forms)
            )
        url = _photo_url(pid)
        sess = _session()
        # 探测存在性: 不存在的 id 该站也可能返回 200, 所以认 Content-Type
        # (与 gallery_base.probe 同一套判据, 复用而不是另写一套)。
        status = probe(sess, url, ctype_prefix="image/", accept=IMAGE_ACCEPT)
        if status != "ok":
            raise ValueError(
                f"Pexels 照片 {pid} 不存在或无法访问(探测结果: {status})。"
                f"若确认该 ID 有效, 可能是图片直链命名规则变了。"
            )
        return [{
            "type": "image",
            "url": url,
            "headers": {"Accept": IMAGE_ACCEPT},
            "filename": f"{pid}.jpg",
            "mirrors": [
                f"{url}?auto=compress&w=1200",
                f"{url}?auto=compress&w=800",
            ],
            "source": "pexels_single",
        }]

    # ---- 搜索 / 集合 ----

    def _crawl_list(self, raw, search):
        key = _api_key()
        if not key:
            raise ValueError(
                "Pexels 搜索/集合页需要官方 API key(列表内容无法从 id 推导)。\n"
                "  1) 到 https://www.pexels.com/api/ 免费申请\n"
                "  2) 设置环境变量 PEXELS_API_KEY=<你的 key> 后重启服务\n"
                "  或者直接粘贴**单张照片**的 URL / ID, 那条路径不需要 key。"
            )

        collection = _COLLECTION_RE.search(raw)
        if collection:
            return self._crawl_collection(raw, key, collection.group(1))

        query = None
        if search:
            query = unquote(search.group(1))
        else:
            qs = parse_qs(urlparse(raw).query)
            query = (qs.get("query") or qs.get("q") or [""])[0]

        if not query:
            raise ValueError(
                "无法从该 URL 提取搜索关键词。"
                "支持 https://www.pexels.com/search/{关键词}/ 或 ?query=... 形态。"
            )

        resources = []
        page = 1
        sess = _session()
        while len(resources) < DEFAULT_MAX_ITEMS:
            batch, has_next = self._fetch_page(sess, key, query, page)
            resources.extend(batch)
            if not has_next or not batch:
                break
            page += 1

        if not resources:
            # 空列表会被上层当 failed, 但**原因是"这个词没结果"而不是故障**。
            # 抛带上下文的错, 让用户知道该换关键词而不是去查环境。
            raise ValueError(
                f"Pexels 搜索 {query!r} 没有返回任何照片(可能关键词无匹配)。"
            )
        return resources[:DEFAULT_MAX_ITEMS]

    # ---- 集合页 ----

    def _crawl_collection(self, raw, key, slug):
        """集合页: `GET /v1/collections/{id}` 分页拉取。

        与搜索页的两点不同:
          * 端点是 `/collections/{id}`(不是 `/search`);
          * **必须先从 slug 里抽出 ID** —— 见 `_collection_id` 的说明, 拿 slug 去
            请求只会得到一个与"集合真不存在"无法区分的 404。
        """
        cid = _collection_id(slug)
        if not cid:
            raise ValueError(
                f"无法从 {raw} 里确定集合 ID。\n"
                "  Pexels 的集合页 URL 前面是可读的 slug(如 nature-2sx8z9c), 而官方\n"
                "  集合 API 只认末尾的 ID; 拿 slug 去试会返回『集合不存在』, 与\n"
                "  『我们猜错了 ID』无法区分, 所以这里宁可明确报错。可改用:\n"
                "    · 集合页里任意一张照片的链接(单张路径不需要集合 ID)\n"
                "    · 搜索页 https://www.pexels.com/search/{关键词}/\n"
                "    · 或把集合 ID 直接拼成 "
                "https://www.pexels.com/collections/{slug}-{id}/ 再试"
            )

        resources = []
        page = 1
        sess = _session()
        while len(resources) < DEFAULT_MAX_ITEMS:
            batch, has_next = self._fetch_collection_page(sess, key, cid, page)
            resources.extend(batch)
            if not has_next or not batch:
                break
            page += 1

        if not resources:
            raise ValueError(
                f"Pexels 集合 {cid} 没有返回任何照片(集合可能是空的, 或已被作者删除)。"
            )
        return resources[:DEFAULT_MAX_ITEMS]

    # ---- API 调用 ----

    def _api_get(self, sess, key, path, params, what):
        """调官方 API 并把每种失败翻译成"用户能照做的一句话"。

        单独提出来是因为搜索页与集合页**共用同一套失败语义**: key 错了要提示检查
        环境变量, 限流要说清免费额度, 非 JSON 说明可能被网关拦了 —— 两处各写一遍
        迟早漏掉一种, 而漏掉的那种会以"任务失败但不知道为什么"的形式出现。
        """
        import requests

        try:
            resp = sess.get(
                f"{API_BASE}{path}",
                params=params,
                headers={"Authorization": key, "Accept": "application/json"},
                timeout=30,
            )
        except requests.RequestException as e:
            raise ValueError(f"调用 Pexels API 失败({what}): {e}") from e

        if resp.status_code == 401:
            raise ValueError(
                "Pexels API 拒绝了这个 key(401)。检查 PEXELS_API_KEY 是否填对、"
                "是否已被撤销。"
            )
        if resp.status_code == 404:
            raise ValueError(
                f"Pexels API 说这个 {what} 不存在(404)。"
                "若确认链接有效, 可能是站点改了集合/搜索页的 URL 形态。"
            )
        if resp.status_code == 429:
            raise ValueError(
                "Pexels API 触发限流(429)。官方免费额度是每月 200 次请求, "
                "等一会儿或下个月再试。"
            )
        resp.raise_for_status()
        try:
            return resp.json()
        except json.JSONDecodeError as e:
            raise ValueError(f"Pexels API 返回的不是 JSON({what}): {e}") from e

    def _fetch_page(self, sess, key, query, page):
        """拉一页搜索结果, 返回 (resources, has_next)。"""
        data = self._api_get(
            sess, key, "/search",
            {"query": query, "per_page": API_PAGE_SIZE, "page": page},
            f"搜索 {query!r}",
        )
        return _resources_of(data.get("photos") or []), bool(data.get("next_page"))

    def _fetch_collection_page(self, sess, key, cid, page):
        """拉一页集合内容, 返回 (resources, has_next)。

        ⚠️ 集合端点的返回**形状与搜索页不同**, 而且站点在不同版本里用过两种:
        顶层是 `photos` 或 `media`(后者每项外面还包了一层 `{type: "Photo", ...}`)。
        两种都认 —— 只认一种的话, 另一种会表现为"集合是空的", 而不是报错,
        用户完全无从判断到底是没图还是我们解析错了。
        """
        data = self._api_get(
            sess, key, f"/collections/{cid}",
            {"per_page": API_PAGE_SIZE, "page": page},
            f"集合 {cid}",
        )
        raw_items = data.get("photos")
        if raw_items is None:
            raw_items = data.get("media") or []
        photos = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            # `{type: Photo, photo: {...}}` 与直接的 photo 对象都要能接住
            inner = item.get("photo")
            photos.append(inner if isinstance(inner, dict) else item)
        return _resources_of(photos, source="pexels_collection"), bool(
            data.get("next_page")
        )


def _resources_of(photos, source="pexels_search"):
    """把 API 返回的照片对象列表转成资源列表(两种端点的公共部分)。"""
    out = []
    for p in photos or []:
        if not isinstance(p, dict):
            continue
        src = p.get("src") or {}
        if not isinstance(src, dict):
            src = {}
        # 优先 original, 退化到 large2x/large —— 三级都拿不到就跳过这一条,
        # 而不是塞一个空 URL 进去(下载层会因为空 URL 报一个含糊的错)。
        url = src.get("original") or src.get("large2x") or src.get("large")
        if not url:
            continue
        pid = p.get("id")
        mirrors = [u for u in (src.get("large2x"), src.get("large"),
                               src.get("medium")) if u]
        out.append({
            "type": "image",
            "url": url,
            "headers": {"Accept": IMAGE_ACCEPT},
            "filename": f"{pid}.jpg" if pid else "",
            # 页面/API 自报体积, 可用于体积筛选(下载前就知道大小)
            "size": None,
            "mirrors": mirrors,
            "source": source,
        })
    return out
