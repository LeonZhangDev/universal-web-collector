<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import FolderPicker from "./components/FolderPicker.vue";
import TaskDetail from "./components/TaskDetail.vue";
import TaskTable from "./components/TaskTable.vue";
import {
  bulkDeleteTasks,
  createTask,
  createWatch,
  deleteSession,
  deleteTask,
  deleteWatch,
  getCollectors,
  getConfig,
  getLoginJob,
  getStorageOverview,
  listSessions,
  listTasks,
  listWatches,
  pauseTask,
  previewTask,
  runWatch,
  resolveCollector,
  resumeTask,
  startLogin,
  stopLogin,
  toggleWatch,
} from "./api";

const tasks = ref([]);
const selectedId = ref(null);
const url = ref("");
const collector = ref("auto");
// 后端列表不含 "auto", 由前端补在最前 —— 它是个"让后端挑"的选择, 不是采集器
const collectors = ref(["auto"]);
// 采集器中文名; 后端只给代号, 展示层做映射, 未收录的回退显示代号本身
const collectorLabel = {
  auto: "自动识别 (推荐)",
  generic: "通用网页(浏览器抓取)",
  xchina: "XChina 页面(浏览器抓取)",
  xchina_gallery: "XChina 图集/视频相册(相册 ID / 相册页 URL 均可)",
  xchina_video: "XChina 视频页(自动过 Cloudflare 抓带签名 m3u8)",
  xchina_aggregate: "XChina 聚合页(模特 / 演员 / 系列 / 索引页, 展开出多个相册)",
};
// 自动识别的结论。**必须回显给用户并可覆盖** —— 悄悄生效的自动识别, 一旦
// 猜错用户连"该去哪里改"都无从下手, 只会以为站点坏了。
const resolved = ref(null);
// 实际会用的采集器: 手动指定 > 已识别 > generic(让后端报错)
const effectiveCollector = computed(() => {
  if (collector.value !== "auto") return collector.value;
  return resolved.value?.collector || "generic";
});
// 不同采集器对输入的要求不同, 提示语跟着切换
const urlPlaceholder = computed(() =>
  collector.value === "auto"
    ? "粘贴任意链接或图集 ID, 自动识别采集器(相册页 / 视频页 URL / 图片直链 / 6aa113208a506 均可)"
    : effectiveCollector.value === "xchina_gallery"
      ? "相册 ID(6aa113208a506) / 相册页 URL(https://xchina.co/photo/id-XXX.html) / 任意一张图片或视频 URL 都行"
      : effectiveCollector.value === "xchina_video"
        ? "视频页 URL(https://xchina.co/video/id-XXX.html) 或带签名的 m3u8 直链"
        : effectiveCollector.value === "xchina_aggregate"
          ? "聚合页 URL: 模特/演员 /model/id-XXX.html、索引 /models.html、全量列表 /videos/model-XXX.html"
          : "输入采集 URL, 例如 https://example.com/photoShow.html?id=xxx"
);
const creating = ref(false);
const errorMsg = ref("");
let timer = null;
let es = null;

// ---- 下载目录 ----
const defaultDir = ref("");
const downloadDir = ref("");
const showPicker = ref(false);

// ---- 图集画质档(仅图集采集器有效) ----
// 站点通常为同一张图提供多档尺寸, 低档位体积可省约 80% 而肉眼几乎无差。
// 未被选中的档位会作为备用下载点, 主档位失败时自动切换。
const qualities = ref(["original", "1200", "800", "600"]);
const quality = ref("original");
const isGallery = computed(() => effectiveCollector.value === "xchina_gallery");
const isVideo = computed(() => effectiveCollector.value === "xchina_video");
const isAggregate = computed(() => effectiveCollector.value === "xchina_aggregate");
// 预览面板对图集/视频/聚合页都展示(都是"先读页面再决定采什么"); 但画质/媒体/
// 目录命名那些选项只对图集有意义, 视频与聚合页用不上
const isGalleryLike = computed(
  () => isGallery.value || isVideo.value || isAggregate.value
);
const qualityLabel = {
  original: "原图 (画质最高)",
  1200: "1200px WebP",
  800: "800px WebP",
  600: "600px WebP (最小)",
};

// ---- 图集媒体类型 / 输出目录命名(仅图集采集器有效) ----
// 有的相册同时含图片和视频(实测 xchina 视频相册 = 12 张图 + 4 段 mp4),
// 所以"采什么"要能选; auto 会先读相册页/探测一次, 再决定采哪些。
const medias = ref(["auto", "image", "video", "both"]);
const media = ref("auto");
const mediaLabel = {
  auto: "自动 (相册里有什么采什么)",
  image: "仅图片",
  video: "仅视频",
  both: "图片 + 视频",
};
// 输出目录名: 相册页 <title> 就是相册名。默认去掉 " - 分类 - 站名" 的尾巴,
// 想保留原样选"完整标题", 老行为选"图集 ID"。
const albumTitles = ref(["clean", "full", "h1", "id"]);
const albumTitle = ref("clean");
const albumTitleLabel = {
  clean: "相册名 (取 <title>, 去掉站点后缀)",
  full: "完整 <title>",
  h1: "页面 <h1> (信息更全)",
  id: "图集 ID (不开浏览器)",
};
// 相册名外面再套一层站点标签目录: 丝袜-情趣内衣/相册名/00001.jpg。
// 标签来自相册页(实测该类站每册 3~6 个), 只取前 3 个 —— 再多只会把路径撑长。
const albumTagsDir = ref(false);

