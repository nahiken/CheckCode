<!-- web/src/components/ApprovalPanel.vue —— 当前任务的浏览器审批面板。
当任务状态 awaiting_approval(收到 approval SSE 事件)时,App 把它渲染在对话流上方:
展示待审批工具/参数/理由,提供允许/拒绝(approve → POST approvals/{id})。 -->
<script setup lang="ts">
import { ref } from 'vue'
import { approveTask } from '../api/sse'

const props = withDefaults(
  defineProps<{
    taskId: string | null
    approval: { approval_id: string; tool_name: string; args?: Record<string, unknown>; reason?: string } | null
    fetchImpl?: typeof fetch
  }>(),
  {},
)

const emit = defineEmits<{
  (e: 'resolved'): void
  (e: 'error', message: string): void
}>()

const fetching = ref(false)

async function decide(approve: boolean) {
  if (!props.taskId || !props.approval || fetching.value) return
  fetching.value = true
  try {
    await approveTask(props.taskId, props.approval.approval_id, approve, {
      fetchImpl: props.fetchImpl,
    })
    emit('resolved') // App 清空当前审批(任务恢复 running)
  } catch (e) {
    emit('error', e instanceof Error ? e.message : String(e))
  } finally {
    fetching.value = false
  }
}
</script>

<template>
  <div v-if="approval" class="approval-panel" data-testid="approval-panel">
    <div class="head">
      <strong>等待人工审批</strong>
      <span class="tool">{{ approval.tool_name }}</span>
    </div>
    <p v-if="approval.reason" class="reason">{{ approval.reason }}</p>
    <pre v-if="approval.args && Object.keys(approval.args).length" class="args">
      {{ JSON.stringify(approval.args, null, 2) }}
    </pre>
    <div class="approval-actions">
      <button class="ok" data-testid="panel-approve-btn" :disabled="fetching" @click="decide(true)">
        允许执行
      </button>
      <button class="no" data-testid="panel-deny-btn" :disabled="fetching" @click="decide(false)">
        拒绝
      </button>
    </div>
  </div>
</template>

<style scoped>
.approval-panel {
  margin: 8px 12px;
  padding: 10px 12px;
  border: 1px solid #e67e22;
  border-left: 4px solid #e67e22;
  border-radius: 4px;
  background: #fff8f0;
}
.head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.tool {
  font-family: monospace;
  color: #e67e22;
}
.reason {
  margin: 6px 0 0;
  color: #555;
  font-size: 13px;
}
.args {
  background: #f5f5f5;
  padding: 6px;
  border-radius: 4px;
  font-size: 12px;
  white-space: pre-wrap;
}
</style>
