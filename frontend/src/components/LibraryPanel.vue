<script setup>
// 跨任务资源库: 回答"我手上已经有什么"。
//
// 与任务详情里的资源列表是**两个视角**: 那个是"这个任务采到了什么", 这个是
// "整个下载目录里有什么"。同一张图被 sha256 去重复用时会在多处出现, 这里
// 各列一条并给出引用数(refs) —— 让人知道"删这个文件会不会影响另一个任务"。
import { computed, onMounted, ref } from "vue";
import {
  clearUrlArchive,
  deleteLibrarySearch,
  exportLibrary,
  exportUrlArchive,
  extractLibraryColors,
  getLibraryFacets,
  importUrlArchive,
  libraryArchiveUrl,
  libraryBulkDelete,
  libraryEditTags,
  libraryNormalize,
  libraryRate,
  librarySetFavorite,
  libraryVerify,
  listLibrary,
  listLibraryAlbums,
  listLibraryDuplicates,
  listLibraryFailures,
  listLibrarySearches,
  listLibrarySimilar,
  listLibraryTags,
  listVirtualAlbumItems,
  replayLibraryFailures,
  saveLibrarySearch,
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
// 卡片徽章的中文(missing / corrupt / mismatch)。由 `/library` 下发, 前端
// **不自己编** —— 否则后端加一类, 界面会静默显示成代号(第 9 条)。
const errorKindLabels = ref({});

const query = ref({
  q: "",
  kind: "all",
  album: "",
  tag: "",
  // 含子标签: 层级是名字前缀, 这是一次前缀匹配。默认**关** —— 精确匹配才是
  // "我就要这一个标签"时唯一正确的语义, 含子标签是额外的便利。
  tag_children: false,
  favorite: false,
  // V42: 星级下限(0 = 不限)与整理型维度(取值由 /library/facets 下发)
  min_rating: 0,
  special: "",
  // V44: 主色系筛选。色系由 /library/facets 的 colors 下发(键 + 中文名 + 代表色),
  // 前端不硬编码 —— 后端加一个色系时界面自动多一项。
  color: "",
  // V43: 排序。档位代号由 /library/facets 的 sorts 下发 —— 前端不硬编码,
  // 后端加一档时界面自动多一项; 未知代号后端会回 400(不静默忽略)。
  sort: "",
  order: "desc",
  // ---- V45: 排除筛选 ----
  // 与正向筛选**各占一个字段**, 不做成"同一个下拉里选 包含/排除" —— 那样
  // "包含 A 且排除 B"这种最常见组合就没法表达了(那是整理时真正的用法)。
  exclude_tag: "",
  exclude_tag_children: false,
  exclude_album: "",
  exclude_kind: "",
  exclude_special: "",
  exclude_color: "",
  // ---- V45: 落盘日期区间 ----
  // ⚠️ 口径是**落盘时间**(created_time), 不是拍摄时间 —— 我们没有 EXIF。
  // 形态必须 YYYY-MM-DD: 别的一律后端 400, 前端不做自己的宽松解析(两套规则
  // 迟早不一致, 而"宽松"的那一侧会静默接受 2026-13-40 这种输入)。
  date_from: "",
  date_to: "",
  page: 1,
  page_size: 40,
});
// 后端回灌的"这次真正生效的条件"(第 27 条: 规则生效要有计数/证据)。
// ⚠️ 用它来显示而不是拿 query 反推 —— query 里有 kind=all 这种"没筛"的值,
// 照着显示会让人以为"按类型筛过了"。
const applied = ref([]);
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

// 卡片上值得挂徽章的 error_kind。⚠️ 写死**这三个**而不是"非空就显示":
// resources 上的 error_kind 还有 http-4xx / network 等**下载期**原因, 那些已经
// 由 status=failed 表达过一遍, 再挂一枚徽章等于同一件事说两遍。
const BADGED_KINDS = ["missing", "corrupt", "mismatch"];

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
      // 措辞刻意区分三类: 缺失(文件没了) / 截断(文件还在但可能看不全) /
      // 不符(下来的是别的东西, 多半是错误页)。处置方式不同 —— 前两类重下有救,
      // 第三类重下还是它 —— 合成一句"发现 N 个问题"就丢掉了这个区别。
      toast(
        `检查 ${r.checked} 项: 缺失 ${r.missing} 项, 疑似截断 ${r.truncated} 项, ` +
          `名字与内容不符 ${r.mismatched || 0} 项`,
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

// ---- V42: 体检视图 / 评分 / 重复分组 / URL 归档 ----
// 这四件事的共同点: 它们回答的都是"库**现在的状态**怎么样", 而不是"某个字段
// 等于多少"。这类问题在字段级筛选里没有入口 —— 指望用户自己拼出"没打标签 AND
// 类型是图片"是不现实的(TagStudio 用 `special:` 语法、Czkawka 做成十种扫描模式,
// 说的是同一件事)。所以它们各自成项, 每一项都带**计数**。
const facets = ref(null);
const facetsBusy = ref(false);

async function loadFacets() {
  if (facetsBusy.value) return;
  facetsBusy.value = true;
  try {
    facets.value = await getLibraryFacets();
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  } finally {
    facetsBusy.value = false;
  }
}
function toggleFacets() {
  if (facets.value) {
    facets.value = null;
    return;
  }
  loadFacets();
}
// 点一个维度 = 按它筛选; 再点一次 = 取消。
// ⚠️ n === 0 时**仍然可点**(点了会看到空列表并明确写着"没有这类"), 不做成禁用 ——
// 禁用会让人以为是"还没算出来", 而 0 是后端真数过的结论(第 26 条)。
function pickSpecial(key) {
  query.value.special = query.value.special === key ? "" : key;
  search();
}
// 星级筛选。点当前档位再点一次 = 取消(与标签同一套手感)
function pickRating(n) {
  query.value.min_rating = query.value.min_rating === n ? 0 : n;
  search();
}
function clearOrganize() {
  query.value.special = "";
  query.value.min_rating = 0;
  search();
}

// ---- V45: 排除筛选 + 落盘日期区间 ----
// 这两组控件默认收起: 它们不是日常入口, 摊开会把"找东西"那一行挤成两行。
// 但收起不等于没有 —— 收起时若条件非空, 生效条件条里照样显示(见 org-bar)。
const advOpen = ref(false);
function clearAdvanced() {
  query.value.exclude_tag = "";
  query.value.exclude_tag_children = false;
  query.value.exclude_album = "";
  query.value.exclude_kind = "";
  query.value.exclude_special = "";
  query.value.exclude_color = "";
  query.value.date_from = "";
  query.value.date_to = "";
  search();
}
// 日期用原生 <input type="date">: 它给的就是 YYYY-MM-DD, 与后端正则同一形态。
// ⚠️ 不自己写文本框 + 解析 —— 手输的 "2026/3/5" 会被后端 400 拒掉, 而那不算
// bug(宽松接受输入才是: 今天能被接受、明天换个格式就查不到东西)。
const hasAdvanced = computed(
  () =>
    !!query.value.exclude_tag ||
    !!query.value.exclude_album ||
    !!query.value.exclude_kind ||
    !!query.value.exclude_special ||
    !!query.value.exclude_color ||
    !!query.value.date_from ||
    !!query.value.date_to
);

// ---- V45: 找相似(感知指纹汉明距离) ----
// ⚠️ 与"疑似重复"是**两个阈值**(重复 ≤4 / 相似 ≤12), 界面上分成两个入口:
// 相似里混着"构图像但不是同一张"的图, 当成重复删掉就真丢了东西。
const similarData = ref(null);
const similarBusy = ref(false);
const SIMILAR_DISTANCES = [6, 12, 20];
async function openSimilar(r, maxDistance = 12) {
  similarBusy.value = true;
  try {
    const d = await listLibrarySimilar(r.id, { max_distance: maxDistance, limit: 24 });
    // 把"被查的那一张"也带上 —— 结果区里没有它的话, 用户不知道在跟谁比。
    d.origin = r;
    similarData.value = d;
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  } finally {
    similarBusy.value = false;
  }
}
function closeSimilar() {
  similarData.value = null;
}
// ⚠️ ok=false 时的 reason 是**代号**, 中文由同一份响应的 reason_label 给。
// 前端不维护映射表(第 9 条), 也不把"没法比"显示成"没有相似的"。
function similarReason(d) {
  return d?.reason_label || d?.reason || "";
}

// ---- V43: 排序 + 保存的搜索(智能文件夹) ----
// 排序档位由后端在 `/library` 与 `/library/facets` 两处**同一份定义**下发;
// 前端只拿 key 与中文名, 不认识任何列名。未知代号后端回 400 而不是静默按默认
// 排序 —— 否则"排序没生效"和"本来就是这个顺序"长得一模一样。
const sortOptions = ref([]);
const defaultSort = ref("added");
const sortOpen = ref(false);
function pickSort(key) {
  // 再点当前档位 = 回到默认(与标签/星级的"再点一次取消"同一套手感)
  query.value.sort = query.value.sort === key ? "" : key;
  sortOpen.value = false;
  search();
}
function toggleOrder() {
  query.value.order = query.value.order === "asc" ? "desc" : "asc";
  search();
}
const currentSortLabel = computed(() => {
  const k = query.value.sort || defaultSort.value;
  return (sortOptions.value.find((s) => s.key === k) || {}).label || k;
});

const searches = ref([]);
const searchName = ref("");
const searchBusy = ref(false);
// 当前正在看的是哪个保存的搜索。⚠️ 一旦用户手动改了任何筛选条件就必须清掉,
// 否则侧栏会一直高亮"搜索 A"而列表其实已经是别的东西 —— 这种不一致不报错,
// 只会让人以为自己记错了。
const activeSearch = ref(null);

async function loadSearches() {
  searchBusy.value = true;
  try {
    searches.value = await listLibrarySearches();
  } catch (e) {
    searches.value = [];
  } finally {
    searchBusy.value = false;
  }
}
async function saveCurrentSearch() {
  const name = (searchName.value || "").trim();
  if (!name) {
    toast("先给这个搜索起个名字", "warn");
    return;
  }
  try {
    const r = await saveLibrarySearch(name, currentParams());
    // "新建"与"覆盖"要说清: 同名覆盖是常态, 但用户得知道刚才那一条被换掉了。
    toast(r.created ? `已保存「${r.name}」` : `已覆盖同名的「${r.name}」`, "ok");
    searchName.value = "";
    await loadSearches();
    activeSearch.value = r.id;
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  }
}
function applySearch(s) {
  const p = s.params || {};
  // ⚠️ **整体替换**而不是逐项合并: 侧栏上显示的命中数是按"存下来的那组条件"
  // 算的, 若带着上一轮的残留条件去查, 列表条数会和那个数字对不上 —— 而这种
  // 偏差不报错, 只表现为"这个搜索的数好像不太准"。
  query.value.q = p.q || "";
  query.value.kind = p.kind || "all";
  query.value.album = p.album || "";
  query.value.tag = p.tag || "";
  query.value.tag_children = !!p.tag_children;
  query.value.favorite = !!p.favorite;
  query.value.min_rating = p.min_rating || 0;
  query.value.special = p.special || "";
  // V45: 排除与日期。**整体替换**的一部分 —— 少清一项就会带着上一个搜索的
  // 排除条件, 列表条数与侧栏命中数对不上而没有任何报错。
  query.value.exclude_tag = p.exclude_tag || "";
  query.value.exclude_tag_children = !!p.exclude_tag_children;
  query.value.exclude_album = p.exclude_album || "";
  query.value.exclude_kind = p.exclude_kind || "";
  query.value.exclude_special = p.exclude_special || "";
  query.value.exclude_color = p.exclude_color || "";
  query.value.date_from = p.date_from || "";
  query.value.date_to = p.date_to || "";
  query.value.page = 1;
  activeSearch.value = s.id;
  load();
}
async function removeSearch(s) {
  try {
    const r = await deleteLibrarySearch(s.id);
    if (activeSearch.value === s.id) activeSearch.value = null;
    toast(r.deleted ? `已删除「${s.name}」` : "这条搜索已经不在了", "ok");
    await loadSearches();
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  }
}

// 打星。`rating=0` = 清除评分(回到"未评分"), 不是"打 0 分" —— 提示文案要说清,
// 否则用户以为自己"评了 0 分"而界面看不出差别。
async function rate(ids, value) {
  if (!ids.length) return;
  try {
    const r = await libraryRate(ids, value);
    toast(
      value ? `已给 ${r.updated} 项打 ${value} 星` : `已清除 ${r.updated} 项的评分`,
      "ok"
    );
    await load();
    if (facets.value) loadFacets();
  } catch (e) {
    // 越界由后端拦(400), 原样透出 —— 换成"操作失败"用户不知道该点几颗
    toast(e.response?.data?.detail || String(e), "err");
  }
}
function rateOne(r, value) {
  // 再点同一颗 = 清除。否则"手滑点错了"没有回退路径(只能去批量栏找清除)。
  rate([r.id], r.rating === value ? 0 : value);
}
// 批量栏里的打星: 本页选中项全是同一档时, 再点那档 = 清除
const selRating = computed(() => {
  const onPage = items.value.filter((r) => selected.value.has(r.id));
  if (!onPage.length) return 0;
  const first = Number(onPage[0].rating || 0);
  return onPage.every((r) => Number(r.rating || 0) === first) ? first : 0;
});
const RATE_OPTS = [1, 2, 3, 4, 5];

// 疑似重复分组。**只标记不删**是本产品的一贯原则(dHash 会误判 —— 纯色图互相
// 距离 0), 所以这里默认只展示; 真要清理由用户确认, 且默认只删记录不动文件。
const dupData = ref(null);
const dupBusy = ref(false);

// ---- V44: 主色检索 ----
// ⚠️ `written` 与 `checked` 两个数字都要显示: 只说"成功"的话, "一张都没提"
// 既可能是库里没图、也可能是 ffmpeg 不在(后端那时回 501) —— 两者必须能分开看。
const busyColor = ref(false);
async function runExtractColors() {
  busyColor.value = true;
  try {
    const r = await extractLibraryColors(500, true);
    toast(`提取主色: 写入 ${r.written} / 检查 ${r.checked}`, "ok");
    await load();
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  } finally {
    busyColor.value = false;
  }
}

// ---- V44: 打包导出 ----
// 导出的是**当前相册**(没选相册就是空条件 -> 后端会要求给 album 或 ids)。
const busyExport = ref(false);
async function runExport() {
  busyExport.value = true;
  try {
    const r = await exportLibrary({ album: query.value.album || null, ids: [] });
    const skip = r.skipped?.length ? `, 跳过 ${r.skipped.length} 个` : "";
    toast(`已打包 ${r.count} 个文件 -> ${r.name}${skip}`, "ok");
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  } finally {
    busyExport.value = false;
  }
}

// ---- V44: 文件头规范化 ----
// ⚠️ **默认 dry-run**: 改名落到用户磁盘上且不可逆。所以这里第一步只出计划,
// 让用户看清"要改成什么"之后才执行 —— 不做"点了就直接改"。
const busyNorm = ref(false);
async function runNormalize() {
  busyNorm.value = true;
  try {
    const plan = await libraryNormalize({ ids: [], dryRun: true });
    if (!plan.changed?.length) {
      toast(`检查了 ${plan.checked} 个: 没有名字与内容不符的`, "ok");
      return;
    }
    const sample = plan.changed[0];
    const ok = confirm(
      `有 ${plan.changed.length} 个文件的扩展名与文件头不符。\n` +
        `例如: ${sample.from} → ${sample.to}\n\n` +
        `只换扩展名、不动主名。确定改名?`
    );
    if (!ok) return;
    const done = await libraryNormalize({ ids: [], dryRun: false });
    toast(`已改名 ${done.changed.length} 个`, "ok");
    await load();
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  } finally {
    busyNorm.value = false;
  }
}

async function loadDuplicates() {
  if (dupBusy.value) return;
  dupBusy.value = true;
  try {
    dupData.value = await listLibraryDuplicates(50);
    if (!dupData.value.groups.length) toast("没有疑似重复的资源", "ok");
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  } finally {
    dupBusy.value = false;
  }
}
// keep_reason 是**代号**, 中文从 reasons 里查 —— 前端不硬编码(第 9 条)
function keepLabel(key) {
  return (
    dupData.value?.reasons.find((x) => x.key === key)?.label || key || "最早"
  );
}
// 只保留建议项: 删掉同组里其它**记录**, 文件不动。
// ⚠️ 必须确认且说清"不动文件": 用户看到"只保留"三个字时默认理解可能是"删文件"。
async function keepOnly(g) {
  const others = (g.members || []).filter((m) => m.id !== g.keep_id);
  if (!others.length) return;
  if (
    !confirm(
      `这一组共 ${g.n} 条, 将删除其余 ${others.length} 条资源库记录。\n\n` +
        `磁盘上的文件**不会**被删除(判据可能误报, 删文件不可逆)。确认继续?`
    )
  )
    return;
  try {
    const r = await libraryBulkDelete(
      others.map((m) => m.id),
      false
    );
    toast(`已删除 ${r.deleted} 条记录, 文件保留`, "ok");
    await loadDuplicates();
    await load();
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  }
}

// URL 归档: 换机器 / 重装之后让增量还能认出"这个我下过"。
// ⚠️ 导入结果把 added / skipped 两个数**都显示** —— 只说"导入成功"的话, 用户
// 没法判断这份归档是不是真被吃进去了(第 27 条)。
const archiveText = ref("");
const archiveBusy = ref(false);
const archiveResult = ref(null);
const archiveOpen = ref(false);

async function importArchive() {
  if (!archiveText.value.trim()) {
    toast("先粘贴归档内容(每行一条 URL)", "warn");
    return;
  }
  archiveBusy.value = true;
  try {
    const r = await importUrlArchive(archiveText.value);
    archiveResult.value = r;
    toast(`导入完成: 新增 ${r.added} 条, 已存在 ${r.skipped} 条, 共 ${r.total} 条`, "ok");
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  } finally {
    archiveBusy.value = false;
  }
}
async function exportArchive() {
  archiveBusy.value = true;
  try {
    const r = await exportUrlArchive();
    if (!r.urls.length) {
      toast("归档还是空的", "warn");
      return;
    }
    // 归档的本体是**纯文本** —— 它的全部价值就在于"能被带到另一台机器上",
    // 所以这里直接落成一个 .txt 文件, 而不是只在页面里显示一段 JSON。
    const blob = new Blob([r.urls.join("\n") + "\n"], { type: "text/plain" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `download-archive-${r.total}.txt`;
    a.click();
    URL.revokeObjectURL(a.href);
    toast(`已导出 ${r.total} 条 URL`, "ok");
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  } finally {
    archiveBusy.value = false;
  }
}
async function resetArchive() {
  if (!confirm("清空 URL 归档?\n\n只清这一张表, 不碰资源库与任何文件。")) return;
  try {
    const r = await clearUrlArchive();
    archiveResult.value = null;
    toast(`已清空 ${r.cleared} 条`, "ok");
  } catch (e) {
    toast(e.response?.data?.detail || String(e), "err");
  }
}

// 巡检标记的中文: 从后端下发的 `kinds` 里取。⚠️ 前端不维护映射表 ——
// 维护一份就会在后端新增类别(mismatch 就是这么加的)时静默显示成代号。
function verifyKindLabel(kind) {
  return verifyResult.value?.kinds.find((k) => k.kind === kind)?.label || kind;
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

// 当前的**筛选条件**(不含分页/排序)。抽出来是为了让"查询"与"保存这个搜索"
// 用同一份对象 —— 两处各拼一遍的话, 用户保存下来的条件会与他看到的列表悄悄
// 差一项, 而这种偏差不报错, 只表现为"这个搜索好像不太对"。
function currentParams() {
  return {
    q: query.value.q,
    kind: query.value.kind,
    album: query.value.album,
    tag: query.value.tag,
    tag_children: query.value.tag_children,
    favorite: query.value.favorite,
    min_rating: query.value.min_rating,
    special: query.value.special,
    color: query.value.color,
    // V45 排除与日期区间。保存搜索时一起带走 —— "排除 A 的那一批"同样是个
    // 值得存下来的视角, 少带一项就等于存了个不同的搜索。
    exclude_tag: query.value.exclude_tag,
    exclude_tag_children: query.value.exclude_tag_children,
    exclude_album: query.value.exclude_album,
    exclude_kind: query.value.exclude_kind,
    exclude_special: query.value.exclude_special,
    exclude_color: query.value.exclude_color,
    date_from: query.value.date_from,
    date_to: query.value.date_to,
  };
}

async function load() {
  loading.value = true;
  try {
    const r = await listLibrary({
      ...currentParams(),
      sort: query.value.sort || undefined,
      order: query.value.order || undefined,
      page: query.value.page,
      page_size: query.value.page_size,
    });
    items.value = r.items || [];
    total.value = r.total || 0;
    pages.value = r.pages || 1;
    stats.value = r.stats || null;
    // 徽章中文由后端下发(见 api/tasks.py 的 error_kind_labels)
    if (r.error_kind_labels) errorKindLabels.value = r.error_kind_labels;
    // 生效条件由**生成 SQL 的那一处**回灌(后端 database.library_filters)。
    // 这里原样显示, 不按本地 query 重算 —— 否则"传了但没生效"会被画成已生效。
    applied.value = r.applied || [];
    // 排序档位也由后端在 facets 里下发, 这里的回显只用于**校正**本地 state:
    // 未知代号会被后端 400 挡掉, 所以一旦请求成功, 本地值就一定与真实顺序一致。
    if (r.sort) query.value.sort = r.sort === defaultSort.value ? "" : r.sort;
    if (r.order) query.value.order = r.order;
    // 排序档位与默认档位一起下发: 前端只存代号, 中文名始终来自后端(第 9 条)。
    if (r.sorts?.length) sortOptions.value = r.sorts;
    if (r.default_sort) defaultSort.value = r.default_sort;
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
  // 手动改条件 = 不再是"正在看某个保存的搜索"。不清掉的话侧栏会一直高亮
  // 那一条, 而列表早就是别的东西了。
  activeSearch.value = null;
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
  loadSearches();
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
      <!-- V42: 三项"库现在怎么样"的视图。它们与"失败诊断"是**两件事**:
           那个问"该拿到却没拿到的是什么", 这些问"已经拿到的里面, 哪些还没整理 /
           哪些互相重复 / 哪些叫错了名字"。 -->
      <button
        class="ghost mini"
        :class="{ on: facets }"
        :disabled="facetsBusy"
        @click="toggleFacets"
      >{{ facetsBusy ? "读取中…" : "体检" }}</button>
      <button class="ghost mini" :disabled="dupBusy" @click="loadDuplicates">
        {{ dupBusy ? "读取中…" : "查重复" }}
      </button>
      <button
        class="ghost mini"
        :class="{ on: archiveOpen }"
        @click="archiveOpen = !archiveOpen"
      >URL 归档</button>
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
      <!-- V44 主色筛选: 色系(键 + 中文名)由 /library/facets 的 colors 下发,
           前端不硬编码 —— 后端加一个色系时界面自动多一项。 -->
      <select v-model="query.color" class="lib-sel" @change="search">
        <option value="">全部颜色</option>
        <option v-for="c in facets?.colors || []" :key="c.key" :value="c.key">
          {{ c.label }}
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
      <!-- V43 排序: 档位名来自后端(facets/library 同源), 这里只画。
           再点当前档位 = 回到默认; 方向单独一个按钮切换。 -->
      <div class="sort-wrap">
        <button class="ghost sort-btn" @click="sortOpen = !sortOpen">
          排序: {{ currentSortLabel }} <i class="caret">▾</i>
        </button>
        <button
          class="ghost mini"
          :title="query.order === 'asc' ? '当前升序, 点一下改降序' : '当前降序, 点一下改升序'"
          @click="toggleOrder"
        >{{ query.order === "asc" ? "↑" : "↓" }}</button>
        <div v-if="sortOpen" class="sort-pop">
          <button
            v-for="s in sortOptions"
            :key="s.key"
            class="sort-item"
            :class="{ on: (query.sort || defaultSort) === s.key }"
            @click="pickSort(s.key)"
          >{{ s.label }}</button>
        </div>
      </div>
      <!-- V44: 主色检索 / 打包导出 / 文件头规范化。
           ⚠️ "修正扩展名"是**两步**的: 先出计划(dry-run), 确认后才真改 ——
           改名落到磁盘上且不可逆, 不做"点了就直接改"。 -->
      <button
        class="ghost"
        :disabled="busyColor"
        title="给还没提过色的图片提取主色(需要 ffmpeg)"
        @click="runExtractColors"
      >
        {{ busyColor ? "提色中…" : "提取主色" }}
      </button>
      <button
        class="ghost"
        :disabled="busyExport"
        title="把当前相册打包成 zip(带 manifest.json)"
        @click="runExport"
      >
        {{ busyExport ? "打包中…" : "打包导出" }}
      </button>
      <button
        class="ghost"
        :disabled="busyNorm"
        title="按文件头把叫错名字的文件改成正确的扩展名"
        @click="runNormalize"
      >
        {{ busyNorm ? "检查中…" : "修正扩展名" }}
      </button>
      <!-- V45 高级筛选: 排除 + 日期区间。默认收起, 但**非空时强制展开** ——
           收着的条件是"列表少了大半却说不清为什么"的经典来源。 -->
      <button
        class="ghost"
        :class="{ on: advOpen }"
        :title="hasAdvanced ? '已设了排除/日期条件' : '排除某些标签/相册, 或按落盘日期筛选'"
        @click="advOpen = !advOpen"
      >
        高级{{ hasAdvanced ? " ●" : "" }} <i class="caret">▾</i>
      </button>
    </div>

    <!-- V45 排除 / 日期区间 -->
    <div v-if="advOpen || hasAdvanced" class="adv-box">
      <div class="adv-row">
        <span class="tb-label">排除标签</span>
        <select v-model="query.exclude_tag" class="lib-sel" @change="search">
          <option value="">(不排除)</option>
          <option v-for="t in tags" :key="t.tag" :value="t.tag">{{ t.tag }}</option>
        </select>
        <label class="tb-sub" v-if="query.exclude_tag">
          <input
            type="checkbox"
            :checked="query.exclude_tag_children"
            @change="query.exclude_tag_children = !query.exclude_tag_children; search()"
          />
          含子标签
        </label>
        <span class="tb-label">排除相册</span>
        <select v-model="query.exclude_album" class="lib-sel" @change="search">
          <option value="">(不排除)</option>
          <option v-for="a in albums" :key="a.album" :value="a.album">{{ a.album }}</option>
        </select>
        <span class="tb-label">排除类型</span>
        <select v-model="query.exclude_kind" class="lib-sel" @change="search">
          <option value="">(不排除)</option>
          <option v-for="k in KINDS.filter((x) => x.key !== 'all')" :key="k.key" :value="k.key">
            {{ k.label }}
          </option>
        </select>
      </div>
      <div class="adv-row">
        <span class="tb-label">排除状态</span>
        <select v-model="query.exclude_special" class="lib-sel" @change="search">
          <option value="">(不排除)</option>
          <option v-for="f in facets?.items || []" :key="f.key" :value="f.key">{{ f.label }}</option>
        </select>
        <span class="tb-label">排除颜色</span>
        <select v-model="query.exclude_color" class="lib-sel" @change="search">
          <option value="">(不排除)</option>
          <option v-for="c in facets?.colors || []" :key="c.key" :value="c.key">{{ c.label }}</option>
        </select>
        <!-- 日期区间。<input type="date"> 给的就是 YYYY-MM-DD, 与后端校验同形态。
             ⚠️ 标签写"落盘于"不写"拍摄于": 我们没有 EXIF, 这是落盘时间。 -->
        <span class="tb-label">落盘于</span>
        <input v-model="query.date_from" class="lib-sel" type="date" @change="search" />
        <span class="adv-sep">→</span>
        <input v-model="query.date_to" class="lib-sel" type="date" @change="search" />
        <span class="grow"></span>
        <button class="ghost mini" :disabled="!hasAdvanced" @click="clearAdvanced">清除</button>
      </div>
      <p class="adv-note">
        排除是「<b>没有</b>这个标签 / <b>不在</b>这个相册」, 用 NOT EXISTS 实现 ——
        与正向筛选不是简单取反(标签为 NULL 时"不等于 X"在 SQL 里既不是真也不是假)。
        日期含两端, 上界自动算到当天 23:59:59。
      </p>
    </div>

    <!-- V43 保存的搜索(智能文件夹)。每条都带**命中数**:
         "配了但一条都不匹配"与"没配"在界面上必须能分开, 所以 0 就显示 0;
         count 为 null 表示**没算出结果**(库里的条件读坏了), 显示成 "—" ——
         这与 0 是两件事(第 26 条)。 -->
    <div class="sw-bar">
      <span class="tb-label">智能文件夹</span>
      <span class="sw-list">
        <span
          v-for="s in searches"
          :key="s.id"
          class="sw-chip"
          :class="{ on: activeSearch === s.id, broken: s.broken }"
        >
          <button
            class="sw-pick"
            :title="s.broken ? '这个搜索的条件在库里读不出来' : '应用这组筛选'"
            @click="applySearch(s)"
          >
            {{ s.name }}
            <em class="sw-n">{{ s.count === null ? "—" : s.count }}</em>
          </button>
          <button class="sw-del" title="删除" @click="removeSearch(s)">×</button>
        </span>
        <span v-if="!searches.length && !searchBusy" class="sw-empty">
          把当前条件存下来, 以后一键回到这一屏
        </span>
      </span>
      <span class="grow"></span>
      <input
        v-model="searchName"
        class="sw-name"
        placeholder="给这组条件起个名字…"
        @keyup.enter="saveCurrentSearch"
      />
      <button
        class="ghost mini"
        :disabled="searchBusy"
        @click="saveCurrentSearch"
      >保存当前筛选</button>
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

    <!-- 当前生效的整理型筛选。⚠️ 必须常驻显示: 体检面板是可以收起的, 而
         "列表少了大半"这件事如果不说清原因, 用户的第一反应是"东西丢了"。 -->
    <div class="org-bar" v-if="query.special || query.min_rating">
      <span class="org-lab">整理筛选</span>
      <span class="org-chip on" v-if="query.special">
        {{ (facets?.items || []).find((x) => x.key === query.special)?.label || query.special }}
      </span>
      <span class="org-chip on" v-if="query.min_rating">≥ {{ query.min_rating }} 星</span>
      <button class="ghost mini" @click="clearOrganize">清除</button>
    </div>

    <!-- V45: 后端回灌的"这次真正生效了哪些条件"。
         ⚠️ 显示这一条的理由是第 27 条: 规则生效要有证据。没有它, "我传了但后端
         没认"与"条件生效了但就是没东西"长得一模一样 —— 两者都表现为列表空。
         取值**只来自后端 applied**, 不拿 query 反推: query 里 kind=all 是"没筛",
         照着画会让人以为筛过了。 -->
    <div class="org-bar applied-bar" v-if="applied.length">
      <span class="org-lab">已生效</span>
      <span v-for="c in applied" :key="c.key" class="org-chip on" :title="c.key">
        {{ c.label }}
      </span>
    </div>

    <!-- V45 找相似: 按 dHash 汉明距离找"看着像"的图。
         ⚠️ 与"疑似重复"刻意分成两个入口、两个阈值: 重复(≤4)是"就是同一张",
         相似(默认 ≤12)里混着"构图像但不是同一张" —— 当成重复处理会真丢东西。
         ⚠️ ok=false 时**必须显示 reason**: no-fingerprint / no-decoder 都是
         "没法比", 不是"没有相似的"(第 26 条 —— 空列表会把两者混成一个答案)。 -->
    <div v-if="similarData" class="sim-box">
      <div class="sm-head">
        <b>与「{{ baseName(similarData.origin?.local_path) }}」相似</b>
        <span class="sm-sub" v-if="similarData.ok && !similarBusy">
          {{ similarData.items.length }} 条 · 距离 ≤ {{ similarData.max_distance }}/64
        </span>
        <span class="grow"></span>
        <span class="sm-seg">
          <button
            v-for="d in SIMILAR_DISTANCES"
            :key="d"
            class="ghost mini"
            :class="{ on: similarData.max_distance === d }"
            :title="`汉明距离 ≤ ${d}/64`"
            @click="openSimilar(similarData.origin, d)"
          >≤{{ d }}</button>
        </span>
        <button class="ghost mini" @click="closeSimilar">收起</button>
      </div>

      <!-- 三种"没法比"都在这里说清楚, 而不是给一个空列表 -->
      <p v-if="similarData && !similarData.ok" class="sm-reason">
        没法比: {{ similarReason(similarData) }}
      </p>
      <p v-else-if="similarBusy" class="sm-note">正在比对…</p>
      <p v-else-if="!similarData.items.length" class="sm-note">
        距离 ≤ {{ similarData.max_distance }} 以内没有别的图。可以放宽到 ≤20 再试。
      </p>
      <div v-else class="sm-grid">
        <div class="sm-card" v-for="s in similarData.items" :key="s.id">
          <img
            v-if="s.local_path"
            :src="thumbUrl(s.local_path, 200)"
            loading="lazy"
            alt=""
            @error="(e) => (e.target.style.display = 'none')"
          />
          <div class="sm-meta">
            <span class="sm-d" :title="`汉明距离 ${s.distance}/64`">{{ s.distance }}</span>
            <span class="sm-nm" :title="s.local_path">{{ baseName(s.local_path) }}</span>
          </div>
        </div>
      </div>
      <!-- 候选被截断要**报出来**(同族 J): 不报的话"最像的那张没出现在列表里"
           会被当成功能不准, 而其实是扫描上限到了。 -->
      <p v-if="similarData.truncated" class="sm-note warn">
        候选超过扫描上限, 只比了前 {{ similarData.scanned }} 条 —— 可能有更近的没比到。
      </p>
      <p class="sm-note">只展示, 不动任何文件。</p>
    </div>

    <!-- 体检视图: 每个整理型维度各有多少条。点一下即筛选。
         ⚠️ n === 0 的项**仍然可点**: 0 是后端真数过的结论, 不是"还没算"
         (做成禁用会让人以为还没算出来, 与"没法数"混为一谈)。 -->
    <div v-if="facets" class="facet-box">
      <div class="fc-head">
        <b>体检</b>
        <span class="fc-sub">共 {{ facets.total }} 项已落盘</span>
        <span class="grow"></span>
        <button class="ghost mini" :disabled="facetsBusy" @click="loadFacets">刷新</button>
        <button class="ghost mini" @click="facets = null">收起</button>
      </div>
      <div class="fc-grid">
        <button
          v-for="f in facets.items"
          :key="f.key"
          class="facet"
          :class="{ on: query.special === f.key }"
          :title="f.hint"
          @click="pickSpecial(f.key)"
        >
          <span class="fc-name">{{ f.label }}</span>
          <em class="fc-n">{{ f.n }}</em>
        </button>
      </div>
      <!-- 星级分布: 与上面的维度是**两套东西** —— 上面是"状态", 这里是"我给的
           评价"。分开摆是因为它们的处置方式不同(前者要整理, 后者只是挑出来看)。 -->
      <div class="fc-stars">
        <span class="fc-lab">评分</span>
        <button
          v-for="b in facets.ratings"
          :key="b.stars"
          class="star-chip"
          :class="{ on: query.min_rating > 0 && b.stars >= query.min_rating }"
          :title="b.stars === 0 ? '未评分(含还没评过)' : `≥ ${b.stars} 星`"
          @click="b.stars ? pickRating(b.stars) : pickSpecial('unrated')"
        >
          <span v-if="b.stars">{{ "★".repeat(b.stars) }}</span>
          <span v-else>未评分</span>
          <em>{{ b.n }}</em>
        </button>
      </div>
      <p class="fc-note">
        每一项都带条数, 且 0 就是 0(真的数过了)。点一下即按该维度筛选。
      </p>
    </div>

    <!-- 疑似重复分组。只标记不删 —— dHash 会误判, 所以"只保留建议项"默认
         只删记录不动文件, 并且要二次确认。 -->
    <div v-if="dupData" class="dup-box">
      <div class="dp-head">
        <b>疑似重复 {{ dupData.total }} 组</b>
        <span class="dp-sub">只标记, 不删文件</span>
        <span class="grow"></span>
        <button class="ghost mini" :disabled="dupBusy" @click="loadDuplicates">刷新</button>
        <button class="ghost mini" @click="dupData = null">收起</button>
      </div>
      <p v-if="!dupData.groups.length" class="dp-note">没有疑似重复的资源。</p>
      <div v-else class="dp-list">
        <div class="dp-group" v-for="g in dupData.groups.slice(0, 20)" :key="g.keep_id">
          <div class="dp-ghead">
            <em class="dp-n">{{ g.n }} 条</em>
            <span class="dp-bytes">{{ fmtSize(g.bytes) }}</span>
            <span class="grow"></span>
            <button class="ghost mini" :title="'按像素/体积/先后给出'" @click="keepOnly(g)">
              只保留建议项
            </button>
          </div>
          <div
            class="dp-row"
            v-for="m in g.members"
            :key="m.id"
            :class="{ keep: m.id === g.keep_id }"
          >
            <em v-if="m.id === g.keep_id" class="dp-keep">保留</em>
            <span v-else class="dp-dot"></span>
            <span class="dp-name" :title="m.local_path">{{ baseName(m.local_path) }}</span>
            <span class="dp-dim">{{ mediaMeta(m) || "—" }}</span>
            <span class="dp-size">{{ fmtSize(m.size) }}</span>
            <span class="dp-why" v-if="m.id === g.keep_id">{{ keepLabel(g.keep_reason) }}</span>
          </div>
        </div>
        <div v-if="dupData.groups.length > 20" class="dp-more">
          只列出前 20 组
        </div>
      </div>
      <p class="dp-note">
        判据是感知指纹(dHash), 会误判 —— 纯色图之间距离恒为 0。这里给的是建议, 决定权在你。
      </p>
    </div>

    <!-- URL 归档(对标 yt-dlp / gallery-dl 的 --download-archive):
         归档是**能随身带走的纯文本**, 换机器 / 重装后带过来, 增量采集就还能
         认出"这个我下过"。 -->
    <div v-if="archiveOpen" class="arc-box">
      <div class="ar-head">
        <b>已下载 URL 归档</b>
        <span class="ar-sub" v-if="archiveResult">共 {{ archiveResult.total }} 条</span>
        <span class="grow"></span>
        <button class="ghost mini" :disabled="archiveBusy" @click="exportArchive">导出 txt</button>
        <button class="ghost mini danger" :disabled="archiveBusy" @click="resetArchive">清空</button>
        <button class="ghost mini" @click="archiveOpen = false">收起</button>
      </div>
      <textarea
        v-model="archiveText"
        class="ar-input"
        rows="4"
        placeholder="粘贴归档内容, 每行一条 URL(支持 yt-dlp 的 `extractor id` 两列格式); # 开头与空行会被忽略"
      ></textarea>
      <div class="ar-actions">
        <button class="ghost mini" :disabled="archiveBusy || !archiveText.trim()" @click="importArchive">
          {{ archiveBusy ? "导入中…" : "导入" }}
        </button>
        <span class="ar-note">
          ⚠️ 归档只用于"我记得下过这个地址", 不参与"文件在不在"的判断 ——
          那归资源库管。
        </span>
      </div>
      <!-- 导入结果必须给出 added / skipped: 只说"导入成功"的话, 用户没法判断
           这份归档是不是真被吃进去了 -->
      <p v-if="archiveResult" class="ar-res">
        新增 {{ archiveResult.added }} 条, 已存在而跳过 {{ archiveResult.skipped }} 条,
        当前共 {{ archiveResult.total }} 条
      </p>
    </div>

    <!-- 巡检结果: 只在有问题时占版面, 全部完好就一句话 -->
    <div v-if="verifyResult && verifyResult.marked" class="verify-box">
      <div class="vb-head">
        <b>校验发现 {{ verifyResult.marked }} 项异常</b>
        <span class="vb-sub">
          已检查 {{ verifyResult.checked }} 项 ·
          缺失 {{ verifyResult.missing }} · 疑似截断 {{ verifyResult.truncated }} ·
          名字与内容不符 {{ verifyResult.mismatched || 0 }}
        </span>
        <span class="grow"></span>
        <button class="ghost mini" @click="verifyResult = null">知道了</button>
      </div>
      <div class="vb-list">
        <div class="vb-row" v-for="it in verifyResult.items.slice(0, 50)" :key="it.id">
          <!-- 三类分色: 缺失(红) / 截断(黄) / 不符(紫)。"不符"多数还能打开,
               它只是**叫错了名字**, 所以不该与"内容坏了"长得一样。 -->
          <em class="vb-kind" :class="it.kind">{{ verifyKindLabel(it.kind) }}</em>
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
      <span class="sep"></span>
      <!-- 批量打星: 已选里全是同一档时, 再点该档 = 清除 -->
      <span class="rate-pick">
        <button
          v-for="n in RATE_OPTS"
          :key="n"
          class="rst"
          :class="{ on: selRating && n <= selRating }"
          :title="selRating === n ? `清除这 ${selectedCount} 项的评分` : `给所选打 ${n} 星`"
          @click="rate([...selected], selRating === n ? 0 : n)"
        >{{ selRating && n <= selRating ? "★" : "☆" }}</button>
      </span>
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
          <!-- 巡检标记: missing/corrupt/mismatch 由 /library/verify 写入 error_kind。
               ⚠️ 它也可能出现在**成功**资源上(status=done + corrupt),
               所以这里只做标记, 不隐藏卡片、也不改状态 —— 文件通常还在,
               只是可能看不全(mismatch 尤其如此: 它只是叫错了名字)。 -->
          <em
            v-if="BADGED_KINDS.includes(r.error_kind)"
            class="tagbad"
            :class="r.error_kind"
            :title="r.note || ''"
          >{{ errorKindLabels[r.error_kind] || r.error_kind }}</em>
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
          <!-- V45 找相似。⚠️ 只在图片上出现: 指纹只对图片算(见 task_manager 里
               `type == 'image'` 那一处), 在视频上放这个按钮等于保证它没结果。 -->
          <button
            v-if="r.type === 'image'"
            class="sim"
            title="找相似(按感知指纹, 与'疑似重复'不是一个阈值)"
            @click.stop="openSimilar(r)"
          >⧉</button>
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
          <!-- 评分: 与收藏**并存而不是合并** —— 收藏只能把东西分成"要/不要"两堆,
               而在几千张里挑"最好的那几张"时, 需要的是**能排序**的序数。
               ⚠️ 点当前那一档 = 清除(否则手滑点错没有回退路径)。 -->
          <div
            class="rate-row"
            :title="r.rating ? `已评 ${r.rating} 星, 再点一次清除` : '点一颗星打分'"
          >
            <button
              v-for="n in RATE_OPTS"
              :key="n"
              class="rst"
              :class="{ on: n <= (r.rating || 0) }"
              @click.stop="rateOne(r, n)"
            >{{ n <= (r.rating || 0) ? "★" : "☆" }}</button>
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
/* ---- V43 排序下拉 ---- */
.sort-wrap { position: relative; display: flex; align-items: center; gap: 4px; flex: none; }
.sort-btn .caret { font-style: normal; opacity: .6; margin-left: 2px; }
.sort-pop {
  position: absolute; top: calc(100% + 4px); right: 0; z-index: 20;
  background: var(--panel-2); border: 1px solid var(--border); border-radius: 8px;
  padding: 4px; min-width: 120px; display: flex; flex-direction: column;
  box-shadow: 0 6px 18px rgba(0, 0, 0, .28);
}
.sort-item {
  background: none; border: 0; color: var(--text); text-align: left;
  padding: 5px 9px; border-radius: 6px; font-size: 12px; cursor: pointer;
}
.sort-item:hover { background: var(--border); }
.sort-item.on { color: var(--accent); }
/* ---- V43 保存的搜索(智能文件夹) ---- */
.sw-bar {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  margin: -6px 0 14px;
}
.sw-list { display: contents; }
.sw-chip {
  display: inline-flex; align-items: center;
  border: 1px solid var(--border); border-radius: 20px;
  background: var(--panel-2); overflow: hidden;
}
.sw-chip.on {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 12%, transparent);
}
/* 条件读不出来的: 边框提示, 但**不隐藏** —— 让它消失等于把"坏了"变成"没有"。 */
.sw-chip.broken { border-style: dashed; border-color: var(--warn, #d29922); }
.sw-pick {
  background: none; border: 0; color: inherit; cursor: pointer;
  padding: 3px 4px 3px 10px; font-size: 12px;
}
.sw-chip.on .sw-pick { color: var(--accent); }
.sw-n {
  font-style: normal; opacity: .6; margin-left: 5px; font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.sw-del {
  background: none; border: 0; color: var(--muted); cursor: pointer;
  padding: 3px 8px 3px 4px; font-size: 13px; line-height: 1;
}
.sw-del:hover { color: var(--err, #f85149); }
.sw-empty { color: var(--muted); font-size: 11px; }
.sw-name {
  background: var(--panel-2); border: 1px solid var(--border); border-radius: 6px;
  color: var(--text); padding: 4px 8px; font-size: 12px; width: 170px;
}
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

/* ---- V42: 整理筛选 / 体检 / 重复 / 归档 ---- */
/* 当前生效的整理型筛选。常驻显示: 体检面板可收起, 而"列表少了大半"不说清原因
   的话, 第一反应会是"东西丢了"。 */
.org-bar {
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  margin: -6px 0 12px; font-size: 12px;
}
.org-lab { color: var(--muted); }
.org-chip {
  padding: 2px 9px; border-radius: 20px; font-size: 11px;
  background: var(--panel-2); border: 1px solid var(--border); color: var(--muted);
}
.org-chip.on {
  color: var(--accent); border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 12%, transparent);
}
/* 已生效条件(后端 applied)与上面的"整理筛选"是两件事: 那一条是用户点的,
   这一条是后端**真的写进 SQL 的**。两者不一致时, 以这一条为准。 */
.applied-bar { margin: -10px 0 12px; opacity: .9; }

/* ---- V45: 排除 / 日期区间 ---- */
.adv-box {
  border: 1px solid var(--border); border-radius: 10px; background: var(--panel-2);
  padding: 10px 12px; margin: -6px 0 12px;
}
.adv-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.adv-row + .adv-row { margin-top: 8px; }
.adv-sep { color: var(--muted); font-size: 12px; }
.adv-note { margin: 8px 0 0; color: var(--muted); font-size: 11px; line-height: 1.6; }
.adv-note b { color: var(--text); font-weight: 600; }

/* ---- V45: 找相似 ---- */
.sim-box {
  border: 1px solid var(--border); border-radius: 10px; background: var(--panel-2);
  padding: 10px 12px; margin-bottom: 12px;
}
.sm-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 13px; }
.sm-sub { color: var(--muted); font-size: 12px; }
.sm-seg { display: inline-flex; gap: 2px; }
.sm-seg .on { color: var(--accent); border-color: var(--accent); }
.sm-grid { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
.sm-card { width: 116px; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; background: var(--panel); }
.sm-card img { width: 100%; height: 86px; object-fit: cover; display: block; background: #000; }
.sm-meta { display: flex; align-items: center; gap: 4px; padding: 4px 6px; font-size: 11px; }
.sm-d { font-variant-numeric: tabular-nums; color: var(--accent); font-weight: 600; }
.sm-nm { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); }
.sm-note { margin: 8px 0 0; color: var(--muted); font-size: 11px; line-height: 1.6; }
.sm-note.warn { color: var(--warn); }
/* "没法比"不是错误也不是空结果 —— 用中性偏提示的样式, 别用 err 色
   (那会让人以为请求失败了, 而请求是成功的, 只是比不了)。 */
.sm-reason { margin: 8px 0 0; color: var(--warn); font-size: 12px; line-height: 1.6; }
/* 卡片上的"找相似"按钮。⚠️ 与 `.star` 同一条纪律: 都在右下角那一排, 位置必须
   让开(星标占 right 32..57), 叠在一起会表现为"这个按钮点不到"。
   同样平时透明、hover 才亮 —— 一页 40 个图标按钮会很吵。 */
.sim {
  position: absolute; right: 62px; bottom: 6px; z-index: 2;
  background: rgba(0, 0, 0, .42); border: none; border-radius: 6px;
  color: #d7dde5; font-size: 13px; line-height: 1; padding: 3px 6px;
  cursor: pointer; opacity: 0; transition: opacity .15s, color .15s;
}
.card:hover .sim { opacity: 1; }
.sim:hover { color: var(--accent); }
/* 体检面板: 中性色(不是警告色) —— "有多少没打标签"是待办, 不是故障。
   用 warn 色会让人以为库坏了。 */
.facet-box {
  margin-bottom: 12px; padding: 10px 12px; border-radius: 9px;
  background: var(--panel-2); border: 1px solid var(--border);
}
.fc-head { display: flex; align-items: baseline; gap: 10px; font-size: 13px; }
.fc-sub { color: var(--muted); font-size: 12px; }
.fc-head .grow { flex: 1; }
.fc-grid {
  margin-top: 8px; display: grid; gap: 6px;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
}
.facet {
  display: flex; align-items: center; justify-content: space-between; gap: 6px;
  padding: 5px 9px; border-radius: 7px; cursor: pointer; font-size: 12px;
  background: var(--panel); border: 1px solid var(--border); color: var(--muted);
  transition: color .15s, border-color .15s, background .15s;
}
.facet:hover { color: var(--text); }
.facet.on {
  color: var(--accent); border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 12%, transparent);
}
.fc-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* 条数用等宽数字: 一列数字对不齐时, 扫一眼比大小的动作会被打回成逐个读 */
.fc-n { font-style: normal; font-variant-numeric: tabular-nums; opacity: .7; font-size: 11px; }
.fc-stars { margin-top: 8px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.fc-lab { color: var(--muted); font-size: 12px; }
.star-chip {
  display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px;
  border-radius: 20px; font-size: 11px; cursor: pointer;
  background: var(--panel); border: 1px solid var(--border); color: var(--muted);
}
.star-chip:hover { color: var(--text); }
.star-chip.on { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 55%, var(--border)); }
.star-chip em { font-style: normal; font-variant-numeric: tabular-nums; opacity: .7; }
.fc-note, .dp-note { margin: 8px 0 0; font-size: 11px; color: var(--muted); }
/* 重复分组 */
.dup-box {
  margin-bottom: 12px; padding: 10px 12px; border-radius: 9px;
  background: color-mix(in srgb, var(--warn) 8%, var(--panel-2));
  border: 1px solid color-mix(in srgb, var(--warn) 30%, var(--border));
}
.dp-head { display: flex; align-items: baseline; gap: 10px; font-size: 13px; }
.dp-sub { color: var(--muted); font-size: 12px; }
.dp-head .grow, .dp-ghead .grow { flex: 1; }
.dp-list { margin-top: 8px; display: flex; flex-direction: column; gap: 8px; max-height: 320px; overflow: auto; }
.dp-group { padding: 6px 8px; border-radius: 7px; background: var(--panel); border: 1px solid var(--border); }
.dp-ghead { display: flex; align-items: center; gap: 8px; font-size: 12px; }
.dp-n { font-style: normal; color: var(--text); }
.dp-bytes { color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.dp-row {
  display: flex; align-items: baseline; gap: 6px; margin-top: 3px;
  font-size: 11px; color: var(--muted);
}
.dp-row.keep { color: var(--text); }
.dp-keep {
  font-style: normal; font-size: 10px; padding: 0 5px; border-radius: 4px; flex: none;
  background: color-mix(in srgb, var(--ok) 70%, #000); color: #fff;
}
.dp-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--border); flex: none; }
.dp-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dp-dim, .dp-size { flex: none; font-variant-numeric: tabular-nums; }
.dp-why { flex: none; font-size: 10px; color: var(--muted); }
.dp-more { font-size: 11px; color: var(--muted); margin-top: 4px; }
/* URL 归档 */
.arc-box {
  margin-bottom: 12px; padding: 10px 12px; border-radius: 9px;
  background: var(--panel-2); border: 1px solid var(--border);
}
.ar-head { display: flex; align-items: baseline; gap: 10px; font-size: 13px; }
.ar-sub { color: var(--muted); font-size: 12px; }
.ar-head .grow { flex: 1; }
.ar-input {
  display: block; width: 100%; margin-top: 8px; resize: vertical;
  background: var(--panel); border: 1px solid var(--border); border-radius: 7px;
  padding: 6px 8px; color: var(--text); font-size: 12px; font-family: inherit;
}
.ar-input:focus { outline: none; border-color: var(--accent); }
.ar-actions { display: flex; align-items: center; gap: 8px; margin-top: 6px; flex-wrap: wrap; }
.ar-note { font-size: 11px; color: var(--muted); }
.ar-res { margin: 6px 0 0; font-size: 12px; color: var(--text); }
/* 评分: 未点亮时压得很淡 —— 一页 40 张卡 × 5 颗亮星会把整屏变成星星 */
.rate-row { display: flex; gap: 1px; margin-top: 4px; }
.rst {
  background: none; border: none; padding: 0 1px; cursor: pointer;
  font-size: 12px; line-height: 1; color: var(--muted); opacity: .35;
  transition: opacity .12s, color .12s;
}
.rst.on { color: var(--warn); opacity: 1; }
.rate-row:hover .rst, .rate-pick:hover .rst { opacity: .75; }
.rate-row:hover .rst.on, .rate-pick:hover .rst.on { opacity: 1; }
.rate-pick { display: inline-flex; align-items: center; gap: 1px; }
.rate-pick .rst { font-size: 13px; }
/* "名字与内容不符"用紫: 它与"缺失/损坏"不是同一类 —— 多数还能打开, 只是叫错了
   名字。用同一个红色会让人以为文件坏了。 */
.tagbad.mismatch, .vb-kind.mismatch {
  background: color-mix(in srgb, var(--accent) 78%, #000); color: #fff;
}
</style>
