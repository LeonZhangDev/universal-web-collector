<script setup>
import { computed, onUnmounted, ref, watch } from "vue";
import {
  archiveUrl,
  cancelTask,
  getLogs,
  getManifest,
  getTask,
  pauseTask,
  resumeTask,
  retryFailed,
  retryResource,
  retryTask,
} from "../api";
import { statusLabel } from "../status";
import { toast } from "../toast";
import Lightbox from "./Lightbox.vue";

const props = defineProps({ taskId: { type: Number, required: true } });
const emit = defineEmits(["close", "remove", "changed"]);

const task = ref(null);
const logs = ref([]);
let timer = null;
const active = () =>
  ["pending", "running", "extracting", "downloading"].includes(task.value?.status);

const tab = ref("resources");
// tab 记忆: 打开不同任务时保持上次看的那一栏(资源/日志/信息), 高频用户不用反复点。
const TAB_KEY = "uwc.detail.tab.v1";
try {
  const saved = localStorage.getItem(TAB_KEY);
  if (saved) tab.value = saved;
} catch (e) {
  /* ignore */
}
watch(tab, (v) => {
  try {
    localStorage.setItem(TAB_KEY, v);
  } catch (e) {
    /* ignore */
  }
});

const STATUS_TABS = [
  { key: "all", label: "全部" },
  { key: "done", label: "已下载" },
  { key: "failed", label: "失败" },
  { key: "filtered", label: "已过滤" },
  { key: "pending", label: "待处理" },
];
const view = ref("all");
const viewMode = ref("grid"); // grid | list —— 资源区列表/网格切换

// 速率指示: 详情每 2 秒刷新一次, 用"已下载资源数"的差值估算采集速率(张/分)。
// 不是真实字节速率(后端进度是百分比, 不回传字节), 但足够让人看出"还在动、快不快"。
const ratePerMin = ref(0);
let lastDone = null;
let lastTs = 0;
const visible = computed(() => {
  const rs = task.value?.resources || [];
  if (view.value === "all") return rs;
  return rs.filter((r) =>
    view.value === "pending"
      ? ["pending", "downloading", "skipped"].includes(r.status)
      : r.status === view.value
  );
});
// 灯箱: 只看 visible 里的图片, 去掉视频/文档
const lightboxImages = computed(() =>
  visible.value
    .filter((r) => r.type === "image" && r.file_url)
    .map((r) => ({ url: r.file_url, name: (r.url || "").split("/").pop() || "" }))
);
const lbIndex = ref(0);
const showLb = ref(false);
function openLb(i) {
  lbIndex.value = i;
  showLb.value = true;
}

const manifest = ref(null);
const showManifest = ref(false);
const archiveHref = computed(() => archiveUrl(props.taskId));

const typeIcon = { video: "▶", audio: "♪", doc: "📄", text: "📝" };

const canRetryResource = computed(
  () =>
    task.value &&
    ["success", "partial", "failed", "cancelled"].includes(task.value.status)
);

const stat = computed(() => {
  const m = {};
  for (const r of task.value?.resources || []) {
    m[r.status] = (m[r.status] || 0) + 1;
  }
  return m;
});
function badgeClass(s) {
  if (s === "done") return "success";
  if (s === "failed") return "failed";
  if (s === "filtered") return "filtered";
  return "pending";
}
const dupCount = computed(
  () => (task.value?.resources || []).filter((r) => r.duplicate_of).length
);
const failedSkippedCount = computed(
  () =>
    (task.value?.resources || []).filter((r) =>
      ["failed", "skipped"].includes(r.status)
    ).length
);

// 失败原因聚合: 解析失败资源的 note 归类成 403/超时/404/其他,
// 让人一眼看出"该换代理还是该换采集器"。
const failReasons = computed(() => {
  const rs = (task.value?.resources || []).filter((r) =>
    ["failed", "skipped"].includes(r.status)
  );
  const buckets = { "403/被封": 0, "超时": 0, "404/不存在": 0, "其他": 0 };
  for (const r of rs) {
    const n = String(r.note || "");
    if (/\b403\b|forbidden|blocked|被封|风控/i.test(n)) buckets["403/被封"]++;
    else if (/timeout|timed out|超时/i.test(n)) buckets["超时"]++;
    else if (/\b404\b|not found|不存在/i.test(n)) buckets["404/不存在"]++;
    else buckets["其他"]++;
  }
  const total = rs.length || 1;
  return Object.entries(buckets)
    .filter(([, n]) => n > 0)
    .map(([label, n]) => ({ label, n, pct: Math.round((n / total) * 100) }));
});

