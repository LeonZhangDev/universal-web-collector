<script setup>
import { ref, watch } from "vue";
import { browseFs, mkdirFs, DRIVES_VIEW } from "../api";

const props = defineProps({
  show: { type: Boolean, default: false },
  initial: { type: String, default: "" },
  // 标题与底部说明做成 props: 这个选择器现在有两个用途(选下载目录 / 选相册集目录),
  // 而"文件保存到 …/<任务ID>/"这句话对后者是**错的** —— 相册集目录是只读的。
  // 抄一份出来改字更省事, 但那就有两个目录选择器要各自维护了。
  title: { type: String, default: "选择下载文件夹" },
  hint: { type: String, default: "" },
});
const emit = defineEmits(["select", "close"]);

const cwd = ref("");
const manualPath = ref("");
const parentPath = ref(null);
const entries = ref([]);
const selected = ref("");
const loading = ref(false);
const errorMsg = ref("");
const newName = ref("");

async function open(path) {
  loading.value = true;
  errorMsg.value = "";
  selected.value = "";
  try {
    const data = await browseFs(path || "");
    cwd.value = data.cwd;
    // 手输路径成功跳转后同步输入框; 失败时保留用户输入便于修改
    manualPath.value = data.cwd;
    parentPath.value = data.parent;
    entries.value = data.entries;
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || String(e);
  } finally {
    loading.value = false;
  }
}

async function go() {
  // 容错: 去掉从资源管理器"复制文件地址"带来的引号
  const p = manualPath.value.trim().replace(/^["']+|["']+$/g, "");
  if (!p) return;
  if (p !== cwd.value) await open(p);
}

function up() {
  if (parentPath.value) open(parentPath.value);
}

function enter(dir) {
  open(dir.path);
}

function toggle(dir) {
  selected.value = selected.value === dir.path ? "" : dir.path;
}

async function createFolder() {
  const name = newName.value.trim();
  if (!name) return;
  try {
    await mkdirFs(cwd.value, name);
    newName.value = "";
    await open(cwd.value);
  } catch (e) {
    errorMsg.value = e.response?.data?.detail || String(e);
  }
}

function confirm() {
  emit("select", selected.value || cwd.value);
}

watch(
  () => props.show,
  (v) => {
    if (v) open(props.initial || "");
  }
);
</script>

<template>
  <div v-if="show" class="modal-mask" @click.self="emit('close')">
    <div class="modal">
      <div class="modal-head">
        <h3>{{ title }}</h3>
        <button class="ghost" @click="emit('close')">✕</button>
      </div>

      <div class="path-bar">
        <button class="ghost" :disabled="!parentPath" @click="up">↑ 上级</button>
        <button class="ghost" @click="open(DRIVES_VIEW)">此电脑</button>
        <input
          v-model="manualPath"
          type="text"
          spellcheck="false"
          placeholder="手动输入路径回车跳转, 如 D:\photos"
          @keyup.enter="go"
        />
        <button class="ghost" @click="go">跳转</button>
      </div>

      <div class="error-box" v-if="errorMsg">{{ errorMsg }}</div>

      <div class="dir-list">
        <div v-if="loading" class="empty">加载中...</div>
        <div v-else-if="entries.length === 0" class="empty">该目录下没有子文件夹</div>
        <div
          v-for="d in entries"
          :key="d.path"
          class="dir-item"
          :class="{ picked: selected === d.path }"
          @click="toggle(d)"
          @dblclick="enter(d)"
        >
          <span class="icon">{{ cwd ? "📁" : "💽" }}</span>
          <span class="nm">{{ d.name }}</span>
          <button class="ghost mini" @click.stop="enter(d)">进入</button>
        </div>
      </div>

      <div class="mkdir-row" v-if="cwd">
        <input
          v-model="newName"
          type="text"
          placeholder="在当前目录下新建文件夹"
          @keyup.enter="createFolder"
        />
        <button class="ghost" @click="createFolder">新建</button>
      </div>

      <div class="modal-foot">
        <div class="hint">
          <template v-if="hint">{{ hint }}</template>
          <template v-else>
            文件保存到 <code>{{ selected || cwd || "-" }}</code> / &lt;任务ID&gt; /
          </template>
        </div>
        <span style="flex: 1"></span>
        <button class="ghost" @click="emit('close')">取消</button>
        <button :disabled="!cwd && !selected" @click="confirm">选择此文件夹</button>
      </div>
    </div>
  </div>
</template>
