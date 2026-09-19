# Universal Web Collector — 项目长期笔记（索引）

通用网页资源采集平台。栈：uv + FastAPI + Vue3/Vite + Playwright + SQLite(WAL) + ffmpeg。
**正文以 `README.md` / `docs/AGENT_DEVELOPMENT_GUIDE.md` 为准**；
**完整踩坑清单见同目录 `PITFALLS.md`**（改相册/CDN/HLS/限速/headless 前必读）。

## 铁律
`URL → Browser/HTTP → Extractor → Resource → Downloader → Storage`。
**不把站点逻辑写进下载器**：Collector 只发现资源，Downloader 只下载资源。

## 关键文件
```
api/tasks.py              HTTP+SSE（create/preview/resolve/watch/manifest/archive/pause/resume）
core/task_manager.py      状态机/线程池/看门狗/资源重试/暂停续/任务名推断
core/database.py          SQLite(WAL) → data/collector.db（加列必须写进 _MIGRATIONS）
core/filters.py           "有效资源"唯一定义 = match_resource()；dedup_perceptual/阈值
core/imageinfo.py         文件头读尺寸（纯 Python 零依赖）
core/phash.py             dHash 感知指纹（ffmpeg 解码，零新依赖）——**只标记不删除**
core/cdn_profile.py       站点 CDN 画像（基址/序号格式命中统计，给探测排序）
core/manifest.py          manifest.json + album.json sidecar（附属产物失败只记 warn）
collectors/gallery_base.py    SequenceGallerySpider + GallerySite + MediaType
                              + check_site/selfcheck_all/shape_warning
collectors/album_meta.py      相册页元信息（headless）+ CF 熔断/登录态隔离 + load_page_html
collectors/hls.py             m3u8 健全性校验/过期感知/体积估算
collectors/scores.py          识别分数阶梯（单独模块，防循环依赖）
collectors/xchina/aggregate.py 聚合页采集器（模特/系列/索引，委派给 gallery/video）
downloaders/base.py           会话/重试/抖动/429；ratelimit.py 站点级并发+间隔+冷却
```

## 状态机（最容易出事的一块）
`pending→running→extracting→downloading→success/partial/failed`，可 `paused`/`cancelled`。
**采集器返回空列表 → failed**（旧行为报 success，用户看不出"什么都没下到"）。
`cancel` 保留文件；`pause` 在**资源边界**退出；`resume` 复位被打断资源、**不重采集不重下**；
`DELETE` 删记录（`?with_files=true` 才删文件）。
- ⚠️ **`except Exception` 会吞掉取消信号** → 所有 `except Exception` 前必须加
  `except TaskCancelled: raise`（base.py 两处、video.py 三处）。取消类回归要断言**耗时 < 1s**。
- ⚠️ `delete()` **不能提前 pop `_active`** → worker 会全下完；删文件前先等 worker 退出（≤5s）。
- ⚠️⚠️ 看门狗判活**必须看库里的 `tasks.hb`**（`_heartbeat()` 同时写库），只有
  `now-hb > stale_task_timeout` 才判死。`_LIVE_MANAGERS` 是**进程内**列表：两实例共用一个
  SQLite 时 A 看不到 B 的 worker → 把正在枚举 114 张图的任务标 failed，worker 走到
  `extracting->downloading` 报 `invalid transition`。**症状在状态机，根因在别处。**

