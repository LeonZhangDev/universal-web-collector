import axios from "axios";

const api = axios.create({ baseURL: "/" });

// 旧签名 listTasks() 返回裸数组; 现在后端改成 {items,total,...} 且支持分页/筛选。
// 这里统一收一个 options 对象, 没传参数时退化为"拉第一页全量", 老调用方不报错。
export function listTasks(opts = {}) {
  const params = {};
  if (opts.q) params.q = opts.q;
  if (opts.status) params.status = opts.status; // 数组或单值均可
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
