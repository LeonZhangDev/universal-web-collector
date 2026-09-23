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
  libraryEditTags,
  librarySetFavorite,
  libraryVerify,
  listLibrary,
  listLibraryAlbums,
  listLibraryFailures,
  listLibraryTags,
  replayLibraryFailures,
  setTagColor,
  thumbUrl,
} from "../api";
import { toast } from "../toast";

const items = ref([]);
const total = ref(0);
const pages = ref(1);
const stats = ref(null);
const albums = ref([]);
const tags = ref([]);
// 调色板由后端下发(`/library/tags` 的 `colors`)。⚠️ 前端**不硬编码色值** ——
// 否则后端改了配色, 界面上还是旧的, 而没人会想到去查"色值写在哪"。
const tagPalette = ref([]);
const tagSep = ref("/");
const loading = ref(false);

const query = ref({
  q: "",
  kind: "all",
  album: "",
  tag: "",
  // 含子标签: 层级是名字前缀, 这是一次前缀匹配。默认**关** —— 精确匹配才是
  // "我就要这一个标签"时唯一正确的语义, 含子标签是额外的便利。
  tag_children: false,
  favorite: false,
  page: 1,
  page_size: 40,
});
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

// ---- 标签 / 收藏 ----
// 标签是"我自己定的分类", 收藏是"我要的那一批"。两者都不改文件、不改任务,
// 只是资源库这一层的附加信息 —— 所以它们永远不该让采集失败或数据丢失。
const tagDraft = ref("");        // 批量打标签的输入框
const busyTag = ref(false);
// 正在改颜色的那个标签(点一下色块弹出调色板)。空 = 没有弹层。
const colorEditTag = ref("");

// 标签 -> 色值。键取小写: 后端是 NOCASE 列, 而 JS 对象不是 —— 不折一下就会
// 出现"后端查得到颜色、界面点不亮"。
const tagColorMap = computed(() => {
  const m = {};
  for (const t of tags.value) {
    if (t.color) m[String(t.tag).toLowerCase()] = t.color;
  }
  return m;
});
function colorOf(tag) {
  return tagColorMap.value[String(tag).toLowerCase()] || "";
}
// 从后端下发的调色板里取色值。取不到就不上色(而不是猜一个) ——
// 猜出来的颜色会与"这个标签本来就没设色"无法区分。
function colorValue(key) {
  const hit = tagPalette.value.find((c) => c.key === key);
  return hit ? hit.value : "";
}
// 树里只显示**末段名**(`系列/角色A` -> `角色A`), 靠缩进表达层级。
// 全路径已经由缩进与父节点表达了, 再重复一遍会让长名字把这一行撑爆。
function tagLeaf(t) {
  const s = String(t || "");
  const i = s.lastIndexOf(tagSep.value);
  return i >= 0 ? s.slice(i + tagSep.value.length) : s;
}
// 缩进用**不换行空格**而不是普通空格/margin: HTML 会把连续普通空格折成一个,
// 而这块是横向换行排布的, margin 在折行时不会把标签自然地推到右边。
function tagIndent(t) {
  return "\u00a0\u00a0\u00a0".repeat(Math.max(0, t.depth || 0));
}
// 中间层节点自己可能一条资源都没有(用户只打过 `系列/角色A`), 这时显示"连子
// 标签一共多少"才是用户点下去会看到的数量。
function tagCount(t) {
  return t.n || t.n_tree || 0;
}
//: 当前筛选的标签有没有子标签(有才显示"含子标签"开关 —— 否则是个点了没用的按钮)
const hasChildren = computed(() => {
  const cur = query.value.tag;
  if (!cur) return false;
  const p = cur + tagSep.value;
  return tags.value.some((t) => String(t.tag).startsWith(p));
});

function pickTag(t) {
  // 再点一次同一个标签 = 取消筛选(比"再去找清空按钮"顺手)
  if (query.value.tag === t) {
    query.value.tag = "";
    query.value.tag_children = false;
  } else {
    query.value.tag = t;
  }
  colorEditTag.value = "";
  search();
}
function toggleTagChildren() {
  query.value.tag_children = !query.value.tag_children;
  search();
}
function toggleFavoriteOnly() {
  query.value.favorite = !query.value.favorite;
  search();
}

