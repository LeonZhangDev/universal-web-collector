<script setup>
import { computed, onMounted, ref, watch } from "vue";
import FolderPicker from "./FolderPicker.vue";
import { toast } from "../toast";
import {
  batchCreateTasks,
  createTask,
  previewTask,
  resolveCollector,
} from "../api";
import {
  BUILTIN_PRESETS,
  applyPreset,
  deleteUserPreset,
  exportPresetsText,
  importPresetsText,
  loadUserPresets,
  saveUserPreset,
} from "../presets";

const props = defineProps({
  collectors: { type: Array, default: () => ["auto"] },
  config: { type: Object, default: () => ({}) },
});
const emit = defineEmits(["created"]);

// ---- 采集器中文名(后端只给代号, 展示层做映射) ----
const collectorLabel = {
  auto: "自动识别 (推荐)",
  generic: "通用网页(浏览器抓取)",
  xchina: "XChina 页面(浏览器抓取)",
  xchina_gallery: "XChina 图集/视频相册",
  xchina_video: "XChina 视频页(过 Cloudflare 抓 m3u8)",
  xchina_aggregate: "XChina 聚合页(展开多个相册)",
};

const qualityLabel = {
  original: "原图 (画质最高)",
  1200: "1200px WebP",
  800: "800px WebP",
  600: "600px WebP (最小)",
};
const mediaLabel = {
  auto: "自动 (相册里有什么采什么)",
  image: "仅图片",
  video: "仅视频",
  both: "图片 + 视频",
};
const albumTitleLabel = {
  clean: "相册名 (取 <title>, 去站名后缀)",
  full: "完整 <title>",
  h1: "页面 <h1> (信息更全)",
  id: "图集 ID (不开浏览器)",
};
// 下载顺序 = 资源**提交给下载池**的顺序。线程池是固定大小的, 空闲 worker 按提交
// 序取任务, 所以这个顺序就是事实上的优先级。它只影响"谁先开始", 不改变"下不下"。
const orderLabel = {
  original: "站点顺序 (默认)",
  video_first: "视频优先 (最慢的先开跑)",
  small_first: "小文件优先 (进度涨得快)",
};

// ---- 表单状态(单一 reactive, 方便预设整体套用) ----
const form = ref({
  url: "",
  collector: "auto",
  downloadDir: "",
  quality: "original",
  media: "auto",
  albumTitle: "clean",
  // 提交顺序(下载优先级)。默认 "original" = 与历史行为完全一致。
  resourceOrder: "original",
  aggregateDepth: 1,
  aggregateMax: 50,
  selTypes: [],
  exts: "",
  excludeExts: "",
  keywords: "",
  excludeKeywords: "",
  minSize: { num: "", unit: "KB" },
  maxSize: { num: "", unit: "MB" },
  minWidth: "",
  minHeight: "",
  dedupPerceptual: true,
  proxy: "",
});

const mode = ref("single"); // single | batch
const creating = ref(false);
const showAdvanced = ref(false);
const showPicker = ref(false);

// ---- 自动识别(单条模式) ----
const resolved = ref(null);
const manualWarning = ref("");
const effectiveCollector = computed(() => {
  if (form.value.collector !== "auto") return form.value.collector;
  return resolved.value?.collector || "generic";
});
const isGallery = computed(() => effectiveCollector.value === "xchina_gallery");
const isVideo = computed(() => effectiveCollector.value === "xchina_video");
const isAggregate = computed(() => effectiveCollector.value === "xchina_aggregate");
const isGalleryLike = computed(
  () => isGallery.value || isVideo.value || isAggregate.value
);

const urlPlaceholder = computed(() => {
  const ec = effectiveCollector.value;
  if (form.value.collector === "auto")
    return "粘贴任意链接或图集 ID, 自动识别(相册页 / 视频页 URL / 图片直链 / 6aa113208a506 均可)";
  if (ec === "xchina_gallery")
    return "相册 ID / 相册页 URL / 任意一张图片或视频 URL 都行";
  if (ec === "xchina_video") return "视频页 URL 或带签名的 m3u8 直链";
  if (ec === "xchina_aggregate")
    return "聚合页 URL: 模特/演员页、索引页、全量列表";
  return "输入采集 URL, 例如 https://example.com/photoShow.html?id=xxx";
});