function fmtSize(n) {
  if (n === null || n === undefined) return "";
  let v = Number(n);
  if (!isFinite(v)) return "";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return i === 0 ? `${v} B` : `${v.toFixed(1)} ${units[i]}`;
}

async function load() {
  try {
    task.value = await getTask(props.taskId);
    logs.value = await getLogs(props.taskId);
    // 估算采集速率: 用两次刷新间"已下载资源数"的增量
    const done = (task.value?.resources || []).filter((r) => r.status === "done").length;
    const now = Date.now();
    if (lastDone !== null && lastTs && active()) {
      const dt = (now - lastTs) / 1000;
      if (dt > 0) ratePerMin.value = Math.round(((done - lastDone) / dt) * 60);
    }
    lastDone = done;
    lastTs = now;
    if (!active()) {
      clearInterval(timer);
      timer = null;
    }
  } catch (e) {
    if (e.response?.status === 404) emit("close");
  }
}
async function refreshLogs() {
  try {
    logs.value = await getLogs(props.taskId);
  } catch (e) {}
}
async function retry() {
  try {
    await retryTask(props.taskId);
    await load();
    if (!timer) timer = setInterval(load, 2000);
    emit("changed");
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  }
}
async function retryFailedRes() {
  try {
    const r = await retryFailed(props.taskId);
    toast(`已重新派发 ${r.count} 个失败资源`, "ok");
    await load();
    if (!timer) timer = setInterval(load, 2000);
    emit("changed");
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  }
}
async function retryRes(r) {
  try {
    const updated = await retryResource(props.taskId, r.id);
    const i = task.value.resources.findIndex((x) => x.id === r.id);
    if (i >= 0) task.value.resources[i] = updated;
    setTimeout(load, 1500);
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  }
}
async function stop() {
  try {
    await cancelTask(props.taskId);
    await load();
    emit("changed");
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  }
}
async function pause() {
  try {
    await pauseTask(props.taskId);
    await load();
    emit("changed");
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  }
}
async function resume() {
  try {
    await resumeTask(props.taskId);
    await load();
    if (!timer) timer = setInterval(load, 2000);
    emit("changed");
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  }
}
async function loadManifest() {
  showManifest.value = !showManifest.value;
  if (!showManifest.value || manifest.value) return;
  try {
    manifest.value = await getManifest(props.taskId);
  } catch (e) {
    manifest.value = null;
    toast(e.response?.data?.detail || "该任务暂无产出清单", "warn");
  }
}
function archive() {
  const a = document.createElement("a");
  a.href = archiveHref.value;
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  a.remove();
}
function doRemove() {
  emit("remove", props.taskId);
}

watch(
  () => props.taskId,
  () => {
    clearInterval(timer);
    load();
    timer = setInterval(load, 2000);
  },
  { immediate: true }
);
onUnmounted(() => clearInterval(timer));
</script>

