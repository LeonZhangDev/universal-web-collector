"""V36 测试: 分片级断点续传的**跨任务复用** + 顺手修掉的 HLS `info` 回填。

要解决的问题
============
HLS/DASH 的分片缓存原先只躺在目标路径旁边(`.<stem>.parts/`)。取消之后它会
留在那里 —— 只要**下次还是同一个相册名、同一个文件名**, 就还能接着下。可用户
一改命名模板、或者站点换了相册标题, 目标路径就变了, 谁也找不到那堆分片了:
几百 MB 的字节白下。

做法是把分片缓存也交给 `core/partials.py` 那个按 **URL** 寻址的暂存区(与单文件
`.part` 共用 TTL/预算/淘汰), 于是"文件落在哪"与"字节存在哪"彻底解耦。

两条判据必须钉死
================
1. **换路径仍能续传** —— 否则这次改动没有意义;
2. **清单变了不许复用旧分片** —— 单文件有 `.partsrc` 挡这个, 分片清单的对应物是
   `fingerprint`。顺序复用错批次的分片拼出来是"长度对得上、能播一部分、错得没有
   声响"的字节, 比重新下一遍糟得多。
"""
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import pytest
import requests

import core.partials as partials
import downloaders.video as V
from downloaders.dash import parse_mpd
from downloaders.video import VideoDownloader


# ==================================================================
# 夹具
# ==================================================================

@pytest.fixture()
def stage(tmp_path, monkeypatch):
    """独立下载根(= 独立暂存区)+ 放开体积门槛。"""
    dl = tmp_path / "dl"
    dl.mkdir()
    # 暂存区每次调用重新读 settings.download_dir, 所以指到临时目录就够了
    monkeypatch.setattr(partials.settings, "download_dir", dl)
    # 阈值默认 256KB: 夹具里写几十 KB 只为触发搬运, 阈值本身的判据由
    # `test_park_segments_skips_a_bundle_below_min_bytes` 单独钉住
    monkeypatch.setattr(partials.settings, "partial_min_bytes", 0)
    monkeypatch.setattr(partials.settings, "partial_staging", True)
    # 清掉上一条用例留下的"搬家失败"记录, 否则断言里的诊断信息会指向别处
    partials._LAST_PARK_ERROR = None
    return dl


def _mkparts(d, n=3, size=100, base=65):
    """造一个"下了一部分"的分片目录: `000000.part` ... 每片 size 字节。"""
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (d / f"{i:06d}.part").write_bytes(bytes([base + i]) * size)
    return d


# ==================================================================
# 1. 指纹: 回答"这还是不是同一份清单"
# ==================================================================

def test_fingerprint_is_order_sensitive():
    """顺序即内容 —— 同一个 URL 集合换个次序就是另一份字节。"""
    assert partials.fingerprint(["a", "b"]) != partials.fingerprint(["b", "a"])


def test_fingerprint_does_not_conflate_adjacent_paths():
    """拼接必须带分隔符: 否则 `["ab","c"]` 与 `["a","bc"]` 同哈希。

    那正是"清单错位却指纹相同"的入口 —— 而指纹相同的后果是照单复用。
    """
    assert partials.fingerprint(["ab", "c"]) != partials.fingerprint(["a", "bc"])


def test_fingerprint_covers_byte_ranges():
    """DASH SegmentBase 的分片是 `(url, (start, end))`: 区间也必须进指纹。

    只哈希 URL 的话, 同一个文件换了区间就是另一份内容, 而复用旧缓存不会报错。
    """
    one = [("https://h/f.mp4", (0, 100))]
    two = [("https://h/f.mp4", (100, 200))]
    assert partials.fingerprint(one) != partials.fingerprint(two)
    assert partials.fingerprint(one) != partials.fingerprint(["https://h/f.mp4"])


# ==================================================================
# 2. 收进暂存区 / 取回
# ==================================================================

def test_seg_dir_is_addressed_by_the_manifest_url(stage):
    a, b = "https://h/720.m3u8", "https://h/1080.m3u8"
    assert partials.seg_dir(a) != partials.seg_dir(b)
    assert partials.seg_dir(a).name.endswith(partials.SEG_SUFFIX)
    # 与单文件的落点彻底分开: 同一个 URL 既当文件又当清单也不会互相顶掉
    assert partials.seg_dir(a) != partials.stage_path(a)


def test_park_segments_then_take_round_trips(stage):
    """核心闭环: 收进暂存区 -> 换个目标目录取回, 字节一片不少。"""
    addr = "https://h/720.m3u8"
    segs = ["https://h/ts/1.ts", "https://h/ts/2.ts", "https://h/ts/3.ts"]
    d = _mkparts(stage / "相册A" / ".v.parts")
    # ⚠️ 断言里带上 `last_error`: 搬家失败时产品**故意不抛错**(原地那份保持不动,
    # 最坏只是下次从头下), 于是失败信息只能从 `stats()` 里取。Windows 上目录
    # rename 会被"另一个句柄还开着"打断 —— 那是环境, 不是回归, 但必须能看见。
    moved = partials.park_segments(addr, d, segs)
    assert moved == 300, f"没收下: {partials.stats().get('last_error')}"
    assert not d.exists(), "park 是**搬走**不是复制"

    st = partials.stats()
    assert st["bundles"] == 1 and st["files"] == 0 and st["bytes"] == 300

    dest = stage / "换个名字的相册" / ".v.parts"
    assert partials.take_segments(addr, dest, segs) == 300
    assert sorted(p.name for p in dest.iterdir()) == [
        "000000.part", "000001.part", "000002.part",
    ]
    assert partials.stats()["count"] == 0, "取回后索引里不该还留着它"


def test_park_segments_merges_two_partial_batches(stage):
    """两份"下了一部分"(旧路径旁边 + 暂存区)合起来才是省得最多的那份。

    整体替换必然丢掉其中一半 —— 这也正是不能用 `os.replace` 直接覆盖的原因。
    """
    addr = "https://h/720.m3u8"
    segs = [f"https://h/{i}.ts" for i in range(4)]
    partials.park_segments(addr, _mkparts(stage / "A", 2), segs)      # 只有 0,1
    partials.park_segments(addr, _mkparts(stage / "B", 4), segs)      # 0..3

    st = partials.stats()
    assert st["bundles"] == 1, "同一个清单只该有一条索引"
    assert st["bytes"] == 400, "合并后应是 4 片 400 字节, 不是取其一"

    dest = stage / "C"
    partials.take_segments(addr, dest, segs)
    assert len(list(dest.glob("*.part"))) == 4


