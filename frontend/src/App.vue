<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import CreatePanel from "./components/CreatePanel.vue";
import TaskTable from "./components/TaskTable.vue";
import { selectTaskFromSearch } from "./task-query.mjs";
import TaskDetail from "./components/TaskDetail.vue";
import EnvDiagnose from "./components/EnvDiagnose.vue";
import StatsPanel from "./components/StatsPanel.vue";
import LibraryPanel from "./components/LibraryPanel.vue";
import LocalAlbumPanel from "./components/LocalAlbumPanel.vue";
import NotificationCenter from "./components/NotificationCenter.vue";
import ToastHost from "./components/ToastHost.vue";
import {
  bulkDeleteTasks,
  cancelTask,
  clearPartials,
  createWatch,
  createWebhook,
  deleteSession,
  deleteTask,
  deleteWatch,
  deleteWebhook,
  getConfig,
  getCollectors,
  getLoginJob,
  getPartials,
  getStorageOverview,
  getSystemGates,
  listSessions,
  listTasks,
  listWatches,
  listWebhookDeliveries,
  listWebhooks,
  pauseTask,
  resumeTask,
  startLogin,
  stopLogin,
  testWebhook,
  toggleWatch,
  runWatch,
} from "./api";
import { groupToStatuses } from "./status";
import { toast } from "./toast";
import { pushByteSample } from "./byterate";

// ---- 任务列表(分页 / 搜索 / 筛选) ----
const query = ref({ q: "", status: "", page: 1, page_size: 20, collector: "" });
const tasks = ref([]);
const total = ref(0);
const pages = ref(1);
const loading = ref(false);
const selectedId = ref(null);
// 键盘导航: 当前高亮行(不一定打开抽屉)
const activeId = ref(0);

const collectors = ref(["auto"]);
const config = ref({});
// 主视图切换: 任务列表 / 资源库 / 本地相册集。记忆选择, 高频用户不用每次点回来。
// 三个视图的分工: 任务列表 = "我正在采什么"; 资源库 = "我**下过**什么"(每一行都
// 对得上一份本程序产出的文件, 所以能删); 本地相册集 = "我**本来就有**什么"
// (用户自己的目录, **只读**)。
const view = ref("tasks");
const VIEW_KEY = "uwc.view.v1";
const VIEWS = ["tasks", "library", "local"];
try {
  const savedView = localStorage.getItem(VIEW_KEY);
  if (VIEWS.includes(savedView)) view.value = savedView;
} catch (e) {
  /* ignore */
}
watch(view, (v) => {
  try {
    localStorage.setItem(VIEW_KEY, v);
  } catch (e) {
    /* ignore */
  }
  // 切走任务列表时把抽屉关掉: 资源库是独立视图, 留一个浮在别处的抽屉很怪
  if (v !== "tasks") selectedId.value = null;
});

let es = null;
let reloadTimer = null;
function scheduleReload() {
  clearTimeout(reloadTimer);
  reloadTimer = setTimeout(load, 600);
}

async function load() {
  loading.value = true;
  try {
    const r = await listTasks({
      q: query.value.q || undefined,
      status: groupToStatuses(query.value.status),
      collector: query.value.collector || undefined,
      page: query.value.page,
      page_size: query.value.page_size,
    });
    tasks.value = r.items;
    total.value = r.total;
    pages.value = r.pages;
    // 高亮行若已不在当前页, 跟随到第一项(若有)
    if (!tasks.value.find((t) => t.id === activeId.value) && tasks.value.length) {
      activeId.value = tasks.value[0].id;
    }
  } catch (e) {
    /* 后端不可达时保持原列表 */
  } finally {
    loading.value = false;
  }
}

const filterActive = computed(
  () =>
    !!(
      query.value.q ||
      query.value.collector ||
      (query.value.status && query.value.status !== "all")
    )
);

function onSearch(q) {
  query.value.q = q;
  query.value.page = 1;
  load();
}
function onFilter(statusValue) {
  query.value.status = statusValue;
  query.value.page = 1;
  load();
}
function onCollector(c) {
  query.value.collector = c;
  query.value.page = 1;
  load();
}
function onGoto(page) {
  query.value.page = page;
  load();
}
function select(id) {
  selectedId.value = id;
  activeId.value = id;
}

