"""本地相册集的 HTTP 接口(逻辑全在 `core/localalbums.py`)。

这一层只做三件事, 刻意不做第四件
================================
1. **把代号翻成状态码**: 核心模块抛的是 `RootError(kind=…)`, 这里是唯一把它映射成
   HTTP 的地方。
2. **把绝对路径翻成 URL**: 核心模块只认 `(root_id, rel)`, 出图的两个接口按
   `?root_id=&rel=` 收参 —— 于是"读文件"这件事的输入**永远是一个根 + 一个根内相对
   路径**, 没有一个接口接受裸的绝对路径(那等于开一个任意文件读取口子)。
3. **把缩略图/尺寸这类渲染细节拼进返回**: 前端不该自己拼这些字符串。

不做的那一件: **任何改文件的动作**。这个功能对用户的相册目录是只读的 ——
见 `core/localalbums.py` 顶部的"只读边界"。这里连"删除相册"这样的按钮都对应
`remove_root`(只取消登记), 而不是删目录。
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from core import localalbums as la
from core import thumbs
from models.schemas import (
    LocalFavoriteForgetIn,
    LocalFavoriteIn,
    LocalRootIn,
    LocalRootRenameIn,
)

router = APIRouter()

#: `RootError.kind` -> HTTP 状态码。**只在这一处映射** —— 散在各接口里写
#: `if kind == …`, 就会出现"同一个 kind 在这里是 404、在那里是 400"。
#:
#: 分档的理由: 400 是"这次请求本身的参数不对(换个目录就能用)"; 403 是"你越界了,
#: 换参数也没用"; 404 是"它真的不在那儿"; 409 是"状态冲突(已经登记过)";
#: 415 是"传了非图片 —— 这是调用方的 bug, 别伪装成 404"。
_KIND_STATUS = {
    la.KIND_UNKNOWN_ROOT: 404,
    la.KIND_NOT_FOUND: 404,
    la.KIND_NOT_DIR: 400,
    la.KIND_INSIDE_CACHE: 400,
    la.KIND_DUPLICATE: 409,
    la.KIND_ESCAPES_ROOT: 403,
    la.KIND_NOT_IMAGE: 415,
}


def _fail(exc):
    """把 `RootError` 翻成 HTTPException。

    `detail` 里同时带**代号**和文案: 前端按代号分支, 人看文案。
    (只给文案的话, 前端就只能 match 中文 —— 那是本项目那条假红的成因。)
    """
    status = _KIND_STATUS.get(getattr(exc, "kind", ""), 400)
    return HTTPException(status_code=status, detail={"kind": exc.kind, "message": exc.message})


def _photo_urls(root_id, item):
    """给一张照片补上可以直接塞进 `<img src>` 的两个地址。"""
    rel = quote(str(item["rel"]), safe="/")
    out = dict(item)
    out["root_id"] = root_id
    out["photo_url"] = "/local/photo?root_id=%d&rel=%s" % (root_id, rel)
    out["thumb_url"] = "/local/thumb?root_id=%d&size=%d&rel=%s" % (
        root_id, thumbs.DEFAULT_MAX_SIDE, rel)
    return out


def _album_cover(item):
    """把相册的封面(索引里那一条 `cover` 相对路径)翻成地址。

    ⚠️ 封面是**扫描时顺手记下的**, 不是"现在再去看一眼"。所以它有可能正好是那一张
    已经被用户删掉的照片 —— 这时 `thumb` 会回退、`photo` 会给 404。这不是 bug:
    相册列表是索引视图(见 `core/localalbums.py` 顶部那段"两套口径"),
    而空封面只是少一张缩略图, 不该让整页报错。
    """
    cover = item.get("cover")
    if not cover:
        item["cover_url"] = None
        item["cover_thumb_url"] = None
        return item
    rel = quote(str(cover), safe="/")
    root_id = item.get("root_id")
    item["cover_url"] = "/local/photo?root_id=%s&rel=%s" % (root_id, rel)
    item["cover_thumb_url"] = "/local/thumb?root_id=%s&size=%d&rel=%s" % (
        root_id, thumbs.DEFAULT_MAX_SIDE, rel)
    return item


# ---- 登记 ------------------------------------------------------------------
@router.get("/local/roots")
def list_roots():
    return {"items": la.roots(), "stats": la.stats()}


@router.post("/local/roots")
def add_root(payload: LocalRootIn):
    """登记一个目录。**只登记不扫描** —— 扫描可能要走几万个文件, 不该在一次
    POST 里做; 前端拿到返回后立刻拉 `/local/albums`, 那一步才会真正扫。"""
    try:
        root = la.add_root(payload.path, payload.name, exclude=payload.exclude)
    except la.RootError as exc:
        raise _fail(exc)
    return {"root": root}


@router.patch("/local/roots/{root_id}")
def rename_root(root_id: int, payload: LocalRootRenameIn):
    """改名 / 改排除模式。

    ⚠️ 两个字段是**同一个接口**而不是两个: 它们在界面上是同一张编辑卡里的两个
    输入框, 分成两个接口就得让前端决定"先提交哪个" —— 而改名之后立刻改排除
    模式会作废两次索引。传哪个改哪个, 都不传就是空操作。
    """
    try:
        if payload.exclude is not None:
            la.set_exclude(root_id, payload.exclude)
        # ⚠️ `name is None` 表示"这次不改名字", 与 `name == ""`(清空)必须分开。
        # 一律调 rename_root 的话, 只提交排除模式就会顺手把名字抹掉。
        row = (la.rename_root(root_id, payload.name)
               if payload.name is not None else la.get_root(root_id))
        return {"root": row}
    except la.RootError as exc:
        raise _fail(exc)


@router.delete("/local/roots/{root_id}")
def remove_root(root_id: int):
    """取消登记。**磁盘上的文件一个都不动** —— 见核心模块 `remove_root`。"""
    try:
        return {"removed": la.remove_root(root_id)}
    except la.RootError as exc:
        raise _fail(exc)


@router.post("/local/roots/{root_id}/scan")
def scan_root(root_id: int):
    """强制重扫(绕开 TTL 缓存)。界面上的"重新扫描"按钮走这里。"""
    try:
        return {"index": la.index_brief(la.build_index(root_id, force=True))}
    except la.RootError as exc:
        raise _fail(exc)


# ---- 浏览 ------------------------------------------------------------------
@router.get("/local/albums")
def list_albums(
    root_id: int = Query(None, description="只看某一个相册集; 不传就是全部"),
    q: str = Query(None, description="按相册名/相对路径过滤"),
    sort: str = Query("mtime", description="name | path | photos | bytes | mtime"),
    order: str = Query("desc", description="asc | desc"),
    min_photos: int = Query(1, ge=0),
    refresh: bool = Query(False, description="绕开索引缓存重扫"),
):
    try:
        data = la.albums(root_id=root_id, q=q, sort=sort, order=order,
                         min_photos=min_photos, refresh=refresh)
    except la.RootError as exc:
        raise _fail(exc)
    data["items"] = [_album_cover(a) for a in data["items"]]
    return data


@router.get("/local/photos")
def list_photos(
    root_id: int = Query(...),
    rel: str = Query("", description="相册目录相对根的路径; 空串 = 根目录本身"),
    q: str = Query(None),
    sort: str = Query("name"),
    order: str = Query("asc"),
    offset: int = Query(0, ge=0),
    limit: int = Query(la.MAX_PAGE_SIZE, ge=1, le=la.MAX_PAGE_SIZE),
):
    try:
        data = la.photos(root_id, rel=rel, q=q, sort=sort, order=order,
                         offset=offset, limit=limit)
    except la.RootError as exc:
        raise _fail(exc)
    data["items"] = [_photo_urls(root_id, p) for p in data["items"]]
    return data


@router.get("/local/random")
def random_photos(
    count: int = Query(60, ge=1, le=la.MAX_PAGE_SIZE),
    root_id: int = Query(None),
    album: str = Query(None, description="只从一个相册里抽(配合 root_id)"),
    mode: str = Query("album", description="album = 各相册机会均等; photo = 各张机会均等"),
    page: int = Query(0, ge=0),
    seed: int = Query(None, description="同一个 seed + 递增 page = 一副可以一直往下翻的牌"),
    min_bytes: int = Query(0, ge=0, description="忽略小于这个体积的文件(过滤缩略图/图标)"),
    favorites_only: bool = Query(False),
):
    """随机池。⚠️ 读的是**索引快照**, 不是实时目录 —— 返回里带 `at`(快照时刻)与
    `pool`(池里一共多少张), 界面必须显示, 否则"怎么没抽到我刚放的那张"无从解释。"""
    pair = (root_id, album) if (album and root_id) else None
    try:
        data = la.random_photos(count=count, root_id=root_id, album=pair, mode=mode,
                                page=page, seed=seed, min_bytes=min_bytes,
                                favorites_only=favorites_only)
    except la.RootError as exc:
        raise _fail(exc)
    data["items"] = [_photo_urls(p["root_id"], p) for p in data["items"]]
    return data


# ---- 出图 ------------------------------------------------------------------
# ⚠️ 这两个接口收的是 `(root_id, rel)` 而**不是绝对路径**。这不是风格问题:
# 收绝对路径的接口就是在给"任意文件读取"开门, 而它唯一的挡板只有一句
# "请传合法的路径" —— 而那句话不在代码里。
@router.get("/local/photo")
def serve_photo(root_id: int = Query(...), rel: str = Query(...)):
    try:
        path = la.safe_photo(root_id, rel)
    except la.RootError as exc:
        raise _fail(exc)
    return FileResponse(path)


@router.get("/local/thumb")
def serve_thumb(
    root_id: int = Query(...),
    rel: str = Query(...),
    size: int = Query(thumbs.DEFAULT_MAX_SIDE, ge=64, le=1024),
):
    """缩略图; **生成不了就回退原图**。

    与 `/files/thumb` 同一条取舍: 缩略图是加速手段, 没有 ffmpeg 或这张图解不开时
    用户仍然该**看得到内容**, 只是慢一点。给一个 404 就等于把人要的信息拿走了 ——
    而前端多半会用 `onerror` 兜住它, 于是连"图挂了"都看不见(本项目第 11 条坑)。
    """
    try:
        path = la.safe_photo(root_id, rel)
    except la.RootError as exc:
        raise _fail(exc)
    thumb = thumbs.ensure_thumb(la.thumb_base(), path, max_side=size)
    resp = FileResponse(thumb if thumb is not None else path)
    if thumb is not None:
        resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


# ---- 收藏 / 统计 -----------------------------------------------------------
@router.get("/local/on-this-day")
def on_this_day(
    root_id: int = Query(None, description="只看某一个相册集; 不传就是全部"),
    per_year: int = Query(6, ge=1, le=30),
    limit: int = Query(60, ge=1, le=120),
):
    """"往年的今天" —— 同月同日、但不是今年的照片, 按年份分组。

    ⚠️ 口径是**文件修改时间**, 不是拍摄时间(见 `la.on_this_day` 的说明)。
    接口刻意**不接受** `today` 参数: 那只是给测试用的注入点, 暴露出去会让
    "今天"变成可以伪造的输入, 而这个功能唯一的锚点就是真实的今天。
    """
    try:
        data = la.on_this_day(root_id=root_id, per_year=per_year, limit=limit)
    except la.RootError as exc:
        raise _fail(exc)
    for group in data["years"]:
        group["photos"] = [_photo_urls(p["root_id"], p) for p in group["photos"]]
    return data


@router.get("/local/duplicates")
def list_duplicates(
    root_id: int = Query(..., description="相册集 id"),
    rel: str = Query("", description="相册相对路径; 空串 = 根目录下那些照片"),
    threshold: int = Query(None, ge=0, le=64, description="海明距离阈值, 默认 4"),
    limit: int = Query(la.MAX_DUPLICATE_SCAN, ge=1, le=la.MAX_DUPLICATE_SCAN),
):
    """一个相册里**疑似重复**的照片对。**只标记, 绝不删任何文件。**

    返回里的 `reason` 必须被前端当真: `no-decoder` 表示"本机没有 ffmpeg, 一张
    都没算" —— 那是"没验过", 不是"没有重复"。界面上这两种情况要显示不同的话。
    """
    try:
        data = la.duplicates(root_id, rel, threshold=threshold, limit=limit)
    except la.RootError as exc:
        raise _fail(exc)
    data["pairs"] = [
        {
            "distance": p["distance"],
            "a": _photo_urls(root_id, p["a"]),
            "b": _photo_urls(root_id, p["b"]),
        }
        for p in data["pairs"]
    ]
    return data


@router.get("/local/favorites")
def list_favorites(offset: int = Query(0, ge=0),
                   limit: int = Query(la.MAX_PAGE_SIZE, ge=1, le=la.MAX_PAGE_SIZE),
                   min_bytes: int = Query(0, ge=0)):
    """收藏页。

    `items` 是**现在还能出图**的那些; `stale` 是解析不出来的(文件没了、
    或那个目录已经不再被登记)。⚠️ 失效那些**不静默丢掉** —— 少了东西却不说,
    用户只会以为收藏功能坏了。
    """
    data = la.favorites_view(offset=offset, limit=limit, min_bytes=min_bytes)
    data["items"] = [dict(p, favorite=True,
                          photo_url="/local/photo?root_id=%d&rel=%s" % (
                              p["root_id"], quote(str(p["rel"]), safe="/")),
                          thumb_url="/local/thumb?root_id=%d&size=%d&rel=%s" % (
                              p["root_id"], thumbs.DEFAULT_MAX_SIDE,
                              quote(str(p["rel"]), safe="/")))
                     for p in data["items"]]
    return data


@router.post("/local/favorite")
def set_favorite(payload: LocalFavoriteIn):
    try:
        return la.set_favorite(payload.root_id, payload.rel, payload.value)
    except la.RootError as exc:
        raise _fail(exc)


@router.post("/local/favorite/forget")
def forget_favorite(payload: LocalFavoriteForgetIn):
    """删掉一条**失效**的收藏记录(只删我们库里那一行, 不碰磁盘)。"""
    return {"removed": la.forget_favorite(payload.path)}


@router.get("/local/stats")
def local_stats():
    return la.stats()


@router.post("/local/thumbs/prune")
def prune_thumbs():
    """清掉"现在看不见的照片"的缩略图。缓存的唯一作用是加速, 只增不减最终会变成
    用户磁盘上看不见的账。"""
    return {"removed": la.prune_thumbs(), "cache": la.thumb_cache_info()}
