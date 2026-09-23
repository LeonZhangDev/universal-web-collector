"""取消信号穿透的**静态门禁**。

⚠️ 为什么需要一条静态用例, 光靠测试点停止不够: `except Exception` 会吞掉
`TaskCancelled`, 而吞掉之后的症状是"点了停止没反应"或"停止后又去续传一个注定
被放弃的文件" —— 这种问题**只有真的卡住那几秒才知道**, 而且每次加新的 try
都可能重新引入。项目里为此手写了 8 处 `except TaskCancelled: raise`, 加没加全
靠人记得。这条用例把"记得加"变成"忘了就红"。

判定规则: 在一个 try 块里(不含嵌套 try, 那些由它们自己的 handler 负责)
调用了会抛取消信号的入口, 那么在遇到 `except Exception` 之前必须先有
`except TaskCancelled`。
"""
import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
SCAN_DIRS = ("core", "collectors", "downloaders")

#: 这些调用可能抛出 TaskCancelled(直接抛, 或经由下载/采集链路抛出)
CANCEL_SOURCES = {
    "_check_cancel",        # 直接抛, 取消检查点
    "progress_cb",          # 下载过程中的心跳回调, 内部会 _check_cancel
    "download",             # downloader.download(...)
    "stream_download",
    "_stream_one",
    "download_with_mirrors",
    "crawl",
    "discover",
    "_crawl",
    "_download_all",
    "_download_one",
    "_download_m3u8",       # 视频链路: download -> _download_m3u8 -> _fetch_segments
    "_download_file",
    "_fetch_segments",
    "_fetch_one_segment",   # 单片下载: 重试与限速里都会检查取消
    "_ffmpeg_pull",         # ffmpeg 拉流的子进程监控里会检查取消
}


def _targets():
    for d in SCAN_DIRS:
        yield from sorted((BACKEND / d).rglob("*.py"))


def _handler_names(handler):
    """这个 except 子句捕获的类型名集合。"""
    t = handler.type
    if t is None:
        return {"BaseException"}          # 裸 except: 什么都吞
    if isinstance(t, ast.Tuple):
        return {e.id for e in t.elts if isinstance(e, ast.Name)}
    if isinstance(t, ast.Name):
        return {t.id}
    if isinstance(t, ast.Attribute):
        return {t.attr}
    return set()


def _walk_without_nested_try(node):
    """遍历子树但不进入嵌套的 try —— 嵌套 try 有自己的 handler 负责。"""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.Try):
            continue
        yield child
        yield from _walk_without_nested_try(child)


def _cancel_source_in(try_node):
    """这个 try 块里是否调用了会抛取消信号的入口, 返回其名字(没有则 None)。"""
    for n in _walk_without_nested_try(try_node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        if name in CANCEL_SOURCES:
            return name
    return None


def _guarded_before_broad(try_node):
    """在遇到 `except Exception` 之前, 是否先拦下了 TaskCancelled。"""
    for h in try_node.handlers:
        names = _handler_names(h)
        if "TaskCancelled" in names:
            return True
        if names & {"Exception", "BaseException"}:
            return False
    return True     # 根本没有宽泛捕获, 异常会照原样往上抛


def test_cancel_signal_is_not_swallowed():
    offenders = []
    for path in _targets():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            src = _cancel_source_in(node)
            if src and not _guarded_before_broad(node):
                offenders.append(
                    f"  {path.relative_to(BACKEND)}:{node.lineno}: "
                    f"try 里调用了 {src}(), 但 except Exception 之前没有拦下 TaskCancelled"
                )
    assert not offenders, (
        "以下位置会吞掉取消信号(用户点停止后 worker 不会立刻退出):\n"
        + "\n".join(offenders)
    )


def test_guard_actually_sees_a_violation():
    """门禁自身的回归: 故意写一段违规代码, 必须被抓出来。

    ⚠️ 少了这条, 扫描器写错了(比如永远返回"没问题")会安静地一直绿下去。
    """
    bad = ast.parse(
        "try:\n"
        "    _check_cancel(1)\n"
        "except Exception:\n"
        "    pass\n"
    )
    node = next(n for n in ast.walk(bad) if isinstance(n, ast.Try))
    assert _cancel_source_in(node) == "_check_cancel"
    assert not _guarded_before_broad(node)

    good = ast.parse(
        "try:\n"
        "    _check_cancel(1)\n"
        "except TaskCancelled:\n"
        "    raise\n"
        "except Exception:\n"
        "    pass\n"
    )
    node = next(n for n in ast.walk(good) if isinstance(n, ast.Try))
    assert _guarded_before_broad(node)
