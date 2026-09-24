r"""图片库 / API 分页型站点的探针 —— `probe_site.py` 管不了的那一类。

为什么是**另一个脚本**而不是给 probe_site 加个分支
==================================================
两类站点要探的东西**没有一个字段重合**:

    序号枚举型(probe_site.py)      API / 分页型(本脚本)
    ────────────────────────      ──────────────────────
    ① 越界返不返 404               ① 分页参数是哪个, 语义是什么(page / offset / cursor)
    ② Accept 有没有被校验           ② 一份密钥是不是必需
    ③ 要不要浏览器                  ③ 每页最多给多少条
    ④ 尺寸/格式变体                 ④ 一个条目对应几张资源
    ⑤ 几种 URL 形态 -> 同一个 gid   ⑤ 列表内容从哪个端点下发

硬塞进一个脚本只会得到"两套 half-done 的探测", 而每一套的五项都是猜错就静默的类型。
所以这里刻意**只做探测、不出声明** —— 见下。

为什么**不生成声明**
====================
因为现在只有**一个**这类站点(`collectors/stockphotos/spider.py`, pexels)。一个实例
抽象出来的东西一定不对: 那 17KB 里有大量是 pexels 自己的形态(三种输入、集合页 ID 要
从 slug 里抽、缺密钥要报错而不是返回空)。等第二个这类站出现, 才看得出哪些是共性。
本脚本的产出是**实测报告 + 一份"要写这类采集器, 还缺哪几个字段"的清单**。

用法
====
::

    # 给它一个列表页, 它自己去找端点
    python scripts/probe_feed.py https://www.example.com/search/cat/

    # 直接给端点 + 密钥(密钥从环境变量读, 命令行里只写变量名)
    python scripts/probe_feed.py --api https://api.example.com/v1/search \
        --header "Authorization" --key-env SITE_API_KEY

⚠️ 密钥**从不回显**: 报告里只出现 header 名字与"已从 <ENV> 读取(n 字符)"。
⚠️ 本脚本只读**列表页与其 JSON 响应**, 不下任何资源。发现的资源 URL 只打印前几条。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from urllib.parse import urlencode, urljoin, urlparse

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from core.config import settings  # noqa: E402

MEDIA_RE = re.compile(
    r"https?://[^\s\"'<>\\]+\.(?:jpe?g|png|webp|avif|gif|bmp|mp4|m4v|mov|webm)(?:\?[^\s\"'<>\\]*)?",
    re.I,
)

#: 常见的"翻页"参数。分页语义在站点之间**完全不同**(`page=2` 是第 2 页, `offset=2` 是
#: 跳过 2 条), 所以不能只判"参数有没有用", 还要判"它是哪种语义" —— 写采集器时这两者
#: 的代码不一样, 判错就从第 2 页开始整段错位。
PAGE_PARAMS = ("page", "page_number", "pageno", "p", "offset", "start", "from", "cursor")

#: 常见的"每页条数"参数。
SIZE_PARAMS = ("per_page", "per-page", "perPage", "limit", "page_size", "pageSize", "size")

#: 探每页上限时用的档位(从小到大)。取到"不再增长"就停 —— 再大的值站点多半直接忽略,
#: 而多打几个请求并不会让结论更准。
SIZE_STEPS = (1, 5, 20, 50, 80, 100, 200)


def headers_for(user_agent=None, extra=None):
    h = {"User-Agent": user_agent or settings.user_agent, "Accept": "application/json, */*"}
    for k, v in (extra or {}).items():
        h[k] = v
    return h


def get_json(session, url, headers, timeout=None):
    """取 (状态码, 解析出的 JSON 或 None, Content-Type)。失败返回 (None, None, "")。"""
    try:
        r = session.get(url, headers=headers, timeout=timeout or settings.request_timeout,
                        allow_redirects=True)
    except Exception:
        return None, None, ""
    ct = (r.headers.get("Content-Type") or "").lower()
    try:
        return r.status_code, r.json(), ct
    except ValueError:
        return r.status_code, None, ct


def pick_json_list(obj, min_items=2):
    """在 JSON 里找"那个列表" —— 返回 `(路径, [item, ...])`; 找不到返回 `(None, [])`。

    判据是"**里面装着对象**且至少 `min_items` 个", 而不是"叫 items/data/results" ——
    字段名各家不同, 但"列表里是一堆同类对象"这个形状是共通的。找不到就老实返回空,
    绝不猜一个出来: 猜错会让后面的分页结论全部作废。
    """
    best = (None, [])

    def walk(node, path):
        nonlocal best
        if isinstance(node, list):
            dicts = [x for x in node if isinstance(x, dict)]
            if len(dicts) >= min_items and len(dicts) > len(best[1]):
                best = (path or "<root>", dicts)
            for i, x in enumerate(node[:3]):
                walk(x, "%s[%d]" % (path, i))
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(v, "%s.%s" % (path, k) if path else k)

    walk(obj, "")
    return best


def ids_of(items):
    """给每个条目找一个"身份字段", 用来判断两页内容是不是同一批。

    按 id / photo_id / uuid / slug 这类名字试; 都没有就退化成"整个条目的指纹" ——
    只要**能区分两条**就够了, 这里的用途只是"这两页一样吗"。
    """
    if not items:
        return []
    keys = ("id", "photo_id", "image_id", "uuid", "slug", "key", "pk", "code")
    for k in keys:
        if all(k in it for it in items[:3]):
            return [str(it.get(k)) for it in items]
    return [json.dumps(it, sort_keys=True, ensure_ascii=False)[:120] for it in items]


def classify_pagination(a, b):
    """比对同一个参数取两个值时的返回, 判出它是哪种分页语义。

    `a` / `b` 是 `(count, ids)`。返回:

        "no-effect"      内容没变 -> 这个参数站点不认
        "offset-like"    b 的第一条正好是 a 的第二条 -> 值表示"跳过多少条"
        "page-like"      内容变了, 但看不出偏移关系 -> 值表示"第几页"
        "empty"          取第二个值时变空 -> 可能已到末页, 或参数只是被忽略了
        "unknown"        有一边拿不到

    区分"跳过多少条"和"第几页"很值: 前者 `v = (page-1) * size`, 后者 `v = page`。
    写错的话从第 2 页起整段错位, 而现象只是"少了些图", 不会报错。
    """
    if not a or not b:
        return "unknown"
    (ca, ia), (cb, ib) = a, b
    if ca is None or cb is None:
        return "unknown"
    if not ib:
        return "empty"
    if ia and ib and ia[0] == ib[0]:
        return "no-effect"
    if len(ia) >= 2 and ib and ib[0] == ia[1]:
        return "offset-like"
    return "page-like"


def discover_endpoints(session, page_url, headers, timeout):
    """从页面 HTML 里找候选 API 端点。找不到就返回空列表(不猜)。"""
    try:
        r = session.get(page_url, headers=dict(headers, Accept="text/html,*/*"),
                        timeout=timeout or settings.request_timeout, allow_redirects=True)
        html = r.text
    except Exception:
        return []
    cands = []
    for m in re.finditer(r"""["'](https?://[^"'<>\\\s]*?/(?:v\d+/)?(?:api|search|photos|"""
                         r"""images|media|feed|list|query)[^"'<>\\\s]*)["']""", html):
        cands.append(m.group(1))
    for m in re.finditer(r"""["'](/[a-z0-9/_-]*(?:api|search|feed)[a-z0-9/_-]*)["']""", html):
        cands.append(urljoin(page_url, m.group(1)))
    out, seen = [], set()
    for u in cands:
        u = u.split("\\")[0]
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:8]


