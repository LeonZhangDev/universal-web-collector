"""把资源库里的东西打包成一份**可带走**的 zip(带 manifest)。

为什么打包要带 manifest
=======================
打包的目的是"把采集到的东西交出去/带走", 而一堆脱离了库的文件是**没有来源
信息**的: 三个月后没人知道 `00001.jpg` 是从哪个地址来的、属于哪个任务。
manifest.json 就是把这条线索一起带走 —— 它是 sidecar(`core/manifest.py`)在
"导出物"这一侧的对应物。

⚠️ 文件**不重新压缩**(ZIP_STORED): 库里绝大多数是 jpg / mp4, 它们本身就是
压缩过的, 再压一遍只是白烧 CPU, 体积几乎不变。

上限与跳过都要计数(第 27 条)
============================
`max_items` 超了**报错而不是截断**: 静默只导出前 500 张, 用户会以为导全了。
磁盘上已经不在的文件记进 `skipped` —— 不记的话"导出 480 个"和"库里有 500 个"
对不上, 而没人看得出少的那 20 个去哪了。
"""

import json
import time
import zipfile
from pathlib import Path

from core import database as db
from core.config import settings

DEFAULT_MAX_ITEMS = 500


def export(album=None, ids=None, out_dir=None, max_items=DEFAULT_MAX_ITEMS):
    """打包一个相册(或一组指定资源)。返回结果的字典。

    `album` 与 `ids` 至少要给一个: 两个都不给就是"把整个库打包", 那通常不是
    用户点这一下时想要的东西(而且会非常大)。
    """
    wanted = [int(i) for i in (ids or []) if str(i).strip().lstrip("-").isdigit()]
    if not album and not wanted:
        raise ValueError("album or ids is required")

    join = ("SELECT r.id, r.url, r.local_path, r.filename, r.size, r.hash, "
            "r.type, t.name AS album FROM resources r "
            "LEFT JOIN tasks t ON t.id = r.task_id ")
    if wanted:
        marks = ",".join("?" * len(wanted))
        rows = db.query(join + f"WHERE r.id IN ({marks}) AND r.status='done'",
                        tuple(wanted))
    else:
        rows = db.query(join + "WHERE r.status='done' AND t.name=? "
                        "ORDER BY r.id", (album,))

    limit = int(max_items or DEFAULT_MAX_ITEMS)
    if len(rows) > limit:
        raise ValueError(f"too many items ({len(rows)} > {limit}); "
                         "narrow the album or raise max_items")

    base = Path(out_dir) if out_dir else Path(settings.download_dir) / "_meta" / "exports"
    base.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    zip_path = base / f"export-{stamp}.zip"

    # zip 内的重名要自己消解: layout.claim 管的是**磁盘布局**, 而这里是压缩包
    # 内部的命名(同名条目解压时会互相覆盖, 用户只会看到"少了一个")。
    used = {}
    items, skipped, written = [], [], 0

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for row in rows:
            src = Path(row["local_path"] or "")
            if not src.exists():
                skipped.append({"id": row["id"], "reason": "missing"})
                continue
            name = row["filename"] or src.name
            if name in used:
                used[name] += 1
                stem, suffix = Path(name).stem, Path(name).suffix
                name = f"{stem}({used[name]}){suffix}"
            else:
                used[name] = 1
            zf.write(str(src), name)
            written += 1
            items.append({
                "name": name,
                "url": row["url"],
                "size": row["size"],
                "hash": row["hash"],
                "album": row["album"],
                "type": row["type"],
            })

        manifest = {
            "exported_at": time.time(),
            "album": album,
            "count": written,
            "skipped": skipped,
            "items": items,
        }
        zf.writestr("manifest.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2))

    return {
        "path": str(zip_path),
        "name": zip_path.name,
        "count": written,
        "size": zip_path.stat().st_size,
        "skipped": skipped,
        "checked": len(rows),
    }
