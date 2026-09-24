"""跳过项策略(R-CI-3)的自检。

为什么这些用例是必要的
======================
钩子本身也是判据, 所以它同样会"绿得没道理"或"红得没道理":

* **假绿**: 钩子写了, 但它一条也没拦住 —— 比如标签正则写窄了, `[deps:ffmpeg]`
  被当成"没有标签"之外的东西放过去。
* **假红**: 钩子把一条**正确**的跳过判红 —— 比如把"平台门控"误当成"依赖缺失"。

所以下面每条规则都有一个**故意造坏**的输入, 并且反向(`platform` 标着别的平台时
不许报)也有用例。除此之外还有一条**只关于语义方向**的用例 —— 它证明"标签等于
当前平台"那种判据在任何平台上都不可能两边都绿, 见 `test_the_platform_tag_is_
satisfiable_on_both_platforms`。那一版判据真的上线过, 而且**自检是绿的**: 用例
当时照着实现写, 忠实地编码了一个错的意图。发现它的是端到端那次全量。

最后一组特别重要: 它**静态**扫全仓的跳过点 —— 运行时钩子只能看见"这次真的跳了"
的用例, 而一个在 Windows 上跑、在 Linux 上跳的用例, 本机永远看不见它有没有标签。
静态扫才管得住"新加的那条"。
"""

import ast
from pathlib import Path

import conftest

TESTS = Path(__file__).resolve().parent

#: 标签格式**只有一份定义**(在 `tests/conftest.py`)。这里再抄一遍就会各自演化,
#: 出现"钩子认、静态扫不认"这种半通状态 —— 那正是本项目反复踩的"同一个东西两个定义"。
TAG = conftest._SKIP_TAG


def _skips(*pairs):
    return [(nodeid, reason) for nodeid, reason in pairs]


# ---------------------------------------------------------------- 勾子本身


def test_a_clean_run_has_no_problems():
    assert conftest._skip_problems(skips=[], on_ci=True) == []


def test_an_untagged_skip_is_a_problem():
    """「运气不好跳过了」和「这类用例本来就不该跑」必须是两个结论。"""
    problems = conftest._skip_problems(
        skips=_skips(("tests/t.py::test_x", "环境不支持")), on_ci=False)
    assert len(problems) == 1
    assert "机器标签" in problems[0]


def test_an_unknown_tag_category_counts_as_untagged():
    """不能靠"括号里有个冒号"就算有标签 —— 类别必须是白名单里的三个。"""
    problems = conftest._skip_problems(
        skips=_skips(("tests/t.py::test_x", "[whatever:foo] 随便写的")), on_ci=False)
    assert len(problems) == 1
    assert "机器标签" in problems[0]


def test_a_skip_for_a_platform_we_are_not_on_is_fine(monkeypatch):
    """`[platform:X]` 里的 X 是"这条用例**需要**的平台" —— 在别的平台上跳掉正是它该有的样子。"""
    monkeypatch.setattr(conftest, "_current_platform", lambda: "windows")
    assert conftest._skip_problems(
        skips=_skips(("tests/t.py::test_x", "[platform:posix] 只在 POSIX 上跑")),
        on_ci=True) == []


def test_a_skip_for_a_platform_we_are_on_means_the_gate_is_reversed(monkeypatch):
    """标 `[platform:windows]`(需要的正是 Windows)却又在 Windows 上跳过 = 门控写反了。"""
    monkeypatch.setattr(conftest, "_current_platform", lambda: "windows")
    problems = conftest._skip_problems(
        skips=_skips(("tests/t.py::test_x", "[platform:windows] 需要 NT 语义")),
        on_ci=False)
    assert len(problems) == 1
    assert "写反了" in problems[0]


def test_the_platform_tag_is_satisfiable_on_both_platforms(monkeypatch):
    """**为什么 `[platform:X]` 只能有一个方向** —— 这条用例就是那个证明。

    源文件里的标签是**写死的一次**: `[platform:posix]` 给"只在 POSIX 跑"的用例,
    `[platform:windows]` 给"只在 Windows 跑"的。而"这次真的跳了"的那一批取决于
    当前平台 —— 每条只在自己**不擅长**的平台上跳。

    所以判据只有一个方向能两边都绿。上一次的实现判的是"标签必须**等于**当前平台":
    在 Windows 上, 那批 POSIX-only 用例跳了, 标签是 `[platform:posix]` -> 红;
    把它们改标成 `[platform:windows]` 可以让 Windows 绿, 但同一个源文件拿到 POSIX 上,
    Windows-only 的那条跳了、标签 `[platform:windows]` -> 又红。
    **无论标签怎么写, 总有一个平台是红的** —— 这不是配置问题, 是判据方向错了。
    """
    posix_only = ("tests/t.py::test_posix_only", "[platform:posix] 需要 POSIX 语义")
    windows_only = ("tests/t.py::test_windows_only", "[platform:windows] 需要 NT 语义")
    for current, skipped in (("windows", posix_only), ("posix", windows_only)):
        monkeypatch.setattr(conftest, "_current_platform", lambda c=current: c)
        assert conftest._skip_problems(skips=_skips(skipped), on_ci=True) == [], (
            "在 %s 上, 一条只在别的平台跑的用例被自己需要的平台标签判红了" % current)