def test_take_segments_drops_the_bundle_when_the_manifest_moved_on(stage):
    """清单 URL 没变但分片换代了 -> **整份丢弃**, 不许按序号勉强复用。

    这是整个改动里唯一会"下出坏文件"的口子: 复用了另一批次的第 3 片, 拼出来的
    东西长度对、能播一部分, 而每一环都报告成功。
    """
    addr = "https://h/720.m3u8"
    partials.park_segments(addr, _mkparts(stage / "A", 2), ["https://h/a1.ts"])
    assert partials.take_segments(
        addr, stage / "out" / ".v.parts", ["https://h/b1.ts"]
    ) == 0
    assert partials.stats()["count"] == 0, "指纹不符的缓存必须被丢掉"
    assert not (stage / "out" / ".v.parts").exists()


def test_take_segments_ignores_a_bundle_of_another_manifest(stage):
    segs = ["https://h/x.ts"]
    partials.park_segments("https://h/1.m3u8", _mkparts(stage / "p", 1), segs)
    assert partials.take_segments("https://h/2.m3u8", stage / "out", segs) == 0
    assert partials.stats()["count"] == 1, "别人的缓存不许被取走"


def test_park_segments_skips_a_bundle_below_min_bytes(stage, monkeypatch):
    monkeypatch.setattr(partials.settings, "partial_min_bytes", 10_000)
    assert partials.park_segments(
        "https://h/720.m3u8", _mkparts(stage / "A", 2), ["https://h/1.ts"]
    ) == 0
    assert partials.stats()["count"] == 0
    assert (stage / "A").is_dir(), "没收下时原地那份保持不动"


def test_park_segments_is_a_noop_when_staging_is_disabled(stage, monkeypatch):
    monkeypatch.setattr(partials.settings, "partial_staging", False)
    d = _mkparts(stage / "A", 2)
    assert partials.park_segments("https://h/720.m3u8", d, ["https://h/1.ts"]) == 0
    assert d.is_dir() and partials.stats()["enabled"] is False


def test_park_segments_without_a_manifest_url_is_refused(stage):
    d = _mkparts(stage / "A", 2)
    assert partials.park_segments("", d, []) == 0
    assert d.is_dir()


# ==================================================================
# 3. 清单指纹标记(分片目录版 `.partsrc`)
# ==================================================================

def test_ensure_fingerprint_writes_a_marker_then_reuses_the_dir(stage):
    segs = ["https://h/a.ts", "https://h/b.ts"]
    d = stage / "相册" / ".v.parts"
    # 第一次(目录还不存在): 写下标记放行
    assert partials.ensure_fingerprint(d, segs) is True
    assert (d / partials.FP_MARKER).is_file()
    # 下了几片之后再来一次: 认得出, 分片一片不动
    _mkparts(d, 2)
    assert partials.ensure_fingerprint(d, segs) is True
    assert len(list(d.glob("*.part"))) == 2


def test_ensure_fingerprint_wipes_a_dir_of_unknown_origin(stage):
    """有内容却没标记 = 来路不明, 一律清掉。

    与 `base.py` 的 `.partsrc` 是同一条判断: 宁可重下, 也不拿一批来路不明的分片
    去拼。区别只是这里多一个"崩在写标记之前"的窗口 —— 那种情况同样无从证明。
    """
    d = _mkparts(stage / "相册" / ".v.parts", 2)
    assert partials.ensure_fingerprint(d, ["https://h/a.ts"]) is False
    assert not d.exists()


def test_ensure_fingerprint_wipes_when_the_manifest_moved_on(stage):
    d = stage / "相册" / ".v.parts"
    assert partials.ensure_fingerprint(d, ["https://h/a.ts"]) is True
    _mkparts(d, 1)
    assert partials.ensure_fingerprint(d, ["https://h/b.ts"]) is False
    assert not d.exists()


def test_ensure_fingerprint_prepares_an_empty_dir(stage):
    """空目录不算来路不明: 写下标记放行(否则第一次下载永远过不去)。"""
    d = stage / "相册" / ".v.parts"
    assert partials.ensure_fingerprint(d, ["https://h/a.ts"]) is True
    assert (d / partials.FP_MARKER).is_file()
    assert list(d.glob("*.part")) == []


# ==================================================================
# 4. 清理: TTL / 预算 / 残骸 / 连带
# ==================================================================

def test_sweep_evicts_an_expired_bundle(stage, monkeypatch):
    monkeypatch.setattr(partials.settings, "partial_ttl_hours", 1)
    addr = "https://h/720.m3u8"
    partials.park_segments(addr, _mkparts(stage / "A", 2), ["https://h/1.ts"])
    n, freed = partials.sweep(now=time.time() + 7200)
    assert (n, freed) == (1, 200)
    assert partials.stats()["count"] == 0


def test_sweep_reclaims_orphans_missing_from_the_index(stage):
    """磁盘上有、索引里没有的残骸也要清。

    没有 URL 就无从复用, 也不会被任何一次按索引走的清理命中 —— 它是最容易永久
    占着磁盘的那一类(索引写在搬动之后, 崩在中间就会留下一个)。
    """
    orphan = stage / "_meta" / "partial" / ("a" * 40 + partials.SEG_SUFFIX)
    orphan.mkdir(parents=True, exist_ok=True)
    (orphan / "000000.part").write_bytes(b"x" * 50)

    n, freed = partials.sweep()
    assert (n, freed) == (1, 50)
    assert not orphan.exists()


def test_sweep_is_a_noop_on_an_empty_stage(stage):
    assert partials.sweep() == (0, 0)
    assert not partials.stage_dir().exists(), "什么都没有时别把目录建出来"


def test_clear_by_origin_reclaims_the_track_bundles(stage):
    """DASH 的两条轨按 `清单url#video` / `#audio` 寻址, 只精确匹配够不着它们。

    用户"删任务带文件"之后这些字节本来会永远躺在暂存区里(界面上看不见, 也不会
    被任何一次清理命中) —— 索引里的 `origin` 就是为此存在的。
    """
    mpd = "https://h/m.mpd"
    for kind in ("video", "audio"):
        partials.park_segments(
            f"{mpd}#{kind}", _mkparts(stage / kind, 1),
            [f"https://h/{kind}1.m4s"], origin=mpd,
        )
    assert partials.stats()["bundles"] == 2
    n, freed = partials.clear(mpd)
    assert (n, freed) == (2, 200)
    assert partials.stats()["count"] == 0


