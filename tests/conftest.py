"""全局测试隔离。

我们来过两轮"用例之间隔着磁盘互相说话", 两次的根因都一样: 有些模块会在运行期
往磁盘攒状态, 而测试默认读写的是**用户真实的那份**。

1. CDN 画像(`core/cdn_profile.py`)记住"哪条基址命中过"来给候选探测排序, 落在
   `data/cdn_profile.json`。于是 `test_resolve_base_finds_photos2...` 跑完之后,
   后面每一个用 XCHINA 的用例探测顺序都被改掉 —— 报错只有"请求数 4 != 3",
   完全看不出跟上一个用例有关。**单跑绿、全跑红、重跑又绿**, 最费时间的一类。
2. 数据库与下载目录。这个比画像危险: 里面装的是用户真实的任务记录。任何忘了加
   夹具的用例调一次 `db.create_task`, 用户的任务列表里就多一条假数据, 界面上
   看不出是谁写的。
3. 代理健康(`core/proxy_health.py`)。与 (1) **同型**: 记住"哪条代理最近不行",
   一个用例把某条线打到熔断, 下个用例的候选顺序就变了。它的特殊性在于状态是
   **跨任务全局**的(按代理 URL 分桶, 不按任务), 所以更容易串。

另外两个小 JSON 状态文件现在共用 `core/jsonstore.py` 的四条并发纪律 ——
"读写都进 RLock + replace 退避重试 + 写不进去不假装成功"只有一份实现,
新增状态文件不该再手写一遍(手写一遍 = 少一条纪律 = 又是静默丢一半)。

所以隔离靠**机制**而不靠自觉: 这里对每个用例自动把共享状态指到它自己的临时目录,
写脏了真实目录则整会话判红。清单集中在 `isolation.py`, 新增共享状态只改那一个文件。
"""

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))     # 让 isolation 可 import

import isolation  # noqa: E402

import core.task_manager as _tm  # noqa: E402


# --------------------------------------------------------------------------
# R-CI-3: 跳过项必须**说得清是哪一类**, 且 CI 里只允许平台门控
# --------------------------------------------------------------------------
#
# 这条规矩治的是本项目最贵的一种假绿: **最该跑的那些用例整类只在跳, 而 job 是绿的**。
#
# 2026-09-24 逐位还原过 CI 的 8 条跳过: 其中 7 条是「没装 ffmpeg」(4 条 phash 真解码
# + 3 条 DASH 端到端)。也就是说 —— **CI 从不验证"产物内容是不是好的"**, 而那 7 条
# 恰好是最贵的判据。绿灯给了"它验过了"的错觉, 实际是"它根本没跑"。
#
# 判据不能挂在中文理由上(那是本项目记过的假红: 文案一改, 正确实现被判红), 所以:
#   ① 每条跳过都必须带一个**机器标签**: `[platform:posix]` / `[deps:ffmpeg]` / `[env:make]`
#      没有标签 -> 红。强迫写的人回答"这次跳过属于哪一类", 而不是写一句"环境不支持"。
#   ② `[platform:X]` 里的 X 是**这条用例需要的平台**(不是"我现在在哪个平台"),
#      所以判据是 `X != 当前平台` 才算合理跳过; `X == 当前平台` 却仍在跳 -> 红,
#      那说明门控条件写反了(在自己需要的平台上把自己跳掉了)。
#   ③ `[deps:*]` / `[env:*]` -> 在 CI(`UWC_CI=1`)里**一律不许出现**。
#      CI 只有两个选择: 把依赖装上, 或者承认这个 job 是 partial 的 —— 而现在只允许前者。
#
# ⚠️ ② 的这一版是**改过一次**的, 前一次的判据是"必须当前平台确实是它"。那一条
#    在本仓里**永远不可能两边都绿**: 仓库同时有"只在 POSIX 跑"和"只在 Windows 跑"
#    的用例, 于是无论跑在哪个平台上, 总有一类跳过的标签等于当前平台 -> 必然红。
#    更值得记的是它**当时没红**: 那轮只跑了 `test_skip_policy.py`, 而那几条双向
#    用例是照**实现**写的(实现说"标签要等于当前平台", 用例就断言这一条), 于是
#    "实现与意图一致地错"通过了自检。暴露它的是**端到端**的那次全量 —— 真实站点
#    上真的跳了, 标签真的不等于当前平台, 钩子真的报了红。
#    这就是 F 类("通过但理由已经不对")的现场, 也是 ⑦ 那条心法的用法: 判据要能被
#    **整条链路**跑到, 光有双向单测不算 —— 单测可以忠实地编码一个错的意图。
#    反过来说, `[platform:X]` 的语义必须**只有一个方向**, 否则同一条标签在两个
#    平台上会各有一次被判红。那个证明被写成了用例:
#    `tests/test_skip_policy.py::test_the_platform_tag_is_satisfiable_on_both_platforms`
#
# 与 `scripts/gateguard.py` 是一条链: 那边管 ci.yml 有没有装依赖, 这边管"装了之后
# 是不是真的不跳了"。

