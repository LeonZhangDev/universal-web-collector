# 采集器插件 SDK

面向**想加一个新站点的人**(包括未来的自己与 AI)。这份文档把 `collectors/gallery_base.py`
里**已经存在**的声明式契约写出来 —— 它一直在, 只是没被写成一份可读的说明书。

> gallery-dl 最值得学的不是它的代码, 是它**庞大的插件生态**: 加站的成本被压到
> "写一份声明"。我们的做法一样: **加站的成本在探测, 不在写声明**。

## 0. 先确认: 你的站是"序号枚举型"吗

本 SDK 只覆盖一类站点 —— 一个相册下 N 张按**序号**命名的图片
(`00001.jpg` ~ `00056.jpg`), 资源地址可以由 `图集 ID + 序号` 直接拼出来。

判据不看"是不是以数字开头", 而看**序号后面还有没有残留熵**:

* `/photos/<gid>/00001.jpg` —— 是(序号就是序号);
* `/data/<hash>/1-<sha256>.png` —— **不是**(改序号必定 404, 按内容哈希寻址)。

不确定就先跑探测(见第 2 节), 它会替你回答这个问题。

## 1. 一个采集器就是一份 `GallerySite` 声明

```python
from collectors.gallery_base import GallerySite, register

@register
SITE = GallerySite(
    name="example",
    base="https://img.example.com/photos",
    variants=["", "_800"],              # 画质从高到低
    seq_format="{seq:05d}",
    id_patterns=[r"/photo/id-([0-9a-f]+)", r"/photos/([0-9a-f]+)"],
    album_url_template="https://example.com/photo/id-{gid}.html",
    id_samples=[                        # 自检样本, 见第 4 节
        ("https://example.com/photo/id-6aa5136f606fe.html", "6aa5136f606fe"),
    ],
)
```

关键字段(齐全的清单见 `gallery_base.GallerySite` 的注释):

| 字段 | 作用 | 不填会怎样 |
| --- | --- | --- |
| `base` | 资源 URL 前缀 | 必填 |
| `variants` | 画质变体后缀, 按**画质从高到低** | 必填 |
| `id_patterns` | ID 提取正则(第 1 组即 ID), **顺序即优先级** | 必填 |
| `seq_format` | 序号格式(`{seq:05d}`) | 默认 5 位 |
| `base_candidates` / `base_candidate_digits` | CDN 分桶(`photos` / `photos2` / `photos3`) | 站点换分桶时**整批判空**, 表现为"任务失败"而看不出是路径变了 |
| `base_host_templates` | 换 host / 换前缀的迁移 | 同上, 只能靠用户报障才发现 |
| `gid_shape` | ID 的**形状**正则(fullmatch) | 自动识别时可能把路径词当成 ID |
| `page_tail` | 末段匹配此规则视为**页码**而非 ID | 拿页码当 gid → "任务成功但 0 个资源" |
| `id_samples` | 自检样本 | 见第 4 节 |
| `browser_impersonation` | CF 站点要开浏览器指纹 | 被 TLS 指纹拦掉 |
| `video_*` | 同一 gid 下还有视频时填 | 留空 = 只采图片 |

## 2. 加站的正确顺序: **先探测, 再写声明**

```bash
python scripts/probe_site.py <一条资源直链> [相册页URL]   # 五项探测 + 声明草稿
python scripts/probe_site.py <直链> --smoke               # 追加"真下一张", 验能落地
python scripts/probe_site.py <直链> --json                # 落机器可读快照(drift_check 用)
python scripts/add_site.py --list                         # 看现有站点
python scripts/add_site.py --verify                       # 加站门禁
python scripts/drift_check.py                             # 站点漂移分档(一个都没查成则退出 2)
```

探测会回答五件事: 资源在哪、序号怎么编号、有没有分页/页码陷阱、要不要浏览器
指纹、视频在哪。它会**顺手产出一份声明草稿** —— 加站因此变成"改几个字段"而不是
"从零猜结构"。

⚠️ 探测带**证据门禁**: 拿不到证据的字段它会**留空**, 不编一个看起来合理的值。
宁可声明里少一个字段(后果看得见), 不要错一个字段(后果是静默出错)。

## 3. 三条必须守住的纪律

1. **`match_score` 不自写宽松解析**。复用 `gallery_base` 的 `_match_score`;
   同分时按采集器名字的字典序取 —— 否则"认领 URL"会变成两个采集器互相抢,
   而表现为"这个 URL 被一个不相干的采集器接走了"。
2. **拿不到 ID 就返回 `None`, 绝不猜**。猜错会去枚举一个不存在的图集,
   结果是"任务成功但 0 个资源" —— 这类失败最难查, 因为它不报错。
3. **形状判据取下界**。从**一条**样本生成的规则要故意写得比样本宽
   (如 `photos\d*` 而不是 `photos`), 否则站点加一个 `photos2` 就整批失效。

## 4. `check_site` 是验收标准, 不是可选的

`check_site(site)` 在**启动期**跑, `assert_site()` 会在不合规时直接抛错;
`selfcheck_all()` 一次验所有站点。它防的是一类很难查的回归:

> 新加的 `id_patterns` 与旧的对同一条 URL **各配出一个不同的 ID**, 而 `parse_gid`
> 取的是**首个命中** —— 于是后加的那条**静默改变**了已有行为(某个相册突然采空)。

所以 `id_samples` 里每条样本必须是 `(url, 期望 gid)`, 且都要求能被本站的
`id_patterns` 解析出**同一个** gid。

## 5. 加完之后

* `python scripts/selfcheck.py` —— 仓库自检;
* `pytest tests/` —— 声明自检用例会覆盖新站点;
* `python scripts/gateguard.py` —— 仓库级门禁(文档数字 / CI 契约 / 结构规则)。

改一行正则的副作用, 应该在这些地方变成**红灯**, 而不是在某个用户的下载里变成
"任务失败"。