def try_page_param(session, api, headers, param, timeout):
    """试一个分页参数: 取 1 与 2, 返回 `(结论, 两次的条数, 两次的首个 id)`。"""
    res = []
    for v in (1, 2):
        url = api + ("&" if "?" in api else "?") + urlencode({param: v})
        _st, js, _ct = get_json(session, url, headers, timeout)
        path, items = pick_json_list(js) if js is not None else (None, [])
        res.append((len(items) if js is not None else None, ids_of(items)))
    return (classify_pagination(res[0], res[1]), res[0][0], res[1][0],
            (res[0][1][0] if res[0][1] else None), (res[1][1][0] if res[1][1] else None))


def try_size_param(session, api, headers, param, timeout):
    """试"每页条数"参数: 逐档加大, 返回 `(实际用上的档位, 各档条数)`。

    一发现"再加就不涨了"就停 —— 那个平台上限是**实测**出来的, 不是文档抄的。
    """
    rows = []
    for n in SIZE_STEPS:
        url = api + ("&" if "?" in api else "?") + urlencode({param: n})
        _st, js, _ct = get_json(session, url, headers, timeout)
        _path, items = pick_json_list(js) if js is not None else (None, [])
        rows.append((n, len(items) if js is not None else None))
        if len(rows) >= 3 and rows[-1][1] == rows[-2][1] == rows[-3][1]:
            break
    return rows