let resolveTimer = null;
let resolveSeq = 0;
async function autoResolve() {
  const u = form.value.url.trim();
  if (form.value.collector !== "auto" || !u) {
    resolved.value = null;
    return;
  }
  const seq = ++resolveSeq;
  const got = await resolveCollector(u);
  if (seq === resolveSeq) resolved.value = got;
}
watch(
  () => [form.value.url, form.value.collector],
  () => {
    resolved.value = null;
    manualWarning.value = "";
    clearTimeout(resolveTimer);
    resolveTimer = setTimeout(autoResolve, 300);
  }
);
const resolveHint = computed(() => {
  if (form.value.collector !== "auto" || !form.value.url.trim() || !resolved.value)
    return null;
  const r = resolved.value;
  if (!r.collector) return { kind: "bad", text: "无法识别该输入, 请手动选择采集器" };
  const name = collectorLabel[r.collector] || r.collector;
  if (r.ambiguous) return { kind: "warn", text: `识别结果不唯一: ${r.reason}` };
  return { kind: "ok", text: `已识别为「${name}」`, reason: r.reason };
});

// ---- 预览(单条) ----
const preview = ref(null);
const previewError = ref("");
const previewing = ref(false);

// ---- 预设 ----
const userPresets = ref(loadUserPresets());
const allPresets = computed(() => [...BUILTIN_PRESETS, ...userPresets.value]);
const activePreset = ref("");

