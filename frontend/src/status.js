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
  // "源站已无"(404/410)。与 failed 分开: 两者的**下一步动作**完全不同 ——
  // 失败该重试, 这一类重试一百次结果都一样。
  gone: "源站已无",
};

// ⚠️ 资源级与任务级的文案**不能共用一张表**, 而 `pending` 恰好是两边都有的词:
// 任务 pending 是"等待中"(排队还没轮到), 资源 pending 是"待处理"(还没轮到下行)。
//
// 这里原来只有一个 `STATUS_LABELS`, 里面写了**两个** `pending` 键。JS 对象后写的
// 覆盖先写的, 所以先写的那个一直没生效 —— 任务列表上"等待中"显示成了"待处理"。
// 重复键不会报错, 只会静默丢掉一半意思, 所以拆成两张表 + 一个 scope 参数。
const RESOURCE_LABELS = {
  pending: "待处理",
};

export function statusLabel(s, scope) {
  if (scope === "resource" && RESOURCE_LABELS[s]) return RESOURCE_LABELS[s];
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