def wants_key(session, api, headers, bare_headers, timeout):
    """判密钥是不是必需: 带 / 不带各请求一次, 比对。

    返回 `"required"` / `"not-required"` / `"unknown"`。
    ⚠️ 只看**状态码**: 有的平台不带密钥也回 200, 只是把结果换成空列表 —— 那一种要
    靠条数判, 所以这里同时看条数。
    """
    a_st, a_js, _ = get_json(session, api, headers, timeout)
    b_st, b_js, _ = get_json(session, api, bare_headers, timeout)
    if a_st is None or b_st is None:
        return "unknown"
    if a_st == 200 and b_st in (401, 403):
        return "required"
    _p1, it1 = pick_json_list(a_js) if a_js is not None else (None, [])
    _p2, it2 = pick_json_list(b_js) if b_js is not None else (None, [])
    if a_st == 200 and b_st == 200 and len(it1) > 0 and len(it2) == 0:
        return "required(静默: 不带密钥只回空列表)"
    if b_st == 200 and len(it2) > 0:
        return "not-required"
    return "unknown"


def resource_urls(items, limit=3):
    """从条目里抽资源直链 —— 只打印, 不下载。"""
    found = []
    blob = json.dumps(items[:5], ensure_ascii=False)
    for m in MEDIA_RE.finditer(blob):
        u = m.group(0).replace("\\/", "/")
        if u not in found:
            found.append(u)
        if len(found) >= limit:
            break
    return found


def claim_of(urls):
    """这些资源 URL 会被哪个采集器认领(没人认领就落到通用采集器)。"""
    out = []
    try:
        from collectors import resolve_collector
    except Exception as e:
        return [("(认领检查不可用: %s)" % type(e).__name__, "")]
    for u in urls:
        try:
            r = resolve_collector(u)
            out.append((r.get("collector"), r.get("reason") or ""))
        except Exception as e:
            out.append(("?", "%s" % type(e).__name__))
    return out