<template>
  <div class="drawer-mask" @click.self="emit('close')">
    <div class="drawer" v-if="task">
      <div class="drawer-head">
        <h3>任务 #{{ task.id }}</h3>
        <span class="badge" :class="badgeClass(task.status)">{{ statusLabel(task.status) }}</span>
        <span class="grow"></span>
        <button v-if="active()" class="ghost" @click="stop">停止</button>
        <button v-if="active()" class="ghost" @click="pause">暂停</button>
        <button v-if="task.status === 'paused'" class="primary" @click="resume">继续</button>
        <button
          v-if="['partial', 'failed', 'cancelled'].includes(task.status)"
          class="ghost"
          @click="retry"
        >重跑任务</button>
        <button
          v-if="failedSkippedCount > 0"
          class="ghost"
          @click="retryFailedRes"
          title="只重下失败/跳过的资源, 保留已成功的部分"
        >重试失败资源 ({{ failedSkippedCount }})</button>
        <button class="ghost" @click="archive">打包下载</button>
        <button class="ghost" @click="loadManifest">清单</button>
        <button class="ghost danger" @click="doRemove">删除</button>
        <button class="ghost drawer-close" @click="emit('close')">关闭</button>
      </div>

      <div class="drawer-tabs">
        <button class="drawer-tab" :class="{ on: tab === 'resources' }" @click="tab = 'resources'">
          资源 <span class="cnt">{{ task.resources.length }}</span>
        </button>
        <button class="drawer-tab" :class="{ on: tab === 'logs' }" @click="tab = 'logs'; refreshLogs()">
          日志 <span class="cnt">{{ logs.length }}</span>
        </button>
        <button class="drawer-tab" :class="{ on: tab === 'info' }" @click="tab = 'info'">信息</button>
      </div>

      <div class="drawer-body">
        <!-- 资源 -->
        <div v-show="tab === 'resources'">
          <div class="out-dir" v-if="task.name">{{ task.name }}</div>
          <div style="color:var(--muted); margin-bottom:8px; word-break:break-all;">{{ task.url }}</div>
          <div class="stat-line" v-if="Object.keys(stat).length">
            <span v-for="(v, k) in stat" :key="k" class="stat-item" :class="k">{{ k }} {{ v }}</span>
            <span v-if="ratePerMin > 0 && active()" class="stat-item rate">≈ {{ ratePerMin }} 张/分</span>
          </div>
          <div class="dup-report" v-if="dupCount">
            <span class="di">⚠️</span>
            本任务有 <b>{{ dupCount }}</b> 张与已有资源疑似重复（依据感知指纹），文件已保留、未删除。
          </div>
          <!-- 失败原因分布: 判断"换代理"还是"换采集器" -->
          <div class="fail-report" v-if="failReasons.length">
            <div class="fr-head">
              失败原因分布 <em>{{ failedSkippedCount }} 项</em>
              <span class="grow"></span>
              <button class="ghost mini" @click="retryFailedRes">全部重试</button>
            </div>
            <div class="fr-row" v-for="f in failReasons" :key="f.label">
              <span class="fr-lb">{{ f.label }}</span>
              <span class="fr-bar"><span class="fr-fill" :style="{ width: f.pct + '%' }"></span></span>
              <span class="fr-n">{{ f.n }}</span>
            </div>
          </div>
          <div class="view-tabs" v-if="task.resources.length">
            <button
              v-for="t in STATUS_TABS"
              :key="t.key"
              class="tab"
              :class="{ on: view === t.key }"
              @click="view = t.key"
            >
              {{ t.label }}
              <em v-if="t.key !== 'all' && t.key !== 'pending'">{{ stat[t.key] || 0 }}</em>
            </button>
            <span class="grow"></span>
            <button class="tab" :class="{ on: viewMode === 'grid' }" @click="viewMode = 'grid'" title="网格">▦</button>
            <button class="tab" :class="{ on: viewMode === 'list' }" @click="viewMode = 'list'" title="列表">☰</button>
          </div>
          <div class="resource-grid" v-if="visible.length && viewMode === 'grid'">
            <div class="resource-item" v-for="(r, i) in visible" :key="r.id" :title="r.url">
              <a
                v-if="r.file_url && r.type === 'image'"
                @click.prevent="openLb(lightboxImages.findIndex((x) => x.url === r.file_url))"
              >
                <img :src="r.file_url" loading="lazy" />
              </a>
              <div v-else class="video-placeholder">{{ typeIcon[r.type] || "📄" }}</div>
              <div class="meta">
                <div class="name">{{ (r.url || "").split("/").pop() || r.url }}</div>
                <span class="badge" :class="badgeClass(r.status)">{{ r.status }}</span>
                <span class="sz dup-hint" v-if="r.duplicate_of" title="感知指纹判定疑似相同, 文件已保留">
                  疑似重复 #{{ r.duplicate_of }}
                </span>
                <span class="sz" v-if="r.size">{{ fmtSize(r.size) }}</span>
                <button
                  v-if="canRetryResource && ['failed', 'skipped', 'filtered'].includes(r.status)"
                  class="ghost mini"
                  @click="retryRes(r)"
                >{{ r.status === 'filtered' ? '强制下载' : '重试' }}</button>
                <div class="note" v-if="r.note" :title="r.note">{{ r.note }}</div>
              </div>
            </div>
          </div>
          <table class="res-list" v-else-if="visible.length && viewMode === 'list'">
            <thead>
              <tr><th>状态</th><th>文件</th><th>类型</th><th>大小</th><th>操作</th></tr>
            </thead>
            <tbody>
              <tr v-for="r in visible" :key="r.id">
                <td><span class="badge" :class="badgeClass(r.status)">{{ r.status }}</span></td>
                <td class="nm">
                  <a v-if="r.file_url && r.type === 'image'" @click.prevent="openLb(lightboxImages.findIndex((x) => x.url === r.file_url))" class="lk">{{ (r.url || '').split('/').pop() || r.url }}</a>
                  <span v-else>{{ (r.url || '').split('/').pop() || r.url }}</span>
                  <span class="sz dup-hint" v-if="r.duplicate_of" title="感知指纹判定疑似相同, 文件已保留">疑似重复 #{{ r.duplicate_of }}</span>
                </td>
                <td>{{ typeIcon[r.type] || "📄" }} {{ r.type }}</td>
                <td>{{ r.size ? fmtSize(r.size) : "—" }}</td>
                <td>
                  <button
                    v-if="canRetryResource && ['failed', 'skipped', 'filtered'].includes(r.status)"
                    class="ghost mini"
                    @click="retryRes(r)"
                  >{{ r.status === 'filtered' ? '强制下载' : '重试' }}</button>
                </td>
              </tr>
            </tbody>
          </table>
          <div v-else-if="task.resources.length" class="empty">该分类下没有资源</div>
          <div v-else class="empty">未发现资源</div>
        </div>

        <!-- 日志 -->
        <div v-show="tab === 'logs'">
          <div class="logs">
            <div v-if="logs.length === 0" class="line">暂无日志</div>
            <div v-for="l in logs" :key="l.id" class="line" :class="l.level">
              <span class="t">{{ (l.created_time || '').slice(11) }}</span>{{ l.message }}
            </div>
          </div>
        </div>

        <!-- 信息 -->
        <div v-show="tab === 'info'">
          <div class="info-grid">
            <div class="k">状态</div><div class="v"><span class="badge" :class="badgeClass(task.status)">{{ statusLabel(task.status) }}</span></div>
            <div class="k">来源</div><div class="v mono">{{ task.url }}</div>
            <div class="k">采集器</div><div class="v">{{ task.collector }}</div>
            <div class="k">名称</div><div class="v">{{ task.name || "—" }}</div>
            <div class="k">创建时间</div><div class="v">{{ task.created_time }}</div>
            <div class="k" v-if="task.download_dir">下载目录</div><div class="v mono" v-if="task.download_dir">{{ task.download_dir }}</div>
            <div class="k">重试次数</div><div class="v">{{ task.retry_count }}</div>
          </div>
          <div class="progress-track" style="width:100%; margin:10px 0;">
            <span class="progress-fill" :style="{ width: (task.progress || 0) + '%' }"></span>
          </div>
          <div class="error-box" v-if="task.error">{{ task.error }}</div>
        </div>

        <!-- 清单 -->
        <div class="manifest" v-if="showManifest && manifest">
          <div class="m-head">
            <strong>产出清单 manifest.json</strong>
            <span class="m-sub">{{ manifest.resource_count }} 项 · {{ manifest.finished_time }} · {{ manifest.download_root }}</span>
          </div>
          <div class="m-table">
            <div class="m-tr m-th">
              <span>#</span><span>状态</span><span>文件</span><span>来源</span><span>sha256</span>
            </div>
            <div class="m-tr" v-for="i in manifest.resources" :key="i.id" :class="{ 'is-dup': i.duplicate_of }">
              <span>{{ i.seq }}</span>
              <span><em class="badge" :class="badgeClass(i.status)">{{ i.status }}</em></span>
              <span class="ell" :title="i.file || '—'">{{ i.file || "—" }}
                <em v-if="i.duplicate_of" class="tag dup" title="感知指纹判定疑似相同, 文件已保留">疑似重复 #{{ i.duplicate_of }}</em>
              </span>
              <span class="ell" :title="i.resolved_url || i.source_url">{{ (i.resolved_url || i.source_url || '').split('/').pop() }}
                <em v-if="i.resolved_url && i.resolved_url !== i.source_url" class="tag">镜像</em>
              </span>
              <span class="ell mono" :title="i.sha256 || ''">{{ (i.sha256 || '—').slice(0, 12) }}</span>
            </div>
          </div>
          <div class="m-note" v-if="dupCount">其中 {{ dupCount }} 项被标为疑似重复 —— 依据是感知指纹, 文件全部保留。</div>
          <div class="m-note" v-if="manifest.error">任务错误: {{ manifest.error }}</div>
        </div>
      </div>
    </div>

    <Lightbox
      v-if="showLb"
      :images="lightboxImages"
      :index="lbIndex < 0 ? 0 : lbIndex"
      @close="showLb = false"
      @update:index="lbIndex = $event"
    />
  </div>