_SKIP_TAG = re.compile(r"\[(platform|deps|env):([a-z0-9_.+-]+)\]")

_SKIPPED = []           # [(nodeid, reason)]
_SKIP_PROBLEMS = []


def _current_platform():
    return "windows" if os.name == "nt" else "posix"


def _skip_reason(report):
    """跳过的理由。`skipif` 的 longrepr 是 `(file, lineno, reason)` 三元组。"""
    lr = getattr(report, "longrepr", None)
    if isinstance(lr, tuple) and len(lr) == 3:
        return str(lr[2])
    return str(lr)


def pytest_runtest_logreport(report):
    if report.skipped:
        _SKIPPED.append((report.nodeid, _skip_reason(report)))


def _skip_problems(skips=None, on_ci=None):
    """把跳过清单判成"有没有问题"。纯函数, 所以可以被用例直接喂假数据。"""
    skips = _SKIPPED if skips is None else skips
    on_ci = bool(os.getenv("UWC_CI")) if on_ci is None else on_ci
    problems = []
    for nodeid, reason in skips:
        where = nodeid.split("::", 1)[-1] if "::" in nodeid else nodeid
        m = _SKIP_TAG.search(reason or "")
        if not m:
            problems.append(
                "%s\n      跳过理由 %r 里没有机器标签。加一个 `[platform:…]` / "
                "`[deps:…]` / `[env:…]` —— 「运气不好跳过了」与「这类用例本来就不该跑」"
                "必须是两个结论, 而现在看不出来是哪一个。" % (where, (reason or "")[:80]))
            continue
        category, name = m.group(1), m.group(2)
        if category == "platform":
            # 标的是"这条用例**需要**哪个平台"。所以合理的跳过必然发生在**别的**
            # 平台上; 如果当前平台正是它要的那个却仍然跳了, 门控条件就是反的。
            if name == _current_platform():
                problems.append(
                    "%s\n      标了 `[platform:%s]`(这条用例需要 %s), 而这次就**跑在 %s 上**"
                    "却仍然跳过了 —— 门控条件写反了?" % (where, name, name, name))
        elif on_ci:
            problems.append(
                "%s\n      因为 `[%s:%s]` 跳过了, 而这跑在 CI 上。CI 里**只允许平台门控**"
                "的跳过: 要么在 ci.yml 里把 %s 装上(见 README「CI 契约」), 要么这个"
                "job 就得承认自己是 partial。" % (where, category, name, name))
    return problems


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    _SKIP_PROBLEMS[:] = _skip_problems()
    if _SKIP_PROBLEMS and not session.exitstatus:
        session.exitstatus = 1


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if not _SKIP_PROBLEMS:
        return
    terminalreporter.write_sep("=", "跳过项策略 (R-CI-3) 不通过")
    for p in _SKIP_PROBLEMS:
        terminalreporter.write_line("  !! " + p)
    terminalreporter.write_line(
        "  规则见 README 的「CI 契约」: 跳过必须带 [platform:*] / [deps:*] / [env:*],"
        "且 CI 里只允许平台门控的跳过。")


