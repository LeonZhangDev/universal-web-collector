"""`scripts/gateguard.py` 的自检。

一条门禁如果没有"故意造坏"的用例, 就没法区分"查了没问题"和"根本没查" ——
与第 18 条坑同源: 该断言的地方没断言。

所以下面**每条检查都有一正一反**:
  · 反: 造一份被改坏的最小仓库/文档, 证明它会报出**带代号**的问题
  · 正: 说明这条检查在当前仓库上是绿的(而不是"永远是红的")

判据全部读 `gate.kinds()` / `gate.checked`, 不读中文文案(那是本项目记过的假红)。
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "backend"))

import gateguard  # noqa: E402
from gate import Gate, Problem  # noqa: E402


def _repo(tmp_path, readme, ci=None, scripts=("gateguard.py",), extra_files=None):
    """造一份最小仓库: README + ci.yml + 若干脚本。"""
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / "README.md").write_text(readme, encoding="utf-8", newline="")
    (tmp_path / "scripts").mkdir()
    for name in scripts:
        (tmp_path / "scripts" / name).write_text("# x\n", encoding="utf-8", newline="")
    if ci is not None:
        (tmp_path / ".github" / "workflows" / "ci.yml").write_text(ci, encoding="utf-8", newline="")
    for rel, body in (extra_files or {}).items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8", newline="")
    return tmp_path


CONTRACT = """# R
| 命令 | 在 CI 跑 | 不在 CI 的理由 |
| --- | --- | --- |
| `python scripts/gateguard.py` | ✅ | — |
"""

#: 一个"本仓库之外的脚本一律不跑"的最小 ci.yml。带 `UWC_CI=1`, 否则会额外报
#: `ci-env-missing` —— 那样用例就测不出它本来要测的东西了。
CI_BARE = """jobs:
  b:
    steps:
      - run: echo hi
        env:
          UWC_CI: "1"
"""

CI_OK = """jobs:
  backend:
    steps:
      - run: python scripts/gateguard.py
        env:
          UWC_CI: "1"
