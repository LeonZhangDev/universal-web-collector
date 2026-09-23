<script setup>
import { onMounted, ref } from "vue";
import { getBandwidth, getEnvDiagnose, setBandwidth } from "../api";
import { toast } from "../toast";

// 环境诊断: Python / 浏览器 / ffmpeg / 磁盘 / 下载目录。后端每项给
// ok / warn / fail 三档 + 可行动的提示; 这里只负责展示并支持「重新检测」。
// warn 不是 fail —— ffmpeg 缺失只影响 m3u8, 不该拦住图片采集。
const items = ref([]);
const loading = ref(false);
const last = ref("");
const collapsed = ref(true);
const usable = ref(true);

// ---- 全局带宽上限 ----
// 与站点级 `domain_min_interval` 是**两件事**: 那个管"多久发一次请求"(受
// 站点风控约束), 这个管"每秒最多落多少字节"(受你的出口带宽约束)。图集站的图
// 单张就有几 MB, 请求间隔再大也可能把上行打满 —— 所以两个都要能调。
const bps = ref(0);
const saving = ref(false);
const PRESETS = [
  { v: 0, label: "不限" },
  { v: 1024 * 1024, label: "1 MB/s" },
  { v: 5 * 1024 * 1024, label: "5 MB/s" },
  { v: 20 * 1024 * 1024, label: "20 MB/s" },
];

function fmtBps(v) {
  const n = Number(v) || 0;
  if (n <= 0) return "不限速";
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(n % (1024 * 1024) ? 1 : 0)} MB/s`;
  return `${Math.round(n / 1024)} KB/s`;
}

async function loadBps() {
  try {
    const r = await getBandwidth();
    bps.value = r.bytes_per_sec || 0;
  } catch (e) {
    /* 限速读不到不影响诊断主体 */
  }
}

async function applyBps(v) {
  const value = Math.max(0, Math.floor(Number(v) || 0));
  saving.value = true;
  try {
    // ⚠️ 后端会**立即**改运行中的令牌桶(不是只写配置), 所以不需要重启服务。
    // 用返回的值回填而不是用本地值: 生效值才是事实, 否则界面会显示一个
    // "设置成功但其实没生效"的数字。
    const r = await setBandwidth(value);
    bps.value = r.bytes_per_sec || 0;
    toast(`下载带宽已设为 ${fmtBps(bps.value)}`, "ok");
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  } finally {
    saving.value = false;
  }
}

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

onMounted(() => {
  run();
  loadBps();
});
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

      <!-- 下载带宽上限: 全局生效, 改完立即作用于运行中的令牌桶(无需重启)。 -->
      <div class="bw-box">
        <div class="bw-head">
          <b>下载带宽上限</b>
          <span class="bw-cur">当前 {{ fmtBps(bps) }}</span>
          <span class="grow"></span>
          <span class="bw-tip">与站点请求间隔是两回事: 这个管每秒落多少字节</span>
        </div>
        <div class="bw-row">
          <button
            v-for="p in PRESETS"
            :key="p.v"
            class="bw-chip"
            :class="{ on: bps === p.v }"
            :disabled="saving"
            @click="applyBps(p.v)"
          >{{ p.label }}</button>
          <input
            v-model.number="bps"
            class="bw-num"
            type="number"
            min="0"
            step="1024"
            title="自定义上限, 单位 字节/秒; 0 = 不限"
          />
          <span class="bw-unit">B/s</span>
          <button class="ghost mini" :disabled="saving" @click="applyBps(bps)">应用</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.panel-toggle { display: flex; align-items: center; gap: 10px; }
.panel-toggle .summary { color: var(--accent); font-size: 12px; }
.panel-toggle .summary.muted { color: var(--muted); }
.panel-toggle .grow { flex: 1; }
.diag-foot { margin-top: 8px; color: var(--muted); font-size: 11px; text-align: right; }
/* 带宽上限 */
.bw-box {
  margin-top: 10px; padding: 9px 10px; border-radius: 9px;
  background: var(--panel-2); border: 1px solid var(--border);
}
.bw-head { display: flex; align-items: baseline; gap: 8px; font-size: 12px; }
.bw-cur { color: var(--accent); font-variant-numeric: tabular-nums; }
.bw-head .grow { flex: 1; }
.bw-tip { color: var(--muted); font-size: 11px; }
.bw-row { display: flex; align-items: center; gap: 6px; margin-top: 7px; flex-wrap: wrap; }
.bw-chip {
  background: var(--panel); color: var(--muted); border: 1px solid var(--border);
  border-radius: 7px; padding: 3px 9px; font-size: 12px; cursor: pointer;
  transition: color .15s, border-color .15s;
}
.bw-chip:hover:not(:disabled) { color: var(--text); }
.bw-chip.on { color: var(--accent); border-color: var(--accent); }
.bw-chip:disabled { opacity: .5; cursor: default; }
.bw-num {
  width: 108px; background: var(--panel); border: 1px solid var(--border);
  border-radius: 7px; padding: 4px 8px; color: var(--text); font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.bw-num:focus { outline: none; border-color: var(--accent); }
.bw-unit { color: var(--muted); font-size: 11px; }
</style>