// 设置 / 清除标签颜色。⚠️ 成功后就地改 `tags` 里的那条, 而不是整份重拉:
// 重拉会把用户滚动到的位置和展开状态一起冲掉, 而颜色是这一屏内的操作。
async function applyTagColor(tag, color) {
  try {
    const r = await setTagColor(tag, color);
    const hit = tags.value.find((t) => t.tag === tag);
    if (hit) hit.color = r.color;
    colorEditTag.value = "";
    toast(r.color ? `已设置颜色` : "已清除颜色", "ok");
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  }
}

// 逗号/顿号/空格都当分隔符: 中文输入法下用户会自然地打顿号
function parseDraft(s) {
  return String(s || "")
    .split(/[,，、\s]+/)
    .map((x) => x.trim())
    .filter(Boolean);
}

async function applyTags(ids, { add = [], remove = [], clear = false } = {}) {
  if (!ids.length) {
    toast("先勾选要操作的资源", "warn");
    return false;
  }
  busyTag.value = true;
  try {
    const r = await libraryEditTags(ids, { add, remove, clear });
    const parts = [];
    if (r.added) parts.push(`新增 ${r.added} 个标签`);
    if (r.removed) parts.push(`去掉 ${r.removed} 个标签`);
    toast(parts.length ? parts.join(", ") : "没有变化", "ok");
    await load();
    await loadTags();
    return true;
  } catch (e) {
    // 后端在标签超长/超上限时返回 400 并说清是哪一条 —— 原样透出,
    // 换成"操作失败"用户就不知道该改什么了。
    toast(e.response?.data?.detail || String(e), "err");
    return false;
  } finally {
    busyTag.value = false;
  }
}

async function addDraftToSelected() {
  const ts = parseDraft(tagDraft.value);
  if (!ts.length) {
    toast("先输入要打的标签", "warn");
    return;
  }
  if (await applyTags([...selected.value], { add: ts })) tagDraft.value = "";
}

async function removeTagFromSelected(t) {
  await applyTags([...selected.value], { remove: [t] });
}

async function toggleFavorite(ids, value) {
  if (!ids.length) return;
  try {
    const r = await librarySetFavorite(ids, value);
    toast(`${value ? "已收藏" : "已取消收藏"} ${r.updated} 项`, "ok");
    await load();
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  }
}

// 卡片上的星标: 只作用于这一条, 点完立刻反映在卡片上(load 会刷新整页)
function starOne(r) {
  toggleFavorite([r.id], !r.favorite);
}

// 批量栏里的"收藏/取消": 看**本页被选中的那些**的状态决定动作 ——
// 全是已收藏就取消, 否则一律收藏。这比固定两颗按钮少一次判断。
// ⚠️ 只看本页: 选中项可能都在别的页, 那时 `every()` 会在空数组上恒真,
// 于是按钮永远显示"取消收藏" —— 一个看起来能用、实则反着的开关。
const selAllFav = computed(() => {
  const onPage = items.value.filter((r) => selected.value.has(r.id));
  return onPage.length > 0 && onPage.every((r) => r.favorite);
});

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

// ---- 死信(跨任务失败资源)与重放 ----
// null = 还没查过。与巡检不同: 巡检问"库里的记录还在不在磁盘上", 死信问
// "整个库里现在坏在哪、哪些还能救" —— 同一批 404 散在十几个任务里时, 逐个任务
// 点进去看根本拼不出全貌, 也就没人会去重放它们。
const failData = ref(null);
const failBusy = ref(false);
const failNotes = ref([]);
// 连 gone(源站已删/下线)一起看。默认关: 它们重放基本是空转, 混在一起会把
// "其实还有救的"淹没掉。
const failIncludeGone = ref(false);

async function loadFailures() {
  if (failBusy.value) return;
  failBusy.value = true;
  try {
    failData.value = await listLibraryFailures({
      limit: 50,
      include_gone: failIncludeGone.value,
    });
    failNotes.value = [];
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  } finally {
    failBusy.value = false;
  }
}

function toggleGone() {
  failIncludeGone.value = !failIncludeGone.value;
  loadFailures();
}

