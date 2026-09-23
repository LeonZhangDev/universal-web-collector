"""V35 收尾三件: 断点续传持久化 / DASH(.mpd) / 资源库标签与收藏。

这一版的共同点: 三件都是**把"能省下来"和"能找回来"变成产品能力**, 而它们的
坑全在**选错方向的代价不对等**上。

1. **断点续传暂存区**(`core/partials.py`)。取回时少一份只是白下一次; 但
   **把已知坏的字节 park 进去**, 下次会取回来接着拼 —— 一次已知的坏结果变成
   持续的坏结果。所以判据不是"能不能省", 而是"这份字节还值不值得留"。
   另一个方向: `take()` 必须**拒收不属于自己的字节**(索引里的 url 与请求的
   url 不一致时返回 0) —— 这就是第 6 条静默坑那道防线, 换了寻址维度也不能松。

2. **DASH(.mpd)**。它比 m3u8 复杂一个量级, 而"尽力而为"在这里是最坏的选择:
   DRM 流下下来是密文、SegmentBase 下下来只有一小段 —— 都属于**下载成功但文件
   没用**。那种假成功比明确失败糟糕得多, 所以判据只取能被证明的部分, 认不出
   就拒绝。测试重点因此是**拒绝路径**, 不是解析成功路径。

3. **标签 / 收藏**。看似纯 UI 功能, 危险在**边界**: 标签数上限、超长标签、
   大小写折叠、按标签筛时的分页口径。最后一条尤其: 用 JOIN 而不是 EXISTS 会让
   "一个资源带 3 个标签"在筛选结果里变成 3 行, 于是总数与页内容口径不一致 ——
   表现为"翻到最后一页数量对不上", 极难归因。

测试策略: 纯逻辑(解析/归一化/筛选条件)直接单测; 涉及落盘的用 tmp_path 真写文件
(暂存区的正确性**就是**文件搬来搬去, 用假文件系统测等于什么都没测)。
"""

import io
import os
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import api.tasks as T
import core.database as db
import core.partials as partials
import downloaders.base as B
from downloaders.dash import parse_mpd


# ============================ 公共夹具 ============================