def test_clear_all_also_removes_orphans(stage):
    """ "释放空间"这个动作必须连索引里没有的残骸一起覆盖。"""
    partials.park_segments(
        "https://h/720.m3u8", _mkparts(stage / "A", 1), ["https://h/1.ts"]
    )
    orphan = stage / "_meta" / "partial" / ("b" * 40 + partials.PART_SUFFIX)
    orphan.write_bytes(b"y" * 30)
    n, freed = partials.clear()
    assert n == 2 and freed == 130
    assert not orphan.exists()


def test_stats_reports_bundles_and_files_separately(stage):
    partials.park_segments(
        "https://h/720.m3u8", _mkparts(stage / "A", 2), ["https://h/1.ts"]
    )
    part = stage / "b.jpg.part"
    part.write_bytes(b"z" * 10)
    partials.park(part, "https://h/b.jpg")
    st = partials.stats()
    assert (st["count"], st["bundles"], st["files"]) == (2, 1, 1)
    assert {i["kind"] for i in st["items"]} == {"segments", "file"}


# ==================================================================
# 5. 端到端: 换目标路径仍能续传 / 换批次绝不混拼
# ==================================================================

class _FakeResp:
    """`_fetch_segments` 需要 iter_content/close, `inspect_playlist` 需要 text。"""

    def __init__(self, body, status=200):
        self._body = body if isinstance(body, (bytes, bytearray)) else body.encode()
        self.text = self._body.decode("utf-8", "replace")
        self.content = bytes(self._body)
        self.status_code = status
        self.headers = {}
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, size):
        for i in range(0, len(self._body), size):
            yield self._body[i:i + size]

    def close(self):
        self.closed = True


class _FakeSession:
    """URL -> 响应体。`missing` 里的 URL 一律 404。"""

    def __init__(self, routes=None, missing=()):
        self.routes = dict(routes or {})
        self.missing = set(missing)
        self.calls = []

    def get(self, url, headers=None, timeout=None, **kw):
        self.calls.append(url)
        if url in self.missing:
            return _FakeResp("", status=404)
        body = self.routes.get(url)
        if body is None:
            return _FakeResp("", status=404)
        return _FakeResp(body)


def _manifest(segs):
    lines = ["#EXTM3U", "#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-TARGETDURATION:5"]
    for u in segs:
        lines += ["#EXTINF:5.0,", u]
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines)


def _downloader(monkeypatch, session):
    """builtin 引擎 + 没有 ffmpeg: 只走内置分片器, 不依赖本机有没有 ffmpeg。"""
    monkeypatch.setattr(V.settings, "video_engine", "builtin")
    monkeypatch.setattr(V, "find_ffmpeg", lambda: None)
    monkeypatch.setattr(V.settings, "segment_retries", 1)
    monkeypatch.setattr(V.settings, "segment_min_interval", 0)
    monkeypatch.setattr(V.settings, "segment_max_interval", 0)
    monkeypatch.setattr(V.settings, "segment_concurrency", 4)
    d = VideoDownloader()
    d.session = session
    return d


def test_hls_segment_cache_survives_a_target_path_change(stage, monkeypatch, tmp_path):
    """**这次改动的全部意义**: 同一个清单, 换个相册目录接着下, 已下好的分片不重下。"""
    manifest = "https://h/720.m3u8"
    segs = [f"https://h/ts/{i:05d}.ts" for i in range(6)]
    bodies = {u: f"P{i}".encode() for i, u in enumerate(segs)}

    # ---- 第一趟: 最后一片 404 -> 失败, 但前 5 片必须进暂存区 ----
    s1 = _FakeSession({manifest: _manifest(segs), **bodies}, missing={segs[5]})
    save = tmp_path / "out"
    with pytest.raises(RuntimeError, match="分片"):
        _downloader(monkeypatch, s1).download(
            manifest, save_dir=save, filename="相册A/x.mp4"
        )
    d = save / "相册A" / ".x.parts"
    assert not d.exists(), "失败时缓存应被搬进暂存区, 不是留在原地"
    st = partials.stats()
    assert st["bundles"] == 1 and st["bytes"] == 2 * 5

    # ---- 第二趟: 换个相册名/目录 -> 从暂存区接着下 ----
    s2 = _FakeSession({manifest: _manifest(segs), **bodies})
    path, _sha = _downloader(monkeypatch, s2).download(
        manifest, save_dir=save, filename="换个名字/重命名后的文件.mp4"
    )
    assert path.read_bytes() == b"".join(bodies[u] for u in segs)
    fetched = [u for u in s2.calls if u.endswith(".ts")]
    assert fetched == segs[5:], f"前五片必须来自暂存区, 实际又下了 {fetched}"
    assert partials.stats()["count"] == 0, "下完就该把暂存条目摘掉"


def test_hls_never_mixes_two_batches_at_the_same_target_path(stage, monkeypatch,
                                                              tmp_path):
    """同一个输出路径 + 同一个清单 URL, 但分片换代了 -> 不许把两批拼在一起。

    这是 `fingerprint` 存在的理由。没有它的话, 第一趟留下的 `000001.part` 会被
    第二趟按序号复用, 产物是两批字节的混合体: 长度够、能播一部分、不报错。
    """
    manifest = "https://h/720.m3u8"
    old = [f"https://h/old/{i}.ts" for i in range(4)]
    new = [f"https://h/new/{i}.ts" for i in range(4)]
    old_bodies = {u: b"OLD" for u in old}
    new_bodies = {u: b"NEW" for u in new}

    save = tmp_path / "out"
    s1 = _FakeSession({manifest: _manifest(old), **old_bodies}, missing={old[2]})
    with pytest.raises(RuntimeError):
        _downloader(monkeypatch, s1).download(
            manifest, save_dir=save, filename="album/x.mp4"
        )
    # 第一趟以失败告终: 缓存进了暂存区, 原地不该留下旧的标记目录
    assert not (save / "album" / ".x.parts").exists()

    # 第二趟: 清单内容换成新分片
    s2 = _FakeSession({manifest: _manifest(new), **new_bodies})
    path, _sha = _downloader(monkeypatch, s2).download(
        manifest, save_dir=save, filename="album/x.mp4"
    )
    body = path.read_bytes()
    assert body == b"NEW" * 4, f"产物混进了旧批次: {body!r}"
    assert b"OLD" not in body


