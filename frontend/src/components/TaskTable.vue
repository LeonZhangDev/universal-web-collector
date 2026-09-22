<script setup>
import { ref, watch } from "vue";
import { STATUS_GROUPS, STATUS_LABELS, statusLabel } from "../status";

const props = defineProps({
  tasks: { type: Array, default: () => [] },
  total: { type: Number, default: 0 },
  pages: { type: Number, default: 1 },
  page: { type: Number, default: 1 },
  pageSize: { type: Number, default: 20 },
  statusFilter: { type: String, default: "" },
  loading: { type: Boolean, default: false },
});
const emit = defineEmits(["search", "filter", "goto", "select", "remove", "pause", "resume"]);

const q = ref("");
let t = null;
function onSearch() {
  clearTimeout(t);
  t = setTimeout(() => emit("search", q.value.trim()), 300);
}
watch(
  () => props.statusFilter,
  (v) => { /* keep select in sync if parent resets */ }
);

function badgeClass(s) {
  if (["running", "extracting", "downloading"].includes(s)) return "running";
  return s; // pending/success/failed/cancelled/partial/paused 都有对应样式
}
const ACTIVE = ["pending", "running", "extracting", "downloading"];
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

    <div v-if="loading && tasks.length === 0" class="empty">加载中…</div>
    <div v-else-if="tasks.length === 0" class="empty">暂无任务, 在上方输入 URL 创建第一个采集任务</div>
    <table v-else>
      <thead>
        <tr>
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
          :class="{ active: false }"
          @click="emit('select', t.id)"
        >
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
/* 暂停态: 用蓝灰, 区别于失败红与进行中蓝 */
.badge.paused { background: #3a4250; color: #9fb0c2; }
.badge.partial { background: #4a3520; color: #e0a458; }
</style>