// ---- 键盘快捷键: / 聚焦搜索, j/k 上下选, 回车打开, 空格暂停/继续, Esc 关抽屉 ----
function onKey(e) {
  const tag = (e.target.tagName || "").toLowerCase();
  const typing = tag === "input" || tag === "textarea" || tag === "select";
  if (e.key === "/" && !typing) {
    e.preventDefault();
    const el = document.querySelector(".search-input");
    if (el) el.focus();
    return;
  }
  if (e.key === "Escape") {
    if (selectedId.value) selectedId.value = null;
    return;
  }
  if (typing) return;
  if (e.key === "j" || e.key === "ArrowDown") {
    moveActive(1);
    e.preventDefault();
  } else if (e.key === "k" || e.key === "ArrowUp") {
    moveActive(-1);
    e.preventDefault();
  } else if (e.key === "Enter") {
    if (activeId.value) select(activeId.value);
  } else if (e.key === " ") {
    if (activeId.value) {
      const t = tasks.value.find((x) => x.id === activeId.value);
      if (t) {
        if (["pending", "running", "extracting", "downloading"].includes(t.status)) doPause(t.id);
        else if (t.status === "paused") doResume(t.id);
      }
      e.preventDefault();
    }
  }
}
function moveActive(dir) {
  if (!tasks.value.length) return;
  const ids = tasks.value.map((t) => t.id);
  let idx = ids.indexOf(activeId.value);
  if (idx < 0) idx = 0;
  else idx = Math.min(ids.length - 1, Math.max(0, idx + dir));
  activeId.value = ids[idx];
}

function connectSSE() {
  es = new EventSource("/events");
  es.addEventListener("task.updated", (e) => {
    const d = JSON.parse(e.data);
    const i = tasks.value.findIndex((t) => t.id === d.id);
    if (i >= 0) {
      tasks.value[i] = { ...tasks.value[i], ...d };
    } else if (query.value.page === 1 && !query.value.q && !query.value.status) {
      // 新任务不在当前页: 仅在第一页无筛选时自动刷新, 避免打断用户翻页
      scheduleReload();
    }
  });
  // 字节级吞吐: 后端按 ~0.4s 节流推累计字节数, 这里换算成瞬时速率并保留
  // 一段时间序列, 供详情页画真实速率曲线。纯内存、不落库。
  es.addEventListener("task.bytes", (e) => {
    try {
      const d = JSON.parse(e.data);
      pushByteSample(d.task_id, d.bytes, d.ts);
    } catch (_) {}
  });
}

async function doPause(id) {
  try {
    await pauseTask(id);
    await load();
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  }
}
async function doResume(id) {
  try {
    await resumeTask(id);
    await load();
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  }
}

// ---- 删除任务 / 清理 ----
const pending = ref(null);
const busy = ref(false);
const storage = ref(null);
const partials = ref(null);
const FINISHED = ["success", "partial", "failed", "cancelled"];

async function refreshStorage() {
  try {
    storage.value = await getStorageOverview();
  } catch (e) {}
  // ⚠️ 暂存区单独拉, 且**失败不影响 storage**: 它是补充信息(磁盘上那块
  // 看不见的占用), 拉不到只是不显示那一行, 不该连任务概览一起空掉。
  try {
    partials.value = await getPartials();
  } catch (e) {
    partials.value = null;
  }
}

// 清空暂存区: 代价只是"下次从头下", 不会下出坏文件, 所以不做二次确认。
// 反过来说要如实回报"释放了多少" —— 用户点它就是为了还磁盘。
async function doClearPartials() {
  try {
    const r = await clearPartials();
    toast(
      r.cleared
        ? `已释放暂存 ${r.bytes_h}, 这些资源下次将从头下载`
        : "暂存区本来就是空的",
      "ok"
    );
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  }
  await refreshStorage();
}
function askRemove(id) {
  pending.value = { mode: "one", id };
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
      toast(
        withFiles
          ? `已删除任务 #${p.id}, 同时删除 ${info.files ?? 0} 个文件`
          : `已删除任务 #${p.id}, 磁盘文件保留`,
        "ok"
      );
    } else {
      const r = await bulkDeleteTasks(FINISHED, withFiles);
      await load();
      toast(
        `已清理 ${r.deleted.length} 个任务` +
          (withFiles ? `, 删除 ${r.files} 个文件 / ${(r.bytes || 0)}` : ", 文件保留"),
        "ok"
      );
    }
    pending.value = null;
    await refreshStorage();
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  } finally {
    busy.value = false;
  }
}
function askCleanup() {
  pending.value = { mode: "bulk" };
}