@pytest.fixture(autouse=True)
def _muzzle_import_time_singleton():
    """导入期就存在的那只 TaskManager 单例, 在测试里必须**闭嘴**。

    `backend/core/task_manager.py` 末尾有一句 `task_manager = TaskManager()` ——
    生产环境需要它(api 层直接用它提交任务, main.py 的 lifespan 靠它)。但只要有人
    import 了这个模块, 它的**两个后台线程就活了**, 而且它们干活时读的是
    **当前的 `database.DB_PATH`** —— 测试的库是每条用例现换的。于是:

      · 调度线程扫当前的库, 把用例刚启用的订阅源"顺手跑掉"并回写 `next_run`
        (症状: `due_watches()` 刚从 0 变 1, 一转眼又变回 0)
      · 用例里的活动任务被它当成自己的, 在**单例**的 `_active` 里留下条目; 之后
        别条用例的看门狗问 `_owned_by_any_manager(tid)` 得到 True, 就不敢收那个
        tid 的任务 —— 而 `tid` 在小库上从 1 重新数, **跨用例撞号**
        (症状: 心跳 99999 秒没跳了, 却判不出死活)

    症状全是"**单跑绿、全跑红、重跑又绿**", 与 conftest 顶部记的那两轮同型 ——
    只是这次的污染源不是磁盘上的状态文件, 而是**一条进程内线程**加**一个跨用例
    复用的 dict**。所以隔离清单管不到它: `isolation.py` 登记的是路径, 不是线程。

    ⚠️ 只停**后台循环**, 不动方法本身 —— 有用例直接调
    `tm.task_manager._final_status` / `.submit_resource`, 那些是同步调用, 与循环无关。
    另外把它 `_active` 的增删在用例结束后抹平, 免得下一个用例又撞号。
    """
    mgr = _tm.task_manager
    mgr._watchdog_stop.set()
    mgr._scheduler_stop.set()
    with mgr._active_lock:
        before = dict(mgr._active)
    yield
    with mgr._active_lock:
        mgr._active.clear()
        mgr._active.update(before)


@pytest.fixture(autouse=True)
def _isolate_shared_state(tmp_path, monkeypatch):
    """把 CDN 画像 / 代理健康 / 数据库 / 下载目录 / 浏览器态都指到本次用例的临时目录。

    CDN 画像与代理健康用环境变量而不是 setattr —— 它们是**按 env 读取**设计的
    (支持 `off`), 与 path 字段走的不是一套机制。

    ⚠️ 代理健康必须隔离, 理由与 CDN 画像同型: 一个用例把某条代理打到熔断, 下个
    用例的 `pick()` 顺序就变了 —— 症状是"另一个用例莫名其妙挑中了别的代理",
    与真实原因隔了两层, 且单跑绿、全跑红。
    """
    monkeypatch.setenv("UWC_CDN_PROFILE", str(tmp_path / "cdn_profile.json"))
    monkeypatch.setenv("UWC_PROXY_HEALTH", str(tmp_path / "proxy_health.json"))
    isolation.isolate(monkeypatch, tmp_path / "state")
    yield


@pytest.fixture(scope="session", autouse=True)
def _no_writes_to_real_dirs():
    """整轮跑完, 用户真实的库 / 下载目录必须一个字节都没动。

    ⚠️ 光有隔离还不够: 隔离清单可能漏登记新模块, 那时测试照样写真目录而且不报错。
    这道守卫把"漏登记"从**静默**变成红 —— 上面 autouse 的夹具保证正常情况下它
    永远绿, 红了就说明隔离失效, 而不是某个用例写错了。

    这里同时装上**即时守卫**: 一旦真要打开真实库, 当场带调用栈失败。指纹比对
    只能事后发现"变了", 而 `_migrate` 幂等 —— 加一列之后真实库就永久变绿,
    同型的错再看不出第二次。两道闸各管一段, 缺一个都会漏。

    例外: 确实要对着真实环境跑时 ``UWC_TEST_ALLOW_REAL_WRITE=1 pytest``。
    """
    if os.getenv("UWC_TEST_ALLOW_REAL_WRITE"):
        yield
        return
    isolation.install_real_db_guard()
    before = isolation.snapshot_real()
    yield
    after = isolation.snapshot_real()
    changed = isolation.changed_keys(before, after)
    assert not changed, (
        "测试期间修改了用户的真实目录: " + ", ".join(changed)
        + "\n隔离清单可能漏了新模块 —— 去 tests/isolation.py 登记, 别在用例里临时打补丁。"
    )