function applyPresetByName(name) {
  const p = allPresets.value.find((x) => x.name === name);
  if (!p) return;
  applyPreset(form.value, p.opts);
  activePreset.value = name;
  toast(`已套用预设「${name}」`, "ok");
}
function saveCurrentPreset() {
  const name = prompt("给这个预设起个名字:");
  if (!name) return;
  userPresets.value = saveUserPreset(name.trim(), snapshotOpts());
  activePreset.value = name.trim();
  toast(`已保存预设「${name.trim()}」`, "ok");
}
function removePreset(name) {
  userPresets.value = deleteUserPreset(name);
  if (activePreset.value === name) activePreset.value = "";
}
// ---- 预设配置导入/导出(可迁移分享) ----
function exportPresets() {
  const text = exportPresetsText();
  const blob = new Blob([text], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "uwc-presets.json";
  a.click();
  URL.revokeObjectURL(a.href);
  toast("已导出预设配置", "ok");
}
function onImportPresets(e) {
  const file = e.target.files?.[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    try {
      const r = importPresetsText(String(reader.result || ""));
      userPresets.value = r.presets;
      toast(`已导入 ${r.imported} 个用户预设`, "ok");
    } catch (err) {
      toast(err.message || "导入失败", "err");
    }
  };
  reader.onerror = () => toast("读取文件失败", "err");
  reader.readAsText(file);
  e.target.value = "";
}
// 把当前表单抽成「预设能套用的 opts」(供保存)
function snapshotOpts() {
  const f = buildFilters() || {};
  return {
    collector: form.value.collector,
    quality: isGallery.value ? form.value.quality : null,
    media: isGallery.value ? form.value.media : null,
    album_title: isGallery.value ? form.value.albumTitle : null,
    aggregate_depth: form.value.aggregateDepth,
    max_items: form.value.aggregateMax,
    download_dir: form.value.downloadDir,
    // 下载顺序不受采集器类型限制(任何任务都有"先下哪个"的问题), 所以不像
    // quality/media 那样带 isGallery 判断。
    resource_order: form.value.resourceOrder,
    filters: Object.keys(f).length ? f : {},
  };
}

// ---- 过滤条件解析 ----
const UNITS = ["B", "KB", "MB", "GB"];
const resourceTypes = computed(
  () => props.config.resource_types || ["image", "video", "audio", "doc", "text"]
);
const qualities = computed(() => props.config.qualities || ["original", "1200", "800", "600"]);
const medias = computed(() => props.config.medias || ["auto", "image", "video", "both"]);
const albumTitles = computed(
  () => props.config.album_titles || ["clean", "full", "h1", "id"]
);
// 兜底值必须与后端 RESOURCE_ORDERS 一致(连顺序都对着, 否则旧后端 + 新前端时
// 下拉框会少一项, 而"少一项"没人会当成 bug 报)。
const orders = computed(
  () => props.config.resource_orders || ["original", "video_first", "small_first"]
);

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
function dimVal(v) {
  const s = String(v ?? "").trim();
  if (!s || !/^\d+$/.test(s)) return null;
  const n = parseInt(s, 10);
  return n > 0 ? n : null;
}
function buildFilters() {
  const f = {};
  const sel = form.value.selTypes;
  if (sel.length) f.types = sel;
  const e = splitExts(form.value.exts);
  if (e.length) f.exts = e;
  const ee = splitExts(form.value.excludeExts);
  if (ee.length) f.exclude_exts = ee;
  const k = splitWords(form.value.keywords);
  if (k.length) f.keywords = k;
  const ek = splitWords(form.value.excludeKeywords);
  if (ek.length) f.exclude_keywords = ek;
  const mn = sizeVal(form.value.minSize);
  if (mn) f.min_size = mn;
  const mx = sizeVal(form.value.maxSize);
  if (mx) f.max_size = mx;
  const mw = dimVal(form.value.minWidth);
  if (mw) f.min_width = mw;
  const mh = dimVal(form.value.minHeight);
  if (mh) f.min_height = mh;
  if (!form.value.dedupPerceptual) f.dedup_perceptual = false;
  return f;
}
function toggleType(t) {
  const i = form.value.selTypes.indexOf(t);
  if (i >= 0) form.value.selTypes.splice(i, 1);
  else form.value.selTypes.push(t);
}
const activeFilterCount = computed(() => Object.keys(buildFilters()).length);

function aggregateOpts() {
  if (!isAggregate.value) return {};
  return {
    aggregate_depth: Number(form.value.aggregateDepth) || 1,
    max_items: Number(form.value.aggregateMax) || 50,
  };
}

// ---- 批次模式 ----
const batchText = ref("");
const allowDup = ref(false);
const batchResult = ref(null);
const parsed = computed(() => {
  const raw = batchText.value.split(/\r?\n/);
  const seen = new Set();
  return raw.map((line, idx) => {
    const s = line.trim();
    if (!s) return { ln: idx + 1, raw: line, kind: "empty", msg: "空行" };
    if (seen.has(s)) return { ln: idx + 1, raw: line, kind: "reject", msg: "与前面重复" };
    seen.add(s);
    if (/\s/.test(s)) return { ln: idx + 1, raw: line, kind: "reject", msg: "含空格, 一行一个链接" };
    if (s.length > 2000) return { ln: idx + 1, raw: line, kind: "reject", msg: "过长, 不像链接" };
    return { ln: idx + 1, raw: line, kind: "ok", msg: s };
  });
});
const okCount = computed(() => parsed.value.filter((p) => p.kind === "ok").length);
const rejectCount = computed(() => parsed.value.filter((p) => p.kind !== "ok").length);

// 拿到后端返回的逐行结论后, 把每条原因映射成可读文案
function batchMsg(item) {
  if (item.ok) return `已创建 #${item.task_id}`;
  return item.message || item.reason || "未创建";
}
function batchRowClass(item, idx) {
  const p = parsed.value[idx];
  if (!p) return item.ok ? "ok" : "reject";
  return p.kind === "ok" && item.ok ? "ok" : "reject";
}

// ---- 提交流程 ----
async function submitSingle() {
  if (!form.value.url.trim()) return;
  creating.value = true;
  previewError.value = "";
  try {
    const f = buildFilters();
    const res = await createTask(form.value.url.trim(), form.value.collector, {
      download_dir: form.value.downloadDir || null,
      filters: Object.keys(f).length ? f : null,
      quality: isGallery.value ? form.value.quality : null,
      media: isGallery.value ? form.value.media : null,
      album_title: isGallery.value ? form.value.albumTitle : null,
      proxy: form.value.proxy.trim() || null,
      resource_order: form.value.resourceOrder,
      ...aggregateOpts(),
    });
    if (res.warning) toast(res.warning, "warn");
    else toast(`已创建任务 #${res.task_id}`, "ok");
    form.value.url = "";
    resolved.value = null;
    manualWarning.value = "";
    savePrefs();
    emit("created", { ids: [res.task_id] });
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  } finally {
    creating.value = false;
  }
}

async function submitBatch() {
  const lines = parsed.value.filter((p) => p.kind === "ok").map((p) => p.msg);
  if (!lines.length) {
    toast("没有可创建的链接(全是空行或重复)", "warn");
    return;
  }
  creating.value = true;
  batchResult.value = null;
  try {
    const f = buildFilters();
    const res = await batchCreateTasks({
      urls: lines,
      collector: form.value.collector,
      download_dir: form.value.downloadDir || null,
      filters: Object.keys(f).length ? f : null,
      quality: isGallery.value ? form.value.quality : null,
      media: isGallery.value ? form.value.media : null,
      album_title: isGallery.value ? form.value.albumTitle : null,
      proxy: form.value.proxy.trim() || null,
      resource_order: form.value.resourceOrder,
      ...aggregateOpts(),
      allow_duplicates: allowDup.value,
    });
    batchResult.value = res;
    const msg =
      `创建 ${res.created_count} 个, 跳过 ${res.rejected_count} 个` +
      (res.truncated_count ? `, 截断 ${res.truncated_count} 行` : "");
    toast(msg, res.created_count ? "ok" : "warn");
    savePrefs();
    emit("created", {
      ids: res.items.filter((i) => i.ok).map((i) => i.task_id),
    });
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  } finally {
    creating.value = false;
  }
}

async function runPreview() {
  if (!form.value.url.trim()) {
    previewError.value = "请先填写 URL 或图集 ID";
    return;
  }
  previewing.value = true;
  preview.value = null;
  previewError.value = "";
  try {
    preview.value = await previewTask({
      url: form.value.url.trim(),
      collector: form.value.collector,
      quality: isGallery.value ? form.value.quality : null,
      media: isGallery.value ? form.value.media : null,
      album_title: isGallery.value ? form.value.albumTitle : null,
      ...aggregateOpts(),
    });
  } catch (e) {
    previewError.value = e.response?.data?.detail || String(e);
  } finally {
    previewing.value = false;
  }
}
const previewSummary = computed(() => {
  const p = preview.value;
  if (!p) return "";
  const mark = p.sampled ? "≥" : "";
  if (p.kind === "aggregate") {
    const b = [];
    if (p.photos != null) b.push(`${mark}${p.photos} 个相册`);
    if (p.videos != null) b.push(`${mark}${p.videos} 个视频页`);
    return b.join(" + ") || "数量未知";
  }
  const b = [];
  if (p.photos != null) b.push(`${mark}${p.photos} 张图`);
  if (p.videos != null) b.push(`${mark}${p.videos} 段视频`);
  return b.join(" + ") || "数量未知";
});

function onPickDir(path) {
  form.value.downloadDir = path;
  showPicker.value = false;
  savePrefs();
}

// ---- 偏好持久化 ----
const LS_KEY = "uwc.create.prefs";
function savePrefs() {
  try {
    localStorage.setItem(
      LS_KEY,
      JSON.stringify({
        collector: form.value.collector,
        downloadDir: form.value.downloadDir,
        quality: form.value.quality,
        media: form.value.media,
        albumTitle: form.value.albumTitle,
        resourceOrder: form.value.resourceOrder,
        selTypes: form.value.selTypes,
        exts: form.value.exts,
        excludeExts: form.value.excludeExts,
        keywords: form.value.keywords,
        excludeKeywords: form.value.excludeKeywords,
        minSize: form.value.minSize,
        maxSize: form.value.maxSize,
        minWidth: form.value.minWidth,
        minHeight: form.value.minHeight,
        dedupPerceptual: form.value.dedupPerceptual,
        aggregateDepth: form.value.aggregateDepth,
        aggregateMax: form.value.aggregateMax,
        proxy: form.value.proxy,
      })
    );
  } catch (e) {
    /* ignore */
  }
}
function loadPrefs() {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return;
    const p = JSON.parse(raw);
    if (p.collector) form.value.collector = p.collector;
    if (p.downloadDir) form.value.downloadDir = p.downloadDir;
    if (p.quality && qualities.value.includes(p.quality)) form.value.quality = p.quality;
    if (p.media && medias.value.includes(p.media)) form.value.media = p.media;
    if (p.albumTitle && albumTitles.value.includes(p.albumTitle))
      form.value.albumTitle = p.albumTitle;
    // 与上面三条同一个道理: 只在**当前后端认识的取值**里还原。存过的偏好可能来自
    // 更老的/更新的版本, 直接套用会把下拉框设成一个不存在的选项(显示成空白),
    // 而且提交时会因为 400 被拒 —— 那看起来像"软件坏了"。
    if (p.resourceOrder && orders.value.includes(p.resourceOrder))
      form.value.resourceOrder = p.resourceOrder;
    if (Array.isArray(p.selTypes)) form.value.selTypes = p.selTypes;
    form.value.exts = p.exts || "";
    form.value.excludeExts = p.excludeExts || "";
    form.value.keywords = p.keywords || "";
    form.value.excludeKeywords = p.excludeKeywords || "";
    if (p.minSize) form.value.minSize = { ...form.value.minSize, ...p.minSize };
    if (p.maxSize) form.value.maxSize = { ...form.value.maxSize, ...p.maxSize };
    form.value.minWidth = p.minWidth || "";
    form.value.minHeight = p.minHeight || "";
    if (p.dedupPerceptual !== undefined)
      form.value.dedupPerceptual = !!p.dedupPerceptual;
    if (p.aggregateDepth) form.value.aggregateDepth = p.aggregateDepth;
    if (p.aggregateMax) form.value.aggregateMax = p.aggregateMax;
    if (p.proxy) form.value.proxy = p.proxy;
  } catch (e) {
    /* ignore */
  }
}