def test_hls_fills_the_callers_info_dict(stage, monkeypatch, tmp_path):
    """调用方传进来的 `info` 必须被回填(`resolved_url` / `content_type`)。

    ⚠️ 这条以前是**坏的**: `_download_m3u8` 里预检结果的局部变量与参数重名,
    把任务管理器传进来的那只 dict 直接顶掉了 —— 于是 HLS 视频的 `resolved_url`
    一直是空的(DASH 与直链都正常)。不报错、不影响下载, 只是那条溯源信息静静地
    没了。测试放在这里是为了"下次有人重命名时立刻炸"。
    """
    manifest = "https://h/720.m3u8"
    segs = ["https://h/ts/1.ts", "https://h/ts/2.ts"]
    bodies = {u: b"S" for u in segs}
    s = _FakeSession({manifest: _manifest(segs), **bodies})

    info = {}
    path, _sha = _downloader(monkeypatch, s).download(
        manifest, save_dir=tmp_path / "out", info=info
    )
    assert path.suffix == ".ts"
    assert info.get("resolved_url") == manifest
    assert info.get("content_type") == "video/mp2t"


# ==================================================================
# 6. 标签层级与颜色 (database + /library/tags)
# ==================================================================

@pytest.fixture()
def tagsdb(tmp_path, monkeypatch):
    """独立库 + 任务/资源工厂。标签测试只碰库, 不需要下载根。"""
    import core.database as db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "v36.db")
    monkeypatch.setattr(db, "_conn", None)

    class Ctx:
        @staticmethod
        def task(name="相册A"):
            return db.create_task("https://example.com/a", "generic", name=name)

        @staticmethod
        def done(task_id, name, url=None):
            return db.add_resource(
                task_id, "image", url or f"https://example.com/{name}",
                status="done", local_path=f"/tmp/{name}",
            )

    return Ctx()


def _client():
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    import api.tasks as T

    app = FastAPI()
    app.include_router(T.router)
    return TestClient(app)


def test_tag_parent_and_depth_are_derived_from_the_name():
    """层级是名字里算出来的, 不是一张树表 —— 没有 `/` 就是顶层。"""
    import core.database as db

    assert db.tag_parent("系列/角色A") == "系列"
    assert db.tag_parent("系列/角色A/番外") == "系列/角色A"
    assert db.tag_parent("系列") == ""
    assert db.tag_depth("系列") == 0
    assert db.tag_depth("系列/角色A") == 1


def test_normalize_tag_rejects_trailing_separator_and_bad_depth():
    """⚠️ 尾随/前导 `/` 与超深层级必须在归一化阶段就被挡住。

    放进去的话 `a/` 与 `a` 互为父子、树上多出一个空节点, 而这两条记录
    在筛选时又是两个不同的标签 —— 属于"能用但结果不对"。
    """
    import core.database as db

    with pytest.raises(ValueError):
        db.normalize_tag("系列/")
    with pytest.raises(ValueError):
        db.normalize_tag("/系列")
    with pytest.raises(ValueError):
        db.normalize_tag("a/b/c/d/e")
    assert db.normalize_tag("系列/角色A") == "系列/角色A"


def test_normalize_color_rejects_unknown_keys():
    """非法颜色报错而不是悄悄退回默认色: 界面变灰比报错更难查。"""
    import core.database as db

    assert db.normalize_color("") == ""
    assert db.normalize_color("RED") == "red"
    with pytest.raises(ValueError):
        db.normalize_color("chartreuse")
    assert "red" in db.TAG_COLORS


def test_all_tags_fills_parent_nodes_and_tree_counts(tagsdb):
    """父标签自己没资源也得补齐, 否则树上缺一层。"""
    import core.database as db

    t = tagsdb.task()
    r1 = tagsdb.done(t, "a.jpg")
    r2 = tagsdb.done(t, "b.jpg")
    db.add_tags([r1], ["系列/角色A"])
    db.add_tags([r2], ["系列/角色B"])

    rows = {r["tag"]: r for r in db.all_tags()}
    assert set(rows) == {"系列", "系列/角色A", "系列/角色B"}
    parent = rows["系列"]
    assert parent["n"] == 0, "父标签自己没有资源"
    assert parent["n_tree"] == 2, "但连子标签一共有 2 项"
    assert parent["depth"] == 0
    assert rows["系列/角色A"]["depth"] == 1
    assert rows["系列/角色A"]["parent"] == "系列"


def test_all_tags_counts_each_resource_once_per_tag(tagsdb):
    """一个资源带三个标签, 每个标签各算它一次 —— 不是被算三次。"""
    import core.database as db

    t = tagsdb.task()
    r = tagsdb.done(t, "a.jpg")
    db.add_tags([r], ["x", "y", "z"])
    rows = {i["tag"]: i for i in db.all_tags()}
    assert rows["x"]["n"] == 1 and rows["y"]["n"] == 1 and rows["z"]["n"] == 1


def test_tag_color_survives_resource_deletion(tagsdb):
    """颜色是**标签**的属性, 不随资源删除而丢。"""
    import core.database as db

    t = tagsdb.task()
    r = tagsdb.done(t, "a.jpg")
    db.add_tags([r], ["系列"])
    db.set_tag_color("系列", "red")

    db.delete_resource(r)
    assert db.all_tags() == []
    assert db.tag_colors() == {"系列": "red"}, "资源没了, 颜色还在"

    r2 = tagsdb.done(t, "b.jpg")
    db.add_tags([r2], ["系列"])
    assert db.all_tags()[0]["color"] == "red", "重新打上同一个标签, 颜色得回来"


def test_set_tag_color_clear_removes_the_row(tagsdb):
    """清色 = 删行, 不留 `color=''` 的空壳(否则 tag_meta 会越涨越大)。"""
    import core.database as db

    db.set_tag_color("tmp", "blue")
    assert db.tag_colors() == {"tmp": "blue"}
    assert db.set_tag_color("tmp", "") == ""
    assert db.tag_colors() == {}
    assert db.query("SELECT COUNT(*) AS n FROM tag_meta")[0]["n"] == 0


