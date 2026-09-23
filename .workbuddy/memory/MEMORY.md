# Universal Web Collector — 长期笔记（索引）

栈：uv + FastAPI + Vue3/Vite + Playwright + SQLite(WAL) + ffmpeg。
**正文以 `README.md` / `docs/AGENT_DEVELOPMENT_GUIDE.md` 为准；踩坑的完整清单（实例 + 修法）见 `PITFALLS.md`。**
本文件只做三件事：**索引 + 本机操作契约 + 一眼能认出的坑名**。
> 维护规则：**新坑先写进 `PITFALLS.md`，这里只加一行索引** —— 否则本文件会重新长到被截断（2026-09-23 溢过一次）。

## 铁律 / 落盘
`URL → Browser/HTTP → Extractor → Resource → Downloader → Storage`；站点逻辑不进下载器。
布局（唯一入口 `core/layout.py`）：`downloads/相册名/图片`；**视频平铺到 downloads 根**（网站原名，m3u8 用 `gid.mp4`）。
重名：异源→`name(2).mp4`；相册视频撞名→`0001_相册名.mp4`；同源复用不重下。清单在 `downloads/_meta/{任务ID}/`。
⚠️ 采集器的 `album_tags_dir`（标签层）已废弃 —— 别引回来，留着就是"按了没反应"。

## 关键文件
`api/tasks.py`(HTTP+SSE) · `core/task_manager.py`(状态机/看门狗/下载顺序) · `core/database.py`(加列**必须同时**进 SCHEMA 与 `_ADD_COLUMNS`；`_migrate` 幂等)
`core/filters.py`(`match_resource` = "有效资源"唯一定义) · `core/errors.py`(`classify`/`KIND_*`/`KIND_LABELS`) · `core/layout.py` · `core/disk.py`
`core/phash.py`(只标记不删) · `core/mediacheck.py`(完整性**算术**判据) · `core/manifest.py` · `core/thumbs.py`(按 **sha** 不按路径)
**`core/jsonstore.py` = JSON 状态存储唯一实现**（`cdn_profile`/`proxy_health` 共用四条并发纪律，**别手写第二遍**）
`core/partials.py`(续传暂存区：**按 URL 寻址**) · `downloaders/dash.py`(MPD 解析，纯函数不碰网络) · `downloaders/video.py`(**HEAD 是 CRLF**)
`collectors/gallery_base.py` · `collectors/hls.py` · `downloaders/base.py` · `downloaders/ratelimit.py`(请求桶 + **全局字节桶**) · `main.py`(lifespan)
`tests/isolation.py`(隔离清单 + 真实库即时守卫) · `tests/conftest.py`

## ⚠️ 静默坑索引（1–14 + 同族 A–G；细节见 `PITFALLS.md`）
**共同特征：不报错，只是结果错 —— 所以会在真实使用中活很久。**

| # | 一句话 | 判据 / 触发器 |
| --- | --- | --- |
| 1 | 次序即契约 | 先 `_write_manifest` 再 `_settle_status`；后者**不得抛** |
| 2 | 原子落盘 | `.part`+`.partsrc`+`os.replace`；**`.partsrc` 记来源 URL**，没它会拼出"恰好等长"的混合文件 |
| 3 | 单写者 | 孤儿恢复挂 **lifespan**（别放模块级）；SQLite 写-写 → `busy_timeout`+`_retry_write` |
| 4 | 取消穿透 | 每个 `except Exception` 前必须 `except TaskCancelled: raise`（**AST 门禁**） |
| 5 | pydantic 吞字段 | 新增字段（`FilterIn`/`ResourceOut`…）必须**显式声明**（+ `extra="allow"`），否则静默消失 |
| 6 | 重名须先消解 | `layout.claim`；"目标已存在"当**半成品**续传，要本次采集内的占位表 |
| 7 | 前端传"代号/意图" | 取值不在库内必须**显式映射**，不能直接拼 SQL（多一个词就 0 条） |
| 8 | 认领 URL 是共享资源 | 复用 `gallery_base._match_score`；同分按**采集器名字字典序** |
| 9 | 把"人看的文案"当数据 | 归类改用 `error_kind`；⚠️ 中文标签由后端下发 |
| 10 | `except OSError: pass` | 盖在"本来就会失败"的写入上 = 把丢数据改装成静默（两问） |
| 11 | 引用不存在的接口 | 前端引用 `/files/raw` 而它**从没实现过** → 404 被 `onerror` 藏掉 |
| 12 | 流式响应不关 = 连接泄漏 | 一条 404 漏一个连接 → 池满后所有 worker 阻塞，**任务卡死不报错** |
| 13 | 形参被局部变量遮蔽 | `_download_m3u8` 的 `info` 被预检结果顶掉 → HLS `resolved_url` 恒缺 |
| 14 | 测试里"顺手调真实入口" | `POST /tasks/create` → 全局 `task_manager.submit()` 起**真 worker**；用例结束后它给**下一个用例的库**写心跳（id 都从 1 开始） |
| A | 条件请求 ⊥ 续传 | 有 `Range` 不能带 `If-None-Match`（回 304 而非 206，收尾路径永远走不到） |
| B | 隔离的窗口期 | `monkeypatch.undo()` 还原 `DB_PATH` → 谁在那时碰库就写**用户真库** |
| C | 素材/断言的前提会失效 | 产品加了校验或能力 → 回头问 fixture 与旧断言（**也包括文档声称**） |
| D | 测试基线会漂 | 会话间有"自动提交"会合并别处分支 → 别拿旧用例数当事实 |
| F | **通过但理由已经不对** | 新代码在**另一条**判据上报错、或在更早一步就炸。比失败更危险 |
| G | 静默降级必须有痕 | `park*` 搬不动时不抛错，但「怎么永远攒不起来」得有人知道 → `stats()['last_error']` |
| E | 测试替身写死签名 | 抽出通用函数时旧的 `lambda *a:` 会立刻炸 —— 那是**替身不诚实**，不是回归 |

