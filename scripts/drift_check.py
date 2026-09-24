r"""站点特征漂移巡检 —— 拿快照跟线上**逐项比对**。

它补的是哪一段
==============
本项目的头号静默故障是"站点改版" —— 改完之后任务照样 `success`, 只是资源变成 0 个。
而在这之前**没有任何信号**:

    selfcheck.py     只做**离线**自洽(正则 vs 样本), 完全不知道线上变了没
    cdn_profile      只在**已经采不到东西之后**才看得出来(且只有跑过任务才有记录)
    用户报障          最晚的那一道, 也是现在唯一的那一道

`probe_site.py --json` 已经把五项实测落成快照, 本脚本负责**过一段时间再跑一次**并比对。

漂移为什么必须分档
==================
因为告警一旦变成噪音, 就没人看了 —— 而"永远在闪的灯"和"没有灯"是一回事。

    硬漂移  会静默少采 / 采空, **必须**报警
            进不去 / 序号枚举型前提塌了 / 存在性判定规则变了 / Accept 开始被校验 /
            序号宽度变了 / 基址搬家 / 主档位消失 / 原来能解析的 URL 解析不出来了
    软漂移  只是"多了一点或换了个说法", 记一笔就好
            多一个画质档 / 多一条页面基址 / 多一种 URL 形态

判据和 `probe_site.py` 的三档门禁是**同一条**: 猜错了, 是每条 URL 都错, 还是只是少采一点。

用法
====
::

    python scripts/drift_check.py                  # 巡检所有已注册的声明式站点
    python scripts/drift_check.py --site xchina_gallery
    python scripts/drift_check.py --update         # 确认漂移是站点正常改版, 刷新基线
    python scripts/drift_check.py --list           # 只看有哪些基线

⚠️ 它**必须联网**, 所以**不要放进 CI** —— CI 里那套(`selfcheck.py` 的声明自洽)是纯离线的,
两者分工不同: 一个守"我写错了", 一个守"外面变了"。

⚠️ 基线快照在 `data/site_probe.json`(可用 `UWC_SITE_PROBE` 覆盖)。第一次跑只会**记基线**
并明确说出来 —— "没有基线"和"没有漂移"是两件事, 混在一起会让人以为巡检过了。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from collectors import COLLECTORS  # noqa: E402
from collectors.gallery_base import GallerySite  # noqa: E402

import probe_site  # noqa: E402  (复用 site_name / MEDIA_EXT / 快照默认路径)

DEFAULT_SNAPSHOT = probe_site.SNAPSHOT_DEFAULT


def snapshot_path():
    """快照位置; `UWC_SITE_PROBE=off` 可以完全关掉(与其它状态文件一致的约定)。"""
    raw = os.environ.get("UWC_SITE_PROBE")
    if raw is not None and raw.strip().lower() in (
            "", "0", "off", "no", "false", "none", "disable", "disabled"):
        return None
    return Path(raw) if raw else DEFAULT_SNAPSHOT


def load_baseline(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_baseline(path, data):
    """原子落盘(临时文件 + replace)。返回是否成功 —— 失败**不能**静默。"""
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                       encoding="utf-8", newline="")
        tmp.replace(p)
        return True
    except OSError:
        return False


def declared_sites():
    """所有**声明式**站点: 返回 `[(collector_name, site), ...]`。

    只收 `site` 是 `GallerySite` 的那些 —— 别的采集器(通用 / 图片库型)没有
    `id_samples` 这份样本, 也就没有可比的基线。
    """
    out = []
    for name, cls in sorted(COLLECTORS.items()):
        site = getattr(cls, "site", None)
        if isinstance(site, GallerySite):
            out.append((name, site))
    return out


def sample_media_urls(site):
    """从声明里挑"能当靶子"的资源直链(只挑媒体扩展名的那几条)。

    `id_samples` 里同时有直链与相册页, 而**只有直链**能用来重跑五项探测 —— 拿相册页
    当输入会被探针当成"没有资源直链"直接拒掉。
    """
    out = []
    for item in (site.id_samples or []):
        u = item[0] if isinstance(item, (list, tuple)) and item else None
        if not isinstance(u, str) or not u.startswith("http"):
            continue
        tail = u.split("?")[0].split("#")[0].rstrip("/").rsplit("/", 1)[-1]
        ext = tail.rsplit(".", 1)[1].lower() if "." in tail else ""
        if ext in probe_site.MEDIA_EXT:
            out.append(u)
    return out


def probe_now(urls, scan=6, timeout=None):
    """跑一遍探针, 返回它写出的快照。

    用**子进程**而不是 import: 探针是给人看的 CLI(满屏 print), 直接调它的函数要么被
    输出淹掉, 要么得给它加一堆 quiet 开关 —— 那才是真正的耦合。子进程还能顺手验证
    "`probe_site.py` 作为独立脚本仍然跑得起来"(它是最常被单独使用的那个入口)。
    """
    with tempfile.TemporaryDirectory(prefix="uwc-drift-") as td:
        out = Path(td) / "snap.json"
        cmd = [sys.executable, "-u", str(Path(__file__).with_name("probe_site.py"))]
        cmd += list(urls) + ["--json", str(out), "--scan", str(scan)]
        if timeout:
            cmd += ["--timeout", str(timeout)]
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                           cwd=str(ROOT))
        except subprocess.TimeoutExpired:
            return {}
        return load_baseline(out)


# --------------------------------------------------------------------------
# 比对: 纯函数, 好测
# --------------------------------------------------------------------------

HARD, SOFT = "hard", "soft"


def _f(level, field, old, new, why):
    return {"level": level, "field": field, "old": old, "new": new, "why": why}


def classify_drift(old, new):
    """比对两份快照, 返回 `[{level, field, old, new, why}, ...]`。

    只有**会静默少采 / 采空**的差异算硬漂移。判据与 `probe_site.py` 的门禁同源:
    猜错了, 是每条 URL 都错, 还是只是少采一点。
    """
    out = []
    if not old or not new:
        return out

    # ---- 硬: 进得去吗 ----
    if old.get("reachable") and not new.get("reachable"):
        out.append(_f(HARD, "reachable", True, False,
                      "原来能进, 现在进不去(%s %s)" % (new.get("reach_status"),
                                                   new.get("reach_ctype") or "-")))

    # ---- 硬: 序号枚举型这个前提还在吗 ----
    if old.get("opaque_suffix") is False and new.get("opaque_suffix") is True:
        out.append(_f(HARD, "opaque_suffix", False, True,
                      "序号之后变成了不透明串(内容哈希) —— 序号枚举型的前提没了"))
    oh, nh = old.get("exists_hits"), new.get("exists_hits")
    if isinstance(oh, int) and isinstance(nh, int) and oh > 0 and nh < oh:
        out.append(_f(HARD, "exists_hits", oh, nh,
                      "同一批序号里命中数下降 —— 可能只是图集变短, 也可能是判定规则变了, "
                      "先看下面 over_* 那一项"))
    if (old.get("exists_shape") and new.get("exists_shape")
            and old["exists_shape"] != new["exists_shape"]
            and old["exists_shape"].startswith("T") and not new["exists_shape"].startswith("T")):
        out.append(_f(HARD, "exists_shape", old["exists_shape"], new["exists_shape"],
                      "第一个序号不再命中 —— 基址/序号宽度/suffix 至少有一个错了"))

    # ---- 硬: 存在性判定规则 ----
    for k in ("over_status", "over_ctype"):
        if old.get(k) != new.get(k):
            out.append(_f(HARD, k, old.get(k), new.get(k),
                          "越界对照的样子变了 —— 判定规则(状态码 vs Content-Type)跟着变, "
                          "枚举的上界/终止条件必须重看"))

    # ---- 硬: Accept 从"无所谓"变成"必须带" ----
    if old.get("accept") == "not-required" and new.get("accept") == "required":
        out.append(_f(HARD, "accept", "not-required", "required",
                      "站点开始校验 Accept —— 所有请求会一起 403, 现象与「整站挂了」无法区分"))

    # ---- 硬: 拼 URL 的那几个字段 ----
    for k, why in (("base", "基址搬家"), ("base_path", "资源换了子路径"),
                   ("seq_format", "序号补零宽度变了(拼出来的每一页都会 404)")):
        if old.get(k) and new.get(k) and old[k] != new[k]:
            out.append(_f(HARD, k, old[k], new[k], why))

    # ---- 硬: 主档位消失 / 原来能解析的 URL 解析不出来 ----
    suf = old.get("suffix")
    if suf and suf in (old.get("variants") or []) and suf not in (new.get("variants") or []):
        out.append(_f(HARD, "variants", old.get("variants"), new.get("variants"),
                      "采信的那条直链的档位(%r)在新结果里没了" % suf))
    orr, nrr = old.get("id_resolution") or {}, new.get("id_resolution") or {}
    for u, g in orr.items():
        if u in nrr and g is not None and nrr[u] != g:
            out.append(_f(HARD, "id_resolution", "%s -> %r" % (u, g),
                          "%s -> %r" % (u, nrr[u]),
                          "这条输入以前能解析出 gid, 现在解析不出来了(URL 形态或正则失效)"))
            break

    # ---- 软: 只是多 / 只是换了说法 ----
    ov, nv = set(old.get("variants") or []), set(new.get("variants") or [])
    if nv - ov:
        out.append(_f(SOFT, "variants", sorted(ov), sorted(nv),
                      "多出画质档 %s —— 好事, 可以考虑写进声明" % sorted(nv - ov)))
    opb, npb = set(old.get("page_bases") or []), set(new.get("page_bases") or [])
    if npb - opb:
        out.append(_f(SOFT, "page_bases", sorted(opb), sorted(npb),
                      "页面里多出基址候选 %s" % sorted(npb - opb)))
    if (old.get("id_patterns") or []) != (new.get("id_patterns") or []):
        out.append(_f(SOFT, "id_patterns", old.get("id_patterns"),
                      new.get("id_patterns"), "生成的正则变了(可能是 URL 形态微调)"))
    return out


# --------------------------------------------------------------------------


def _one(name, site, baseline, snapshot, update, scan, timeout):
    """巡检单个站点; 返回 `(findings, status_note)`, 并就地更新 baseline/snapshot。"""
    urls = sample_media_urls(site)
    if not urls:
        return [], "跳过: `id_samples` 里没有资源直链(只剩相册页样例)"
    key = probe_site.site_name(urlparse(urls[0]).netloc)
    now = probe_now(urls[:2], scan=scan, timeout=timeout)
    if not now:
        return [], "!! 探针没跑出结果(子进程失败或超时) —— 这一条**没查**, 不要当成通过"
    snap = now.get(key) or (list(now.values())[0] if len(now) == 1 else None)
    if not snap:
        return [], "!! 探针写出的快照里找不到 %r" % key

    if key not in baseline:
        snapshot[key] = snap
        return [], "第一次见 —— 已记录基线(下次才有得比)"

    old = baseline[key]
    findings = classify_drift(old, snap)
    snapshot[key] = snap if update else old
    if update and findings:
        return findings, "已按 `--update` 刷新基线"
    return findings, ("与基线一致" if not findings else "")


def main():
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        print(__doc__)
        return
    update = "--update" in args
    site_filter = ""
    if "--site" in args:
        i = args.index("--site")
        site_filter = args[i + 1] if i + 1 < len(args) else ""
    scan = 6
    if "--scan" in args:
        i = args.index("--scan")
        try:
            scan = int(args[i + 1])
        except (IndexError, ValueError):
            pass

    path = snapshot_path()
    if path is None:
        print("快照被 `UWC_SITE_PROBE` 关掉了 —— 巡检没有基线可用。")
        sys.exit(1)
    baseline = load_baseline(path)
    sites = declared_sites()
    if site_filter:
        sites = [(n, s) for n, s in sites if n == site_filter]

    if "--list" in args:
        print("== 快照里的基线 ==")
        for k in sorted(baseline):
            b = baseline[k] or {}
            print("  %-24s base=%s  hits=%s  accept=%s"
                  % (k, b.get("base"), b.get("exists_hits"), b.get("accept")))
        print("\n== 可巡检的声明式站点 ==")
        for n, _s in declared_sites():
            print("  %s" % n)
        return

    if not sites:
        print("没有匹配的声明式站点(用 --list 看有哪些)。")
        sys.exit(1)

    print("=" * 74)
    print("站点特征漂移巡检  (基线: %s)" % path)
    print("=" * 74)
    snapshot = dict(baseline)
    hard_n = 0
    checked = 0
    for name, site in sites:
        print("\n[%s]" % name)
        findings, note = _one(name, site, baseline, snapshot, update, scan, None)
        if note.startswith("!!"):
            print("  %s" % note)
            continue
        if note.startswith("跳过"):
            print("  %s" % note)
            continue
        checked += 1
        if not findings:
            print("  ok  与基线一致(%s)" % (note or "无差异"))
            continue
        for f in findings:
            mark = "!!" if f["level"] == HARD else " ·"
            print("  %s [%s] %s" % (mark, f["level"], f["field"]))
            print("        %r -> %r" % (f["old"], f["new"]))
            print("        %s" % f["why"])
            if f["level"] == HARD:
                hard_n += 1
        print("  %s" % ("!! 硬漂移 %d 条 —— 去看上面的字段"
                        % sum(1 for f in findings if f["level"] == HARD)
                        if any(f["level"] == HARD for f in findings)
                        else "只有软漂移(不影响能不能采到)"))

    if update:
        ok = save_baseline(path, snapshot)
        print("\n基线已刷新 -> %s%s" % (path, "" if ok else "  !! 写不进去"))
    elif any(snapshot.get(k) != baseline.get(k) for k in snapshot):
        ok = save_baseline(path, snapshot)
        print("\n新站点的基线已补记 -> %s%s" % (path, "" if ok else "  !! 写不进去"))

    print("\n" + "=" * 74)
    if hard_n:
        print("结论: %d 个站点查过, **硬漂移 %d 条** —— 站点可能改版了, 去看 collector"
              % (checked, hard_n))
        print("      对应字段, 改完重跑本脚本; 确认是正常改版就 `--update` 刷新基线。")
        sys.exit(1)
    print("结论: %d 个站点查过, 无硬漂移。软漂移只是线索, 不影响能不能采到。" % checked)


if __name__ == "__main__":
    main()
