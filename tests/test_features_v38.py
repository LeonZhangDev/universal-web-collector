"""V38 测试: DASH 两条"以前明确报错"的路 —— 嵌套 `sidx` 与直播录制。

为什么要把"明确报错"改成"支持"
================================
V35/V36 立过一条规矩: **认不出就拒绝**(与 `mediacheck` 同源)。那是对的 ——
但"拒绝"的前提是"我们确实做不了"。这两类不是做不了, 是当时没做:

* `sidx` 里的 `reference_type=1`(嵌套索引) —— 以前直接报错。可它的结构并不新奇:
  只是"索引里还有索引", 取回那一小段字节再解一次就完了。
* `type="dynamic"`(直播) —— 以前直接报错。可用户把直播清单丢进来, 他想要的是
  **录一段**, 不是"被告知这不能下"。直播没有"下完"这回事, 所以缺的不是能力而是
  **一个结束条件**(时间上限), 那是产品决策, 不是技术欠债。

四条判据(每条都对应一个"做错了也不报错"的失败模式)
==================================================
1. **嵌套索引必须原地 DFS**。子层分片在文件里的位置就是紧挨着那个索引 box 的,
   所以顺序 = 原地展开的顺序。先收父层媒体、事后再补子层 → 子层分片被排到父层
   后半段之后, 拼出来长度对得上、能播、内容是错位的。
2. **直播不回头**。窗口一直往前滚。回头的两个后果: 对已滚出的分片发请求(全 404),
   以及流结束时清单变成 static(带完整时长) → 从节目开头重下一整遍。
3. **显式列出的分片不拿时钟裁**。清单既然一条条写出来了, 那就是它声明可用的范围。
   用时钟裁的话, 时钟偏一点就把该录的分片裁掉了 —— 那是静默少录。
4. **"漏了几片"必须有人知道**。它不影响退出码, 也不影响能不能播, 用户唯一的感知
   是"这几秒怎么跳过去了"。所以它必须出现在日志里。

另外两条**行为差异**是故意的, 各自有测试钉住:
* 直播取消时**仍然封文件**(点播取消留的是能续传的半成品; 直播窗口滚过去补不回来);
* 点播里 `r="-1"` 仍然报错(那个"末尾"= 时段末尾, 需要时长; 直播里"末尾"= 窗口末尾)。

测试里一律用**假时钟**推进窗口, 不 sleep、不依赖墙钟。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import pytest

import downloaders.video as V
from core.cancel import TaskCancelled
from downloaders.dash import parse_iso_time, parse_mpd, parse_sidx, parse_sidx_refs
from downloaders.video import VideoDownloader, _seg_key

AST = "2026-09-23T10:00:00Z"
_AST_EPOCH = parse_iso_time(AST)


# ==================================================================
# 夹具 / 替身
# ==================================================================

class _Resp:
    """一个够用的假响应: `_fetch_mpd` 要 text/headers, 分片路径要 iter_content。"""

    def __init__(self, body, status=200, headers=None):
        self.content = body if isinstance(body, bytes) else str(body).encode()
        self.text = self.content.decode("utf-8", "replace")
        self.status_code = status
        self.headers = dict(headers or {})
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, size=65536):
        for i in range(0, len(self.content), size):
            yield self.content[i:i + size]

    def close(self):
        self.closed = True


class _Session:
    """URL -> 响应体。三条能力都是测试必需的:

    * 同一个 URL 给**一串**体(直播清单就是这样一轮一轮滚的, 最后一个会一直粘住);
    * 列表里放 `Exception` 就抛出去(取清单失败);
    * `Range` 头会被真的切片(`sidx` 那条路要靠它)。
    """

    def __init__(self, routes=None, fallback=None, missing=(), on_get=None):
        self.routes = {}
        for k, v in (routes or {}).items():
            self.routes[k] = list(v) if isinstance(v, list) else [v]
        self.fallback = fallback
        self.missing = set(missing)
        self.on_get = on_get
        self.calls = []
        self.returned = []

    def get(self, url, headers=None, timeout=None, **kw):
        headers = dict(headers or {})
        self.calls.append((url, headers))
        if self.on_get:
            self.on_get(url)
        if url in self.missing:
            r = _Resp(b"", 404)
        elif url in self.routes:
            q = self.routes[url]
            item = q.pop(0) if len(q) > 1 else q[0]
            if isinstance(item, Exception):
                raise item
            r = item if isinstance(item, _Resp) else _Resp(item)
        elif self.fallback is not None:
            r = _Resp(self.fallback(url))
        else:
            r = _Resp(b"", 404)
        rng = headers.get("Range")
        if rng and r.status_code == 200 and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            r = _Resp(r.content[int(a):int(b) + 1], 206)
        self.returned.append(r)
        return r

    def close(self):
        pass

    def urls(self, needle):
        return [u for u, _h in self.calls if needle in u]


class _Clock:
    """假时钟: `time()` 由测试(或会话)推进, `monotonic()` 每读一次走一步。

    ⚠️ 不用真 sleep 是**必需**的, 不是图快: 直播的轮询间隔 (`LIVE_MIN_POLL`) 是
    2 秒, 靠墙钟跑一条用例要好几秒, 而且"到点没到点"本身会变成 flaky 的来源。
    """

    def __init__(self, start=0.0, step=1.0):
        self.t = start
        self.step = step
        self.slept = 0.0

    def time(self):
        return self.t

    def monotonic(self):
        self.t += self.step
        return self.t

    def sleep(self, seconds):
        self.slept += seconds


def _plain_body(url):
    """分片体的默认来源: 用文件名当内容 —— 拼出来的字节顺序一眼能读。"""
    return url.rsplit("/", 1)[-1].encode()


def _downloader(monkeypatch, session, clock=None):
    monkeypatch.setattr(V.settings, "video_engine", "builtin")
    monkeypatch.setattr(V, "find_ffmpeg", lambda: None)
    monkeypatch.setattr(V.settings, "segment_retries", 1)
    monkeypatch.setattr(V.settings, "segment_min_interval", 0)
    monkeypatch.setattr(V.settings, "segment_max_interval", 0)
    monkeypatch.setattr(V.settings, "segment_concurrency", 4)
    # 默认给一个"永远到不了"的上限, 免得某条用例意外撞上 300s 的默认值
    monkeypatch.setattr(V.settings, "live_max_seconds", 10 ** 6)
    if clock is not None:
        monkeypatch.setattr(V, "time", clock)
    d = VideoDownloader()
    d.session = session
    return d


# ==================================================================
# 1. 分片身份: 字节区间是身份的一部分
# ==================================================================

def test_seg_key_treats_a_byte_range_as_part_of_the_identity():
    """同一个 URL 的不同字节区间是**不同的分片**。

    `SegmentBase` / `mediaRange` 的整条轨都挂在同一个 URL 上, 只用 URL 当身份的话
    "这一片下过没有"永远答"下过" —— 表现是**整条轨只下第一片**, 静默截断。
    """
    a = _seg_key(("https://h/v.mp4", (0, 99)))
    b = _seg_key(("https://h/v.mp4", (100, 199)))
    assert a != b
    assert a == _seg_key(("https://h/v.mp4", (0, 99)))      # 可哈希且稳定
    assert _seg_key("https://h/v.mp4") == "https://h/v.mp4"
    # 区间坏掉时退回 URL 而不是抛: 这条路只用来"记过账没有"
    assert _seg_key(("https://h/v.mp4", None)) == "https://h/v.mp4"


# ==================================================================
# 2. ISO-8601: 直播窗口全靠它减
# ==================================================================

def test_parse_iso_time_reads_utc_and_treats_a_missing_zone_as_utc():
    """没带时区的按 **UTC** 解释。

    本机在 UTC+8: 按本地时区解释会让窗口整体偏 8 小时 —— 算出来全是早已滚走的
    分片, 报错却指向"下载失败", 与真实原因毫无关系。
    """
    assert parse_iso_time("2026-09-23T10:00:00Z") == _AST_EPOCH
    assert parse_iso_time("2026-09-23T10:00:00+00:00") == _AST_EPOCH
    assert parse_iso_time("2026-09-23T10:00:00") == _AST_EPOCH       # 无时区 == UTC
    assert parse_iso_time("2026-09-23T18:00:00+08:00") == _AST_EPOCH  # 显式偏移要认
    assert parse_iso_time("2026-09-23T10:00:00.500Z") == _AST_EPOCH + 0.5


def test_parse_iso_time_returns_none_instead_of_zero():
    """认不出返回 `None`, **不是** 0。

    当成 0 会算出一个"从上世纪开始"的窗口 → 请求几万片早就滚走的分片, 全是 404,
    而报错指向"下载失败"。None 会让需要时钟的形态明确报错。
    """
    assert parse_iso_time(None) is None
    assert parse_iso_time("") is None
    assert parse_iso_time("   ") is None
    assert parse_iso_time("PT30S") is None            # 这是时长, 不是时间戳
    assert parse_iso_time("2026-13-45T99:99:99Z") is None


# ==================================================================
# 3. 嵌套 sidx
# ==================================================================

def _sidx_box(refs, first_offset=0, version=0):
    """手造 sidx box。`refs` = `[(引用类型, 字节数), ...]`(类型 1 = 嵌套索引)。"""
    body = bytearray()
    body += bytes([1 if version == 1 else 0]) + b"\x00\x00\x00"
    body += (7).to_bytes(4, "big")                    # reference_ID
    body += (1000).to_bytes(4, "big")                 # timescale
    if version == 1:
        body += (0).to_bytes(8, "big") + first_offset.to_bytes(8, "big")
    else:
        body += (0).to_bytes(4, "big") + first_offset.to_bytes(4, "big")
    body += (0).to_bytes(2, "big")                    # reserved
    body += len(refs).to_bytes(2, "big")
    for ref_type, n in refs:
        body += ((ref_type << 31) | n).to_bytes(4, "big")
        body += (0).to_bytes(4, "big")                # subsegment_duration
        body += (0).to_bytes(4, "big")                # SAP
    return (8 + len(body)).to_bytes(4, "big") + b"sidx" + bytes(body)


def _nested_layout():
    """造一个真的嵌套索引文件, 返回 `(body, 期望的展开顺序)`。

    文件布局(偏移)→ 内容::

        0        父 sidx (3 条引用: 媒体10 / 嵌套索引67 / 媒体30)
        68..77   媒体 10 字节
        78       子 sidx (2 条引用: 媒体5 / 媒体6)   <-- 父的第 2 条指向这里
        134..138 子层媒体 5 字节
        139..144 子层媒体 6 字节
        145..174 媒体 30 字节

    ⚠️ 父的**第 3 条引用**排在子层媒体之后 —— 这正是"先收父层媒体再补子层"会
    排错的地方。
    """
    parent = _sidx_box([(0, 10), (1, 67), (0, 30)])
    sub = _sidx_box([(0, 5), (0, 6)])
    assert len(parent) == 68 and len(sub) == 56
    body = bytearray(b"\x00" * 175)
    body[0:68] = parent
    body[78:134] = sub
    return bytes(body), [(68, 77), (134, 138), (139, 144), (145, 174)]


def test_parse_sidx_refs_distinguishes_nested_index_references_in_order():
    parent = _sidx_box([(0, 10), (1, 67), (0, 30)])
    refs = parse_sidx_refs(parent, 0)
    assert refs == [("media", (68, 77)), ("index", (78, 144)), ("media", (145, 174))]


def test_parse_sidx_still_refuses_a_nested_index():
    """单层视图遇到嵌套索引必须报错。

    不能"忽略那一层": 忽略等于静默少下嵌套索引覆盖的全部内容 —— 长度对得上但
    内容是断的。要支持嵌套的调用方走 `parse_sidx_refs` + `_expand_sidx`。
    """
    parent = _sidx_box([(0, 10), (1, 67)])
    with pytest.raises(ValueError) as ei:
        parse_sidx(parent, 0)
    assert "嵌套" in str(ei.value)


def test_expand_sidx_interleaves_nested_children_in_file_order(monkeypatch, tmp_path):
    """**这条钉住原地 DFS**。

    错误实现(先收集父层媒体、事后再补子层)会给出
    `[68-77, 145-174, 134-138, 139-144]` —— 长度对得上、能播、每几秒错一段。
    """
    body, want = _nested_layout()
    media = "https://h/v.mp4"
    s = _Session({media: body})
    d = _downloader(monkeypatch, s, _Clock())

    assert d._expand_sidx(media, 0, body[0:68], {}) == want
    # 只取回索引那一小段(56 字节), 不是整个文件
    assert s.calls[0][1]["Range"] == "bytes=78-144"


def test_expand_sidx_stops_at_the_depth_limit(monkeypatch, tmp_path):
    """索引引用可以成环 —— 到上限**报错**, 不截断(截断就是静默少下几片)。

    造法是让每一层的 box 都指向紧挨着的下一份**同形** box, 于是深度真的无限。
    """
    leaf = _sidx_box([(1, 44)])            # 自指的尺寸: 指向下一份同形 box
    assert len(leaf) == 44
    body = leaf * 20                       # 每一层都能取到一份合法的下一层
    media = "https://h/v.mp4"
    s = _Session({media: body})
    d = _downloader(monkeypatch, s, _Clock())
    with pytest.raises(RuntimeError) as ei:
        d._expand_sidx(media, 0, body[0:44], {})
    assert str(V.SIDX_MAX_DEPTH) in str(ei.value)
    assert len(s.calls) == V.SIDX_MAX_DEPTH, "到上限就停, 不能无限请求下去"


def test_expand_sidx_stops_at_the_index_count_limit(monkeypatch):
    """一层里塞几百个索引引用也不行 —— 又一个"无限请求"的入口。"""
    n = V.SIDX_MAX_INDEXES + 2
    parent = _sidx_box([(1, 44)] * n)
    leaf = _sidx_box([(0, 5)])
    body = parent + leaf * (n + 4)
    media = "https://h/v.mp4"
    s = _Session({media: body})
    d = _downloader(monkeypatch, s, _Clock())
    with pytest.raises(RuntimeError) as ei:
        d._expand_sidx(media, 0, parent, {})
    assert str(V.SIDX_MAX_INDEXES) in str(ei.value)
    assert len(s.calls) == V.SIDX_MAX_INDEXES, "到上限就停, 不能无限请求下去"


def test_expand_sidx_refuses_an_index_without_any_media(monkeypatch):
    """一条媒体引用都没有的索引是**不可用**, 不是"空文件"。

    报错出自 `parse_sidx_refs` —— 展开这层就没有媒体, 往上也是白搭, 所以在那里
    就断掉(错误信息也说明是"这份索引"的问题, 而不是"下载失败")。
    """
    box = _sidx_box([])
    s = _Session({"https://h/v.mp4": box})
    d = _downloader(monkeypatch, s, _Clock())
    with pytest.raises(ValueError) as ei:
        d._expand_sidx("https://h/v.mp4", 0, box, {})
    assert "没有任何" in str(ei.value)
    assert s.calls == [], "读不出引用的索引不该再去请求别的东西"


def test_resolve_index_walks_a_nested_index_end_to_end(monkeypatch):
    """`SegmentBase` 的入口: 嵌套索引也照样解成分片区间。"""
    body, want = _nested_layout()
    media = "https://h/v.mp4"
    s = _Session({media: body})
    d = _downloader(monkeypatch, s, _Clock())
    track = {"segments": [], "init": None,
             "index": {"kind": "sidx", "media": media, "range": (0, 67)}}
    d._resolve_index(track, {})
    assert track["segments"] == [(media, r) for r in want]


# ==================================================================
# 4. 字节区间取回
# ==================================================================

def test_fetch_range_returns_the_bytes_of_that_window(monkeypatch):
    media = "https://h/v.mp4"
    s = _Session({media: bytes(range(256))})
    d = _downloader(monkeypatch, s, _Clock())
    assert d._fetch_range(media, (10, 19), {}) == bytes(range(10, 20))


def test_fetch_range_refuses_a_server_that_ignores_range(monkeypatch):
    """服务器忽略 `Range` 时明确报错。

    它返回的整份文件从偏移 0 开始, 而调用方是拿"索引区间的绝对偏移"去解它的 ——
    分片边界会整体错位, 拼出来长度对得上、能播。
    """
    media = "https://h/v.mp4"
    s = _Session({media: _Resp(b"x" * 5000)})
    # 把 Range 切片关掉: 模拟忽略 Range 的服务器
    s.get = lambda url, headers=None, **kw: _Resp(b"x" * 5000, 200)
    d = _downloader(monkeypatch, s, _Clock())
    with pytest.raises(RuntimeError) as ei:
        d._fetch_range(media, (0, 67), {})
    assert "没有按区间返回" in str(ei.value)


# ==================================================================
# 5. 直播清单: 窗口怎么算
# ==================================================================

def _timeline_mpd(entries, *, kind="dynamic", tsbd="PT20S", mup="PT4S",
                  pub="2026-09-23T10:00:30Z", ast=AST, tid="v",
                  dur="PT8S", start="PT0S"):
    """`SegmentTimeline` 形态。`entries` = `[("0", "2000", "2"), ...]`。"""
    s = "".join(
        f'<S{"" if t is None else f" t=\"{t}\""} d="{d}"'
        f'{"" if r is None else f" r=\"{r}\""}/>'
        for t, d, r in entries)
    attr = (f' availabilityStartTime="{ast}" publishTime="{pub}"'
            f' minimumUpdatePeriod="{mup}" timeShiftBufferDepth="{tsbd}"'
            if kind == "dynamic" else ' mediaPresentationDuration="PT8S"')
    return (
        f'<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="{kind}"{attr}>'
        f'<Period start="{start}"><AdaptationSet contentType="video">'
        f'<Representation id="{tid}" bandwidth="800000">'
        f'<SegmentTemplate media="{tid}/$Time$.m4s" initialization="{tid}/init.mp4"'
        f' timescale="1000" startNumber="1"><SegmentTimeline>{s}</SegmentTimeline>'
        '</SegmentTemplate></Representation></AdaptationSet></Period></MPD>')


def _duration_mpd(*, tsbd="PT20S", mup="PT4S", pub="2026-09-23T10:00:30Z",
                  ast=AST, tid="v", seg="2000", mime="video/mp4"):
    """`SegmentTemplate@duration` 均分形态 —— 清单里一个分片都没写, 个数只能由窗口算。"""
    return (
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="dynamic"'
        f' availabilityStartTime="{ast}" publishTime="{pub}"'
        f' minimumUpdatePeriod="{mup}" timeShiftBufferDepth="{tsbd}">'
        '<Period start="PT0S"><AdaptationSet contentType="video"'
        f' mimeType="{mime}"><Representation id="{tid}" bandwidth="800000">'
        f'<SegmentTemplate media="{tid}/$Time$.m4s" duration="{seg}"'
        ' timescale="1000" startNumber="1"/>'
        '</Representation></AdaptationSet></Period></MPD>')


def _live(text, now=None):
    return parse_mpd(text, "https://live/m.mpd", now=now, allow_dynamic=True)


def _times(track):
    """分片 URL 里的时间戳 —— 直播的序号是算出来的, 直接看 URL 最省事。"""
    out = []
    for seg in track["segments"]:
        url = seg[0] if isinstance(seg, tuple) else seg
        out.append(int(url.rsplit("/", 1)[-1].split(".")[0]))
    return out


def test_dynamic_is_refused_unless_the_caller_opted_in():
    """老行为必须留着: 不开口就不录直播。

    解析器不替调用方决定"录不录" —— 一个自动跑 5 分钟的任务不该是默认行为。
    """
    text = _duration_mpd()
    with pytest.raises(ValueError) as ei:
        parse_mpd(text, "https://live/m.mpd")
    assert "dynamic" in str(ei.value) and "直播" in str(ei.value)


def test_live_window_is_anchored_on_the_clock_and_the_buffer_depth():
    """窗口 = `[now - timeShiftBufferDepth, now]`。

    `now` = 发布时刻 +30s, 回溯 20s → 窗口 [10s, 30s]; 分片 2s 一片 → 第 5 片到
    第 15 片(含), 共 11 片。
    """
    spec = _live(_duration_mpd(), now=_AST_EPOCH + 30)
    assert spec["live"] is True
    assert spec["time_shift_buffer_depth"] == 20.0
    assert spec["minimum_update_period"] == 4.0
    assert _times(spec["video"]) == [10000 + 2000 * i for i in range(11)]


def test_live_window_falls_back_to_the_manifest_publish_time():
    """没给 `now` 就用清单自己的 `publishTime` —— 它的定义就是"这份清单的现在"。"""
    a = _live(_duration_mpd(pub="2026-09-23T10:00:30Z"), now=None)
    b = _live(_duration_mpd(pub="2026-09-23T10:00:30Z"), now=_AST_EPOCH + 30)
    assert _times(a["video"]) == _times(b["video"])


def test_live_without_a_clock_does_not_guess():
    """推不出"现在"就不猜。

    猜(比如把窗口起点当 0)会算出几万片早已滚走的分片, 请求全 404 —— 报错指向
    "下载失败", 与真实原因(时钟缺失)隔了两层。
    """
    text = _duration_mpd(pub=None)
    text = text.replace(' publishTime="None"', "")
    with pytest.raises(ValueError) as ei:
        _live(text, now=None)
    assert "窗口" in str(ei.value)


def test_live_without_availability_start_says_so():
    """没有 `availabilityStartTime` 就没有窗口原点 —— 说明写在 notes 里, 而不是静默。"""
    text = _duration_mpd().replace(f' availabilityStartTime="{AST}"', "")
    with pytest.raises(ValueError) as ei:
        _live(text, now=_AST_EPOCH + 30)
    assert "窗口" in str(ei.value)


def test_live_does_not_crop_segments_the_manifest_explicitly_lists():
    """清单一条条写出来的分片**不拿时钟裁**。

    时钟偏一点(或 tsbd 填得比实际小)时, 裁剪会把本该录到的有效分片裁掉 ——
    那是静默少录。清单声明了可用范围, 就以清单为准。
    """
    # 窗口算出来是 [10s, 30s], 但清单显式列了 t=0/2000/4000(在窗口之外)
    spec = _live(_timeline_mpd([("0", "2000", "2")]), now=_AST_EPOCH + 30)
    assert _times(spec["video"]) == [0, 2000, 4000]


def test_live_expands_r_minus_one_up_to_the_window_end():
    """`r="-1"` 在直播里 = "重复到**可用窗口末尾**"(不是时段末尾)。

    用错(当成时段末尾)会永远只录到第一次取回的那几片, 而且不报错。
    """
    spec = _live(_timeline_mpd([("0", "2000", "2"), (None, "2000", "-1")]),
                 now=_AST_EPOCH + 18)          # tsbd 20 -> 起点 0, 末尾 18s
    # 前三条 t=0/2000/4000, 之后 r=-1 到窗口末尾(18s) -> 6000/8000/10000/12000...
    assert _times(spec["video"]) == [0, 2000, 4000, 6000, 8000, 10000, 12000,
                                     14000, 16000, 18000]


def test_static_timeline_with_r_minus_one_still_refuses():
    """点播里的 `r="-1"` 仍然报错: 那个"末尾"= 时段末尾, 得先解出时长。

    不猜 —— 猜错会少下尾部一截, 而且不报错。
    """
    text = _timeline_mpd([(None, "2000", "-1")], kind="static")
    with pytest.raises(ValueError) as ei:
        parse_mpd(text, "https://live/m.mpd")
    assert "r=-1" in str(ei.value)


def test_live_clamps_the_window_start_at_the_stream_start():
    """`timeShiftBufferDepth` 比开播时长还长时, 起点夹到 0 —— 不是负数。"""
    spec = _live(_duration_mpd(tsbd="PT9999S"), now=_AST_EPOCH + 30)
    assert _times(spec["video"])[0] == 0


def test_live_notes_say_how_long_the_window_is():
    spec = _live(_duration_mpd(), now=_AST_EPOCH + 30)
    assert any("可用窗口 20s" in n for n in spec["notes"])


def test_live_without_minimum_update_period_warns_it_records_one_round():
    """没有 `minimumUpdatePeriod` 就不知道新分片何时出现 → 只录一轮。

    这是**必须说出来**的: 用户以为"录了 5 分钟", 实际只有取回那一刻的两秒。
    """
    text = _duration_mpd().replace(' minimumUpdatePeriod="PT4S"', "")
    spec = _live(text, now=_AST_EPOCH + 30)
    assert any("minimumUpdatePeriod" in n for n in spec["notes"])


# ==================================================================
# 6. 直播录制: 滚动窗口 / 只往前 / 何时停
# ==================================================================

def _record(d, spec, out, stem="rec", logs=None, progress_cb=None, mirrors=None):
    """跑一次直播录制, 返回 `(成品路径, 日志行, info 字典)`。"""
    logs = logs if logs is not None else []
    info = {}
    final, _sha = d._record_live("https://live/m.mpd", spec, {}, Path(out), stem,
                                 progress_cb, logs.append, info, mirrors)
    return final, logs, info


def test_live_recording_only_takes_the_segments_it_has_not_seen(
        monkeypatch, tmp_path):
    """**这次改动的核心**: 每轮只下新出现的分片, 且按发现顺序拼。

    第一轮的清单列了 t=10000/12000/14000; 第二轮(源站已切成 static)列了
    12000/14000/16000 —— 只有 16000 是新的。
    """
    murl = "https://live/m.mpd"
    s = _Session({murl: [_timeline_mpd([("12000", "2000", "2")], kind="static")]},
                 fallback=_plain_body)
    d = _downloader(monkeypatch, s, _Clock(start=_AST_EPOCH + 30, step=0.001))
    spec = _live(_timeline_mpd([("10000", "2000", "2")]), now=_AST_EPOCH + 30)

    final, logs, info = _record(d, spec, tmp_path / "o")
    # 初始化段在最前, 然后严格按发现顺序
    assert final.read_bytes() == (b"init.mp4" + b"10000.m4s" + b"12000.m4s"
                                  + b"14000.m4s" + b"16000.m4s")
    for name in ("init.mp4", "10000.m4s", "12000.m4s", "14000.m4s", "16000.m4s"):
        assert len(s.urls("/" + name)) == 1, f"{name} 应该只请求一次"
    assert any("清单已变成 static" in m for m in logs)
    assert info.get("duration") is None       # 没 ffprobe 就不编一个 0 出来


def test_live_recording_never_goes_back_to_what_it_already_passed(
        monkeypatch, tmp_path):
    """窗口滚过我们的末尾时, 只往前录 —— 不回头发请求。

    回头的第一个后果是给已滚出的分片发请求(全 404); 第二个更贵: 流结束时源站会
    把清单换成 static(带完整时长), 那时"回头"= 从节目开头重下一整遍。
    """
    murl = "https://live/m.mpd"
    s = _Session({murl: [_timeline_mpd([("6000", "2000", "1")], kind="static")]},
                 fallback=_plain_body)
    d = _downloader(monkeypatch, s, _Clock(start=_AST_EPOCH + 30, step=0.001))
    spec = _live(_timeline_mpd([("0", "2000", "2")]), now=_AST_EPOCH + 30)

    final, logs, _info = _record(d, spec, tmp_path / "o")
    assert final.read_bytes() == b"init.mp4" + b"0.m4s" + b"2000.m4s" + b"4000.m4s" \
        + b"6000.m4s" + b"8000.m4s"
    assert len(s.urls("/v/0.m4s")) == 1       # 0.m4s 只在下第一轮时取过
    assert any("滚过" in m for m in logs)


def test_live_recording_with_no_segment_at_all_is_a_hard_failure(
        monkeypatch, tmp_path):
    """一片都没取到 = 地址模板/鉴权/时钟之一不对 → 明确失败。

    不产出一个"能播但空的"文件: 那会让用户以为录成功了。
    """
    murl = "https://live/m.mpd"
    s = _Session({murl: _timeline_mpd([("0", "2000", "0")], kind="static")},
                 fallback=_plain_body,
                 missing={"https://live/v/init.mp4", "https://live/v/0.m4s"})
    d = _downloader(monkeypatch, s, _Clock(start=_AST_EPOCH + 30, step=0.001))
    spec = _live(_timeline_mpd([("0", "2000", "0")]), now=_AST_EPOCH + 30)

    with pytest.raises(RuntimeError) as ei:
        _record(d, spec, tmp_path / "o")
    assert "一个分片都没取到" in str(ei.value)
    assert not (tmp_path / "o" / "rec.mp4").exists()


def test_live_recording_stops_at_the_time_limit(monkeypatch, tmp_path):
    """时间上限到了就收工 —— 直播没有"下完"这回事, 结束条件是我们给的。"""
    murl = "https://live/m.mpd"
    # 第二轮拿回来的还是同一份动态清单(没有新分片), 于是只能靠时间停下来
    s = _Session({murl: [_timeline_mpd([("0", "2000", "0")])]},
                 fallback=_plain_body)
    d = _downloader(monkeypatch, s, _Clock(start=_AST_EPOCH + 30, step=1.0))
    monkeypatch.setattr(V.settings, "live_max_seconds", 1)
    spec = _live(_timeline_mpd([("0", "2000", "0")]), now=_AST_EPOCH + 30)

    final, logs, _info = _record(d, spec, tmp_path / "o")
    assert final.exists()
    assert any("上限" in m for m in logs)


def test_live_recording_gives_up_after_repeated_manifest_failures(
        monkeypatch, tmp_path):
    """连续取不回清单就放弃 —— 否则这是一个永远不会结束的任务。"""
    murl = "https://live/m.mpd"
    s = _Session({murl: RuntimeError("boom")}, fallback=_plain_body)
    d = _downloader(monkeypatch, s, _Clock(start=_AST_EPOCH + 30, step=0.001))
    spec = _live(_timeline_mpd([("0", "2000", "0")]), now=_AST_EPOCH + 30)

    with pytest.raises(RuntimeError) as ei:
        _record(d, spec, tmp_path / "o")
    assert f"连续 {V.LIVE_MAX_FETCH_FAILURES} 次" in str(ei.value)
    assert len(s.urls("m.mpd")) == V.LIVE_MAX_FETCH_FAILURES


def test_cancelling_a_live_recording_still_keeps_what_was_recorded(
        monkeypatch, tmp_path):
    """⚠️ 与点播**故意不同**: 取消时仍把录到的封成文件。

    点播取消留的是"下次能接着下"的半成品; 直播窗口滚过去就补不回来了 ——
    取消那一刻手上的分片就是全部产物, 丢掉等于把已经花掉的时间扔了。
    任务状态照旧是"已取消"(异常原样穿出去)。
    """
    murl = "https://live/m.mpd"
    served = {"n": 0}

    def on_get(url):
        if url != murl:
            served["n"] += 1

    s = _Session({murl: _timeline_mpd([("10000", "2000", "2")])},
                 fallback=_plain_body, on_get=on_get)
    d = _downloader(monkeypatch, s, _Clock(start=_AST_EPOCH + 30, step=0.001))
    spec = _live(_timeline_mpd([("10000", "2000", "2")]), now=_AST_EPOCH + 30)

    def cb(_n=0):
        if served["n"] >= 4:              # 4 片都发出去之后, 用户点了停止
            raise TaskCancelled("user")

    with pytest.raises(TaskCancelled):
        _record(d, spec, tmp_path / "o", progress_cb=cb)

    final = tmp_path / "o" / "rec.mp4"
    assert final.exists(), "取消也必须把已录到的封成文件"
    # 第 4 片写到一半被打断, 所以它不在产物里; 前三片完整
    assert final.read_bytes() == b"init.mp4" + b"10000.m4s" + b"12000.m4s"


def test_live_recording_reports_the_segments_it_missed(monkeypatch, tmp_path):
    """漏了几片必须出现在日志/结果里。

    它不影响退出码, 也不影响能不能播 —— 用户唯一的感知是"这几秒怎么跳过去了",
    而没有任何地方会告诉他。分片编号会因此出现空洞, 但**拼接顺序不依赖编号**。
    """
    murl = "https://live/m.mpd"
    s = _Session({murl: [_timeline_mpd([("2000", "2000", "2")], kind="static")]},
                 fallback=_plain_body, missing={"https://live/v/2000.m4s"})
    d = _downloader(monkeypatch, s, _Clock(start=_AST_EPOCH + 30, step=0.001))
    spec = _live(_timeline_mpd([("0", "2000", "2")]), now=_AST_EPOCH + 30)

    final, logs, _info = _record(d, spec, tmp_path / "o")
    assert final.read_bytes() == b"init.mp4" + b"0.m4s" + b"4000.m4s" + b"6000.m4s"
    assert any("1 片没取到" in m for m in logs)
    assert any("没取到" in m and "滚出窗口" in m for m in logs)


def test_live_recording_cleans_up_the_work_dir_on_success(monkeypatch, tmp_path):
    """成品落盘后分片目录要清掉 —— 否则一次 5 分钟的录制在磁盘上留两份。"""
    murl = "https://live/m.mpd"
    s = _Session({murl: [_timeline_mpd([("0", "2000", "0")], kind="static")]},
                 fallback=_plain_body)
    d = _downloader(monkeypatch, s, _Clock(start=_AST_EPOCH + 30, step=0.001))
    spec = _live(_timeline_mpd([("0", "2000", "0")]), now=_AST_EPOCH + 30)

    final, _logs, _info = _record(d, spec, tmp_path / "o")
    assert final.exists()
    assert not (tmp_path / "o" / ".rec.dash").exists()


def test_live_recording_keeps_a_separate_window_per_period(
        monkeypatch, tmp_path):
    """**多时段时每段各有自己的滚动窗口**。

    只按 `kind` 记"上一轮的末尾"的话, 后面时段的末尾会被当成整条轨的末尾, 于是
    前面时段新出现的分片全被判成"已经过去了"跳过 —— 录出来的文件缺了那几秒,
    而且不报错。这条用例就是为了让那种实现红。
    """
    def two_periods(kind, tsbd="PT20S", mup="PT4S", pub="2026-09-23T10:00:30Z"):
        def period(pid, start):
            return (f'<Period id="{pid}" start="{start}">'
                    '<AdaptationSet contentType="video">'
                    f'<Representation id="{pid}" bandwidth="800000">'
                    f'<SegmentTemplate media="{pid}/$Time$.m4s" duration="2000"'
                    ' timescale="1000" startNumber="1"/>'
                    '</Representation></AdaptationSet></Period>')
        if kind == "dynamic":
            attr = (f' availabilityStartTime="{AST}" publishTime="{pub}"'
                    f' minimumUpdatePeriod="{mup}" timeShiftBufferDepth="{tsbd}"')
        else:
            attr = ' mediaPresentationDuration="PT60S"'
        return (f'<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="{kind}"{attr}>'
                + period("p0", "PT0S") + period("p1", "PT20S") + "</MPD>")

    def timeline(pid, start, t0, n):
        return (f'<Period id="{pid}" start="{start}"><AdaptationSet contentType="video">'
                f'<Representation id="{pid}" bandwidth="800000">'
                f'<SegmentTemplate media="{pid}/$Time$.m4s" timescale="1000"'
                f' startNumber="1"><SegmentTimeline>'
                f'<S t="{t0}" d="2000" r="{n}"/></SegmentTimeline>'
                '</SegmentTemplate></Representation></AdaptationSet></Period>')

    murl = "https://live/m.mpd"
    # 第二轮: 两个时段的窗口都往前走了。⚠️ p1 的清单**保留了上一轮的末尾**
    # (t=4000..14000 含 10000) —— 这正是"按 kind 记末尾"那个实现会踩的地方:
    # 它会在 p1 的旧末尾处切断, 于是 p0 的两片新分片被当成"已经过去了"。
    static = ('<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"'
              ' mediaPresentationDuration="PT60S">'
              + timeline("p0", "PT0S", 32000, 2)
              + timeline("p1", "PT20S", 4000, 5) + "</MPD>")
    s = _Session({murl: [static]}, fallback=_plain_body)
    d = _downloader(monkeypatch, s, _Clock(start=_AST_EPOCH + 30, step=0.001))
    # p0 的窗口是 [10s, 30s] -> t=10000..30000; p1 的窗口是 [0s, 10s] -> t=0..10000
    spec = _live(two_periods("dynamic"), now=_AST_EPOCH + 30)
    assert _times(spec["periods"][0]["video"]) == [10000 + 2000 * i for i in range(11)]
    assert _times(spec["periods"][1]["video"]) == [2000 * i for i in range(6)]

    final, _logs, _info = _record(d, spec, tmp_path / "o")
    body = final.read_bytes()
    # 两个时段各只下**新出现的**分片, 且都是按各自的窗口算出来的
    assert len(s.urls("p0/32000.m4s")) == 1, "p0 的新分片不能被末尾的时段的窗口挤掉"
    assert len(s.urls("p0/34000.m4s")) == 1
    assert len(s.urls("p0/36000.m4s")) == 1
    assert len(s.urls("p1/12000.m4s")) == 1
    assert len(s.urls("p1/14000.m4s")) == 1
    # 没有任何一片被重复请求: p0 11+3, p1 6+2
    assert len([u for u, _h in s.calls if "/p0/" in u]) == 14
    assert len([u for u, _h in s.calls if "/p1/" in u]) == 8
    # 拼接顺序 = 发现顺序: 第二轮先 p0 的两片, 再 p1 的两片
    # (体是 URL 的最后一段 —— 见 `_plain_body`)
    assert body.endswith(b"32000.m4s34000.m4s36000.m4s12000.m4s14000.m4s")


def test_live_recording_is_reachable_from_the_normal_download_entry(
        monkeypatch, tmp_path):
    """`download()` 认得出 .mpd 并走直播那条路 —— 不然前面测的都是内部函数。"""
    murl = "https://live/m.mpd"
    body = _timeline_mpd([("0", "2000", "0")], kind="static")
    s = _Session({murl: [_timeline_mpd([("0", "2000", "0")]), body]},
                 fallback=_plain_body)
    d = _downloader(monkeypatch, s, _Clock(start=_AST_EPOCH + 30, step=0.001))
    logs = []
    out = d.download(murl, save_dir=str(tmp_path / "o"), log=logs.append)
    path = Path(out[0]) if isinstance(out, tuple) else Path(out)
    assert path.name == "m.mp4"
    assert path.read_bytes() == b"init.mp4" + b"0.m4s"


# ==================================================================
# 7. 单片下载: "怎么安全地取一个分片"只有一份实现
# ==================================================================

def test_fetch_one_segment_closes_the_response_even_when_it_fails(monkeypatch,
                                                                 tmp_path):
    """失败路径**必须**关响应。

    没读完的响应不会被自动归还连接, 而会话是 `pool_block=True` 的 —— 泄漏够多之后
    后面的分片会永久阻塞在 `_get_conn`, 症状是"任务卡死"而不是报错。
    """
    url = "https://live/v/0.m4s"
    resp = _Resp(b"", 404)
    s = _Session({url: resp})
    d = _downloader(monkeypatch, s, _Clock())
    part = tmp_path / "000000.part"
    with pytest.raises(RuntimeError):
        d._fetch_one_segment(part, url, {}, V.DomainLimiter(1, 0, 0), None)
    assert resp.closed is True
    assert not part.exists()
    assert not list(tmp_path.glob("*.tmp")), "临时文件必须清掉"


def test_fetch_one_segment_writes_atomically(monkeypatch, tmp_path):
    url = "https://live/v/0.m4s"
    s = _Session({url: b"payload"})
    d = _downloader(monkeypatch, s, _Clock())
    part = tmp_path / "000000.part"
    d._fetch_one_segment(part, url, {}, V.DomainLimiter(1, 0, 0), None)
    assert part.read_bytes() == b"payload"
    assert not list(tmp_path.glob("*.tmp"))


def test_fetch_segments_still_reports_which_one_failed(monkeypatch, tmp_path):
    """点播那条路改走 `_fetch_one_segment` 之后, 报错文案不许退化。

    "第几片失败"是用户唯一能拿去做诊断的信息。
    """
    url = "https://h/1.ts"
    segs = ["https://h/0.ts", url, "https://h/2.ts"]
    s = _Session({"https://h/0.ts": b"a", "https://h/2.ts": b"c"})
    d = _downloader(monkeypatch, s, _Clock())
    limiter = V.DomainLimiter(2, 0, 0)
    with pytest.raises(RuntimeError) as ei:
        d._fetch_segments(segs, {}, tmp_path / "p", limiter, None, None)
    assert "分片 2/3" in str(ei.value)


# ==================================================================
# 8. 收尾: 点播与直播共用同一份"三选一"
# ==================================================================

def _finalize(d, tmp_path, parts, stem="s", **kw):
    out = tmp_path / "o"
    out.mkdir(exist_ok=True)
    return d._finalize_tracks(parts, out, stem, kw.pop("ff", None), None, None,
                              **kw)


def test_finalize_tracks_audio_only_lands_as_m4a(monkeypatch, tmp_path):
    """纯音频落 `.m4a`: 拿 `.mp4` 出去, 播放器会按"没有视频轨的 mp4"处理。"""
    d = _downloader(monkeypatch, _Session(), _Clock())
    a = tmp_path / "audio"; a.write_bytes(b"a")
    final, ctype = _finalize(d, tmp_path, {"audio": a})
    assert final.name == "s.m4a" and final.read_bytes() == b"a"
    assert ctype == "audio/mp4"


def test_finalize_tracks_video_only_moves_without_ffmpeg(monkeypatch, tmp_path):
    d = _downloader(monkeypatch, _Session(), _Clock())
    v = tmp_path / "video.mp4"; v.write_bytes(b"v")
    final, ctype = _finalize(d, tmp_path, {"video": v})
    assert final.read_bytes() == b"v" and ctype == "video/mp4"


def test_finalize_tracks_muxes_both_tracks(monkeypatch, tmp_path):
    d = _downloader(monkeypatch, _Session(), _Clock())
    calls = []

    def fake_mux(v, a, dst, ff, progress_cb=None):
        calls.append((v.read_bytes(), a.read_bytes()))
        Path(dst).write_bytes(b"muxed")

    monkeypatch.setattr(d, "_ffmpeg_mux", fake_mux)
    v = tmp_path / "video.mp4"; v.write_bytes(b"v")
    a = tmp_path / "audio.mp4"; a.write_bytes(b"a")
    final, ctype = _finalize(d, tmp_path, {"video": v, "audio": a}, ff="ff")
    assert calls == [(b"v", b"a")]
    assert final.read_bytes() == b"muxed" and ctype == "video/mp4"


def test_finalize_tracks_remuxes_ts_or_multi_period(monkeypatch, tmp_path):
    """TS 分片 / 多时段的视频轨要靠 ffmpeg 重封装 —— 直接 `os.replace` 会留下
    一个后缀对不上内容的文件。"""
    d = _downloader(monkeypatch, _Session(), _Clock())
    seen = []

    def fake_remux(src, dst, ff, progress_cb=None):
        seen.append(str(src))
        Path(dst).write_bytes(b"remuxed")

    monkeypatch.setattr(d, "_ffmpeg_remux", fake_remux)
    v = tmp_path / "video.ts"; v.write_bytes(b"v")
    final, _ctype = _finalize(d, tmp_path, {"video": v}, ff="ff")
    assert seen and final.read_bytes() == b"remuxed"


# ==================================================================
# 9. 配置: 录多久
# ==================================================================

def test_live_max_seconds_default_is_five_minutes_and_reads_the_env(
        monkeypatch, tmp_path):
    """默认 300s 而不是"不限时": 一个会自动跑满磁盘的任务不该是默认行为。"""
    from core.config import load

    cfg_file = tmp_path / "c.yaml"
    cfg_file.write_text("", encoding="utf-8")
    monkeypatch.delenv("UWC_LIVE_MAX_SECONDS", raising=False)
    assert load(cfg_file).live_max_seconds == 300

    monkeypatch.setenv("UWC_LIVE_MAX_SECONDS", "45")
    assert load(cfg_file).live_max_seconds == 45

    monkeypatch.setenv("UWC_LIVE_MAX_SECONDS", "0")
    assert load(cfg_file).live_max_seconds == 0            # 0 = 不限时

    monkeypatch.setenv("UWC_LIVE_MAX_SECONDS", "-10")
    assert load(cfg_file).live_max_seconds == 0            # 负数夹到 0

    monkeypatch.setenv("UWC_LIVE_MAX_SECONDS", "abc")
    assert load(cfg_file).live_max_seconds == 300          # 写错不该让服务起不来


def test_live_clock_is_read_once_per_manifest(monkeypatch, tmp_path):
    """`now` 必须来自"取清单的那一刻"。

    重放一份几小时前抓到的清单时, 用 `publishTime` 会算出早已滚走的窗口 ——
    所以下载层显式传 `time.time()`, 这条钉住它别被"省一次调用"改掉。
    """
    murl = "https://live/m.mpd"
    clock = _Clock(start=_AST_EPOCH + 30, step=0.001)
    d = _downloader(monkeypatch, _Session({murl: _duration_mpd()}), clock)
    _leaf, spec = d._fetch_mpd(murl, {}, None)
    # 窗口末尾 = clock - AST = 30s(而不是 publishTime 推出来的别的值)
    assert _times(spec["video"])[-1] == 30000

    clock.t = _AST_EPOCH + 50
    _leaf, spec2 = d._fetch_mpd(murl, {}, None)
    assert _times(spec2["video"])[-1] == 50000, "时钟动了窗口就要跟着动"


def test_clock_does_not_sleep_the_poll_interval():
    """夹具自检: 假时钟的 `sleep` 不真等 —— 否则上面的用例会各花好几秒。"""
    c = _Clock()
    t0 = time.monotonic()
    c.sleep(999)
    assert time.monotonic() - t0 < 0.5