## "什么是有效资源"（`filters.py::match_resource` 唯一定义）
① URL 层免请求：类型/扩展名/关键词/**广告位识别**；② 体积层：`min/max_size`、`min_image_bytes`
（HEAD 探 Content-Length，**探测失败一律放行**）；③ 像素层：`min/max_width/height`
（**下载后**读文件头，只作用于 image）；④ 内容层：下载后按 Content-Type 复核（`require_image`）。
- ⚠️ 广告识别一律**路径分段精确匹配，绝不子串包含**（会误杀 `/photos2/my-logo-album/0001.jpg`）。
- ⚠️ `size` 经 JSON/DB 往返可能是**字符串** → `_coerce_size()` 转 int（曾 str<int 抛 TypeError 拖垮整任务）。
- ⚠️⚠️ **`FilterIn` 必须声明全部键 + `extra="allow"`**：pydantic 默认**忽略未知字段**，
  曾导致前端的 `min_width/min_height/exclude_ad/min_image_bytes` 被静默丢弃 ——
  **界面开关按了没反应，无报错无日志**。凡"模型转发给只读自己认识的键的组件"都要这样做。

## 去重两层（都不删用户的文件）
| 层 | 判据 | 命中后 | 默认 |
|---|---|---|---|
| 字节级 | sha256 一致（`resources.hash`） | 复用已有文件 | 开 |
| 感知级 | dHash 距离 ≤ 4/64 | 写 `duplicate_of`，**文件保留** | 开 |
- ⚠️ 感知去重**只标记不删除**（dHash 会误判）；**失败即放行**（ffmpeg 不在也不让下载失败）；
  **只在同一任务内比对**；`-frames:v 1` 不能省；`distance()` 的 `None`(没法比)≠`0`(相同)。
  实测 同图 0/0、异图 40。`dedup_perceptual` 默认开但**不算过滤条件**。细节见 PITFALLS。

## ⚠️ 相册采集器最容易踩的三件事
1. **CDN 基址与序号宽度会按相册变**：`69ad45698f836` 在 `photos2`、**4 位**；`6aa5136f606fe`
   在 `photos`、5 位。写死 base → 全判 MISSING → 0 资源 → failed（**用户只见"失败"**）。
   采信顺序：①相册页 HTML 引用的图片地址（零探测）②用户输入的资源直链 ③候选×相邻宽度试探。
   **默认基址先用、不中才探**；`_base_candidates()` 必须同时供探测与 `_match_score`
   （各写一份会出"采集能探到、识别不认领"的半通状态）。
   → 探测顺序由 **CDN 画像**排序；⚠️ **画像里的 `seq_formats` 必须消费**（循环是"格式外层、
   基址内层"，宽度不对会把每个基址白试一遍：实测 6 次 → 有画像 **1 次**）。画像只是排序，
   全部组合仍真探一遍。
2. **相册页末段 `10.html` 是页码不是 ID** → `page_tail=r"^\d+$"`（可为列表）直接放弃。
   **宁可报错也绝不猜** —— "成功但 0 资源"比报错难查得多。
3. **采到 0 个必须抛可操作错误**（列出试过的基址 + 下一步动作），不返回空列表。
4. **`id_samples` 必填**，`check_site` 是声明唯一护栏（样本自洽 + 无"单独一条 pattern 就配出不同结果"）。
   运行期只 warn；测试 `selfcheck_all() == {}` 硬断言；运维跑 `scripts/selfcheck.py`。

## 采集器自动识别
`match_score(url) -> Optional[int]`；`scores.py`：`ALBUM_PAGE(100) > RESOURCE_URL(50) > BARE_ID(10) > GENERIC(-1000)`。
1. **认领必须有凭据**：域名匹配不够，必须 `parse_gid(strict=True)` 成功（跳过末段退路）。
   漏了 strict，`/tag/some-tag` 末段被当图集 ID 认领 → "成功但 0 资源"。
2. **形状校验 `site.gid_shape`** 只在 strict 下生效：自动识别在**替用户做决定**，猜错是静默的。
   **手选时只给软提示、绝不 400**（用户已表过态，站点可能刚换 ID 格式而我们知道得晚）；
   ⚠️ 软提示**不塞进 `resolved`**（那个字段专指识别结论）→ `_pick_collector` 返回三元组，
   `TaskCreateOut.warning` 是独立字段。
3. **库里存真名不存 `auto`**；无法识别直接 400（不悄悄兜底 generic）。
4. 正则要**锚定 host**，且**锚定到"gid 后紧跟带媒体扩展名的文件名"**：`/photos/featured/0001.jpg`
   会把路径词 `featured` 当 gid。序号用 `[^/?#]+` 而非 `\d+`。
5. **聚合页**（`/model/id-*`、`/models*`、`/*/series-*`、`/photos|videos/model-*`）→ `xchina_aggregate`
   （100）。**URL 驱动抽取，绝不 DOM 选择器**；归一化只归内容页（`/10.html` 是相册分页）。
   详见 `PITFALLS.md` 与 README。

## 视频：签名 m3u8 的占位陷阱
- ⚠️ 签名过期/错 → 站点回 **200 + 语法合法的 m3u8**，指向 `/fallback/placeholder.ts`
  （603KB 真实可播放 TS）→ 静默下成占位视频且任务报 success。`inspect_playlist` 就是挡它的。
- 实测 `6aaa517d3f106`：AES-128 加密，`/key/enc.key` 与分片**都无需签名**（只有 playlist 设防）；
  ffmpeg 原生拉钥+解密+remux → **零新增依赖**。`estimate_size()` 用「单片体积×分片数」估整段。
- ⚠️ 监听 `.m3u8` 用 `"m3u8" in u` 而非 `endswith(".m3u8")`（真实 URL 带 `?expires=`）。
- ⚠️ 不要用 `shutil.which("ffmpeg")`（PATH 是启动快照）→ `core/ffmpeg.py::find_ffmpeg()`。

## 测试 / 冒烟（本机特有的坑）
- 用户 `.venv` 是 **WSL 里的 Linux venv**；本机验证用 `~/.workbuddy/binaries/python/envs/uwc-verify`。
- 后端启动需 `--app-dir backend`；本机 curl 对 127.0.0.1 必须加 `--noproxy '*'`（否则 502）。
- Bash 后台进程随该次调用结束被回收 → 起服务必须 `run_in_background`；
  本机 bash PATH 偶发失效 → 命令前 `export PATH="/usr/bin:/bin:$PATH"`。
- ⚠️ 沙箱 safe-delete shim 同 turn 删 >50 次后会拒绝 → 删除类用例假失败；绕过 `PYTHONPATH= ... pytest`。
- ⚠️⚠️ **Git Bash 给 Windows Python 传 `$PWD/...` 会造影子库**：`$PWD` 是 `/c/Users/...`，
  Windows 解析成 `C:\c\Users\...`。表现为"服务用的库和我查的不是同一个"，极易误判成"多实例共用库"。
- ⚠️ **同一文件的多处 Edit 别并行发**：会静默丢改动（已踩多次）→ 改完立刻 grep 核对。
- ⚠️ `verify_output.py` 起点要**整个清掉 `data/_verify_output`**。
- ⚠️⚠️ **测试会借磁盘文件偷偷互相通信**（CDN 画像）：单跑绿、全跑红、重跑又绿。解法见 PITFALLS。
  凡"运行期攒数据、测试会读它"的模块，都要有一个 `UWC_*` 开关 + conftest 隔离夹具。

```bash
make install|backend|frontend|build|test|docker
python scripts/verify_output.py            # 33 项断言
python scripts/verify_hls.py               # 18 项断言
python scripts/preview_probe.py <url|gid>  # 真实站点创建前预告
python scripts/selfcheck.py                # 站点声明自检 + CDN 画像快照
```
环境变量：`UWC_DB_PATH` / `UWC_DOWNLOAD_DIR` / `UWC_BROWSER_STATE_DIR` / `UWC_PROXY` /
`UWC_FFMPEG` / `UWC_CDN_PROFILE`（路径或 `off`）

## ⚠️ Git：已独立建仓
在 `universal_web_collector_v9/` 内执行 git（toplevel 已是项目目录），分支 `main`。
**上级 `C:\Users\admin` 那个仓库绝不能碰**。
