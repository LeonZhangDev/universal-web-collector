# Universal Web Collector — 长期笔记（索引）

栈：uv + FastAPI + Vue3/Vite + Playwright + SQLite(WAL) + ffmpeg。
**正文以 `README.md` / `docs/AGENT_DEVELOPMENT_GUIDE.md` 为准；坑的完整清单（实例 + 修法）见 `PITFALLS.md`。**
本文件只做三件事：**索引 + 本机操作契约 + 一眼能认出的坑名**。
> 维护规则：**新坑先写进 `PITFALLS.md`，这里只加一行索引**；版本史只留"做了什么 + 顺带抓出第几条 + 测试数"。否则会重新长到被截断（09-23、09-24 各溢过一次）。

## 铁律 / 落盘
`URL → Browser/HTTP → Extractor → Resource → Downloader → Storage`；站点逻辑不进下载器。
布局（唯一入口 `core/layout.py`）：`downloads/相册名/图片`；**视频平铺到 downloads 根**（网站原名，m3u8 用 `gid.mp4`）。
重名：异源→`name(2).mp4`；相册视频撞名→`0001_相册名.mp4`；同源复用不重下。清单在 `downloads/_meta/{任务ID}/`。
⚠️ 采集器的 `album_tags_dir`（标签层）已废弃 —— 别引回来，留着就是"按了没反应"。

## 关键文件
`api/tasks.py`(HTTP+SSE) · `core/task_manager.py`(状态机/看门狗/下载顺序) · `core/database.py`(加列**必须同时**进 SCHEMA 与 `_ADD_COLUMNS`)
`core/filters.py`(`match_resource` = "有效资源"唯一定义) · `core/errors.py`(`classify`/`KIND_*`/`KIND_LABELS`) · `core/layout.py` · `core/disk.py`
`core/phash.py`(只标记不删) · `core/mediacheck.py`(完整性**算术**判据) · `core/manifest.py` · `core/thumbs.py`(按 **sha** 不按路径)
**`core/jsonstore.py` = JSON 状态存储唯一实现**（`cdn_profile`/`proxy_health` 共用四条并发纪律，**别手写第二遍**）
`core/partials.py`(续传暂存区：**按 URL 寻址**) · `downloaders/dash.py`(MPD 解析，纯函数不碰网络) · `downloaders/video.py`(**HEAD 是 CRLF**)
`collectors/gallery_base.py`(声明式契约；`check_site` = 声明唯一验收标准) · `collectors/hls.py` · `downloaders/base.py` · `downloaders/ratelimit.py`(请求桶 + **全局字节桶**) · `main.py`(lifespan)
`tests/isolation.py`(隔离清单 + 真实库即时守卫) · `tests/conftest.py`
`scripts/probe_site.py`(**新站点五项探针** + 声明草稿 + **三档证据门禁**；**回归**靶子=本地假站点、**找 bug** 靶子=真实站点) · `scripts/probe.py`(浏览器探针，两码事)

## ⚠️ 静默坑索引（1–18 + 同族 A–G；细节见 `PITFALLS.md`）
**共同特征：不报错，只是结果错 —— 所以会在真实使用中活很久。**

