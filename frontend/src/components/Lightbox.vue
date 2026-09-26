<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from "vue";

// 灯箱: 点击缩略图放大查看, 支持左右切换、缩放、旋转、原图下载、幻灯片播放与 Esc 关闭。
// 只接收「已经能直接打开的图」(file_url 非空), 视频/文档不在灯箱里展示。
const props = defineProps({
  images: { type: Array, default: () => [] }, // [{ url, name }]
  index: { type: Number, default: 0 },
});
const emit = defineEmits(["close", "update:index"]);

const current = computed(() => props.images[props.index] || null);
const zoom = ref(1);
const rotate = ref(0);

// ---- 幻灯片自动播放 ----
// ⚠️ 默认**不播**: 自动翻页是"程序在替你翻", 没点开就自己跑会让人以为界面坏了。
// ⚠️ 到点调用的就是手动翻页那个 go(1) —— 不另写一套推进逻辑, 否则两条路的边界会分叉。
const SLIDE_STEPS = [3, 5, 10, 20]; // 秒
const playing = ref(false);
const slideSeconds = ref(5);
let slideTimer = null;

// 只有 1 张时播放键是禁用的(go() 在 length<=1 时直接 return, 播了也看不出动静)。
const canSlide = computed(() => props.images.length > 1);

function stopSlide() {
  if (slideTimer !== null) {
    clearInterval(slideTimer);
    slideTimer = null;
  }
}
function startSlide() {
  stopSlide();
  // 间隔至少 1 秒: 0 或负数会让 setInterval 变成"每帧一次", 界面直接卡死。
  slideTimer = setInterval(() => go(1), Math.max(1, slideSeconds.value) * 1000);
}
function togglePlay() {
  if (!canSlide.value) return;
  playing.value = !playing.value;
  if (playing.value) startSlide();
  else stopSlide();
}
function setSlideSeconds(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return;
  slideSeconds.value = n;
  if (playing.value) startSlide(); // 改间隔要立刻生效, 否则"选了 3s 还是 5s"
}
// 换图时重置计时: 手动翻了一张后不该立刻又被定时器翻走。
watch(
  () => props.index,
  () => {
    if (playing.value) startSlide();
  }
);
// 图变少了(比如列表被刷新)就停 —— 留着定时器在 1 张图里空转是"看不见的活跃"。
watch(canSlide, (ok) => {
  if (!ok && playing.value) {
    playing.value = false;
    stopSlide();
  }
});

// 换图时复位缩放/旋转, 否则上一张的 3 倍放大状态会带到下一张, 看着像图坏了。
watch(
  () => props.index,
  () => {
    zoom.value = 1;
    rotate.value = 0;
  }
);

const imgStyle = computed(() => ({
  transform: `scale(${zoom.value}) rotate(${rotate.value}deg)`,
  cursor: zoom.value > 1 ? "zoom-out" : "zoom-in",
}));

function go(delta) {
  if (props.images.length <= 1) return;
  let n = props.index + delta;
  if (n < 0) n = props.images.length - 1;
  if (n >= props.images.length) n = 0;
  emit("update:index", n);
}

function zoomBy(delta) {
  zoom.value = Math.min(6, Math.max(0.25, +(zoom.value + delta).toFixed(2)));
}
function toggleZoom() {
  zoom.value = zoom.value > 1 ? 1 : 2;
}
function rot(delta) {
  rotate.value = (rotate.value + delta + 360) % 360;
}
function reset() {
  zoom.value = 1;
  rotate.value = 0;
}
function download() {
  if (!current.value) return;
  const a = document.createElement("a");
  a.href = current.value.url;
  a.download = current.value.name || "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function onKey(e) {
  if (e.key === "Escape") emit("close");
  else if (e.key === "ArrowLeft") go(-1);
  else if (e.key === "ArrowRight") go(1);
  else if (e.key === "+" || e.key === "=") zoomBy(0.25);
  else if (e.key === "-") zoomBy(-0.25);
  else if (e.key === "0") reset();
  else if (e.key === " ") {
    // 空格默认会滚动页面 / 触发聚焦按钮, 必须拦掉再播。
    e.preventDefault();
    togglePlay();
  } else if (e.key.toLowerCase() === "p") togglePlay();
  else if (e.key.toLowerCase() === "r") rot(90);
}

onMounted(() => window.addEventListener("keydown", onKey));
// ⚠️ 定时器必须在这里清: 组件销毁后 setInterval 仍会 emit("update:index"),
// 那是一个"看不见的东西在翻页" —— 关掉灯箱后列表自己在动。
onUnmounted(() => {
  stopSlide();
  window.removeEventListener("keydown", onKey);
});
</script>

<template>
  <div class="lightbox-mask" @click.self="emit('close')">
    <div class="lb-tools" @click.stop>
      <button class="lb-btn" title="缩小 (-)" @click="zoomBy(-0.25)">−</button>
      <span class="lb-zoom">{{ Math.round(zoom * 100) }}%</span>
      <button class="lb-btn" title="放大 (+)" @click="zoomBy(0.25)">＋</button>
      <button class="lb-btn" title="旋转 (R)" @click="rot(90)">⟳</button>
      <button class="lb-btn" title="复位 (0)" @click="reset">↺</button>
      <button class="lb-btn" title="下载原图" @click="download">⤓</button>
      <span class="lb-sep"></span>
      <button
        class="lb-btn"
        :class="{ 'lb-on': playing }"
        :disabled="!canSlide"
        :title="
          canSlide
            ? playing
              ? '停止播放 (空格)'
              : '自动播放 (空格)'
            : '至少 2 张才能自动播放'
        "
        @click="togglePlay"
      >
        {{ playing ? "⏸" : "▶" }}
      </button>
      <select
        class="lb-select"
        :disabled="!canSlide"
        :value="slideSeconds"
        title="每张停留时长"
        @change="setSlideSeconds($event.target.value)"
      >
        <option v-for="s in SLIDE_STEPS" :key="s" :value="s">{{ s }} 秒</option>
      </select>
    </div>
    <span class="lb-close" @click="emit('close')">✕</span>
    <span v-if="images.length > 1" class="nav prev" @click="go(-1)">‹</span>
    <img
      v-if="current"
      :src="current.url"
      :alt="current.name"
      :style="imgStyle"
      @click="toggleZoom"
    />
    <span v-if="images.length > 1" class="nav next" @click="go(1)">›</span>
    <span v-if="images.length > 1" class="lb-count">
      {{ index + 1 }} / {{ images.length }}
      <template v-if="current && current.name"> · {{ current.name }}</template>
      <!-- 播放中必须看得见: 否则"点了播放没反应"和"正在播"长得一样 -->
      <b v-if="playing" class="lb-live">播放中 · 每 {{ slideSeconds }}s</b>
    </span>
  </div>
</template>
