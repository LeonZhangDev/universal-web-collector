<script setup>
// 跨任务资源库: 回答"我手上已经有什么"。
//
// 与任务详情里的资源列表是**两个视角**: 那个是"这个任务采到了什么", 这个是
// "整个下载目录里有什么"。同一张图被 sha256 去重复用时会在多处出现, 这里
// 各列一条并给出引用数(refs) —— 让人知道"删这个文件会不会影响另一个任务"。
import { computed, onMounted, ref } from "vue";
import { listLibrary, listLibraryAlbums } from "../api";
import { toast } from "../toast";

const items = ref([]);
const total = ref(0);
const pages = ref(1);
const stats = ref(null);
const albums = ref([]);
const loading = ref(false);

const query = ref({ q: "", kind: "all", album: "", page: 1, page_size: 40 });
const KINDS = [
  { key: "all", label: "全部" },
  { key: "image", label: "图片" },
  { key: "video", label: "视频" },
  { key: "text", label: "文本" },
];

// 名字扩展成人类可读, 与后端 core/disk.py 的口径保持一致
function fmtSize(n) {
  const v = Number(n) || 0;
  if (v <= 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let x = v;
  let i = 0;
  while (x >= 1024 && i < units.length - 1) {
    x /= 1024;
    i += 1;
  }
  return `${x >= 100 || i === 0 ? Math.round(x) : x.toFixed(1)} ${units[i]}`;
}

function baseName(p) {
  if (!p) return "—";
  const s = String(p).split(/[\\/]/);
  return s[s.length - 1] || p;
}

async function load() {
  loading.value = true;
  try {
    const r = await listLibrary({
      q: query.value.q || undefined,
      kind: query.value.kind,
      album: query.value.album || undefined,
      page: query.value.page,
      page_size: query.value.page_size,
    });
    items.value = r.items || [];
    total.value = r.total || 0;
    pages.value = r.pages || 1;
    stats.value = r.stats || null;
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  } finally {
    loading.value = false;
  }
}

async function loadAlbums() {
  try {
    const r = await listLibraryAlbums();
    albums.value = r.items || [];
  } catch (e) {
    albums.value = [];
  }
}

function search() {
  // 换筛选条件必须回到第一页 —— 否则在第 5 页改关键词会看到一个空列表,
  // 而用户以为"搜不到", 其实结果在第 1 页。
  query.value.page = 1;
  load();
}
function pickKind(k) {
  query.value.kind = k;
  search();
}
function goto(p) {
  const n = Math.min(pages.value, Math.max(1, p));
  if (n === query.value.page) return;
  query.value.page = n;
  load();
}

const pageList = computed(() => {
  const out = [];
  const cur = query.value.page;
  const last = pages.value;
  const push = (n) => {
    if (!out.includes(n)) out.push(n);
  };
  push(1);
  for (let i = cur - 1; i <= cur + 1; i++) if (i > 1 && i < last) push(i);
  if (last > 1) push(last);
  return out.sort((a, b) => a - b);
});

onMounted(() => {
  load();
  loadAlbums();
});
</script>

<template>
  <div class="lib">
    <div class="lib-head">
      <h2>资源库</h2>
      <span class="lib-sub" v-if="stats">
        {{ stats.resources }} 项 · {{ fmtSize(stats.bytes) }} · {{ stats.albums }} 个相册
      </span>
      <span class="grow"></span>
      <div class="kind-tabs">
        <button
          v-for="k in KINDS"
          :key="k.key"
          class="ktab"
          :class="{ on: query.kind === k.key }"
          @click="pickKind(k.key)"
        >{{ k.label }}</button>
      </div>
    </div>

    <div class="lib-bar">
      <input
        v-model="query.q"
        class="lib-search"
        placeholder="搜文件名或来源 URL…"
        @keyup.enter="search"
      />
      <select v-model="query.album" class="lib-sel" @change="search">
        <option value="">全部相册</option>
        <option v-for="a in albums" :key="a.album" :value="a.album">
          {{ a.album }} ({{ a.n }})
        </option>
      </select>
      <button class="ghost" @click="search">搜索</button>
    </div>

    <div v-if="loading" class="lib-empty">加载中…</div>

    <div v-else-if="!items.length" class="lib-empty">
      <svg viewBox="0 0 120 84" class="empty-art" aria-hidden="true">
        <rect x="14" y="20" width="92" height="52" rx="7" fill="none" stroke="currentColor" stroke-width="2" opacity=".35" />
        <path d="M14 44 h92" stroke="currentColor" stroke-width="2" opacity=".25" />
        <circle cx="38" cy="34" r="5" fill="currentColor" opacity=".3" />
        <path d="M22 66 l22-18 16 13 14-11 24 16" fill="none" stroke="currentColor" stroke-width="2" opacity=".3" />
      </svg>
      <p>{{ query.q || query.album ? "没有匹配的资源" : "资源库还是空的, 先去采集一些内容" }}</p>
    </div>

    <div v-else class="lib-grid">
      <div class="card" v-for="r in items" :key="r.id">
        <div class="thumb">
          <img
            v-if="r.type === 'image' && r.local_path"
            :src="`/files/raw?path=${encodeURIComponent(r.local_path)}`"
            loading="lazy"
            alt=""
            @error="(e) => (e.target.style.display = 'none')"
          />
          <span v-else class="ph">{{ r.type === "video" ? "🎬" : "📄" }}</span>
          <em class="tagkind">{{ r.type }}</em>
          <em v-if="r.duplicate_of" class="tagdup" title="感知指纹判定疑似重复">重复</em>
        </div>
        <div class="meta">
          <div class="nm" :title="r.local_path">{{ baseName(r.local_path) }}</div>
          <div class="sub">
            <span class="alb" :title="r.task_name">{{ r.task_name || "—" }}</span>
            <span class="size">{{ fmtSize(r.size) }}</span>
          </div>
          <div class="sub2">
            <span class="coll">{{ r.collector }}</span>
            <span v-if="r.refs > 1" class="refs" title="该文件被多个任务共用, 删任务不会删文件">
              共用 ×{{ r.refs }}
            </span>
          </div>
        </div>
      </div>
    </div>

    <div class="lib-pager" v-if="pages > 1">
      <button class="ghost" :disabled="query.page === 1" @click="goto(query.page - 1)">上一页</button>
      <button
        v-for="p in pageList"
        :key="p"
        class="ghost pnum"
        :class="{ on: p === query.page }"
        @click="goto(p)"
      >{{ p }}</button>
      <button class="ghost" :disabled="query.page === pages" @click="goto(query.page + 1)">下一页</button>
      <span class="ptot">共 {{ total }} 项 / {{ pages }} 页</span>
    </div>
  </div>
</template>

<style scoped>
.lib { padding: 4px 2px 24px; }
.lib-head { display: flex; align-items: baseline; gap: 10px; margin-bottom: 12px; }
.lib-head h2 { font-size: 16px; margin: 0; }
.lib-sub { color: var(--muted); font-size: 12px; }
.lib-head .grow { flex: 1; }
.kind-tabs { display: flex; gap: 4px; }
.ktab {
  background: var(--panel-2); color: var(--muted); border: 1px solid var(--border);
  border-radius: 7px; padding: 4px 10px; font-size: 12px; cursor: pointer;
  transition: color .15s, border-color .15s, background .15s;
}
.ktab:hover { color: var(--text); }
.ktab.on { color: var(--accent); border-color: var(--accent); background: color-mix(in srgb, var(--accent) 12%, transparent); }
.lib-bar { display: flex; gap: 8px; margin-bottom: 14px; }
.lib-search {
  flex: 1; background: var(--panel-2); border: 1px solid var(--border);
  border-radius: 8px; padding: 7px 10px; color: var(--text); font-size: 13px;
}
.lib-search:focus { outline: none; border-color: var(--accent); }
.lib-sel {
  background: var(--panel-2); border: 1px solid var(--border); border-radius: 8px;
  padding: 7px 10px; color: var(--text); font-size: 13px; max-width: 220px;
}
.lib-empty {
  display: flex; flex-direction: column; align-items: center; gap: 10px;
  padding: 56px 0; color: var(--muted); font-size: 13px;
}
.empty-art { width: 120px; height: 84px; color: var(--muted); }
.lib-grid {
  display: grid; gap: 12px;
  grid-template-columns: repeat(auto-fill, minmax(168px, 1fr));
}
.card {
  background: var(--panel-2); border: 1px solid var(--border); border-radius: 10px;
  overflow: hidden; transition: border-color .15s, transform .15s;
}
.card:hover { border-color: color-mix(in srgb, var(--accent) 45%, var(--border)); transform: translateY(-1px); }
.thumb {
  position: relative; aspect-ratio: 4 / 3; background: var(--panel);
  display: flex; align-items: center; justify-content: center; overflow: hidden;
}
.thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.thumb .ph { font-size: 30px; opacity: .55; }
.tagkind {
  position: absolute; left: 6px; top: 6px; font-style: normal; font-size: 10px;
  padding: 1px 6px; border-radius: 5px; background: rgba(0,0,0,.55); color: #fff;
}
.tagdup {
  position: absolute; right: 6px; top: 6px; font-style: normal; font-size: 10px;
  padding: 1px 6px; border-radius: 5px; background: color-mix(in srgb, var(--warn) 85%, #000); color: #1a1208;
}
.meta { padding: 8px 9px 9px; }
.nm { font-size: 12px; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sub, .sub2 {
  display: flex; align-items: center; gap: 6px; margin-top: 3px;
  font-size: 11px; color: var(--muted);
}
.sub .alb { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
.sub .size { flex: none; font-variant-numeric: tabular-nums; }
.sub2 .refs { color: var(--accent); flex: none; }
.lib-pager { display: flex; align-items: center; gap: 5px; margin-top: 18px; }
.pnum.on { color: var(--accent); border-color: var(--accent); }
.ptot { margin-left: auto; color: var(--muted); font-size: 12px; }
</style>
