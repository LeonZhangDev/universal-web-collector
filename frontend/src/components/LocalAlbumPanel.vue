<script setup>
// 本地相册集: 把自己电脑上的目录当相册集来浏览。
//
// 与「资源库」的分工
// ==================
// 资源库回答"我**下过**什么"(它的每一行都对应本程序的一个产物, 所以它能删文件);
// 这里回答"我**本来就有**什么" —— 手机导出、旧收藏、别的工具存下来的照片。
//
// 三条约束决定了这个界面的样子(每一条都有具体的原因, 不是风格问题):
//
// 1. **只读。** 界面上的"忘记这个目录"只删一条登记记录, 磁盘上一个文件都不动。
//    所以按钮的文案与二次确认必须说清楚 —— 不然用户根本不敢点。
// 2. **随机池是索引快照, 要能自报时刻。** 它不是一个实时查询(几万张每次重扫不
//    合理)。所以界面上**必须**显示"从 N 张里抽 · 快照于 HH:MM" —— 否则
//    "怎么没抽到我刚放进去的那张"这件事没有任何解释入口。
//    与之相对, 打开某个相册**永远是实时列目录**, 所以看得见的一定打得开。
// 3. **失效的东西要说出来。** 收藏的照片被删掉之后, 那条记录还在库里。静默过滤
//    会让用户以为收藏功能坏了; 所以它单独列在"已失效"里, 并且**能删掉**。
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import FolderPicker from "./FolderPicker.vue";
import Lightbox from "./Lightbox.vue";
import {
  addLocalRoot,
  forgetLocalFavorite,
  getLocalDuplicates,
  getLocalOnThisDay,
  getLocalRandom,
  listLocalAlbums,
  listLocalFavorites,
  listLocalPhotos,
  listLocalRoots,
  pruneLocalThumbs,
  removeLocalRoot,
  scanLocalRoot,
  setLocalFavorite,
  updateLocalRootExclude,
} from "../api";
import { toast } from "../toast";

// random | albums | memories | favorites。
// 「往年今日」是一等公民而不是相册里的一个筛选项: 它的入口价值就在于**不用你记得
// 任何事** —— 一旦要点进某个相册再筛日期, 它就退化成了一个普通搜索。
const tab = ref("random");
const stats = ref(null);
const roots = ref([]);
const loading = ref(false);
const busy = ref(false);
const errorMsg = ref("");
const picker = ref(false);

// ---- 随机墙 ----
// `seed` 与 `page` 是两个不同的东西, 别合并:
//   换一批 = 换 seed(整副牌重洗);  更多 = page+1(同一副牌的下一页, 不会重复)。
const wall = ref([]);
const wallMeta = ref({ seed: 0, page: 0, pool: 0, has_more: false, at: null, truncated: false });
const wallOpts = ref({ count: 60, mode: "album", root_id: "", min_kb: 0, favorites_only: false });

// ---- 相册 ----
const albums = ref([]);
const albumBriefs = ref([]);
const albumOpts = ref({ q: "", sort: "mtime", order: "desc" });
const opened = ref(null);               // { root_id, rel, name, root_name }
const photos = ref([]);
const photosTotal = ref(0);

// ---- 往年今日 ----
const memories = ref(null);             // { date, years, total, basis }
// ---- 查重复 ----
// ⚠️ 结果里那个 `reason` 一定要显示出来: 没有 ffmpeg 时后端会返回
// "no-decoder"(一张都没算), 而把它当成"没有重复"就是最典型的一次假绿 ——
// 界面上什么都不显示, 用户只会以为自己这个相册很干净。
const dupes = ref(null);
const dupLoading = ref(false);
// ---- 排除模式 ----
const editing = ref(null);              // 正在编辑排除模式的那个 root
const editText = ref("");

// ---- 收藏 ----
const favs = ref([]);
const favStale = ref([]);

// ---- 灯箱与幻灯片 ----
const lb = ref({ show: false, index: 0 });
const playing = ref(false);
//: 每张停多久。写死而不是做成旋钮: 幻灯片是"放着看"的场景, 多一个输入框只会
//: 让工具栏更长, 而 2~8 秒之间的差别没有大到需要用户去调。
const SLIDE_MS = 4000;
let slideTimer = null;

const SORTS = [
  { key: "mtime", label: "最近修改" },
  { key: "name", label: "按名字" },
  { key: "photos", label: "按张数" },
  { key: "bytes", label: "按体积" },
];