// ---- 聚合页 (模特/系列/索引页) ----
// 「展开层数」是从入口页再往下钻几层去找内容链接; 「条目上限」是相册+视频页
// 的**合计**上限。两者到顶都会在预告里标成"下限"而不是静默截断。
const aggregateDepth = ref(1);
const aggregateMax = ref(50);
// 只有聚合页才带这两个参数, 免得给别的采集器塞无意义选项(与画质/媒体的处理一致)
function aggregateOpts() {
  if (!isAggregate.value) return {};
  return {
    aggregate_depth: Number(aggregateDepth.value) || 1,
    max_items: Number(aggregateMax.value) || 50,
  };
}

// ---- 过滤条件 ----
const resourceTypes = ref(["image", "video", "audio", "doc", "text"]);
const showFilters = ref(false);
const selTypes = ref([]);
const exts = ref("");
const excludeExts = ref("");
const keywords = ref("");
const excludeKeywords = ref("");
const minSize = ref({ num: "", unit: "KB" });
const maxSize = ref({ num: "", unit: "MB" });
// 尺寸下限(px): 横幅(728x90)、按钮(88x31)、信标(1x1)的文件名和体积都可能"正常",
// 只有量宽高才认得出。留空 = 不启用(默认不启用: 相册里也有竖构图小图)。
const minWidth = ref("");
const minHeight = ref("");

const LS_KEY = "uwc.create.prefs";
const UNITS = ["B", "KB", "MB", "GB"];

function splitWords(s) {
  return (s || "")
    .split(/[,\s]+/)
    .map((x) => x.trim())
    .filter(Boolean);
}

function splitExts(s) {
  return splitWords(s).map((x) => x.toLowerCase().replace(/^\.+/, ""));
}

function sizeVal(s) {
  const n = parseFloat(s.num);
  if (s.num === "" || Number.isNaN(n) || n < 0) return null;
  return `${n}${s.unit}`;
}

// 像素下限: 只接受正整数。填 0 或负数等于没填 —— 放进去只会让用户以为"在过滤"
// 而实际上后端也会把它当非法值忽略, 两边观感不一致。
function dimVal(v) {
  const s = String(v ?? "").trim();
  if (!s || !/^\d+$/.test(s)) return null;
  const n = parseInt(s, 10);
  return n > 0 ? n : null;
}

function buildFilters() {
  const f = {};
  if (selTypes.value.length) f.types = selTypes.value;
  const e = splitExts(exts.value);
  if (e.length) f.exts = e;
  const ee = splitExts(excludeExts.value);
  if (ee.length) f.exclude_exts = ee;
  const k = splitWords(keywords.value);
  if (k.length) f.keywords = k;
  const ek = splitWords(excludeKeywords.value);
  if (ek.length) f.exclude_keywords = ek;
  const mn = sizeVal(minSize.value);
  if (mn) f.min_size = mn;
  const mx = sizeVal(maxSize.value);
  if (mx) f.max_size = mx;
  const mw = dimVal(minWidth.value);
  if (mw) f.min_width = mw;
  const mh = dimVal(minHeight.value);
  if (mh) f.min_height = mh;
  return f;
}

const activeFilterCount = computed(() => Object.keys(buildFilters()).length);

// ---- 采集器自动识别 ----
// 为什么要回显: 不回显的自动识别, 一旦认错, 用户只能看到"采集到 0 个资源",
// 既不知道错在哪、也不知道手选能绕过。所以结论必须在**创建之前**就摆在界面上。
// debounce 只为避免打字过程中刷屏; 过期结果按序号丢弃(输入已变就别回填)。
let resolveTimer = null;
let resolveSeq = 0;

async function autoResolve() {
  const u = url.value.trim();
  if (collector.value !== "auto" || !u) {
    resolved.value = null;
    return;
  }
  const seq = ++resolveSeq;
  const got = await resolveCollector(u);
  if (seq === resolveSeq) resolved.value = got;
}

watch([url, collector], () => {
  resolved.value = null;
  clearTimeout(resolveTimer);
  resolveTimer = setTimeout(autoResolve, 300);
});

const resolveHint = computed(() => {
  if (collector.value !== "auto" || !url.value.trim() || !resolved.value) return null;
  const r = resolved.value;
  if (!r.collector) return { kind: "bad", text: "无法识别该输入, 请手动选择采集器" };
  const name = collectorLabel[r.collector] || r.collector;
  if (r.ambiguous) return { kind: "warn", text: `识别结果不唯一: ${r.reason}` };
  return { kind: "ok", text: `已识别为「${name}」`, reason: r.reason };
});

function toggleType(t) {
  const i = selTypes.value.indexOf(t);
  if (i >= 0) selTypes.value.splice(i, 1);
  else selTypes.value.push(t);
}

function savePrefs() {
  try {
    localStorage.setItem(
      LS_KEY,
      JSON.stringify({
        collector: collector.value,
        downloadDir: downloadDir.value,
        quality: quality.value,
        media: media.value,
        albumTitle: albumTitle.value,
        albumTagsDir: albumTagsDir.value,
        selTypes: selTypes.value,
        exts: exts.value,
        excludeExts: excludeExts.value,
        keywords: keywords.value,
        excludeKeywords: excludeKeywords.value,
        minSize: minSize.value,
        maxSize: maxSize.value,
        minWidth: minWidth.value,
        minHeight: minHeight.value,
        showFilters: showFilters.value,
      })
    );
  } catch (e) {
    /* localStorage 不可用时忽略 */
  }
}