def main():
    ap = argparse.ArgumentParser(
        description="API / 分页型站点探针(不出声明 —— 一个实例不足以抽象)")
    ap.add_argument("page", nargs="?", default="", help="列表页 URL(可选, 用来找端点)")
    ap.add_argument("--api", default="", help="直接指定 API 端点")
    ap.add_argument("--header", default="", help="要带的 header 名, 如 Authorization")
    ap.add_argument("--key-env", default="", help="从哪个环境变量读该 header 的值")
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--timeout", type=float, default=None)
    args = ap.parse_args()

    if not args.page and not args.api:
        print(__doc__)
        sys.exit(1)

    import requests

    session = requests.Session()
    proxy = args.proxy if args.proxy is not None else settings.proxy
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})

    extra, bare = {}, {}
    if args.header:
        if not args.key_env:
            print("!! 给了 `--header` 就要给 `--key-env`(密钥只从环境变量读, 不放进命令行)。")
            sys.exit(1)
        val = os.environ.get(args.key_env, "")
        if not val:
            print("!! 环境变量 %s 是空的 —— 先设置它, 别把密钥写在命令行里(会进 shell 历史)。"
                  % args.key_env)
            sys.exit(1)
        extra[args.header] = val
    headers = headers_for(extra=extra)
    bare_headers = headers_for()

    print("=" * 74)
    print("API / 分页型站点探针  (只探测, 不下载任何资源)")
    print("=" * 74)
    if args.header:
        print("密钥     : header `%s` 已从 %s 读取(%d 字符, 不回显)"
              % (args.header, args.key_env, len(extra.get(args.header, ""))))
    else:
        print("密钥     : 未提供(无密钥时下面会明说哪些结论验不出来)")
    print()

    # ---- ① 端点 ----
    print("== ① 列表内容从哪个端点下发 ==")
    api = args.api
    if not api and args.page:
        cands = discover_endpoints(session, args.page, headers, args.timeout)
        if cands:
            print("  页面里发现的候选端点:")
            for u in cands:
                print("    %s" % u[:100])
            api = cands[0]
            print("  => 先用第一个试; 若下面每项都拿不到 JSON, 换 `--api` 指定另一个。")
        else:
            print("  !! 页面里没找到候选端点(多半是 JS 动态拼接的)。")
            print("     下一步: 浏览器 F12 -> Network -> XHR, 把真正的请求 URL 用 `--api` 传进来。")
            sys.exit(2)
    if not api:
        print("  (没给列表页, 也没给 `--api` —— 后面几项没得跑)")
        sys.exit(1)
    print("  端点: %s" % api)

    st, js, ct = get_json(session, api, headers, args.timeout)
    print("  探测: %s  %s" % (st, ct or "(无 Content-Type)"))
    if st is None:
        print("  => !! 端点本身就请求不通 —— 先解决可达性(代理 / 密钥 / 端点对不对)。")
        sys.exit(1)
    if js is None:
        print("  => !! 返回的不是 JSON(多半打到了 HTML 页面) —— 换一个候选端点再试。")
        print("     现象与「端点写错」无法区分, 所以这里不下任何结论。")
        sys.exit(2)
    path, items = pick_json_list(js)
    if not items:
        print("  => !! 响应是 JSON, 但里面**没有**一个「装着 ≥2 个对象的列表」。")
        print("     可能要用密钥、也可能是单条查询。下面几项都验不出来。")
        sys.exit(2)
    print("  列表在   : `%s`  共 %d 条" % (path, len(items)))
    for u in resource_urls(items):
        print("    资源示例: %s" % u[:96])
    for who, why in claim_of(resource_urls(items)):
        print("    认领     : %s   %s" % (who, why))

    # ---- ② 密钥 ----
    print("\n== ② 密钥是不是必需 ==")
    if args.header:
        verdict = wants_key(session, api, headers, bare_headers, args.timeout)
        print("  带 / 不带 `%s` 各请求一次 -> %s" % (args.header, verdict))
        if verdict == "unknown":
            print("  => 结论不明确, 人工看一眼两次的状态码与条数。")
    else:
        st2, js2, _ = get_json(session, api, bare_headers, args.timeout)
        _p2, items2 = pick_json_list(js2) if js2 is not None else (None, [])
        if st2 == 200 and items2:
            print("  => 不带任何密钥就能拿到 %d 条 —— 这个端点**不需要**密钥。" % len(items2))
        else:
            print("  => 不带密钥时: %s, 列表 %s 条 —— **看起来需要密钥**, 但没验(没给 "
                  "`--header`/`--key-env`), 别当成结论。" % (st2, len(items2)))

    # ---- ③ 分页协议 ----
    print("\n== ③ 分页协议 ==")
    worked = []
    for param in PAGE_PARAMS:
        kind, ca, cb, fa, fb = try_page_param(session, api, headers, param, args.timeout)
        if kind in ("unknown", "no-effect"):
            print("  %-10s -> %-10s (%s / %s 条)" % (param, kind, ca, cb))
            continue
        print("  %-10s -> %-10s (%s -> %s 条; 首个 id %s -> %s)"
              % (param, kind, ca, cb, str(fa)[:12], str(fb)[:12]))
        worked.append((param, kind))
    if not worked:
        print("  => !! 上面这些常见参数**一个都没能让内容变** —— 分页协议还没验出来。")
        print("     下一步: 在浏览器里翻到第 2 页, 看 XHR 里多了哪个参数或 header")
        print("     (有的平台用 `Link` 响应头 / `next` 字段做游标翻页, 那种要按响应里的")
        print("     下一跳地址走, 不能自己拼参数)。")
    else:
        p, k = worked[0]
        print("  => 用 `%s`(%s)。⚠️ 语义判错会让第 2 页起整段错位, 而现象只是"
              % (p, k))
        print("     「少了些图」, 不报错 —— 上面 `offset-like` 与 `page-like` 的差别就是它。")
        for extra_p, ek in worked[1:]:
            print("     (另外 `%s` 也有效: %s)" % (extra_p, ek))

    # ---- ④ 每页上限 ----
    print("\n== ④ 每页最多给多少条 ==")
    size_param = worked[0][0] if worked else PAGE_PARAMS[0]
    best = None
    for param in SIZE_PARAMS:
        rows = try_size_param(session, api, headers, param, args.timeout)
        shown = ", ".join("%s->%s" % (n, c) for n, c in rows)
        grew = [c for _n, c in rows if isinstance(c, int)]
        if grew and max(grew) > len(items) and max(grew) > (grew[0] if grew else 0):
            print("  %-10s %s" % (param, shown))
            best = (param, max(grew))
            break
        print("  %-10s %s   (没超过默认 %d 条)" % (param, shown, len(items)))
    if best:
        print("  => 用 `%s`, 实测最多一次给 %d 条(再大就不涨了)。" % best)
    else:
        print("  => 上面这些参数都没能把条数顶上去 —— 站点可能就按固定页大小给,")
        print("     那就只能一页页翻(记得设一个 max_items 上限, 别把整个搜索结果翻完)。")
    print("     分页参数用 `%s`(见 ③)。" % size_param)

    # ---- ⑤ 要写采集器还缺什么 ----
    print("\n" + "=" * 74)
    print("要写这类采集器, 声明里需要的东西(现在**没有**通用契约)")
    print("=" * 74)
    print("  · 端点模板(含把用户输入的 slug/query 填进去的位置)")
    print("  · 分页: 参数名 + 语义(%s) + 每页上限" % (worked[0][1] if worked else "还没验出来"))
    print("  · 密钥: %s" % ("必需, 从环境变量读" if args.header else "待验"))
    print("  · 条目 -> 资源的映射(一个条目几张图? 这里只看到 %d 条 URL 样例)"
          % len(resource_urls(items)))
    print("  · 认领正则 + `gid_shape`(它要能挡住列表页 URL, 别把页码当 ID)")
    print()
    print("⚠️ 本脚本**不生成声明**: 现在只有 pexels 一个实例, 抽象出来的契约一定不对。")
    print("   等第二个这类站点出现, 再回头看哪些字段是共性 —— 那时 `gallery_base` 的")
    print("   `GallerySite` 才是该加东西的地方。")


if __name__ == "__main__":
    main()