// ---- 订阅巡检 ----
const watches = ref([]);
const showWatches = ref(false);
const watchUrl = ref("");
const watchEvery = ref(360);
async function loadWatches() {
  try {
    watches.value = await listWatches();
  } catch (e) {}
}
// V45: cron 排期。填了 cron 就**按 cron 排下一次**, interval 只作兜底;
// 两个都不填还是"每 N 分钟"(老行为不变)。
// ⚠️ 校验交给后端: 它回 400 并带**中文原因**(第 K 条 —— 代号管红不红, 文案管
// 哪里红)。前端另写一套解析器, 两套规则迟早不一致, 而"前端说合法、后端说不"
// 的表现是订阅静默不跑。
const watchCron = ref("");
async function addWatch() {
  if (!watchUrl.value.trim()) return;
  const cron = (watchCron.value || "").trim();
  try {
    await createWatch({
      url: watchUrl.value.trim(),
      collector: "auto",
      interval_minutes: Number(watchEvery.value) || 360,
      cron: cron || null,
      download_dir: null,
      quality: null,
      media: null,
      album_title: null,
    });
    watchUrl.value = "";
    watchCron.value = "";
    await loadWatches();
    await load();
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  }
}
// ⚠️ 显示的是**真实排期来源**: 有 cron 就显示 cron。不然用户改了 cron 却看到
// "每 360 分钟", 会以为没生效 —— 而它其实生效了(第 28 条: 静默回退成默认值)。
function scheduleText(w) {
  return w.cron ? `cron ${w.cron}` : `每 ${w.interval_minutes} 分钟`;
}
async function toggleW(w) {
  await toggleWatch(w.id);
  await loadWatches();
}
async function runW(w) {
  await runWatch(w.id);
  await loadWatches();
  await load();
}
async function removeW(w) {
  if (!confirm(`删除订阅 #${w.id}?`)) return;
  await deleteWatch(w.id);
  await loadWatches();
}