function loadPrefs() {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return;
    const p = JSON.parse(raw);
    downloadDir.value = p.downloadDir || "";
    // 持久化的可能是已经被后端移除的采集器名, 交给 onMounted 的列表校验兜底
    if (p.collector) collector.value = p.collector;
    if (p.quality && qualities.value.includes(p.quality)) quality.value = p.quality;
    if (p.media && medias.value.includes(p.media)) media.value = p.media;
    if (p.albumTitle && albumTitles.value.includes(p.albumTitle)) {
      albumTitle.value = p.albumTitle;
    }
    albumTagsDir.value = !!p.albumTagsDir;
    selTypes.value = Array.isArray(p.selTypes) ? p.selTypes : [];
    exts.value = p.exts || "";
    excludeExts.value = p.excludeExts || "";
    keywords.value = p.keywords || "";
    excludeKeywords.value = p.excludeKeywords || "";
    if (p.minSize) minSize.value = { ...minSize.value, ...p.minSize };
    if (p.maxSize) maxSize.value = { ...maxSize.value, ...p.maxSize };
    minWidth.value = p.minWidth || "";
    minHeight.value = p.minHeight || "";
    showFilters.value = !!p.showFilters;
  } catch (e) {
    /* 本地数据损坏就用默认值 */
  }
}

async function refresh() {
  try {
    tasks.value = await listTasks();
  } catch (e) {
    /* 后端不可达时保持原列表 */
  }
}

function connectSSE() {
  es = new EventSource("/events");
  es.addEventListener("task.updated", (e) => {
    const d = JSON.parse(e.data);
    const i = tasks.value.findIndex((t) => t.id === d.id);
    if (i >= 0) {
      tasks.value[i] = { ...tasks.value[i], ...d };
    } else {
      tasks.value.unshift(d); // 新任务
    }
  });
  // EventSource 断线自动重连, 无需额外处理
}

async function submit() {
  if (!url.value.trim()) return;
  creating.value = true;
  errorMsg.value = "";
  try {
    const f = buildFilters();
    await createTask(url.value.trim(), collector.value, {
      download_dir: downloadDir.value || null,
      filters: Object.keys(f).length ? f : null,
      // 画质/媒体/命名只对图集采集器有意义, 其他采集器不传, 免得塞无意义参数
      quality: isGallery.value ? quality.value : null,
      media: isGallery.value ? media.value : null,
      album_title: isGallery.value ? albumTitle.value : null,
      album_tags_dir: isGallery.value ? albumTagsDir.value : null,
      ...aggregateOpts(),
    });
    url.value = "";
    savePrefs();
    await refresh();
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || String(e);
  } finally {
    creating.value = false;
  }
}

// ---- 创建前预览 ----
// 一个相册可能是"12 张图 + 4 段视频共 251MiB"。让用户在**创建之前**就看见
// 体积与命名, 而不是等它默默下完 —— 想只要图片就改"采集媒体", 想卡体积就设
// "最大体积"。预览只发现不下载、不写库, 所以随便点。
const preview = ref(null);
const previewError = ref("");
const previewing = ref(false);

async function runPreview() {
  if (!url.value.trim()) {
    previewError.value = "请先填写 URL 或图集 ID";
    return;
  }
  previewing.value = true;
  preview.value = null;
  previewError.value = "";
  try {
    preview.value = await previewTask({
      url: url.value.trim(),
      collector: collector.value,
      quality: isGallery.value ? quality.value : null,
      media: isGallery.value ? media.value : null,
      album_title: isGallery.value ? albumTitle.value : null,
      album_tags_dir: isGallery.value ? albumTagsDir.value : null,
      ...aggregateOpts(),
    });
  } catch (e) {
    previewError.value = e.response?.data?.detail || String(e);
  } finally {
    previewing.value = false;
  }
}

// sampled=true 表示数量来自抽样枚举, 只能说"至少这么多"
const previewSummary = computed(() => {
  const p = preview.value;
  if (!p) return "";
  const mark = p.sampled ? "≥" : "";
  // 聚合页的 photos/videos 是**子页面数量**(要展开几个相册), 不是文件数。
  // 沿用"N 张图 + M 段视频"的文案会严重误导(用户以为只下 12 个文件)。
  if (p.kind === "aggregate") {
    const bits = [];
    if (p.photos != null) bits.push(`${mark}${p.photos} 个相册`);
    if (p.videos != null) bits.push(`${mark}${p.videos} 个视频页`);
    return bits.join(" + ") || "数量未知";
  }
  const bits = [];
  if (p.photos != null) bits.push(`${mark}${p.photos} 张图`);
  if (p.videos != null) bits.push(`${mark}${p.videos} 段视频`);
  return bits.join(" + ") || "数量未知";
});

// 被"采集媒体"挡在外面的部分要说出来 —— 预告里少了 4 段视频却不解释,
// 用户会以为站点漏给了, 或者以为任务下漏了。
const skippedByMedia = computed(() => {
  const p = preview.value;
  if (!p) return "";
  const bits = [];
  if (p.photos == null && p.photos_declared != null) {
    bits.push(`${p.photos_declared} 张图`);
  }
  if (p.videos == null && p.videos_declared != null) {
    bits.push(`${p.videos_declared} 段视频`);
  }
  return bits.join(" + ");
});

