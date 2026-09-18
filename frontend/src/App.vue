<script setup>
import { computed, onMounted, onUnmounted, ref } from "vue";
import FolderPicker from "./components/FolderPicker.vue";
import TaskDetail from "./components/TaskDetail.vue";
import TaskTable from "./components/TaskTable.vue";
import {
  createTask,
  createWatch,
  deleteSession,
  deleteTask,
  deleteWatch,
  getCollectors,
  getConfig,
  getLoginJob,
  listSessions,
  listTasks,
  listWatches,
  runWatch,
  startLogin,
  stopLogin,
  toggleWatch,
} from "./api";

const tasks = ref([]);
const selectedId = ref(null);
const url = ref("");
const collector = ref("generic");
const collectors = ref(["generic"]);
// 采集器中文名; 后端只给代号, 展示层做映射, 未收录的回退显示代号本身
const collectorLabel = {
  generic: "通用网页(浏览器抓取)",
  xchina: "XChina 页面(浏览器抓取)",
  xchina_gallery: "XChina 相册(按 ID 枚举全图)",
};
// 不同采集器对输入的要求不同, 提示语跟着切换
const urlPlaceholder = computed(() =>
  collector.value === "xchina_gallery"
    ? "输入相册 ID(如 6aa113208a506)或该相册任意一张图片 URL"
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
const isGallery = computed(() => collector.value === "xchina_gallery");
const qualityLabel = {
  original: "原图 (画质最高)",
  1200: "1200px WebP",
  800: "800px WebP",
  600: "600px WebP (最小)",
};

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
  return f;
}

const activeFilterCount = computed(() => Object.keys(buildFilters()).length);

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
        downloadDir: downloadDir.value,
        quality: quality.value,
        selTypes: selTypes.value,
        exts: exts.value,
        excludeExts: excludeExts.value,
        keywords: keywords.value,
        excludeKeywords: excludeKeywords.value,
        minSize: minSize.value,
        maxSize: maxSize.value,
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
    if (p.quality && qualities.value.includes(p.quality)) quality.value = p.quality;
    selTypes.value = Array.isArray(p.selTypes) ? p.selTypes : [];
    exts.value = p.exts || "";
    excludeExts.value = p.excludeExts || "";
    keywords.value = p.keywords || "";
    excludeKeywords.value = p.excludeKeywords || "";
    if (p.minSize) minSize.value = { ...minSize.value, ...p.minSize };
    if (p.maxSize) maxSize.value = { ...maxSize.value, ...p.maxSize };
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
      // 画质档只对图集采集器有意义, 其他采集器不传, 免得塞无意义参数
      quality: isGallery.value ? quality.value : null,
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

function select(id) {
  selectedId.value = selectedId.value === id ? null : id;
}

async function remove(id) {
  if (!confirm(`删除任务 #${id}?(磁盘文件会保留)`)) return;
  try {
    await deleteTask(id);
    tasks.value = tasks.value.filter((t) => t.id !== id);
    if (selectedId.value === id) selectedId.value = null;
  } catch (e) {
    alert(e.response?.data?.detail || String(e));
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
      collectors.value = list.map((c) => c.name);
      if (!collectors.value.includes(collector.value)) {
        collector.value = collectors.value[0];
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
  } catch (e) {
    /* 后端不可达时用默认 */
  }
  loadWatches();
  loadSessions();
});
onUnmounted(() => {
  if (es) es.close();
  clearInterval(timer);
  clearInterval(loginTimer);
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
    </form>

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
      <span class="tip">未选中的档位自动作为备用下载点</span>
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
    </div>

    <div v-if="errorMsg" class="error-box">{{ errorMsg }}</div>
  </div>

  <div class="card">
    <TaskTable :tasks="tasks" :selected-id="selectedId" @select="select" @remove="remove" />
  </div>

  <TaskDetail v-if="selectedId" :task-id="selectedId" @close="selectedId = null" />

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

      <div class="row-list" v-if="sessionsData.sessions.length">
        <div class="row-item" v-for="s in sessionsData.sessions" :key="s.domain">
          <span class="dot"></span>
          <span class="grow">{{ s.domain }}</span>
          <span class="mono dim">{{ s.modified }}</span>
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
</style>