**六条通用心法**（比记具体条目重要）：
① 加一个状态，就有多处**口径**要同步（`_final_status`/`summarize_resources`/前端计数）。
② 凡"静默"处都问：**失败会发生吗？失败之后有人知道吗？** 两个都否 → 加重试或留痕。
③ **误报的代价**决定判据松紧：只下"能被证明"的结论（`mediacheck` 认不出的容器一律放行）。
④ **新功能接完，回头验旧的前提**（fixture / 断言 / 文档声称），别改产品去迁就旧的。
⑤ **卡死类问题第一动作是打调用栈**，不是猜 —— `faulthandler.dump_traceback_later(N, exit=True)`。
⑥ **写测试时问：这条路径会不会起后台线程？**（第 14 条的教训）

## 状态机 / 速度 / 去重
`pending→running→extracting→downloading→success/partial/failed`，可 paused/cancelled；采集器返回空 → failed。
cancel 留文件；pause 在资源边界退出；resume 不重采不重下。看门狗判活**必须看库里的 `tasks.hb`**（`_LIVE_MANAGERS` 只是进程内列表）；`delete()` 不能提前 pop `_active`。
资源级终态 `gone`（源站 404/410）**自动不重试**、手动可放行。规格不匹配报 400 而不是静默忽略。
吞吐由 **`domain_min_interval`** 决定，不由并发决定；令牌桶只改突发。AIMD 只按成功与否，**不看延迟**。
站点级并发 `DomainLimiter` 按 `site_key` 分组（默认 3）。枚举快路径：页面给数量 → 抽样校验 → 跳过逐张探测（117→6 次）。
去重两层都不删文件：sha256 一致复用；dHash ≤ 4 写 `duplicate_of` 保留。

## 本机验证
`~/.workbuddy/binaries/python/envs/uwc-verify`（用户 `.venv` 是 WSL 的）。后端要 `--app-dir backend`；
curl 对 127.0.0.1 加 `--noproxy '*'`；起服务用 `run_in_background`。
⚠️ 别把 Git Bash 的 `$PWD/...` 传给 Windows Python（造影子库）；同一文件多处 Edit **不要并行发**。
⚠️ 本机 venv 缺主依赖 `curl-cffi` → 4 条测试红（`test_downloaders` ×3 / `test_gallery_preview` ×1）是**环境问题不是回归**。
⚠️ **别前置 `export PATH="/usr/bin:/bin:$PATH"`** —— 会把裸 `python` 换成交管版 3.13.12（没 pytest）。coreutils 全无（`cat`/`grep`/`tail`/`dirname`），但 `echo`、`git`、`python` 可用。
⚠️ 宿主包装器 `windows-child-process-containment.cjs` 偶发缺失 → 跑得久的命令直接 `MODULE_NOT_FOUND`（**压根没跑**）；**加 `run_in_background=true` 绕过**。
⚠️ **`exit 1` ≠ 有失败**：safe-delete 守卫拦"清空大目录"（阈值 50），现有三个实例：`verify_output.py` 的 `rmtree` / `vite build` 的 `emptyDir` / **pytest 收尾清 `tmp_path`** → 无 `FAILED` 行、**连汇总行都没有**。判据：看进度行有没有 `F`。
⚠️ **行尾**：`Path.write_text()` 在 Windows 上把 `\n` 翻成 `\r\n` → 被 Python 改写过的文件**整份变 CRLF**（diff 71 行炸到 415）。提交前必查；用 `newline=""` 或 `write_bytes`。
⚠️ `downloaders/video.py` 与 `scripts/verify_output.py` 的 **HEAD 本来就是 CRLF**，别去"统一"。

```bash
make install|backend|frontend|build|test|docker
python scripts/verify_output.py   # 40 项
python scripts/verify_hls.py      # 18 项
python scripts/selfcheck.py       # 站点声明自检 + CDN 画像快照
```
环境变量：`UWC_DB_PATH` / `UWC_DOWNLOAD_DIR` / `UWC_BROWSER_STATE_DIR` / `UWC_PROXY` / `UWC_FFMPEG` /
`UWC_CDN_PROFILE` / `UWC_PROXY_HEALTH` / `UWC_MAX_BPS`（`5MB`/`512k` 写法）/ `UWC_PARTIAL_STAGING`(off 可关) /
`UWC_PARTIAL_TTL_HOURS` / `UWC_PARTIAL_MAX_BYTES` / `UWC_PARTIAL_MIN_BYTES`