// ---- 批量模式: 拖拽 .txt 导入 URL ----
function onBatchDrop(e) {
  const file = e.dataTransfer?.files?.[0];
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".txt") && file.type !== "text/plain") {
    toast("请拖入 .txt 文本文件(每行一个链接)", "warn");
    return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    const text = String(reader.result || "");
    const merged = (batchText.value ? batchText.value.replace(/\s*$/, "") + "\n" : "") + text;
    batchText.value = merged.replace(/\r\n/g, "\n");
    toast(`已从 ${file.name} 导入链接`, "ok");
  };
  reader.onerror = () => toast("读取文件失败", "err");
  reader.readAsText(file);
}
function onBatchDragOver(e) {
  e.preventDefault();
}

onMounted(() => {
  loadPrefs();
  // 配置里的默认值回填(仅当本地没存过时)
  if (props.config.download_dir && !form.value.downloadDir)
    form.value.downloadDir = props.config.download_dir;
});
</script>

<template>
  <div class="card">
    <!-- 模式切换 + 预设 -->
    <div class="create-top">
      <div class="seg">
        <button :class="{ on: mode === 'single' }" @click="mode = 'single'">单条</button>
        <button :class="{ on: mode === 'batch' }" @click="mode = 'batch'">批量粘贴</button>
      </div>
      <div class="preset-row">
        <span
          v-for="p in allPresets"
          :key="p.name"
          class="preset-chip"
          :class="{ on: activePreset === p.name }"
          @click="applyPresetByName(p.name)"
        >
          <span>{{ p.icon }} {{ p.name }}</span>
          <span
            v-if="!BUILTIN_PRESETS.includes(p)"
            class="x"
            title="删除该预设"
            @click.stop="removePreset(p.name)"
            >✕</span
          >
        </span>
        <button type="button" class="ghost mini" @click="saveCurrentPreset">存为预设</button>
        <label class="ghost mini import-btn">
          导入预设
          <input type="file" accept="application/json,.json" hidden @change="onImportPresets" />
        </label>
        <button type="button" class="ghost mini" @click="exportPresets">导出预设</button>
      </div>
    </div>

    <!-- 单条模式: 输入网址 → 预览 → 开始 -->
    <form v-if="mode === 'single'" class="create-form" @submit.prevent="submitSingle">
      <input v-model="form.url" type="text" :placeholder="urlPlaceholder" />
      <select v-model="form.collector">
        <option v-for="c in collectors" :key="c" :value="c">
          {{ collectorLabel[c] || c }}
        </option>
      </select>
      <button type="submit" :disabled="creating || !form.url.trim()">
        {{ creating ? "创建中..." : "开始" }}
      </button>
      <button
        type="button"
        class="ghost"
        v-if="isGalleryLike"
        :disabled="previewing || !form.url.trim()"
        @click="runPreview"
      >
        {{ previewing ? "读取中..." : "预览" }}
      </button>
    </form>

    <!-- 批量模式: 粘贴多行, 实时标注重复/无效 -->
    <div
      v-else
      class="batch-wrap"
      @dragover="onBatchDragOver"
      @drop.prevent="onBatchDrop"
    >
      <textarea
        v-model="batchText"
        class="batch-input"
        placeholder="每行一个链接或图集 ID, 支持空行与重复检测; 也可把存了链接的 .txt 拖进来"
        rows="6"
      ></textarea>
      <div class="batch-foot">
        <span class="summary">可创建 <b>{{ okCount }}</b> 个 · 跳过 <b>{{ rejectCount }}</b> 个</span>
        <label class="allow-dup">
          <input type="checkbox" v-model="allowDup" /> 允许与已有任务重复
        </label>
        <span class="grow"></span>
        <button
          type="button"
          class="ghost"
          v-if="isGalleryLike"
          :disabled="previewing || !okCount"
          @click="runPreview"
        >
          {{ previewing ? "读取中..." : "预览首条" }}
        </button>
        <button type="button" :disabled="creating || !okCount" @click="submitBatch">
          {{ creating ? "创建中..." : `创建 ${okCount} 个` }}
        </button>
      </div>
      <div v-if="batchText.trim()" class="batch-box">
        <div v-for="(p, i) in parsed" :key="i" class="batch-row" :class="p.kind === 'ok' ? 'ok' : 'reject'">
          <span class="ln">{{ p.ln }}</span>
          <span class="msg" :class="{ bad: p.kind !== 'ok' }">
            <template v-if="batchResult">{{ batchRowClass(batchResult.items[i], i) === 'ok' ? '✓ ' : '✕ ' }}</template>
            <template v-else>{{ p.kind === 'ok' ? '✓ ' : '✕ ' }}</template>
            {{ batchResult ? batchMsg(batchResult.items[i]) : p.msg }}
          </span>
        </div>
      </div>
    </div>

    <!-- 自动识别结论 -->
    <div v-if="resolveHint" class="dir-row resolve-row" :class="resolveHint.kind">
      <span class="lbl">识别</span>
      <span :title="resolveHint.reason || ''">{{ resolveHint.text }}</span>
    </div>
    <div v-if="manualWarning" class="dir-row resolve-row warn">
      <span class="lbl">提示</span><span>{{ manualWarning }}</span>
    </div>

    <!-- 单条预览结果 -->
    <div
      v-if="mode === 'single' && (previewing || preview || previewError)"
      class="preview-box"
    >
      <div v-if="previewing">正在读取…</div>
      <div v-else-if="previewError" class="preview-err">预览失败: {{ previewError }}</div>
      <template v-else-if="preview">
        <div><b>将保存到</b> <code>{{ isVideo ? "下载目录根目录" : preview.group }}</code></div>
        <div><b>本次将采集</b> {{ previewSummary }}</div>
      </template>
    </div>

    <!-- 高级设置折叠 -->
    <div class="adv-head">
      <button type="button" class="ghost" @click="showAdvanced = !showAdvanced">
        {{ showAdvanced ? "▾" : "▸" }} 高级设置
      </button>
      <span class="summary" v-if="activeFilterCount">已启用 {{ activeFilterCount }} 项过滤</span>
      <span class="summary muted" v-else>未启用过滤, 下载全部发现的资源</span>
    </div>

    <div class="adv-body" v-if="showAdvanced">
      <div class="dir-row">
        <span class="lbl">下载目录</span>
        <input
          type="text"
          :value="form.downloadDir || config.download_dir || ''"
          :placeholder="config.download_dir || '未连接后端'"
          readonly
          :title="form.downloadDir || config.download_dir"
        />
        <button type="button" class="ghost" @click="showPicker = true">浏览...</button>
        <button
          type="button"
          class="ghost"
          v-if="form.downloadDir"
          @click="form.downloadDir = ''; savePrefs()"
        >恢复默认</button>
      </div>

      <div class="dir-row" v-if="isGallery">
        <span class="lbl">画质档</span>
        <select v-model="form.quality" @change="savePrefs">
          <option v-for="q in qualities" :key="q" :value="q">{{ qualityLabel[q] || q }}</option>
        </select>
        <span class="tip">未选中的档位自动作为备用下载点; 视频只有一档</span>
      </div>
      <div class="dir-row" v-if="isGallery">
        <span class="lbl">采集媒体</span>
        <select v-model="form.media" @change="savePrefs">
          <option v-for="m in medias" :key="m" :value="m">{{ mediaLabel[m] || m }}</option>
        </select>
        <span class="tip">有的相册同时含图片与视频; 自动 = 读相册页判断</span>
      </div>
      <div class="dir-row" v-if="isGallery">
        <span class="lbl">目录命名</span>
        <select v-model="form.albumTitle" @change="savePrefs">
          <option v-for="a in albumTitles" :key="a" :value="a">{{ albumTitleLabel[a] || a }}</option>
        </select>
        <span class="tip">相册名取自相册页 &lt;title&gt;; 取不到时回退图集 ID</span>
      </div>

      <!-- 下载顺序对所有采集器都成立(任何任务都有"先下哪个"的问题), 所以不像
           上面三行那样带 isGallery 判断。它只决定**开始顺序**: 线程池空闲 worker
           按提交序取任务, 排在前面只是先开跑, 不改变任何资源的下载结论。 -->
      <div class="dir-row">
        <span class="lbl">下载顺序</span>
        <select v-model="form.resourceOrder" @change="savePrefs">
          <option v-for="o in orders" :key="o" :value="o">{{ orderLabel[o] || o }}</option>
        </select>
        <span class="tip">决定资源开始下载的先后(不改下不下); 视频通常最慢, 先下它把长尾提前</span>
      </div>

      <div class="dir-row" v-if="isAggregate">
        <span class="lbl">展开范围</span>
        <label class="agg">
          层数
          <input class="num" type="number" min="1" max="3" v-model.number="form.aggregateDepth" />
          条目上限
          <input class="num" type="number" min="1" max="500" v-model.number="form.aggregateMax" />
        </label>
        <span class="tip">从入口页往下钻几层找相册; 到顶时预告里的数量只会是下限</span>
      </div>

      <div class="dir-row">
        <span class="lbl">目录结构</span>
        <span class="tip">下载目录 / 相册名 / 图片；视频直接放在下载目录根下，文件名取站点原名</span>
      </div>

      <div class="dir-row">
        <span class="lbl">代理</span>
        <input
          type="text"
          v-model="form.proxy"
          class="proxy-input"
          placeholder="可选, 如 http://127.0.0.1:7890 ; 留空用全局 UWC_PROXY"
          style="flex:1"
        />
        <span class="tip">仅对本任务生效, 优先于全局代理</span>
      </div>

      <!-- 过滤条件 -->
      <div class="frow">
        <label>资源类型</label>
        <div class="chips">
          <button
            type="button"
            v-for="t in resourceTypes"
            :key="t"
            class="chip"
            :class="{ on: form.selTypes.includes(t) }"
            @click="toggleType(t)"
          >{{ t }}</button>
        </div>
        <span class="tip">不选 = 全部类型</span>
      </div>
      <div class="frow">
        <label>仅要后缀</label>
        <input v-model="form.exts" type="text" placeholder="jpg,png,webp,mp4" />
        <span class="tip">留空不限</span>
      </div>
      <div class="frow">
        <label>排除后缀</label>
        <input v-model="form.excludeExts" type="text" placeholder="gif,svg" />
        <span class="tip">留空不限</span>
      </div>
      <div class="frow">
        <label>URL 需含</label>
        <input v-model="form.keywords" type="text" placeholder="original,full" />
        <span class="tip">满足其一即保留</span>
      </div>
      <div class="frow">
        <label>URL 排除</label>
        <input v-model="form.excludeKeywords" type="text" placeholder="thumb,icon,logo" />
        <span class="tip">过滤缩略图/图标很有效</span>
      </div>
      <div class="frow">
        <label>文件大小</label>
        <input v-model="form.minSize.num" class="num" type="text" placeholder="最小" />
        <select v-model="form.minSize.unit">
          <option v-for="u in UNITS" :key="u" :value="u">{{ u }}</option>
        </select>
        <span class="sep">~</span>
        <input v-model="form.maxSize.num" class="num" type="text" placeholder="最大" />
        <select v-model="form.maxSize.unit">
          <option v-for="u in UNITS" :key="u" :value="u">{{ u }}</option>
        </select>
        <span class="tip">需 HEAD 探测, 服务器不支持时自动放行</span>
      </div>
      <div class="frow">
        <label>图片尺寸</label>
        <input v-model="form.minWidth" class="num" type="text" placeholder="最小宽" />
        <span class="sep">×</span>
        <input v-model="form.minHeight" class="num" type="text" placeholder="最小高" />
        <span class="tip">像素, 留空不限</span>
      </div>
      <div class="frow">
        <label>重复检测</label>
        <label class="chk"><input v-model="form.dedupPerceptual" type="checkbox" @change="savePrefs" /> <span>感知去重</span></label>
        <span class="tip">默认开。标出换尺寸/重压缩仍相同的图, <b>只标记不删除</b></span>
      </div>
    </div>

    <FolderPicker
      :show="showPicker"
      :initial="form.downloadDir || config.download_dir"
      @select="onPickDir"
      @close="showPicker = false"
    />
  </div>
