// 任务字节吞吐的**内存**采样表。
//
// 后端按 ~0.4s 节流推送 `task.bytes`(累计字节数)，这里把累计值换算成瞬时速率，
// 保留最近 N 个采样点供详情页画曲线。
//
// ⚠️ 为什么不落库、也不放进 Pinia/大响应式对象:
//   1. 这条流每秒来好几条，写 SQLite 会撞上"写-写单写者"的瓶颈，拖慢所有任务;
//   2. 它是**纯展示**数据，刷新页面从头再来完全可接受 —— 曲线丢几个点没有代价，
//      但为了它去动持久层会影响正确性。
//
// 采样点结构: { t: 秒级时间戳, rate: 字节/秒 }
// 用**差分**而不是直接把累计字节画出来: 累计值是一条单调曲线，看不出快慢，
// 用户要的是"现在多快"。

const MAX_POINTS = 120; // 0.4s 一点 ≈ 最近 48 秒
const STALE_MS = 3000; // 超过 3 秒没新点就认为停了，速率归零

// taskId -> { bytes, ts, points: [] }
const store = new Map();
// 订阅者(组件 unmount 时注销，避免泄漏)
const listeners = new Set();

function emit(taskId) {
  listeners.forEach((fn) => {
    try {
      fn(taskId, getSeries(taskId));
    } catch (_) {}
  });
}

/**
 * 记录一次累计字节采样。
 * @param {number} taskId
 * @param {number} totalBytes 该任务**本次运行**的累计已下载字节
 * @param {number} ts 后端时间戳(秒)
 */
export function pushByteSample(taskId, totalBytes, ts) {
  const now = ts || Date.now() / 1000;
  let s = store.get(taskId);
  if (!s) {
    s = { bytes: 0, ts: now, points: [] };
    store.set(taskId, s);
  }
  const dt = now - s.ts;
  const db = totalBytes - s.bytes;

  // ⚠️ dt 太小算出的速率会被时间精度放大成天文数字(比如 0.001s 内 256KB
  // → 256MB/s)，曲线直接被一个尖刺拉平。低于 50ms 的样本直接忽略，
  // 但**仍要更新累计值**，否则下一次差分会把两次的量算到一起。
  if (dt >= 0.05 && db >= 0) {
    s.points.push({ t: now, rate: db / dt });
    if (s.points.length > MAX_POINTS) s.points.shift();
  }
  s.bytes = totalBytes;
  s.ts = now;
  emit(taskId);
}

/** 取某任务的速率序列(已过期归零的尾部会补一个 0 点，曲线自然落下)。 */
export function getSeries(taskId) {
  const s = store.get(taskId);
  if (!s) return [];
  const pts = s.points.slice();
  if (Date.now() / 1000 - s.ts > STALE_MS && pts.length) {
    pts.push({ t: Date.now() / 1000, rate: 0 });
  }
  return pts;
}

/** 当前瞬时速率(字节/秒)，取自最近一个采样点。 */
export function currentRate(taskId) {
  const s = store.get(taskId);
  if (!s || !s.points.length) return 0;
  if (Date.now() / 1000 - s.ts > STALE_MS) return 0;
  return s.points[s.points.length - 1].rate;
}

/** 订阅变化，返回注销函数。 */
export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** 任务结束后清掉，避免长会话里 Map 无限增长。 */
export function clearSeries(taskId) {
  store.delete(taskId);
  emit(taskId);
}

/** 把字节速率格式化成人类可读(与后端 core/disk.py 的口径保持一致)。 */
export function fmtRate(bytesPerSec) {
  const n = Number(bytesPerSec) || 0;
  if (n <= 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}/s`;
}