## Git / 远端
独立建仓（toplevel = 项目目录），分支 `main`。**上级 `C:\Users\admin` 那个仓库绝不能碰**。
⚠️ 本机 Bash 极简：`git -C <绝对路径>` 解析失败 → 用默认 cwd；提交用多个 `-m`（`\n` **不会**被展开，要真换行就 `git commit -F <文件>`）。
⚠️ **会话间"自动提交"会移动 HEAD** → 每轮先 `git rev-parse HEAD` + `git ls-remote origin refs/heads/main`，别拿上轮记的 SHA 当事实。
⚠️ **CI 跑的是 `origin/main`，不是本地 HEAD**；"本地全绿 / CI 全红"第一条命令是 `git log --oneline origin/main..HEAD`。
- **先试 `git push origin main`**（能通的次数在变多）；只在真报 `502`/`000`/`CONNECT tunnel failed` 时降级用 Git Data API（skill `git-push-via-rest-api`）。两种都**别在没核对时 `--force`**；推完用 `git ls-remote` 与本地 `rev-parse HEAD` 对一次。
- 远端 <https://github.com/LeonZhangDev/universal-web-collector>；本机**无 `gh` CLI** → `printf "protocol=https\nhost=github.com\n\n" | git credential fill` 取 `gho_` token。
- CI `.github/workflows/ci.yml` 三 job：`backend`(`uv sync --group dev`+`pytest`) / `frontend`(`npm install`+`build`) / `docker`。看日志 `GET /repos/{o}/{r}/actions/jobs/{id}/logs`（纯文本不是 zip）。
- 依赖别漏：`curl-cffi`（xchina TLS 伪装，**主依赖**）、`httpx2`（dev 组，`starlette.testclient` 必需）。漏了不是导入期报错，而是**测试 collection 阶段整片红**。
- 相关 skill：`verification-red-triage`、`github-ci-failure-triage`、`git-push-via-rest-api`、`downloader-output-layout`、`graceful-cancel-worker`、`gallery-site-probe`。

## 版本史（压缩；细节见各版 commit 与 README）
- **V28** 落盘重构 · **V29** 批量创建/搜索分页/环境诊断 + 前端改版 · **V30** 批量操作/统计面板/代理字段 · **V31** 代理池联动下载层 + 通知中心 + SVG 图表。
- **V32**：代理熔断 / 字节级进度 / 跨任务资源库 / Pexels 采集器；首次配远端并推送成功。测试 587。
- **V33**：下载层四缺陷 —— 4xx 分流（`PERMANENT_STATUS`→`GoneError`，新状态 `gone`）/ `mediacheck` 算术判据 / `decode_gray_ex` 分开"缺解码器"与"解码失败" / `cdn_profile` 并发丢更新。测试 819。
- **V34**：八项 backlog —— 资源遥测 / 缩略图 + `/files/raw` / 条件请求 / 字节限速 / 资源库批量 / 完整性巡检 / 熔断持久化 / Pexels 集合页；修掉"接口从不存在"与隔离窗口期。测试 880。前端 90 modules / 209.35 kB。
- **V35**：断点续传持久化（`core/partials.py`，**按 URL 寻址** + TTL/预算 + 启动清扫）/ DASH(.mpd) / 资源库标签与收藏。**顺带修掉**：`_fetch_segments` 不关响应 → 404 漏连接 → 池(10)漏满后任务卡死（第 12 条）。**文档复核抓出 3 处谎报**（站点并发配额 / 巡检落库 / 熔断"只在内存"）。测试 942。
- **V36**：分片缓存跨任务复用（`partials` 扩 `kind=segments`，按**清单 URL** 寻址 + **指纹**校验）/ 标签层级与颜色（层级 = 名字里的 `/`，零新表；调色板后端下发）/ DASH 字节区间与多时段（`parse_sidx` 按 §8.16.3，**拿 ffmpeg 真吐的 sidx 钉答案**；多时段**逐轨** `-f concat`，顶层 `video`/`audio` 置 `None` 防静默截断）。**顺带修掉**：第 13 条形参遮蔽、`park` 失败无痕。测试 997。前端 90 modules / 216.05 kB。
- **V37**：三条**被静默丢掉的建议**（既不在 backlog 也没实现，靠逐条读代码才发现）——① 媒体元数据落库（宽高/时长；**复用已有探测**，不新增 ffprobe；"没测量"≠0）；② 下载顺序（`order_resources` 纯函数重排**提交序**；**排序不是过滤**）；③ 跨任务死信重放（`_failure_where` 单一定义 + 两数字 `n`/`replayable`；复用 `submit_resource` 唯一入口）。**顺带抓出**第 14 条（测试点火真 worker）。
- **下一步候选**（详见 `docs/PROJECT_OVERVIEW.md`「后续可做」）：更多站点插件（按需求）/ `sidx` 嵌套索引 / 直播（`type="dynamic"`）—— 后两条都标"低"。
