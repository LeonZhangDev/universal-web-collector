<script setup>
// 跨任务资源库: 回答"我手上已经有什么"。
//
// 与任务详情里的资源列表是**两个视角**: 那个是"这个任务采到了什么", 这个是
// "整个下载目录里有什么"。同一张图被 sha256 去重复用时会在多处出现, 这里
// 各列一条并给出引用数(refs) —— 让人知道"删这个文件会不会影响另一个任务"。
import { computed, onMounted, ref } from "vue";
import {
  libraryArchiveUrl,
  libraryBulkDelete,
  libraryVerify,
  listLibrary,
  listLibraryAlbums,
  thumbUrl,
} from "../api";
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

//: 哪些类型值得去取缩略图。⚠️ 不含 text —— 后端在生成失败时会**回退原图**,
//: 而 .txt 回退过来是一个文本文件, `<img>` 拿到的必然是一张破图。
//: 视频可以: ffmpeg 取首帧当海报图(见 core/thumbs.py)。
const THUMBABLE = ["image", "video"];

// ---- 多选 + 批量操作 ----
// 选中集合存 id 且**跨翻页保留**(与任务表格同一套语义: 资源库一页 40 项,
// 全选"这一批不要的"往往要跨几页, 每翻一页就清空等于逼用户重来)。
const selected = ref(new Set());
const selectedCount = ref(0);
function syncCount() {
  selectedCount.value = selected.value.size;
}
function isSel(id) {
  return selected.value.has(id);
}
function toggleRow(id) {
  if (selected.value.has(id)) selected.value.delete(id);
  else selected.value.add(id);
  syncCount();
}
const pageAllOn = computed(
  () => items.value.length > 0 && items.value.every((r) => selected.value.has(r.id))
);
function togglePage() {
  const all = pageAllOn.value;
  for (const r of items.value) {
    if (all) selected.value.delete(r.id);
    else selected.value.add(r.id);
  }
  syncCount();
}
function clearSel() {
  selected.value.clear();
  syncCount();
}

// 巡检结果。null = 还没跑过; 跑过但零问题则展示"全部完好"。
const verifyResult = ref(null);
const verifying = ref(false);

async function runVerify() {
  if (verifying.value) return;
  verifying.value = true;
  try {
    const r = await libraryVerify({ limit: 500 });
    verifyResult.value = r;
    if (!r.marked) {
      toast(`已检查 ${r.checked} 项, 文件都还在`, "ok");
    } else {
      // 措辞刻意区分"缺失"与"截断": 前者是文件没了, 后者是文件还在但可能看不全,
      // 处置方式不同(重下 vs 可能还能用), 合成一句"发现 N 个问题"就丢掉了这个区别。
      toast(
        `检查 ${r.checked} 项: 缺失 ${r.missing} 项, 疑似截断 ${r.truncated} 项`,
        r.missing ? "err" : "warn"
      );
    }
    load();
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  } finally {
    verifying.value = false;
  }
}

function downloadSelected() {
  if (!selectedCount.value) {
    toast("先勾选要下载的资源", "warn");
    return;
  }
  // 直接导航到流式 zip 接口 —— 打包在服务端边压边发, 不经过 JS 内存
  window.location.href = libraryArchiveUrl([...selected.value]);
}

