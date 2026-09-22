// 全局轻提示。把散落在各处的 confirm()/alert() 收敛成一致的 toast,
// 这样「软提示(任务已创建但多半粘错链接)」和「硬错误」用的是同一套视觉,
// 不会有的弹窗、有的红条、有的浏览器原生框, 用户记不住哪扇门对应什么。
import { reactive } from "vue";

export const toastState = reactive({ items: [] });
let seq = 0;

// kind: "ok" | "warn" | "err"; ttl 毫秒后自动消失
export function toast(text, kind = "ok", ttl = 6000) {
  const id = ++seq;
  toastState.items.push({ id, text, kind });
  setTimeout(() => {
    const i = toastState.items.findIndex((x) => x.id === id);
    if (i >= 0) toastState.items.splice(i, 1);
  }, ttl);
  return id;
}