def test_tag_color_is_case_insensitive(tagsdb):
    """`tag_meta.tag` 是 NOCASE 列, 所以 `系列` 与 `X` 的色都得取得回来。

    ⚠️ `tag_colors()` 的键取小写 —— Python 字典不是 NOCASE, 不折一下就会
    "库里查得到颜色、字典里查不到"。
    """
    import core.database as db

    db.set_tag_color("Series", "cyan")
    assert db.tag_colors() == {"series": "cyan"}
    assert db.set_tag_color("SERIES", "pink") == "pink"
    assert db.tag_colors() == {"series": "pink"}, "大小写不同不该留下两行"


def test_library_filter_by_parent_includes_children(tagsdb):
    """含子标签 = 一次**带分隔符的**前缀匹配。"""
    import core.database as db

    t = tagsdb.task()
    r1 = tagsdb.done(t, "a.jpg")
    r2 = tagsdb.done(t, "b.jpg")
    r3 = tagsdb.done(t, "c.jpg")
    db.add_tags([r1], ["系列/角色A"])
    db.add_tags([r2], ["系列二/角色B"])
    db.add_tags([r3], ["系列/角色A/番外"])

    assert db.library_count(tag="系列", tag_children=False) == 0,         "父标签自己没资源时, 精确匹配应为 0"
    assert db.library_count(tag="系列", tag_children=True) == 2,         "含子标签: 角色A 与 角色A/番外 都算"


def test_library_filter_refuses_a_loose_prefix(tagsdb):
    """前缀必须带分隔符: `系列` 不能把 `系列二` 一起捞进来。"""
    import core.database as db

    t = tagsdb.task()
    r = tagsdb.done(t, "a.jpg")
    db.add_tags([r], ["系列二/角色B"])

    assert db.library_count(tag="系列", tag_children=True) == 0
    assert db.library_count(tag="系列二", tag_children=True) == 1


def test_library_lists_the_tag_children_flag(tagsdb):
    """接口层也要能把 `tag_children` 传到库里(否则开关是个点了没用的钮)。"""
    import core.database as db

    t = tagsdb.task()
    r = tagsdb.done(t, "a.jpg")
    db.add_tags([r], ["系列/角色A"])

    c = _client()
    plain = c.get("/library", params={"tag": "系列"}).json()
    deep = c.get("/library", params={"tag": "系列", "tag_children": "true"}).json()
    assert plain["total"] == 0
    assert deep["total"] == 1


def test_tag_api_exposes_the_palette_and_sep(tagsdb):
    """调色板由**后端下发** —— 前端不硬编码色值。"""
    c = _client()
    r = c.get("/library/tags")
    assert r.status_code == 200
    body = r.json()
    assert body["sep"] == "/"
    keys = [x["key"] for x in body["colors"]]
    assert "red" in keys
    assert all("value" in x and "label" in x for x in body["colors"])


def test_tag_api_returns_tree_fields(tagsdb):
    """接口回的每条带 `parent`/`depth`/`n_tree`, 前端才拼得出树。"""
    import core.database as db

    t = tagsdb.task()
    r = tagsdb.done(t, "a.jpg")
    db.add_tags([r], ["系列/角色A"])

    body = _client().get("/library/tags").json()
    row = next(x for x in body["items"] if x["tag"] == "系列/角色A")
    assert row["parent"] == "系列" and row["depth"] == 1


def test_tag_color_api_sets_and_clears(tagsdb):
    c = _client()
    r = c.post("/library/tags/color", json={"tag": "系列", "color": "purple"})
    assert r.status_code == 200 and r.json()["color"] == "purple"
    r = c.post("/library/tags/color", json={"tag": "系列", "color": ""})
    assert r.status_code == 200 and r.json()["color"] == ""


def test_tag_color_api_rejects_an_unknown_color(tagsdb):
    """不能悄悄退回默认色 —— 400 并说清可选值。"""
    c = _client()
    r = c.post("/library/tags/color", json={"tag": "x", "color": "chartreuse"})
    assert r.status_code == 400
    assert "red" in r.json()["detail"]


def test_tag_color_api_refuses_an_empty_tag(tagsdb):
    """空标签无处可挂 —— 报错, 而不是写一行没有名字的 tag_meta。"""
    c = _client()
    r = c.post("/library/tags/color", json={"tag": "   ", "color": "red"})
    assert r.status_code == 400

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import threading


# ==================================================================
# 7. DASH 字节区间(SegmentBase / mediaRange)与多时段
# ==================================================================

#: ffmpeg `-single_file 1` **真吐出来**的那个 sidx box(52 字节, version=1)。
#: 它在 `out-stream0.mp4` 的 814-865 字节处, 引用一段 13155 字节的媒体。
#: 把它钉在这里 = 钉住四件事: box 自报大小 / 版本(version=1 的字段是 8 字节) /
#: 引用条数 / **起点 = box 结束处 + first_offset**。最后一条错了会整体错位 ——
#: 拼出来长度对得上、能播、但每隔几秒糊一下, 属于最难发现的那类。
_REAL_SIDX_B64 = "AAAANHNpZHgBAAAAAAAAAQAAKAAAAAAAAAAAAAAAAAAAAAAAAAAAAQAAM2MAAFAAgAAAAA=="


def _sidx(refs, first_offset=0, version=0, ref_type=0):
    """手工造一个 sidx box。`refs` = 每个引用段的字节数。"""
    body = bytearray()
    body += bytes([1 if version == 1 else 0]) + b"\x00\x00\x00"
    body += (7).to_bytes(4, "big")                       # reference_ID
    body += (1000).to_bytes(4, "big")                    # timescale
    if version == 1:
        body += (0).to_bytes(8, "big") + first_offset.to_bytes(8, "big")
    else:
        body += (0).to_bytes(4, "big") + first_offset.to_bytes(4, "big")
    body += (0).to_bytes(2, "big")                       # reserved
    body += len(refs).to_bytes(2, "big")
    for n in refs:
        body += ((ref_type << 31) | n).to_bytes(4, "big")
        body += (0).to_bytes(4, "big")                   # subsegment_duration
        body += (0).to_bytes(4, "big")                   # SAP
    return (8 + len(body)).to_bytes(4, "big") + b"sidx" + bytes(body)


def _mpd(body):
    return ('<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" '
            'mediaPresentationDuration="PT6.0S">' + body + "</MPD>")


