"""V48 测试: 本地视频识别 / m3u8 播放分流 / 受控出图。

三条主线, 每条都对着一个"不报错、只是结果不对"的失效面:

1. **视频不污染图片池**(`index["order"]`)。这是最关键的一条: 随机池、往年今日、
   重复分组(dHash)全都在 `order` 上, 下游按图片做缩略图与指纹。视频混进去,
   随机池就会抽出视频然后配一张坏缩略图 —— 而界面上看不出"这条是视频"。

2. **信任锚不被"跨根"顺手放松**。`list_media(root_id=0)` 跨全部已登记根, 于是
   出现了两种"这个目录不在这个根下": 跨根时是**正常**(那目录在别的盘),
   单根时是**越界**(403)。把它们混成"都跳过"就等于把越界探测变成了
   "传什么都返回空列表" —— 和"这里没有视频"在界面上完全一样。

3. **`.m3u8` 会被认出来, 且标记只有一个来源**。前端不自己 `endsWith('.m3u8')`:
   那是"清单与后端可能不一致"的入口, 而那种不一致的表征是"视频点了没反应"。
   所以判据是 `classify()` 单一翻译点, 并断言前端拿到的 `hls` 标记与之一致。
"""

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import core.database as db
import core.localalbums as la

from api import local as L