| # | 一句话 | 判据 / 触发器 |
| --- | --- | --- |
| 1 | 次序即契约 | 先 `_write_manifest` 再 `_settle_status`；后者**不得抛** |
| 2 | 原子落盘 | `.part`+`.partsrc`+`os.replace`；**`.partsrc` 记来源 URL**，没它会拼出"恰好等长"的混合文件 |
| 3 | 单写者 | 孤儿恢复挂 **lifespan**（别放模块级）；SQLite 写-写 → `busy_timeout`+`_retry_write` |
| 4 | 取消穿透 | 每个 `except Exception` 前必须 `except TaskCancelled: raise`（**AST 门禁**） |
| 5 | pydantic 吞字段 | 新增字段（`FilterIn`/`ResourceOut`…）必须**显式声明**（+ `extra="allow"`） |
| 6 | 重名须先消解 | `layout.claim`；"目标已存在"当**半成品**续传 |
| 7 | 前端传"代号/意图" | 取值不在库内必须**显式映射**，不能直接拼 SQL（多一个词就 0 条） |
| 8 | 认领 URL 是共享资源 | 复用 `gallery_base._match_score`；同分按**采集器名字字典序** |
| 9 | 把"人看的文案"当数据 | 归类改用 `error_kind`；⚠️ 中文标签由后端下发 |
| 10 | `except OSError: pass` | 盖在"本来就会失败"的写入上 = 把丢数据改装成静默（两问） |
| 11 | 引用不存在的接口 | 前端引用 `/files/raw` 而它**从没实现过** → 404 被 `onerror` 藏掉 |
| 12 | 流式响应不关 = 连接泄漏 | 一条 404 漏一个连接 → 池满后所有 worker 阻塞，**任务卡死不报错** |
| 13 | 形参被局部变量遮蔽 | `_download_m3u8` 的 `info` 被预检结果顶掉 → HLS `resolved_url` 恒缺 |
| 14 | 测试里"顺手调真实入口" | `POST /tasks/create` 会起**真 worker**，它给**下一个用例的库**写心跳 |
| 15 | 登记成"已处理"又拿"未处理"去筛它 | 直播 `absorb()` 把 init 塞进 `seen`，而 `seen` 正是过滤器 → init 永不下 |
| 16 | 从**一条**样本生成规则 | 生成层要比样本**宽**（`photos` → `photos\d*`）；形状判据取更宽的**下界** |
| 17 | 序号后跟**内容哈希** ≠ 序号枚举型 | `/data/<hash>/1-<sha256>.png` 改序号必然 404。看残留熵，**不看**"是否以数字开头" |
| 18 | 测试断言的是"**我想到的行**" | 产出类改动的最后一步是**目视完整输出**：正则重复、截断方向只有看才发现 |
| A | 条件请求 ⊥ 续传 | 有 `Range` 不能带 `If-None-Match`（回 304 而非 206） |
| B | 隔离的窗口期 | `monkeypatch.undo()` 还原 `DB_PATH` → 谁在那时碰库就写**用户真库** |
| C | 素材/断言的前提会失效 | 产品加了校验或能力 → 回头问 fixture 与旧断言（**也包括文档声称**） |
| D | 测试基线会漂 | 会话间"自动提交"会合并别处分支 → 别拿旧用例数当事实 |
| E | 测试替身写死签名 | 抽出通用函数时旧的 `lambda *a:` 会立刻炸 —— **替身不诚实**，不是回归 |
| F | **通过但理由已经不对** | 新代码在**另一条**判据上报错、或在更早一步就炸。比失败更危险 |
| G | 静默降级必须有痕 | `park*` 搬不动时不抛错，但"怎么永远攒不起来"得有人知道 → `stats()['last_error']` |

**七条通用心法**（比记具体条目重要）：
① 加一个状态，就有多处**口径**要同步（`_final_status`/`summarize_resources`/前端计数）。
② 凡"静默"处都问：**失败会发生吗？失败之后有人知道吗？** 两个都否 → 加重试或留痕。
③ **误报的代价**决定判据松紧：只下"能被证明"的结论（`mediacheck` 认不出的容器一律放行）。
④ **新功能接完，回头验旧的前提**（fixture / 断言 / 文档声称），别改产品去迁就旧的。
⑤ **卡死类问题第一动作是打调用栈**，不是猜 —— `faulthandler.dump_traceback_later(N, exit=True)`。
⑥ 写测试时问：**这条路径会不会起后台线程？**（第 14 条）
⑦ **补丁 vs 规则**：同一个错出现第三遍，就把它抽成程序可执行的规则（第 18 条、V39 的门禁）。

## 状态机 / 速度 / 去重
`pending→running→extracting→downloading→success/partial/failed`，可 paused/cancelled；采集器返回空 → failed。
cancel 留文件；pause 在资源边界退出；resume 不重采不重下。看门狗判活**必须看库里的 `tasks.hb`**（`_LIVE_MANAGERS` 只是进程内列表）；`delete()` 不能提前 pop `_active`。
资源级终态 `gone`（源站 404/410）**自动不重试**、手动可放行。规格不匹配报 400 而不是静默忽略。
吞吐由 **`domain_min_interval`** 决定，不由并发决定；令牌桶只改突发。AIMD 只按成功与否，**不看延迟**。
站点级并发 `DomainLimiter` 按 `site_key` 分组（默认 3）。枚举快路径：页面给数量 → 抽样校验 → 跳过逐张探测（117→6 次）。
去重两层都不删文件：sha256 一致复用；dHash ≤ 4 写 `duplicate_of` 保留。