// 实际生效的资源根。这一行是"为什么采不到"的直接证据: 站点悄悄把相册挪到
// 另一个 CDN 子路径、或换了序号位数时, 用户在这里一眼就能看出来。
const previewRoot = computed(() => {
  const roots = (preview.value && preview.value.resource_roots) || {};
  const first = Object.values(roots)[0];
  if (!first || !first.base) return "";
  const m = /0(\d+)d/.exec(first.seq_format || "");
  const src = { hint: "来自链接/页面", probe: "自动探测", default: "站点默认" }[
    first.source
  ];
  return (
    first.base + (m ? ` · 序号 ${m[1]} 位` : "") + (src ? ` · ${src}` : "")
  );
});

function select(id) {
  selectedId.value = selectedId.value === id ? null : id;
}

// ---- 删除任务 / 清理 ----
// "删记录"和"删文件"是两件不同的事, 所以不合并成一个确认框。
// 默认只删记录(磁盘文件保留) —— 去重机制下别的任务可能正引用着这些文件;
// 想连文件一起删必须显式再点一次。用 confirm() 的"确定/取消"表达三态
// (删哪个?) 很容易点错, 所以用一个弹层把两种后果摊开写清楚。
const pending = ref(null); // { mode: "one", id } | { mode: "bulk" }
const busy = ref(false);
const flash = ref("");
const storage = ref(null);

const FINISHED = ["success", "partial", "failed", "cancelled"];

function fmtSize(n) {
  if (n === null || n === undefined) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = Number(n);
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${i === 0 ? v : v.toFixed(1)}${u[i]}`;
}

function say(text) {
  flash.value = text;
  setTimeout(() => {
    if (flash.value === text) flash.value = "";
  }, 6000);
}

async function refreshStorage() {
  try {
    storage.value = await getStorageOverview();
  } catch (e) {
    /* 后端不可达时保持原值 */
  }
}

function askRemove(id) {
  pending.value = { mode: "one", id };
}

async function doPause(id) {
  try {
    await pauseTask(id);
    await refresh();
  } catch (e) {
    alert(e.response?.data?.detail || String(e));
  }
}

async function doResume(id) {
  try {
    await resumeTask(id);
    await refresh();
  } catch (e) {
    alert(e.response?.data?.detail || String(e));
  }
}

function askCleanup() {
  pending.value = { mode: "bulk" };
}

async function doDelete(withFiles) {
  const p = pending.value;
  if (!p) return;
  busy.value = true;
  try {
    if (p.mode === "one") {
      const info = await deleteTask(p.id, withFiles);
      tasks.value = tasks.value.filter((t) => t.id !== p.id);
      if (selectedId.value === p.id) selectedId.value = null;
      say(
        withFiles
          ? `已删除任务 #${p.id}, 同时删除 ${info.files} 个文件`
          : `已删除任务 #${p.id}, 磁盘文件保留`
      );
    } else {
      const r = await bulkDeleteTasks(FINISHED, withFiles);
      r.deleted.forEach((id) => {
        if (selectedId.value === id) selectedId.value = null;
      });
      await refresh();
      say(
        `已清理 ${r.deleted.length} 个任务` +
          (withFiles ? `, 删除 ${r.files} 个文件 / ${fmtSize(r.bytes)}` : ", 文件保留")
      );
    }
    pending.value = null;
    await refreshStorage();
  } catch (e) {
    alert(e.response?.data?.detail || String(e));
  } finally {
    busy.value = false;
  }
}

function onPickDir(path) {
  downloadDir.value = path;
  showPicker.value = false;
  savePrefs();
}

// ---- 订阅巡检 ----
// 场景: 某个相册/作者会不定期更新。手工盯既不现实也记不住, 这里把 URL +
// 采集参数存成订阅源, 由后端周期性巡检。**巡检强制走增量**, 只补新出现的
// 资源 —— 否则一个 6 小时一轮的订阅会把同一个相册反复全量重下。
const watches = ref([]);
const showWatches = ref(false);
const watchUrl = ref("");
const watchEvery = ref(360);

async function loadWatches() {
  try {
    watches.value = await listWatches();
  } catch (e) {
    /* 后端不可达保持原列表 */
  }
}

async function addWatch() {
  if (!watchUrl.value.trim()) return;
  try {
    await createWatch({
      url: watchUrl.value.trim(),
      collector: collector.value,
      interval_minutes: Number(watchEvery.value) || 360,
      download_dir: downloadDir.value || null,
      quality: isGallery.value ? quality.value : null,
      media: isGallery.value ? media.value : null,
      album_title: isGallery.value ? albumTitle.value : null,
      album_tags_dir: isGallery.value ? albumTagsDir.value : null,
      ...aggregateOpts(),
    });
    watchUrl.value = "";
    await loadWatches();
    await refresh();
  } catch (e) {
    alert(e.response?.data?.detail || String(e));
  }
}

async function toggleW(w) {
  await toggleWatch(w.id);
  await loadWatches();
}
async function runW(w) {
  await runWatch(w.id);
  await refresh();
}
async function removeW(w) {
  if (!confirm(`删除订阅 #${w.id}?`)) return;
  await deleteWatch(w.id);
  await loadWatches();
}

// ---- 登录态 ----
// 会员/付费站点的采集前提是登录态。这里起一个可见浏览器让用户手动登录,
// 登录过程中后端周期性快照 cookies, 所以**直接把浏览器关掉就行**,
// 不需要在第三方页面上找什么"完成"按钮。
const sessionsData = ref({ sessions: [], jobs: [] });
// 哪些域的登录态在采集时被判定失效(带它访问会被 Cloudflare 拒绝)
const cfStaleDomains = computed(() => sessionsData.value.cf_stale || []);
const showSessions = ref(false);
const loginUrl = ref("");
const loginJob = ref(null);
let loginTimer = null;

