"""输出命名模板: 变量渲染与安全兜底。

模板由用户在 UI 里手写, 因此这里的重点不是"渲染得漂亮", 而是
**怎么写都不会把文件写到任务目录外面去**。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from core.naming import (  # noqa: E402
    build_context,
    registered_domain,
    render,
    safe_relative,
    site_of,
)

GALLERY_URL = "https://img.xchina.io/photos/6aa113208a506/00046_600x0.webp"


def _ctx(url=GALLERY_URL, seq=1, album=None):
    return build_context(7, url, "image", seq, album=album)


def test_default_template_keeps_url_tail():
    ctx = _ctx()
    assert render("{name}", ctx, GALLERY_URL, "image") == "00046_600x0.webp"


def test_album_template_builds_subdirectory():
    ctx = _ctx(seq=7, album="6aa113208a506")
    out = render("{album}/{seq4}.{ext}", ctx, GALLERY_URL, "image")
    assert out == "6aa113208a506/0007.webp"


def test_context_variables():
    ctx = _ctx(seq=42, album="gid01")
    assert ctx["task_id"] == "7"
    assert ctx["seq"] == "42" and ctx["seq4"] == "0042"
    assert ctx["stem"] == "00046_600x0" and ctx["ext"] == "webp"
    assert ctx["site"] == "xchina"
    assert ctx["host"] == "img.xchina.io"
    assert ctx["album"] == "gid01"


def test_album_falls_back_to_site():
    ctx = _ctx(album=None)
    assert ctx["album"] == "xchina"


def test_path_traversal_is_rejected():
    """`..` 必须被拒, 否则模板能把文件写到任务目录之外。"""
    ctx = _ctx()
    out = render("../../etc/passwd", ctx, GALLERY_URL, "image")
    assert ".." not in out
    assert out == "00046_600x0.webp"  # 回退到默认命名


def test_absolute_windows_path_is_rejected():
    ctx = _ctx()
    out = render("C:/Windows/System32/drivers/hosts", ctx, GALLERY_URL, "image")
    assert not out.lower().startswith(("c:", "/"))
    assert out == "00046_600x0.webp"


def test_unsafe_characters_are_scrubbed():
    ctx = _ctx(album='bad:name*?"<>|')
    out = render("{album}/{name}", ctx, GALLERY_URL, "image")
    assert all(c not in out for c in ':*?"<>|')


def test_unknown_variable_is_left_visible():
    """未知变量保留成 {foo}, 让用户立刻看出模板写错了, 而不是静默吞掉。"""
    ctx = _ctx()
    out = render("{foo}", ctx, GALLERY_URL, "image")
    assert out == "{foo}"


def test_empty_template_falls_back_to_name():
    ctx = _ctx()
    assert render("", ctx, GALLERY_URL, "image") == "00046_600x0.webp"
    assert render(None, ctx, GALLERY_URL, "image") == "00046_600x0.webp"


def test_url_without_extension_gets_fallback_name():
    url = "https://cdn.example.com/stream/playlist"
    ctx = build_context(1, url, "video", 1)
    out = render("{name}", ctx, url, "video")
    # 没有扩展名时用 sha1 片段 + 类型默认后缀, 而不是把 "playlist" 当文件名
    assert out.endswith(".mp4") and out != "playlist"


def test_safe_relative_rejects_junk():
    assert safe_relative("../../x") is None
    assert safe_relative("/abs/path") is None
    assert safe_relative("https://evil.com/a") is None
    assert safe_relative("") is None
    assert safe_relative("a/b/c.webp") == "a/b/c.webp"


def test_safe_relative_collapses_empty_segments():
    assert safe_relative("a//b/") == "a/b"
    assert safe_relative("///") is None


def test_registered_domain_rules():
    assert registered_domain("img.xchina.io") == "xchina.io"
    assert registered_domain("example.com") == "example.com"
    assert registered_domain("a.b.c.example.co.jp") == "example.co.jp"
    assert registered_domain("localhost") == "localhost"


def test_site_of():
    assert site_of("https://img.xchina.io/a/b.jpg") == "xchina"
    assert site_of("https://example.com/x") == "example"