@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    """独立库 + 独立下载根 + 任务/资源工厂。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "v35.db")
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(T.task_manager, "submit", lambda tid, **kw: None)
    dl = tmp_path / "dl"
    dl.mkdir()
    # ⚠️ 暂存区按 settings.download_dir 寻址(每次调用重新读), 所以只要把这个
    # 指向临时目录, 暂存区就跟着隔离 —— 不必额外登记路径。
    monkeypatch.setattr(partials.settings, "download_dir", dl)
    # ⚠️ 阈值默认 256KB(见核心配置)。夹具里写几十 KB 的真实文件只为触发搬运,
    # 既慢又没有任何额外覆盖 —— 阈值本身的判据由
    # `test_park_skips_files_below_min_bytes` 专门钉住, 这里放开到 0。
    monkeypatch.setattr(partials.settings, "partial_min_bytes", 0)

    class Ctx:
        root = dl

        @staticmethod
        def task(name="相册A", download_dir=None, url="https://example.com/a"):
            return db.create_task(url, "generic", download_dir=str(download_dir or dl),
                                  name=name)

        @staticmethod
        def res(task_id, rtype="image", url="https://example.com/1.jpg", **kw):
            return db.add_resource(task_id, rtype, url, **kw)

        @staticmethod
        def done(task_id, name="a.jpg", body=b"hello world", sub="", rtype="image"):
            p = dl / sub / name if sub else dl / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(body)
            rid = db.add_resource(task_id, rtype, f"https://example.com/{name}",
                                  status="done", local_path=str(p))
            db.update_resource(rid, size=len(body))
            return rid, p

    return Ctx()


def _client():
    app = FastAPI()
    app.include_router(T.router)
    return TestClient(app)


# ==================================================================
# 1. 断点续传暂存区 (core/partials.py)
# ==================================================================

def test_key_for_is_stable_and_hides_the_query_string(ctx):
    """URL -> 文件名必须**稳定且不泄露查询串**(里面常有 token/expires)。"""
    u = "https://cdn.example.com/a/1.jpg?token=SECRET&expires=123"
    k1, k2 = partials.key_for(u), partials.key_for(u)
    assert k1 == k2 and len(k1) == 40
    assert "SECRET" not in k1 and "?" not in k1 and "/" not in k1


def test_park_then_take_round_trips_bytes_and_clears_index(ctx):
    """核心闭环: 收进暂存区 -> 再取回, 字节一致且索引不再留着它。"""
    url = "https://example.com/big.mp4"
    part = ctx.root / "album" / "big.mp4.part"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"x" * 5000)

    n = partials.park(part, url, sidecar=ctx.root / "album" / "big.mp4.partsrc")
    assert n == 5000
    assert not part.exists(), "park 是**搬走**不是复制 —— 原地不该再留一份"
    assert partials.stats()["count"] == 1

    dest = ctx.root / "album" / "big.mp4.part"
    got = partials.take(url, dest)
    assert got == 5000
    assert dest.read_bytes() == b"x" * 5000
    # 索引是"待续传"的清单, 不是备份目录: 取回后必须摘掉
    assert partials.stats()["count"] == 0


def test_park_drops_the_sidecar(ctx):
    """`.partsrc` 进了暂存区就没有意义(URL 由索引记着), 必须一并清掉。"""
    url = "https://example.com/a.jpg"
    part = ctx.root / "a.jpg.part"
    src = ctx.root / "a.jpg.partsrc"
    part.write_bytes(b"y" * 3000)
    src.write_text(url, encoding="utf-8")

    partials.park(part, url, sidecar=src)
    assert not src.exists(), "留着就是相册目录里的垃圾"


def test_take_rejects_bytes_belonging_to_another_url(ctx):
    """⚠️ 第 6 条静默坑的那道防线: 不能把别人的字节当自己的续上。

    文件名是 url 的 sha1, 所以"哈希撞了 / 索引被手改过"这条路仍然要挡住 ——
    与 `.partsrc` 的来源比对是同一个理由(两个来源拼成一份恰好等长的文件)。
    """
    owner = "https://example.com/OWNER.mp4"
    part = ctx.root / "OWNER.mp4.part"
    part.write_bytes(b"z" * 4000)
    partials.park(part, owner)

    dest = ctx.root / "OTHER.mp4.part"
    assert partials.take("https://example.com/OTHER.mp4", dest) == 0
    assert not dest.exists()


def test_take_does_not_overwrite_an_existing_part(ctx):
    """目标位置已经有东西时不覆盖 —— 取回是锦上添花, 不该毁掉现场那份。"""
    url = "https://example.com/a.mp4"
    part = ctx.root / "a.mp4.part"
    part.write_bytes(b"old" * 1000)
    partials.park(part, url)

    dest = ctx.root / "a.mp4.part"
    dest.write_bytes(b"NEW")
    assert partials.take(url, dest) == 0
    assert dest.read_bytes() == b"NEW", "现场那份必须原封不动"


def test_park_skips_files_below_min_bytes(ctx, monkeypatch):
    """为几 KB 维护一条索引, 收益是负的 —— 阈值以下不收, 且**原地不动**。"""
    monkeypatch.setattr(partials.settings, "partial_min_bytes", 10000)
    part = ctx.root / "small.jpg.part"
    part.write_bytes(b"s" * 100)
    assert partials.park(part, "https://example.com/s.jpg") == 0
    assert part.exists(), "没收下就该留在原地(下次同名同源照样能续)"


def test_park_is_a_noop_when_staging_is_disabled(ctx, monkeypatch):
    """关掉暂存要**一点痕迹都不留**: 不建目录、不写索引。"""
    monkeypatch.setattr(partials.settings, "partial_staging", False)
    part = ctx.root / "a.jpg.part"
    part.write_bytes(b"q" * 9000)
    assert partials.park(part, "https://example.com/a.jpg") == 0
    assert part.exists()
    assert not partials.stage_dir().exists()


def test_park_without_url_is_refused(ctx):
    """来源不明的字节不该进按 URL 寻址的暂存区 —— 它永远取不回来。"""
    part = ctx.root / "a.jpg.part"
    part.write_bytes(b"q" * 9000)
    assert partials.park(part, "") == 0
    assert part.exists()


def test_sweep_evicts_by_ttl(ctx):
    """TTL 到期就该走。用**未来时间**判定, 不靠 sleep —— 测试不该有墙钟依赖。"""
    url = "https://example.com/old.mp4"
    part = ctx.root / "old.mp4.part"
    part.write_bytes(b"o" * 4000)
    partials.park(part, url)

    n, freed = partials.sweep(now=time.time() + 100 * 3600)
    assert (n, freed) == (1, 4000)
    assert partials.stats()["count"] == 0


def test_sweep_evicts_oldest_first_when_over_budget(ctx, monkeypatch):
    """超预算时按"最久没用"淘汰 —— 最近还在续传的才最可能有下一半。"""
    monkeypatch.setattr(partials.settings, "partial_max_bytes", 5000)
    # 造三份各 3000B, 总 9000 > 5000: 至少要砍掉 4000
    for i, age in enumerate([9000, 5000, 100]):
        part = ctx.root / f"f{i}.mp4.part"
        part.write_bytes(b"B" * 3000)
        partials.park(part, f"https://example.com/{i}.mp4")
        # 直接改索引里的 touched_at, 模拟"很久没动过"
        ok = partials.key_for(f"https://example.com/{i}.mp4")

        def _touch(data, k=ok, a=age):
            if k in data:
                data[k]["touched_at"] = time.time() - a
            return None

        partials._index.update(_touch)

    partials.sweep(now=time.time())
    left = {i["url"] for i in partials.stats()["items"]}
    assert left == {"https://example.com/2.mp4"}, \
        "最新用过的那条必须留下, 最旧的先走"


def test_sweep_self_heals_dangling_index_entries(ctx):
    """内容被外部删掉时, 索引条目是空头支票 —— sweep 要顺手摘掉。"""
    url = "https://example.com/gone.mp4"
    part = ctx.root / "gone.mp4.part"
    part.write_bytes(b"g" * 4000)
    partials.park(part, url)

    partials.stage_path(url).unlink()          # 模拟用户手动清了暂存目录
    n, freed = partials.sweep(now=time.time())
    assert (n, freed) == (0, 0), "内容已经不在了, 没什么可释放的"
    assert partials.stats()["count"] == 0, "但索引条目必须被摘掉"


def test_clear_and_clear_urls(ctx):
    """清空是"释放空间"的入口, 代价只是下次从头下 —— 所以可以放心给用户。"""
    urls = [f"https://example.com/{i}.mp4" for i in range(3)]
    for i, u in enumerate(urls):
        p = ctx.root / f"c{i}.mp4.part"
        p.write_bytes(b"C" * 2000)
        partials.park(p, u)

    assert partials.clear(urls[0]) == (1, 2000)
    assert partials.stats()["count"] == 2

    n, freed = partials.clear()
    assert (n, freed) == (2, 4000)
    assert partials.stats()["count"] == 0


def test_clear_urls_batch(ctx):
    urls = [f"https://example.com/b{i}.mp4" for i in range(2)]
    for i, u in enumerate(urls):
        p = ctx.root / f"b{i}.mp4.part"
        p.write_bytes(b"D" * 1500)
        partials.park(p, u)
    n, freed = partials.clear_urls(urls)
    assert (n, freed) == (2, 3000)


def test_park_keeps_the_larger_copy(ctx):
    """同一 URL 两次中断(进程被杀 / 两个任务同时取消)时取字节多的那份。

    内容没校验过, 无法判定谁更"完整"; 字节数大是合理的近似。
    """
    url = "https://example.com/dup.mp4"
    small = ctx.root / "s.mp4.part"
    small.write_bytes(b"S" * 1000)
    partials.park(small, url)

    big = ctx.root / "b.mp4.part"
    big.write_bytes(b"B" * 5000)
    n = partials.park(big, url)
    assert n == 5000, "收下的是大的那份"
    stats = partials.stats()
    assert stats["count"] == 1 and stats["bytes"] == 5000

    # 再来一份更小的: 原地那份该被丢掉, 暂存区保持 5000
    tiny = ctx.root / "t.mp4.part"
    tiny.write_bytes(b"T" * 100)
    assert partials.park(tiny, url) == 5000
    assert partials.stats()["bytes"] == 5000


def test_stats_never_raises(ctx, monkeypatch):
    """它是只读观测, 不该有能力影响流程 —— 索引坏掉也要给出一份可用的结构。"""
    (partials.stage_dir()).mkdir(parents=True, exist_ok=True)
    (partials.stage_dir() / "index.json").write_text("{ 不是 json", encoding="utf-8")
    st = partials.stats()
    assert st["count"] == 0 and st["items"] == []
    assert "enabled" in st and "dir" in st


# ---- 接进下载路径: park 与 discard 的分工 ----

def test_park_partial_helper_moves_part_next_to_target(ctx):
    """`park_partial(target, url)` 是下载层用的入口: 它要自己找到 `.part`。"""
    url = "https://example.com/p.jpg"
    target = ctx.root / "album" / "p.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    (ctx.root / "album" / "p.jpg.part").write_bytes(b"P" * 4000)
    (ctx.root / "album" / "p.jpg.partsrc").write_text(url, encoding="utf-8")

    assert B.park_partial(target, url) == 4000
    assert not (ctx.root / "album" / "p.jpg.part").exists()
    assert not (ctx.root / "album" / "p.jpg.partsrc").exists()
    assert partials.stats()["count"] == 1


def test_park_partial_without_url_leaves_the_part_alone(ctx):
    """url 读不出来时**不能顺手删** —— 这是刻意选的保守方向。

    反过来问: 删掉的代价是什么? 这次读不出 URL, 不代表磁盘上的 `.partsrc` 也
    没有 —— 它可能记着真实来源。下次带着真 URL 重试时, `_prepare_resume` 会
    比对 `.partsrc` 命中并续上传; 而那正是"删了就白下一次"的那一份。

    所以拿不准时**不动**: 最坏结果是相册目录里留一个 `.part`(V33 的
    `_discard_partial` 会在取消/坏文件时清掉它), 而不是把一个能续的半成品删了。
    """
    target = ctx.root / "album" / "n.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    (ctx.root / "album" / "n.jpg.part").write_bytes(b"N" * 4000)
    assert B.park_partial(target, "") == 0
    assert (ctx.root / "album" / "n.jpg.part").exists(), "收不下就保持原地不动"
    assert partials.stats()["count"] == 0, "没 URL 就进不了暂存区(取不回来)"


def test_discard_partial_removes_everything(ctx):
    """取消/坏文件走这条路: 最终文件 + .part + .partsrc 全清。"""
    target = ctx.root / "album" / "d.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"D")
    (ctx.root / "album" / "d.jpg.part").write_bytes(b"DD")
    (ctx.root / "album" / "d.jpg.partsrc").write_text("u", encoding="utf-8")

    B.discard_partial(target)
    assert not target.exists()
    assert not (ctx.root / "album" / "d.jpg.part").exists()
    assert not (ctx.root / "album" / "d.jpg.partsrc").exists()


def test_prepare_resume_takes_from_stage(ctx):
    """续传前先问暂存区要 —— 这是"换路径也能续上"的实现点。"""
    url = "https://example.com/staged.mp4"
    parked = ctx.root / "elsewhere" / "staged.mp4.part"
    parked.parent.mkdir(parents=True, exist_ok=True)
    parked.write_bytes(b"Z" * 6000)
    partials.park(parked, url)

    # 全新的目标路径(站改了相册名 / 用户改了命名模板)
    target = ctx.root / "new-album" / "renamed.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    offset = B._prepare_resume(target, url)

    assert offset == 6000
    assert B._part_path(target).read_bytes() == b"Z" * 6000
    # ⚠️ 来源必须被写回: 续传前的 .partsrc 比对是第 6 条静默坑那道防线
    assert B._read_src(B._part_src(target)) == url


def test_prepare_resume_ignores_staged_bytes_of_other_url(ctx):
    """暂存区里有别人的字节时, 不能拿来当自己的续传起点。"""
    other = "https://example.com/other.mp4"
    parked = ctx.root / "o.mp4.part"
    parked.write_bytes(b"O" * 6000)
    partials.park(parked, other)

    target = ctx.root / "mine.mp4"
    assert B._prepare_resume(target, "https://example.com/mine.mp4") == 0
    assert not B._part_path(target).exists()


# ==================================================================
# 2. DASH(.mpd)  (downloaders/dash.py)
# ==================================================================

_TPL_MPD = """<?xml version="1.0" encoding="utf-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     mediaPresentationDuration="PT12.5S">
 <Period>
  <AdaptationSet contentType="video" mimeType="video/mp4">
   <Representation id="v0" bandwidth="300000" width="640" height="360" codecs="avc1.4d401e">
    <SegmentTemplate timescale="1000" duration="5000" startNumber="1"
      initialization="v0/init.mp4" media="v0/$Number%04d$.m4s"/>
   </Representation>
   <Representation id="v1" bandwidth="1200000" width="1280" height="720" codecs="avc1.640028">
    <SegmentTemplate timescale="1000" duration="5000" startNumber="1"
      initialization="v1/init.mp4" media="v1/$Number%04d$.m4s"/>
   </Representation>
  </AdaptationSet>
  <AdaptationSet contentType="audio" lang="en" mimeType="audio/mp4">
   <Representation id="a0" bandwidth="128000">
    <SegmentTemplate timescale="1000" duration="5000"
      initialization="a0/init.mp4" media="a0/$Number$.m4s"/>
   </Representation>
  </AdaptationSet>
 </Period>