async function loadSessions() {
  try {
    sessionsData.value = await listSessions();
  } catch (e) {
    /* 后端不可达保持原值 */
  }
}

async function beginLogin() {
  if (!loginUrl.value.trim()) return;
  try {
    loginJob.value = await startLogin(loginUrl.value.trim());
    loginUrl.value = "";
    clearInterval(loginTimer);
    loginTimer = setInterval(trackLogin, 2000);
  } catch (e) {
    alert(e.response?.data?.detail || String(e));
  }
}

async function trackLogin() {
  if (!loginJob.value) return;
  if (["done", "error"].includes(loginJob.value.status)) {
    clearInterval(loginTimer);
    loginTimer = null;
    await loadSessions();
    return;
  }
  try {
    loginJob.value = await getLoginJob(loginJob.value.id);
  } catch (e) {
    clearInterval(loginTimer);
    loginTimer = null;
  }
}

async function endLogin() {
  if (!loginJob.value) return;
  await stopLogin(loginJob.value.id);
  await trackLogin();
}

async function removeSession(domain) {
  if (!confirm(`删除 ${domain} 的登录态?`)) return;
  await deleteSession(domain);
  await loadSessions();
}

onMounted(async () => {
  loadPrefs();
  await refresh();
  connectSSE();
  try {
    const list = await getCollectors();
    if (list.length) {
      // "auto" 由前端补: 它是"让后端挑"的选择, 不是一个真实采集器。
      // 放在最前 = 默认项, 用户不用先理解三个采集器分别是什么才能开始用。
      collectors.value = ["auto", ...list.map((c) => c.name)];
      // 上次持久化的采集器可能已经被后端移除, 这种情况退回自动识别,
      // 而不是让下拉框悄悄停在一个不存在的值上
      if (!collectors.value.includes(collector.value)) {
        collector.value = "auto";
      }
    }
  } catch (e) {
    /* 后端不可达时用默认 */
  }
  try {
    const cfg = await getConfig();
    defaultDir.value = cfg.download_dir || "";
    if (Array.isArray(cfg.resource_types) && cfg.resource_types.length) {
      resourceTypes.value = cfg.resource_types;
    }
    if (Array.isArray(cfg.qualities) && cfg.qualities.length) {
      qualities.value = cfg.qualities;
      if (!qualities.value.includes(quality.value)) {
        quality.value = qualities.value[0];
      }
    }
    if (Array.isArray(cfg.medias) && cfg.medias.length) {
      medias.value = cfg.medias;
      if (!medias.value.includes(media.value)) {
        media.value = cfg.default_media || medias.value[0];
      }
    }
    if (Array.isArray(cfg.album_titles) && cfg.album_titles.length) {
      albumTitles.value = cfg.album_titles;
      if (!albumTitles.value.includes(albumTitle.value)) {
        albumTitle.value = cfg.default_album_title || albumTitles.value[0];
      }
    }
  } catch (e) {
    /* 后端不可达时用默认 */
  }
  loadWatches();
  loadSessions();
  refreshStorage();
});
onUnmounted(() => {
  if (es) es.close();
  clearInterval(timer);
  clearInterval(loginTimer);
  clearTimeout(resolveTimer);
});
</script>

