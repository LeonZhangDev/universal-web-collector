import axios from "axios";

const api = axios.create({ baseURL: "/" });

export function createTask(url, collector = "generic", options = {}) {
  return api.post("/tasks/create", { url, collector, ...options }).then((r) => r.data);
}
// 创建前预告: 只发现不下载, 不写库。返回目录名/张数/视频体积等。
export function previewTask(payload) {
  return api.post("/tasks/preview", payload).then((r) => r.data);
}
export function listTasks() {
  return api.get("/tasks").then((r) => r.data);
}
export function getTask(id) {
  return api.get(`/tasks/${id}`).then((r) => r.data);
}
export function getLogs(id) {
  return api.get(`/tasks/${id}/logs`).then((r) => r.data);
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
