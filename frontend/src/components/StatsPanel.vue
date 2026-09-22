<script setup>
import { onMounted, onUnmounted, ref } from "vue";
import { getStats } from "../api";
import { statusLabel } from "../status";

const stats = ref(null);
let timer = null;

async function load() {
  try {
    stats.value = await getStats();
  } catch (e) {
    /* 后端不可达时保留上次数据 */
  }
}
function fmtBytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = Number(n);
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return i === 0 ? `${v} B` : `${v.toFixed(1)} ${units[i]}`;
}

onMounted(() => {
  load();
  timer = setInterval(load, 5000);
});
onUnmounted(() => clearInterval(timer));
</script>

<template>
  <div class="card">
    <div class="panel-toggle">
      <span class="lbl">采集统计</span>
      <span class="summary muted">任务量与产出概览(每 5 秒刷新)</span>
    </div>
    <div v-if="stats" class="stats-grid">
      <div class="stat-card active">
        <div class="lbl">进行中</div>
        <div class="num">{{ stats.active }}</div>
        <div class="sub">运行中 / 提取中 / 下载中</div>
      </div>
      <div class="stat-card">
        <div class="lbl">任务总数</div>
        <div class="num">{{ stats.total_tasks }}</div>
        <div class="sub">{{ Object.keys(stats.by_collector || {}).length }} 种采集器</div>
      </div>
      <div class="stat-card">
        <div class="lbl">资源总数</div>
        <div class="num">{{ stats.total_resources }}</div>
        <div class="sub">已下载占 {{ stats.by_status && stats.by_status.done ? stats.by_status.done : 0 }}</div>
      </div>
      <div class="stat-card">
        <div class="lbl">累计体积</div>
        <div class="num">{{ fmtBytes(stats.total_bytes) }}</div>
        <div class="sub">仅已成功下载的资源</div>
      </div>
    </div>
    <div v-if="stats && stats.by_status && Object.keys(stats.by_status).length" class="stat-bar">
      <span v-for="(v, k) in stats.by_status" :key="k" class="seg-mini">
        {{ statusLabel(k) }} {{ v }}
      </span>
    </div>
  </div>
</template>

<style scoped>
.panel-toggle { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.panel-toggle .lbl { color: var(--muted); font-size: 13px; }
.panel-toggle .summary { color: var(--muted); font-size: 12px; }
</style>
