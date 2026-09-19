<script setup>
defineProps({
  tasks: { type: Array, default: () => [] },
  selectedId: { type: Number, default: null },
});
defineEmits(["select", "remove", "pause", "resume"]);
</script>

<template>
  <div v-if="tasks.length === 0" class="empty">暂无任务, 在上方输入 URL 创建第一个采集任务</div>
  <table v-else>
    <thead>
      <tr>
        <th>#</th>
        <th>名称</th>
        <th>URL</th>
        <th>采集器</th>
        <th>状态</th>
        <th>进度</th>
        <th>重试</th>
        <th>创建时间</th>
        <th></th>
      </tr>
    </thead>
    <tbody>
      <tr
        v-for="t in tasks" :key="t.id"
        class="row" :class="{ active: t.id === selectedId }"
        @click="$emit('select', t.id)"
      >
        <td>{{ t.id }}</td>
        <td>
          <span class="tname" :title="t.name || t.url">{{ t.name || "—" }}</span>
        </td>
        <td><span class="url" :title="t.url">{{ t.url }}</span></td>
        <td><span class="collector">{{ t.collector }}</span></td>
        <td><span class="badge" :class="t.status">{{ t.status }}</span></td>
        <td>
          <span class="progress-track"><span class="progress-fill" :style="{ width: t.progress + '%' }"></span></span>
          {{ t.progress }}%
        </td>
        <td>{{ t.retry_count }}</td>
        <td>{{ t.created_time }}</td>
        <td>
          <span class="row-actions">
            <button
              v-if="['pending', 'running', 'extracting', 'downloading'].includes(t.status)"
              class="ghost mini"
              title="暂停(保留已下载文件, 之后可续跑)"
              @click.stop="$emit('pause', t.id)"
            >暂停</button>
            <button
              v-if="t.status === 'paused'"
              class="primary mini"
              title="从断点继续(只补下缺失部分)"
              @click.stop="$emit('resume', t.id)"
            >继续</button>
            <button
              class="ghost danger"
              title="删除任务(可选择是否同时删除已下载的文件)"
              @click.stop="$emit('remove', t.id)"
            >✕</button>
          </span>
        </td>
      </tr>
    </tbody>
  </table>
</template>

<style scoped>
.tname {
  display: inline-block;
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: bottom;
  color: var(--text);
}
</style>
