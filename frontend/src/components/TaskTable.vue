<script setup>
import { ref, watch } from "vue";
import { STATUS_GROUPS, statusLabel } from "../status";
import { bulkAction } from "../api";
import { toast } from "../toast";

const props = defineProps({
  tasks: { type: Array, default: () => [] },
  total: { type: Number, default: 0 },
  pages: { type: Number, default: 1 },
  page: { type: Number, default: 1 },
  pageSize: { type: Number, default: 20 },
  statusFilter: { type: String, default: "" },
  loading: { type: Boolean, default: false },
  activeId: { type: Number, default: 0 },
  filterActive: { type: Boolean, default: false },
});
const emit = defineEmits([
  "search",
  "filter",
  "goto",
  "select",
  "remove",
  "pause",
  "resume",
  "changed",
]);

const q = ref("");
let t = null;
function onSearch() {
  clearTimeout(t);
  t = setTimeout(() => emit("search", q.value.trim()), 300);
}
watch(
  () => props.statusFilter,
  () => {
    /* keep select in sync if parent resets */
  }
);

function badgeClass(s) {
  if (["running", "extracting", "downloading"].includes(s)) return "running";
  return s; // pending/success/failed/cancelled/partial/paused 都有对应样式
}
const ACTIVE = ["pending", "running", "extracting", "downloading"];

// ---- 多选 + 批量操作 ----
// 选中集合存 id(跨翻页保留), 不依赖 DOM 状态。
const selected = ref(new Set());
const selectedCount = ref(0);
function syncCount() {
  selectedCount.value = selected.value.size;
}
function isSel(id) {
  return selected.value.has(id);
}
function toggleRow(id) {
  if (selected.value.has(id)) selected.value.delete(id);
  else selected.value.add(id);
  syncCount();
}
function togglePage() {
  const allOnPage = props.tasks.every((t) => selected.value.has(t.id));
  for (const t of props.tasks) {
    if (allOnPage) selected.value.delete(t.id);
    else selected.value.add(t.id);
  }
  syncCount();
}
function clearSel() {
  selected.value.clear();
  syncCount();
}
const pageAllOn = ref(false);
watch(
  () => props.tasks,
  () => {
    pageAllOn.value =
      props.tasks.length > 0 && props.tasks.every((t) => selected.value.has(t.id));
  }
);

async function runBulk(action) {
  if (!selected.value.size) {
    toast("先勾选要操作的任务", "warn");
    return;
  }
  if (action === "delete") {
    if (!confirm(`确认删除选中的 ${selected.value.size} 个任务？(仅删除任务记录, 不删文件)`))
      return;
  }
  const ids = [...selected.value];
  try {
    const r = await bulkAction(action, ids, action === "delete");
    const okN = (r.ok || []).length;
    if (action === "delete") {
      toast(`已删除 ${okN} 个任务`, "ok");
    } else {
      const skipped = (r.skipped || []).length + (r.not_found || []).length;
      toast(`已对 ${okN} 个任务执行「${action}」${skipped ? `, ${skipped} 个跳过` : ""}`, "ok");
    }
    clearSel();
    emit("changed");
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  }
}
</script>

