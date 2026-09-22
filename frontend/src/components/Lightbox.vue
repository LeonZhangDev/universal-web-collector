<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from "vue";

// 灯箱: 点击缩略图放大查看, 支持左右切换、缩放、旋转、原图下载与 Esc 关闭。
// 只接收「已经能直接打开的图」(file_url 非空), 视频/文档不在灯箱里展示。
const props = defineProps({
  images: { type: Array, default: () => [] }, // [{ url, name }]
  index: { type: Number, default: 0 },
});
const emit = defineEmits(["close", "update:index"]);

const current = computed(() => props.images[props.index] || null);
const zoom = ref(1);
const rotate = ref(0);

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
  else if (e.key.toLowerCase() === "r") rot(90);
}

onMounted(() => window.addEventListener("keydown", onKey));
onUnmounted(() => window.removeEventListener("keydown", onKey));
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
    </span>
  </div>
</template>
