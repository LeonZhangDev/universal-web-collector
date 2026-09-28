"""V46: 可选 OCR(图中文字提取), 零强制依赖。

设计纪律:
  * **不引入任何 Python OCR 依赖**(不装 pytesseract / easyocr), 只在运行时探测
    系统里有没有 `tesseract` 可执行文件。有就用, 没有就**整体禁用**(功能安静
    地不可用, 但 UI 会明确标"未启用", 不假装能识别 —— 第 27 条: 规则/能力生效
    要有证据, 没装就是没装, 不能报"识别成功 0 字")。
  * 这是与项目"ffmpeg-only、不引 Pillow"同方向的选择: 能力外置, 内核保持小。
  * OCR 是**重 CPU** 操作, 所以: 绝不自动全量扫(那会是一次没人知道的 CPU 风暴);
    只跑在用户**显式触发**的资源上。

对外只暴露两个纯函数 + 一个能力探测, 不碰数据库(写回交给调用方)。
"""

import shutil
import subprocess

# 能力探测结果缓存: 进程内只探测一次。
_TESSERACT_OK = None


def is_available():
    """系统里有没有可用的 tesseract。None/False 时整个 OCR 能力关闭。"""
    global _TESSERACT_OK
    if _TESSERACT_OK is None:
        exe = shutil.which("tesseract")
        _TESSERACT_OK = bool(exe)
    return _TESSERACT_OK


def ocr_image(path):
    """对一张图片做 OCR, 返回识别文字(多行合并为单串, 空识别返回 \"\")。

    无 tesseract 时返回 None —— 调用方据此区分"识别出来是空"和"根本没能力"。
    ⚠️ 用 `-` 当输出文件 + `stdout` 收文字(tesseract 的标准用法); 失败也返回
    None 而不是抛, 因为 OCR 失败不应中断下载/整理主流程。
    """
    if not is_available():
        return None
    try:
        proc = subprocess.run(
            ["tesseract", path, "stdout", "-l", "eng+chi_sim"],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
