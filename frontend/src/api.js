import axios from "axios";

const api = axios.create({ baseURL: "/" });

// 旧签名 listTasks() 返回裸数组; 现在后端改成 {items,total,...} 且支持分页/筛选。
// 这里统一收一个 options 对象, 没传参数时退化为"拉第一页全量", 老调用方不报错。
export function listTasks(opts = {}) {
  const params = {};
  if (opts.q) params.q = opts.q;
  if (opts.status) params.status = opts.status; // 数组或单值均可
  if (opts.collector) params.collector = opts.collector;
  if (opts.page) params.page = opts.page;
  if (opts.page_size) params.page_size = opts.page_size;
  return api.get("/tasks", { params }).then((r) => r.data);
}
export function getTask(id) {
  return api.get(`/tasks/${id}`).then((r) => r.data);
}
export function getLogs(id) {
  return api.get(`/tasks/${id}/logs`).then((r) => r.data);
}
export function createTask(url, collector = "auto", options = {}) {
  return api.post("/tasks/create", { url, collector, ...options }).then((r) => r.data);
}
// 批量创建: 一次粘贴多行, 后端在创建前逐行标出重复/无效。
// payload: { urls:[...], collector, download_dir, filters, quality, media,
//           album_title, max_items, aggregate_depth, allow_duplicates }
export function batchCreateTasks(payload) {
  return api.post("/tasks/batch-create", payload).then((r) => r.data);
}
export function getEnvDiagnose() {
  return api.get("/env/diagnose").then((r) => r.data);
}
// URL -> 采集器(纯字符串判定, 不打网络请求), 用于"已识别为 X"回显。
export function resolveCollector(url) {
  return api
    .get("/collectors/resolve", { params: { url } })
    .then((r) => r.data)
    .catch(() => null);
}
// 创建前预告: 只发现不下载, 不写库。返回目录名/张数/视频体积等。
export function previewTask(payload) {
  return api.post("/tasks/preview", payload).then((r) => r.data);
}
export function retryTask(id) {
  return api.post(`/tasks/${id}/retry`).then((r) => r.data);
}
// withFiles=true 时连磁盘上的下载文件一起删(默认只删任务记录)
export function deleteTask(id, withFiles = false) {
  return api
    .delete(`/tasks/${id}`, { params: { with_files: withFiles } })
    .then((r) => r.data);
}
// 批量清理: 按状态删除已结束的任务
export function bulkDeleteTasks(statuses, withFiles = false) {
  return api
    .post("/tasks/bulk-delete", { statuses, with_files: withFiles })
    .then((r) => r.data);
}
export function getStorageOverview() {
  return api.get("/tasks/storage").then((r) => r.data);
}
// 断点续传暂存区: 中断过的下载按 URL 留在 downloads/_meta/partial/ 里等着续传。
// ⚠️ 这块占用在磁盘上**看不见**(不在相册目录里), 不做成界面就等于"程序在偷偷吃盘"。
export function getPartials() {
  return api.get("/library/partials").then((r) => r.data);
}
// 清空暂存: 代价只是"下次从头下", 不会下出坏文件 —— 所以不需要二次确认。
export function clearPartials() {
  return api.delete("/library/partials").then((r) => r.data);
}

