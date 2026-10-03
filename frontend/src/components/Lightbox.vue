<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { setWatchPosition } from "../api";

// 灯箱: 点击缩略图放大查看, 支持左右切换、缩放、旋转、原图下载、幻灯片播放与 Esc 关闭。
// 既收图片也收视频: 每个条目可带 `type` 字段, `type==="video"` 时渲染 <video> 直读本地文件。
const props = defineProps({
  images: { type: Array, default: () => [] }, // [{ url, name, type?, id?, duration?, size?, watch_position? }]
  index: { type: Number, default: 0 },
});
const emit = defineEmits(["close", "update:index"]);

const current = computed(() => props.images[props.index] || null);
// 视频条目用 <video> 而不是 <img>。type 缺省按图片处理(老调用方不传也没关系)。
const isVideo = computed(() => current.value && current.value.type === "video");
const zoom = ref(1);
const rotate = ref(0);
const videoEl = ref(null);

// ---- 视频控制(只在 isVideo 时启用) ----
// ⚠️ 浏览器的自动播放策略: 带声音的 autoplay 一律被拦。所以默认**静音自动播放**,
// 用户点一下"开声"再解锁声音 —— 这样"下载完的视频打开就播"在绝大多数浏览器下真的生效,
// 而不是静默不播(那是个真 bug, 不是体验问题)。
const muted = ref(true);
const SPEEDS = [0.5, 1, 1.25, 1.5, 2];
const speed = ref(1);
function toggleMute() {
  muted.value = !muted.value;
  if (videoEl.value) videoEl.value.muted = muted.value;
}
function setSpeed(v) {
  speed.value = v;
  if (videoEl.value) videoEl.value.playbackRate = v;
}
async function togglePiP() {
  try {
    if (document.pictureInPictureElement) {
      await document.exitPictureInPicture();
    } else if (videoEl.value && videoEl.value.requestPictureInPicture) {
      await videoEl.value.requestPictureInPicture();
    }
  } catch (e) {
    /* PiP 被策略/环境拦下只该"没反应", 不该报错 */
  }
}
function seek(delta) {
  if (videoEl.value) videoEl.value.currentTime = Math.max(0, videoEl.value.currentTime + delta);
}
function toggleVideoPlay() {
  const v = videoEl.value;
  if (!v) return;
  if (v.paused) v.play().catch(() => {});
  else v.pause();
}

// 续播: 打开视频时从记录的 watch_position 接着看。⚠️ watch_position 是 REAL 秒,
// 0 也是有效值(刚打开), 所以只有"非 0 且是个有限数"才跳; 0 就从开头。
function onVideoMeta() {
  const v = videoEl.value;
  if (!v) return;
  const wp = Number(current.value && current.value.watch_position);
  if (Number.isFinite(wp) && wp > 0.5) {
    try { v.currentTime = wp; } catch (e) { /* 忽略越界 */ }
  }
  // 自动播放策略要求先静音, 这里再 play 才稳; 带声 autoplay 会被静默拦掉。
  v.muted = muted.value;
  v.playbackRate = speed.value;
  v.play().catch(() => {});
}
// 节流上报观看进度(续播用)。⚠️ 失败只静默: 它是附件能力, 不该因上报失败把灯箱弄崩。
let lastReport = 0;
function onTimeUpdate() {
  const v = videoEl.value;
  if (!v || !current.value || !current.value.id) return;
  const now = Date.now();
  if (now - lastReport < 3000) return;
  lastReport = now;
  setWatchPosition(current.value.id, v.currentTime);
}

function fmtDur(d) {
  const t = Math.round(Number(d) || 0);
  if (t <= 0) return "";
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = String(t % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}
function fmtSize(n) {
  const v = Number(n) || 0;
  if (v <= 0) return "";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let x = v, i = 0;
  while (x >= 1024 && i < u.length - 1) { x /= 1024; i++; }
  return `${x >= 100 || i === 0 ? Math.round(x) : x.toFixed(1)} ${u[i]}`;
}

// ---- 幻灯片自动播放 ----
// ⚠️ 默认**不播**: 自动翻页是"程序在替你翻", 没点开就自己跑会让人以为界面坏了。
// ⚠️ 到点调用的就是手动翻页那个 go(1) —— 不另写一套推进逻辑, 否则两条路的边界会分叉。
const SLIDE_STEPS = [3, 5, 10, 20]; // 秒
const playing = ref(false);
const slideSeconds = ref(5);
let slideTimer = null;
// 播放顺序: 默认按传入顺序; 打开"随机"后按打乱的排列走(只影响自动播放的走向,
// 不影响左右方向键的"上一张/下一张"语义——方向键始终在当前序列里相邻移动)。
const shuffle = ref(false);
const order = ref([]); // 当前播放序列(images 的下标排列)
const orderPos = ref(0); // 在 order 中的位置

function buildOrder() {
  const n = props.images.length;
  const base = Array.from({ length: n }, (_, i) => i);
  if (shuffle.value && n > 1) {
    for (let i = n - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [base[i], base[j]] = [base[j], base[i]];
    }
  }
  order.value = base;
  // 当前张在序列里的位置(随机后也要从"正在看的这张"开始播)
  const at = base.indexOf(props.index);
  orderPos.value = at < 0 ? 0 : at;
}

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
  if (playing.value) { buildOrder(); startSlide(); }
  else stopSlide();
}
function setSlideSeconds(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return;
  slideSeconds.value = n;
  if (playing.value) startSlide(); // 改间隔要立刻生效, 否则"选了 3s 还是 5s"
}
function toggleShuffle() {
  shuffle.value = !shuffle.value;
  if (playing.value) { buildOrder(); }
}

