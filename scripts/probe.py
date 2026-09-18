"""站点解析探针: 对任意 URL 输出四解析器发现结果与质量选择结论。

用法:
    uv run python scripts/probe.py https://example.com/photoShow.html?id=xxx
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from collectors.browser import BrowserCollector  # noqa: E402
from collectors.parsers import merge_by_priority, select_quality  # noqa: E402


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    url = sys.argv[1]
    print(f"probing: {url}\n")

    results = BrowserCollector().capture_detailed(url)

    print("== 各解析器原始发现 ==")
    for source in ("api", "network", "js", "dom"):
        items = results.get(source, [])
        print(f"\n[{source}] {len(items)} 个")
        for r in items[:10]:
            print(f"  {r['type']:5s} {r['url'][:100]}")
        if len(items) > 10:
            print(f"  ... 共 {len(items)} 个")

    merged = merge_by_priority(results)
    final = select_quality(merged)
    by_type = {}
    for r in final:
        by_type.setdefault(r["type"], []).append(r)

    print("\n== 优先级合并后 ==")
    print(f"共 {len(merged)} 个 (去重 {sum(len(v) for v in results.values()) - len(merged)} 个)")

    print("\n== 质量选择后(将进入下载) ==")
    for t, items in by_type.items():
        print(f"  {t}: {len(items)} 个")
    for r in final:
        print(f"  {r['type']:5s} [{r['source']:7s}] {r['url'][:100]}")

    print("\nheaders 示例(下载时携带):")
    for r in final[:3]:
        print(f"  {r['url'][:60]} -> {json.dumps(r.get('headers', {}), ensure_ascii=False)[:120]}")


if __name__ == "__main__":
    main()