def test_range_of_only_accepts_the_closed_form():
    """DASH 的 range 只允许 `a-b`。认不出返回 None 而不是猜一个区间。"""
    from downloaders.dash import _range_of

    assert _range_of("814-14020") == (814, 14020)
    assert _range_of(" 0-0 ") == (0, 0)
    assert _range_of("") is None
    assert _range_of(None) is None
    assert _range_of("1234-") is None           # 开放区间不是 DASH 的写法
    assert _range_of("100-50") is None          # 终点小于起点
    assert _range_of("a-b") is None
    assert _range_of("10-20-30") is None        # partition 只切第一段, "20-30" 不是整数


def test_parse_sidx_pins_offsets_against_a_real_encoder_box():
    """拿 ffmpeg 真造出来的 sidx 字节对答案 —— 手造的 box 只能证明自洽。"""
    import base64

    from downloaders.dash import parse_sidx

    blob = base64.b64decode(_REAL_SIDX_B64)
    assert len(blob) == 52
    # 索引在文件里的偏移是 814, box 自身 52 字节, first_offset=0
    # -> 第一段媒体从 814 + 52 = 866 开始, 长度 13155 -> 到 14020
    assert parse_sidx(blob, 814) == [(866, 14020)]


def test_parse_sidx_computes_consecutive_ranges():
    from downloaders.dash import parse_sidx

    # 版本 0: box 总长 = 32 + 12n
    box = _sidx([100, 200, 300])
    assert len(box) == 32 + 36
    assert parse_sidx(box, 1000) == [(1068, 1167), (1168, 1367), (1368, 1667)]
    # first_offset 是相对于 **box 结束处** 的, 不是相对于 box 开头: box 长 56,
    # 所以第一段从 56 + 5 = 61 开始
    assert parse_sidx(_sidx([10, 10], first_offset=5), 0) == [(61, 70), (71, 80)]
    # 版本 1 的字段宽一倍: box 总长 = 40 + 12n = 64
    b1 = _sidx([10, 10], first_offset=5, version=1)
    assert len(b1) == 40 + 24
    assert parse_sidx(b1, 0) == [(69, 78), (79, 88)]


def test_parse_sidx_refuses_what_it_cannot_prove():
    from downloaders.dash import parse_sidx

    with pytest.raises(ValueError):
        parse_sidx(b"\x00\x00\x00\x08ftypAAAA", 0)          # 不是 sidx
    with pytest.raises(ValueError):
        parse_sidx(b"", 0)                                    # 连 box 头都没有
    with pytest.raises(ValueError):
        parse_sidx(_sidx([100, 100])[:30], 0)                 # 段表被截断
    with pytest.raises(ValueError):
        parse_sidx(_sidx([]), 0)                              # 空索引
    with pytest.raises(ValueError) as ei:
        parse_sidx(_sidx([100], ref_type=1), 0)
    assert "嵌套" in str(ei.value)


def test_segment_base_with_index_becomes_a_sidx_plan():
    """有 `indexRange` 时解析器给的是"去哪儿取索引", 因为它不碰网络。"""
    spec = parse_mpd(_mpd(
        '<Period><AdaptationSet contentType="video">'
        '<Representation id="v" bandwidth="1">'
        '<BaseURL>v.mp4</BaseURL>'
        '<SegmentBase indexRange="814-865"><Initialization range="0-813"/></SegmentBase>'
        "</Representation></AdaptationSet></Period>"
    ), "https://c.example.com/d/m.mpd")
    v = spec["video"]
    assert v["segments"] == [], "分片要等 sidx 取回来才算得出来"
    assert v["index"]["kind"] == "sidx"
    assert v["index"]["media"] == "https://c.example.com/d/v.mp4"
    assert v["index"]["range"] == (814, 865)
    assert v["init"] == ("https://c.example.com/d/v.mp4", (0, 813))


def test_segment_base_without_an_index_is_one_whole_file():
    """没有 `indexRange`: 那个文件**就是**整条轨, 下全即正确。

    ⚠️ 此时必须把**同文件**的 `Initialization` 丢掉 —— 它和文件开头是同一批
    字节, 拼到前面等于把 moov 放两遍, 而字节数"看起来没问题"。
    """
    spec = parse_mpd(_mpd(
        '<Period><AdaptationSet contentType="video">'
        '<Representation id="v" bandwidth="1">'
        '<BaseURL>v.mp4</BaseURL>'
        '<SegmentBase><Initialization range="0-813"/></SegmentBase>'
        "</Representation></AdaptationSet></Period>"
    ), "https://c.example.com/d/m.mpd")
    v = spec["video"]
    assert v["segments"] == ["https://c.example.com/d/v.mp4"]
    assert v["init"] is None
    assert v["index"] is None


def test_segment_base_keeps_a_separate_init_file():
    """`sourceURL` 指向**另一个文件**的那种初始化段要保留。"""
    spec = parse_mpd(_mpd(
        '<Period><AdaptationSet contentType="video">'
        '<Representation id="v" bandwidth="1">'
        '<BaseURL>v.mp4</BaseURL>'
        '<SegmentBase><Initialization sourceURL="init.mp4" range="0-99"/></SegmentBase>'
        "</Representation></AdaptationSet></Period>"
    ), "https://c.example.com/d/m.mpd")
    v = spec["video"]
    assert v["init"] == ("https://c.example.com/d/init.mp4", (0, 99))
    assert v["segments"] == ["https://c.example.com/d/v.mp4"]


def test_segment_list_media_range_becomes_byte_addresses():
    """`SegmentList@mediaRange`: 地址是 `(URL, (起, 止))`, 且区间已给全, 不需 sidx。"""
    spec = parse_mpd(_mpd(
        '<Period><AdaptationSet contentType="video">'
        '<Representation id="v" bandwidth="1">'
        '<BaseURL>v.mp4</BaseURL>'
        '<SegmentList timescale="1000" duration="2000" startNumber="1">'
        '<Initialization range="0-813"/>'
        '<SegmentURL mediaRange="814-14020" indexRange="814-865"/>'
        '<SegmentURL mediaRange="14021-26359" indexRange="14021-14072"/>'
        "</SegmentList></Representation></AdaptationSet></Period>"
    ), "https://c.example.com/d/m.mpd")
    v = spec["video"]
    assert v["init"] == ("https://c.example.com/d/v.mp4", (0, 813))
    assert v["segments"] == [
        ("https://c.example.com/d/v.mp4", (814, 14020)),
        ("https://c.example.com/d/v.mp4", (14021, 26359)),
    ]
    assert v["index"] is None, "区间已经给全了, 不必再去取索引"


