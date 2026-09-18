"""Collector 插件注册表。

新增站点采集器: 在 collectors/ 下建包, 定义 Spider 类(crawl(url) -> list[Resource]),
用 @register("站点名") 注册, 并在本文件 import 该包即可。
无需改动 core/api。
"""

COLLECTORS = {}


def register(name):
    def deco(cls):
        COLLECTORS[name] = cls
        return cls

    return deco


def get_collector(name):
    try:
        return COLLECTORS[name]()
    except KeyError:
        raise ValueError(f"unknown collector: {name}")


# 注册内置采集器(导入 spider 模块触发 @register)
from .generic.spider import GenericSpider  # noqa: E402,F401
from .xchina.spider import XChinaSpider  # noqa: E402,F401
from .xchina.gallery import XChinaGallerySpider  # noqa: E402,F401