## 本机验证
`~/.workbuddy/binaries/python/envs/uwc-verify`（用户 `.venv` 是 WSL 的）。后端要 `--app-dir backend`；curl 对 127.0.0.1 加 `--noproxy '*'`；起服务用 `run_in_background`。
⚠️ 别把 Git Bash 的 `$PWD/...` 传给 Windows Python（造影子库）；同一文件多处 Edit **不要并行发**。
⚠️ 本机 venv 已补装 `curl-cffi`；全量 **1086 条全绿**（1080 passed / 6 skipped）。
⚠️ **`img.xchina.io` 从本机整段 403**（`text/plain`，不是 CF 挑战页；裸 curl + 浏览器 UA、`impersonate=chrome` 都一样）→ 这条网络的问题，**不是站点改版、不是回归**。别拿它当靶子。
⚠️ 别前置 `export PATH="/usr/bin:/bin:$PATH"` —— 会把裸 `python` 换成 3.13.12（没 pytest）。coreutils 全无（`cat`/`grep`/`tail`），但 `echo`、`git`、`python` 可用。
⚠️ 宿主包装器 `windows-child-process-containment.cjs` 偶发缺失 → 长命令直接 `MODULE_NOT_FOUND`（**压根没跑**）；加 `run_in_background=true` 绕过。
⚠️ **`exit 1` ≠ 有失败**：safe-delete 守卫拦"清空大目录"（阈值 50），三个实例（`verify_output.py` 的 `rmtree` / `vite build` 的 `emptyDir` / pytest 清 `tmp_path`）→ 无 `FAILED` 行、**连汇总行都没有**。判据：看进度行有没有 `F`。
⚠️ 被守卫 kill 时重定向到文件的那份输出会丢 → 脚本要 `python -u`；想拿失败清单先 `--collect-only -q`（`pytest-randomly` 没装）再按 `F` 的**列位置**反查。
⚠️ 想拿到 pytest 汇总行：`--basetemp` 指**系统临时目录下一个还不存在的路径**（`.../Temp/uwcpt-$$`）。
⚠️⚠️ **绝不把 `--basetemp` 指进项目目录**：垫片清 basetemp 抛 `OSError [Errno 53]` 且 FAIL_CLOSED → **每个用例 setup 都 ERROR**（实测 986 个）。**第二遍才炸**。
⚠️ **行尾**：`Path.write_text()` 在 Windows 上把 `\n` 翻成 `\r\n` → 被 Python 改写过的文件**整份变 CRLF**。提交前必查；用 `newline=""` 或 `write_bytes`。
⚠️ `downloaders/video.py` 与 `scripts/verify_output.py` 的 **HEAD 本来就是 CRLF**，别去"统一"。

```bash
make install|backend|frontend|build|test|docker
python scripts/verify_output.py   # 40 项
python scripts/verify_hls.py      # 18 项
python scripts/selfcheck.py       # 站点声明自检 + CDN 画像快照
python scripts/probe_site.py <资源直链> [相册页URL]   # 新站点五项探测 + 声明草稿
```
环境变量：`UWC_DB_PATH` / `UWC_DOWNLOAD_DIR` / `UWC_BROWSER_STATE_DIR` / `UWC_PROXY` / `UWC_FFMPEG` /
`UWC_CDN_PROFILE` / `UWC_PROXY_HEALTH` / `UWC_MAX_BPS`（`5MB`/`512k` 写法）/ `UWC_PARTIAL_STAGING`(off 可关) /
`UWC_PARTIAL_TTL_HOURS` / `UWC_PARTIAL_MAX_BYTES` / `UWC_PARTIAL_MIN_BYTES` / `UWC_LIVE_MAX_SECONDS`(直播录制上限, 0=不限时)

