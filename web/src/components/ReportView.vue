<!-- web/src/components/ReportView.vue —— 完成后展示 Diff + RiskReport + 回滚按钮。 -->
<script setup lang="ts">
import { computed, ref } from 'vue'
import { rollbackTask } from '../api/sse'
import DiffView from './DiffView.vue'

const props = defineProps<{ report: Record<string, unknown> | null; taskId: string | null }>()
const emit = defineEmits<{ (e: 'rolledback', msg: string): void }>()

const rolling = ref(false)
const rollbackMsg = ref('')

const diff = computed(() => (typeof props.report?.diff === 'string' ? props.report.diff : ''))
const changedFiles = computed(() => {
  const files = props.report?.changed_files
  return Array.isArray(files) ? files : []
})
const risk = computed(() => (typeof props.report?.remaining_risk === 'string' ? props.report.remaining_risk : ''))
const passed = computed(() => props.report?.passed === true)
// 零改动任务(计划被拒 / 只读分析)从未创建分支,回滚会凭空建出一条分支,后端也会拒。
// 前端同样不展示按钮 —— 否则用户点了一个必然失败的按钮(审计 M10–M15 B7)。
const rollbackable = computed(
  () => !!props.taskId && props.report?.plan_rejected !== true && props.report?.readonly !== true,
)

async function onRollback() {
  if (!props.taskId || rolling.value) return
  rolling.value = true
  rollbackMsg.value = ''
  try {
    const res = await rollbackTask(props.taskId)
    if (res.error) {
      rollbackMsg.value = res.error
    } else {
      rollbackMsg.value = res.message ?? '已回滚'
      emit('rolledback', rollbackMsg.value)
    }
  } catch (e) {
    rollbackMsg.value = e instanceof Error ? e.message : String(e)
  } finally {
    rolling.value = false
  }
}
</script>

<template>
  <div v-if="report" class="report" data-testid="report">
    <h3>
      风险报告
      <span :class="passed ? 'pass' : 'fail'">{{ passed ? '通过' : '失败' }}</span>
    </h3>
    <p class="risk">剩余风险: {{ risk }}</p>
    <p class="files">
      修改文件({{ changedFiles.length }}): {{ changedFiles.join(', ') || '(无)' }}
    </p>
    <DiffView :diff="diff" />
    <div class="rollback">
      <button
        v-if="rollbackable"
        data-testid="rollback-btn"
        :disabled="rolling"
        @click="onRollback"
      >
        {{ rolling ? '回滚中…' : '回滚修改' }}
      </button>
      <span v-if="rollbackMsg" class="rollback-msg">{{ rollbackMsg }}</span>
    </div>
  </div>
  <div v-else class="report-placeholder">任务完成后显示 Diff 与验证报告</div>
</template>

<style scoped>
.report {
  padding: 12px;
  font-family: monospace;
  font-size: 13px;
}
.pass {
  color: #27ae60;
}
.fail {
  color: #c0392b;
}
.risk,
.files {
  color: #555;
}
.report-placeholder {
  color: #999;
  padding: 20px;
}
.rollback {
  margin-top: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.rollback button {
  padding: 6px 14px;
  cursor: pointer;
}
.rollback-msg {
  color: #27ae60;
  font-size: 12px;
}
</style>
