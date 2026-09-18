"""任务产出清单 manifest.json。

每个任务跑完(或中途取消/失败)都在输出目录写一份 manifest.json, 回答三个
日常必然被问到的问题:

    这个文件原来在哪 -> source_url / resolved_url
    它是不是完整     -> sha256 / size
    为什么没下载     -> status / note

它是**面向用户的交付物**, 不是内部状态: 内部状态以 SQLite 为准(DB 可能被
清理、换台机器也拿不到)。所以这里用宽松写法: 拿不到的字段
写 null, 整个写出失败也不影响任务本身(只记一条 warn 日志)。

注意 file 字段是**相对 manifest 所在目录**的相对路径 —— 目录整体搬家后仍然
可解析, 不像绝对路径那样换个盘/换台机器就全失效。
"""

import json
import time
from pathlib import Path

from core.config import settings

MANIFEST_NAME = "manifest.json"
VERSION = 1


def _rel_path(out_dir, local_path):
    """把绝对路径转成相对输出目录的 POSIX 路径; 不在目录内时返回 None。"""
    if not local_path:
        return None
    try:
        base = Path(out_dir).resolve()
        rel = Path(local_path).resolve().relative_to(base)
    except (ValueError, OSError, TypeError):
        return None
    return rel.as_posix()


def build_manifest(task, resources, out_dir, stats=None, error=None):
    """生成 manifest 数据结构。

    task/resources 均为 sqlite3.Row —— 只支持下标取值, 不支持 .get()。
    """
    items = []
    for idx, r in enumerate(resources, 1):
        local = r["local_path"] if "local_path" in r.keys() else None
        items.append(
            {
                "id": r["id"],
                "seq": idx,
                "type": r["type"],
                "source_url": r["url"],
                # 实际落地的 URL: 主 URL 失败切到备用下载点时与 source_url 不同
                "resolved_url": _field(r, "resolved_url"),
                # 采集器建议的文件名(相对输出目录), 未命名时为 null
                "planned_name": _field(r, "filename"),
                "file": _rel_path(out_dir, local),
                "size": _field(r, "size"),
                "sha256": _field(r, "hash"),
                "content_type": _field(r, "content_type"),
                "status": r["status"],
                "note": _field(r, "note"),
            }
        )

    return {
        "version": VERSION,
        "task_id": task["id"],
        "url": task["url"],
        "collector": task["collector"],
        "status": task["status"],
        "created_time": task["created_time"],
        "finished_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "retry_count": task["retry_count"] if "retry_count" in task.keys() else 0,
        "options": _json_field(task, "options"),
        "download_root": str(Path(out_dir).resolve()),
        "counts": stats or {},
        "error": error,
        "resource_count": len(items),
        "resources": items,
    }


def _field(row, key):
    """读 Row 的可选列: 列不存在(旧库未迁移)时返回 None 而不是抛错。"""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _json_field(row, key):
    raw = _field(row, key)
    if not raw:
        return {}
    try:
        return json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (ValueError, TypeError):
        return {}


def write_manifest(task, resources, out_dir, stats=None, error=None, log=None):
    """写 manifest.json, 返回路径。失败只记录日志, 不向上抛。

    之所以不抛: manifest 是附属产物, 磁盘写满/权限问题不该把已经下载成功的
    任务判定为失败。

    error 省略时自动取 task 上的 error —— 调用方通常就是想把任务的报错原样
    记录进去, 没必要每个调用点都重复传一遍(漏传比多传常见得多)。
    """
    if error is None:
        error = _field(task, "error")
    data = build_manifest(task, resources, out_dir, stats, error)
    target = Path(out_dir) / MANIFEST_NAME
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(target)
    except Exception as e:
        if log:
            log(f"manifest 写入失败: {type(e).__name__}: {e}", "warn")
        return None
    return target


def read_manifest(task_dir):
    """读取已有 manifest, 不存在或损坏时返回 None。"""
    p = Path(task_dir) / MANIFEST_NAME
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def manifest_enabled(extra_options=None):
    """是否输出 manifest。可通过任务 options 里的 manifest=false 关闭。"""
    if extra_options and extra_options.get("manifest") is False:
        return False
    return bool(getattr(settings, "write_manifest", True))
