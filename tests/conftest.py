"""全局测试隔离。

目前只有一件事, 但它是个**隐式的污染源**, 值得单独说明。

站点 CDN 画像(`core/cdn_profile.py`)会记住"哪条基址命中过"来给候选探测排序。
写那个模块时它落在真实数据库旁边(`data/cdn_profile.json`), 于是测试一跑:

- `test_resolve_base_finds_photos2_when_photos_misses` 记下 photos2 命中一次;
- 之后每一个用 XCHINA 的用例, 候选顺序都被改成"photos2 优先";
- `test_discover_no_extra_request_on_happy_path`(相册在 photos)于是多探一次,
  断言失败, 而报错信息里只有"请求数 4 != 3", 完全看不出跟上一个用例有关。

这类"用例之间靠磁盘文件偷偷通信"的失败最费时间: 单跑绿、全跑红、重跑又绿。
所以在这里统一把画像指到每个用例自己的临时目录 —— 隔离靠机制, 不靠自觉。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))


@pytest.fixture(autouse=True)
def _isolate_cdn_profile(tmp_path, monkeypatch):
    """把 CDN 画像文件指到本次用例的临时目录, 用例之间互不可见。"""
    monkeypatch.setenv("UWC_CDN_PROFILE", str(tmp_path / "cdn_profile.json"))
    yield
