"""目录选择器后端: /fs/browse 的「此电脑」层级与手输路径容错。

判据重点:

1. **盘符根的上一级必须通到「此电脑」**(A1)。修复前 `Path("C:\\").parent`
   还是它自己, 前端"上级"按钮到此为止 —— 用户从 C 盘**永远点不到 D 盘**,
   这不是"功能没做"而是"做了条死路"。
2. **裸盘符 "D:" 必须补成盘根**(A2)。Windows 语义里 "D:" 是"该盘的当前目录",
   用户想去的却是盘根; 不补反斜杠就会跳到一个莫名的旧目录。
3. **跨平台不断言对方平台的行为**: 同一条用例在 nt/posix 各测各的,
   不用 skipif —— 保证两个平台的收集数一致, 门禁文档数字好维护。
"""

import os
from pathlib import Path

import pytest
from fastapi import HTTPException

from api import tasks as T


def test_drives_view_lists_real_roots():
    data = T.browse_fs(T.DRIVES_VIEW)
    assert data["cwd"] == ""
    assert data["parent"] is None
    if os.name == "nt":
        assert data["entries"], "Windows 至少有 C:"
        assert all(Path(e["path"]).is_dir() for e in data["entries"])
        assert all(e["path"].endswith(":\\") for e in data["entries"])
    else:
        assert data["entries"] == [{"name": "/", "path": "/"}]


def test_browse_lists_subdirs_only_and_hides_dotdirs(tmp_path):
    t = tmp_path.resolve()
    (t / "sub").mkdir()
    (t / ".hidden").mkdir()
    (t / "f.txt").write_text("x")
    data = T.browse_fs(str(t))
    assert data["cwd"] == str(t)
    assert [e["name"] for e in data["entries"]] == ["sub"]
    assert data["parent"] == str(t.parent)


def test_missing_path_raises_404(tmp_path):
    with pytest.raises(HTTPException) as ei:
        T.browse_fs(str(tmp_path / "nope"))
    assert ei.value.status_code == 404


def test_drive_root_parent_is_drives_view_and_bare_letter_normalised():
    if os.name == "nt":
        # A1: 盘根上级 = 此电脑(虚拟层级), 不再是死路
        assert T.browse_fs("C:\\")["parent"] == T.DRIVES_VIEW
        # A2: 裸 "C:" 补成 "C:\"(长度3, 以反斜杠结尾), 而非该盘当前目录
        bare = T.browse_fs("C:")
        assert len(bare["cwd"]) == 3 and bare["cwd"].endswith("\\")
    else:
        # POSIX 没有盘符层级: 根的上级就是 None
        assert T.browse_fs("/")["parent"] is None
