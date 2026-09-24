"""`scripts/drift_check.py` 的自检。

两件事, 各治一种"绿/红是假的"的病
==================================
**比对分档**(`classify_drift`): 纯函数, 不联网 —— 但它错了的后果最贵:
漏报一条硬漂移 = **假绿**(站点改版了, 巡检说"无漂移"), 多报一条软漂移 = **噪音**
(灯永远在闪 = 没有灯)。所以两个方向各要有用例。

**空转不许算绿**(`main()` 的退出码): `checked == 0` 时必须**非 0 退出**。
"巡检跑了但一个站点都没查成"和"查过了没漂移"是两件事 —— 混起来就等于把
"巡检自己坏了"伪装成"站点没变", 而挂进定时任务的人只会一直收到"一切正常"。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "backend"))

import drift_check  # noqa: E402

HARD, SOFT = drift_check.HARD, drift_check.SOFT


def _snap(**over):
    """一份"看起来正常"的快照; 用例只改坏要比对的那一项。"""
    s = dict(reachable=True, reach_status=200, reach_ctype="image/jpeg",
             opaque_suffix=False, exists_hits=6, exists_shape="TTTTTT",
             over_status=200, over_ctype="text/html", accept="not-required",
             base="https://img.example.com/photos", base_path="photos",
             seq_format="{seq:05d}", suffix=".jpg",
             variants=[".jpg", "_1200x0.webp", "_800x0.webp"],
             page_bases=["https://img.example.com/photos"],
             id_patterns=[r"/photos/([0-9a-f]{8,})/"],
             id_resolution={"https://img.example.com/photos/aaa/00001.jpg": "aaa"})
    s.update(over)
    return s


def _levels(findings):
    return {f["field"]: f["level"] for f in findings}


# ------------------------------------------------- 没有差异就是没有差异(反面)


def test_identical_snapshots_report_nothing():
    """最要紧的一条**反面**用例: 没有它, 上面那些"报了问题"的用例可能只是恒报。"""
    assert drift_check.classify_drift(_snap(), _snap()) == []


def test_missing_snapshot_is_not_treated_as_an_empty_diff():
    """没有基线 / 探针没出结果时返回空 —— 但调用方**必须**把它读成"没查", 不是"没变"。

    这条钉的是两个概念的边界: `classify_drift` 返回 `[]` 有**两种**含义, 所以判断
    "过没过"绝不能只看它的长度(见 `main()` 的 `checked == 0` 分支)。
    """
    assert drift_check.classify_drift({}, _snap()) == []
    assert drift_check.classify_drift(_snap(), {}) == []
    assert drift_check.classify_drift({}, {}) == []


# ------------------------------------------------- 硬漂移(必须报)


@pytest.mark.parametrize("field,bad,why_hint", [
    ("reachable", False, "进不去了"),
    ("opaque_suffix", True, "序号后变内容哈希 -> 枚举前提没了"),
    ("accept", "required", "Accept 开始被校验 -> 全部 403"),
    ("base", "https://cdn2.example.com/photos", "基址搬家"),
    ("seq_format", "{seq:04d}", "补零宽度变了 -> 每页都 404"),
    ("over_status", 404, "越界对照变了 -> 判定规则变了"),
    ("over_ctype", "image/jpeg", "越界对照变了 -> 判定规则变了"),
])
def test_hard_drift_is_reported(field, bad, why_hint):
    """凡是"猜错了就**每条 URL 都错**"的差异, 必须报硬漂移。"""
    findings = drift_check.classify_drift(_snap(), _snap(**{field: bad}))
    assert findings, "%s 变了却没报 —— 这就是把站点改版读成'一切正常'" % field
    assert _levels(findings).get(field) == HARD, (field, findings)


def test_hard_drift_when_the_first_sequence_stops_hitting():
    """`exists_shape` 从 T 开头变成不命中 = 第一个序号就没中 -> 基址/宽度/suffix 错一个。"""
    findings = drift_check.classify_drift(_snap(), _snap(exists_shape="......"))
    assert _levels(findings).get("exists_shape") == HARD


def test_hard_drift_when_the_chosen_variant_disappears():
    """采信的那条直链的档位没了 —— 用户按那个档下单会整批 404。

    `suffix` 是样本直链的后缀, 也就是**采信的那个档位**; 新结果里连它都没有了 =
    站点换了档位命名。这里要真的把它从 `variants` 里去掉 —— 只是多一档不算硬漂移
    (那由下面的软漂移用例管)。
    """
    findings = drift_check.classify_drift(
        _snap(), _snap(variants=["_1200x0.webp", "_800x0.webp"]))
    assert _levels(findings).get("variants") == HARD


def test_hard_drift_when_a_url_the_declaration_relies_on_stops_resolving():
    """原来能解析出 gid 的输入现在解析不出来了 -> `id_patterns` 失效。"""
    findings = drift_check.classify_drift(
        _snap(), _snap(id_resolution={"https://img.example.com/photos/aaa/00001.jpg": None}))
    assert _levels(findings).get("id_resolution") == HARD


def test_hard_drift_when_the_hit_count_drops():
    """命中数下降要报 —— 可能只是图集变短, 也可能是判定规则变了, 值得人看一眼。"""
    findings = drift_check.classify_drift(_snap(), _snap(exists_hits=2))
    assert _levels(findings).get("exists_hits") == HARD


def test_hit_count_going_up_is_not_a_drift():
    """反方向不能报: 图集变长是常事, 报它就是噪音(告警一变成噪音就没人看了)。"""
    assert _levels(drift_check.classify_drift(_snap(), _snap(exists_hits=9))) == {}


# ------------------------------------------------- 软漂移(只记不报警)


@pytest.mark.parametrize("field,val", [
    ("variants", [".jpg", "_1200x0.webp", "_800x0.webp", "_600x0.webp"]),
    ("page_bases", ["https://img.example.com/photos", "https://img.example.com/photos2"]),
    ("id_patterns", [r"/photos/([0-9a-f]{8,})/", r"/photo/show/([0-9a-f]{8,})"]),
])
def test_soft_drift_is_recorded_but_never_hard(field, val):
    """多一档 / 多一条基址 / 多一种 URL 形态 —— 都是好事, 记一笔就好。

    它们算硬漂移的后果是**噪音**: 每次站点多挂一个 CDN 就报一次"可能改版了",
    报上几次之后这条告警就没人看了。
    """
    findings = drift_check.classify_drift(_snap(), _snap(**{field: val}))
    assert _levels(findings).get(field) == SOFT, findings


def test_a_new_variant_does_not_drag_the_others_into_the_report():
    """加了档位只该报一条 —— 别把没变的字段也搅进来(`classify_drift` 里 _levels 全覆盖)。"""
    findings = drift_check.classify_drift(
        _snap(), _snap(variants=[".jpg", "_1200x0.webp", "_800x0.webp", "_600x0.webp"]))
    assert set(_levels(findings)) == {"variants"}, findings


# ------------------------------------------------- 空转不许算绿(main 的退出码)


def _run_main(monkeypatch, tmp_path, probe_result):
    """跑 `main()`, 把网络与站点表都换掉; 返回 `SystemExit.code`(正常返回则 None)。

    ⚠️ 成功路径**不调** `sys.exit()`(只 `return`), 所以不能无条件
    `pytest.raises(SystemExit)` —— 那会把"没退出"也当成失败。这里显式区分两种出口。
    """
    monkeypatch.setenv("UWC_SITE_PROBE", str(tmp_path / "site_probe.json"))
    monkeypatch.setattr(drift_check, "declared_sites", lambda: [("demo", object())])
    monkeypatch.setattr(drift_check, "sample_media_urls",
                        lambda site: ["https://img.example.com/photos/aaa/00001.jpg"])
    monkeypatch.setattr(drift_check, "probe_now",
                        lambda urls, scan=6, timeout=None: probe_result)
    monkeypatch.setattr(sys, "argv", ["drift_check.py"])
    try:
        drift_check.main()
    except SystemExit as e:                       # 失败路径
        return e.code
    return None                                   # 正常走完 = 通过


def test_a_poll_where_nothing_was_checked_is_not_a_pass(monkeypatch, tmp_path, capsys):
    """**探针全失败 -> 非 0 退出。** 这是本文件里治"假绿"的那一条。

    以前这条路会印一句"结论: 0 个站点查过, 无硬漂移"然后 `exit 0`。挂进定时任务的人
    看到的是"一切正常", 实际上一次都没查 —— 巡检自己坏了被伪装成了站点没变。
    """
    code = _run_main(monkeypatch, tmp_path, probe_result={})
    out = capsys.readouterr().out
    assert code == 2, "空转却算通过: %r" % code
    assert "一个站点都没查成" in out
    assert "无硬漂移" not in out, "空转不许印任何看起来像'通过'的结论"


def test_a_poll_that_did_check_something_and_found_nothing_is_green(monkeypatch, tmp_path,
                                                                    capsys):
    """正面用例: 免得上面那条只是"`main()` 恒印总结"的空断言。

    第一次见 -> 记基线 -> 这一次确实**查了**(checked=1) -> 正常结束, 不退出。
    """
    code = _run_main(monkeypatch, tmp_path, probe_result={"img.example.com": _snap()})
    out = capsys.readouterr().out
    assert code is None, "查过了却被判失败: %r" % code
    assert "第一次见" in out
    assert "一个站点都没查成" not in out
