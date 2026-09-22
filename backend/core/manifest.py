"""任务产出清单 manifest.json。

每个任务跑完(或中途取消/失败)都写一份 manifest.json, 回答三个
日常必然被问到的问题:

    这个文件原来在哪 -> source_url / resolved_url
    它是不是完整     -> sha256 / size
    为什么没下载     -> status / note

它是**面向用户的交付物**, 不是内部状态: 内部状态以 SQLite 为准(DB 可能被
清理、换台机器也拿不到)。所以这里用宽松写法: 拿不到的字段
写 null, 整个写出失败也不影响任务本身(只记一条 warn 日志)。

⚠️ 清单**不跟媒体文件放在一起**: 视频是平铺在下载根目录的, 清单要是也在根
目录, 每跑完一个视频任务就会盖掉上一个的 manifest.json。它统一落在
`下载根/_meta/<任务ID>/`, 而 `file` 字段相对的是**下载根**(不是清单自己所在
的目录)—— 目录整体搬家后仍然可解析, 用户也能直接拿这个名字去下载根下找文件。
"""

import json
import time
from pathlib import Path

from core.config import settings

MANIFEST_NAME = "manifest.json"
SIDECAR_NAME = "album.json"
VERSION = 1


def _rel_path(base_dir, local_path):
    """把绝对路径转成相对 `base_dir` 的 POSIX 路径; 不在目录内时返回 None。"""
    if not local_path:
        return None
    try:
        base = Path(base_dir).resolve()
        rel = Path(local_path).resolve().relative_to(base)
    except (ValueError, OSError, TypeError):
        return None
    return rel.as_posix()


def build_manifest(task, resources, out_dir, stats=None, error=None, rel_base=None):
    """生成 manifest 数据结构。

    task/resources 均为 sqlite3.Row —— 只支持下标取值, 不支持 .get()。

    `out_dir` 是**写在哪**(现在是 `下载根/_meta/<任务ID>/`), `rel_base` 是
    `file` 字段**相对谁**(下载根)。两者必须分开: 清单挪进 `_meta/` 之后,
    若还按自己的位置算相对路径, 目录里就会写满 `../../相册名/0001.jpg`,
    用户既看不懂, 也没法拿它去别处对文件。
    """
    base = rel_base or out_dir
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
                "file": _rel_path(base, local),
                "size": _field(r, "size"),
                "sha256": _field(r, "hash"),
                "content_type": _field(r, "content_type"),
                "status": r["status"],
                "note": _field(r, "note"),
                # 感知指纹与"疑似重复"(见 core/phash.py)。**只标记不删除**:
                # 文件仍在磁盘上, 这里只是把判断依据留给用户和下游脚本。
                # 写成两个字段而不是塞进 note, 是为了能被 jq 直接筛:
                #   jq '.resources[] | select(.duplicate_of) | .file'
                "phash": _field(r, "phash"),
                "duplicate_of": _field(r, "duplicate_of"),
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
        "download_root": str(Path(base).resolve()),
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


def _log(log, msg, level="warn"):
    """调用日志回调, 容忍只接受一个参数的回调。

    这里刻意宽松: 回调形态不对是**调用方的 bug**, 但 manifest/album.json 只是
    附属产物 —— 为了报一句"写不出来"而抛异常, 反而会把已经下载成功的任务
    判定为失败, 本末倒置。
    """
    if not log:
        return
    try:
        log(msg, level)
    except TypeError:
        try:
            log(msg)
        except Exception:
            pass
    except Exception:
        pass


def write_manifest(task, resources, out_dir, stats=None, error=None, log=None,
                   rel_base=None):
    """写 manifest.json, 返回路径。失败只记录日志, 不向上抛。

    之所以不抛: manifest 是附属产物, 磁盘写满/权限问题不该把已经下载成功的
    任务判定为失败。

    error 省略时自动取 task 上的 error —— 调用方通常就是想把任务的报错原样
    记录进去, 没必要每个调用点都重复传一遍(漏传比多传常见得多)。
    rel_base: 见 `build_manifest`(清单在 `_meta/` 下, 而 file 要相对下载根)。
    """
    if error is None:
        error = _field(task, "error")
    data = build_manifest(task, resources, out_dir, stats, error, rel_base=rel_base)
    target = Path(out_dir) / MANIFEST_NAME
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(target)
    except Exception as e:
        _log(log, f"manifest 写入失败: {type(e).__name__}: {e}")
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


def build_sidecar(task, meta, resources=None):
    """生成 album.json 数据结构。

    与 manifest.json 的分工::

        manifest.json   文件级, 一行一个资源(路径/sha256/实际下载点)
        album.json      集合级, 一份描述整个相册(目录名/厂牌/标签/资源根)

    两者都落在 `下载根/_meta/<任务ID>/`, 与媒体文件分开存放; 整体搬走仍自解释。
    """
    m = dict(meta or {})
    counts = {}
    for r in resources or []:
        t = r.get("type") or "other"
        counts[t] = counts.get(t, 0) + 1
    return {
        "version": VERSION,
        "task_id": task["id"] if task is not None else None,
        "collector": m.get("collector") or (
            task["collector"] if task is not None else None
        ),
        "source_url": m.get("source_url") or (
            task["url"] if task is not None else None
        ),
        "finished_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "gid": m.get("gid"),
        "album": m.get("album"),
        "album_source": m.get("album_source"),
        "title": m.get("title"),
        "maker": m.get("maker"),
        "tags": list(m.get("tags") or []),
        "media": list(m.get("media") or []),
        "counts": counts,
        # 相册页**自报**数量: 只用于对照, 不当资源清单(页面会改版/滞后)
        "photos_declared": m.get("photos_declared"),
        "videos_declared": m.get("videos_declared"),
        # ⚠️ 排查任务为什么采不到东西时, 先看这一项
        "resource_roots": dict(m.get("resource_roots") or {}),
    }


def write_sidecar(task, meta, out_dir, resources=None, log=None):
    """写 album.json, 返回路径。失败只记日志, 不向上抛。

    与 manifest 同样的宽处理: 它是附属产物, 磁盘写满/权限问题不该把已经下载
    成功的任务判定为失败。
    """
    if not meta:
        return None
    data = build_sidecar(task, meta, resources)
    target = Path(out_dir) / SIDECAR_NAME
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(target)
    except Exception as e:
        _log(log, f"album.json 写入失败: {type(e).__name__}: {e}")
        return None
    return target


def manifest_enabled(extra_options=None):
    """是否输出 manifest。可通过任务 options 里的 manifest=false 关闭。"""
    if extra_options and extra_options.get("manifest") is False:
        return False
    return bool(getattr(settings, "write_manifest", True))