</template>

<style scoped>
.grow { flex: 1; }
.drawer-tabs .cnt { color: var(--muted); font-size: 11px; margin-left: 4px; }
.info-grid { display: grid; grid-template-columns: 96px 1fr; gap: 8px 10px; font-size: 13px; }
.info-grid .k { color: var(--muted); }
.info-grid .v { word-break: break-all; }
.info-grid .mono { font-family: ui-monospace, Consolas, monospace; font-size: 12px; }
.view-tabs { display: flex; gap: 6px; margin: 8px 0 10px; flex-wrap: wrap; }
.tab {
  border: 1px solid var(--border); background: var(--panel-2); color: var(--muted);
  border-radius: 999px; padding: 3px 10px; font-size: 12px; cursor: pointer;
}
.tab.on { background: var(--accent); color: #fff; border-color: transparent; }
.tab em { font-style: normal; opacity: 0.75; margin-left: 4px; }
.manifest { margin-top: 14px; border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; background: var(--panel-2); }
.m-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; margin-bottom: 8px; }
.m-sub { color: var(--muted); font-size: 12px; word-break: break-all; }
.m-table { max-height: 320px; overflow: auto; font-size: 12px; }
.m-tr { display: grid; grid-template-columns: 36px 70px 1.2fr 1.4fr 110px; gap: 8px; padding: 4px 2px; border-bottom: 1px solid var(--border); align-items: center; }
.m-th { color: var(--muted); position: sticky; top: 0; background: var(--panel-2); }
.ell { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mono { font-family: ui-monospace, Consolas, monospace; color: var(--muted); }
.tag { font-style: normal; font-size: 11px; padding: 0 4px; border-radius: 4px; background: var(--panel); color: var(--accent); margin-left: 4px; }
.tag.dup { color: var(--warn); cursor: help; }
.m-tr.is-dup span:last-child, .m-tr.is-dup span:first-child { opacity: 0.7; }
.m-note { margin-top: 8px; color: var(--err); font-size: 12px; }
/* 失败原因分布 */
.fail-report {
  border: 1px solid var(--border); border-radius: 10px; padding: 8px 10px;
  margin-bottom: 10px; background: var(--panel-2);
}
.fr-head { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--muted); margin-bottom: 6px; }
.fr-head em { font-style: normal; color: var(--err); }
.fr-head .grow { flex: 1; }
.fr-row { display: grid; grid-template-columns: 90px 1fr 36px; gap: 8px; align-items: center; font-size: 12px; margin: 3px 0; }
.fr-lb { color: var(--muted); }
.fr-bar { height: 7px; border-radius: 4px; background: var(--panel); overflow: hidden; }
.fr-fill { display: block; height: 100%; border-radius: 4px; background: linear-gradient(90deg, #e0654f, #c94a35); transition: width .3s ease; }
.fr-n { text-align: right; color: var(--text); }
</style>