# ---- 夹具 ------------------------------------------------------------------
@pytest.fixture()
def albums(tmp_path, monkeypatch):
    """一个隔离的库 + 两个已登记根(盘A 有一个子目录, 盘B 没有)。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "la.db")
    monkeypatch.setattr(db, "_conn", None)
    a = tmp_path / "A"
    b = tmp_path / "B"
    (a / "sub").mkdir(parents=True)
    b.mkdir(parents=True)
    (a / "clip.mp4").write_bytes(b"0" * 2048)
    (a / "sub" / "inner.m3u8").write_text("#EXTM3U\n#EXT-X-VERSION:3\n", encoding="utf-8")
    (a / "photo.jpg").write_bytes(b"x")
    (a / "note.txt").write_text("not media", encoding="utf-8")
    (b / "movie.mkv").write_bytes(b"0" * 4096)
    (b / "legacy.rmvb").write_bytes(b"0" * 512)
    la.add_root(str(a), name="盘A")
    la.add_root(str(b), name="盘B")
    return {"a": a, "b": b, "ids": {r["name"]: r["id"] for r in la.roots()}}


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(L.router)
    return TestClient(app)


# ---- 1. 识别 ----------------------------------------------------------------
def test_classify_is_the_single_translation_point():
    """前端不自己判扩展名, 所以这张表就是界面上"能不能播"的唯一依据。"""
    assert la.classify("a.jpg") == {"kind": "image", "hls": False, "playable": True}
    # .m3u8 是 HLS 清单: 认得出、标 hls, 前端据此起 hls.js 而不是原生 video
    assert la.classify("a.m3u8") == {"kind": "video", "hls": True, "playable": True}
    assert la.classify("a.mp4") == {"kind": "video", "hls": False, "playable": True}
    # 认得但本机放不了: **仍然列出来**, 但 playable=False 让界面能说明原因
    assert la.classify("a.rmvb") == {"kind": "video", "hls": False, "playable": False}
    assert la.classify("a.txt") == {"kind": "other", "hls": False, "playable": False}


def test_list_media_mixes_images_and_videos(albums):
    rid = albums["ids"]["盘A"]
    got = la.list_media(rid, "")
    names = {i["name"]: i for i in got["items"]}
    assert "clip.mp4" in names and names["clip.mp4"]["kind"] == "video"
    assert "photo.jpg" in names and names["photo.jpg"]["kind"] == "image"
    # 非媒体文件不该出现(否则"这个文件夹里有什么"就变成了列目录)
    assert "note.txt" not in names
    assert got["counts"] == {"images": 1, "videos": 1, "unplayable": 0}


def test_videos_only_filter(albums):
    rid = albums["ids"]["盘B"]
    got = la.list_media(rid, "", include_images=False)
    assert {i["name"] for i in got["items"]} == {"movie.mkv", "legacy.rmvb"}
    # 放不了的也要计入 unplayable, 否则界面报"共 2 个"却点开一个黑框
    assert got["counts"]["videos"] == 2
    assert got["counts"]["unplayable"] == 1


def test_extension_case_insensitive(albums):
    """Windows 上 `.JPG` 与 `.jpg` 是同一个文件 —— 大写扩展名必须照样认。"""
    (albums["a"] / "UPPER.JPG").write_bytes(b"x")
    got = la.list_media(albums["ids"]["盘A"], "")
    assert "UPPER.JPG" in {i["name"] for i in got["items"]}


# ---- 2. 图片池不被污染(最关键) --------------------------------------------
def test_video_never_enters_the_image_order(albums):
    """`order` 是随机池/往年今日/重复分组的共同数据源, 它必须**只含图片**。

    判据挂在"order 里没有任何视频后缀"上, 而不是"扫到了几张图" ——
    后者在视频被混进去时同样会通过(图片还在), 属于假绿。
    """
    index = la.build_index(albums["ids"]["盘A"])
    names = [p["name"] for files in index["order"].values() for p in files]
    assert names, "图片应该照常进 order"
    video_suffixes = la.VIDEO_SUFFIXES | la.UNPLAYABLE_SUFFIXES
    leaked = [n for n in names if la._suffix(n) in video_suffixes]
    assert leaked == [], f"视频混进了图片索引: {leaked}"


def test_photos_endpoint_stays_image_only(albums):
    """`photos()` 是"照片"语义(缩略图/收藏/重复标记建在它上面), 不能含视频。"""
    got = la.photos(albums["ids"]["盘A"], "")
    assert all(i["name"].endswith((".jpg", ".JPG")) for i in got["items"])


# ---- 3. 跨根 vs 越界 --------------------------------------------------------
def test_list_media_across_all_roots(albums):
    """root_id=0 → 跨全部已登记根(与 random_photos 同一约定)。"""
    got = la.list_media(0, "", include_images=False)
    pairs = {(i["name"], i["root_id"]) for i in got["items"]}
    assert ("clip.mp4", albums["ids"]["盘A"]) in pairs
    assert ("movie.mkv", albums["ids"]["盘B"]) in pairs
    assert got["album"]["multi"] is True
    # 跨根时把参与的根报出来: 同名文件在两个盘里, 用户要能分辨点开的是哪个
    assert {r["id"] for r in got["album"]["roots"]} == set(albums["ids"].values())


def test_cross_root_missing_subdir_is_skipped_not_denied(albums):
    """盘A 有 sub/、盘B 没有: 跨根问 "sub" 时盘B 应**跳过**, 而不是让整个列表 403。"""
    got = la.list_media(0, "sub", include_images=False)
    assert {i["name"] for i in got["items"]} == {"inner.m3u8"}


def test_single_root_escape_is_denied(albums):
    """⚠️ 单根时 `../../..` 是**真的越界**, 必须抛, 不能"跳过"。

    写成跳过的话, `list_media` 就成了"传什么都返回空列表"的函数 ——
    越界探测与"这里没有视频"在界面上完全一样(第 26 条)。
    """
    with pytest.raises(la.RootError) as ei:
        la.list_media(albums["ids"]["盘A"], "../../..")
    assert ei.value.kind == la.KIND_ESCAPES_ROOT


def test_single_root_missing_dir(albums):
    with pytest.raises(la.RootError) as ei:
        la.list_media(albums["ids"]["盘A"], "nope")
    assert ei.value.kind == la.KIND_NOT_FOUND


def test_empty_registry_is_not_an_error():
    """一个根都没登记时返回空列表, 而不是 404/500 ——
    "还没登记任何目录"是正常起点, 不是故障(第 26 条)。"""
    got = la.list_media(0, "", include_images=False)
    assert got["items"] == [] and got["total"] == 0
    assert got["counts"] == {"images": 0, "videos": 0, "unplayable": 0}


# ---- 4. 受控出图 ------------------------------------------------------------
def test_safe_media_allows_video_but_not_arbitrary_files(albums):
    rid = albums["ids"]["盘A"]
    assert la.safe_media(rid, "clip.mp4").name == "clip.mp4"
    assert la.safe_media(rid, "sub/inner.m3u8").name == "inner.m3u8"
    # 非媒体后缀必须拒: 不许变成任意文件读取器
    with pytest.raises(la.RootError) as ei:
        la.safe_media(rid, "note.txt")
    assert ei.value.kind == la.KIND_NOT_IMAGE


def test_safe_media_escape_denied(albums):
    with pytest.raises(la.RootError) as ei:
        la.safe_media(albums["ids"]["盘A"], "../../../Windows/win.ini")
    assert ei.value.kind == la.KIND_ESCAPES_ROOT


def test_video_endpoint_serves_bytes_and_range(albums, client):
    """视频要能拖进度条 ⇒ 必须支持 Range(206)。自己手写 206 正是第 A 条坑。"""
    rid = albums["ids"]["盘A"]
    r = client.get(f"/local/video?root_id={rid}&rel=clip.mp4")
    assert r.status_code == 200 and len(r.content) == 2048
    rg = client.get(f"/local/video?root_id={rid}&rel=clip.mp4",
                    headers={"Range": "bytes=0-99"})
    assert rg.status_code == 206
    assert rg.headers["content-range"] == "bytes 0-99/2048"
    assert len(rg.content) == 100


def test_m3u8_gets_playlist_content_type(albums, client):
    """前端 hls.js 靠这个类型决定要不要起解析器; 错了就是"点了没反应"。"""
    rid = albums["ids"]["盘A"]
    r = client.get(f"/local/video?root_id={rid}&rel=sub/inner.m3u8")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/vnd.apple.mpegurl"


def test_video_endpoint_rejects_non_media_and_escape(albums, client):
    rid = albums["ids"]["盘A"]
    # 415/403/404 三种"拿不到"必须**长得不一样**, 否则没法定位
    assert client.get(f"/local/video?root_id={rid}&rel=note.txt").status_code == 415
    assert client.get(f"/local/video?root_id={rid}&rel=../../x").status_code == 403
    assert client.get(f"/local/video?root_id={rid}&rel=gone.mp4").status_code == 404


def test_media_endpoint_matches_core(client, albums):
    """接口层不该有自己的"视频判定" —— 它必须与 core 的分类一致。"""
    got = client.get(f"/local/media?root_id={albums['ids']['盘B']}").json()
    kinds = {i["name"]: (i["kind"], i["hls"], i["playable"]) for i in got["items"]}
    assert kinds["movie.mkv"] == ("video", False, True)
    assert kinds["legacy.rmvb"] == ("video", False, False)
