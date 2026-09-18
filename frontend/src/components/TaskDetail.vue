<script setup>
import { computed, onUnmounted, ref, watch } from "vue";
import {
  archiveUrl,
  cancelTask,
  getLogs,
  getManifest,
  getTask,
  retryResource,
  retryTask,
} from "../api";

const props = defineProps({ taskId: { type: Number, required: true } });
const emit = defineEmits(["close"]);

const task = ref(null);
const logs = ref([]);
let timer = null;
const active = () =>
  ["pending", "running", "extracting", "downloading"].includes(task.value?.status);

const canRetryResource = computed(
  () =>
    task.value &&
    ["success", "partial", "failed", "cancelled"].includes(task.value.status)
);

const typeIcon = { video: "▶", audio: "♪", doc: "📄", text: "📝" };

// ---- 图库视图 ----
// 资源多的时候, 真正想看的往往只有一类: 失败的(为什么失败)或被过滤的
// (是不是规则写错了)。状态筛选比翻列表快得多。
const STATUS_TABS = [
  { key: "all", label: "全部" },
  { key: "done", label: "已下载" },
  { key: "failed", label: "失败" },
  { key: "filtered", label: "已过滤" },
  { key: "pending", label: "待处理" },
];
const view = ref("all");
const visible = computed(() => {
  const rs = task.value?.resources || [];
  if (view.value === "all") return rs;
  return rs.filter((r) =>
    view.value === "pending"
      ? ["pending", "downloading", "skipped"].includes(r.status)
      : r.status === view.value
  );
});
const countOf = (key) => stat.value[key] || 0;

const manifest = ref(null);
const showManifest = ref(false);
const archiveHref = computed(() => archiveUrl(props.taskId));

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

// 各状态资源计数, 让"过滤掉了多少"一目了然
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

async function load() {
  try {
    task.value = await getTask(props.taskId);
    logs.value = await getLogs(props.taskId);
    if (!active()) {
      clearInterval(timer);
      timer = null;
    }
  } catch (e) {
    if (e.response?.status === 404) {
      // 任务被删除
      emit("close");
    }
  }
}

async function retry() {
  try {
    await retryTask(props.taskId);
    await load();
    if (!timer) timer = setInterval(load, 2000);
  } catch (e) {
    alert(e.response?.data?.detail || String(e));
  }
}

async function retryRes(r) {
  try {
    const updated = await retryResource(props.taskId, r.id);
    const i = task.value.resources.findIndex((x) => x.id === r.id);
    if (i >= 0) task.value.resources[i] = updated;
    setTimeout(load, 1500); // 稍后刷新最终状态
  } catch (e) {
    alert(e.response?.data?.detail || String(e));
  }
}

async function stop() {
  if (!confirm("停止任务? 已下载的文件会保留, 未完成的部分会跳。")) return;
  try {
    await cancelTask(props.taskId);
    await load();
  } catch (e) {
    alert(e.response?.data?.detail || String(e));
  }
}