</template>

<style scoped>
.create-top { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
.create-top .grow { flex: 1; }
.batch-wrap { display: grid; gap: 10px; }
.batch-input {
  width: 100%; background: var(--panel-2); border: 1px solid var(--border);
  border-radius: 8px; color: var(--text); padding: 10px 12px; font-size: 13px;
  font-family: ui-monospace, Consolas, monospace; resize: vertical; outline: none;
}
.batch-input:focus { border-color: var(--accent); }
.batch-foot { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.batch-foot .summary { font-size: 12px; color: var(--muted); }
.batch-foot .summary b { color: var(--text); }
.allow-dup { font-size: 12px; color: var(--muted); display: inline-flex; align-items: center; gap: 4px; }
.allow-dup input { accent-color: var(--accent); }
.preview-box {
  display: flex; flex-direction: column; gap: 5px; font-size: 13px; line-height: 1.6;
  padding: 10px 12px; margin-top: 10px; border-radius: 8px; border: 1px solid var(--border);
}
.preview-box code { color: var(--text); font-family: ui-monospace, Consolas, monospace; }
.preview-err { color: var(--err); }
.resolve-row { font-size: 12px; color: var(--muted); margin-top: 10px; }
.resolve-row.ok span:last-child { color: var(--ok); }
.resolve-row.warn span:last-child { color: var(--warn); }
.resolve-row.bad span:last-child { color: var(--err); }
.agg { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; }
</style>