// 灯箱看的就是当前这一屏: 随机墙 / 某个相册 / 收藏。
// 刻意**不做成页面级的全局序列** —— 那样"这张图属于哪一堆"就消失了, 而用户
// 按下左右键时心里是有个范围的。
const lbList = computed(() => {
  if (opened.value) return photos.value;
  if (tab.value === "favorites") return favs.value;
  return wall.value;
});
const lbImages = computed(() =>
  lbList.value.map((p) => ({ url: p.photo_url, name: p.name }))
);
// 随机墙翻到末尾时再要一页, 幻灯片就能一直放下去(不这么做, 放到最后一屏就停了,
// 而用户以为"就这么多"). 只在随机墙上做: 相册内是分页加载的, 自动往下要会把
// "我翻到哪了"这件事弄丢。
watch(
  () => lb.value.index,
  (i) => {
    if (!playing.value || opened.value || tab.value !== "random") return;
    if (i >= wall.value.length - 1 && wallMeta.value.has_more) loadWall(true);
  }
);

function errText(e) {
  // 后端的 detail 是一个 `{kind, message}` 对象(见 api/local.py 的 _fail):
  // 直接往界面上贴会显示 `[object Object]`。代号优先, 文案兜底。
  const d = e?.response?.data?.detail;
  if (d && typeof d === "object") return d.message || d.kind || "请求失败";
  return d || String(e);
}

async function loadRoots() {
  const data = await listLocalRoots();
  roots.value = data.items || [];
  stats.value = data.stats || null;
}

async function loadWall(append = false) {
  loading.value = true;
  errorMsg.value = "";
  try {
    const data = await getLocalRandom({
      count: wallOpts.value.count,
      mode: wallOpts.value.mode,
      root_id: wallOpts.value.root_id || undefined,
      min_bytes: Math.round((wallOpts.value.min_kb || 0) * 1024),
      favorites_only: wallOpts.value.favorites_only,
      seed: append ? wallMeta.value.seed : undefined,
      page: append ? wallMeta.value.page + 1 : 0,
    });
    wall.value = append ? wall.value.concat(data.items) : data.items;
    wallMeta.value = data;
  } catch (e) {
    errorMsg.value = errText(e);
  } finally {
    loading.value = false;
  }
}

async function loadAlbums() {
  loading.value = true;
  errorMsg.value = "";
  try {
    const data = await listLocalAlbums({
      root_id: wallOpts.value.root_id || undefined,
      q: albumOpts.value.q || undefined,
      sort: albumOpts.value.sort,
      order: albumOpts.value.order,
    });
    albums.value = data.items || [];
    albumBriefs.value = data.roots || [];
  } catch (e) {
    errorMsg.value = errText(e);
  } finally {
    loading.value = false;
  }
}

async function openAlbum(a) {
  opened.value = a;
  photos.value = [];
  photosTotal.value = 0;
  await morePhotos(true);
}

async function morePhotos(reset = false) {
  if (!opened.value) return;
  loading.value = true;
  try {
    const data = await listLocalPhotos({
      root_id: opened.value.root_id,
      rel: opened.value.rel,
      offset: reset ? 0 : photos.value.length,
      limit: 200,
    });
    photos.value = reset ? data.items : photos.value.concat(data.items);
    photosTotal.value = data.total;
  } catch (e) {
    errorMsg.value = errText(e);
  } finally {
    loading.value = false;
  }
}

async function loadFavorites() {
  loading.value = true;
  try {
    const data = await listLocalFavorites();
    favs.value = data.items || [];
    favStale.value = data.stale || [];
  } catch (e) {
    errorMsg.value = errText(e);
  } finally {
    loading.value = false;
  }
}

async function loadMemories() {
  loading.value = true;
  try {
    memories.value = await getLocalOnThisDay({ root_id: "", per_year: 6, limit: 60 });
  } catch (e) {
    errorMsg.value = errText(e);
  } finally {
    loading.value = false;
  }
}

async function findDuplicates() {
  if (!opened.value) return;
  dupLoading.value = true;
  dupes.value = null;
  try {
    dupes.value = await getLocalDuplicates({
      root_id: opened.value.root_id,
      rel: opened.value.rel,
    });
  } catch (e) {
    toast(errText(e), "err");
  } finally {
    dupLoading.value = false;
  }
}

// ⚠️ 库里存的是 **JSON 字符串**(后端按 TEXT 存), 不是一个数组。以为它是数组就会
// 在这里写下 `.join` 然后拿到 `undefined is not a function` —— 而且只在"编辑一个
// 已经配过规则的目录"时才发作, 第一次打开永远是空的。
function excludeLines(root) {
  const raw = root && root.exclude;
  if (!raw) return [];
  if (Array.isArray(raw)) return raw;
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [String(parsed)];
  } catch (e) {
    return String(raw).split("\n").map((s) => s.trim()).filter(Boolean);
  }
}

