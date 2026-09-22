<script setup>
import { onMounted, onUnmounted, ref } from "vue";
import { getNotifications, markNotificationsRead } from "../api";

const items = ref([]);
const unread = ref(0);
const open = ref(false);
let timer = null;

async function load() {
  try {
    const r = await getNotifications();
    items.value = r.items || [];
    unread.value = r.unread || 0;
  } catch (e) {
    /* 后端不可达时保留上次数据 */
  }
}
function toggle() {
  open.value = !open.value;
  if (open.value && unread.value) markAll();
}
async function markAll() {
  try {
    await markNotificationsRead(null);
  } catch (e) {}
  unread.value = 0;
  items.value = items.value.map((i) => ({ ...i, read: true }));
}
function fmt(t) {
  if (!t) return "";
  return String(t).slice(0, 16).replace("T", " ");
}

onMounted(() => {
  load();
  timer = setInterval(load, 20000);
});
onUnmounted(() => clearInterval(timer));
</script>

<template>
  <div class="notif">
    <button class="bell" @click="toggle" title="通知中心">
      <span class="ico">🔔</span>
      <span v-if="unread" class="badge">{{ unread > 99 ? "99+" : unread }}</span>
    </button>
    <div v-if="open" class="pop">
      <div class="pop-head">
        <span>通知</span>
        <button class="ghost mini" @click="markAll">全部已读</button>
      </div>
      <div class="pop-body" v-if="items.length">
        <div
          v-for="n in items"
          :key="n.id"
          class="notif-item"
          :class="[n.level, { read: n.read }]"
        >
          <div class="n-title">{{ n.title }}</div>
          <div class="n-body" v-if="n.body">{{ n.body }}</div>
          <div class="n-time">{{ fmt(n.created_time) }}</div>
        </div>
      </div>
      <div v-else class="empty">暂无通知</div>
    </div>
  </div>
</template>

<style scoped>
.notif { position: relative; }
.bell {
  position: relative;
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-radius: 8px;
  width: 34px;
  height: 34px;
  cursor: pointer;
  font-size: 15px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: border-color 0.2s, background 0.2s;
}
.bell:hover { border-color: var(--accent); }
.badge {
  position: absolute;
  top: -6px;
  right: -6px;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  border-radius: 8px;
  background: var(--err);
  color: #fff;
  font-size: 10px;
  line-height: 16px;
  text-align: center;
}
.pop {
  position: absolute;
  right: 0;
  top: 40px;
  width: 320px;
  max-height: 440px;
  overflow-y: auto;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.45);
  z-index: 60;
}
.pop-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
  font-size: 13px;
  color: var(--muted);
}
.pop-body { padding: 4px; }
.notif-item { padding: 8px 10px; border-bottom: 1px solid var(--border); }
.notif-item:last-child { border-bottom: none; }
.notif-item.success .n-title { color: var(--ok); }
.notif-item.error .n-title { color: var(--err); }
.notif-item.warning .n-title { color: var(--warn); }
.notif-item.read { opacity: 0.5; }
.n-title { font-size: 13px; font-weight: 600; }
.n-body { font-size: 12px; color: var(--muted); margin-top: 2px; line-height: 1.5; }
.n-time { font-size: 11px; color: var(--muted); margin-top: 3px; }
.empty { padding: 24px; text-align: center; color: var(--muted); font-size: 12px; }
</style>
