<!-- web/src/components/DiffView.vue —— 任务修改的 Diff 视图。
接收 diff 字符串(risk report 的 diff 字段),渲染为只读代码块。
报告完成前显示占位;空 diff 显示「(无修改)」。 -->
<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ diff: string }>()

const hasDiff = computed(() => props.diff.trim().length > 0)
</script>

<template>
  <div class="diff-view" data-testid="diff">
    <pre v-if="hasDiff" class="diff">{{ diff }}</pre>
    <p v-else class="empty">(无修改)</p>
  </div>
</template>

<style scoped>
.diff-view {
  padding: 10px;
  background: #f8f8f8;
  border-radius: 4px;
}
.diff {
  margin: 0;
  white-space: pre-wrap;
  max-height: 400px;
  overflow-y: auto;
}
.empty {
  margin: 0;
  color: #999;
}
</style>