</MPD>
"""


def test_parse_template_number_and_duration():
    spec = parse_mpd(_TPL_MPD, "https://cdn.example.com/dir/manifest.mpd")
    v = spec["video"]
    assert spec["duration"] == 12.5
    # 12.5 / 5.0 = 2.5 -> 3 片(向上取整)
    assert len(v["segments"]) == 3
    assert v["init"] == "https://cdn.example.com/dir/v1/init.mp4"
    assert v["segments"][0] == "https://cdn.example.com/dir/v1/0001.m4s"
    assert v["segments"][1] == "https://cdn.example.com/dir/v1/0002.m4s"


def test_parse_picks_the_highest_bandwidth_video():
    """多档时必须取最高码率, 而不是 XML 里第一条。"""
    spec = parse_mpd(_TPL_MPD, "https://cdn.example.com/d/manifest.mpd")
    assert spec["video"]["bandwidth"] == 1200000
    assert spec["video"]["height"] == 720
    assert "v1/" in spec["video"]["segments"][0]


def test_parse_separates_audio_track():
    """DASH 的音视频是两条独立的轨 —— 少了音频轨会得到一个没声音的视频。"""
    spec = parse_mpd(_TPL_MPD, "https://cdn.example.com/d/manifest.mpd")
    assert spec["audio"] is not None
    assert spec["audio"]["bandwidth"] == 128000
    assert spec["audio"]["lang"] == "en"
    assert spec["audio"]["init"].endswith("a0/init.mp4")


def test_parse_number_without_padding():
    """`$Number$` 不带宽度时不做左补零。"""
    spec = parse_mpd(_TPL_MPD, "https://cdn.example.com/d/manifest.mpd")
    assert spec["audio"]["segments"][0].endswith("a0/1.m4s")


def test_parse_keeps_absolute_segment_urls():
    """绝对 URL 不该被 urljoin 破坏。"""
    mpd = """<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
       mediaPresentationDuration="PT6S"><Period>
     <AdaptationSet contentType="video"><Representation id="v" bandwidth="1">
      <SegmentTemplate media="https://other.cdn/x/$Number$.m4s" duration="3000"
        timescale="1000" startNumber="1"/>
     </Representation></AdaptationSet></Period></MPD>"""
    spec = parse_mpd(mpd, "https://cdn.example.com/a/b/manifest.mpd")
    assert spec["video"]["segments"][0] == "https://other.cdn/x/1.m4s"


def test_parse_timeline_time_template():
    """`$Time$` + SegmentTimeline 是另一种寻址方式, 必须支持。"""
    mpd = """<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"><Period>
     <AdaptationSet contentType="video" mimeType="video/mp4">
      <Representation id="v" bandwidth="1000">
       <SegmentTemplate timescale="90000"
         initialization="init.mp4" media="seg-$Time$.m4s">
        <SegmentTimeline>
          <S t="0" d="450000" r="1"/>
          <S d="450000"/>
        </SegmentTimeline>
       </SegmentTemplate>
      </Representation></AdaptationSet></Period></MPD>"""
    spec = parse_mpd(mpd, "https://c.example.com/v/m.mpd")
    segs = spec["video"]["segments"]
    # t=0 r=1 -> 两条(t=0, t=450000); 再一条 t=900000
    assert [s.rsplit("/", 1)[-1] for s in segs] == [
        "seg-0.m4s", "seg-450000.m4s", "seg-900000.m4s",
    ]
    assert spec["duration"] is None, "没有 mediaPresentationDuration 就是 None 而不是 0"


def test_parse_segment_list():
    """`SegmentList`(分片清单直接列出来)是第三种寻址方式。"""
    mpd = """<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static">
     <Period><AdaptationSet contentType="video" mimeType="video/mp4">
      <Representation id="v" bandwidth="1">
       <SegmentList timescale="1000" duration="2000">
        <Initialization sourceURL="i.mp4"/>
        <SegmentURL media="s1.m4s"/><SegmentURL media="s2.m4s"/>
       </SegmentList>
      </Representation></AdaptationSet></Period></MPD>"""
    spec = parse_mpd(mpd, "https://c.example.com/dir/m.mpd")
    assert spec["video"]["init"] == "https://c.example.com/dir/i.mp4"
    assert [s.rsplit("/", 1)[-1] for s in spec["video"]["segments"]] == ["s1.m4s", "s2.m4s"]


def test_parse_skips_subtitle_tracks():
    """字幕/缩略图轨不是"视频文件"的一部分, 混进来只会让合并步骤报错。"""
    mpd = """<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
       mediaPresentationDuration="PT4S">
     <Period>
      <AdaptationSet contentType="video" mimeType="video/mp4">
       <Representation id="v" bandwidth="1"><SegmentTemplate media="v/$Number$.m4s"
         duration="1000" timescale="1000"/></Representation></AdaptationSet>
      <AdaptationSet contentType="text" mimeType="text/vtt" lang="en">
       <Representation id="t" bandwidth="1"><SegmentTemplate media="t/$Number$.m4s"
         duration="1000" timescale="1000"/></Representation></AdaptationSet>
      <AdaptationSet contentType="image" mimeType="image/jpeg">
       <Representation id="i" bandwidth="1"><SegmentTemplate media="i/$Number$.m4s"
         duration="1000" timescale="1000"/></Representation></AdaptationSet>
     </Period></MPD>"""
    spec = parse_mpd(mpd, "https://c.example.com/m.mpd")
    assert spec["video"] is not None
    assert "text" not in str(spec["video"]["segments"])
    assert "i/" not in str(spec["video"]["segments"])


def test_parse_audio_only_mpd_is_allowed():
    """只有音频的 MPD 是合法的(它会直接落成 .m4a), 不该被判成"没有可下的轨"。"""
    mpd = """<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
       mediaPresentationDuration="PT3S"><Period>
     <AdaptationSet contentType="audio" mimeType="audio/mp4">
      <Representation id="a" bandwidth="64000">
       <SegmentTemplate media="a/$Number$.m4s" duration="1000" timescale="1000"/>
      </Representation></AdaptationSet></Period></MPD>"""
    spec = parse_mpd(mpd, "https://c.example.com/m.mpd")
    assert spec["video"] is None and spec["audio"] is not None


# ---- 明确拒绝的路径: 这里每一个都对应一种"假成功" ----

def test_drm_is_refused_with_an_actionable_message():
    """DRM 流下下来是**密文**: 会"下载成功"但打不开, 所以必须明确拒绝。"""
    mpd = """<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"><Period>
     <AdaptationSet contentType="video"><Representation id="v" bandwidth="1">
      <ContentProtection schemeIdUri="urn:uuid:EDEF8BA9-79D6-4ACE-A3C8-27DCD51D21ED"/>
      <SegmentTemplate media="v/$Number$.m4s" duration="1000" timescale="1000"/>
     </Representation></AdaptationSet></Period></MPD>"""
    with pytest.raises(ValueError) as ei:
        parse_mpd(mpd, "https://c.example.com/m.mpd")
    msg = str(ei.value)
    assert "DRM" in msg or "内容保护" in msg
    # 说清"这不是故障" —— 否则用户会去查网络
    assert "版权" in msg or "官方" in msg
    assert "EDEF8BA9" in msg, "要把 scheme 带出来, 否则无从判断是哪家的保护"


def test_segment_base_without_a_baseurl_is_refused():
    """SegmentBase 少了 `BaseURL`, 所谓"媒体文件"就是 MPD 自己所在的位置。

    拿它当媒体去请求会下回一份 XML, 而字节数是"有内容"的 —— 所以必须在**开始
    下载之前**拒绝, 而不是下完了再看容器对不对。
    """
    mpd = """<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"><Period>
     <AdaptationSet contentType="video"><Representation id="v" bandwidth="1">
      <SegmentBase indexRange="0-999"/>
     </Representation></AdaptationSet></Period></MPD>"""
    with pytest.raises(ValueError) as ei:
        parse_mpd(mpd, "https://c.example.com/m.mpd")
    assert "BaseURL" in str(ei.value)


def test_live_is_refused():
    """直播没有确定的结束点, 按分片清单下会得到一个一直长不大的文件。"""
    mpd = """<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="dynamic"><Period>
     <AdaptationSet contentType="video"><Representation id="v" bandwidth="1">
      <SegmentTemplate media="v/$Number$.m4s" duration="1000" timescale="1000"/>
     </Representation></AdaptationSet></Period></MPD>"""
    with pytest.raises(ValueError) as ei:
        parse_mpd(mpd, "https://c.example.com/m.mpd")
    assert "dynamic" in str(ei.value) or "直播" in str(ei.value)


def test_multi_period_is_parsed_and_never_silently_truncated():
    """多时段现在**支持**了(V36), 但要钉死"不会被静默截成第一段"。

    ⚠️ 这条以前断言的是"抛错拒绝"。产品能力变了, 但**原来要守的那件事没变** ——
    "取第一段会静默少下后面全部"。现在它由另一条机制守住: 多时段时顶层的
    `video`/`audio` 是 `None`, 只读第一条轨的调用方会拿到空值而**自己炸**。
    """
    mpd = """<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
       mediaPresentationDuration="PT2S">
     <Period duration="PT1S"><AdaptationSet contentType="video">
      <Representation id="v" bandwidth="1">
      <SegmentTemplate media="v/$Number$.m4s" duration="1000" timescale="1000"/>
     </Representation></AdaptationSet></Period>
     <Period duration="PT1S"><AdaptationSet contentType="video">
      <Representation id="v2" bandwidth="1">
      <SegmentTemplate media="v2/$Number$.m4s" duration="1000" timescale="1000"/>
     </Representation></AdaptationSet></Period></MPD>"""
    spec = parse_mpd(mpd, "https://c.example.com/m.mpd")
    assert len(spec["periods"]) == 2
    assert spec["video"] is None and spec["audio"] is None, \
        "多时段时顶层轨必须是空 —— 否则调用方会静默只下第一段"
    urls = [u for p in spec["periods"] for u in p["video"]["segments"]]
    assert urls == ["https://c.example.com/v/1.m4s", "https://c.example.com/v2/1.m4s"]


def test_timeline_open_ended_is_refused():
    """`r="-1"` 表示"直到时段结束" —— 不猜, 明确拒绝。"""
    mpd = """<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"><Period>
     <AdaptationSet contentType="video"><Representation id="v" bandwidth="1">
      <SegmentTemplate timescale="1000" media="s-$Time$.m4s">
       <SegmentTimeline><S t="0" d="2000" r="-1"/></SegmentTimeline>
      </SegmentTemplate></Representation></AdaptationSet></Period></MPD>"""
    with pytest.raises(ValueError):
        parse_mpd(mpd, "https://c.example.com/m.mpd")


def test_errorpage_is_not_a_manifest():
    """最常见的失败其实是"拿到的不是 MPD"(错误页/重定向页)。"""
    with pytest.raises(ValueError) as ei:
        parse_mpd("<html><body>404</body></html>", "https://c.example.com/m.mpd")
    assert "MPD" in str(ei.value)

    with pytest.raises(ValueError) as ei2:
        parse_mpd("", "https://c.example.com/m.mpd")
    assert "0 字节" in str(ei2.value) or "空" in str(ei2.value)


def test_pure_subtitle_mpd_says_so():
    """只有字幕轨时要说清"只有字幕", 而不是"没有任何轨" —— 后者查不到原因。"""
    mpd = """<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"><Period>
     <AdaptationSet contentType="text" mimeType="text/vtt">
      <Representation id="t" bandwidth="1"><SegmentTemplate media="t/$Number$.m4s"
        duration="1000" timescale="1000"/></Representation>
     </AdaptationSet></Period></MPD>"""
    with pytest.raises(ValueError) as ei:
        parse_mpd(mpd, "https://c.example.com/m.mpd")
    assert "text" in str(ei.value)


def test_notes_record_what_was_chosen():
    """选择的依据要能被看见 —— 否则"为什么下的是这条轨"无从复盘。"""
    spec = parse_mpd(_TPL_MPD, "https://cdn.example.com/d/manifest.mpd")
    joined = " | ".join(spec["notes"])
    assert "video" in joined and "audio" in joined
    assert "720" in joined, "要把选中的分辨率写出来"
    assert "初始化段" in joined


def test_is_dash_detection_ignores_the_query_string():
    """判定要看**路径**, 不能被 `?token=...m3u8` 这种查询串骗到。"""
    assert T.task_manager is not None      # 占位, 真正断言在下面
    from downloaders import video as V
    assert V._is_dash("https://e.com/a/b.mpd")
    assert V._is_dash("https://e.com/a/b.mpd?token=x")
    assert not V._is_dash("https://e.com/a/b.m3u8")
    assert not V._is_dash("https://e.com/a/b.mp4")


# ==================================================================
# 3. 资源库标签与收藏
# ==================================================================

def test_normalize_tag_folds_whitespace_but_keeps_case():
    """折叠空白但**不小写化** —— 用户特意写成 MacBook 就该保持原样。"""
    assert db.normalize_tag("  sunset  ") == "sunset"
    assert db.normalize_tag("a\t\t b") == "a b"
    assert db.normalize_tag("MacBook") == "MacBook"
    assert db.normalize_tag("") == "" and db.normalize_tag(None) == ""


def test_normalize_tag_rejects_too_long_and_control_chars():
    with pytest.raises(ValueError):
        db.normalize_tag("x" * (db.MAX_TAG_LEN + 1))
    with pytest.raises(ValueError):
        db.normalize_tag("bad\x00tag")


def test_add_tags_dedupes_and_counts_only_new(ctx):
    """重复打同一个标签是**正常操作**(选中一批再打一次), 不能抛 UNIQUE 冲突。"""
    t = ctx.task()
    r1 = ctx.res(t)
    r2 = ctx.res(t, url="https://example.com/2.jpg")

    assert db.add_tags([r1, r2], ["sunset", "海边"]) == 4
    assert db.add_tags([r1, r2], ["sunset"]) == 0, "已存在的不重复计"
    assert db.tags_of([r1])[r1] == ["sunset", "海边"] or \
        set(db.tags_of([r1])[r1]) == {"sunset", "海边"}


def test_tags_are_case_insensitive_on_comparison(ctx):
    """列的 COLLATE NOCASE 管"比较时等不等价", 与"存成什么样"是两件事。"""
    t = ctx.task()
    r1 = ctx.res(t)
    db.add_tags([r1], ["Sunset"])
    assert db.add_tags([r1], ["sunset"]) == 0, "大小写不同但应视为同一个标签"
    assert db.tags_of([r1])[r1] == ["Sunset"], "存下来的是第一次那个写法"


def test_remove_tags_selective_and_clear_all(ctx):
    t = ctx.task()
    r1 = ctx.res(t)
    db.add_tags([r1], ["a", "b", "c"])

    assert db.remove_tags([r1], ["b"]) == 1
    assert set(db.tags_of([r1])[r1]) == {"a", "c"}

    # 空 tags = 清空这些资源的全部标签
    assert db.remove_tags([r1], []) == 2
    assert db.tags_of([r1]) == {}


def test_tags_of_batches_and_omits_untagged(ctx):
    """批量取 —— 逐行查就是 N+1, 而列表接口是最高频的调用。"""
    t = ctx.task()
    ids = [ctx.res(t, url=f"https://example.com/{i}.jpg") for i in range(5)]
    db.add_tags(ids[:2], ["x"])
    got = db.tags_of(ids)
    assert set(got) == {ids[0], ids[1]}, "没标签的不该出现在结果里"
    assert db.tags_of([]) == {}


def test_tag_limit_raises_instead_of_silently_dropping(ctx):
    """超上限要**整个请求失败**, 而不是"能加的加上、剩下的丢掉"。

    后者会让用户以为标签打上了 —— 那正是本项目反复要消灭的一类静默失败。
    """
    t = ctx.task()
    r1 = ctx.res(t)
    db.add_tags([r1], [f"t{i}" for i in range(db.MAX_TAGS_PER_RESOURCE)])
    with pytest.raises(ValueError) as ei:
        db.add_tags([r1], ["one-more"])
    assert "上限" in str(ei.value)


def test_favorite_toggle(ctx):
    t = ctx.task()
    r1 = ctx.res(t)
    assert db.set_favorite([r1], True) == 1
    assert db.get_resource(r1)["favorite"] == 1
    assert db.set_favorite([r1], False) == 1
    assert db.get_resource(r1)["favorite"] == 0


def test_all_tags_only_counts_done_resources(ctx):
    """只统计已落盘的 —— 否则会出现"数量非 0 但点进去什么都没有"的标签。"""
    t = ctx.task()
    done = ctx.res(t, status="done", url="https://example.com/d.jpg")
    pending = ctx.res(t, status="pending", url="https://example.com/p.jpg")
    db.add_tags([done, pending], ["shared"])
    rows = [dict(r) for r in db.all_tags()]
    # ⚠️ 断言**字段的值**而不是整个字典: `all_tags` 后来又长了 `n_tree`/`parent`/
    # `depth`/`color`(层级与颜色), 拿 `== {...}` 去比会因为加了键就红 —— 那红的是
    # 断言写得太死, 不是产品坏了。这里要守的是"pending 那份没被算进来"。
    assert [r["tag"] for r in rows] == ["shared"]
    assert rows[0]["n"] == 1


def test_tags_cascade_when_resource_is_deleted(ctx):
    """外键级联: 删资源不能留下孤儿标签行(它们会污染标签清单的计数)。"""
    t = ctx.task()
    r1 = ctx.res(t)
    db.add_tags([r1], ["orphan"])
    db.execute("DELETE FROM resources WHERE id=?", (r1,))
    assert db.all_tags() == []
    assert db.tags_of([r1]) == {}


def test_library_count_by_tag_is_not_inflated_by_joins(ctx):
    """⚠️ 按标签筛必须用 EXISTS, 不能用 JOIN。

    一个资源带 3 个标签时 JOIN 会产生 3 行 —— 总数虚高、分页错位。这是资源库
    最难归因的一类 bug("翻到最后一页数量对不上")。
    """
    t = ctx.task()
    r1, _ = ctx.done(t, "a.jpg")
    ctx.done(t, "b.jpg")
    db.add_tags([r1], ["multi", "tag", "here"])

    assert db.library_count() == 2
    assert db.library_count(tag="multi") == 1, "带 3 个标签的资源仍只算 1 条"
    rows = db.library_list(tag="multi")
    assert len(rows) == 1


def test_library_count_combines_tag_and_favorite(ctx):
    """tag / favorite 是**两个独立维度**, 可叠加。"""
    t = ctx.task()
    r1, _ = ctx.done(t, "a.jpg")
    r2, _ = ctx.done(t, "b.jpg")
    db.add_tags([r1, r2], ["keep"])
    db.set_favorite([r1], True)

    assert db.library_count(tag="keep") == 2
    assert db.library_count(favorite=True) == 1
    assert db.library_count(tag="keep", favorite=True) == 1, "叠加后取交集"


def test_library_list_returns_tags_and_favorite(ctx):
    t = ctx.task()
    r1, _ = ctx.done(t, "a.jpg")
    db.add_tags([r1], ["hero"])
    db.set_favorite([r1], True)
    rows = db.library_list()
    assert len(rows) == 1


def test_library_stats_reports_favorites_and_tags(ctx):
    t = ctx.task()
    r1, _ = ctx.done(t, "a.jpg")
    ctx.done(t, "b.jpg")
    db.add_tags([r1], ["hero"])
    db.set_favorite([r1], True)
    st = db.library_stats()
    assert st["resources"] == 2
    assert st["favorites"] == 1
    assert st["tags"] == 1


# ---- API 层 ----

def test_api_tag_endpoints(ctx):
    """打标签 -> 查清单 -> 按标签筛, 走完整 HTTP 契约。"""
    t = ctx.task()
    r1, _ = ctx.done(t, "a.jpg")
    r2, _ = ctx.done(t, "b.jpg")
    c = _client()

    r = c.post("/library/tags", json={"ids": [r1, r2], "add": ["sunset", "海边"]})
    assert r.status_code == 200
    body = r.json()
    assert body["touched"] == 2 and body["added"] == 4

    tags = c.get("/library/tags").json()
    assert {x["tag"] for x in tags["items"]} == {"sunset", "海边"}
    assert all(x["n"] == 2 for x in tags["items"])

    got = c.get("/library", params={"tag": "sunset"}).json()
    assert got["total"] == 2
    assert all("sunset" in x["tags"] for x in got["items"])


def test_api_tag_remove_and_clear(ctx):
    t = ctx.task()
    r1, _ = ctx.done(t, "a.jpg")
    c = _client()
    c.post("/library/tags", json={"ids": [r1], "add": ["a", "b"]})

    assert c.post("/library/tags",
                  json={"ids": [r1], "remove": ["b"]}).json()["removed"] == 1
    assert c.post("/library/tags",
                  json={"ids": [r1], "clear": True}).json()["removed"] == 1
    assert c.get("/library/tags").json()["total"] == 0


def test_api_tag_validation_fails_the_whole_request(ctx):
    """校验失败要 400 并说清原因, 不能"能加的加上"。"""
    t = ctx.task()
    r1, _ = ctx.done(t, "a.jpg")
    c = _client()
    r = c.post("/library/tags",
               json={"ids": [r1], "add": ["x" * (db.MAX_TAG_LEN + 5)]})
    assert r.status_code == 400
    assert "标签太长" in r.json()["detail"]
    assert c.get("/library/tags").json()["total"] == 0, "失败就该一条都没打上"


def test_api_bad_id_is_rejected_not_silently_skipped(ctx):
    """非整数 id 必须被拒, 且**一条都不能写进去**。

    ⚠️ 实际是 422 而不是 400: `ids: List[int]` 在 pydantic 那一层就拦住了,
    请求根本没进业务代码。这比走到 `_unique_ids` 再抛 400 **更好** ——
    越早拒绝, 越不可能出现"处理了一半才发现入参有问题"。
    `_unique_ids` 那道仍有用: 它管**去重与保序**(见下面直接单测它的用例)。
    """
    t = ctx.task()
    r1, _ = ctx.done(t, "a.jpg")
    c = _client()
    r = c.post("/library/tags", json={"ids": [r1, "not-an-id"], "add": ["x"]})
    assert r.status_code == 422
    assert c.get("/library/tags").json()["total"] == 0, "被拒的请求不该留下任何副作用"


def test_unique_ids_dedupes_and_rejects_bad_values():
    """`_unique_ids` 的契约: 去重保序; 非法值 400 而不是静默跳过。"""
    assert T._unique_ids([3, 1, 3, 2, 1]) == [3, 1, 2], "要去重且保持原顺序"
    assert T._unique_ids([]) == []
    assert T._unique_ids(None) == []
    # 字符串数字是合法的(表单/JSON 都可能给成字符串)
    assert T._unique_ids(["7", 7]) == [7]
    with pytest.raises(T.HTTPException) as ei:
        T._unique_ids([1, "oops"])
    assert ei.value.status_code == 400


def test_api_favorite_endpoint(ctx):
    t = ctx.task()
    r1, _ = ctx.done(t, "a.jpg")
    ctx.done(t, "b.jpg")
    c = _client()

    r = c.post("/library/favorite", json={"ids": [r1], "value": True})
    assert r.status_code == 200 and r.json() == {"updated": 1, "value": True}

    got = c.get("/library", params={"favorite": "true"}).json()
    assert got["total"] == 1 and got["items"][0]["favorite"] is True

    c.post("/library/favorite", json={"ids": [r1], "value": False})
    assert c.get("/library", params={"favorite": "true"}).json()["total"] == 0


def test_api_library_filters_are_combined(ctx):
    """多维度同时生效: 标签 + 收藏 + 类型。"""
    t = ctx.task()
    img, _ = ctx.done(t, "a.jpg", rtype="image")
    vid, _ = ctx.done(t, "v.mp4", rtype="video")
    db.add_tags([img, vid], ["keep"])
    db.set_favorite([vid], True)
    c = _client()
    got = c.get("/library", params={"tag": "keep", "favorite": "true",
                                    "kind": "video"}).json()
    assert got["total"] == 1 and got["items"][0]["id"] == vid


def test_api_partials_endpoints(ctx):
    """暂存区的可见性与清理。它是"能省下来的东西", 用户有权看见并还回去。"""
    url = "https://example.com/big.mp4"
    part = ctx.root / "big.mp4.part"
    part.write_bytes(b"P" * 7000)
    partials.park(part, url)

    c = _client()
    st = c.get("/library/partials").json()
    assert st["count"] == 1 and st["bytes"] == 7000
    assert st["items"][0]["url"] == url
    # 单位换算由后端统一给出(`core/filters.fmt_size`), 前端各写一套迟早对不上
    assert "bytes_h" in st and "max_bytes_h" in st
    # "会自己过期"要一起下发: 否则用户以为它会无限涨
    assert st["ttl_hours"] > 0 and st["max_bytes"] > 0

    r = c.delete("/library/partials").json()
    assert r["cleared"] == 1 and r["bytes"] == 7000
    assert "bytes_h" in r
    assert c.get("/library/partials").json()["count"] == 0