"""


# ------------------------------------------------------------------ 收集数解析


def test_the_pytest_summary_line_is_parsed():
    assert gateguard.parse_collected("1146 tests collected in 11.2s") == 1146


def test_the_plural_and_singular_forms_both_parse():
    assert gateguard.parse_collected("1 test collected in 0.1s") == 1


def test_collect_output_without_a_summary_falls_back_to_counting_nodeids():
    out = "tests/test_a.py::test_x\ntests/test_a.py::test_y\n"
    assert gateguard.parse_collected(out) == 2


def test_unparsable_output_yields_none_so_the_gate_says_it_checked_nothing():
    """认不出就必须是 `None` —— 当成 0 或 1 都会变成假绿/假红。"""
    assert gateguard.parse_collected("ERROR: file or directory not found") is None


# ------------------------------------------------------- ② 文档数字 vs 实测


def test_doc_counts_is_green_when_the_readme_number_matches(tmp_path):
    readme = "```bash\nmake test   # pytest (1146 用例, 本机; CI/Linux 收集 1147)\n```\n"
    g = gateguard.gate_doc_counts(_repo(tmp_path, readme), actual_cases=1146)
    assert g.ok, g.problems


def test_doc_counts_accepts_the_other_platform_number_too(tmp_path):
    """口径差异不该造出假红: 文档里同时写了两个数, 哪一个平台都能对上。"""
    readme = "make test   # pytest (1146 用例, 本机 Windows; CI/Linux 收集 1147)\n"
    assert gateguard.gate_doc_counts(_repo(tmp_path, readme), actual_cases=1147).ok


def test_doc_counts_flags_a_stale_number(tmp_path):
    readme = "make test   # pytest (1105 用例)\n"
    g = gateguard.gate_doc_counts(_repo(tmp_path, readme), actual_cases=1146)
    assert not g.ok
    assert g.kinds() == {"doc-count-stale"}
    assert "1146" in str(g.problems[0])


def test_doc_counts_flags_a_readme_with_no_make_test_line(tmp_path):
    g = gateguard.gate_doc_counts(_repo(tmp_path, "没有任何命令清单\n"), actual_cases=1)
    assert not g.ok
    assert g.kinds() == {"doc-missing"}


def test_doc_counts_compares_expected_checks_against_the_readme_line(tmp_path):
    readme = (
        "```bash\nmake test   # pytest (7 用例)\n"
        "python scripts/verify_x.py   # 端到端 (40 项断言)\n```\n")
    extra = {"scripts/verify_x.py": "EXPECTED_CHECKS = 40\n"}
    g = gateguard.gate_doc_counts(
        _repo(tmp_path, readme, scripts=("verify_x.py",), extra_files=extra),
        actual_cases=7)
    assert g.ok, g.problems


def test_doc_counts_flags_an_expected_checks_that_the_readme_disagrees_with(tmp_path):
    readme = (
        "```bash\nmake test   # pytest (7 用例)\n"
        "python scripts/verify_x.py   # 端到端 (39 项断言)\n```\n")
    extra = {"scripts/verify_x.py": "EXPECTED_CHECKS = 40\n"}
    g = gateguard.gate_doc_counts(
        _repo(tmp_path, readme, scripts=("verify_x.py",), extra_files=extra),
        actual_cases=7)
    assert not g.ok
    assert g.kinds() == {"doc-count-stale"}


def test_doc_counts_flags_a_declared_count_that_the_readme_never_mentions(tmp_path):
    readme = "```bash\nmake test   # pytest (7 用例)\n```\n"
    extra = {"scripts/verify_x.py": "EXPECTED_CHECKS = 40\n"}
    g = gateguard.gate_doc_counts(
        _repo(tmp_path, readme, scripts=("verify_x.py",), extra_files=extra),
        actual_cases=7)
    assert not g.ok
    assert g.kinds() == {"doc-missing"}


def test_doc_counts_does_not_silently_pass_when_pytest_could_not_be_asked(
        tmp_path, monkeypatch):
    """问不出收集数时不许猜 —— 要报「这一项没核」, 而不是拿 0 或 1 去比。"""
    monkeypatch.setattr(gateguard, "collected_case_count",
                        lambda root: (None, "ERROR: file or directory not found"))
    g = gateguard.gate_doc_counts(_repo(tmp_path, "make test   # pytest (7 用例)\n"))
    assert not g.ok
    assert g.kinds() == {"pytest-collect-failed"}


def test_doc_counts_reports_nothing_checked_when_there_is_no_readme(tmp_path):
    g = gateguard.gate_doc_counts(tmp_path)
    assert not g.ok
    assert g.kinds() == {"doc-missing"}
    assert g.checked == 0
    assert not g.ok                      # 空转也不许算绿


# --------------------------------------------------------------- ① CI 契约


def test_ci_contract_is_green_when_the_table_matches_ci(tmp_path):
    root = _repo(tmp_path, CONTRACT, ci=CI_OK)
    g = gateguard.gate_ci_contract(root)
    assert g.ok, g.problems


def test_ci_contract_flags_a_script_nobody_says_anything_about(tmp_path):
    """**R-CI-1 的核心**: 没被提到 = 红, 没有第三态。"""
    root = _repo(tmp_path, CONTRACT, ci=CI_OK, scripts=("gateguard.py", "orphan.py"))
    g = gateguard.gate_ci_contract(root)
    assert not g.ok
    assert g.kinds() == {"contract-missing"}
    assert "orphan.py" in str(g.problems[0])


def test_ci_contract_flags_a_row_that_lies_about_running_in_ci(tmp_path):
    root = _repo(tmp_path, CONTRACT, ci=CI_BARE)
    g = gateguard.gate_ci_contract(root)
    assert not g.ok
    assert "contract-lying" in g.kinds()


def test_ci_contract_flags_a_row_that_says_it_does_not_run_in_ci_but_does(tmp_path):
    readme = CONTRACT.replace("| ✅ | — |", "| ❌ | 看着像不该跑 |")
    root = _repo(tmp_path, readme, ci=CI_OK)
    g = gateguard.gate_ci_contract(root)
    assert not g.ok
    assert "contract-lying" in g.kinds()


def test_ci_contract_flags_a_local_only_row_without_a_reason(tmp_path):
    """「暂时不做」不许写成「不该做」—— 没有理由的 ❌ 就是这种。"""
    readme = ("| 命令 | 在 CI 跑 | 不在 CI 的理由 |\n| --- | --- | --- |\n"
              "| `python scripts/gateguard.py` | ❌ | — |\n")
    root = _repo(tmp_path, readme, ci=CI_BARE)
    g = gateguard.gate_ci_contract(root)
    assert not g.ok
    assert "contract-no-reason" in g.kinds()


def test_ci_contract_flags_a_cell_that_is_neither_tick_nor_cross(tmp_path):
    """含糊的格子会被读成"大概跑了吧" —— 只认 ✅ / ❌。"""
    readme = CONTRACT.replace("| ✅ | — |", "| 也许 | — |")
    root = _repo(tmp_path, readme, ci=CI_OK)
    g = gateguard.gate_ci_contract(root)
    assert not g.ok
    assert "contract-format" in g.kinds()


def test_ci_contract_flags_npm_install_even_though_the_lockfile_exists(tmp_path):
    root = _repo(tmp_path, CONTRACT, ci=CI_OK + "      - run: npm install\n")
    g = gateguard.gate_ci_contract(root)
    assert not g.ok
    assert "ci-npm-install" in g.kinds()


def test_ci_contract_flags_a_ci_without_the_uwc_ci_marker(tmp_path):
    """没有 `UWC_CI=1`, "这次的跳过是平台门控还是缺依赖"就分不出来。"""
    root = _repo(tmp_path, CONTRACT, ci="jobs:\n  b:\n    steps:\n"
                                       "      - run: python scripts/gateguard.py\n")
    g = gateguard.gate_ci_contract(root)
    assert not g.ok
    assert "ci-env-missing" in g.kinds()

    # 反向: 同样的 ci.yml 加上 UWC_CI 之后这条检查必须放行 —— 否则它是"永远红",
    # 而永远红的门禁等于没有门禁。
    root2 = _repo(tmp_path / "ok", CONTRACT,
                  ci="jobs:\n  b:\n    steps:\n"
                     "      - run: python scripts/gateguard.py\n"
                     '        env:\n          UWC_CI: "1"\n')
    assert gateguard.gate_ci_contract(root2).ok


def test_ci_contract_flags_a_missing_ci_file(tmp_path):
    root = _repo(tmp_path, CONTRACT)
    g = gateguard.gate_ci_contract(root)
    assert not g.ok
    assert g.checked == 0
    assert g.kinds() == {"ci-missing"}


def test_ci_contract_flags_a_table_that_cannot_be_found(tmp_path):
    root = _repo(tmp_path, "# 没有契约表\n", ci=CI_OK)
    g = gateguard.gate_ci_contract(root)
    assert not g.ok
    assert g.kinds() == {"contract-missing"}


def test_the_ci_contract_parser_reads_multiple_scripts_from_one_row(tmp_path):
    readme = ("| 命令 | 在 CI 跑 | 不在 CI 的理由 |\n| --- | --- | --- |\n"
              "| `scripts/a.py` / `scripts/b.py` | ❌ | 人肉探针 |\n")
    root = _repo(tmp_path, readme, ci=CI_BARE, scripts=("a.py", "b.py"))
    g = gateguard.gate_ci_contract(root)
    assert g.ok, g.problems


def _npm_repo(tmp_path, script_body, ci_run):
    root = _repo(tmp_path, "| 命令 | 在 CI 跑 | 不在 CI 的理由 |\n| --- | --- | --- |\n"
                          "| `npm run test:task-query`"
                          "（`frontend/scripts/test-task-query.mjs`） | ✅ | — |\n",
                 ci="jobs:\n  f:\n    steps:\n      - run: %s\n"
                    '        env:\n          UWC_CI: "1"\n' % ci_run,
                 scripts=())
    (root / "frontend").mkdir()
    (root / "frontend" / "package.json").write_text(
        '{"scripts": {"test:task-query": "%s"}}' % script_body, encoding="utf-8", newline="")
    (root / "frontend" / "scripts").mkdir()
    (root / "frontend" / "scripts" / "test-task-query.mjs").write_text(
        "// x\n", encoding="utf-8", newline="")
    return root


def test_an_npm_run_in_ci_is_resolved_to_the_file_it_really_runs(tmp_path):
    """`npm run X` 在 ci.yml 里看不出跑的是哪个文件 —— 门禁要顺着 package.json 找到它。"""
    root = _npm_repo(tmp_path, "node scripts/test-task-query.mjs",
                     "npm run test:task-query")
    assert gateguard.npm_script_targets(root) == {
        "test:task-query": "frontend/scripts/test-task-query.mjs"}
    assert gateguard.gate_ci_contract(root).ok


def test_an_npm_run_that_ci_never_invokes_leaves_the_script_unaccounted_for(tmp_path):
    """反方向: ci.yml 里没有这一句, 那条 ✅ 就是假的, 必须红。"""
    root = _npm_repo(tmp_path, "node scripts/test-task-query.mjs", "echo hi")
    g = gateguard.gate_ci_contract(root)
    assert not g.ok
    assert "contract-lying" in g.kinds()


def test_a_comment_mentioning_npm_install_does_not_trip_the_gate(tmp_path):
    """**这条是踩出来的假红。**

    我在 ci.yml 里写了一句注释:"用 `npm ci` 而不是 `npm install`, 因为后者会顺手改锁文件"。
    文本扫立刻把这条**正确**的配置判红了 —— 而那句话的意思恰恰是"我们不用它"。
    判据必须落在结构上(同源教训: 那条 AST 门禁第一天用正则扫文本, 被自己的文档字符串判红)。
    """
    ci = (CI_OK + "# 注: 不用 npm install —— 它会顺手改锁文件把不一致吞掉\n"
                    "      - run: npm ci\n")
    root = _repo(tmp_path, CONTRACT, ci=ci)
    g = gateguard.gate_ci_contract(root)
    assert g.ok, g.problems


def test_a_comment_mentioning_a_script_does_not_count_as_running_it(tmp_path):
    """注释里提到某个脚本 ≠ 在跑它 —— 否则"谁在 CI 跑"就成了自由心证。"""
    ci = CI_OK + "# 以后也许把 scripts/orphan.py 接进来\n"
    root = _repo(tmp_path, CONTRACT, ci=ci, scripts=("gateguard.py", "orphan.py"))
    g = gateguard.gate_ci_contract(root)
    assert not g.ok
    assert g.kinds() == {"contract-missing"}


def test_an_unparsable_ci_file_is_reported_rather_than_ignored(tmp_path):
    """解析不出来 = **这一项没核**, 不是通过。"""
    root = _repo(tmp_path, CONTRACT, ci="jobs: [unclosed\n")
    g = gateguard.gate_ci_contract(root)
    assert not g.ok
    assert g.checked == 0
    assert g.kinds() == {"ci-unparsable"}


def test_the_parsed_run_steps_and_env_names_are_what_the_gate_reads():
    runs, envs = gateguard.parse_ci(CI_OK)
    assert runs == ["python scripts/gateguard.py"]
    assert envs == {"UWC_CI"}


def test_parse_ci_returns_none_instead_of_an_empty_run_list_on_garbage():
    assert gateguard.parse_ci("{[") is None


# ------------------------------------------------------------ ③ 未用 import


def test_unused_imports_flags_a_provably_unused_name(tmp_path):
    root = _repo(tmp_path, "# r\n", extra_files={
        "m.py": "import os\nimport sys\n\nprint(sys.argv)\n"})
    g = gateguard.gate_unused_imports(root)
    assert not g.ok
    assert g.kinds() == {"unused-import"}
    assert "os" in str(g.problems[0])


def test_unused_imports_does_not_flag_a_name_mentioned_only_in_a_comment(tmp_path):
    """**误报比漏报贵**: 误报会让下一个人删掉一个在用的 import。

    所以规则要求"这个名字在整份文件的其余文本里一次都不出现" —— 连注释里
    出现过也算用了, 宁可漏报。
    """
    root = _repo(tmp_path, "# r\n", extra_files={
        "m.py": "import os\n\n# os 是给下面那段留的\nprint(1)\n"})
    assert gateguard.gate_unused_imports(root).ok


def test_unused_imports_honours_noqa(tmp_path):
    root = _repo(tmp_path, "# r\n", extra_files={
        "m.py": "from x import y  # noqa: F401\nprint(1)\n"})
    assert gateguard.gate_unused_imports(root).ok


def test_unused_imports_skips_a_module_that_has_all(tmp_path):
    """有 `__all__` 的文件通常在重导出, 整个跳过。"""
    root = _repo(tmp_path, "# r\n", extra_files={
        "m.py": "__all__ = ['y']\nfrom x import y\n"})
    g = gateguard.gate_unused_imports(root)
    assert g.ok
    assert any("__all__" in " ".join(r) for r in g.rows)


def test_unused_imports_skips_a_module_with_type_checking(tmp_path):
    root = _repo(tmp_path, "# r\n", extra_files={
        "m.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from x import y\n"})
    assert gateguard.gate_unused_imports(root).ok


def test_unused_imports_reports_a_file_it_could_not_parse(tmp_path):
    root = _repo(tmp_path, "# r\n", extra_files={"m.py": "def (:\n"})
    g = gateguard.gate_unused_imports(root)
    assert not g.ok
    assert g.kinds() == {"syntax-error"}


# ------------------------------------------------------------- ④ 围栏配平


def test_fences_flag_an_unbalanced_document(tmp_path):
    root = tmp_path
    (root / "README.md").write_text("```bash\nls\n", encoding="utf-8", newline="")
    g = gateguard.gate_fences(root)
    assert not g.ok
    assert g.kinds() == {"fence-unbalanced"}


def test_fences_accept_a_balanced_document(tmp_path):
    (tmp_path / "README.md").write_text("```bash\nls\n```\n", encoding="utf-8", newline="")
    assert gateguard.gate_fences(tmp_path).ok


def test_fences_do_not_scan_the_memory_notes(tmp_path):
    """`.workbuddy/` 是过程笔记, 不是产物 —— 让笔记的围栏决定 CI 红绿是判据挂错了地方。"""
    (tmp_path / "README.md").write_text("```\nx\n```\n", encoding="utf-8", newline="")
    d = tmp_path / ".workbuddy" / "memory"
    d.mkdir(parents=True)
    (d / "n.md").write_text("```\n没闭合\n", encoding="utf-8", newline="")
    assert gateguard.gate_fences(tmp_path).ok


# ------------------------------------------- ⑤ `with <session>.get()` 门禁


def test_the_wrapped_response_gate_flags_the_forbidden_shape(tmp_path):
    root = _repo(tmp_path, "# r\n", extra_files={
        "m.py": "with session.get(u, stream=True) as r:\n    pass\n"})
    g = gateguard.gate_no_wrapped_response(root)
    assert not g.ok
    assert g.kinds() == {"wrapped-response"}


def test_the_wrapped_response_gate_flags_head_and_post_too(tmp_path):
    root = _repo(tmp_path, "# r\n", extra_files={
        "m.py": ("with s.head(u) as r:\n    pass\n"
                 "with s.post(u, data=1) as r2:\n    pass\n")})
    g = gateguard.gate_no_wrapped_response(root)
    assert len(g.problems) == 2


def test_the_wrapped_response_gate_allows_the_transport_helper(tmp_path):
    """正确写法必须放行 —— 否则这条门禁会逼人把它删掉。"""
    root = _repo(tmp_path, "# r\n", extra_files={
        "m.py": "with transport.streamed(s, 'get', u, stream=True) as r:\n    pass\n"})
    assert gateguard.gate_no_wrapped_response(root).ok


def test_the_wrapped_response_gate_allows_ordinary_context_managers(tmp_path):
    root = _repo(tmp_path, "# r\n", extra_files={
        "m.py": ("with open(p) as f:\n    pass\n"
                 "with requests.Session() as s:\n    pass\n")})
    assert gateguard.gate_no_wrapped_response(root).ok


def test_an_async_with_is_checked_too(tmp_path):
    root = _repo(tmp_path, "# r\n", extra_files={
        "m.py": "async def f():\n    async with s.get(u) as r:\n        pass\n"})
    assert not gateguard.gate_no_wrapped_response(root).ok


# ----------------------------------------------------------------- ⑥ 行尾


def test_line_endings_flags_a_crlf_file(tmp_path):
    (tmp_path / "m.py").write_bytes(b"a\nb\r\n")
    g = gateguard.gate_line_endings(tmp_path)
    assert not g.ok
    assert g.kinds() == {"crlf"}
    assert "混合" in str(g.problems[0])


def test_line_endings_reads_the_policy_from_gitattributes(tmp_path):
    """**单一来源**: 改了 `.gitattributes` 判据就跟着变。

    这条用例存在的理由: 如果门禁自己另写一份"哪些文件要 LF"的清单, 就会出现
    "git 认为是 LF、门禁认为是 CRLF"的半通状态 —— 同一个东西两个定义。
    """
    (tmp_path / "m.py").write_text("fine\n", encoding="utf-8", newline="")       # 用来"有东西可核"
    (tmp_path / "m.bin").write_bytes(b"a\r\nb\r\n")
    (tmp_path / ".gitattributes").write_text("* text=auto eol=lf\n*.bin -text\n",
                                             encoding="utf-8", newline="")
    assert gateguard.gate_line_endings(tmp_path).ok
    (tmp_path / ".gitattributes").write_text("* text=auto eol=lf\n", encoding="utf-8", newline="")
    assert not gateguard.gate_line_endings(tmp_path).ok


def test_line_endings_ignores_files_with_nul_bytes(tmp_path):
    (tmp_path / "m.py").write_text("fine\n", encoding="utf-8", newline="")
    (tmp_path / "a.dat").write_bytes(b"\x00\r\n\x00\r\n")
    assert gateguard.gate_line_endings(tmp_path).ok


def test_line_endings_does_not_call_it_green_when_it_checked_nothing(tmp_path):
    """整个目录只有二进制时, 这条闸**一项都没核到** —— 那不是"通过"。"""
    (tmp_path / "a.dat").write_bytes(b"\x00\r\n")
    g = gateguard.gate_line_endings(tmp_path)
    assert g.checked == 0
    assert not g.ok
    assert g.kinds() == {"nothing-checked"}


def test_gitattributes_matching_prefers_the_last_rule(tmp_path):
    (tmp_path / ".gitattributes").write_text(
        "*.dat -text\nsub/*.dat text eol=lf\n", encoding="utf-8", newline="")
    policy = gateguard.eol_policy(tmp_path)
    assert policy("sub/a.dat") == "lf"
    assert policy("a.dat") == "binary"


def test_attr_pattern_without_a_slash_matches_the_basename(tmp_path):
    (tmp_path / ".gitattributes").write_text("*.ps1 text eol=crlf\n", encoding="utf-8", newline="")
    policy = gateguard.eol_policy(tmp_path)
    assert policy("deep/nested/x.ps1") == "crlf"
    assert policy("deep/nested/x.py") == "lf"


# ------------------------------------------------------- main: 退出码与形状


def _gate(title, checked, problems=()):
    return Gate(title, checked=checked, problems=list(problems))


def test_main_returns_nonzero_when_a_gate_is_red(monkeypatch, capsys):
    monkeypatch.setattr(gateguard, "GATES", (
        ("good", lambda root: _gate("好闸", 3)),
        ("bad", lambda root: _gate("坏闸", 1, [Problem("boom", "炸了")])),
    ))
    assert gateguard.main([]) == 1
    out = capsys.readouterr().out
    assert "boom" in out


def test_main_reports_how_many_things_each_gate_checked(monkeypatch, capsys):
    monkeypatch.setattr(gateguard, "GATES", (("good", lambda root: _gate("好闸", 3)),))
    assert gateguard.main([]) == 0
    assert "核了 3 项" in capsys.readouterr().out


def test_main_treats_a_gate_that_checked_nothing_as_red(monkeypatch, capsys):
    """空转不许印 ok —— 这是整套门禁唯一防"假绿"的性质, `Gate` 自己保证。"""
    monkeypatch.setattr(gateguard, "GATES", (("empty", lambda root: _gate("空闸", 0)),))
    assert gateguard.main([]) == 1
    assert "nothing-checked" in capsys.readouterr().out


def test_main_can_run_a_single_gate(monkeypatch):
    monkeypatch.setattr(gateguard, "GATES", (
        ("a", lambda root: _gate("A", 1)),
        ("b", lambda root: _gate("B", 1, [Problem("boom", "炸")])),
    ))
    assert gateguard.main(["--only", "a"]) == 0


# ------------------------------------------------------------- 真实仓库


def test_every_gate_on_the_real_repo_reports_what_it_checked():
    """一条闸也不许是"空转绿" —— 所以它报的项数必须 > 0。

    (`doc-counts` 要起 pytest 子进程, 单列在下面的用例里, 这里跳过。)
    """
    for name, fn in gateguard.GATES:
        if name == "doc-counts":
            continue
        gate = fn(ROOT)
        assert gate.checked > 0, "%s 一项都没核" % name


def test_the_real_repo_has_no_structural_problems():
    """结构类检查(除文档数字外)在当前仓库上必须是绿的。

    这条如果红了, 说明改代码的时候顺手带坏了别处 —— 它是本仓库的"体检基线"。
    """
    red = {}
    for name, fn in gateguard.GATES:
        if name == "doc-counts":
            continue
        gate = fn(ROOT)
        if not gate.ok:
            red[name] = [p.kind for p in gate.effective_problems()]
    assert not red, "结构门禁红了: %r" % red


def test_the_readme_case_count_matches_what_pytest_really_collects():
    """整件事的重点: **README 里那个数是真的**。

    它要起一个 pytest 子进程(几十秒里的大头), 但这一条恰恰是最值得花的 ——
    "文档声称 vs 实测"正是本轮要堵的那个洞。
    """
    actual, err = gateguard.collected_case_count(ROOT)
    assert actual is not None, "问不出收集总数: %s" % err
    gate = gateguard.gate_doc_counts(ROOT, actual_cases=actual)
    assert gate.ok, gate.problems


def test_the_expected_checks_constants_are_found_in_the_scripts():
    found = {p.name: n for p, n in gateguard.expected_checks(ROOT)}
    assert found.get("verify_output.py") == 40
    assert found.get("verify_hls.py") == 18


def test_tracked_files_falls_back_to_a_walk_when_the_root_is_not_its_own_repo(
        tmp_path, monkeypatch):
    """**这条钉的是一个真的踩过的坑**(2026-09-24 被本文件的用例抓出来的)。

    本机项目目录在 `C:\\Users\\admin` 那个大仓库下面。于是当 `root` 是别的目录
    (比如用例的 `tmp_path`)时, `git ls-files` 会**成功返回** —— 只是清单被限定在
    那条前缀下, 一个文件都没有。门禁于是"一项都没核到", 而它自己的判据表面是绿的。

    所以 `tracked_files` 必须先确认 `root` **自己**就是仓库顶层。
    """
    (tmp_path / "m.py").write_text("x\n", encoding="utf-8", newline="")
    monkeypatch.setattr(gateguard, "_git_toplevel", lambda root: Path("C:/其它/仓库"))
    assert {p.name for p in gateguard.tracked_files(tmp_path)} == {"m.py"}


def test_tracked_files_uses_git_when_the_root_is_a_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(gateguard, "_git_toplevel", lambda root: Path(root).resolve())
    monkeypatch.setattr(gateguard.subprocess, "run",
                        lambda *a, **kw: subprocess.CompletedProcess(a, 0, b"a.py\0b.md\0"))
    names = sorted(p.name for p in gateguard.tracked_files(tmp_path))
    assert names == ["a.py", "b.md"]


def test_tracked_files_comes_from_git_not_from_a_directory_walk():
    """判据是"仓库里有什么", 不是"我的工作区有什么"。

    (所以这里只断言"和 `git ls-files` 数出来的一样多", 不断言某个具体文件 ——
    否则新加的文件在 `git add` 之前会让这条用例红, 而那不是回归。)
    """
    files = gateguard.tracked_files(ROOT)
    git = subprocess.run(["git", "ls-files"], cwd=str(ROOT), capture_output=True,
                         text=True)
    assert git.returncode == 0
    assert len(files) == len([x for x in git.stdout.splitlines() if x]) > 150
    assert all(f.is_absolute() for f in files)


@pytest.mark.parametrize("name", ["gate.py", "add_site.py", "gateguard.py"])
def test_the_shared_gate_module_has_exactly_one_definition(name):
    """`Gate` / `Problem` 只许有一份定义 —— 两份就会各自演化。"""
    src = (ROOT / "scripts" / name).read_text(encoding="utf-8")
    tree = ast.parse(src)
    classes = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
    assert "Gate" not in classes or name == "gate.py", \
        "%s 里重新定义了一份 Gate" % name
    assert "Problem" not in classes or name == "gate.py", \
        "%s 里重新定义了一份 Problem" % name
