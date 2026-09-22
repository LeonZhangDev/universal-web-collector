<script setup>
import { computed, onMounted, onUnmounted, ref } from "vue";
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

// ---- SVG 折线图: 近 30 天资源新增 ----
const W = 300,
  H = 76,
  PAD = 6;
const lineData = computed(() => {
  const d = (stats.value && stats.value.by_date) || {};
  const keys = Object.keys(d).sort();
  const vals = keys.map((k) => d[k]);
  const max = Math.max(1, ...vals);
  return keys.map((k, i) => {
    const x = PAD + (keys.length <= 1 ? 0 : (i / (keys.length - 1)) * (W - 2 * PAD));
    const y = H - PAD - (vals[i] / max) * (H - 2 * PAD);
    return { x: +x.toFixed(1), y: +y.toFixed(1), k, v: vals[i] };
  });
});
const linePath = computed(() => lineData.value.map((p, i) => `${i ? "L" : "M"}${p.x},${p.y}`).join(" "));
const lineArea = computed(() => {
  if (!lineData.value.length) return "";
  const f = lineData.value[0];
  const l = lineData.value[lineData.value.length - 1];
  return `${linePath.value} L${l.x},${H - PAD} L${f.x},${H - PAD} Z`;
});

// ---- SVG 饼图: 采集器分布 ----
const PIE_COLORS = ["#5b8def", "#e0a458", "#6cc28f", "#c77dde", "#e06c75", "#56b6c2"];
const pieData = computed(() => {
  const c = (stats.value && stats.value.by_collector) || {};
  const entries = Object.entries(c);
  const total = entries.reduce((s, [, v]) => s + v, 0) || 1;
  let acc = 0;
  return entries.map(([name, v], i) => {
    const frac = v / total;
    const a0 = acc * 2 * Math.PI;
    acc += frac;
    const a1 = acc * 2 * Math.PI;
    const large = frac > 0.5 ? 1 : 0;
    const x0 = (16 + 14 * Math.cos(a0)).toFixed(2);
    const y0 = (16 + 14 * Math.sin(a0)).toFixed(2);
    const x1 = (16 + 14 * Math.cos(a1)).toFixed(2);
    const y1 = (16 + 14 * Math.sin(a1)).toFixed(2);
    return {
      name,
      v,
      color: PIE_COLORS[i % PIE_COLORS.length],
      path: `M16,16 L${x0},${y0} A14,14 0 ${large} 1 ${x1},${y1} Z`,
    };
  });
});

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

    <!-- 图表区: 折线 + 饼图 -->
    <div v-if="stats" class="charts">
      <div class="chart-box">
        <div class="chart-title">近 30 天资源新增</div>
        <svg :viewBox="`0 0 ${W} ${H}`" class="line" preserveAspectRatio="none">
          <path :d="lineArea" class="line-area" />
          <path :d="linePath" class="line-path" />
          <circle
            v-for="(p, i) in lineData"
            :key="i"
            :cx="p.x"
            :cy="p.y"
            r="1.8"
            class="line-dot"
          >
            <title>{{ p.k }}: {{ p.v }}</title>
          </circle>
        </svg>
        <div v-if="!lineData.length" class="chart-empty">暂无数据</div>
      </div>
      <div class="chart-box">
        <div class="chart-title">采集器分布</div>
        <div class="pie-row">
          <svg viewBox="0 0 32 32" class="pie">
            <path v-for="(s, i) in pieData" :key="i" :d="s.path" :fill="s.color" />
          </svg>
          <div class="legend">
            <div v-for="(s, i) in pieData" :key="i" class="lg">
              <span class="sw" :style="{ background: s.color }"></span>
              <span class="nm">{{ s.name }}</span>
              <span class="ct">{{ s.v }}</span>
            </div>
            <div v-if="!pieData.length" class="chart-empty">暂无数据</div>
          </div>
        </div>
      </div>
    </div>

    <!-- 失败原因聚合 -->
    <div v-if="stats && stats.failure_reasons && stats.failure_reasons.length" class="mini-box">
      <div class="chart-title">失败原因 TOP</div>
      <div v-for="f in stats.failure_reasons" :key="f.reason" class="reason-row">
        <span class="rct">{{ f.count }}</span>
        <span class="rtxt" :title="f.reason">{{ f.reason }}</span>
      </div>
    </div>

    <!-- 去重报表 -->
    <div v-if="stats && stats.duplicates" class="dup-box">
      <span class="ico">♻️</span>
      感知去重标记
      <b>{{ stats.duplicates.marked }}</b> 个资源疑似重复,
      已省下 <b>{{ fmtBytes(stats.duplicates.bytes_saved) }}</b>
    </div>
  </div>
</template>

<style scoped>
.panel-toggle { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.panel-toggle .lbl { color: var(--muted); font-size: 13px; }
.panel-toggle .summary { color: var(--muted); font-size: 12px; }
.charts { display: grid; grid-template-columns: 1.4fr 1fr; gap: 12px; margin-top: 12px; }
.chart-box {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 10px;
  background: var(--panel-2);
}
.chart-title { font-size: 12px; color: var(--muted); margin-bottom: 6px; }
.line { width: 100%; height: 76px; display: block; }
.line-area { fill: rgba(91, 141, 239, 0.12); }
.line-path { fill: none; stroke: var(--accent); stroke-width: 1.5; }
.line-dot { fill: var(--accent); }
.chart-empty { font-size: 11px; color: var(--muted); text-align: center; padding: 20px 0; }
.pie-row { display: flex; align-items: center; gap: 12px; }
.pie { width: 64px; height: 64px; flex: none; }
.legend { flex: 1; display: flex; flex-direction: column; gap: 3px; font-size: 12px; }
.lg { display: flex; align-items: center; gap: 6px; }
.sw { width: 10px; height: 10px; border-radius: 2px; flex: none; }
.nm { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); }
.ct { color: var(--muted); font-family: ui-monospace, Consolas, monospace; }
.mini-box { margin-top: 12px; border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; background: var(--panel-2); }
.reason-row { display: flex; align-items: center; gap: 8px; padding: 3px 0; font-size: 12px; }
.rct {
  min-width: 28px;
  text-align: center;
  background: rgba(224, 92, 92, 0.15);
  color: var(--err);
  border-radius: 4px;
  padding: 0 6px;
  font-family: ui-monospace, Consolas, monospace;
}
.rtxt { flex: 1; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dup-box {
  margin-top: 12px;
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--muted);
  padding: 8px 10px;
  border: 1px solid #2f5a44;
  border-radius: 8px;
  background: rgba(108, 194, 143, 0.08);
}
.dup-box b { color: var(--ok); }
.dup-box .ico { font-size: 14px; }
</style>
