<script setup>
import { onMounted, ref } from "vue";
import { getEnvDiagnose } from "../api";

// 环境诊断: Python / 浏览器 / ffmpeg / 磁盘 / 下载目录。后端每项给
// ok / warn / fail 三档 + 可行动的提示; 这里只负责展示并支持「重新检测」。
// warn 不是 fail —— ffmpeg 缺失只影响 m3u8, 不该拦住图片采集。
const items = ref([]);
const loading = ref(false);
const last = ref("");
const collapsed = ref(true);
const usable = ref(true);

async function run() {
  loading.value = true;
  try {
    const d = await getEnvDiagnose();
    items.value = d.items || [];
    last.value = d.checked_at || "";
    usable.value = !!d.usable;
  } catch (e) {
    items.value = [];
  } finally {
    loading.value = false;
  }
}

onMounted(run);
</script>

<template>
  <div class="card">
    <div class="panel-toggle">
      <button type="button" class="ghost" @click="collapsed = !collapsed">
        {{ collapsed ? "▸" : "▾" }} 环境诊断
      </button>
      <template v-if="!collapsed">
        <span class="summary" :class="usable ? 'muted' : ''">
          {{ usable ? "环境正常, 可以开工" : "存在阻碍项, 见下方红色条目" }}
        </span>
        <span class="grow"></span>
        <button type="button" class="ghost mini" :disabled="loading" @click="run">
          {{ loading ? "检测中..." : "重新检测" }}
        </button>
      </template>
      <span v-else class="summary muted">Python / 浏览器 / ffmpeg / 磁盘 一键自检</span>
    </div>

    <div v-if="!collapsed">
      <div v-if="items.length === 0 && !loading" class="empty">读取失败</div>
      <div v-else class="diag-grid">
        <div
          v-for="it in items"
          :key="it.key"
          class="diag-item"
          :class="it.level"
        >
          <div class="top">
            <span class="st" :class="it.level">
              <span>{{ it.label }}</span>
            </span>
          </div>
          <div class="val">{{ it.value }}</div>
          <div v-if="it.detail" class="detail" :title="it.detail">{{ it.detail }}</div>
          <div v-if="it.hint" class="hint">{{ it.hint }}</div>
        </div>
      </div>
      <div v-if="last" class="diag-foot">检测于 {{ last }}</div>
    </div>
  </div>
</template>

<style scoped>
.panel-toggle { display: flex; align-items: center; gap: 10px; }
.panel-toggle .summary { color: var(--accent); font-size: 12px; }
.panel-toggle .summary.muted { color: var(--muted); }
.panel-toggle .grow { flex: 1; }
.diag-foot { margin-top: 8px; color: var(--muted); font-size: 11px; text-align: right; }
</style>
