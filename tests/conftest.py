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

所以隔离靠**机制**而不靠自觉: 这里对每个用例自动把共享状态指到它自己的临时目录,
写脏了真实目录则整会话判红。清单集中在 `isolation.py`, 新增共享状态只改那一个文件。
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))     # 让 isolation 可 import

import isolation  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_shared_state(tmp_path, monkeypatch):
    """把 CDN 画像 / 数据库 / 下载目录 / 浏览器态都指到本次用例的临时目录。

    CDN 画像用环境变量而不是 setattr —— 它是**按 env 读取**设计的(支持 `off`),
    与 path 字段走的不是一套机制。
    """
    monkeypatch.setenv("UWC_CDN_PROFILE", str(tmp_path / "cdn_profile.json"))
    isolation.isolate(monkeypatch, tmp_path / "state")
    yield


@pytest.fixture(scope="session", autouse=True)
def _no_writes_to_real_dirs():
    """整轮跑完, 用户真实的库 / 下载目录必须一个字节都没动。

    ⚠️ 光有隔离还不够: 隔离清单可能漏登记新模块, 那时测试照样写真目录而且不报错。
    这道守卫把"漏登记"从**静默**变成红 —— 上面 autouse 的夹具保证正常情况下它
    永远绿, 红了就说明隔离失效, 而不是某个用例写错了。

    例外: 确实要对着真实环境跑时 ``UWC_TEST_ALLOW_REAL_WRITE=1 pytest``。
    """
    if os.getenv("UWC_TEST_ALLOW_REAL_WRITE"):
        yield
        return
    before = isolation.snapshot_real()
    yield
    after = isolation.snapshot_real()
    changed = isolation.changed_keys(before, after)
    assert not changed, (
        "测试期间修改了用户的真实目录: " + ", ".join(changed)
        + "\n隔离清单可能漏了新模块 —— 去 tests/isolation.py 登记, 别在用例里临时打补丁。"
    )
