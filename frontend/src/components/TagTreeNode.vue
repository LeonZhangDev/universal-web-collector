<script setup>
// V46: 嵌套标签树的递归节点。树是标签名里 `/` 的**呈现**, 不引入新表。
// 点父节点 = 按前缀收子(emit pick 时由父级把 tag_children 置真), 点叶子 = 精确匹配。
const props = defineProps({
  node: { type: Object, required: true },
  depth: { type: Number, default: 0 },
  activeTag: { type: String, default: "" },
  activeChildren: { type: Boolean, default: false },
});
const emit = defineEmits(["pick"]);
// 父节点被选中且处于"含子"模式 = 激活; 叶子则要求精确匹配(不含子)。
const isOn = props.node.full === props.activeTag &&
  (props.node.children.length ? props.activeChildren : !props.activeChildren);
</script>

<template>
  <div class="tn">
    <button
      class="tn-btn"
      :class="{ on: isOn }"
      :style="{ paddingLeft: depth * 14 + 6 + 'px' }"
      @click="emit('pick', node)"
    >
      <span class="tn-tw" v-if="node.children.length">▾</span>
      <span class="tn-name">{{ node.name }}</span>
      <em class="tn-cnt">{{ node.count }}</em>
    </button>
    <TagTreeNode
      v-for="c in node.children"
      :key="c.full"
      :node="c"
      :depth="depth + 1"
      :active-tag="activeTag"
      :active-children="activeChildren"
      @pick="emit('pick', $event)"
    />
  </div>
</template>

<style scoped>
.tn-btn {
  display: block; width: 100%; text-align: left; background: transparent;
  border: none; color: inherit; padding: 3px 6px; font-size: 12px;
  border-radius: 5px; cursor: pointer; white-space: nowrap;
}
.tn-btn:hover { background: var(--panel-2); }
.tn-btn.on { background: var(--accent); color: #fff; }
.tn-tw { display: inline-block; width: 12px; color: var(--muted); }
.tn-name { margin-left: 2px; }
.tn-cnt { margin-left: 6px; color: var(--muted); font-style: normal; font-size: 11px; }
.tn-btn.on .tn-cnt { color: rgba(255, 255, 255, 0.8); }
</style>