## Git / 远端
独立建仓（toplevel = 项目目录），分支 `main`。**上级 `C:\Users\admin` 那个仓库绝不能碰**。
⚠️ 本机 Bash 极简：`git -C <绝对路径>` 解析失败 → 用默认 cwd；提交用多个 `-m` 或 `git commit -F <文件>`（`\n` 不会被展开）。
⚠️ **会话间"自动提交"会移动 HEAD** → 每轮先 `git rev-parse HEAD` + `git ls-remote origin refs/heads/main`，别拿上轮记的 SHA 当事实。
⚠️ **CI 跑的是 `origin/main`，不是本地 HEAD**；"本地全绿 / CI 全红"第一条命令是 `git log --oneline origin/main..HEAD`。
- 先试 `git push origin main`（能通的次数在变多）；只在真报 `502`/`000`/`CONNECT tunnel failed` 时降级用 Git Data API（skill `git-push-via-rest-api`）。两种都**别在没核对时 `--force`**；推完用 `git ls-remote` 与本地 `rev-parse HEAD` 对一次。
- 远端 <https://github.com/LeonZhangDev/universal-web-collector>；本机**无 `gh` CLI** → `printf "protocol=https\nhost=github.com\n\n" | git credential fill` 取 `gho_` token。
- CI `.github/workflows/ci.yml` 三 job：`backend`(`uv sync --group dev`+`pytest`) / `frontend`(`npm install`+`build`) / `docker`。看日志 `GET /repos/{o}/{r}/actions/jobs/{id}/logs`（纯文本）。
- 依赖别漏：`curl-cffi`（主依赖）、`httpx2`（dev 组，`starlette.testclient` 必需）。漏了是**测试 collection 阶段整片红**。
- 相关 skill：`verification-red-triage`、`github-ci-failure-triage`、`git-push-via-rest-api`、`downloader-output-layout`、`graceful-cancel-worker`、`gallery-site-probe`。

## 版本史（细节见各版 commit / README / `PITFALLS.md`）

| 版 | 一句话 | 测试 |
| --- | --- | --- |
| V28–V31 | 落盘重构 / 批量创建与搜索分页 / 批量操作与统计面板 / 代理池联动 + 通知中心 | — |
| V32 | 代理熔断 · 字节进度 · 跨任务资源库 · Pexels 采集器；首次推送远端 | 587 |
| V33 | 4xx→`gone` · `mediacheck` 算术判据 · 解码失败分流 · `cdn_profile` 并发丢更新 | 819 |
| V34 | 八项 backlog（遥测/缩略图/条件请求/限速/巡检/熔断持久化…）+ 修掉"接口从不存在" | 880 |
| V35 | 断点续传 `partials`（**按 URL 寻址**）· DASH · 标签与收藏；修第 12 条 | 942 |
| V36 | 分片缓存复用（按**清单 URL**寻址 + 指纹）· 标签层级 · DASH 字节区间；修第 13 条 | 997 |
| V37 | 三条**被静默丢掉的建议**（元数据 / 下载顺序 / 死信重放）；抓出第 14 条 | — |
| V38 | 嵌套 `sidx` · 直播录制（⚠️ 取消时**仍封文件**）；抓出第 15 条 | 1063 |
| V39 | 新站点探针：五项探测 → **真实站点当靶子** → **三档证据门禁**；抓出第 16/17/18 条 | 1075 → 1082 → **1086** |

- **V38 复核要点**：当初写的拒绝理由（"真实站点几乎不出现 / 与产物模型冲突"）**站不住** —— **"暂时不做"不许写成"不该做"。**
- **V39 的关键判断**：**加站的成本在探测不在写声明**（声明本体约 70 行、基类零改动）；而"加站"多数时候真正的瓶颈是**可达性**（图集/漫画站多在 CF 后面）。
- **下一步候选**：更多站点插件（**按需求，先跑 `probe_site.py`**）/ HLS 直播（`EVENT` 或无 `ENDLIST`，可复用 `_record_live` 骨架，缺真站样本）/ 直播断点续录（低）。