function openExclude(root) {
  editing.value = root;
  // 编辑框里给"一行一条" —— 人在输入框里最自然的写法; 后端两种形态都收。
  editText.value = excludeLines(root).join("\n");
}

async function saveExclude() {
  const root = editing.value;
  if (!root) return;
  const lines = editText.value
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
  busy.value = true;
  try {
    await updateLocalRootExclude(root.id, lines);
    editing.value = null;
    await refreshAll();
    toast(lines.length ? `已保存 ${lines.length} 条排除规则` : "已清空排除规则", "ok");
  } catch (e) {
    toast(errText(e), "err");
  } finally {
    busy.value = false;
  }
}

function refreshAll() {
  // ⚠️ 先清空再拉, 否则"切过去看到的是上一次 tab 的内容"会让人以为数据乱了。
  const jobs = [loadRoots()];
  if (tab.value === "random") jobs.push(loadWall());
  if (tab.value === "albums") jobs.push(loadAlbums());
  if (tab.value === "memories") jobs.push(loadMemories());
  if (tab.value === "favorites") jobs.push(loadFavorites());
  return Promise.all(jobs);
}

function switchTab(next) {
  tab.value = next;
  opened.value = null;
  dupes.value = null;
  errorMsg.value = "";
  refreshAll();
}

// ---- 目录管理 ----
async function pickRoot(path) {
  picker.value = false;
  if (!path) return;
  busy.value = true;
  try {
    await addLocalRoot(path);
    await refreshAll();
    toast(`已登记为相册集: ${path}`, "ok");
  } catch (e) {
    toast(errText(e), "err");
  } finally {
    busy.value = false;
  }
}

async function rescan(root) {
  busy.value = true;
  try {
    const r = await scanLocalRoot(root.id);
    await refreshAll();
    const idx = r.index;
    // ⚠️ 三件都要报: 排除了多少(证明规则生效了)、相对上一次多了/少了多少
    // (证明"有东西不见了"这件事能被看见, 而不是只表现为总数变小)、以及读不到的
    // 子目录(那与"这个目录是空的"长得一样)。
    const ex = idx.excluded || {};
    toast(
      `重新扫描完成: ${idx.albums} 个相册 / ${idx.photos} 张` +
        (ex.dirs || ex.files ? ` · 按规则跳过 ${ex.dirs || 0} 个目录 / ${ex.files || 0} 个文件` : "") +
        (idx.delta
          ? ` · 比上次 +${idx.delta.added} / -${idx.delta.removed}`
          : " · 没有可对比的上一次快照") +
        (idx.exclude_dropped ? ` · ${idx.exclude_dropped} 条规则超限被丢掉` : "") +
        (idx.truncated ? " · 已达张数上限, 只登记了前一部分" : "") +
        (idx.unreadable ? ` · ${idx.unreadable} 个子目录读不到` : ""),
      idx.error ? "err" : "ok"
    );
  } catch (e) {
    toast(errText(e), "err");
  } finally {
    busy.value = false;
  }
}

//: 二次确认要说清"不会删文件"。这个按钮如果让人以为会删照片, 就没人敢按;
//: 而如果它真的删照片, 那就更不该存在。
const forgetting = ref(null);
async function doForget() {
  const root = forgetting.value;
  forgetting.value = null;
  if (!root) return;
  try {
    await removeLocalRoot(root.id);
    await refreshAll();
    toast("已取消登记, 磁盘上的文件一个都没动", "ok");
  } catch (e) {
    toast(errText(e), "err");
  }
}

async function doPrune() {
  busy.value = true;
  try {
    const r = await pruneLocalThumbs();
    stats.value = Object.assign({}, stats.value, { thumb_cache: r.cache });
    toast(`清理了 ${r.removed} 个缩略图缓存(下次看时会重新生成)`, "ok");
  } catch (e) {
    toast(errText(e), "err");
  } finally {
    busy.value = false;
  }
}

// ---- 收藏 ----
async function toggleFav(p) {
  try {
    await setLocalFavorite(p.root_id, p.rel, !p.favorite);
    // 就地改标记: 重拉整页会让网格重新布局, 用户按星号时视线会跳。
    const target = (p.favorite ? "unset" : "set");
    for (const list of [wall.value, photos.value, favs.value]) {
      for (const item of list) {
        if (item.root_id === p.root_id && item.rel === p.rel) {
          item.favorite = target === "set";
        }
      }
    }
    if (tab.value === "favorites") await loadFavorites();
  } catch (e) {
    toast(errText(e), "err");
  }
}