async function loadManifest() {
  showManifest.value = !showManifest.value;
  if (!showManifest.value || manifest.value) return;
  try {
    manifest.value = await getManifest(props.taskId);
  } catch (e) {
    manifest.value = null;
    alert(e.response?.data?.detail || "该任务暂无产出清单");
  }
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
  <div class="card" v-if="task">
    <div style="display:flex; align-items:center;">
      <h3>任务 #{{ task.id }} · <span class="badge" :class="task.status">{{ task.status }}</span></h3>
      <span style="flex:1"></span>
      <button v-if="active()" class="ghost" @click="stop">停止</button>
      <button v-if="['partial', 'failed', 'cancelled'].includes(task.status)" class="ghost" @click="retry">
        重跑任务
      </button>
      <a class="ghost btn-link" :href="archiveHref">打包下载</a>
      <button class="ghost" @click="loadManifest">清单</button>
      <button class="ghost" style="margin-left:8px" @click="emit('close')">关闭</button>
    </div>

    <div style="color:var(--muted); margin-bottom:8px; word-break: break-all;">{{ task.url }}</div>

    <div class="out-dir" v-if="task.download_dir">
      自定义输出目录 <code>{{ task.download_dir }}</code> / {{ task.id }} /
    </div>

    <div class="progress-track" style="width:100%">
      <span class="progress-fill" :style="{ width: task.progress + '%' }"></span>
    </div>

    <div class="error-box" v-if="task.error">{{ task.error }}</div>

    <div class="detail-grid" style="margin-top:14px;">
      <div>
        <h3>资源 ({{ task.resources.length }})</h3>
        <div class="stat-line" v-if="Object.keys(stat).length">
          <span v-for="(v, k) in stat" :key="k" class="stat-item" :class="k">{{ k }} {{ v }}</span>
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
            <em v-if="t.key !== 'all' && t.key !== 'pending'">{{ countOf(t.key) }}</em>
          </button>
        </div>

        <div class="resource-grid" v-if="visible.length">
          <div class="resource-item" v-for="r in visible" :key="r.id" :title="r.url">
            <a v-if="r.file_url && r.type === 'image'" :href="r.file_url" target="_blank">
              <img :src="r.file_url" loading="lazy" />
            </a>
            <div v-else class="video-placeholder">{{ typeIcon[r.type] || "📄" }}</div>
            <div class="meta">
              <div class="name">{{ r.url.split("/").pop() || r.url }}</div>
              <span class="badge" :class="badgeClass(r.status)">{{ r.status }}</span>
              <span class="sz" v-if="r.size">{{ fmtSize(r.size) }}</span>
              <button
                v-if="canRetryResource && ['failed', 'skipped', 'filtered'].includes(r.status)"
                class="ghost mini"
                :title="r.status === 'filtered' ? '忽略过滤规则, 强制下载此资源' : '重试此资源'"
                @click="retryRes(r)"
              >{{ r.status === "filtered" ? "强制下载" : "重试" }}</button>
              <div class="note" v-if="r.note" :title="r.note">{{ r.note }}</div>
            </div>
          </div>
        </div>
        <div v-else-if="task.resources.length" class="empty">该分类下没有资源</div>
        <div v-else class="empty">未发现资源</div>
      </div>

      <div>
        <h3>日志</h3>
        <div class="logs">
          <div v-if="logs.length === 0" class="line">暂无日志</div>
          <div v-for="l in logs" :key="l.id" class="line" :class="l.level">
            <span class="t">{{ l.created_time.slice(11) }}</span>{{ l.message }}
          </div>
        </div>
      </div>
    </div>

    <div class="manifest" v-if="showManifest && manifest">
      <div class="m-head">
        <strong>产出清单 manifest.json</strong>
        <span class="m-sub">
          {{ manifest.resource_count }} 项 · {{ manifest.finished_time }} · {{ manifest.download_root }}
        </span>
      </div>
      <div class="m-table">
        <div class="m-tr m-th">
          <span>#</span><span>状态</span><span>文件</span><span>来源</span><span>sha256</span>
        </div>
        <div class="m-tr" v-for="i in manifest.resources" :key="i.id">
          <span>{{ i.seq }}</span>
          <span><em class="badge" :class="badgeClass(i.status)">{{ i.status }}</em></span>
          <span class="ell" :title="i.file || '—'">{{ i.file || "—" }}</span>
          <span class="ell" :title="i.resolved_url || i.source_url">
            {{ (i.resolved_url || i.source_url || "").split("/").pop() }}
            <em v-if="i.resolved_url && i.resolved_url !== i.source_url" class="tag">镜像</em>
          </span>
          <span class="ell mono" :title="i.sha256 || ''">{{ (i.sha256 || "—").slice(0, 12) }}</span>
        </div>
      </div>
      <div class="m-note" v-if="manifest.error">任务错误: {{ manifest.error }}</div>
    </div>
  </div>
</template>

<style scoped>
.btn-link {
  display: inline-flex;
  align-items: center;
  text-decoration: none;
  margin-left: 8px;
}
.view-tabs {
  display: flex;
  gap: 6px;
  margin: 8px 0 10px;
  flex-wrap: wrap;
}
.tab {
  border: 1px solid var(--border);
  background: var(--panel-2);
  color: var(--muted);
  border-radius: 999px;
  padding: 3px 10px;
  font-size: 12px;
  cursor: pointer;
}
.tab.on {
  background: var(--accent);
  color: #fff;
  border-color: transparent;
}
.tab em {
  font-style: normal;
  opacity: 0.75;
  margin-left: 4px;
}
.manifest {
  margin-top: 14px;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 10px 12px;
  background: var(--panel-2);
}
.m-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
.m-sub {
  color: var(--muted);
  font-size: 12px;
  word-break: break-all;
}
.m-table {
  max-height: 320px;
  overflow: auto;
  font-size: 12px;
}
.m-tr {
  display: grid;
  grid-template-columns: 36px 70px 1.2fr 1.4fr 110px;
  gap: 8px;
  padding: 4px 2px;
  border-bottom: 1px solid var(--border);
  align-items: center;
}
.m-th {
  color: var(--muted);
  position: sticky;
  top: 0;
  background: var(--panel-2);
}
.ell {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.mono {
  font-family: ui-monospace, Consolas, monospace;
  color: var(--muted);
}
.tag {
  font-style: normal;
  font-size: 11px;
  padding: 0 4px;
  border-radius: 4px;
  background: var(--panel);
  color: var(--accent);
  margin-left: 4px;
}
.m-note {
  margin-top: 8px;
  color: var(--err);
  font-size: 12px;
}
</style>
