<script setup>
import { computed, onMounted, onUnmounted, ref } from "vue";
import CreatePanel from "./components/CreatePanel.vue";
import TaskTable from "./components/TaskTable.vue";
import TaskDetail from "./components/TaskDetail.vue";
import EnvDiagnose from "./components/EnvDiagnose.vue";
import StatsPanel from "./components/StatsPanel.vue";
import ToastHost from "./components/ToastHost.vue";
import {
  bulkDeleteTasks,
  createWatch,
  deleteSession,
  deleteTask,
  deleteWatch,
  getConfig,
  getCollectors,
  getLoginJob,
  getStorageOverview,
  listSessions,
  listTasks,
  listWatches,
  pauseTask,
  resumeTask,
  startLogin,
  stopLogin,
  toggleWatch,
  runWatch,
} from "./api";
import { groupToStatuses } from "./status";
import { toast } from "./toast";

// ---- 任务列表(分页 / 搜索 / 筛选) ----
const query = ref({ q: "", status: "", page: 1, page_size: 20 });
const tasks = ref([]);
const total = ref(0);
const pages = ref(1);
const loading = ref(false);
const selectedId = ref(null);
// 键盘导航: 当前高亮行(不一定打开抽屉)
const activeId = ref(0);

const collectors = ref(["auto"]);
const config = ref({});

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
  () => !!(query.value.q || (query.value.status && query.value.status !== "all"))
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
const FINISHED = ["success", "partial", "failed", "cancelled"];

async function refreshStorage() {
  try {
    storage.value = await getStorageOverview();
  } catch (e) {}
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
async function addWatch() {
  if (!watchUrl.value.trim()) return;
  try {
    await createWatch({
      url: watchUrl.value.trim(),
      collector: "auto",
      interval_minutes: Number(watchEvery.value) || 360,
      download_dir: null,
      quality: null,
      media: null,
      album_title: null,
    });
    watchUrl.value = "";
    await loadWatches();
    await load();
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  }
}
async function toggleW(w) {
  await toggleWatch(w.id);
  await loadWatches();
}
async function runW(w) {
  await runWatch(w.id);
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

onMounted(async () => {
  await load();
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
  </div>

  <EnvDiagnose />

  <CreatePanel :collectors="collectors" :config="config" @created="load" />

  <StatsPanel />

  <div class="card">
    <div class="list-bar">
      <span class="lbl">任务列表</span>
      <span class="summary muted" v-if="storage">
        共 {{ storage.tasks }} 个 · 已下载资源 {{ storage.done_resources }} 个 · 记录体积 {{ storage.recorded_bytes }} B
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
      @search="onSearch"
      @filter="onFilter"
      @goto="onGoto"
      @select="select"
      @remove="askRemove"
      @pause="doPause"
      @resume="doResume"
      @changed="load"
    />
    <p class="kbd-hint">快捷键: <b>/</b> 搜索 · <b>j/k</b> 上下选 · <b>Enter</b> 打开 · <b>Space</b> 暂停/继续 · <b>Esc</b> 关闭</p>
  </div>

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
        <button type="button" class="ghost" :disabled="!watchUrl.trim()" @click="addWatch">订阅</button>
      </div>
      <div class="row-list" v-if="watches.length">
        <div class="row-item" v-for="w in watches" :key="w.id">
          <span class="dot" :class="{ off: !w.enabled }"></span>
          <span class="ell grow" :title="w.url">{{ w.url }}</span>
          <span class="mono">每 {{ w.interval_minutes }} 分钟</span>
          <span class="mono">新增 {{ w.hits }}</span>
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

  <ToastHost />
</template>

<style scoped>
.list-bar { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
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
</style>
