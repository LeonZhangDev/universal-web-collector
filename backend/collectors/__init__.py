"""Collector 插件注册表。

新增站点采集器: 在 collectors/ 下建包, 定义 Spider 类(crawl(url) -> list[Resource]),
用 @register("站点名") 注册, 并在本文件 import 该包即可。
无需改动 core/api。

自动识别采集器
==============
用户不该记住"这个 URL 要用哪个采集器"。每个采集器自己声称能处理哪些 URL,
约定成一个类方法::

    class SomeSpider:
        @classmethod
        def match_score(cls, url) -> Optional[int]:
            ...

返回 None = 处理不了; 返回整数 = 能处理, **值越大越优先**。
分数相同时按采集器名的字典序取第一个, 保证同一 URL 每次解析结果一致 ——
可复现比"更聪明"重要, 否则同一个输入今天走 A 明天走 B, 出了问题没法复现。

⚠️ 这与"绝不猜图集 ID"那条铁律并不冲突, 差别在**猜错之后会怎样**:

- 猜错 ID 的后果是静默的 —— 去枚举一个不存在的图集, 该站照样返回
  `200 text/html`, 于是"看起来正常"地采到 0 个资源, 任务还报 success。
  (2026-09-18 真实事故, 见 `gallery_base.parse_gid`。)
- 猜错采集器的后果是**响亮**的 —— 图集采集器会在第一步 `parse_gid` 就失败,
  任务报 failed 并把可接受的 URL 形态列给用户。用户立刻知道要手动选。

所以这里敢从候选里挑一个; ID 解析那里一个字都不能猜。
配套原则: 自动识别的结果必须**回显给用户并可覆盖**, 不能悄悄生效
(见 `POST /collectors/resolve` 与前端的"已识别为 X"提示)。
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


#: 通用采集器的竞争分数。它什么 URL 都能"试着抓", 所以永远参与竞争,
#: 但必须低于**所有**专用采集器的分数, 否则专用采集器永远选不上。
#: 专用采集器的分数在 `collectors/scores.py` 里定义。
from .scores import SCORE_GENERIC  # noqa: F401  (向下兼容旧引用)


def match_collectors(url):
    """所有声称能处理该 URL 的采集器, 按 (分数降序, 名字升序) 排列。

    返回 [(score, name), ...]; 任一采集器的匹配逻辑自己抛异常时当作"处理不了",
    而不是让一次解析失败 —— 匹配是纯 CPU 操作, 没有理由因为某个采集器写错
    而拖垮整条选择链路。
    """
    hits = []
    for name, cls in COLLECTORS.items():
        fn = getattr(cls, "match_score", None)
        if not callable(fn):
            continue
        try:
            score = fn(url)
        except Exception:
            continue
        if score is not None:
            hits.append((int(score), name))
    hits.sort(key=lambda t: (-t[0], t[1]))
    return hits


def resolve_collector(url, fallback="generic"):
    """为 URL 挑一个采集器。

    `fallback`: 无人认领时的兜底(None = 拒绝兜底, 由调用方报错)。
    **连 URL 都不是**的输入建议传 None —— 让它在创建之前就报错,
    而不是 DNS 解析失败之后才报错。

    返回 dict::

        {
          "collector":  最终挑中的采集器名,
          "score":      它的分数,
          "auto":       本次是否是非通用采集器命中(False = 没有专用采集器认领),
          "ambiguous":  是否存在并列的高级候选(True 时应让用户确认),
          "candidates": [(name, score), ...] 全量候选, 供界面回显,
          "reason":     为什么选它(要能原样显示给用户),
        }
    """
    hits = match_collectors(url)
    if not hits:
        if not fallback:
            return {
                "collector": None,
                "score": None,
                "auto": False,
                "ambiguous": False,
                "candidates": [],
                "reason": "没有采集器能处理该输入, 请手动选择采集器",
            }
        name = fallback if fallback in COLLECTORS else "generic"
        return {
            "collector": name,
            "score": None,
            "auto": False,
            "ambiguous": False,
            "candidates": [],
            "reason": "没有采集器认领该 URL, 使用 %r" % name,
        }

    top_score, top = hits[0]
    # 只有**专用**采集器之间的并列才算难决 —— 通用采集器垫底不算候选
    ambiguous = (
        top_score > SCORE_GENERIC
        and len(hits) > 1
        and hits[1][0] == top_score
    )
    if top_score <= SCORE_GENERIC:
        reason = "没有专用采集器认领该 URL, 使用通用采集器 %r" % top
    elif ambiguous:
        others = ", ".join(n for s, n in hits[1:] if s == top_score)
        reason = ("%r 与 %s 都声称能处理该 URL, 已选 %r —— "
                  "若有误请手动切换采集器" % (top, others, top))
    else:
        reason = "已识别为 %r" % top
    return {
        "collector": top,
        "score": top_score,
        "auto": top_score > SCORE_GENERIC,
        "ambiguous": ambiguous,
        "candidates": [{"name": n, "score": s} for s, n in hits],
        "reason": reason,
    }


# 注册内置采集器(导入 spider 模块触发 @register)
from .generic.spider import GenericSpider  # noqa: E402,F401
from .xchina.spider import XChinaSpider  # noqa: E402,F401
from .xchina.gallery import XChinaGallerySpider  # noqa: E402,F401
from .xchina.spider_video import XChinaVideoSpider  # noqa: E402,F401
