from collectors.parsers import classify, looks_like_resource
from downloaders.text import extract_text


def test_classify_by_extension():
    assert classify("https://a.com/x.mp4") == "video"
    assert classify("https://a.com/x.M3U8?token=1") == "video"
    assert classify("https://a.com/x.jpg") == "image"
    assert classify("https://a.com/song.mp3") == "audio"
    assert classify("https://a.com/doc.pdf") == "doc"
    assert classify("https://a.com/sheet.xlsx") == "doc"


def test_classify_by_content_type():
    assert classify("https://a.com/get?id=1", "audio/mpeg") == "audio"
    assert classify("https://a.com/get?id=1", "application/pdf") == "doc"
    assert classify("https://a.com/get?id=1", "video/mp4") == "video"
    assert classify("https://a.com/get?id=1", "image/webp") == "image"


def test_looks_like_resource_new_types():
    assert looks_like_resource("https://a.com/a.mp3")
    assert looks_like_resource("https://a.com/a.pdf")
    assert not looks_like_resource("https://a.com/page.html")


def test_extract_text():
    html = """
    <html><head><title>测试页面</title><style>.x{color:red}</style></head>
    <body><script>var a=1;</script><p>第一段</p><div>第二段</div></body></html>
    """
    title, text = extract_text(html)
    assert title == "测试页面"
    assert "第一段" in text
    assert "第二段" in text
    assert "var a=1" not in text
    assert "color:red" not in text