async function forgetStale(item) {
  try {
    await forgetLocalFavorite(item.path);
    await loadFavorites();
    toast("已删掉这条收藏记录", "ok");
  } catch (e) {
    toast(errText(e), "err");
  }
}

// ---- 灯箱 / 幻灯片 ----
function openLb(index) {
  lb.value = { show: true, index: Math.max(0, index) };
}
function closeLb() {
  lb.value = { show: false, index: 0 };
  stopSlides();
}
function nextLb() {
  const n = lbList.value.length;
  if (n <= 0) return;
  lb.value = { show: true, index: (lb.value.index + 1) % n };
}
function startSlides() {
  if (slideTimer) return;
  playing.value = true;
  slideTimer = setInterval(nextLb, SLIDE_MS);
}
function stopSlides() {
  playing.value = false;
  if (slideTimer) {
    clearInterval(slideTimer);
    slideTimer = null;
  }
}
function toggleSlides() {
  if (playing.value) stopSlides();
  else startSlides();
}
// 空格 = 播放/暂停。放在**这一层**而不是灯箱里: 灯箱不知道"幻灯片"这回事,
// 而 `playing` 是这个面板的状态。
function onPanelKey(e) {
  if (e.key !== " " && e.code !== "Space") return;
  if (!lb.value.show) return;
  // ⚠️ 别抢输入框的空格: 在搜索框里打一个空格就把幻灯片开起来, 是键盘快捷键
  // 最典型的踩法。判据是"焦点在不在可输入元素上", 不是"当前 tab 是什么"。
  const t = e.target;
  const tag = ((t && t.tagName) || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select" || (t && t.isContentEditable)) {
    return;
  }
  e.preventDefault();
  toggleSlides();
}

// ⚠️ 离开组件必须清掉定时器与监听。不清的后果不是"报错", 而是**关掉面板之后后台
// 还在偷偷翻页并继续请求** —— 那种现象没人会归因到一个已经在屏幕上消失的组件上。
onMounted(() => {
  window.addEventListener("keydown", onPanelKey);
  refreshAll();
});
onUnmounted(() => {
  window.removeEventListener("keydown", onPanelKey);
  stopSlides();
});

function fmtBytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v >= 10 || i === 0 ? Math.round(v) : v.toFixed(1)} ${u[i]}`;
}
function fmtTime(ts) {
  if (!ts) return "未扫描";
  const d = new Date(ts * 1000);
  const p = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
// 给 title 用: 前缀是"哪个目录里的哪个相册", 用户排查"这是哪张"时唯一有用的信息。
function fullPath(p) {
  const root = p.root_path || "";
  const rel = p.rel || "";
  if (!rel) return root;
  return root ? `${root}/${rel}` : rel;
}

</script>

<template>
  <div class="card">
    <div class="list-bar">
      <span class="lbl">本地相册集</span>
      <span class="summary muted" v-if="stats">
        {{ stats.root_count }} 个目录 · {{ stats.album_count }} 个相册 ·
        {{ stats.photo_count }} 张照片
        <template v-if="stats.favorite_count"> · 收藏 {{ stats.favorite_count }}</template>
      </span>
      <span class="grow"></span>
      <button class="ghost" :disabled="busy" @click="picker = true">＋ 添加目录</button>
    </div>

    <p class="muted ro-note">
      <b>只读</b>:这里只登记"哪个目录是相册集",<b>不会改动、也不会删除</b>目录里的
      任何文件。缩略图缓存在本程序自己的数据目录里(当前 {{ fmtBytes(stats?.thumb_cache?.bytes) }})。
    </p>

    <!-- 已登记的目录 -->
    <div class="roots">
      <div v-for="r in roots" :key="r.id" class="root-chip" :class="{ bad: r.error }">
        <span class="nm" :title="r.path">{{ r.name || r.path }}</span>
        <span class="muted mini">{{ r.albums }} 册 / {{ r.photos }} 张</span>
        <span class="muted mini">{{ fmtTime(r.last_scan) }}</span>
        <span class="err mini" v-if="r.error" :title="r.error">读不到</span>
        <span class="grow"></span>
        <button class="ghost mini" :disabled="busy" @click="rescan(r)">重新扫描</button>
        <button class="ghost mini" :disabled="busy" @click="openExclude(r)">排除</button>
        <button class="ghost mini" @click="forgetting = r">忘记</button>
      </div>
      <div v-if="!roots.length" class="empty">
        还没有登记任何目录。点右上角「添加目录」,把一个装着照片的文件夹加进来。
      </div>
    </div>

    <div class="view-switch local-tabs" v-if="roots.length">
      <button class="vtab" :class="{ on: tab === 'random' }" @click="switchTab('random')">随机</button>
      <button class="vtab" :class="{ on: tab === 'albums' }" @click="switchTab('albums')">相册</button>
      <button class="vtab" :class="{ on: tab === 'memories' }" @click="switchTab('memories')">
        往年今日
      </button>
      <button class="vtab" :class="{ on: tab === 'favorites' }" @click="switchTab('favorites')">收藏</button>
    </div>

    <div class="error-box" v-if="errorMsg">{{ errorMsg }}</div>

    <!-- ============ 随机 ============ -->
    <div v-if="roots.length && tab === 'random'">
      <div class="frow">
        <label>抽多少</label>
        <select v-model.number="wallOpts.count">
          <option :value="30">30</option>
          <option :value="60">60</option>
          <option :value="120">120</option>
          <option :value="200">200</option>
        </select>
        <label>取向</label>
        <select v-model="wallOpts.mode">
          <option value="album">各相册机会均等</option>
          <option value="photo">每张机会均等</option>
        </select>
        <label>忽略小于</label>
        <input class="num" type="number" min="0" step="16" v-model.number="wallOpts.min_kb" />
        <span class="tip">KB</span>
        <label class="chk">
          <input type="checkbox" v-model="wallOpts.favorites_only" />
          只要收藏
        </label>
        <span class="grow"></span>
        <button class="ghost" :disabled="loading" @click="loadWall(false)">换一批</button>
        <button class="ghost" :disabled="loading" @click="loadWall(true)">更多</button>
      </div>
      <p class="muted mini">
        从 {{ wallMeta.pool }} 张里抽 ·
        <template v-if="wallMeta.at">索引快照 {{ fmtTime(wallMeta.at) }}</template>
        <template v-else>收藏按磁盘现状</template>
        <template v-if="wallMeta.truncated"> · 目录超出张数上限, 只登记了前一部分</template>
        <template v-if="wallOpts.mode === 'album'">
          · 同一副牌里各页不重复(「更多」是翻下一页,「换一批」才重洗)
        </template>
      </p>
      <div class="resource-grid" v-if="wall.length">
        <div v-for="(p, i) in wall" :key="`${p.root_id}|${p.rel}`" class="resource-item">
          <img :src="p.thumb_url" :alt="p.name" loading="lazy" @click="openLb(i)" />
          <div class="meta">
            <div class="name" :title="`${p.root_name} / ${p.album_name} / ${p.name}`">{{ p.name }}</div>
            <div>
              {{ fmtBytes(p.size) }}
              <span class="mini muted">{{ p.album_name }}</span>
            </div>
          </div>
          <button class="fav" :class="{ on: p.favorite }" :title="p.favorite ? '取消收藏' : '收藏'"
                  @click.stop="toggleFav(p)">★</button>
        </div>
      </div>
      <div v-else class="empty">这个条件下没有照片。</div>
    </div>

    <!-- ============ 相册 ============ -->
    <div v-if="roots.length && tab === 'albums' && !opened">
      <div class="frow">
        <input type="text" placeholder="搜相册名" v-model="albumOpts.q" @keyup.enter="loadAlbums" />
        <label>排序</label>
        <select v-model="albumOpts.sort" @change="loadAlbums">
          <option v-for="s in SORTS" :key="s.key" :value="s.key">{{ s.label }}</option>
        </select>
        <select v-model="albumOpts.order" @change="loadAlbums">
          <option value="desc">降序</option>
          <option value="asc">升序</option>
        </select>
        <button class="ghost" @click="loadAlbums">搜索</button>
        <span class="grow"></span>
        <button class="ghost" :disabled="loading" @click="loadAlbums">刷新</button>
      </div>
      <p class="muted mini">
        每个「直接装着照片的目录」算一个相册;里面的子目录是另外的相册(不会混在一起)。
      </p>
      <div class="album-grid" v-if="albums.length">
        <div v-for="a in albums" :key="`${a.root_id}|${a.rel}`" class="album-card"
             @click="openAlbum(a)">
          <img v-if="a.cover_thumb_url" :src="a.cover_thumb_url" :alt="a.name" loading="lazy" />
          <div v-else class="no-cover">📁</div>
          <div class="album-meta">
            <div class="name" :title="fullPath(a)">{{ a.name }}</div>
            <div class="mini muted">
              {{ a.photos }} 张 · {{ fmtBytes(a.bytes) }}
              <template v-if="a.root_name"> · {{ a.root_name }}</template>
            </div>
          </div>
        </div>
      </div>
      <div v-else class="empty">没有匹配的相册。</div>
    </div>

    <!-- ============ 打开某个相册 ============ -->
    <div v-if="opened">
      <div class="frow">
        <button class="ghost" @click="opened = null">← 返回相册</button>
        <span class="lbl">{{ opened.name }}</span>
        <span class="muted mini">
          {{ opened.root_name }} · 已加载 {{ photos.length }} / {{ photosTotal }} 张
        </span>
        <span class="grow"></span>
        <button class="ghost" :disabled="dupLoading" @click="findDuplicates">
          {{ dupLoading ? "正在比对…" : "查重复" }}
        </button>
        <button class="ghost" v-if="playing" @click="stopSlides">停止播放</button>
        <button class="ghost" v-else :disabled="!photos.length" @click="openLb(0); startSlides()">
          幻灯片
        </button>
      </div>

      <!-- 查重复的结果。**只标记, 不删任何文件** —— 文案必须说清这一点。 -->
      <div v-if="dupes" class="dupe-block">
        <div class="lbl">查重复的结果</div>
        <p v-if="dupes.reason === 'no-decoder'" class="err mini">
          <b>这一轮没有真的比对</b> —— 本机没有找到 ffmpeg, 一张指纹都没算出来。
          这不是"没有重复", 是<b>没验过</b>。装好 ffmpeg 再查一次才有结论。
        </p>
        <template v-else>
          <p class="muted mini">
            比对了 {{ dupes.scanned }} 张
            <template v-if="dupes.skipped">
              (超出单次上限, 还有 {{ dupes.skipped }} 张没比)
            </template>
            <template v-if="dupes.undecodable">
              · {{ dupes.undecodable }} 张解不开, 未参与比对
            </template>
            <template v-if="dupes.flat">
              · {{ dupes.flat }} 张是纯色/无内容的图, 指纹对它们没有意义, 已排除
            </template>
            · 阈值 {{ dupes.threshold }} 位
          </p>
          <div v-if="dupes.pairs.length">
            <div v-for="(pr, i) in dupes.pairs" :key="i" class="dupe-pair">
              <div class="dupe-cell">
                <img :src="pr.a.thumb_url" :alt="pr.a.name" loading="lazy" />
                <div class="mini">{{ pr.a.name }}</div>
              </div>
              <div class="dupe-mid">
                <span class="mini muted">差 {{ pr.distance }} 位</span>
              </div>
              <div class="dupe-cell">
                <img :src="pr.b.thumb_url" :alt="pr.b.name" loading="lazy" />
                <div class="mini">{{ pr.b.name }}</div>
              </div>
            </div>
            <p class="muted mini">
              只列出来, <b>没有动过任何文件</b> —— 指纹会误判(连拍、纯色图都可能
              撞车), 删哪张由你决定。
            </p>
          </div>
          <div v-else class="muted mini">
            这个范围里没有找到疑似重复 —— 这次是真的比过了。
          </div>
        </template>
      </div>
      <div class="resource-grid" v-if="photos.length">
        <div v-for="(p, i) in photos" :key="p.rel" class="resource-item">
          <img :src="p.thumb_url" :alt="p.name" loading="lazy" @click="openLb(i)" />
          <div class="meta">
            <div class="name" :title="p.name">{{ p.name }}</div>
            <div>{{ fmtBytes(p.size) }}</div>
          </div>
          <button class="fav" :class="{ on: p.favorite }" @click.stop="toggleFav(p)">★</button>
        </div>
      </div>
      <div v-else class="empty">这个相册里没有照片了(可能刚被移走)。</div>
      <div class="frow" v-if="photos.length < photosTotal">
        <button class="ghost" :disabled="loading" @click="morePhotos(false)">
          加载更多(还有 {{ photosTotal - photos.length }} 张)
        </button>
      </div>
      <p class="muted mini" v-if="photos.length">
        这一页是<b>实时</b>列目录的结果 —— 看得见的一定打得开。
      </p>
    </div>

    <!-- ============ 往年今日 ============ -->
    <div v-if="roots.length && tab === 'memories'">
      <p class="muted mini">
        同月同日、但不是今年的照片, 按年份分组。
        <b>口径是文件的修改时间, 不是拍摄时间</b> —— 复制或重新导出会刷新它,
        所以整批导入的照片会挤在同一天。
      </p>
      <div v-if="memories && memories.years.length">
        <div v-for="g in memories.years" :key="g.year" class="mem-year">
          <div class="lbl">{{ g.year }} 年 · {{ g.age }} 年前</div>
          <div class="resource-grid">
            <div
              v-for="(p, i) in g.photos"
              :key="`${p.root_id}|${p.rel}`"
              class="resource-item"
            >
              <img
                :src="p.thumb_url"
                :alt="p.name"
                loading="lazy"
                @click="openLb(i)"
              />
              <div class="meta">
                <div class="name" :title="`${p.root_name} / ${p.album} / ${p.name}`">
                  {{ p.name }}
                </div>
                <div>{{ fmtBytes(p.size) }}</div>
              </div>
            </div>
          </div>
        </div>
        <p class="muted mini">
          同一天里反复打开, 看到的是同一批 —— 这是刻意的(每次都换一批就没有"回忆"了)。
        </p>
      </div>
      <div v-else class="empty">
        今天没有往年的照片。这个结果本身就是答案: 没有过滤条件被忽略, 也没有
        任何东西被藏起来 —— 只是 {{ memories ? memories.date : "" }} 那天没有。
      </div>
    </div>

    <!-- ============ 收藏 ============ -->
    <div v-if="roots.length && tab === 'favorites'">
      <div class="resource-grid" v-if="favs.length">
        <div v-for="(p, i) in favs" :key="`${p.root_id}|${p.rel}`" class="resource-item">
          <img :src="p.thumb_url" :alt="p.name" loading="lazy" @click="openLb(i)" />
          <div class="meta">
            <div class="name" :title="`${p.root_name} / ${p.album_name} / ${p.name}`">{{ p.name }}</div>
            <div>{{ fmtBytes(p.size) }} <span class="mini muted">{{ p.album_name }}</span></div>
          </div>
          <button class="fav on" title="取消收藏" @click.stop="toggleFav(p)">★</button>
        </div>
      </div>
      <div v-else class="empty">还没有收藏。在照片右下角点一下★就行。</div>

      <div v-if="favStale.length" class="stale-block">
        <div class="lbl">已失效({{ favStale.length }})</div>
        <p class="muted mini">
          这些收藏指向的照片现在取不到了。<b>它们仍然列在这里, 而不是被悄悄丢掉</b> ——
          否则你只会看到收藏数变少, 却不知道少了谁。
        </p>
        <div v-for="s in favStale" :key="s.path" class="root-chip bad">
          <span class="nm" :title="s.path">{{ s.path }}</span>
          <span class="err mini">
            {{ s.reason === 'unknown-root' ? '所在目录已不再登记'
               : s.reason === 'not-image' ? '不是图片'
               : '文件已不在' }}
          </span>
          <span class="grow"></span>
          <button class="ghost mini" @click="forgetStale(s)">删掉这条收藏</button>
        </div>
      </div>
    </div>

    <!-- 取消登记的二次确认 -->
    <div v-if="forgetting" class="modal-mask" @click.self="forgetting = null">
      <div class="modal">
        <h3>忘记这个目录?</h3>
        <p class="muted">
          <code>{{ forgetting.path }}</code>
        </p>
        <p class="muted">
          只会删掉这条登记记录。<b>磁盘上的文件一个都不会动</b> ——
          照片、子目录、任何东西都保持原样。
        </p>
        <p class="muted mini">收藏记录也会保留(重新登记这个目录后它们就回来了)。</p>
        <div class="modal-foot">
          <span class="grow"></span>
          <button class="ghost" @click="forgetting = null">取消</button>
          <button @click="doForget">忘记</button>
        </div>
      </div>
    </div>

    <div class="frow" v-if="roots.length">
      <span class="grow"></span>
      <button class="ghost mini" :disabled="busy || !stats?.thumb_cache?.files" @click="doPrune">
        清理缩略图缓存({{ stats?.thumb_cache?.files || 0 }} 个文件)
      </button>
    </div>

    <!-- 排除规则。⚠️ 一定要把"跳过了多少"显示出来: 一条写错的规则**什么都不排除**,
         而界面上唯一能证明它生效过的就是这个数字 —— 不显示, 就没有任何东西能让你
         发现规则是失效的。 -->
    <div v-if="editing" class="modal-mask" @click.self="editing = null">
      <div class="modal">
        <h3>排除规则</h3>
        <p class="muted">
          <code>{{ editing.path }}</code>
        </p>
        <p class="muted mini">
          一行一条, 用 glob(<code>*</code> 匹配任意字符)。写目录名就能排除整个目录:
          <code>Raw</code>、<code>*_edited*</code>、<code>2024/不要</code>。
        </p>
        <textarea v-model="editText" rows="6" class="exc-input" spellcheck="false"></textarea>
        <p class="muted mini">
          保存后<b>下一次扫描</b>生效。点「重新扫描」, 完成后的提示会告诉你按规则
          跳过了多少 —— <b>那个数字是判断规则有没有生效的唯一依据</b>(写错的规则
          什么都不排除, 而结果看起来和"没有规则"一模一样)。
        </p>
        <div class="modal-foot">
          <span class="grow"></span>
          <button class="ghost" @click="editing = null">取消</button>
          <button :disabled="busy" @click="saveExclude">保存</button>
        </div>
      </div>
    </div>

    <FolderPicker
      :show="picker"
      title="选择相册集目录"
      hint="把这个目录登记为相册集。里面的照片以只读方式浏览, 不会改动任何文件。"
      @select="pickRoot"
      @close="picker = false"
    />

    <Lightbox
      v-if="lb.show && lbImages.length"
      :images="lbImages"
      :index="lb.index"
      @close="closeLb"
      @update:index="lb.index = $event"
    />
  </div>
</template>

<style scoped>
/* `.list-bar` 在 App.vue 里是 scoped(只作用于它自己的模板), 所以这里自带一份。
   它是四行 flex, 不值得为它建一套全局类。 */
.list-bar { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
.list-bar .lbl { color: var(--muted); font-size: 13px; }
.list-bar .summary { color: var(--muted); font-size: 12px; }
.list-bar .grow { flex: 1; }
.ro-note { font-size: 12px; margin: -4px 0 10px; line-height: 1.6; }
.local-tabs { margin: 12px 0 10px; }
.roots { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 4px; }
.root-chip {
  display: flex; align-items: center; gap: 8px;
  border: 1px solid var(--border); border-radius: 8px;
  padding: 6px 10px; background: var(--panel-2); font-size: 12px; max-width: 100%;
}
.root-chip.bad { border-color: rgba(224, 92, 92, .45); }
.root-chip .nm {
  color: var(--text); max-width: 320px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.err { color: var(--err); }
.mini { font-size: 11px; }
.grow { flex: 1; }
.chk { display: inline-flex; align-items: center; gap: 4px; }
input.num { flex: 0 0 72px; min-width: 0; }

/* 相册卡片比照片卡大: 封面要能看清是哪个相册 */
.album-grid {
  display: grid; gap: 10px;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
}
.album-card {
  background: var(--panel-2); border: 1px solid var(--border); border-radius: 8px;
  overflow: hidden; cursor: pointer; transition: border-color .15s;
}
.album-card:hover { border-color: var(--accent); }
.album-card img { width: 100%; height: 120px; object-fit: cover; display: block; }
.album-card .no-cover {
  height: 120px; display: flex; align-items: center; justify-content: center; font-size: 30px;
  color: var(--muted);
}
.album-meta { padding: 6px 8px; font-size: 12px; }
.album-meta .name {
  color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

/* 星标压在缩略图右下角: 网格里的每一项都要能收藏, 但不能挤掉文件名 */
.resource-item { position: relative; }
.resource-item .fav {
  position: absolute; right: 4px; top: 4px;
  background: rgba(0, 0, 0, .45); border: 0; color: #8b96a2;
  border-radius: 6px; padding: 2px 6px; cursor: pointer; font-size: 13px;
}
.resource-item .fav.on { color: #e0a458; }

.stale-block { margin-top: 16px; display: grid; gap: 8px; }

/* 往年今日: 一年一组, 组与组之间要能看出"这是另一年" */
.mem-year { margin-top: 14px; display: grid; gap: 6px; }
.mem-year .lbl { font-weight: 600; }

/* 查重复: 两张并排, 中间写距离。⚠️ 绝不提供"删除"按钮 —— 这个功能只标记。 */
.dupe-block {
  margin: 12px 0; padding: 10px 12px; display: grid; gap: 8px;
  border: 1px solid var(--border, #2b3440); border-radius: 8px;
}
.dupe-pair { display: flex; align-items: center; gap: 10px; }
.dupe-cell { display: grid; gap: 4px; justify-items: center; max-width: 140px; }
.dupe-cell img {
  width: 120px; height: 90px; object-fit: cover;
  border-radius: 6px; background: #161b22;
}
.dupe-cell .mini { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 130px; }
.dupe-mid { color: var(--muted, #8b96a2); }

/* 排除规则输入框: 等宽字体, 因为写的是 glob 模式 */
.exc-input {
  width: 100%; box-sizing: border-box; font-family: ui-monospace, Consolas, monospace;
  background: var(--bg, #0d1117); color: var(--text, #e6edf3);
  border: 1px solid var(--border, #2b3440); border-radius: 6px; padding: 8px;
}
</style>
