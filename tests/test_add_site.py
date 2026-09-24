"""`scripts/add_site.py` 加站门禁的自检。

为什么这些用例是**必要的**(而不是"顺手补的覆盖")
==================================================
门禁脚本本身的失败方式是"**它什么也没查, 却印着一行 ok**" —— 与第 18 条坑同源:
该断言的地方没断言。所以每一条闸都必须有一个**故意造坏**的输入, 证明它会报错:
闸只要全是"通过"的用例, 就没法区分"查了没问题"和"根本没查"。

本文件同时钉住**两类"绿/红是假的"的病**, 它们是同一个根因的两面 ——
**判据挂在了错误的东西上**:

    假绿: 判据挂在"没报错"上   -> 空转(Gate.checked == 0)也算过
    假红: 判据挂在"中文文案"上 -> 文案一改, 正确实现被判红

两类都在本项目真实发生过, 所以各有一个用例段落专门守着:
`test_checked_is_the_only_thing_that_stops_a_vacuous_green` 与
`test_verdict_rides_on_kind_not_on_wording`。

另一个专门用例是 `pexels`: 它有 `site` 声明却不是 `SequenceGallerySpider`, 于是
四道闸会在一份**不存在的 URL** 图案上"全绿"。这正是本项目反复踩的家族 F(通过但
理由已经不对), 所以要用一条用例把它钉死 —— 宁可不验, 不给假绿。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "backend"))

import add_site  # noqa: E402
from collectors.gallery_base import GallerySite  # noqa: E402
from gate import NOTHING_CHECKED, Gate, Problem  # noqa: E402

GID = "6aa5136f606fe"


def _site(**over):
    """一个最小的、能通过全部闸的声明; 用例只改坏其中一个字段。"""
    kw = dict(
        name="demo",
        base="https://cdn.example.com/photos",
        variants=[".jpg"],
        seq_format="{seq:05d}",
        gid_shape=r"[0-9a-f]{8,}",
        id_patterns=[r"/photos/([0-9a-f]{8,})/"],
        id_samples=[("https://cdn.example.com/photos/%s/00001.jpg" % GID, GID)],
    )
    kw.update(over)
    return GallerySite(**kw)


def _real_seq_site():
    """一个**真实注册**的序号枚举型站点 (没有就返回 None, 别把用例变成环境依赖)。"""
    seq_sites, _others = add_site.registered_sites()
    return seq_sites.get("xchina_gallery")


# ==========================================================================
# 判据必须挂在**代号**上, 不能挂在文案上 —— 治「假红」
# ==========================================================================


def test_verdict_rides_on_kind_not_on_wording():
    """同一条问题, 文案反复改写 -> 判据(kinds / ok)必须**一模一样**。

    真实事故: 一条断言写 `assert "chrome" not in msg`, 而 `ip_block` 的文案里提到
    Chrome 恰恰是在说"**换指纹没用**"(IP 封禁只能换出口)。于是一条**正确**的实现
    被判红 —— 而且它看起来像"产品文案有病", 实际是判据挂错了地方。

    文案是给人看的: 会改措辞、会有反讽、会被翻译、会带一堆排查建议。判据只能是代号。
    (同族: `core/errors.py` 用 `error_kind` 归类, 而不是 match 中文标签。)
    """
    wordings = [
        "防盗链: 请带上 Referer 再试",
        "换 chrome 指纹也没用, 只能换出口 IP",
        "401 -> 需要登录; --impersonate chrome 不是这里的解法",
    ]
    verdicts = set()
    for w in wordings:
        g = Gate("t", checked=1, problems=[Problem("ip-block", w)])
        verdicts.add((g.ok, tuple(sorted(g.kinds()))))
    assert len(verdicts) == 1, "文案一改判据就变 = 判据挂在文案上, 迟早假红: %r" % (verdicts,)


def test_a_wording_only_change_does_not_flip_a_gate_to_green():
    """反方向也要管: 不能靠"文案里出现了'通过'两个字"把红变成绿。"""
    g = Gate("t", checked=1,
                      problems=[Problem("filtered-out", "已通过常规检查, 但…")])
    assert not g.ok
    assert g.kinds() == {"filtered-out"}


# ==========================================================================
# 空转不算绿 —— 治「假绿」(本文件真实存在过的那一种)
# ==========================================================================


def test_checked_is_the_only_thing_that_stops_a_vacuous_green():
    """`checked == 0` 且没有问题 -> 必须**报红**, 且报的是 `NOTHING_CHECKED`。

    这是本文件真实存在过的**活的假绿**: `check_site()` 在 `id_samples` 为空时返回
    空列表(它的每条检查都被 `if ... and samples:` 挡掉了), 于是闸 1 会印一行 ok,
    而它一项都没核 —— 一个还没写样本的新站点直接放行。
    """
    g = Gate("闸 X", checked=0, problems=[])
    assert not g.ok, "空转被算成绿 —— 这就是那条假绿"
    assert NOTHING_CHECKED in g.kinds()


def test_a_real_problem_is_not_masked_by_the_nothing_checked_marker():
    """"空转"只是**兜底**判据: 已经报了具体问题时, 必须报具体问题。

    否则排查时看到的是"一项都没核到", 而真正的原因(比如 seq_format 渲染失败)被吞掉,
    等于用一个笼统的红替掉一条能直接照做的问题。
    """
    g = Gate("闸 X", checked=0,
                      problems=[Problem("seq-format-unrenderable", "渲染失败")])
    assert g.kinds() == {"seq-format-unrenderable"}


def test_every_gate_reports_how_many_things_it_actually_checked():
    """`checked` 是必填的**实际数目** —— 新写闸时忘了填, 那闸就是永远绿的。

    顺带钉住"闸返回 `Gate` 而不是裸 list / tuple": 早先的版本返回
    `(problems, rows)` 元组, 调用方各自拆解, 于是"空转"这件事根本没有字段可表达。
    """
    gates = [
        add_site.gate_declaration(_site()),
        add_site.gate_claim(_site(), "demo"),
        add_site.gate_filter(_site()),
        add_site.gate_layout(_site()),
    ]
    for g in gates:
        assert isinstance(g, Gate), g
        # ⚠️ 这一条是**防复发**的: `Gate` 一旦在 `add_site.py` 里被重新抄一份,
        #    两边就会各自演化 —— 到时候"脚本认 `checked`、gateguard 不认"这种
        #    不一致不会报错, 只会让某道闸悄悄变成永远绿。
        assert add_site.Gate is Gate, "Gate 必须只有一份定义(scripts/gate.py)"
        assert isinstance(g.checked, int), g
        assert g.checked > 0, "有样本却报 0 项已核 = 这闸没在数"


# ==========================================================================
# 闸 1 声明自洽
# ==========================================================================


def test_gate1_passes_on_a_self_consistent_declaration():
    g = add_site.gate_declaration(_site())
    assert g.ok, g.problems
    assert g.checked == 1


def test_gate1_catches_a_sample_that_parses_to_a_different_gid():
    """样本期望值与正则解析结果不一致 —— 这是"站点换 ID 形态"的第一现场。"""
    s = _site(id_samples=[("https://cdn.example.com/photos/ffffffffffff/00001.jpg", GID)])
    g = add_site.gate_declaration(s)
    assert not g.ok, "样本与正则矛盾却报 ok = 这一闸没在查"
    assert "declaration" in g.kinds()


def test_gate1_refuses_a_declaration_with_no_samples_at_all():
    """**零样本必须算红** —— 这是那条活的假绿, 单独立一条守着。

    没写样本的新站点, `check_site()` 会一路"通过"; 若闸 1 只看"有没有问题",
    它就会放行一个从未被验证过的声明。
    """
    g = add_site.gate_declaration(_site(id_samples=[]))
    assert not g.ok
    assert g.kinds() == {NOTHING_CHECKED}


# ==========================================================================
# 闸 2 认领
# ==========================================================================


def test_gate2_catches_a_url_no_specialised_collector_claims():
    """没人认领 = 会静默落到通用采集器, 行为完全不同。

    用 `.invalid` 顶级域造一个**任何采集器都认不了**的 URL。
    """
    s = _site(id_samples=[("https://cdn.example.invalid/photos/%s/00001.jpg" % GID, GID)])
    g = add_site.gate_claim(s, "demo")
    assert not g.ok
    assert "unclaimed" in g.kinds()


def test_gate2_catches_a_url_stolen_by_another_site():
    """样本 URL 其实是**别人**的 —— 传错名字 / 正则写太宽都会这样。"""
    real = _real_seq_site()
    if real is None:
        return                                     # 站点没注册就没什么可验的
    g = add_site.gate_claim(real, "some_other_site")   # 故意报错的名字
    assert not g.ok
    assert "stolen" in g.kinds()


def test_gate2_passes_on_a_real_registered_site():
    """正面用例: 真站点的真样本必须被它自己接走(否则前面那条"抢先认领"是空断言)。"""
    real = _real_seq_site()
    if real is None:
        return
    g = add_site.gate_claim(real, "xchina_gallery")
    assert g.ok, g.problems
    assert g.checked == len(add_site._url_samples(real))
    assert g.checked > 0, "真站点却一条 URL 样本都没有 —— 那这条正面用例什么也没验"


def test_gate2_says_so_when_there_is_nothing_to_verify():
    """没有 URL 样本时**不能说 ok** —— "没验到"与"验过了没问题"是两件事。"""
    g = add_site.gate_claim(_site(id_samples=[(GID, GID)]), "demo")   # 只有纯 ID 形态
    assert not g.ok
    assert g.kinds() == {NOTHING_CHECKED}


# ==========================================================================
# 闸 3 过滤
# ==========================================================================


def test_gate3_passes_on_an_ordinary_cdn_path():
    g = add_site.gate_filter(_site())
    assert g.ok, g.problems
    assert g.checked == 1


def test_gate3_catches_a_path_segment_that_looks_like_an_ad():
    """资源路径里有个 `banner` 段 -> 默认过滤规则会**全部**拦掉 -> 任务 success + 0 资源。

    这是"静默少采"里最难查的一种: 探针、声明自洽、认领三道闸全绿, 唯独资源一个都
    下不来。所以要专门造一个。判据用的是 filters 里的 `_AD_SEGMENTS`(路径分段精确
    匹配), 不是子串 —— 造 `banner` 整段, 而不是 `my-banner-album`。
    """
    g = add_site.gate_filter(_site(base="https://cdn.example.com/banner"))
    assert not g.ok, "广告段被静默过滤却报 ok = 这一闸没在查"
    assert "filtered-out" in g.kinds()


def test_gate3_says_so_when_no_url_can_be_built():
    g = add_site.gate_filter(
        _site(id_samples=[("https://cdn.example.com/photos/%s/00001.jpg" % GID, "")]))
    assert not g.ok
    assert g.rows == []
    assert g.kinds() == {NOTHING_CHECKED}


# ==========================================================================
# 闸 4 落盘/命名
# ==========================================================================


def test_gate4_keeps_the_album_folder_and_flattens_video():
    g = add_site.gate_layout(_site(video_variants=[".mp4"]))
    assert g.ok, g.problems
    paths = dict(g.rows)
    assert paths["image"] == "%s/00001.jpg" % add_site._ALBUM     # 图片进相册文件夹
    assert "/" not in paths["video"]                              # 视频平铺
    assert paths["claim"] != "%s/00001.jpg" % add_site._ALBUM     # 撞名被消解
    assert g.checked == 2                                        # 图片 + 视频


def test_gate4_catches_a_sequence_format_that_cannot_render():
    """`seq_format` 写错的表现是"每张都判 MISSING -> 0 资源 -> failed", 用户只看到失败。"""
    g = add_site.gate_layout(_site(seq_format="{seq:05d}{oops}"))
    assert not g.ok
    assert "seq-format-unrenderable" in g.kinds()


def test_gate4_reports_a_broken_media_declaration_instead_of_crashing():
    """`variants=[]` 让 `GallerySite.media()` 抛 `IndexError` —— 门禁必须**报出来**。

    这条用例原本是想验"没媒体可排时说没验到", 写完发现闸是**崩**的。崩比报错更糟:
    脚本跑不完, 前面几道闸已经核出来的结论一起丢掉, 用户看到的是一个 traceback,
    而真正的原因(声明里 `variants` 是空的)一个字也没提。
    """
    g = add_site.gate_layout(_site(variants=[]))
    assert not g.ok
    assert g.kinds() == {"media-declaration-broken"}
    assert "IndexError" in str(g.problems[0]), "报错要说清是什么抛的"


def test_a_broken_media_declaration_is_reported_by_every_gate_that_needs_media():
    """同一份坏声明, 每道要用到媒体的闸都要报 —— 不许"闸 A 报、闸 B 崩"。"""
    broken = _site(variants=[])
    for gate in (add_site.gate_filter(broken), add_site.gate_layout(broken),
                 add_site.gate_smoke(broken)):
        assert not gate.ok, gate
        assert "media-declaration-broken" in gate.kinds(), gate


def test_verify_survives_a_broken_media_declaration():
    """端到端: 一份坏声明也要**跑完全部闸并给出退出码**, 而不是半路 traceback。"""
    ok, total = add_site.verify("demo", _site(variants=[]))
    assert not ok
    assert total >= 1


# ==========================================================================
# 端到端: 门禁自己的退出语义
# ==========================================================================


def test_verify_is_not_green_when_a_gate_verified_nothing():
    """把"空转"接到最终判据上 —— 否则前面拦了、`verify()` 又放过去, 就是半拉子护栏。"""
    ok, total = add_site.verify("demo", _site(id_samples=[]))
    assert not ok
    assert total >= 1


def test_verify_is_green_on_a_well_formed_declaration():
    """正面用例(离线四闸): 免得上面那条"红"只是因为 `verify()` 恒红。"""
    real = _real_seq_site()
    if real is None:
        return
    ok, total = add_site.verify("xchina_gallery", real)
    assert ok, "真站点没跑过门禁, 这四道闸就是摆设"


# ==========================================================================
# 站点选择器
# ==========================================================================


def test_selector_excludes_sites_that_are_not_sequence_based():
    """`pexels` 有 `site` 声明但**不是** `SequenceGallerySpider` —— 必须被排除。

    否则四道闸会在 `url_for` 拼出的一个**根本不存在**的 URL 图案上"全绿"。这是
    家族 F(通过但理由已经不对): 比失败更危险, 因为它给的是**假的信心**。
    """
    seq_sites, others = add_site.registered_sites()
    assert "pexels" not in seq_sites, "非序号枚举型站点混进了本门禁"
    assert "pexels" in others or not others       # 允许注册表变化, 但不许错判
    for name, site in seq_sites.items():
        assert getattr(site, "variants", None) is not None, name
