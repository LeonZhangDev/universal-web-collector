"""站点声明自检 + CDN 画像 + 手选采集器的形状软提示。

这三件事凑在一个文件里, 因为它们服务同一个目的: **把"静默出错"变成"当场看得见"**。

- 声明自检(`check_site`): 新加一条 ID 正则时若不慎也能匹配旧 URL, `parse_gid`
  取的是首个命中 —— 行为就变了, 而日志里一切正常。样本断言把它变成测试红灯。
- CDN 画像(`cdn_profile`): 候选探测顺序固定的话, 站点把资源搬去 photos2 之后
  每个相册都要先白试一次; 更要紧的是画像能回答"是不是站点在迁移 CDN"。
- 形状软提示(`shape_warning`): 手选采集器时我们**没有资格**拒绝用户, 但让他
  知道"这个 ID 形状本站从没见过"是有价值的 —— 手滑粘错时当场就能改。
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from collectors import gallery_base as G  # noqa: E402
from collectors.xchina.gallery import XCHINA  # noqa: E402
from core import cdn_profile as cp  # noqa: E402

GID = "69ad45698f836"


# ---- 1. 声明自检 ----

def test_all_registered_sites_pass_selfcheck():
    """每个已注册的图集站点都必须通过自己的声明自检。

    这是"新增/修改正则"这件事唯一的护栏: 断言的是**样本与正则自洽**,
    而样本是人在写正则时顺手留下的真实 URL。
    """
    problems = G.selfcheck_all()
    assert problems == {}, f"站点声明自检未通过: {problems}"


def test_xchina_samples_cover_every_input_form():
    """样本要盖住所有输入形态 —— 少一种, 那种形态回归时就没有护栏。"""
    assert len(XCHINA.id_samples) >= 6
    joined = " ".join(u for u, _ in XCHINA.id_samples)
    for token in ("photos2/", ".mp4", "_1200x0.webp", "/photo/id-", "photoShow"):
        assert token in joined, f"样本缺少 {token} 这条形态"


def test_check_site_flags_pattern_conflict():
    """一条 pattern 单独就能配出不同结果 = 存在"谁先谁赢"的隐式依赖。

    顺序一变行为就变, 而 `parse_gid` 的顺序是"人写代码的顺序" —— 这种 bug
    不会自己暴露, 只会等某个相册突然采空。
    """
    site = G.GallerySite(
        name="t", base="https://c/photos", variants=[".jpg"],
        # 第二条把 "id-" 也吃进去了: 单独匹配会得到 "id-abcdef12"
        id_patterns=[r"/photo/id-([0-9a-z]{8,})", r"/photo/(id-[0-9a-z]{8,})"],
        id_samples=[("https://c/photo/id-abcdef12.html", "abcdef12")],
    )
    problems = G.check_site(site)
    assert any("冲突" in p for p in problems), problems


def test_check_site_flags_over_narrow_shape():
    """声明了 gid_shape 却没有任何样本能通过 = 形状写窄了。

    后果是自动识别把**真图集**也挡在外面, 用户看到"无法识别, 请手动选择采集器"
    —— 明明 URL 一点问题都没有。
    """
    site = G.GallerySite(
        name="t", base="https://c/photos", variants=[".jpg"],
        gid_shape=r"[0-9]{20}",           # 样本是 8 位十六进制, 过不了
        id_samples=[("https://c/photo/id-abcdef12.html", "abcdef12")],
    )
    assert any("gid_shape" in p for p in G.check_site(site))


def test_check_site_flags_malformed_sample():
    site = G.GallerySite(name="t", base="https://c", variants=[".jpg"],
                         id_samples=["not-a-pair"])
    assert G.check_site(site), "样本格式写错必须被指出来, 不能静默跳过"


def test_assert_site_raises_with_readable_message():
    site = G.GallerySite(
        name="broken", base="https://c/p", variants=[".jpg"],
        gid_shape=r"[0-9]{20}",
        id_samples=[("https://c/photo/id-abcdef12.html", "abcdef12")],
    )
    with pytest.raises(ValueError) as ei:
        G.assert_site(site)
    assert "broken" in str(ei.value), "报错要说清是哪个站点"


# ---- 2. page_tail 列表化 与 URL 解码 ----

def test_page_tail_accepts_a_list():
    """分页形态不止一种时(10.html / page-10.html)只加不改。

    ⚠️ 站点得先有一条能认出相册 ID 的 pattern。只声明 page_tail 的话, 末段
    仍然会走"退路"逻辑 —— 那条退路本来就是最后手段, 不是主路径。
    """
    site = G.GallerySite(
        name="t", base="https://c/photos", variants=[".jpg"],
        id_patterns=[r"/photo/id-([0-9a-z]{8,})"],
        page_tail=[r"^\d+$", r"^page-\d+$"],
    )
    assert G.parse_gid(site, "https://c/photo/id-abcdef12/10.html") == "abcdef12"
    assert G.parse_gid(site, "https://c/photo/id-abcdef12/page-3.html") == "abcdef12"
    # 两条都命中才叫"列表生效": 只剩一条能过的话, 列表化等于没做
    assert G.parse_gid(site, "https://c/photo/id-abcdef12/10.html", strict=True) \
        == "abcdef12"


def test_single_string_page_tail_still_works():
    """旧写法(单条正则)不能被列表化破坏 —— 站点声明是既有配置。"""
    site = G.GallerySite(name="t", base="https://c/photos", variants=[".jpg"],
                         page_tail=r"^\d+$")
    assert G.parse_gid(site, "https://c/thing/10.html") is None


def test_parse_gid_decodes_percent_escapes():
    """URL 里 %2D 之类很常见(浏览器/复制粘贴都会产生), 不解码就解析不出 ID。"""
    assert G.parse_gid(XCHINA, "https%3A%2F%2Fxchina.co%2Fphoto%2Fid-6aa5136f606fe.html") \
        == "6aa5136f606fe"
    assert G.parse_gid(XCHINA, "https://xchina.co/photo/id-6aa5136f606fe%2Ehtml") \
        == "6aa5136f606fe"


def test_percent_sign_in_plain_text_does_not_crash():
    """普通文本里的 % 不该让 unquote 抛异常(它只是可能解不出 ID)。"""
    assert G.parse_gid(XCHINA, "100%-discount") is None


# ---- 3. 序号宽度与基址候选 ----

def test_seq_width_variants_span_two_each_way():
    """默认 ±2 —— ±1 会漏掉"站点写 5 位而实际相册 3 位"这种差 2 的情形。"""
    out = G._seq_format_variants("{seq:05d}")
    assert out[0] == "{seq:05d}", "站点默认必须排第一(它命中的概率最高)"
    assert "{seq:03d}" in out and "{seq:07d}" in out
    assert "{seq:01d}" not in out, "宽度下界是 1 位, 不该生成 0 位"


def test_seq_width_variants_never_generate_zero_width():
    """宽度下界是 1 位。

    生成 `{seq:00d}` 会让 `url_for` 抛 ValueError, 整个任务从一个"探测候选"
    变成异常 —— 一条只为"多试一次"的辅助逻辑不该有能力炸掉采集。
    """
    for f in ("{seq:01d}", "{seq:02d}", "{seq:05d}"):
        out = G._seq_format_variants(f)
        widths = [int(re.search(r"seq:0(\d+)d", x).group(1)) for x in out]
        assert min(widths) >= 1, f"{f} 生成了非法宽度: {out}"
        assert widths[0] == int(re.search(r"seq:0(\d+)d", f).group(1))


def test_seq_format_without_padding_is_returned_as_is():
    assert G._seq_format_variants("{seq}") == ["{seq}"]
    assert G._seq_format_variants(None) == [None]


def test_base_candidates_includes_host_templates():
    """换 host 的迁移是"数字后缀"表达不了的, 只能显式声明。"""
    site = G.GallerySite(
        name="t", base="https://img.a.io/photos", variants=[".jpg"],
        base_host_templates=["https://img2.a.io/photos"],
    )
    out = G._base_candidates(site)
    assert out[0] == "https://img.a.io/photos"
    assert "https://img2.a.io/photos" in out


def test_base_candidates_never_generates_photos1():
    """`photos1` 不是真实存在的形态 —— 生成它只是白白多探一次。"""
    out = G._base_candidates(XCHINA)
    assert "https://img.xchina.io/photos1" not in out
    assert "https://img.xchina.io/photos2" in out


# ---- 4. CDN 画像 ----

def test_profile_records_and_prefers_last_hit():
    cp.reset()
    cp.record_hit("s", "https://c/photos")
    cp.record_hit("s", "https://c/photos2")
    cp.record_hit("s", "https://c/photos2")
    assert cp.preferred_bases("s")[0] == "https://c/photos2", "最近命中的要排最前"
    assert set(cp.preferred_bases("s")) == {"https://c/photos", "https://c/photos2"}


def test_profile_is_per_site():
    cp.reset()
    cp.record_hit("a", "https://c/a")
    cp.record_hit("b", "https://c/b")
    assert cp.preferred_bases("a") == ["https://c/a"]
    assert cp.preferred_bases("b") == ["https://c/b"]
    assert cp.preferred_bases("never-seen") == []


def test_profile_seq_format_distribution():
    cp.reset()
    for _ in range(3):
        cp.record_hit("s", "https://c/p", "{seq:04d}")
    cp.record_hit("s", "https://c/p", "{seq:05d}")
    assert cp.preferred_seq_format("s") == "{seq:04d}"


def test_profile_survives_corrupt_file():
    """画像文件被写坏(断电/手工编辑)时不能把采集拖下水。"""
    p = cp._path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ this is not json", encoding="utf-8")
    assert cp.preferred_bases("s") == []
    assert cp.summarize() == {}
    # 写一次应当能自愈
    cp.record_hit("s", "https://c/p")
    assert cp.preferred_bases("s") == ["https://c/p"]


def test_profile_can_be_disabled_by_env(monkeypatch):
    monkeypatch.setenv("UWC_CDN_PROFILE", "off")
    assert cp._path() is None
    cp.record_hit("s", "https://c/p")     # 不该抛, 也不该落盘
    assert cp.preferred_bases("s") == []


def test_profile_summary_reports_share_and_last():
    cp.reset()
    cp.record_hit("s", "https://c/p")
    for _ in range(3):
        cp.record_hit("s", "https://c/p2")
    got = cp.summarize("s")["s"]
    assert got["total_hits"] == 4
    assert got["last"] == "https://c/p2"
    assert got["bases"][0]["base"] == "https://c/p2"
    assert got["bases"][0]["share"] == 0.75


def test_profile_never_raises_on_bad_input():
    cp.reset()
    cp.record_hit(None, "https://c/p")
    cp.record_hit("s", None)
    cp.record_hit("s", "")
    assert cp.summarize() == {}


def test_resolve_base_records_the_hit():
    """探测命中要记进画像 —— 这是新子路径**唯一**能被学到的时机。

    只记"线索命中"的话, 画像永远只覆盖用户手动粘过直链的相册, 对自动探索
    一点帮助都没有。
    """

    class Routes:
        def __init__(self, ok):
            self.ok = ok

        def head(self, url, **kw):
            ok = url.startswith(self.ok)
            return type("R", (), {"status_code": 200, "headers": {
                "Content-Type": "image/jpeg" if ok else "text/html",
            }})()

        def close(self):
            pass

    cp.reset()
    sess = Routes("https://img.xchina.io/photos2/")
    base, fmt = G._resolve_base(XCHINA, GID, sess, "image")
    assert base.endswith("photos2")
    assert cp.preferred_bases(XCHINA.name)[0] == base


def test_resolve_base_does_not_record_a_fallback():
    """退回默认值**不算命中** —— 记进去会让画像被"没探到"的结果污染。

    这是最容易被写错的一处: 把函数末尾那个 `return default, default_fmt`
    也改成 `_hit(...)`, 看着"更完整", 实际是在教画像学错误的东西, 下次
    反而更慢找到真基址。
    """

    class Routes:
        def head(self, url, **kw):
            return type("R", (), {"status_code": 200,
                                  "headers": {"Content-Type": "text/html"}})()

        def close(self):
            pass

    cp.reset()
    G._resolve_base(XCHINA, GID, Routes(), "image")
    assert cp.preferred_bases(XCHINA.name) == []


def test_profiled_base_is_tried_first():
    """画像排序要真的影响探测顺序, 而不只是存在文件里好看。"""
    cp.reset()
    cp.record_hit(XCHINA.name, "https://img.xchina.io/photos3")
    out = G._base_candidates(XCHINA)
    assert out[0] == "https://img.xchina.io/photos3"
    assert len(out) == len(set(out)), "重排不能弄出重复项"


def test_profile_reordering_loses_nothing():
    """重排只改顺序, 不增不减 —— 画像出错也不该让相册变成"探不到"。"""
    cp.reset()
    before = set(G._base_candidates(XCHINA))
    cp.record_hit(XCHINA.name, "https://img.xchina.io/photos4")
    after = G._base_candidates(XCHINA)
    assert set(after) == before


def test_profiled_seq_format_is_tried_first():
    """实测价值: 把上次命中的**宽度**提到首位, 6 次探测 -> 1 次。

    循环是"格式外层、基址内层", 所以宽度不对时会把每个候选基址都白试一遍才
    轮到正确宽度(photos/photos2..5 × 05d 全部未命中, 才试到 photos2+04d)。
    这是画像里最省事的一笔收益: 数据早就存了(`seq_formats`), 只是没人消费。
    """

    class Routes:
        def __init__(self, ok):
            self.ok = ok
            self.calls = []

        def head(self, url, **kw):
            self.calls.append(url)
            ok = url.startswith(self.ok) and url.rsplit("/", 1)[-1].startswith("0001.")
            return type("R", (), {"status_code": 200, "headers": {
                "Content-Type": "image/jpeg" if ok else "text/html",
            }})()

        def close(self):
            pass

    cp.reset()
    sess = Routes("https://img.xchina.io/photos2/")
    # 第一次: 没有画像, 只能靠逐个组合试
    base, fmt = G._resolve_base(XCHINA, GID, sess, "image")
    assert (base.endswith("photos2"), fmt) == (True, "{seq:04d}")
    cold = len(sess.calls)
    assert cold > 1, "冷启动本来就该多试几次, 否则这个用例证明不了什么"

    # 第二次: 画像里已经有 photos2 + 04d, 应当一击命中
    sess2 = Routes("https://img.xchina.io/photos2/")
    base2, fmt2 = G._resolve_base(XCHINA, GID, sess2, "image")
    assert base2 == base and fmt2 == fmt
    assert len(sess2.calls) == 1, f"有画像时应当 1 次命中, 实际 {len(sess2.calls)}: {sess2.calls}"


def test_profiled_format_does_not_hide_other_widths():
    """画像只是排序: 换一个宽度不同的相册, 仍然要能探到。

    这是"画像错了也不会让本来能采的相册采不到"的反向断言 —— 少了它, 未来
    有人把 `fmts` 直接替换成 `[fav_fmt]` 也不会有测试拦住。
    """

    class Routes:
        def __init__(self, ok, width):
            self.ok = ok
            self.width = width
            self.calls = []

        def head(self, url, **kw):
            self.calls.append(url)
            tail = url.rsplit("/", 1)[-1].split(".")[0]
            ok = url.startswith(self.ok) and len(tail) == self.width
            return type("R", (), {"status_code": 200, "headers": {
                "Content-Type": "image/jpeg" if ok else "text/html",
            }})()

        def close(self):
            pass

    cp.reset()
    cp.record_hit(XCHINA.name, "https://img.xchina.io/photos", "{seq:04d}")
    # 这个相册实际是 5 位 —— 画像说 4 位, 但它只排在前面, 不该垄断
    sess = Routes("https://img.xchina.io/photos/", width=5)
    base, fmt = G._resolve_base(XCHINA, GID, sess, "image")
    assert fmt == "{seq:05d}", "画像指错宽度时仍要能回到正确宽度"


# ---- 5. 手选采集器的形状软提示 ----

def test_shape_warning_is_silent_for_real_ids():
    for u in [
        "https://img.xchina.io/photos2/69ad45698f836/0001.jpg",
        "https://xchina.co/photo/id-6aa5136f606fe.html",
        "6aa5136f606fe",
    ]:
        assert G.shape_warning(XCHINA, u) is None, u


def test_shape_warning_fires_on_path_word():
    """`featured` 含 t/u/r, 不是十六进制 —— 这正是自动识别要挡的那一类。

    手选时只提示不阻止: 站点可能刚换了 ID 格式, 我们比用户知道得晚。
    """
    msg = G.shape_warning(XCHINA, "https://img.xchina.io/photos/featured/0001.jpg")
    assert msg and "featured" in msg
    assert "继续" in msg, "提示语必须说清还能继续, 否则用户以为被拒了"
    assert "6aa5136f606fe" in msg, "给一个正确形态的例子比讲正则有用"


def test_shape_warning_silent_when_no_id_at_all():
    """解析不出 ID 时"没意见"就是正确的表态 —— 报错留给采集器去做。"""
    assert G.shape_warning(XCHINA, "not a url at all") is None
    assert G.shape_warning(XCHINA, "") is None


def test_shape_warning_silent_when_site_has_no_shape():
    site = G.GallerySite(name="t", base="https://c/p", variants=[".jpg"])
    assert G.shape_warning(site, "https://c/p/whatever/0001.jpg") is None


def test_manual_resolve_returns_warning_and_does_not_block():
    """手选 = 用户已表态。给提示, 但绝不 400; 且 `resolved` 仍是 None。

    `resolved` 专指**自动识别的结论**, 手选不该往里塞东西 —— 否则界面那句
    "已识别为 X" 就得靠 `auto` 二次判断, 语义越用越糊。所以提示走第三个返回值。
    """
    from api.tasks import _pick_collector

    name, resolved, warning = _pick_collector(
        "https://img.xchina.io/photos/featured/0001.jpg", "xchina_gallery")
    assert name == "xchina_gallery", "手选必须被尊重"
    assert resolved is None, "手选没有识别结论可回显"
    assert warning and "featured" in warning


def test_manual_resolve_is_silent_for_a_normal_url():
    from api.tasks import _pick_collector

    _, resolved, warning = _pick_collector(
        "https://xchina.co/photo/id-6aa5136f606fe.html", "xchina_gallery")
    assert resolved is None and warning is None


def test_auto_resolve_has_no_warning():
    """自动识别走的是严格路径: 形状不符直接**不认领**, 轮不到软提示。"""
    from api.tasks import _pick_collector

    name, resolved, warning = _pick_collector(
        "https://img.xchina.io/photos2/69ad45698f836/0001.jpg", "auto")
    assert name == "xchina_gallery"
    assert resolved["auto"] is True
    assert warning is None


def test_unknown_collector_still_400s():
    from fastapi import HTTPException

    from api.tasks import _pick_collector

    with pytest.raises(HTTPException) as ei:
        _pick_collector("https://x/y", "no_such_collector")
    assert ei.value.status_code == 400


# ---- 6. 过滤器参数不得被静默丢弃 ----

def test_filter_in_keeps_every_dimension_it_declares():
    """回归: `FilterIn` 曾经漏声明 min_width/min_height/exclude_ad/min_image_bytes,
    pydantic 默认忽略未知字段 -> 前端开关按了没反应, 无报错无日志。

    这条断言的价值不在于"字段有没有写全", 而在于: `Filters` 认识的每个键,
    经 `FilterIn` 往返后都必须还在。以后新增维度忘了声明, 这里立刻红。
    """
    from core.filters import Filters
    from models.schemas import FilterIn

    payload = {
        "types": ["image"], "exts": [".jpg"], "exclude_exts": [".gif"],
        "keywords": ["album"], "exclude_keywords": ["logo"],
        "min_size": "10KB", "max_size": "5MB", "min_image_bytes": "5KB",
        "exclude_ad": True, "min_width": "300", "min_height": "300",
        "min_pixels": "100000", "dedup_perceptual": False, "dedup_threshold": 3,
    }
    got = FilterIn(**payload).model_dump(exclude_none=True)
    assert set(got) == set(payload), f"被丢掉的键: {set(payload) - set(got)}"
    # 再确认这些键真的被 Filters 接住(而不是仅"没丢")
    f = Filters(got)
    assert f.min_width == 300 and f.min_height == 300
    assert f.exclude_ad is True
    assert f.min_image_bytes == 5120
    assert f.dedup_perceptual is False and f.dedup_threshold == 3


def test_unknown_filter_key_does_not_break_anything():
    """允许额外键是有意的: 新前端 + 老后端混跑时不该把参数吞掉。"""
    from core.filters import Filters
    from models.schemas import FilterIn

    got = FilterIn(types=["image"], some_future_option=1).model_dump(exclude_none=True)
    assert got["some_future_option"] == 1
    Filters(got)     # 不认识就忽略, 不该抛


def test_dedup_default_is_on_and_not_part_of_active():
    """感知去重默认开, 但它**不算过滤条件**。

    ⚠️ `active` 的判据是"用户配了任何一条过滤规则"。`exclude_ad` 默认就是开的,
    所以空配置下 `active` 本来就是 True —— 要验"去重没被算进去", 必须把
    exclude_ad 关掉再看(那才是真正的"什么都没配")。
    """
    from core.filters import Filters

    f = Filters({})
    assert f.dedup_perceptual is True
    assert Filters({"exclude_ad": False}).active is False, \
        "去重默认开不该让空配置变成「有过滤条件」"
    assert Filters({"exclude_ad": False, "min_width": 300}).active is True


def test_dedup_threshold_falls_back_on_garbage():
    from core.filters import Filters

    assert Filters({"dedup_threshold": "abc"}).dedup_threshold == 4
    assert Filters({"dedup_threshold": None}).dedup_threshold == 4
    assert Filters({"dedup_threshold": "8"}).dedup_threshold == 8
