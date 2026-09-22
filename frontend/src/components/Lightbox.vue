<script setup>
import { computed, onMounted, onUnmounted } from "vue";

// 灯箱: 点击缩略图放大查看, 支持左右切换与 Esc 关闭。
// 只接收「已经能直接打开的图」(file_url 非空), 视频/文档不在灯箱里展示。
const props = defineProps({
  images: { type: Array, default: () => [] }, // [{ url, name }]
  index: { type: Number, default: 0 },
});
const emit = defineEmits(["close", "update:index"]);

const current = computed(() => props.images[props.index] || null);

function go(delta) {
  if (props.images.length <= 1) return;
  let n = props.index + delta;
  if (n < 0) n = props.images.length - 1;
  if (n >= props.images.length) n = 0;
  emit("update:index", n);
}

function onKey(e) {
  if (e.key === "Escape") emit("close");
  else if (e.key === "ArrowLeft") go(-1);
  else if (e.key === "ArrowRight") go(1);
}

onMounted(() => window.addEventListener("keydown", onKey));
onUnmounted(() => window.removeEventListener("keydown", onKey));
</script>

<template>
  <div class="lightbox-mask" @click.self="emit('close')">
    <span class="lb-close" @click="emit('close')">✕</span>
    <span v-if="images.length > 1" class="nav prev" @click="go(-1)">‹</span>
    <img v-if="current" :src="current.url" :alt="current.name" />
    <span v-if="images.length > 1" class="nav next" @click="go(1)">›</span>
    <span v-if="images.length > 1" class="lb-count">
      {{ index + 1 }} / {{ images.length }}
      <template v-if="current && current.name"> · {{ current.name }}</template>
    </span>
  </div>
</template>