function go(delta) {
  if (props.images.length <= 1) return;
  let pos = orderPos.value + delta;
  if (pos < 0) pos = order.value.length - 1;
  if (pos >= order.value.length) pos = 0;
  orderPos.value = pos;
  const target = order.value[pos];
  if (target !== undefined) emit("update:index", target);
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
  // 视频正在看时: 方向键做快退/快进, 空格做播放/暂停, 而不是翻页/切换。
  if (isVideo.value && videoEl.value && !videoEl.value.paused || (isVideo.value && videoEl.value && e.key === " ")) {
    if (e.key === "ArrowLeft") { e.preventDefault(); seek(-5); return; }
    if (e.key === "ArrowRight") { e.preventDefault(); seek(5); return; }
    if (e.key === " ") { e.preventDefault(); toggleVideoPlay(); return; }
    if (e.key.toLowerCase() === "m") { toggleMute(); return; }
    if (e.key.toLowerCase() === "f") { e.preventDefault(); togglePiP(); return; }
  }
  if (e.key === "Escape") emit("close");
  else if (!isVideo.value && e.key === "ArrowLeft") go(-1);
  else if (!isVideo.value && e.key === "ArrowRight") go(1);
  else if (e.key === "+" || e.key === "=") zoomBy(0.25);
  else if (e.key === "-" ) zoomBy(-0.25);
  else if (e.key === "0") reset();
  else if (!isVideo.value && e.key === " ") { e.preventDefault(); togglePlay(); }
  else if (!isVideo.value && e.key.toLowerCase() === "p") togglePlay();
  else if (!isVideo.value && e.key.toLowerCase() === "r") rot(90);
}

onMounted(() => {
  window.addEventListener("keydown", onKey);
  buildOrder();
});
// ⚠️ 定时器必须在这里清: 组件销毁后 setInterval 仍会 emit("update:index"),
// 那是一个"看不见的东西在翻页" —— 关掉灯箱后列表自己在动。
onUnmounted(() => {
  stopSlide();
  window.removeEventListener("keydown", onKey);
});

// 换图时重置计时: 手动翻了一张后不该立刻又被定时器翻走。
// ⚠️ 视频条目停在灯箱里时**暂停**幻灯片推进: 否则定时器会在视频播到一半时把它翻走,
// 用户看着像"视频自己跳过了"。(img 条目则照常推进。)
watch(
  () => props.index,
  () => {
    if (isVideo.value) stopSlide();
    else if (playing.value) startSlide();
    // 同步序列位置 + 复位缩放/旋转
    const at = order.value.indexOf(props.index);
    if (at >= 0) orderPos.value = at;
    zoom.value = 1;
    rotate.value = 0;
    lastReport = 0;
  }
);
// 图变少了(比如列表被刷新)就停 —— 留着定时器在 1 张图里空转是"看不见的活跃"。
watch(canSlide, (ok) => {
  if (!ok && playing.value) {
    playing.value = false;
    stopSlide();
  }
});
</script>

