"""一键自检: 站点声明 + CDN 画像快照。

用法::

    uv run python scripts/selfcheck.py                 # 两项都看
    uv run python scripts/selfcheck.py --profile-only  # 只看 CDN 画像
    uv run python scripts/selfcheck.py --reset-profile # 清空画像后再看

为什么要有这个脚本(而不是只留测试)
====================================
测试回答的是"代码改对了没", 这个脚本回答的是"**现在这台机器上的配置和画像
是什么状态**"。两者不能互相替代:

- 站点声明自检(`check_site`)在运行期只记 warn —— 采集器不该因为一次自检失败
  就拒绝干活, 那时用户更需要"先把资源下下来"。于是问题只会躺在日志里,
  需要一个主动去翻它的入口。
- CDN 画像(`data/cdn_profile.json`)是运行期**攒出来**的数据, 测试里永远是空的。
  它的价值恰恰在于看趋势: 某条基址的命中率何时开始塌, 就是站点在迁 CDN 的
  第一个信号 —— 那比用户报"某天开始全失败"要早得多。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from collectors.gallery_base import selfcheck_all  # noqa: E402
from core import cdn_profile  # noqa: E402


def _declarations():
    print("== 站点声明自检 ==")
    problems = selfcheck_all()
    if not problems:
        print("  ok  所有已注册站点的声明自洽(样本 -> gid 无冲突)")
        return True
    print(f"  !!  {len(problems)} 个站点有问题:")
    for site, items in problems.items():
        print(f"  [{site}]")
        for p in items:
            print(f"      - {p}")
    print("\n  这些是**配置**问题(正则/形状/样本互相矛盾), 不是网络问题。")
    print("  修法: 改 collectors/<站点>/gallery.py 的 id_patterns / gid_shape /"
          " id_samples, 改完重跑本脚本。")
    return False


def _profile():
    print("\n== CDN 画像 ==")
    snap = cdn_profile.summarize()
    if not snap:
        print("  还没有任何记录(没跑过图集任务, 或画像被关掉了)")
        print("  画像位置: "
              + (str(cdn_profile._path()) if cdn_profile._path()
                 else "已由 UWC_CDN_PROFILE 关闭"))
        return
    for site, info in sorted(snap.items()):
        print(f"  [{site}] 命中 {info['total_hits']} 次, 最近用 {info['last']}")
        for b in info["bases"]:
            bar = "#" * max(1, int(round(b["share"] * 24)))
            print(f"      {b['share']:>5.1%} {bar:<24} {b['base']}  ({b['hits']})")
        if info["seq_formats"]:
            fmts = ", ".join(f"{k}×{v}" for k, v in
                             sorted(info["seq_formats"].items(), key=lambda kv: -kv[1]))
            print(f"      序号格式: {fmts}")
    print("\n  怎么看: 某条基址的占比**突然**从主跌到 0, 基本就是站点换了 CDN 子路径。")
    print("  此时去 collectors/<站点>/gallery.py 把 base / base_candidates 更新一下,")
    print("  或者只加 base_host_templates 把新 host 声明为候选(不必动旧值)。")


def main():
    args = sys.argv[1:]
    if "--reset-profile" in args:
        cdn_profile.reset()
        print("已清空 CDN 画像\n")

    ok = True
    if "--profile-only" not in args:
        ok = _declarations()
        if "--decl-only" in args:
            sys.exit(0 if ok else 1)
    _profile()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