// 失败原因的中文标签: 从后端下发的 kinds 里取, 前端**不维护映射表** ——
// 维护一份就会在后端改措辞时静默对不上(这正是原来"正则解析文案"翻车的同一型)。
function kindLabel(kind) {
  return failData.value?.kinds.find((k) => k.kind === kind)?.label || kind;
}

// kind 为空 = 全部可重放的。条数取 `replayable` 而**不是** `n` —— 后者含
// corrupt(文件在、解码器说坏), 重放救不了, 拿它提示"将要重下 N 条"就是谎报。
async function replayFailures(kind = "") {
  if (failBusy.value) return;
  const n = kind
    ? (failData.value?.kinds.find((k) => k.kind === kind)?.replayable ?? 0)
    : (failData.value?.replayable ?? 0);
  if (!n) {
    toast("没有可重放的资源", "warn");
    return;
  }
  // 重放是对站点的**真实请求**, 必须先确认数量与范围。
  if (!confirm(`重新下载 ${n} 条(向站点发真实请求)?`)) return;
  failBusy.value = true;
  try {
    const r = await replayLibraryFailures({
      kinds: kind ? [kind] : [],
      includeGone: failIncludeGone.value,
    });
    // 跳过理由**一定要显示**: 点了 30 条只起来 4 条时, 不解释就等于让用户以为
    // 程序吞了 26 条。后端最多回 20 条理由 + 一条汇总。
    failNotes.value = r.notes || [];
    toast(
      `已提交 ${r.submitted} 条${r.skipped ? `, 跳过 ${r.skipped} 条` : ""}`,
      r.submitted ? "ok" : "warn"
    );
    await loadFailures();
    await load();
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  } finally {
    failBusy.value = false;
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
    loadTags();
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

// 媒体元数据: 图片宽高 / 视频时长。数据由后端在下载完成时落库(见
// core/task_manager.py 的"媒体元数据落库")。
//
// ⚠️ 视频时长在**没装 ffprobe** 的机器上就是 null —— 那是"没测量", 不是 0 秒。
// 所以这里返回空串而不是 "0:00": 把能力缺失显示成内容问题, 会让用户去重新下载
// 一个其实完好、只是本机缺个探测器的文件。
function mediaMeta(r) {
  if (r.width && r.height) return `${r.width}×${r.height}`;
  const d = Number(r.duration);
  if (!Number.isFinite(d) || d <= 0) return "";
  const t = Math.round(d);
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = String(t % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

async function load() {
  loading.value = true;
  try {
    const r = await listLibrary({
      q: query.value.q || undefined,
      kind: query.value.kind,
      album: query.value.album || undefined,
      tag: query.value.tag || undefined,
      tag_children: query.value.tag_children || undefined,
      favorite: query.value.favorite || undefined,
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

async function loadTags() {
  try {
    const r = await listLibraryTags();
    tags.value = r.items || [];
    tagPalette.value = r.colors || [];
    if (r.sep) tagSep.value = r.sep;
  } catch (e) {
    // 标签是附加信息, 拉不到就让筛选区空着 —— 不该因此挡住整个资源库
    tags.value = [];
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
  loadTags();
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
      <button class="ghost mini" :disabled="failBusy" @click="loadFailures">
        {{ failBusy ? "读取中…" : "失败诊断" }}
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
      <!-- 收藏筛选: 独立于标签, 可叠加 -->
      <button
        class="ghost fav-btn"
        :class="{ on: query.favorite }"
        :title="query.favorite ? '取消只看收藏' : '只看收藏'"
        @click="toggleFavoriteOnly"
      >
        {{ query.favorite ? "★" : "☆" }} 收藏<span v-if="stats?.favorites"> {{ stats.favorites }}</span>
      </button>
      <button class="ghost" @click="search">搜索</button>
      <button class="ghost" @click="togglePage">
        {{ pageAllOn ? "取消本页" : "全选本页" }}
      </button>
    </div>

    <!-- 标签条: 点一下筛, 再点一下取消。只在真有标签时占版面。
         ⚠️ 层级是**名字里的前缀**(`系列/角色A`), 父节点本身未必是个标签 ——
         所以这里按 `parent` 补齐中间层并缩进, 而不是只平铺"有资源的那些"。 -->
    <div class="tag-bar" v-if="tags.length || query.tag">
      <span class="tb-label">标签</span>
      <span class="tb-chips">
        <span class="chip-wrap" v-for="t in tags.slice(0, 24)" :key="t.tag">
          <button
            class="chip"
            :class="{ on: query.tag === t.tag }"
            :style="colorOf(t.tag) ? { '--tc': colorValue(colorOf(t.tag)) } : null"
            :title="`${t.tag}\n自己 ${t.n} 项${t.n_tree > t.n ? ` · 连子标签 ${t.n_tree} 项` : ''}`"
            @click="pickTag(t.tag)"
          >
            <i class="tc-dot" v-if="colorOf(t.tag)"></i>
            <span class="tc-pad">{{ tagIndent(t) }}</span>{{ tagLeaf(t) }}
            <em>{{ tagCount(t) }}</em>
          </button>
          <button
            class="tc-edit"
            :title="`给「${t.tag}」设颜色`"
            @click.stop="colorEditTag = colorEditTag === t.tag ? '' : t.tag"
          >🎨</button>
          <span class="palette" v-if="colorEditTag === t.tag">
            <button
              v-for="c in tagPalette"
              :key="c.key"
              class="sw"
              :style="{ background: c.value }"
              :title="c.label"
              @click="applyTagColor(t.tag, c.key)"
            ></button>
            <button class="sw sw-none" title="清除颜色" @click="applyTagColor(t.tag, '')">✕</button>
          </span>
        </span>
      </span>
      <!-- 被筛的标签如果不在前 24 个里, 上面那行就看不到它 —— 补一颗,
           否则用户会看到一个"筛选生效了但标签条里没有高亮项"的困惑状态 -->
      <button
        v-if="query.tag && !tags.slice(0, 24).some((t) => t.tag === query.tag)"
        class="chip on"
        @click="pickTag(query.tag)"
      >{{ query.tag }} ✕</button>
      <label v-if="hasChildren" class="tb-sub">
        <input type="checkbox" :checked="query.tag_children" @change="toggleTagChildren" />
        含子标签
      </label>
      <span v-if="tags.length > 24" class="tb-more">还有 {{ tags.length - 24 }} 个</span>
      <button v-if="query.tag" class="ghost mini" @click="pickTag(query.tag)">清除筛选</button>
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

    <!-- 死信: 跨任务的失败资源分布 + 重放。与上面的"校验文件"是**两件事**:
         那个问"库里的记录还在不在磁盘上", 这个问"整个库现在坏在哪、哪些还能救"。
         同一批 404 散在十几个任务里时, 逐个任务点进去看根本拼不出全貌。 -->
    <div v-if="failData" class="fail-box">
      <div class="fb-head">
        <b>失败 {{ failData.total }} 项</b>
        <span class="fb-sub">其中可重放 {{ failData.replayable }} 项</span>
        <label class="fb-gone" title="源站已删/已下线。默认不看 —— 重放它们基本是空转">
          <input type="checkbox" :checked="failIncludeGone" @change="toggleGone" />
          含源站已删
        </label>
        <span class="grow"></span>
        <button
          class="ghost mini"
          :disabled="failBusy || !failData.replayable"
          @click="replayFailures()"
        >全部重放</button>
        <button class="ghost mini" @click="failData = null; failNotes = []">收起</button>
      </div>

      <div class="fb-kinds" v-if="failData.kinds.length">
        <span class="fb-kind" v-for="k in failData.kinds" :key="k.kind">
          <span class="fb-lab">{{ k.label }}</span>
          <em class="fb-n">{{ k.n }}</em>
          <!-- ⚠️ 用 k.replayable 而不是 k.n: n 含 corrupt(文件还在、只是解码器
               读不动), 那批重下救不了。按 n 显示会变成"点 12 条、起来 0 条"。 -->
          <button
            v-if="k.replayable"
            class="ghost mini"
            :disabled="failBusy"
            :title="`只重放「${k.label}」这一类, 共 ${k.replayable} 条`"
            @click="replayFailures(k.kind)"
          >重放 {{ k.replayable }}</button>
          <span
            v-else
            class="fb-nr"
            title="重下救不了(多数是文件还在、只是内容坏了), 所以不提供重放"
          >重放不了</span>
        </span>
      </div>
      <p class="fb-note" v-else>没有失败资源。</p>

      <!-- 跳过理由必须显示: "点了 30 条只起来 4 条"不解释就等于程序吞了 26 条 -->
      <div class="fb-notes" v-if="failNotes.length">
        <div class="fb-nrow" v-for="(n, i) in failNotes" :key="i">{{ n }}</div>
      </div>

      <div class="fb-list" v-if="failData.items.length">
        <div class="fb-row" v-for="it in failData.items.slice(0, 20)" :key="it.id">
          <em class="fb-k">{{ kindLabel(it.error_kind) }}</em>
          <span class="fb-p" :title="it.local_path || it.url">{{ it.url }}</span>
          <span class="fb-t">{{ it.task_name || `#${it.task_id}` }}</span>
        </div>
        <div v-if="failData.items.length > 20" class="fb-more">
          只列出 20 项; 重放不受此限制(按原因整批)
        </div>
      </div>
    </div>

    <!-- 批量操作栏: 勾选后出现 -->
    <div class="bulk-bar" v-if="selectedCount > 0">
      <span class="sel-count">已选 <b>{{ selectedCount }}</b> 项</span>
      <button class="ghost mini" @click="downloadSelected">打包下载</button>
      <button class="ghost mini" @click="removeSelected(false)">删除记录</button>
      <button class="ghost mini danger" @click="removeSelected(true)">删除记录与文件</button>
      <span class="sep"></span>
      <!-- 打标签: 逗号/顿号/空格分隔, 可一次打多个 -->
      <input
        v-model="tagDraft"
        class="tag-input"
        :disabled="busyTag"
        placeholder="打标签, 逗号分隔…"
        @keyup.enter="addDraftToSelected"
      />
      <button class="ghost mini" :disabled="busyTag || !tagDraft.trim()" @click="addDraftToSelected">
        打标签
      </button>
      <button
        class="ghost mini"
        :disabled="busyTag"
        :title="selAllFav ? '取消收藏所选' : '收藏所选'"
        @click="toggleFavorite([...selected], !selAllFav)"
      >{{ selAllFav ? "★ 取消收藏" : "☆ 收藏" }}</button>
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
          <!-- 星标: 单击只作用于这一条 -->
          <button
            class="star"
            :class="{ on: r.favorite }"
            :title="r.favorite ? '取消收藏' : '收藏'"
            @click.stop="starOne(r)"
          >{{ r.favorite ? "★" : "☆" }}</button>
        </div>
        <div class="meta">
          <div class="nm" :title="r.local_path">{{ baseName(r.local_path) }}</div>
          <div class="sub">
            <span class="alb" :title="r.task_name">{{ r.task_name || "—" }}</span>
            <span class="dims" v-if="mediaMeta(r)">{{ mediaMeta(r) }}</span>
            <span class="size">{{ fmtSize(r.size) }}</span>
          </div>
          <div class="sub2">
            <span class="coll">{{ r.collector }}</span>
            <span v-if="r.refs > 1" class="refs" title="该文件被多个任务共用, 删任务不会删文件">
              共用 ×{{ r.refs }}
            </span>
          </div>
          <!-- 标签: 点一下就地筛选。标签是用户自己定的, 所以顺序按存储顺序(字母序)即可,
               不做"重要度排序" —— 那需要用户去维护优先级, 是另一种负担。
               有颜色的标签左侧带一个圆点: 颜色是**标签**的属性, 全库统一, 所以
               同一颗标签在每张卡上都长一样 —— 这正是它有用的原因。 -->
          <div class="card-tags" v-if="(r.tags || []).length">
            <button
              v-for="t in r.tags"
              :key="t"
              class="minichip"
              :class="{ on: query.tag === t }"
              :style="colorOf(t) ? { '--tc': colorValue(colorOf(t)) } : null"
              :title="t"
              @click.stop="pickTag(t)"
            >
              <i class="tc-dot" v-if="colorOf(t)"></i>{{ t }}
            </button>
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
/* 死信面板: 用 err 色系而不是 warn —— 校验发现的"文件不在"是环境问题, 而失败
   资源是"该拿到却没拿到", 更接近错误。两者同时在场时颜色要能区分开。 */
.fail-box {
  margin-bottom: 12px; padding: 10px 12px; border-radius: 9px;
  background: color-mix(in srgb, var(--err) 8%, var(--panel-2));
  border: 1px solid color-mix(in srgb, var(--err) 32%, var(--border));
}
.fb-head { display: flex; align-items: baseline; gap: 10px; font-size: 13px; flex-wrap: wrap; }
.fb-head .fb-sub { color: var(--muted); font-size: 12px; }
.fb-head .grow { flex: 1; }
.fb-gone { font-size: 11px; color: var(--muted); display: flex; align-items: center; gap: 4px; }
.fb-kinds { margin-top: 8px; display: flex; flex-wrap: wrap; gap: 6px; }
.fb-kind {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 2px 8px; border-radius: 999px; font-size: 12px;
  background: var(--panel-2); border: 1px solid var(--border);
}
.fb-lab { color: var(--text); }
.fb-n { font-style: normal; color: var(--muted); font-variant-numeric: tabular-nums; }
/* "重放不了"必须看起来就是禁用的, 而不是一个点了没反应的按钮 */
.fb-nr { font-size: 10px; color: var(--muted); opacity: 0.75; }
.fb-note { margin: 8px 0 0; font-size: 12px; color: var(--muted); }
.fb-notes {
  margin-top: 8px; padding: 6px 8px; border-radius: 7px;
  background: var(--panel-2); max-height: 120px; overflow: auto;
}
.fb-nrow { font-size: 11px; color: var(--muted); }
.fb-list { margin-top: 8px; display: flex; flex-direction: column; gap: 4px; max-height: 200px; overflow: auto; }
.fb-row { display: flex; align-items: baseline; gap: 8px; font-size: 12px; }
.fb-k {
  font-style: normal; font-size: 10px; padding: 1px 6px; border-radius: 5px; flex: none;
  background: color-mix(in srgb, var(--err) 72%, #000); color: #fff;
}
.fb-p { color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
.fb-t { flex: none; max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fb-more { font-size: 11px; color: var(--muted); margin-top: 2px; }
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
/* 收藏筛选: 打开时用暖色, 与"收藏"的心理预期一致(不要用蓝色 accent,
   那会与"类型筛选"看起来是同一类开关) */
.fav-btn.on {
  color: var(--warn); border-color: color-mix(in srgb, var(--warn) 55%, var(--border));
  background: color-mix(in srgb, var(--warn) 12%, transparent);
}
/* 标签条 */
.tag-bar {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  margin: -6px 0 14px;
}
.tb-label { color: var(--muted); font-size: 12px; flex: none; }
.tb-more { color: var(--muted); font-size: 11px; }
/* 含子标签: 只在当前标签真有子标签时才出现 */
.tb-sub {
  display: inline-flex; align-items: center; gap: 4px; font-size: 11px;
  color: var(--muted); cursor: pointer; user-select: none; flex: none;
}
.tb-sub input { margin: 0; accent-color: var(--accent); }
/* display:contents 让这层包装对 flex 布局透明 —— 里面的每颗标签仍然直接参与
   `.tag-bar` 的换行与间距, 而不是被塞进一个不会换行的格子里。 */
.tb-chips { display: contents; }
.chip {
  background: var(--panel-2); color: var(--muted); border: 1px solid var(--border);
  border-radius: 20px; padding: 3px 10px; font-size: 12px; cursor: pointer;
  transition: color .15s, border-color .15s, background .15s;
}
.chip em { font-style: normal; opacity: .55; margin-left: 3px; font-size: 11px; }
.chip:hover { color: var(--text); }
.chip.on {
  color: var(--accent); border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 12%, transparent);
}
/* 有颜色的标签: 边框与一颗圆点跟着走。⚠️ 只改**边框和圆点**, 不改文字色 ——
   调色板里有黄有绿, 直接当文字色会有几档在深色底上读不清。 */
.chip[style*="--tc"] { border-color: color-mix(in srgb, var(--tc) 55%, var(--border)); }
.chip[style*="--tc"].on { border-color: var(--tc); }
.tc-dot {
  display: inline-block; width: 7px; height: 7px; border-radius: 50%;
  background: var(--tc); margin-right: 5px; vertical-align: 1px;
}
/* 缩进用不换行空格(见 tagIndent), 这里只在折行时不允许被断开 */
.tc-pad { white-space: pre; }
/* 改颜色: 平时不出现, hover 那颗标签才浮现 —— 一页几十个色块太吵 */
.chip-wrap { position: relative; display: inline-flex; align-items: center; }
.tc-edit {
  opacity: 0; background: none; border: none; cursor: pointer; font-size: 11px;
  padding: 0 2px; margin-left: -4px; line-height: 1; transition: opacity .15s;
}
.chip-wrap:hover .tc-edit { opacity: .7; }
.tc-edit:hover { opacity: 1; }
.palette {
  position: absolute; top: 100%; left: 0; z-index: 30; margin-top: 4px;
  display: flex; gap: 4px; padding: 6px; border-radius: 8px;
  background: var(--panel); border: 1px solid var(--border);
  box-shadow: 0 6px 20px rgba(0, 0, 0, .28);
}
.sw {
  width: 16px; height: 16px; border-radius: 50%; border: 1px solid rgba(0, 0, 0, .25);
  cursor: pointer; padding: 0;
}
.sw:hover { transform: scale(1.15); }
.sw-none {
  background: var(--panel-2); color: var(--muted); font-size: 10px; line-height: 1;
  border-color: var(--border);
}
/* 批量栏里的标签输入 */
.bulk-bar .sep {
  width: 1px; height: 18px; background: var(--border); flex: none; margin: 0 2px;
}
.tag-input {
  background: var(--panel-2); border: 1px solid var(--border); border-radius: 7px;
  padding: 3px 8px; color: var(--text); font-size: 12px; width: 170px;
}
.tag-input:focus { outline: none; border-color: var(--accent); }
.tag-input:disabled { opacity: .5; }
/* 卡片上的星标: 平时淡, hover 才亮 —— 否则一页 40 颗空心星会很吵。
   ⚠️ 位置必须避开 `.pick`(右下角的选择框): 两者都放 `right: 6px` 会叠在一起,
   表现为"星标点不到" —— 而用户只会觉得"这个按钮坏了"。往左让开 26px。 */
.star {
  position: absolute; right: 32px; bottom: 6px; z-index: 2;
  background: rgba(0, 0, 0, .42); border: none; border-radius: 6px;
  color: #d7dde5; font-size: 13px; line-height: 1; padding: 3px 6px;
  cursor: pointer; opacity: 0; transition: opacity .15s, color .15s;
}
.card:hover .star, .star.on { opacity: 1; }
.star.on { color: var(--warn); }
/* 卡片上的标签小片 */
.card-tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 5px; }
.minichip {
  background: color-mix(in srgb, var(--accent) 10%, var(--panel-2));
  color: var(--muted); border: 1px solid transparent; border-radius: 5px;
  padding: 0 6px; font-size: 10px; line-height: 16px; cursor: pointer;
  max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.minichip:hover { color: var(--text); border-color: var(--border); }
.minichip.on { color: var(--accent); border-color: var(--accent); }
/* 带颜色的标签: 圆点跟着走, 文字色不动(理由同上方 `.chip[style*="--tc"]`) */
.minichip[style*="--tc"] {
  background: color-mix(in srgb, var(--tc) 14%, var(--panel-2));
  border-color: color-mix(in srgb, var(--tc) 40%, transparent);
}
.minichip .tc-dot { width: 6px; height: 6px; margin-right: 4px; }
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
/* 尺寸/时长用等宽数字: 一排卡片里 1920×1080 与 800×600 不跳动, 扫一眼就能比大小 */
.sub .dims {
  flex: none; font-variant-numeric: tabular-nums;
  padding: 0 4px; border-radius: 4px;
  background: color-mix(in srgb, var(--fg, #888) 10%, transparent);
}
.sub2 .refs { color: var(--accent); flex: none; }
.lib-pager { display: flex; align-items: center; gap: 5px; margin-top: 18px; }
.pnum.on { color: var(--accent); border-color: var(--accent); }
.ptot { margin-left: auto; color: var(--muted); font-size: 12px; }
</style>
