"""真实站点预览实测: 只发现、不下载。

用法(需在项目根目录执行, 让它能 import backend):
    python scripts/preview_probe.py <相册页URL或图集ID> [media] [album_title]
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from collectors import get_collector  # noqa: E402


def main():
    url = sys.argv[1]
    media = sys.argv[2] if len(sys.argv) > 2 else "auto"
    title = sys.argv[3] if len(sys.argv) > 3 else "clean"

    spider = get_collector("xchina_gallery")
    logs = []
    data = spider.preview(url, {"media": media, "album_title": title},
                          log=logs.append, max_items=4)

    print("=== LOGS ===")
    for line in logs:
        print(" ", line)
    print("=== PREVIEW ===")
    show = {k: v for k, v in data.items() if k != "video_items"}
    print(json.dumps(show, ensure_ascii=False, indent=1))
    print("video_items:", json.dumps(data["video_items"], ensure_ascii=False))
    mb = data["video_bytes"] / 1024 / 1024
    print(f"\n>>> 目录名 {data['group']!r} | {data['photos']} 图 / "
          f"{data['videos']} 视频 / 视频合计 {mb:.1f}MB | "
          f"来自页面={data['page']} 抽样={data['sampled']}")


if __name__ == "__main__":
    main()