<template>
  <div class="header">
    <h1>Universal Web Collector</h1>
    <span class="sub">v10 · Resource Extraction Platform</span>
  </div>

  <div class="card">
    <form class="create-form" @submit.prevent="submit">
      <input v-model="url" type="text" :placeholder="urlPlaceholder" />
      <select v-model="collector">
        <option v-for="c in collectors" :key="c" :value="c">
          {{ collectorLabel[c] || c }}
        </option>
      </select>
      <button type="submit" :disabled="creating || !url.trim()">
        {{ creating ? "创建中..." : "创建任务" }}
      </button>
      <button
        type="button"
        class="ghost"
        v-if="isGalleryLike"
        :disabled="previewing || !url.trim()"
        @click="runPreview"
      >
        {{ previewing ? "读取中..." : "预览" }}
      </button>
    </form>

    <!-- 自动识别的结论: 必须在创建之前给用户看见, 且一行就够, 不占流程位置 -->
    <div v-if="resolveHint" class="dir-row resolve-row" :class="resolveHint.kind">
      <span class="lbl">识别结果</span>
      <span :title="resolveHint.reason || ''">{{ resolveHint.text }}</span>
    </div>

    <div class="dir-row">
      <span class="lbl">下载目录</span>
      <input
        type="text"
        :value="downloadDir || defaultDir"
        :placeholder="defaultDir || '未连接后端'"
        readonly
        :title="downloadDir || defaultDir"
      />
      <button type="button" class="ghost" @click="showPicker = true">浏览...</button>
      <button
        type="button"
        class="ghost"
        v-if="downloadDir"
        @click="downloadDir = ''; savePrefs()"
      >
        恢复默认
      </button>
    </div>

    <div class="dir-row" v-if="isGallery">
      <span class="lbl">画质档</span>
      <select v-model="quality" @change="savePrefs">
        <option v-for="q in qualities" :key="q" :value="q">
          {{ qualityLabel[q] || q }}
        </option>
      </select>
      <span class="tip">未选中的档位自动作为备用下载点; 视频只有一档</span>
    </div>

    <div class="dir-row" v-if="isGallery">
      <span class="lbl">采集媒体</span>
      <select v-model="media" @change="savePrefs">
        <option v-for="m in medias" :key="m" :value="m">
          {{ mediaLabel[m] || m }}
        </option>
      </select>
      <span class="tip">有的相册同时含图片与视频; 自动 = 读相册页判断</span>
    </div>

    <div class="dir-row" v-if="isGallery">
      <span class="lbl">目录命名</span>
      <select v-model="albumTitle" @change="savePrefs">
        <option v-for="a in albumTitles" :key="a" :value="a">
          {{ albumTitleLabel[a] || a }}
        </option>
      </select>
      <span class="tip">相册名取自相册页 &lt;title&gt;; 取不到时回退图集 ID</span>
    </div>

    <div class="dir-row" v-if="isGallery">
      <span class="lbl">标签分层</span>
      <label style="display:flex;align-items:center;gap:6px;font-size:13px">
        <input type="checkbox" v-model="albumTagsDir" @change="savePrefs" />
        在相册名外再套一层站点标签目录
      </label>
      <span class="tip" v-if="albumTagsDir">
        如 丝袜-情趣内衣/相册名/00001.jpg（取前 3 个标签）
      </span>
      <span class="tip" v-else>关闭时直接用相册名当目录</span>
    </div>

    <div class="dir-row" v-if="isAggregate">
      <span class="lbl">展开范围</span>
      <label style="display:flex;align-items:center;gap:6px;font-size:13px">
        层数
        <input class="num" type="number" min="1" max="3" v-model.number="aggregateDepth" />
        条目上限
        <input class="num" type="number" min="1" max="500" v-model.number="aggregateMax" />
      </label>
      <span class="tip">
        从入口页往下钻几层找相册; 条目上限是相册+视频页的合计上限。
        到顶时预告里的数量只会是下限, 并会明确提示
      </span>
    </div>

    <div
      v-if="isGalleryLike && (previewing || preview || previewError)"
      style="display:flex;flex-direction:column;gap:5px;font-size:13px;line-height:1.6;
             padding:10px 12px;margin-top:10px;border-radius:8px;
             border:1px solid rgba(127,127,127,0.35)"
    >
      <div v-if="previewing">
        {{
          isAggregate
            ? "正在展开聚合页(数一数下面有哪些相册)…"
            : isVideo
              ? "正在打开视频页并捕获播放列表…"
              : "正在读取相册页…"
        }}
      </div>
      <div v-else-if="previewError" style="color:#c0392b">
        预览失败: {{ previewError }}
      </div>
      <template v-else>
        <div>
          <b>{{ isAggregate ? "将展开到" : "将保存到" }}</b>
          <code>{{ preview.group }}</code>
          <span class="tip" v-if="!isVideo && !isAggregate">
            （命名方式: {{ albumTitleLabel[preview.album_source] || preview.album_source }}）
          </span>
          <span class="tip" v-if="isAggregate">
            （聚合页只发现子页面, 每个相册按图集采集器的规则各自命名）
          </span>
        </div>
        <div>
          <b>{{ isAggregate ? "本次将展开" : "本次将采集" }}</b> {{ previewSummary }}
          <span v-if="preview.video_bytes">
            · 视频合计 {{ fmtSize(preview.video_bytes) }}
          </span>
          <span v-if="preview.video_bytes && media === 'auto'" class="tip">
            · 只想留图片就把「采集媒体」改成「仅图片」
          </span>
        </div>
        <div v-if="skippedByMedia" class="tip">
          相册页还有 {{ skippedByMedia }}，当前设置不会采集
        </div>
        <div v-if="preview.tags && preview.tags.length">
          <b>标签</b> {{ preview.tags.join(" / ") }}
          <span v-if="preview.maker" class="tip">· 厂牌 {{ preview.maker }}</span>
        </div>
        <div v-if="preview.sample_files && preview.sample_files.length">
          <b>{{ isAggregate ? "相册示例" : "文件名示例" }}</b>
          <code>{{ preview.sample_files.join("  ,  ") }}</code>
        </div>
        <div v-if="previewRoot" class="tip">
          资源路径 <code>{{ previewRoot }}</code>
        </div>
        <div v-if="preview.sampled" class="tip">
          {{
            isAggregate
              ? "已达展开上限，上面的数量只是下限"
              : "数量来自抽样枚举（相册页读不到），实际可能更多"
          }}
        </div>
        <div v-if="preview.warning" class="preview-warn">
          {{ preview.warning }}
        </div>
      </template>
    </div>

    <div class="filter-toggle">
      <button type="button" class="ghost" @click="showFilters = !showFilters">
        {{ showFilters ? "▾" : "▸" }} 过滤条件
      </button>
      <span class="summary" v-if="activeFilterCount">已启用 {{ activeFilterCount }} 项</span>
      <span class="summary muted" v-else>未启用过滤, 下载全部发现的资源</span>
    </div>

    <div class="filter-panel" v-if="showFilters">
      <div class="frow">
        <label>资源类型</label>
        <div class="chips">
          <button
            type="button"
            v-for="t in resourceTypes"
            :key="t"
            class="chip"
            :class="{ on: selTypes.includes(t) }"
            @click="toggleType(t)"
          >
            {{ t }}
          </button>
        </div>
        <span class="tip">不选 = 全部类型</span>
      </div>

      <div class="frow">
        <label>仅要这些后缀</label>
        <input v-model="exts" type="text" placeholder="jpg,png,webp,mp4" />
        <span class="tip">留空不限</span>
      </div>

      <div class="frow">
        <label>排除后缀</label>
        <input v-model="excludeExts" type="text" placeholder="gif,svg" />
        <span class="tip">留空不限</span>
      </div>

      <div class="frow">
        <label>URL 需包含</label>
        <input v-model="keywords" type="text" placeholder="original,full" />
        <span class="tip">满足其一即保留</span>
      </div>

      <div class="frow">
        <label>URL 排除</label>
        <input v-model="excludeKeywords" type="text" placeholder="thumb,icon,logo,sprite" />
        <span class="tip">过滤缩略图/图标很有效</span>
      </div>

      <div class="frow">
        <label>文件大小</label>
        <input v-model="minSize.num" class="num" type="text" placeholder="最小" />
        <select v-model="minSize.unit">
          <option v-for="u in UNITS" :key="u" :value="u">{{ u }}</option>
        </select>
        <span class="sep">~</span>
        <input v-model="maxSize.num" class="num" type="text" placeholder="最大" />
        <select v-model="maxSize.unit">
          <option v-for="u in UNITS" :key="u" :value="u">{{ u }}</option>
        </select>
        <span class="tip">需 HEAD 探测, 服务器不支持时自动放行</span>
      </div>

      <div class="frow">
        <label>图片尺寸</label>
        <input v-model="minWidth" class="num" type="text" placeholder="最小宽" />
        <span class="sep">×</span>
        <input v-model="minHeight" class="num" type="text" placeholder="最小高" />
        <span class="tip">
          像素, 留空不限。横幅/按钮/信标最有效的判别; 下载后按文件头实测, 判不出则放行
        </span>
      </div>
    </div>

    <div v-if="errorMsg" class="error-box">{{ errorMsg }}</div>
  </div>

  <div class="card">
    <div class="list-bar">
      <span class="lbl">任务列表</span>
      <span class="summary muted" v-if="storage">
        共 {{ storage.tasks }} 个 · 已下载资源 {{ storage.done_resources }} 个 ·
        记录体积 {{ fmtSize(storage.recorded_bytes) }}
      </span>
      <span class="grow"></span>
      <button
        type="button"
        class="ghost"
        :disabled="!storage || !storage.finished_tasks"
        @click="askCleanup"
      >
        清理已结束{{ storage && storage.finished_tasks ? ` (${storage.finished_tasks})` : "" }}
      </button>
    </div>
    <div v-if="flash" class="flash">{{ flash }}</div>
    <TaskTable
      :tasks="tasks"
      :selected-id="selectedId"
      @select="select"
      @remove="askRemove"
      @pause="doPause"
      @resume="doResume"
    />
  </div>

  <TaskDetail v-if="selectedId" :task-id="selectedId" @close="selectedId = null" />

  <div v-if="pending" class="modal-mask" @click.self="pending = null">
    <div class="modal">
      <h3 v-if="pending.mode === 'one'">删除任务 #{{ pending.id }}</h3>
      <h3 v-else>清理已结束的任务</h3>

      <p class="muted" v-if="pending.mode === 'one'">
        任务记录会从列表里移除。<b>磁盘上已下载的文件默认保留</b>。
      </p>
      <p class="muted" v-else>
        将删除 <b>{{ storage ? storage.finished_tasks : 0 }}</b> 个已结束的任务
        (成功 / 部分失败 / 失败 / 已取消)。正在运行的任务不受影响。
      </p>

      <div class="modal-note">
        「连文件一起删除」会清掉这些任务目录下已下载的内容, 且<b>不可恢复</b>。
        内容相同的文件可能被其他任务复用, 删掉后那些任务的产出清单会指向空文件。
      </div>

      <div class="modal-actions">
        <button class="ghost" :disabled="busy" @click="pending = null">取消</button>
        <button class="ghost" :disabled="busy" @click="doDelete(false)">
          仅删除记录(保留文件)
        </button>
        <button class="ghost danger" :disabled="busy" @click="doDelete(true)">
          连文件一起删除
        </button>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="panel-toggle">
      <button type="button" class="ghost" @click="showWatches = !showWatches">
        {{ showWatches ? "▾" : "▸" }} 订阅巡检
      </button>
      <span class="summary muted">定期重跑 URL, 只补新出现的资源</span>
      <span class="summary" v-if="watches.length">已订阅 {{ watches.length }} 个</span>
    </div>

    <div v-if="showWatches">
      <div class="dir-row">
        <span class="lbl">订阅 URL</span>
        <input v-model="watchUrl" type="text" :placeholder="urlPlaceholder" />
        <select v-model.number="watchEvery">
          <option :value="60">每 1 小时</option>
          <option :value="360">每 6 小时</option>
          <option :value="1440">每天</option>
          <option :value="10080">每周</option>
        </select>
        <button type="button" class="ghost" :disabled="!watchUrl.trim()" @click="addWatch">
          订阅
        </button>
      </div>

      <div class="row-list" v-if="watches.length">
        <div class="row-item" v-for="w in watches" :key="w.id">
          <span class="dot" :class="{ off: !w.enabled }"></span>
          <span class="ell grow" :title="w.url">{{ w.url }}</span>
          <span class="mono">每 {{ w.interval_minutes }} 分钟</span>
          <span class="mono">新增 {{ w.hits }}</span>
          <span class="mono dim" :title="'上次 ' + (w.last_run || '—')">
            {{ (w.next_run || "").slice(5, 16) }}
          </span>
          <button class="ghost mini" @click="toggleW(w)">{{ w.enabled ? "暂停" : "启用" }}</button>
          <button class="ghost mini" @click="runW(w)">立即跑</button>
          <button class="ghost mini" @click="removeW(w)">删除</button>
        </div>
      </div>
      <div v-else class="empty">还没有订阅源</div>
    </div>
  </div>

  <div class="card">
    <div class="panel-toggle">
      <button type="button" class="ghost" @click="showSessions = !showSessions">
        {{ showSessions ? "▾" : "▸" }} 站点登录态
      </button>
      <span class="summary muted">付费/会员站点需要先登录一次</span>
      <span class="summary" v-if="sessionsData.sessions.length">
        已保存 {{ sessionsData.sessions.length }} 个
      </span>
    </div>

    <div v-if="showSessions">
      <div class="dir-row">
        <span class="lbl">登录页 URL</span>
        <input v-model="loginUrl" type="text" placeholder="https://example.com/login" />
        <button type="button" class="ghost" :disabled="!loginUrl.trim()" @click="beginLogin">
          打开浏览器登录
        </button>
      </div>

      <div class="login-status" v-if="loginJob">
        <span class="badge" :class="loginJob.status === 'error' ? 'failed' : 'pending'">
          {{ loginJob.domain }}
        </span>
        <span class="msg">{{ loginJob.message }}</span>
        <button
          class="ghost mini"
          v-if="!['done', 'error'].includes(loginJob.status)"
          @click="endLogin"
        >
          完成并保存
        </button>
      </div>

      <!-- 失效的登录态必须显式警告: 带着它访问会被 Cloudflare 永久拒绝,
           而用户看到的现象只是"采到的东西目录名变成了图集 ID" -->
      <div class="cf-stale" v-if="cfStaleDomains.length">
        登录态已失效: {{ cfStaleDomains.join("、") }} (带着它访问会被 Cloudflare
        拒绝)。请重新登录生成新的登录态。
      </div>

      <div class="row-list" v-if="sessionsData.sessions.length">
        <div class="row-item" v-for="s in sessionsData.sessions" :key="s.domain">
          <span class="dot" :class="{ off: cfStaleDomains.includes(s.domain) }"></span>
          <span class="grow">{{ s.domain }}</span>
          <span class="mono dim" v-if="cfStaleDomains.includes(s.domain)">已失效</span>
          <span class="mono dim" v-else>{{ s.modified }}</span>
          <button class="ghost mini" @click="removeSession(s.domain)">删除</button>
        </div>
      </div>
      <div v-else class="empty">暂无保存的登录态</div>
    </div>
  </div>

  <FolderPicker
    :show="showPicker"
    :initial="downloadDir || defaultDir"
    @select="onPickDir"
    @close="showPicker = false"
  />