<template>
  <div
    class="lightbox-mask"
    @click.self="emit('close')"
    @mouseenter="stopSlide"
    @mouseleave="playing ? startSlide() : null"
  >
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
      <button
        class="lb-btn"
        :class="{ 'lb-on': shuffle }"
        :disabled="!canSlide"
        title="随机播放顺序"
        @click="toggleShuffle"
      >🔀</button>
      <select
        class="lb-select"
        :disabled="!canSlide"
        :value="slideSeconds"
        title="每张停留时长"
        @change="setSlideSeconds($event.target.value)"
      >
        <option v-for="s in SLIDE_STEPS" :key="s" :value="s">{{ s }} 秒</option>
      </select>
      <span class="lb-sep"></span>
      <!-- 视频专属: 开声 / 画中画 / 倍速 -->
      <template v-if="isVideo">
        <button
          class="lb-btn"
          :class="{ 'lb-on': !muted }"
          :title="muted ? '取消静音(自动播放默认静音)' : '静音'"
          @click="toggleMute"
        >{{ muted ? "🔇" : "🔊" }}</button>
        <button class="lb-btn" title="画中画" @click="togglePiP">⧉</button>
        <select
          class="lb-select"
          :value="speed"
          title="播放速度"
          @change="setSpeed(Number($event.target.value))"
        >
          <option v-for="s in SPEEDS" :key="s" :value="s">{{ s }}x</option>
        </select>
      </template>
    </div>
    <span class="lb-close" @click="emit('close')">✕</span>
    <span v-if="images.length > 1" class="nav prev" @click="go(-1)">‹</span>
    <img
      v-if="current && !isVideo"
      :src="current.url"
      :alt="current.name"
      :style="imgStyle"
      @click="toggleZoom"
    />
    <video
      v-else-if="current && isVideo"
      ref="videoEl"
      :src="current.url"
      :style="imgStyle"
      :muted="muted"
      controls
      autoplay
      playsinline
      @loadedmetadata="onVideoMeta"
      @timeupdate="onTimeUpdate"
    ></video>
    <span v-if="images.length > 1" class="nav next" @click="go(1)">›</span>
    <span v-if="images.length > 1" class="lb-count">
      {{ index + 1 }} / {{ images.length }}
      <template v-if="current && current.name"> · {{ current.name }}</template>
      <!-- 播放中必须看得见: 否则"点了播放没反应"和"正在播"长得一样 -->
      <b v-if="playing" class="lb-live">播放中 · 每 {{ slideSeconds }}s{{ shuffle ? ' · 随机' : '' }}</b>
    </span>
    <!-- 视频元信息条: 时长 + 文件大小(数据来自后端落库的 duration/size) -->
    <span v-if="isVideo && (fmtDur(current?.duration) || fmtSize(current?.size))" class="lb-vmeta">
      <template v-if="fmtDur(current?.duration)">⏱ {{ fmtDur(current?.duration) }}</template>
      <template v-if="fmtSize(current?.size)"> · 💾 {{ fmtSize(current?.size) }}</template>
    </span>
  </div>
</template>

<style scoped>
.lightbox-mask {
  position: fixed; inset: 0; background: rgba(0, 0, 0, 0.92);
  display: flex; align-items: center; justify-content: center; z-index: 1000;
  animation: lb-fade 0.18s ease;
}
@keyframes lb-fade { from { opacity: 0; } to { opacity: 1; } }
.lb-tools {
  position: absolute; top: 12px; left: 50%; transform: translateX(-50%);
  display: flex; align-items: center; gap: 6px; z-index: 10;
  background: rgba(20, 20, 24, 0.78); padding: 6px 10px; border-radius: 10px;
  backdrop-filter: blur(6px);
}
.lb-btn {
  background: var(--panel-2); color: var(--text); border: 1px solid var(--border);
  border-radius: 7px; min-width: 30px; height: 30px; padding: 0 8px; font-size: 14px;
  cursor: pointer; transition: background .15s, border-color .15s;
}
.lb-btn:hover { border-color: var(--accent); }
.lb-btn.lb-on { color: var(--accent); border-color: var(--accent); background: color-mix(in srgb, var(--accent) 14%, transparent); }
.lb-btn:disabled { opacity: .4; cursor: not-allowed; }
.lb-zoom { color: var(--muted); font-size: 12px; min-width: 38px; text-align: center; }
.lb-sep { width: 1px; height: 20px; background: var(--border); margin: 0 2px; }
.lb-select {
  background: var(--panel-2); color: var(--text); border: 1px solid var(--border);
  border-radius: 7px; height: 30px; padding: 0 4px; font-size: 12px;
}
.lb-select:disabled { opacity: .4; }
.lb-close {
  position: absolute; top: 14px; right: 18px; color: #fff; font-size: 22px;
  cursor: pointer; z-index: 10; opacity: .8;
}
.lb-close:hover { opacity: 1; }
.nav {
  position: absolute; top: 50%; transform: translateY(-50%); color: #fff;
  font-size: 54px; cursor: pointer; user-select: none; opacity: .55; z-index: 10;
  padding: 0 18px;
}
.nav:hover { opacity: 1; }
.nav.prev { left: 8px; }
.nav.next { right: 8px; }
.lb-count {
  position: absolute; bottom: 14px; left: 50%; transform: translateX(-50%);
  color: #ddd; font-size: 13px; background: rgba(0,0,0,.5); padding: 4px 12px; border-radius: 999px;
}
.lb-live { color: var(--accent); margin-left: 6px; }
.lb-vmeta {
  position: absolute; bottom: 52px; left: 50%; transform: translateX(-50%);
  color: #ddd; font-size: 12px; background: rgba(0,0,0,.5); padding: 3px 10px; border-radius: 999px;
}
.lightbox-mask img,
.lightbox-mask video {
  max-width: 92vw; max-height: 84vh; object-fit: contain; border-radius: 4px;
  transition: transform .12s ease;
}
</style>