// ---- 登录态 ----
const sessionsData = ref({ sessions: [], jobs: [] });
const cfStaleDomains = computed(() => sessionsData.value.cf_stale || []);
const showSessions = ref(false);
const loginUrl = ref("");
const loginJob = ref(null);
let loginTimer = null;
async function loadSessions() {
  try {
    sessionsData.value = await listSessions();
  } catch (e) {}
}
async function beginLogin() {
  if (!loginUrl.value.trim()) return;
  try {
    loginJob.value = await startLogin(loginUrl.value.trim());
    loginUrl.value = "";
    clearInterval(loginTimer);
    loginTimer = setInterval(trackLogin, 2000);
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
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

// ---- V45: Webhook(通知外部系统) ----
// 后端 V44 就有 webhook, 但界面上一块都没有 —— "配了没动静"和"没配"在 UI 上
// 完全一样。这里补齐三件事: 看得见列表、手动投一次、看**逐次**投递记录。
const hooks = ref([]);
const hookEvents = ref([]); // 事件代号 + 中文名, 由 /system/gates 下发
const showHooks = ref(false);
const hookUrl = ref("");
const hookSecret = ref("");
const hookPicked = ref([]);
const hookBusy = ref(false);
// 展开投递历史的那个 hook。⚠️ 逐次记录**每次尝试一行**: "投了 3 次才成"与
// "一次就成了"必须数得出来 —— 合并成一行等于把对端的问题藏起来。
const deliveryOf = ref(null);
const deliveryItems = ref([]);
const deliveryBusy = ref(false);

async function loadHooks() {
  try {
    hooks.value = await listWebhooks();
  } catch (e) {
    hooks.value = [];
  }
}
// 事件清单只在第一次展开面板时拉: 它走 /system/gates(会跑仓库门禁), 不该在
// 每次进页面时都跑一遍。
async function toggleHooks() {
  showHooks.value = !showHooks.value;
  if (showHooks.value && !hookEvents.value.length) {
    try {
      const g = await getSystemGates();
      hookEvents.value = g.events || [];
    } catch (e) {
      hookEvents.value = [];
    }
  }
  if (showHooks.value) loadHooks();
}
async function addHook() {
  if (!hookUrl.value.trim()) return;
  hookBusy.value = true;
  try {
    await createWebhook(hookUrl.value.trim(), hookPicked.value.slice(), hookSecret.value.trim());
    hookUrl.value = "";
    hookSecret.value = "";
    hookPicked.value = [];
    await loadHooks();
    toast("已保存。用「投一次」确认对端收得到", "ok");
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  } finally {
    hookBusy.value = false;
  }
}
async function removeHook(h) {
  if (!confirm(`删除 webhook ${h.url}?`)) return;
  await deleteWebhook(h.id);
  if (deliveryOf.value === h.id) {
    deliveryOf.value = null;
    deliveryItems.value = [];
  }
  await loadHooks();
}
async function fireHook(h) {
  try {
    const r = await testWebhook(h.id);
    const first = (r.results || [])[0] || {};
    // ⚠️ 有 status 就报 status, 没有就报 error —— "连不上"和"连上了被拒"是两件事,
    // 而"什么都没说"会让用户以为按钮没生效。
    const what = first.status ? `HTTP ${first.status}` : first.error || "无响应";
    toast(r.delivered ? `投递成功: ${what}` : `投递失败: ${what}`, r.delivered ? "ok" : "warn");
    await loadHooks();
    if (deliveryOf.value === h.id) loadDeliveries(h.id);
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  }
}
async function loadDeliveries(id) {
  deliveryBusy.value = true;
  try {
    const r = await listWebhookDeliveries(id, 30);
    deliveryItems.value = r.items || [];
    deliveryOf.value = id;
  } catch (e) {
    deliveryItems.value = [];
  } finally {
    deliveryBusy.value = false;
  }
}
function toggleDeliveries(h) {
  if (deliveryOf.value === h.id) {
    deliveryOf.value = null;
    deliveryItems.value = [];
    return;
  }
  loadDeliveries(h.id);
}
function pickEvent(key) {
  const i = hookPicked.value.indexOf(key);
  if (i >= 0) hookPicked.value.splice(i, 1);
  else hookPicked.value.push(key);
}
// 投递状态 -> 颜色。⚠️ 未知状态显示为中性色而不是"成功" ——
// 把看不懂的状态画成绿色, 是最容易的一种自我欺骗。
function dvClass(d) {
  if (d.status === null || d.status === undefined) return "bad";
  if (d.status >= 200 && d.status < 300) return "ok";
  return "bad";
}
function dvText(d) {
  if (d.status === null || d.status === undefined) return d.error || "无响应";
  return `HTTP ${d.status}`;
}
// `created_at` 是 epoch 秒(REAL)。⚠️ 直接把 1753… 那个浮点数画到界面上
// 等于没给时间 —— 而"什么时候试的"正是看投递记录时唯一想对上的东西。
function fmtTs(v) {
  const n = Number(v);
  if (!Number.isFinite(n) || n <= 0) return "";
  const d = new Date(n * 1000);
  const p = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

onMounted(async () => {
  await load();
  selectedId.value = selectTaskFromSearch(window.location.search, tasks.value);
  connectSSE();
  try {
    const list = await getCollectors();
    if (list.length) collectors.value = ["auto", ...list.map((c) => c.name)];
  } catch (e) {}
  try {
    config.value = await getConfig();
  } catch (e) {}
  loadWatches();
  loadSessions();
  refreshStorage();
  window.addEventListener("keydown", onKey);
});
onUnmounted(() => {
  if (es) es.close();
  clearInterval(loginTimer);
  clearTimeout(reloadTimer);
  window.removeEventListener("keydown", onKey);
});
</script>

<template>
  <div class="header">
    <h1>Universal Web Collector</h1>
    <span class="sub">v10 · 资源采集平台</span>
    <span class="grow"></span>
    <NotificationCenter @changed="load" />
  </div>

  <EnvDiagnose />

  <CreatePanel :collectors="collectors" :config="config" @created="load" />

  <StatsPanel />

  <div class="view-switch">
    <button
      class="vtab"
      :class="{ on: view === 'tasks' }"
      @click="view = 'tasks'"
    >任务列表</button>
    <button
      class="vtab"
      :class="{ on: view === 'library' }"
      @click="view = 'library'"
    >资源库</button>
    <button
      class="vtab"
      :class="{ on: view === 'local' }"
      @click="view = 'local'"
    >本地相册集</button>
  </div>

  <div class="card" v-show="view === 'tasks'">
    <div class="list-bar">
      <span class="lbl">任务列表</span>
      <span class="summary muted" v-if="storage">
        共 {{ storage.tasks }} 个 · 已下载资源 {{ storage.done_resources }} 个 · 记录体积 {{ storage.recorded_bytes }} B
      </span>
      <!--
        断点续传暂存区: 中断过的下载按 URL 留在 _meta/partial/ 里等着续传。
        ⚠️ 这块占用在磁盘上**看不见**(不在相册目录里), 不显示出来就会被当成
        "程序在偷偷吃盘"。所以连"会自己过期"一起说明。
      -->
      <span
        v-if="partials && partials.count"
        class="summary muted"
        :title="`${partials.dir}\n保留 ${partials.ttl_hours} 小时 · 上限 ${partials.max_bytes_h}\n单文件 ${partials.files} 份 · 分片缓存 ${partials.bundles} 份 —— 取消或失败之后, 换个相册名/目录也能接着下`"
      >
        · 待续传 {{ partials.count }} 份 / {{ partials.bytes_h }}
        <template v-if="partials.bundles">(含 {{ partials.bundles }} 份分片缓存)</template>
      </span>
      <button
        v-if="partials && partials.count"
        type="button"
        class="ghost"
        @click="doClearPartials"
      >
        清空暂存
      </button>
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
    <TaskTable
      :tasks="tasks"
      :total="total"
      :pages="pages"
      :page="query.page"
      :page-size="query.page_size"
      :status-filter="query.status"
      :loading="loading"
      :active-id="activeId"
      :filter-active="filterActive"
      :collectors="collectors.filter((c) => c !== 'auto')"
      :collector-filter="query.collector"
      @search="onSearch"
      @filter="onFilter"
      @collector="onCollector"
      @goto="onGoto"
      @select="select"
      @remove="askRemove"
      @pause="doPause"
      @resume="doResume"
      @changed="load"
    />
    <p class="kbd-hint">快捷键: <b>/</b> 搜索 · <b>j/k</b> 上下选 · <b>Enter</b> 打开 · <b>Space</b> 暂停/继续 · <b>Esc</b> 关闭</p>
  </div>

  <LibraryPanel v-if="view === 'library'" />

  <LocalAlbumPanel v-if="view === 'local'" />

  <TaskDetail
    v-if="selectedId"
    :task-id="selectedId"
    @close="selectedId = null"
    @remove="askRemove"
    @changed="load"
  />

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
        <button class="ghost" :disabled="busy" @click="doDelete(false)">仅删除记录(保留文件)</button>
        <button class="ghost danger" :disabled="busy" @click="doDelete(true)">连文件一起删除</button>
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
        <input v-model="watchUrl" type="text" placeholder="粘贴任意采集 URL" />
        <select v-model.number="watchEvery">
          <option :value="60">每 1 小时</option>
          <option :value="360">每 6 小时</option>
          <option :value="1440">每天</option>
          <option :value="10080">每周</option>
        </select>
        <!-- V45 cron。留空 = 用上面的"每 N 分钟"。
             ⚠️ 提示里必须写明"日与周同时给取或": 那是标准 cron 语义, 与多数人
             直觉的"且"相反 —— 按"且"理解会让订阅一年只跑一次。 -->
        <input
          v-model="watchCron"
          type="text"
          class="cron-input"
          placeholder="cron(可选), 如 0 9 * * 1-5"
          title="5 段: 分 时 日 月 周。留空则用左边的间隔。日与周同时给时取「或」(标准 cron 语义); 周日=0 或 7。"
        />
        <button type="button" class="ghost" :disabled="!watchUrl.trim()" @click="addWatch">订阅</button>
      </div>
      <div class="row-list" v-if="watches.length">
        <div class="row-item" v-for="w in watches" :key="w.id">
          <span class="dot" :class="{ off: !w.enabled }"></span>
          <span class="ell grow" :title="w.url">{{ w.url }}</span>
          <span class="mono">{{ scheduleText(w) }}</span>
          <span class="mono">新增 {{ w.hits }}</span>
          <span class="mono dim" :title="w.last_run || '尚未运行'">
            {{ w.last_run ? "上次 " + (w.last_run || "").slice(5, 16) : "未运行" }}
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
      <span class="summary" v-if="sessionsData.sessions.length">已保存 {{ sessionsData.sessions.length }} 个</span>
    </div>
    <div v-if="showSessions">
      <div class="dir-row">
        <span class="lbl">登录页 URL</span>
        <input v-model="loginUrl" type="text" placeholder="https://example.com/login" />
        <button type="button" class="ghost" :disabled="!loginUrl.trim()" @click="beginLogin">打开浏览器登录</button>
      </div>
      <div class="login-status" v-if="loginJob">
        <span class="badge" :class="loginJob.status === 'error' ? 'failed' : 'pending'">{{ loginJob.domain }}</span>
        <span class="msg">{{ loginJob.message }}</span>
        <button class="ghost mini" v-if="!['done', 'error'].includes(loginJob.status)" @click="endLogin">完成并保存</button>
      </div>
      <div class="cf-stale" v-if="cfStaleDomains.length">
        登录态已失效: {{ cfStaleDomains.join("、") }} (带着它访问会被 Cloudflare 拒绝)。请重新登录。
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

  <div class="card">
    <div class="panel-toggle">
      <button type="button" class="ghost" @click="toggleHooks">
        {{ showHooks ? "▾" : "▸" }} Webhook
      </button>
      <span class="summary muted">任务结束时通知外部系统(带 HMAC 签名)</span>
      <span class="summary" v-if="hooks.length">已配 {{ hooks.length }} 个</span>
    </div>
    <div v-if="showHooks">
      <div class="dir-row">
        <span class="lbl">回调 URL</span>
        <input v-model="hookUrl" type="text" placeholder="https://example.com/hook" />
        <input
          v-model="hookSecret"
          type="text"
          class="hook-secret"
          placeholder="签名密钥(可选)"
          title="留空则不签名。密钥保存后**不再返回**, 界面上只显示是否已设。"
        />
        <button type="button" class="ghost" :disabled="hookBusy || !hookUrl.trim()" @click="addHook">
          保存
        </button>
      </div>
      <!-- 事件代号 + 中文名由 /system/gates 下发, 前端不自带一份。
           一个都不勾 = 订阅全部事件(后端语义)。 -->
      <div class="hook-events" v-if="hookEvents.length">
        <span class="lbl">事件</span>
        <button
          v-for="e in hookEvents"
          :key="e.key"
          class="ghost mini"
          :class="{ on: hookPicked.includes(e.key) }"
          @click="pickEvent(e.key)"
        >{{ e.label }}</button>
        <span class="dim small">一个都不选 = 全部事件</span>
      </div>
      <div class="row-list" v-if="hooks.length">
        <div v-for="h in hooks" :key="h.id" class="hk-wrap">
          <div class="row-item">
            <span class="dot" :class="{ off: !h.enabled }"></span>
            <span class="ell grow" :title="h.url">{{ h.url }}</span>
            <span class="mono dim" v-if="h.has_secret" title="已设签名密钥">已签名</span>
            <!-- 上一次投递的结果。⚠️ 必须常驻显示: webhook 配错的表现就是
                 "什么都没发生", 而失败不留痕的话连"它试过没有"都不知道。 -->
            <span
              class="mono"
              :class="h.last_status && h.last_status < 400 ? 'ok-text' : 'bad-text'"
              :title="h.last_error || ''"
            >
              {{ h.last_status ? "上次 " + h.last_status : "未投过" }}
            </span>
            <button class="ghost mini" @click="fireHook(h)">投一次</button>
            <button class="ghost mini" @click="toggleDeliveries(h)">
              {{ deliveryOf === h.id ? "收起记录" : "投递记录" }}
            </button>
            <button class="ghost mini" @click="removeHook(h)">删除</button>
          </div>
          <!-- 逐次记录: 重试过的投递在这里是**多行** -->
          <div v-if="deliveryOf === h.id" class="dv-box">
            <p v-if="deliveryBusy" class="dim small">加载中…</p>
            <p v-else-if="!deliveryItems.length" class="dim small">
              还没有投递记录。点「投一次」试一下对端。
            </p>
            <div v-else class="dv-list">
              <div v-for="(d, i) in deliveryItems" :key="i" class="dv-item">
                <span class="mono" :class="dvClass(d)">{{ dvText(d) }}</span>
                <span class="dim small">第 {{ d.attempt }} 次</span>
                <span class="dim small">{{ d.event }}</span>
                <span class="grow"></span>
                <span class="dim small">{{ fmtTs(d.created_at) }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>
      <div v-else class="empty">还没有配置 webhook</div>
    </div>
  </div>

  <ToastHost />
</template>

<style scoped>
.list-bar { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
.header .grow { flex: 1; }
.list-bar .lbl { color: var(--muted); font-size: 13px; }
.list-bar .summary { color: var(--muted); font-size: 12px; }
.list-bar .grow { flex: 1; }
.kbd-hint { margin: 10px 2px 0; color: #6c7885; font-size: 11px; }
.kbd-hint b { color: var(--muted); background: var(--panel-2); border: 1px solid var(--border); border-radius: 4px; padding: 0 5px; font-family: ui-monospace, Consolas, monospace; }
.panel-toggle { display: flex; align-items: center; gap: 10px; }
.panel-toggle .summary { color: var(--accent); font-size: 12px; }
.panel-toggle .muted { color: var(--muted); }
.row-list { margin-top: 10px; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.row-item { display: flex; align-items: center; gap: 10px; padding: 8px 10px; border-bottom: 1px solid var(--border); font-size: 13px; }
.row-item:last-child { border-bottom: none; }
.row-item .grow { flex: 1; min-width: 0; }
.ell { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mono { font-family: ui-monospace, Consolas, monospace; font-size: 12px; color: var(--text); }
.dim { color: var(--muted); }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--ok); flex: none; }
.dot.off { background: var(--muted); }
.login-status { display: flex; align-items: center; gap: 10px; margin-top: 10px; padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px; background: var(--panel-2); }
.login-status .msg { flex: 1; color: var(--muted); font-size: 12px; }
.cf-stale { margin-top: 8px; padding: 6px 8px; border-left: 3px solid var(--warn); color: var(--warn); font-size: 12px; background: var(--panel-2); }
.modal h3 { margin: 0; font-size: 15px; }
.modal p { margin: 0; font-size: 13px; line-height: 1.6; color: var(--muted); }
.modal p b { color: var(--text); }
.modal-note { padding: 8px 10px; border: 1px solid #5a3a3a; border-radius: 8px; background: rgba(224, 92, 92, 0.08); color: var(--muted); font-size: 12px; line-height: 1.6; }
.modal-note b { color: var(--err); }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; flex-wrap: wrap; }
/* ---- V45: cron / webhook ---- */
.cron-input { min-width: 190px; font-family: ui-monospace, Consolas, monospace; }
.hook-secret { min-width: 160px; }
.hook-events { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
.hook-events .lbl { color: var(--muted); font-size: 13px; }
.hook-events .on { color: var(--accent); border-color: var(--accent); }
.small { font-size: 11px; }
.hk-wrap { border-bottom: 1px solid var(--border); }
.hk-wrap:last-child { border-bottom: none; }
.ok-text { color: var(--ok); }
.bad-text { color: var(--err); }
.dv-box { padding: 6px 10px 10px 28px; background: var(--panel-2); }
.dv-list { display: flex; flex-direction: column; gap: 4px; }
.dv-item { display: flex; align-items: center; gap: 10px; font-size: 12px; }
.dv-item .grow { flex: 1; }
.dv-item .ok { color: var(--ok); }
.dv-item .bad { color: var(--err); }
</style>