</template>

<style scoped>
/* ---- 任务列表工具条 ---- */
.list-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.list-bar .lbl {
  color: var(--muted);
  font-size: 13px;
}
.list-bar .summary {
  color: var(--muted);
  font-size: 12px;
}
.list-bar .grow {
  flex: 1;
}
.flash {
  margin: -2px 0 10px;
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-left: 3px solid var(--ok);
  border-radius: 6px;
  background: var(--panel-2);
  color: var(--text);
  font-size: 12px;
}

/* ---- 删除确认弹层 ---- */
.modal h3 {
  margin: 0;
  font-size: 15px;
}
.modal p {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
  color: var(--muted);
}
.modal p b {
  color: var(--text);
}
.modal-note {
  padding: 8px 10px;
  border: 1px solid #5a3a3a;
  border-radius: 8px;
  background: rgba(224, 92, 92, 0.08);
  color: var(--muted);
  font-size: 12px;
  line-height: 1.6;
}
.modal-note b {
  color: var(--err);
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  flex-wrap: wrap;
}

.panel-toggle {
  display: flex;
  align-items: center;
  gap: 10px;
}
.panel-toggle .summary {
  color: var(--accent);
  font-size: 12px;
}
.panel-toggle .muted {
  color: var(--muted);
}
.row-list {
  margin-top: 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow: hidden;
}
.row-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--border);
  font-size: 13px;
}
.row-item:last-child {
  border-bottom: none;
}
.row-item .grow {
  flex: 1;
  min-width: 0;
}
.ell {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.mono {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 12px;
  color: var(--text);
}
.dim {
  color: var(--muted);
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--ok);
  flex: none;
}
.dot.off {
  background: var(--muted);
}
.login-status {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel-2);
}
.login-status .msg {
  flex: 1;
  color: var(--muted);
  font-size: 12px;
}
/* 自动识别的结论: 认出来了是绿色, 不唯一用黄色, 认不出来用红色 */
.resolve-row {
  font-size: 12px;
  color: var(--muted);
}
.resolve-row.ok span:last-child {
  color: var(--ok);
}
.resolve-row.warn span:last-child {
  color: var(--warn);
}
.resolve-row.bad span:last-child {
  color: var(--err);
}
.cf-stale {
  margin-top: 8px;
  padding: 6px 8px;
  border-left: 3px solid var(--warn);
  color: var(--warn);
  font-size: 12px;
  background: var(--panel-2);
}
/* 预告里的警告: 采不到东西时给的是"下一步怎么做", 不是一句冷冰冰的失败 */
.preview-warn {
  margin-top: 4px;
  padding: 6px 8px;
  border-left: 3px solid var(--warn);
  color: var(--warn);
  font-size: 12px;
  line-height: 1.6;
  background: var(--panel-2);
}
</style>
