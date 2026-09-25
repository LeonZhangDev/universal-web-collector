"""仓库级门禁: 让「**文档里说的**」与「**代码里做的**」是同一件事。

为什么需要它
============
`add_site.py` 管的是"某个站点的声明对不对"; 本脚本管的是**仓库自己的声称**——
而这一类错误的共同形态, 和本项目 18 条静默坑一模一样: **不报错, 只是结论是错的**。

    README 说"1146 用例", 实际收集 1147   -> 没人发现, 因为两边都是绿的
    README 说"40 项断言", 有人删掉一项     -> 脚本照印"全部 39 项通过"
    CI 里 7 条最贵的用例**只在跳**         -> job 仍然显示绿的
    `with session.get(...)` 被抄到第三处   -> 只在 CF 后面才发作, 静默拿不到正文

四条规矩(前三条写进 README, 第四条是本脚本的存在意义)
======================================================
**R-CI-1 没有第三态**: 仓库里每个判据脚本, 要么在 `.github/workflows/ci.yml`
  的调用链里, 要么在 README 的「CI 契约」表里**写明"只在本地跑 + 为什么"**。
  没被提到 = 红。这样"新增了一个守卫但忘了接 CI"不再可能静默发生。

**R-CI-2 数字必须可核对**: README 是**唯一**的"当前口径"落点(历史版本数字留在
  `docs/AGENT_DEVELOPMENT_GUIDE.md` 的版本史里, 那些**不参与核对** —— 它们本来就
  是"当时是多少", 拿现在的实测去比是另一种假红)。README 里的用例数必须等于这台
  机器上 `pytest --collect-only` 的收集总数; 每个声明了 `EXPECTED_CHECKS` 的脚本,
  它的数必须与 README 里那一行的数一致。

**R-CI-3 平台门控之外不许跳过**(在 `tests/conftest.py` 里执行, 见那里的
  `_skip_policy`): CI 里不许出现"因为没装依赖而整类跳过"。

**④ 判据只挂在代号/结构上**: 这里所有检查读的都是 AST、路径、数字和代码标识符,
  没有一条 match 中文文案。理由见 `scripts/gate.py` 顶部的②(那条真实的假红)。

用法::

    python scripts/gateguard.py              # 全部检查
    python scripts/gateguard.py --only ci-contract
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import functools
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))     # 让 gate 可 import

from gate import Gate, problem  # noqa: E402

#: 这些目录不进任何检查。
#:
#: `.workbuddy/` 是**过程笔记**(记忆/坑表), 不是产物。让笔记的围栏配平或行尾去决定
#: CI 的红绿, 等于把判据挂在一个与结论无关的东西上 —— 那就是假红。
SKIP_DIRS = {
    ".git", ".workbuddy", "node_modules", "__pycache__", ".venv", "dist",
    ".pytest_cache", "downloads", "data", "browser_state",
    # git worktree 的检出目录: 它**自己就是一个仓库**(有自己的 index), 让本仓库的
    # 门禁去判另一个分支的旧代码, 既没有意义又会在本机刷出上百条噪音(CI 上没有
    # 这个目录, 所以它从来不影响 CI 的红绿 —— 加进来只会让本机看得清)。
    ".worktrees",
}

#: `with <某个东西>.get(...)` 这种写法: `requests` 下能用, `curl_cffi` 下抛
#: `TypeError: 'Response' object does not support the context manager protocol`。
#: 三处调用点全包着 `except Exception` → **从不报错, 只是静默什么都拿不到**。
#: 统一走 `core/transport.streamed()`。见 `docs/AGENT_DEVELOPMENT_GUIDE.md` 第 20 条。
_WITH_VERBS = {"get", "head", "post", "put", "patch", "delete", "options", "request", "stream"}


# --------------------------------------------------------------------------
# 基础设施
# --------------------------------------------------------------------------


def _git_toplevel(root):
    """`root` 所在仓库的顶层目录。不是仓库/没有 git -> `None`。"""
    try:
        proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(root),
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return Path(proc.stdout.strip()).resolve()
    except OSError:
        return None


def tracked_files(root):
    """仓库**跟踪**的文件(绝对路径)。

    用 `git ls-files` 而不是走目录: 判据是"仓库里有什么", 不是"我的工作区有什么" ——
    否则一个未跟踪的临时文件就能让门禁红/绿, 而 CI 上根本没有它。

    ⚠️ 但必须先确认 `root` **自己**就是仓库顶层。否则会撞上一种很阴的情形:
    如果 `root` 只是**某个父仓库**里的一个普通目录(本机就是: 项目目录在
    `C:\\Users\\admin` 那个大仓库下面), `git ls-files` 会**成功**返回 —— 只是清单被
    限定在那条前缀下, 于是**一个文件都没有**。门禁于是"一项都没核到", 判据表面全绿。
    所以这里比对 `--show-toplevel`, 不匹配就走目录扫描。
    """
    if _git_toplevel(root) == Path(root).resolve():
        try:
            proc = subprocess.run(["git", "ls-files", "-z"], cwd=str(root),
                                  capture_output=True, timeout=180)
            if proc.returncode == 0:
                names = proc.stdout.decode("utf-8", "surrogateescape").split("\0")
                return [Path(root) / n for n in names if n]
        except (OSError, subprocess.SubprocessError):
            pass
    return _walk_all(root)


def _walk_all(root):
    """磁盘上除 `SKIP_DIRS` 外的所有文件(不管 git 跟不跟踪)。"""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            out.append(Path(dirpath) / f)
    return out


def untracked_sources(root):
    """磁盘上有、但 **git 没跟踪** 的 `.py` 文件(相对路径, 升序)。

    ⚠️ 门禁按 `git ls-files` 扫, 所以**一个新文件只要没 `git add`, 这次就根本没被
    验到** —— 表现为"本机六闸全绿、推上去 CI 红", 而红的正是这个新文件本身。
    (V44 就栽在这上面: 新增的测试文件里有 2 个没用的 import, 本机门禁看不见它。)

    它**不算红**: 临时脚本也会落在这里, 红了就是假红 —— 而假红的下场通常是被
    加进豁免清单。但它必须**看得见**: "没验到"和"验过了没问题"不能是同一个输出。
    """
    tracked = set()
    for p in tracked_files(root):
        try:
            tracked.add(p.relative_to(root).as_posix())
        except ValueError:
            tracked.add(str(p))
    out = []
    for p in _walk_all(root):
        if p.suffix != ".py" or _skipped(p, root):
            continue
        if _rel(p, root) not in tracked:
            out.append(_rel(p, root))
    return sorted(out)


def _skipped(path, root):
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return True
    return any(p in SKIP_DIRS for p in parts)


def _rel(path, root):
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def read_text(path):
    """读文本。

    `newline=None`(默认)走**通用换行**: `\\r\\n` 归一成 `\\n`。这很重要 ——
    否则每一行的末尾都带一个 `\\r`, 正则和 AST 会看到一堆看不见的噪声。
    行尾本身由 `gate_line_endings` 直接读**字节**判, 不靠这里。
    """
    return path.read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------
# 检查 ①  CI 契约(R-CI-1)
# --------------------------------------------------------------------------


try:                                   # pyyaml 是主依赖; 缺失时退化成"这条没核"
    import yaml
except ImportError:                    # pragma: no cover - 只在环境不全时命中
    yaml = None

#: 仓库脚本的**路径形态**。`(?:frontend/)?` 那一段是为了 `frontend/scripts/*.mjs`。
_SCRIPT_RE = r"(?:frontend/)?scripts/[A-Za-z0-9_./-]+\.(?:py|mjs)"


def parse_ci(ci_text):
    """把 ci.yml 解析成 `(所有 run 的命令体, 所有 env 变量名)`。

    ⚠️ **必须解析结构, 不能扫文本**。这条是踩出来的: 我在 ci.yml 的注释里写了一句
    "用 `npm ci` 而不是 `npm install`, 因为……", 文本扫立刻把这条**正确**的配置判红 ——
    而那条注释的意思恰恰是"我们不用它"。文案是给人看的, 判据必须落在结构上
    (同源教训: `scripts/add_site.py` 那条 AST 门禁第一天用正则扫文本就被自己的
    文档字符串判红)。

    返回 `None` 表示**没解析出来** —— 调用方要把它当成"这条检查没核到", 不是"通过"。
    """
    if yaml is None:
        return None
    try:
        data = yaml.safe_load(ci_text)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None

    runs, envs = [], set()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "run" and isinstance(value, str):
                    runs.append(value)
                elif key == "env" and isinstance(value, dict):
                    envs.update(str(k) for k in value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return runs, envs


def npm_script_targets(root):
    """`frontend/package.json` 的 scripts 名 -> 它真正会跑到的仓库脚本。

    `npm run test:task-query` 在 ci.yml 里**看不出**它到底跑哪个文件。所以顺着
    package.json 的 script 体把 `scripts/*.mjs` 找出来, 让"契约"结在**文件**上 ——
    否则 ci.yml 里加一句 `npm run something`, 门禁会以为"什么都没跑", 或者反过来
    把一条真的在跑的脚本判成"没人管"。
    """
    pkg = root / "frontend" / "package.json"
    if not pkg.exists():
        return {}
    try:
        data = json.loads(read_text(pkg))
    except ValueError:
        return {}
    out = {}
    for name, body in (data.get("scripts") or {}).items():
        for m in re.findall(_SCRIPT_RE, str(body)):
            out[str(name)] = m if m.startswith("frontend/") else "frontend/" + m
    return out


def ci_scripts(runs, npm_map=None):
    """`ci.yml` 里**实际被执行到**的仓库脚本。

    `runs` 是 `parse_ci()` 解析出来的命令体列表 —— 只认真正会执行的命令,
    注释里提到某个脚本**不算在跑它**。
    """
    text = "\n".join(runs)
    found = set(re.findall(_SCRIPT_RE, text))
    for name, target in (npm_map or {}).items():
        if re.search(r"npm\s+run\s+%s(?![\w:-])" % re.escape(name), text):
            found.add(target)
    return found


def all_guard_scripts(root):
    """仓库里所有"应该有个说法"的脚本。"""
    out = set()
    for p in sorted((root / "scripts").glob("*.py")):
        out.add("scripts/" + p.name)
    fdir = root / "frontend" / "scripts"
    if fdir.is_dir():
        for p in sorted(fdir.glob("*.mjs")):
            out.add("frontend/scripts/" + p.name)
    return out


def parse_ci_contract(readme_text):
    """解析 README 的「CI 契约」表 -> `{脚本: (在CI跑?, 理由)}`。

    表格式(由 README 保证, 这里是唯一读取方)::

        | 命令 | 在 CI 跑 | 不在 CI 的理由 |
        | --- | --- | --- |
        | `python scripts/gateguard.py` | ✅ | — |

    `在 CI 跑` 只认 `✅`/`❌` 两个符号 —— 别的一律报 `contract-format`。
    "看不太懂就当它跑了吧"正是要防的那种绿。
    """
    lines = readme_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        cells = [c.strip() for c in line.split("|")]
        if len(cells) >= 3 and cells[1] == "命令":
            start = i
            break
    if start is None:
        return None

    out, malformed = {}, []
    for line in lines[start + 2:]:
        if not line.lstrip().startswith("|"):
            break
        cells = [c.strip() for c in line.split("|")][1:-1]
        if len(cells) < 3:
            continue
        command, in_ci, reason = cells[0], cells[1], cells[2]
        scripts = re.findall(_SCRIPT_RE, command)
        if not scripts:
            continue
        if in_ci == "✅":
            flag = True
        elif in_ci == "❌":
            flag = False
        else:
            malformed.append((command, in_ci))
            flag = None
        for s in scripts:
            out[s] = (flag, reason)
    return out, malformed


def gate_ci_contract(root):
    """R-CI-1: 每个脚本要么在 CI 里, 要么在 README 里写明"为什么不在"。"""
    title = "CI 契约(谁在 CI 跑 / 谁只在本地跑, 且必须给理由)"
    problems, rows = [], []
    scripts = all_guard_scripts(root)
    ci_path = root / ".github" / "workflows" / "ci.yml"
    readme_path = root / "README.md"
    checked = 0

    if not ci_path.exists():
        return Gate(title, checked=0,
                    problems=[problem("ci-missing", "找不到 `.github/workflows/ci.yml`")])
    if not readme_path.exists():
        return Gate(title, checked=0,
                    problems=[problem("doc-missing", "找不到 `README.md`")])

    ci_text = read_text(ci_path)
    readme_text = read_text(readme_path)
    parsed = parse_ci(ci_text)
    if parsed is None:
        # 没解析出来 ≠ 通过。这条闸**一项都没核**, 而那正是要防的"假绿"。
        return Gate(title, checked=0, problems=[problem(
            "ci-unparsable",
            "`ci.yml` 解析不出来(pyyaml 缺失, 或 YAML 语法错), 所以 CI 契约**一项都没核**。"
            "这条检查必须落在结构上 —— 扫文本会把注释里「我们不用 npm install」"
            "这类话当成真的在用。)")])
    runs, envs = parsed

    table = parse_ci_contract(readme_text)
    if table is None:
        return Gate(title, checked=0, problems=[problem(
            "contract-missing",
            "README 里找不到「CI 契约」表的表头(`| 命令 | 在 CI 跑 | 不在 CI 的理由 |`)")])
    declared, malformed = table
    actually = ci_scripts(runs, npm_script_targets(root))

    checked += 1
    for command, cell in malformed:
        problems.append(problem(
            "contract-format",
            "「CI 契约」表里 `%s` 的「在 CI 跑」写的是 `%s` —— 只认 `✅` / `❌`。"
            "含糊的格子迟早被读成'大概跑了吧'。" % (command[:60], cell[:20])))

    for s in sorted(scripts):
        checked += 1
        if s not in declared:
            problems.append(problem(
                "contract-missing",
                "`%s` 既不在 CI 里, 也没在 README 的「CI 契约」表里出现 —— "
                "**没有第三态**: 要么接进 ci.yml, 要么写明「只在本地跑 + 为什么」。"
                % s))
            continue
        flag, reason = declared[s]
        in_ci = s in actually
        if flag is True and not in_ci:
            problems.append(problem(
                "contract-lying",
                "README 说 `%s` 在 CI 跑, 但 ci.yml 里找不到它。" % s))
        if flag is False and in_ci:
            problems.append(problem(
                "contract-lying",
                "README 说 `%s` 不在 CI 跑, 但 ci.yml 里**确实**在跑它。" % s))
        if flag is False and reason in ("", "—", "-"):
            problems.append(problem(
                "contract-no-reason",
                "`%s` 标了「不在 CI 跑」却没写理由。没有理由的「暂时不做」，"
                "下一轮就会被当成「不该做」。" % s))
        if flag is True and in_ci:
            rows.append((s, "CI", "✅"))

    # 反方向: ci.yml 里跑了一个仓库里根本不存在的脚本 —— 说明契约表漏了东西
    for s in sorted(actually - scripts):
        checked += 1
        problems.append(problem(
            "contract-stale", "ci.yml 里在跑 `%s`, 但它不在仓库的脚本清单里。" % s))

    # 两条具体的 CI 纪律(都是本项目真实踩过的)
    checked += 1
    if any(re.search(r"(?<![\w-])npm\s+install(?![\w-])", run) for run in runs):
        problems.append(problem(
            "ci-npm-install",
            "ci.yml 的 `run:` 里用的是 `npm install`。仓库里有 `frontend/package-lock.json`, "
            "就该用 `npm ci` —— 它按锁文件装、锁文件与 package.json 不一致时直接红, "
            "而 `npm install` 会**顺手改掉锁文件**把不一致吞掉。"))
    checked += 1
    if "UWC_CI" not in envs:
        problems.append(problem(
            "ci-env-missing",
            "ci.yml 没有设 `UWC_CI=1`。没有它就分不出「这次的跳过是平台门控, 还是"
            "依赖没装」—— R-CI-3 就落不了地。"))

    return Gate(title, checked=checked, problems=problems, rows=rows)


# --------------------------------------------------------------------------
# 检查 ②  文档数字 vs 实测(R-CI-2)
# --------------------------------------------------------------------------


def parse_collected(out):
    """从 `pytest --collect-only -q` 的输出里取出**收集总数**(passed + skipped 的那个数)。

    两条路: 先认 pytest 自己的汇总行, 认不出就数 nodeid。
    **一条都认不出时返回 `None`** —— 不许把"没读出来"当成 0 或 1,
    那会让上层的比较变成一次假绿。
    """
    m = re.search(r"(?<![\d.])(\d+)\s+tests?\s+collected", out)
    if m:
        return int(m.group(1))
    nodeids = [l for l in out.splitlines() if "::" in l]
    if nodeids:
        return len(nodeids)
    return None


def collected_case_count(root):
    """问 pytest 要收集总数(子进程)。

    为什么用子进程而不是 import: 收集会 import 全部 conftest 与 `core.task_manager`,
    后者**一 import 就起后台线程**(本项目第 19 条坑)。放进本进程会污染后面的检查。

    ⚠️ 结果**要缓存**: 一次收集要十几到几十秒(要 import 整个应用 + 1200 多条用例),
    而 `tests/test_gateguard.py` 里有三条用例各自把全部闸在真实仓库上跑一遍、还有一条
    自己再问一次 —— 不缓存就是四五次全量收集, 白等一两分钟。
    缓存的安全边界要说清: 同一次进程生命周期内收集数不会变(它是"这个工作区此刻有多少
    用例"), 而本脚本是跑完就退出的 CLI; 唯一的失真场景是"进程活着的时候有人往 tests/
    里加文件", 那不属于这个脚本的用法。
    """
    return _collected_cached(str(Path(root).resolve()))


@functools.lru_cache(maxsize=8)
def _collected_cached(root_str):
    root = Path(root_str)
    env = dict(os.environ)
    env.pop("PYTEST_ADDOPTS", None)
    args = [sys.executable, "-m", "pytest", "--collect-only", "-q",
            "--no-header", "-p", "no:cacheprovider"]
    try:
        proc = subprocess.run(args, cwd=str(root), capture_output=True,
                              text=True, timeout=900, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return None, "子进程起不来: %s" % e
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    n = parse_collected(out)
    if n is None:
        return None, out.strip()[-1500:]
    return n, None


def expected_checks(root):
    """`scripts/*.py` 里声明的 `EXPECTED_CHECKS = <n>` -> [(路径, n)]。"""
    out = []
    for p in sorted((root / "scripts").glob("*.py")):
        try:
            tree = ast.parse(read_text(p))
        except SyntaxError:
            continue
        for node in tree.body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "EXPECTED_CHECKS"
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, int)):
                out.append((p, node.value.value))
    return out


def gate_doc_counts(root, actual_cases=None):
    """README 里的数字必须等于实测。

    ⚠️ **只查 README**。`docs/AGENT_DEVELOPMENT_GUIDE.md` 里的数字是**版本史**
    ("V33 当时 819 用例"), 拿今天的实测去比是另一种假红 —— 判据要挂在
    "当前口径"这个位置(README)上, 而不是挂在"文档里出现的任何数字"上。
    """
    title = "文档里的数字 vs 实测(README 是唯一的「当前口径」落点)"
    readme_path = root / "README.md"
    if not readme_path.exists():
        return Gate(title, checked=0, problems=[problem("doc-missing", "找不到 README.md")])
    lines = read_text(readme_path).splitlines()
    problems, rows = [], []
    checked = 0

    # ① pytest 收集总数 —— 挂在 `make test` 那一行上
    checked += 1
    case_lines = [l for l in lines if "make test" in l]
    if not case_lines:
        problems.append(problem(
            "doc-missing", "README 里没有 `make test` 那一行 —— 用例数的口径没有落点。"))
    else:
        claimed = set()
        for line in case_lines:
            # 取那一行里**所有**整数当"声称值", 判据是"实测值 ∈ 声称值":
            # 这是个**上界宽松**的判据 —— 多抓几个数只会让它更宽容(它唯一会红的情形是
            # "实测值一个都没被写出来"), 所以不必费劲去精确切分那句话的语法。
            claimed |= {int(n) for n in re.findall(r"(?<!\d)(\d+)(?!\d)", line)}
        if actual_cases is None:
            actual_cases, err = collected_case_count(root)
        else:
            err = None
        if actual_cases is None:
            problems.append(problem(
                "pytest-collect-failed",
                "问不出收集总数, 所以**这一项没核**。pytest 的输出尾部:\n%s" % err))
        else:
            rows.append(("pytest 收集总数", actual_cases, "README 声称 %s" % sorted(claimed)))
            if actual_cases not in claimed:
                problems.append(problem(
                    "doc-count-stale",
                    "本机实测收集 **%d** 条用例, 而 README 的 `make test` 那行只写了 %s。"
                    "用例数变了就得同步 —— 否则下一个读文档的人(或 AI)会拿一个"
                    "过期的数当事实。"
                    % (actual_cases, sorted(claimed) or "（一个数都没有）")))

    # ② 每个声明了 EXPECTED_CHECKS 的脚本, README 那一行必须写同一个数
    for path, n in expected_checks(root):
        checked += 1
        rel = _rel(path, root)
        hits = [l for l in lines if path.name in l]
        if not hits:
            problems.append(problem(
                "doc-missing",
                "`%s` 声明了 `EXPECTED_CHECKS = %d`, 但 README 的命令清单里没有它 —— "
                "这个数没有落点。" % (rel, n)))
            continue
        if not any(re.search(r"(?<!\d)%d(?!\d)\s*项" % n, l) for l in hits):
            got = sorted({int(x) for l in hits
                          for x in re.findall(r"(?<!\d)(\d+)(?!\d)\s*项", l)})
            problems.append(problem(
                "doc-count-stale",
                "`%s` 的 `EXPECTED_CHECKS = %d`, README 里那一行写的是 %s。"
                % (rel, n, got or "（没写几个数）")))
        else:
            rows.append((rel, n, "README 一致"))

    return Gate(title, checked=checked, problems=problems, rows=rows)


# --------------------------------------------------------------------------
# 检查 ③  未用 import(AST)
# --------------------------------------------------------------------------


def _noqa_lines(src):
    return {i + 1 for i, line in enumerate(src.splitlines()) if "noqa" in line}


def _import_lines(tree):
    """所有 import 语句占用的行号。"""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for i in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                out.add(i)
    return out


def gate_unused_imports(root):
    """未用的 import。

    ⚠️ 规则刻意**保守** —— 一条一开始就需要豁免的门禁, 迟早被加到失效(第 18 条)。
    误报的代价不对称: 漏报只是留着一个多余的 import; **误报会让下一个人删掉一个
    在用的 import**, 那才是真坏了。所以要求"这个名字在**整份文件的其余文本里一次
    都不出现**"(连注释/字符串里出现过也算用了), 并且:
      · 有 `__all__`(重导出)或 `TYPE_CHECKING`(条件导入)的文件**整个跳过**
      · 带 `noqa` 的行跳过; `import *` 跳过; `__future__` 跳过
    """
    title = "未用 import(保守规则: 重导出 / 条件导入 的文件不判)"
    problems, rows, skipped = [], [], []
    checked = 0

    for path in sorted(p for p in tracked_files(root)
                       if p.suffix == ".py" and not _skipped(p, root)):
        rel = _rel(path, root)
        src = read_text(path)
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            problems.append(problem("syntax-error", "`%s` 解析失败: %s" % (rel, e)))
            continue
        if any(isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__all__" for t in n.targets)
                for n in tree.body):
            skipped.append(rel + " (有 __all__, 可能在重导出)")
            continue
        if "TYPE_CHECKING" in src:
            skipped.append(rel + " (有 TYPE_CHECKING 条件导入)")
            continue
        checked += 1

        # 判据落在"去掉 import 语句之后的正文"上 —— 见 docstring 里"误报的代价"。
        drop = _import_lines(tree)
        body = "\n".join(line for i, line in enumerate(src.splitlines(), 1)
                         if i not in drop)
        noqa = _noqa_lines(src)

        def unused(name):
            return not re.search(r"(?<![\w.])%s(?![\w])" % re.escape(name), body)

        for node in ast.walk(tree):
            if getattr(node, "lineno", -1) in noqa:
                continue
            if isinstance(node, ast.Import):
                for a in node.names:
                    bound = a.asname or a.name.split(".")[0]
                    if unused(bound):
                        problems.append(problem(
                            "unused-import",
                            "`%s:%d` 导入了 `%s` 但整个文件没有用到它。"
                            % (rel, node.lineno, a.name)))
            elif isinstance(node, ast.ImportFrom):
                if node.module in ("__future__", None):
                    continue
                for a in node.names:
                    if a.name == "*":
                        continue
                    if unused(a.asname or a.name):
                        problems.append(problem(
                            "unused-import",
                            "`%s:%d` 从 `%s` 导入了 `%s` 但没用到。"
                            % (rel, node.lineno, node.module, a.name)))

    for s in skipped:
        rows.append(("跳过", s, ""))

    return Gate(title, checked=checked, problems=problems, rows=rows)


# --------------------------------------------------------------------------
# 检查 ④  代码围栏配平 / ⑤ `with <session>.get()` AST 门禁 / ⑥ 行尾
# --------------------------------------------------------------------------


def gate_fences(root):
    """文档里的代码围栏必须成对。

    只查 README 与 `docs/`, **不查 `.workbuddy/`**(过程笔记, 见 SKIP_DIRS 的说明)。
    """
    title = "代码围栏配平(README 与 docs/)"
    problems = []
    targets = [root / "README.md"] + sorted((root / "docs").rglob("*.md"))
    checked = 0
    for path in targets:
        if not path.exists() or _skipped(path, root):
            continue
        checked += 1
        n = sum(1 for line in read_text(path).splitlines()
                if line.lstrip().startswith("```"))
        if n % 2:
            problems.append(problem(
                "fence-unbalanced",
                "`%s` 里有 %d 个 ``` —— 奇数, 说明有一个没闭合(后面整篇的渲染都会歪)。"
                % (_rel(path, root), n)))
    return Gate(title, checked=checked, problems=problems)


def gate_no_wrapped_response(root):
    """AST 门禁: 不许 `with <某>.get(...)` 包住 HTTP 响应。

    这一条只在**换指纹**的场合发作(`curl_cffi`), 而调用点往往都包着
    `except Exception` → 从不报错。所以它必须由结构上禁止, 而不是靠记得。
    """
    title = "AST: 流式响应不许走 `with`(第 20 条)"
    problems = []
    checked = 0
    for path in sorted(p for p in tracked_files(root)
                       if p.suffix == ".py" and not _skipped(p, root)):
        try:
            tree = ast.parse(read_text(path))
        except SyntaxError:
            continue
        checked += 1
        rel = _rel(path, root)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.With, ast.AsyncWith)):
                continue
            for item in node.items:
                call = item.context_expr
                func = getattr(call, "func", None)
                if isinstance(call, ast.Call) and isinstance(func, ast.Attribute) \
                        and func.attr in _WITH_VERBS:
                    problems.append(problem(
                        "wrapped-response",
                        "`%s:%d` 用 `with …. %s(...)` 包 HTTP 响应。"
                        "`curl_cffi` 的响应**不支持上下文管理器协议**, 换指纹时"
                        "会抛 `TypeError`(而调用点通常包着 `except` → 静默拿不到正文)。"
                        "改用 `core/transport.streamed()` —— 它显式 `close()`, 两种"
                        "传输通用。" % (rel, node.lineno, func.attr)))
    return Gate(title, checked=checked, problems=problems)


def eol_policy(root):
    """从 `.gitattributes` 推出"这个文件的期望行尾"。

    为什么读它而不是各写一份: 一旦两边各有一份规则, 就会出现"git 认为是 LF、
    门禁认为是 CRLF"这种半通状态 —— 那正是本项目反复踩的"同一个东西两个定义"。
    """
    ga = root / ".gitattributes"
    rules = []
    if ga.exists():
        for line in read_text(ga).splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            fields = line.split()
            pattern, attrs = fields[0], fields[1:]
            eol = None
            binary = False
            for a in attrs:
                if a in ("binary", "-text"):
                    # `binary` 是 git 的宏(= -text -diff -merge): 声明了就不该管行尾
                    binary = True
                if a == "text" or a.startswith("text="):
                    eol = eol or "lf"
                if a.startswith("eol="):
                    eol = a.split("=", 1)[1].lower()
            rules.append((pattern, binary, eol))

    def policy(rel):
        binary, eol = False, None
        for pattern, b, e in rules:          # 后写的规则覆盖先写的(git 的语义)
            if _match_attr(pattern, rel):
                binary, eol = b, e
        if binary:
            return "binary"
        return eol or "lf"

    return policy


def _match_attr(pattern, rel):
    if pattern == "*":
        return True
    if "/" not in pattern:
        return fnmatch.fnmatch(Path(rel).name, pattern)
    if pattern.endswith("/**"):
        return rel.startswith(pattern[:-3])
    return fnmatch.fnmatch(rel, pattern)


def gate_line_endings(root):
    """跟踪的文本文件不许出现 CRLF(按 `.gitattributes` 的期望)。

    为什么还要看这个: 加了 `.gitattributes` 之后 `git add` 会把 CRLF **静默归一**,
    于是工作区里的 CRLF 在 `git status` 上**看不见**了 —— 损坏被藏起来, 而不是消失。
    门禁读的是工作区字节, 所以它成了唯一能看见这件事的地方。

    这一条治的是本机反复踩的那个坑: `Path.write_text()` 在 Windows 上默认把每个
    `\\n` 翻成 `\\r\\n`, 一次读写往返就把整份文件换掉(70 行的改动膨胀成 400 行)。
    """
    title = "行尾(`.gitattributes` 说是 LF 的就不许有 CRLF)"
    policy = eol_policy(root)
    problems = []
    checked = 0
    for path in sorted(tracked_files(root)):
        if _skipped(path, root):
            continue
        rel = _rel(path, root)
        if policy(rel) != "lf":
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in data:            # 二进制: 不看行尾
            continue
        checked += 1
        n = data.count(b"\r\n")
        if n:
            lone = data.count(b"\n") - n
            kind = "整份都是 CRLF" if lone == 0 else "**混合**行尾(更危险)"
            problems.append(problem(
                "crlf",
                "`%s` 里有 %d 处 CRLF(%s)。`.gitattributes` 要求 LF。"
                "成因通常是 `Path.write_text(…)` 没带 `newline=\"\"`。"
                % (rel, n, kind)))
    return Gate(title, checked=checked, problems=problems)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

GATES = (
    ("ci-contract", gate_ci_contract),
    ("doc-counts", gate_doc_counts),
    ("unused-imports", gate_unused_imports),
    ("fences", gate_fences),
    ("wrapped-response", gate_no_wrapped_response),
    ("line-endings", gate_line_endings),
)


def main(argv=None):
    ap = argparse.ArgumentParser(description="仓库级门禁: 文档声称 / CI 契约 / 结构规则")
    ap.add_argument("--only", default="", help="只跑某一项(名字见无参数输出)")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()

    print("=" * 74)
    print("仓库门禁: %s" % root)
    print("=" * 74)
    unseen = untracked_sources(root)
    if unseen:
        print("!! %d 个未跟踪的 .py 文件 —— 门禁只扫 git 跟踪的文件, 它们**没被验到**:"
              % len(unseen))
        for r in unseen[:20]:
            print("     " + r)
        if len(unseen) > 20:
            print("     … 还有 %d 个" % (len(unseen) - 20))
        print("   跑门禁前先 `git add` 新文件(本机全绿 / CI 红的常见成因);")
        print("   只是临时脚本的话, 删掉或加进忽略即可 —— 这一条**不计入红绿**。")
        print()
    total = 0
    for name, fn in GATES:
        if args.only and args.only != name:
            continue
        gate = fn(root)
        print("-- [%s] %s" % (name, gate.title))
        for row in gate.rows:
            print("     " + "  ".join(str(x) for x in row))
        problems = gate.effective_problems()
        print("   " + gate.verdict())
        if problems:
            total += len(problems)
            for p in problems:
                print("   !! [%s] %s" % (p.kind, p))
            print()
    print("-" * 74)
    if total:
        print("=> %d 个问题。上面每条都写了「哪里不对 + 怎么改」。" % total)
    else:
        print("=> 全绿: 文档声称与代码实际一致, 结构规则没有违规。")
    print("-" * 74)
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
