<script setup>
import { toastState } from "../toast";
</script>

<template>
  <div class="toast-host">
    <div v-for="t in toastState.items" :key="t.id" class="toast" :class="t.kind">
      {{ t.text }}
    </div>
  </div>
</template>