async function removeSelected(withFiles) {
  if (!selectedCount.value) {
    toast("先勾选要删除的资源", "warn");
    return;
  }
  // 删文件是不可逆的, 所以两种口径分开确认、并且**说清会发生什么** ——
  // 只写"确认删除?"的话, 用户没法区分这两颗按钮。
  const msg = withFiles
    ? `确认删除选中的 ${selectedCount.value} 项？\n\n会连同磁盘上的文件一起删除(被其它任务共用的文件会自动保留)。此操作不可撤销。`
    : `确认删除选中的 ${selectedCount.value} 项？\n\n仅删除资源库记录, 文件保留在磁盘上。`;
  if (!confirm(msg)) return;
  try {
    const r = await libraryBulkDelete([...selected.value], withFiles);
    const parts = [`已删除 ${r.deleted} 条记录`];
    if (withFiles) {
      parts.push(`删除文件 ${r.files} 个 (${fmtSize(r.bytes)})`);
      if ((r.kept_files || []).length) {
        // ⚠️ 必须说出来。不说的话用户下次在别的任务里又看到同一张图,
        // 结论会是"删了没用", 而不是"它被共用了所以留下来了"。
        parts.push(`${r.kept_files.length} 个文件因被其它任务共用而保留`);
      }
    }
    const errN = Object.keys(r.errors || {}).length;
    if (errN) parts.push(`${errN} 个出错`);
    toast(parts.join(", "), errN ? "warn" : "ok");
    clearSel();
    load();
    loadAlbums();
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  }
}

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
      <button class="ghost mini" :disabled="verifying" @click="runVerify">
        {{ verifying ? "校验中…" : "校验文件" }}
      </button>
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
      <button class="ghost" @click="togglePage">
        {{ pageAllOn ? "取消本页" : "全选本页" }}
      </button>
    </div>

    <!-- 巡检结果: 只在有问题时占版面, 全部完好就一句话 -->
    <div v-if="verifyResult && verifyResult.marked" class="verify-box">
      <div class="vb-head">
        <b>校验发现 {{ verifyResult.marked }} 项异常</b>
        <span class="vb-sub">
          已检查 {{ verifyResult.checked }} 项 ·
          缺失 {{ verifyResult.missing }} · 疑似截断 {{ verifyResult.truncated }}
        </span>
        <span class="grow"></span>
        <button class="ghost mini" @click="verifyResult = null">知道了</button>
      </div>
      <div class="vb-list">
        <div class="vb-row" v-for="it in verifyResult.items.slice(0, 50)" :key="it.id">
          <em class="vb-kind" :class="it.kind">{{ it.kind === "missing" ? "缺失" : "截断" }}</em>
          <span class="vb-name" :title="it.path">{{ it.name }}</span>
          <span class="vb-reason">{{ it.reason }}</span>
        </div>
        <div v-if="verifyResult.items.length > 50" class="vb-more">
          还有 {{ verifyResult.items.length - 50 }} 项, 已全部标记在列表里
        </div>
      </div>
      <p class="vb-note">
        只标记不删除 —— 判据可能误报, 删文件是不可逆的。删掉记录或重下这几项即可。
      </p>
    </div>

    <!-- 批量操作栏: 勾选后出现 -->
    <div class="bulk-bar" v-if="selectedCount > 0">
      <span class="sel-count">已选 <b>{{ selectedCount }}</b> 项</span>
      <button class="ghost mini" @click="downloadSelected">打包下载</button>
      <button class="ghost mini" @click="removeSelected(false)">删除记录</button>
      <button class="ghost mini danger" @click="removeSelected(true)">删除记录与文件</button>
      <span class="grow"></span>
      <span class="hint">选择跨翻页保留</span>
      <button class="ghost mini" @click="clearSel">清空</button>
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
      <div class="card" v-for="r in items" :key="r.id" :class="{ sel: isSel(r.id) }">
        <div class="thumb">
          <img
            v-if="THUMBABLE.includes(r.type) && r.local_path"
            :src="thumbUrl(r.local_path, 320)"
            loading="lazy"
            decoding="async"
            alt=""
            @error="(e) => (e.target.style.display = 'none')"
          />
          <span v-else class="ph">{{ r.type === "video" ? "🎬" : "📄" }}</span>
          <em class="tagkind">{{ r.type }}</em>
          <em v-if="r.duplicate_of" class="tagdup" title="感知指纹判定疑似重复">重复</em>
          <!-- 巡检标记: missing/corrupt 由 /library/verify 写入 error_kind。
               ⚠️ 它也可能出现在**成功**资源上(status=done + corrupt),
               所以这里只做标记, 不隐藏卡片、也不改状态 —— 文件通常还在,
               只是可能看不全。 -->
          <em
            v-if="r.error_kind === 'missing' || r.error_kind === 'corrupt'"
            class="tagbad"
            :title="r.note || ''"
          >{{ r.error_kind === "missing" ? "缺失" : "疑似损坏" }}</em>
          <label class="pick" :title="isSel(r.id) ? '取消选择' : '选择'">
            <input type="checkbox" :checked="isSel(r.id)" @change="toggleRow(r.id)" />
          </label>
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
.kind-tabs + .ktab, .lib-head > .ghost + .kind-tabs { margin-left: 4px; }
/* 校验结果面板: 只在真有问题时才展开, 没问题就只留一句 toast */
.verify-box {
  margin-bottom: 12px; padding: 10px 12px; border-radius: 9px;
  background: color-mix(in srgb, var(--warn) 9%, var(--panel-2));
  border: 1px solid color-mix(in srgb, var(--warn) 38%, var(--border));
}
.vb-head { display: flex; align-items: baseline; gap: 10px; font-size: 13px; }
.vb-head .vb-sub { color: var(--muted); font-size: 12px; }
.vb-head .grow { flex: 1; }
.vb-list { margin-top: 8px; display: flex; flex-direction: column; gap: 4px; max-height: 220px; overflow: auto; }
.vb-row { display: flex; align-items: baseline; gap: 8px; font-size: 12px; }
.vb-kind {
  font-style: normal; font-size: 10px; padding: 1px 6px; border-radius: 5px; flex: none;
  background: color-mix(in srgb, var(--warn) 80%, #000); color: #1a1208;
}
/* 缺失比截断更严重: 文件根本没了, 红色 */
.vb-kind.missing { background: color-mix(in srgb, var(--err) 78%, #000); color: #fff; }
.vb-name { flex: none; max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.vb-reason { color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.vb-more { font-size: 11px; color: var(--muted); margin-top: 2px; }
.vb-note { margin: 8px 0 0; font-size: 11px; color: var(--muted); }
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
/* 巡检标记(缺失/疑似损坏)。红色, 与"重复"的黄色区分开 ——
   重复只是提示, 这个是要处理的。 */
.tagbad {
  position: absolute; left: 6px; bottom: 6px; font-style: normal; font-size: 10px;
  padding: 1px 6px; border-radius: 5px;
  background: color-mix(in srgb, var(--err) 82%, #000); color: #fff;
}
/* 选择框: 平时半透明, 悬停/已选时才完全显形 ——
   不选东西时它不该跟缩略图抢注意力。 */
.pick {
  position: absolute; right: 6px; bottom: 6px; width: 22px; height: 22px;
  display: flex; align-items: center; justify-content: center;
  border-radius: 6px; background: rgba(0, 0, 0, .45);
  opacity: 0; transition: opacity .12s; cursor: pointer;
}
.card:hover .pick, .card.sel .pick { opacity: 1; }
.pick input { accent-color: var(--accent); cursor: pointer; margin: 0; }
.card.sel { border-color: var(--accent); }
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