def test_a_missing_dependency_skip_is_allowed_locally_but_not_on_ci():
    """这条规矩的**全部价值**就在这个不对称上。

    本机没装 ffmpeg -> 跳过是正常的; CI 上没装 -> 那 7 条最贵的判据从来没跑过,
    而 job 却是绿的。同一个跳过在两个环境里必须是两个结论。
    """
    skips = _skips(("tests/t.py::test_decode", "[deps:ffmpeg] 未探测到 ffmpeg"))
    assert conftest._skip_problems(skips=skips, on_ci=False) == []
    problems = conftest._skip_problems(skips=skips, on_ci=True)
    assert len(problems) == 1
    assert "[deps:ffmpeg]" in problems[0]
    assert "partial" in problems[0]


def test_an_env_skip_is_judged_the_same_way_as_a_missing_dependency():
    skips = _skips(("tests/t.py::test_make", "[env:make] make is unavailable"))
    assert conftest._skip_problems(skips=skips, on_ci=False) == []
    assert len(conftest._skip_problems(skips=skips, on_ci=True)) == 1


def test_every_skip_gets_its_own_problem_line():
    """一次报清, 别只报第一条 —— 修的人需要看到全部。"""
    problems = conftest._skip_problems(
        skips=_skips(("a::x", "没标签"), ("b::y", "也没标签"), ("c::z", "还是没")),
        on_ci=False)
    assert len(problems) == 3


# ------------------------------------------------- 静态扫: 全仓每个跳过点都要有标签


def _skip_reasons(tree):
    """AST 里所有"跳过"的**理由字符串**。

    按语法结构找, 不按文本找 —— 文本扫会把文档/注释里的反面教材也算进来
    (本项目那条 AST 门禁第一天就是这么红的)。
    """
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = None
        if isinstance(fn, ast.Attribute):
            name = fn.attr
        elif isinstance(fn, ast.Name):
            name = fn.id
        if name not in ("skipif", "skip", "importorskip"):
            continue
        reasons = []
        if name == "skip":
            if node.args:
                reasons.append(node.args[0])
            for kw in node.keywords:
                if kw.arg == "reason":
                    reasons.append(kw.value)
        else:
            for kw in node.keywords:
                if kw.arg == "reason":
                    reasons.append(kw.value)
            if name == "importorskip" and len(node.args) > 1:
                reasons.append(node.args[1])
        for r in reasons:
            out.append((node.lineno, r))
    return out


def test_every_skip_site_in_the_repo_carries_a_machine_tag():
    """**运行时钩子看不见的那一半**, 由这条静态扫补上。

    例: `@pytest.mark.skipif(os.name == "nt", …)` 在本机(Windows)永远不会真的跳,
    于是 `pytest_runtest_logreport` 一次也不会看到它。但它在 CI 上会跳 ——
    标签要是在这时候才发现缺, 已经晚了一整轮。
    """
    missing = []
    for path in sorted(TESTS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno, reason in _skip_reasons(tree):
            if not isinstance(reason, ast.Constant) or not isinstance(reason.value, str):
                missing.append("%s:%d 跳过理由不是字面量, 无法核标签" % (path.name, lineno))
                continue
            if not TAG.search(reason.value):
                missing.append("%s:%d 理由 %r 里没有 [platform|deps|env:…] 标签"
                               % (path.name, lineno, reason.value[:60]))
    assert not missing, "有跳过点没打机器标签:\n  " + "\n  ".join(missing)


def test_the_static_scan_would_catch_a_new_untagged_skip():
    """证明上面那条静态扫**真的会红**(而不是恰好全都有标签)。"""
    tree = ast.parse(
        "import pytest\n"
        "@pytest.mark.skipif(True, reason='环境不支持')\n"
        "def test_x():\n    pass\n")
    reasons = _skip_reasons(tree)
    assert len(reasons) == 1
    assert not TAG.search(reasons[0][1].value)


def test_a_reason_that_is_not_a_literal_is_reported_not_skipped():
    """f-string 拼出来的理由核不了标签 —— 那也要报, 不能默认通过。"""
    tree = ast.parse(
        "import pytest\n"
        "reason = 'x'\n"
        "@pytest.mark.skipif(True, reason='[' + reason + ']')\n"
        "def test_x():\n    pass\n")
    reasons = _skip_reasons(tree)
    assert reasons and isinstance(reasons[0][1], ast.BinOp)
