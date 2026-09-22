// 任务 / 资源状态的统一中文标签与配色。
// 后端返回的 status 字符串同时就是 CSS 类名(见 style.css 的 .badge.*),
// 所以「颜色」直接用 status 本身, 这里只补「中文文案」与「分组」。

export const STATUS_LABELS = {
  pending: "等待中",
  running: "运行中",
  extracting: "提取中",
  downloading: "下载中",
  success: "成功",
  partial: "部分完成",
  failed: "失败",
  cancelled: "已取消",
  paused: "已暂停",
  // 资源级
  done: "已下载",
  filtered: "已过滤",
  skipped: "跳过",
  pending: "待处理",
};

export function statusLabel(s) {
  return STATUS_LABELS[s] || s;
}

// 表格筛选用的分组: 选「进行中」时一次性把四个活动态都发上去,
// 否则用户得在 4 个状态里挑, 而它们本质上都是"还没结束"。
export const STATUS_GROUPS = [
  { value: "", label: "全部" },
  { value: "active", label: "进行中", statuses: ["pending", "running", "extracting", "downloading"] },
  { value: "success", label: "成功" },
  { value: "partial", label: "部分完成" },
  { value: "failed", label: "失败" },
  { value: "cancelled", label: "已取消" },
  { value: "paused", label: "已暂停" },
];

export function groupToStatuses(value) {
  const g = STATUS_GROUPS.find((x) => x.value === value);
  return g && g.statuses ? g.statuses : value ? [value] : null;
}
