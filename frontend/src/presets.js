// 下载预设: 把"采集器 / 画质 / 媒体 / 命名 / 过滤 / 聚合"打包成一键配置。
// 纯前端、存 localStorage —— 这是个人习惯, 不该进后端(不同机器上的"常用配置"
// 没有共享意义, 进了后端反而要在多端之间做同步)。
//
// 内置三套常用档(只读, 不可删); 用户可另存自己的预设并删除。
const USER_KEY = "uwc.presets.v1";

export const BUILTIN_PRESETS = [
  {
    name: "仅图片",
    icon: "🖼",
    opts: { collector: "auto", quality: "original", media: "image", album_title: "clean", filters: {}, aggregate_depth: 1, max_items: 50, download_dir: "" },
  },
  {
    name: "高质量视频",
    icon: "🎬",
    opts: { collector: "auto", quality: null, media: "video", album_title: "clean", filters: {}, aggregate_depth: 1, max_items: 50, download_dir: "" },
  },
  {
    name: "全量",
    icon: "📦",
    opts: { collector: "auto", quality: "original", media: "both", album_title: "clean", filters: {}, aggregate_depth: 1, max_items: 50, download_dir: "" },
  },
];

export function loadUserPresets() {
  try {
    const raw = localStorage.getItem(USER_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return Array.isArray(arr) ? arr : [];
  } catch (e) {
    return [];
  }
}

export function saveUserPreset(name, opts) {
  const list = loadUserPresets().filter((p) => p.name !== name);
  list.push({ name, icon: "⭐", opts: { ...opts } });
  try {
    localStorage.setItem(USER_KEY, JSON.stringify(list));
  } catch (e) {
    /* localStorage 不可用时忽略 */
  }
  return list;
}

export function deleteUserPreset(name) {
  const list = loadUserPresets().filter((p) => p.name !== name);
  try {
    localStorage.setItem(USER_KEY, JSON.stringify(list));
  } catch (e) {
    /* ignore */
  }
  return list;
}

// 把预设的 opts 应用到当前创建状态的「浅合并」 —— 只覆盖预设关心的字段,
// 不碰下载目录之外用户可能临时改的东西(这里连 download_dir 一起覆盖,
// 因为预设就是"整套想要的配置")。
export function applyPreset(state, opts) {
  state.collector = opts.collector ?? "auto";
  if (opts.quality !== undefined) state.quality = opts.quality;
  if (opts.media !== undefined) state.media = opts.media;
  if (opts.album_title !== undefined) state.album_title = opts.album_title;
  if (opts.aggregate_depth !== undefined) state.aggregate_depth = opts.aggregate_depth;
  if (opts.max_items !== undefined) state.max_items = opts.max_items;
  if (opts.download_dir !== undefined) state.download_dir = opts.download_dir;
  if (opts.filters) {
    const f = opts.filters;
    state.selTypes = Array.isArray(f.types) ? [...f.types] : [];
    state.exts = Array.isArray(f.exts) ? f.exts.join(",") : "";
    state.excludeExts = Array.isArray(f.exclude_exts) ? f.exclude_exts.join(",") : "";
    state.keywords = Array.isArray(f.keywords) ? f.keywords.join(",") : "";
    state.excludeKeywords = Array.isArray(f.exclude_keywords) ? f.exclude_keywords.join(",") : "";
    state.minSize = f.min_size ? parseSize(f.min_size) : { num: "", unit: "KB" };
    state.maxSize = f.max_size ? parseSize(f.max_size) : { num: "", unit: "MB" };
    state.minWidth = f.min_width != null ? String(f.min_width) : "";
    state.minHeight = f.min_height != null ? String(f.min_height) : "";
    state.dedupPerceptual = f.dedup_perceptual !== false;
  } else {
    state.selTypes = [];
    state.exts = "";
    state.excludeExts = "";
    state.keywords = "";
    state.excludeKeywords = "";
    state.minSize = { num: "", unit: "KB" };
    state.maxSize = { num: "", unit: "MB" };
    state.minWidth = "";
    state.minHeight = "";
    state.dedupPerceptual = true;
  }
}

function parseSize(s) {
  const m = /^([\d.]+)\s*(B|KB|MB|GB)$/i.exec(String(s || "").trim());
  if (!m) return { num: "", unit: "KB" };
  return { num: m[1], unit: m[2].toUpperCase() };
}

// ---- 预设配置导入/导出(可迁移分享, 不依赖 localStorage) ----
// 导出: 把内置 + 用户预设序列化成一段 JSON 文本。
export function exportPresetsText() {
  const payload = {
    kind: "uwc.presets",
    version: 1,
    builtin: BUILTIN_PRESETS.map((p) => ({ name: p.name, icon: p.icon, opts: p.opts })),
    user: loadUserPresets(),
  };
  return JSON.stringify(payload, null, 2);
}
// 导入: 解析上面格式的 JSON, 合并进 localStorage 的用户预设(同名覆盖)。
// 返回成功导入的用户预设数量; 格式不对抛出错误。
export function importPresetsText(text) {
  const data = JSON.parse(text);
  if (!data || typeof data !== "object" || !Array.isArray(data.user)) {
    throw new Error("不是有效的预设导出文件");
  }
  const existing = loadUserPresets();
  let added = 0;
  for (const p of data.user) {
    if (!p || !p.name || !p.opts) continue;
    saveUserPreset(p.name, p.opts);
    added += 1;
  }
  // saveUserPreset 内部已写回, 这里再读一次确保返回最新列表供 UI 刷新
  return { imported: added, presets: loadUserPresets() };
}