// ---- 跨任务资源库 ----
// 浏览"手上已经有什么": 支持关键词 / 类型 / 相册 / 单任务 / 标签 / 收藏 六种筛选。
export function listLibrary(opts = {}) {
  const params = {};
  if (opts.q) params.q = opts.q;
  if (opts.kind && opts.kind !== "all") params.kind = opts.kind;
  if (opts.album) params.album = opts.album;
  if (opts.task_id) params.task_id = opts.task_id;
  if (opts.tag) params.tag = opts.tag;
  if (opts.tag_children) params.tag_children = "true";
  if (opts.favorite) params.favorite = "true";
  if (opts.page) params.page = opts.page;
  if (opts.page_size) params.page_size = opts.page_size;
  return api.get("/library", { params }).then((r) => r.data);
}
export function listLibraryAlbums() {
  return api.get("/library/albums").then((r) => r.data);
}
// 标签清单(带资源数 / 颜色 / 层级)。只统计已落盘资源上的标签 —— 否则会出现"点进去空的标签"。
// 返回里还带 `colors` 调色板与 `sep`/`max_depth`: 界面上人眼的配色与层级分隔符
// 只由后端定义, 前端不硬编码(否则后端改了色, 界面还是旧的)。
export function listLibraryTags() {
  return api.get("/library/tags").then((r) => r.data);
}
// 设置 / 清除一个标签的颜色。color="" 表示清除。
export function setTagColor(tag, color = "") {
  return api
    .post("/library/tags/color", { tag, color })
    .then((r) => r.data);
}
// 批量改标签。add / remove 一次请求内完成, clear 表示先清空再 add。
// ⚠️ 校验不过会整个失败(400)而不是"能加的加上" —— 静默部分成功会让用户以为
// 标签打上了, 下次找不到却不知道为什么。
export function libraryEditTags(ids, { add = [], remove = [], clear = false } = {}) {
  return api
    .post("/library/tags", { ids, add, remove, clear })
    .then((r) => r.data);
}
// 批量收藏 / 取消收藏。
export function librarySetFavorite(ids, value = true) {
  return api
    .post("/library/favorite", { ids, value })
    .then((r) => r.data);
}
// 资源库批量删除。⚠️ withFiles 默认 false = 只删记录、不动文件:
// 资源库里同一张图可能被多个任务 sha256 去重复用, 真删文件是不可逆的,
// 后端还会按引用数复查一遍(refs>1 时保留文件并在 kept_files 里回报)。
export function libraryBulkDelete(ids, withFiles = false) {
  return api
    .post("/library/bulk-delete", { ids, with_files: withFiles })
    .then((r) => r.data);
}
// 选中资源打包下载。这里只给 URL —— zip 是流式的, 交给浏览器直接导航,
// 走 axios 反而要把整个包读进内存。
export function libraryArchiveUrl(ids) {
  return `/library/archive?ids=${encodeURIComponent((ids || []).join(","))}`;
}
// 跨任务的"死信"视图: 失败资源按 error_kind 的分布 + 最近的一批明细。
// ⚠️ 返回的 `total`(含 corrupt)与 `items`/`replayable`(只含能重放的)口径不同,
// 两个数字回答的是两个问题 —— 界面上别混着显示(见后端 LibraryFailuresOut)。
// `label` 由后端下发, 前端不维护中文映射表。
export function listLibraryFailures(opts = {}) {
  const params = {};
  if (opts.include_gone) params.include_gone = "true";
  if (opts.limit) params.limit = opts.limit;
  return api.get("/library/failures", { params }).then((r) => r.data);
}
// 死信重放: 按 refs(指定资源) 或 kinds(按失败原因整批)重新排进下载队列。
// 两者同时给时后端取**交集**。返回里有 submitted/skipped 与逐条理由 ——
// 必须把理由显示出来, 否则"点了 30 条只起来 4 条"会被当成程序吞了。
export function replayLibraryFailures({
  refs = [],
  kinds = [],
  includeGone = false,
  limit = 200,
} = {}) {
  const body = { include_gone: includeGone, limit };
  if (refs.length) body.refs = refs;
  if (kinds.length) body.kinds = kinds;
  return api.post("/library/replay", body).then((r) => r.data);
}
// 落盘后完整性巡检: 库里有记录、磁盘上却不在或被截断的那些。只标记不删除。
export function libraryVerify(payload = {}) {
  return api.post("/library/verify", payload).then((r) => r.data);
}
// ---- 文件访问 ----
// 资源库是跨任务视图, 条目上只有 local_path —— 所以走按路径定位的 /files/raw,
// 而不是按任务定位的 /files/{task_id}/{file}。
export function rawFileUrl(path) {
  return `/files/raw?path=${encodeURIComponent(path || "")}`;
}
// 缩略图: 网格里铺原图会让一个 40 项的页面下载几百 MB(图集站的图常有几 MB)。
// 后端生成不了时会**回退原图**, 所以这里永远可以放心用。
export function thumbUrl(path, size = 320) {
  return `/files/thumb?path=${encodeURIComponent(path || "")}&size=${size}`;
}
// ---- 全局带宽上限 ----
// 0 = 不限速。对应下载层的字节令牌桶(见 downloaders/ratelimit.py)。
export function getBandwidth() {
  return api.get("/config/bandwidth").then((r) => r.data);
}
export function setBandwidth(bytesPerSec) {
  return api
    .post("/config/bandwidth", { bytes_per_sec: bytesPerSec })
    .then((r) => r.data);
}
// 任务级代理池的实时健康(熔断/失败次数)。任务结束后 running=false。
export function getTaskProxy(id) {
  return api.get(`/tasks/${id}/proxy`).then((r) => r.data);
}
// 批量操作: 对一组 task_id 执行同一动作(pause/resume/cancel/retry/delete)
export function bulkAction(action, taskIds, withFiles = false) {
  return api
    .post("/tasks/bulk-action", { action, task_ids: taskIds, with_files: withFiles })
    .then((r) => r.data);
}
// 失败资源选择性重试: 只重下 failed/skipped 的资源
export function retryFailed(taskId) {
  return api.post(`/tasks/${taskId}/retry-failed`).then((r) => r.data);
}
// 统计面板数据
export function getStats() {
  return api.get("/tasks/stats").then((r) => r.data);
}
// 通知中心
export function getNotifications() {
  return api.get("/notifications").then((r) => r.data);
}
export function markNotificationsRead(ids) {
  return api.post("/notifications/read", { ids: ids || null }).then((r) => r.data);
}
export function retryResource(taskId, resourceId) {
  return api.post(`/tasks/${taskId}/resources/${resourceId}/retry`).then((r) => r.data);
}
export function getCollectors() {
  return api.get("/collectors").then((r) => r.data);
}
export function getConfig() {
  return api.get("/config").then((r) => r.data);
}
export function browseFs(path) {
  return api.get("/fs/browse", { params: path ? { path } : {} }).then((r) => r.data);
}
export function mkdirFs(parent, name) {
  return api.post("/fs/mkdir", { parent, name }).then((r) => r.data);
}
export function cancelTask(id) {
  return api.post(`/tasks/${id}/cancel`).then((r) => r.data);
}
export function pauseTask(id) {
  return api.post(`/tasks/${id}/pause`).then((r) => r.data);
}
export function resumeTask(id) {
  return api.post(`/tasks/${id}/resume`).then((r) => r.data);
}
export function getManifest(id) {
  return api.get(`/files/${id}/manifest`).then((r) => r.data);
}
export function archiveUrl(id, only = "done") {
  return `/tasks/${id}/archive?only=${only}`;
}
// ---- 订阅巡检 ----
export function listWatches() {
  return api.get("/watches").then((r) => r.data);
}
export function createWatch(payload) {
  return api.post("/watches", payload).then((r) => r.data);
}
export function runWatch(id) {
  return api.post(`/watches/${id}/run`).then((r) => r.data);
}
export function toggleWatch(id) {
  return api.post(`/watches/${id}/toggle`).then((r) => r.data);
}
export function deleteWatch(id) {
  return api.delete(`/watches/${id}`).then((r) => r.data);
}
// ---- 登录态 ----
export function listSessions() {
  return api.get("/sessions").then((r) => r.data);
}
export function startLogin(url, timeout = 900) {
  return api.post("/sessions/login", { url, timeout }).then((r) => r.data);
}
export function getLoginJob(id) {
  return api.get(`/sessions/login/${id}`).then((r) => r.data);
}
export function stopLogin(id) {
  return api.post(`/sessions/login/${id}/stop`).then((r) => r.data);
}
export function deleteSession(domain) {
  return api.delete(`/sessions/${encodeURIComponent(domain)}`).then((r) => r.data);
}