def test_seg_target_adds_range_and_strips_conditional_headers():
    """带 `Range` 时绝不能同时带条件请求 —— 服务器会回 304 而不是 206。"""
    url, h = V._seg_target(
        ("https://h/m.mp4", (10, 19)),
        {"Referer": "r", "If-None-Match": 'W/"1"', "If-Modified-Since": "x",
         "Range": "bytes=0-1"},
    )
    assert url == "https://h/m.mp4"
    assert h["Range"] == "bytes=10-19"
    assert "If-None-Match" not in h and "If-Modified-Since" not in h
    assert h["Referer"] == "r", "其它头要原样保留"

    u2, h2 = V._seg_target("https://h/1.ts", {"A": "b"})
    assert u2 == "https://h/1.ts" and h2 == {"A": "b"}


class _RangeSession:
    """URL -> 整段字节; 按 `Range` 头切片, 并记下每次请求的 (url, headers)。"""

    def __init__(self, bodies, ignore_range=False):
        self.bodies = dict(bodies)
        self.ignore_range = ignore_range
        self.calls = []

    def get(self, url, headers=None, timeout=None, **kw):
        self.calls.append((url, dict(headers or {})))
        body = self.bodies[url]
        rng = (headers or {}).get("Range")
        if rng and not self.ignore_range:
            a, _, b = rng[6:].partition("-")
            body = body[int(a):int(b) + 1]
        return _FakeResp(body)

    def close(self):
        pass


def test_fetch_segments_sends_range_and_stitches_in_order(stage, monkeypatch,
                                                          tmp_path):
    """字节区间分片要真的带 `Range`, 且拼回来的顺序与清单一致。"""
    media = "https://h/m.mp4"
    whole = bytes(range(256)) * 4                      # 1024 字节
    segs = [(media, (0, 99)), (media, (100, 199)), (media, (200, 1023))]
    s = _RangeSession({media: whole})
    d = _downloader(monkeypatch, s)
    limiter = V.DomainLimiter(2, 0, 0)

    got = d._fetch_segments(segs, {"Referer": "r"}, tmp_path / "p", limiter,
                            None, None)
    assert b"".join(p.read_bytes() for p in got) == whole
    assert len(got) == 3
    rngs = sorted(h["Range"] for _u, h in s.calls)
    assert rngs == ["bytes=0-99", "bytes=100-199", "bytes=200-1023"]
    assert all(h.get("Referer") == "r" for _u, h in s.calls)


def test_resolve_index_turns_a_real_sidx_into_segments(stage, monkeypatch,
                                                       tmp_path):
    """`SegmentBase` 的入口: 取回索引那一小段字节, 解出真正的分片边界。"""
    import base64

    media = "https://h/v.mp4"
    blob = base64.b64decode(_REAL_SIDX_B64)
    # 索引在文件里的偏移是 814, 所以给会话一份"padded 到该位置"的字节
    # (Range 切片之后拿到的正好是这个 box)
    s = _RangeSession({media: b"\x00" * 814 + blob})
    d = _downloader(monkeypatch, s)
    track = {"segments": [], "init": None,
             "index": {"kind": "sidx", "media": media, "range": (814, 865)}}

    d._resolve_index(track, {"Referer": "r"})
    assert track["segments"] == [(media, (866, 14020))]
    assert s.calls[0][1]["Range"] == "bytes=814-865", "只取索引那一段, 不是整个文件"


def test_resolve_index_refuses_a_server_that_ignores_range(stage, monkeypatch,
                                                           tmp_path):
    """服务器忽略 `Range` 时**必须报错**: 拿整份文件当索引去解, 分片边界会整体错位。"""
    media = "https://h/v.mp4"
    s = _RangeSession({media: b"x" * 5000}, ignore_range=True)
    d = _downloader(monkeypatch, s)
    track = {"segments": [], "init": None,
             "index": {"kind": "sidx", "media": media, "range": (814, 865)}}
    with pytest.raises(RuntimeError) as ei:
        d._resolve_index(track, {})
    assert "没有按区间返回" in str(ei.value)
    assert track["segments"] == []


# ------------------------------------------------------------------
# 7b. 真 ffmpeg 端到端: 字节区间 / 多时段
# ------------------------------------------------------------------

def _have_ffmpeg():
    return bool(V.find_ffmpeg())


_needs_ffmpeg = pytest.mark.skipif(not _have_ffmpeg(), reason="[deps:ffmpeg] 需要 ffmpeg")