<template>
  <div>
    <div class="task-toolbar">
      <input
        class="search-input"
        v-model="q"
        @input="onSearch"
        placeholder="搜索 URL 或任务名…"
      />
      <select
        class="status-select"
        :value="statusFilter"
        @change="emit('filter', $event.target.value)"
      >
        <option v-for="g in STATUS_GROUPS" :key="g.value" :value="g.value">
          {{ g.label }}
        </option>
      </select>
      <span class="grow"></span>
      <span class="pginfo">共 {{ total }} 个任务</span>
    </div>

    <!-- 批量操作栏: 勾选后出现 -->
    <div class="bulk-bar" v-if="selectedCount > 0">
      <span class="sel-count">已选 <b>{{ selectedCount }}</b> 个</span>
      <button class="ghost mini" @click="runBulk('pause')">批量暂停</button>
      <button class="ghost mini" @click="runBulk('resume')">批量继续</button>
      <button class="ghost mini" @click="runBulk('cancel')">批量取消</button>
      <button class="ghost mini" @click="runBulk('retry')">批量重跑</button>
      <button class="ghost mini danger" @click="runBulk('delete')">批量删除</button>
      <span class="grow"></span>
      <span class="hint">选择跨翻页保留</span>
      <button class="ghost mini" @click="clearSel">清空</button>
    </div>

    <!-- 加载中: 骨架屏占位, 比转圈更顺眼 -->
    <div v-if="loading && tasks.length === 0" class="card-flush">
      <div class="skeleton-row" v-for="n in 5" :key="n">
        <span class="sk dot"></span>
        <span class="sk sq"></span>
        <span class="sk line"></span>
        <span class="sk dot"></span>
        <span class="sk line"></span>
      </div>
    </div>

    <!-- 空状态: 区分"还没有任务"与"搜索无结果" -->
    <div
      v-else-if="tasks.length === 0"
      class="empty-state"
    >
      <template v-if="!filterActive">
        <div class="ico">📥</div>
        <div class="t">还没有任务</div>
        <div class="s">在上方粘贴链接, 创建第一个采集任务</div>
      </template>
      <template v-else>
        <div class="ico">🔍</div>
        <div class="t">没有匹配的任务</div>
        <div class="s">试试放宽搜索词或切换状态筛选</div>
      </template>
    </div>

    <table v-else>
      <thead>
        <tr>
          <th style="width:32px">
            <input
              type="checkbox"
              class="row-check"
              :checked="pageAllOn"
              @change="togglePage"
              title="全选本页"
            />
          </th>
          <th style="width:48px">#</th>
          <th>任务</th>
          <th style="width:150px">采集器</th>
          <th style="width:96px">状态</th>
          <th style="width:150px">进度</th>
          <th style="width:120px"></th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="t in tasks"
          :key="t.id"
          class="row"
          :class="{ active: t.id === activeId }"
          @click="emit('select', t.id)"
        >
          <td @click.stop>
            <input
              type="checkbox"
              class="row-check"
              :checked="isSel(t.id)"
              @change="toggleRow(t.id)"
            />
          </td>
          <td>{{ t.id }}</td>
          <td>
            <div class="task-main">
              <span class="nm" :title="t.name || t.url">{{ t.name || "(未命名)" }}</span>
              <span class="src" :title="t.url">{{ t.url }}</span>
            </div>
          </td>
          <td><span class="collector">{{ t.collector }}</span></td>
          <td>
            <span class="badge" :class="badgeClass(t.status)">{{ statusLabel(t.status) }}</span>
          </td>
          <td>
            <span class="progress-track">
              <span class="progress-fill" :style="{ width: (t.progress || 0) + '%' }"></span>
            </span>
            <span class="pct">{{ t.progress || 0 }}%</span>
          </td>
          <td>
            <span class="row-actions">
              <button
                v-if="ACTIVE.includes(t.status)"
                class="ghost mini"
                title="暂停(保留已下载文件, 之后可续跑)"
                @click.stop="emit('pause', t.id)"
              >暂停</button>
              <button
                v-if="t.status === 'paused'"
                class="primary mini"
                title="从断点继续(只补下缺失部分)"
                @click.stop="emit('resume', t.id)"
              >继续</button>
              <button
                class="ghost danger"
                title="删除任务(可选择是否同时删除已下载的文件)"
                @click.stop="emit('remove', t.id)"
              >✕</button>
            </span>
          </td>
        </tr>
      </tbody>
    </table>

    <div class="pager" v-if="pages > 1">
      <button class="ghost" :disabled="page <= 1" @click="emit('goto', page - 1)">上一页</button>
      <span class="pginfo">第 {{ page }} / {{ pages }} 页</span>
      <button class="ghost" :disabled="page >= pages" @click="emit('goto', page + 1)">下一页</button>
      <span class="grow"></span>
      <span class="pginfo">每页 {{ pageSize }} 条</span>
    </div>
  </div>
</template>

<style scoped>
.status-select {
  background: var(--panel-2); border: 1px solid var(--border); border-radius: 8px;
  color: var(--text); padding: 7px 8px; font-size: 13px; outline: none;
}
.pct { color: var(--muted); font-size: 12px; margin-left: 6px; }
.task-toolbar .pginfo { color: var(--muted); font-size: 12px; }
.pager .pginfo { color: var(--muted); font-size: 12px; }
.pager .grow { flex: 1; }
.card-flush { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 8px 12px; margin-bottom: 16px; }
/* 暂停态: 用蓝灰, 区别于失败红与进行中蓝 */
.badge.paused { background: #3a4250; color: #9fb0c2; }
.badge.partial { background: #4a3520; color: #e0a458; }
</style>