def _run(cmd, cwd):
    import subprocess

    p = subprocess.run(cmd, cwd=str(cwd), stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
    assert p.returncode == 0, p.stderr.decode("utf-8", "ignore")[-2000:]


class _RangeHandler(BaseHTTPRequestHandler):
    """支持 `Range` 的极简静态服务 —— 字节区间那条路没有它测不了。"""

    routes = {}

    def do_GET(self):                                   # noqa: N802
        body = self.routes.get(self.path)
        if body is None:
            self.send_error(404)
            return
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = max(0, int(a))
            end = min(len(body) - 1, int(b))
            chunk = body[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            self.wfile.write(chunk)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class _StaticServer:
    def __init__(self, routes):
        handler = type("H", (_RangeHandler,), {"routes": routes})
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def _plain_session():
    """不读环境代理的会话: 走本机 127.0.0.1 的测试服务, 代理插进来只会添乱。"""
    import requests

    s = requests.Session()
    s.trust_env = False
    return s


def _e2e_downloader(monkeypatch):
    monkeypatch.setattr(V.settings, "video_engine", "builtin")
    monkeypatch.setattr(V.settings, "segment_min_interval", 0)
    monkeypatch.setattr(V.settings, "segment_max_interval", 0)
    monkeypatch.setattr(V.settings, "segment_concurrency", 4)
    d = V.VideoDownloader()
    d.session = _plain_session()
    return d


@_needs_ffmpeg
def test_dash_media_range_end_to_end(monkeypatch, tmp_path):
    """`-single_file 1` 那套(`Initialization@range` + `SegmentURL@mediaRange`) 真下一遍。

    判据是**产物能被 ffprobe 认出来且时长对** —— 字节切错一位都会在时长或
    轨道数上暴露, 而"文件存在"这种断言对切错字节完全无感。
    """
    src = tmp_path / "m"
    src.mkdir()
    _run([V.find_ffmpeg(), "-hide_banner", "-loglevel", "error",
          "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=6",
          "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
          "-c:v", "libx264", "-g", "20", "-keyint_min", "20", "-c:a", "aac",
          "-f", "dash", "-single_file", "1", "-seg_duration", "2",
          "-use_timeline", "0", "-use_template", "0",
          "-init_seg_name", "init$RepresentationID$.mp4",
          "-media_seg_name", "chunk$RepresentationID$-$Number$.m4s",
          "out.mpd"], src)

    mpd = (src / "out.mpd").read_bytes()
    assert b"mediaRange" in mpd, "fixture 自己不诚实: 这不是字节区间形态的 MPD"
    routes = {"/out.mpd": mpd}
    for f in src.glob("out-stream*.mp4"):
        routes[f"/{f.name}"] = f.read_bytes()
    assert len(routes) == 3

    srv = _StaticServer(routes)
    try:
        path, _sha = _e2e_downloader(monkeypatch).download(
            srv.url("/out.mpd"), save_dir=tmp_path / "out", filename="单文件/x.mp4"
        )
    finally:
        srv.close()

    assert path.suffix == ".mp4"
    # ⚠️ 下载内部已经拿 MPD 自报的 6.0s 对过一次; 这里再独立验一遍发声/画轨都在
    dur = V._probe_media_duration(path, V.find_ffmpeg())
    assert dur is not None and abs(dur - 6.0) < 0.5, f"时长 {dur}"
    probe = subprocess_probe_streams(path)
    assert "video" in probe and "audio" in probe, probe


def subprocess_probe_streams(path):
    """用 ffprobe 列出流类型。只给测试用, 不去动产品里的探测逻辑。"""
    import json
    import subprocess

    ff = V.find_ffmpeg()
    probe = str(Path(ff).with_name("ffprobe" + Path(ff).suffix))
    p = subprocess.run([probe, "-v", "error", "-show_streams", "-of", "json",
                        str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        return {}
    try:
        return {s.get("codec_type", "") for s in
                json.loads(p.stdout.decode("utf-8", "ignore")).get("streams", [])}
    except ValueError:
        return {}


@_needs_ffmpeg
def test_dash_multi_period_end_to_end(monkeypatch, tmp_path):
    """两个 `Period` 的 MPD: 必须**两段都下到**, 且按轨拼起来。

    这条测试的价值在于"少了后半段"这件事有明确判据: MPD 自报 4.0s, 而只下
    第一个时段只有 2.0s —— 下载内部的时长终检会直接失败。所以它同时钉住了
    "多时段真的被下全"与"下不全就会报错"两件事。
    """
    src = tmp_path / "m"
    src.mkdir()
    _run([V.find_ffmpeg(), "-hide_banner", "-loglevel", "error",
          "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=4",
          "-c:v", "libx264", "-g", "20", "-keyint_min", "20", "-an",
          "-f", "dash", "-seg_duration", "2", "-use_timeline", "0",
          "-use_template", "1", "-init_seg_name", "init-v.mp4",
          "-media_seg_name", "chunk-v-$Number$.m4s", "out.mpd"], src)
    chunks = sorted(src.glob("chunk-v-*.m4s"))
    assert len(chunks) == 2, [c.name for c in chunks]

    # 手写两个时段: 第 2 个时段从第 2 片开始, 于是"两段拼起来 == 整段 4s"
    mpd = (
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" '
        'mediaPresentationDuration="PT4.0S">'
        '<Period id="0" duration="PT2.0S"><AdaptationSet contentType="video" '
        'mimeType="video/mp4"><Representation id="v" bandwidth="100000">'
        '<SegmentTemplate timescale="1000" duration="2000" startNumber="1" '
        'initialization="init-v.mp4" media="chunk-v-$Number$.m4s"/>'
        "</Representation></AdaptationSet></Period>"
        '<Period id="1" duration="PT2.0S"><AdaptationSet contentType="video" '
        'mimeType="video/mp4"><Representation id="v" bandwidth="100000">'
        '<SegmentTemplate timescale="1000" duration="2000" startNumber="2" '
        'initialization="init-v.mp4" media="chunk-v-$Number$.m4s"/>'
        "</Representation></AdaptationSet></Period></MPD>"
    ).encode("utf-8")

    routes = {"/out.mpd": mpd, "/init-v.mp4": (src / "init-v.mp4").read_bytes()}
    for c in chunks:
        routes[f"/{c.name}"] = c.read_bytes()

    srv = _StaticServer(routes)
    logs = []
    try:
        path, _sha = _e2e_downloader(monkeypatch).download(
            srv.url("/out.mpd"), save_dir=tmp_path / "out", filename="多时段/x.mp4",
            log=logs.append,
        )
    finally:
        srv.close()

    assert any("2 个时段" in m for m in logs), logs
    dur = V._probe_media_duration(path, V.find_ffmpeg())
    assert dur is not None and abs(dur - 4.0) < 0.5, f"时长 {dur} (只下第一个时段会是 2.0)"


@_needs_ffmpeg
def test_dash_multi_period_is_refused_without_ffmpeg(monkeypatch, tmp_path):
    """多时段没有 ffmpeg 时**在下载前**就失败 —— 不是下完 GB 字节才发现。"""
    mpd = (
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" '
        'mediaPresentationDuration="PT4.0S">'
        '<Period duration="PT2.0S"><AdaptationSet contentType="video">'
        '<Representation id="v" bandwidth="1">'
        '<SegmentTemplate timescale="1000" duration="2000" startNumber="1" '
        'media="a-$Number$.m4s"/></Representation></AdaptationSet></Period>'
        '<Period duration="PT2.0S"><AdaptationSet contentType="video">'
        '<Representation id="v" bandwidth="1">'
        '<SegmentTemplate timescale="1000" duration="2000" startNumber="2" '
        'media="a-$Number$.m4s"/></Representation></AdaptationSet></Period></MPD>'
    ).encode("utf-8")
    srv = _StaticServer({"/out.mpd": mpd})
    monkeypatch.setattr(V, "find_ffmpeg", lambda: None)
    monkeypatch.setattr(V.settings, "video_engine", "builtin")
    d = V.VideoDownloader()
    d.session = _plain_session()
    try:
        with pytest.raises(RuntimeError) as ei:
            # 下载层把 mpd 解析失败的原因包在 "MPD 不可用" 里, 而这里解析是成功的,
            # 所以抛的是我们自己那条"需要 ffmpeg"
            d.download(srv.url("/out.mpd"), save_dir=tmp_path / "out",
                       filename="x/x.mp4")
    finally:
        srv.close()
    assert "时段" in str(ei.value)
