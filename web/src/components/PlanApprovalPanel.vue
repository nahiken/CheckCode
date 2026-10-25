<!-- web/src/components/PlanApprovalPanel.vue —— 计划审批面板(M10)。
严格模式下,Planner 生成的任务专属计划在进入写阶段之前提交人工审批:
展示计划要修改的文件与要运行的命令,提供批准/拒绝
(approve → POST plan-approvals/{id})。拒绝 → 闭环不做任何修改。 -->
<script setup lang="ts">
import { ref } from 'vue'
import { approvePlan } from '../api/sse'

const props = withDefaults(
  defineProps<{
    taskId: string | null
    plan: { approval_id: string; files?: string[]; commands?: string[] } | null
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
  if (!props.taskId || !props.plan || fetching.value) return
  fetching.value = true
  try {
    await approvePlan(props.taskId, props.plan.approval_id, approve, {
      fetchImpl: props.fetchImpl,
    })
    emit('resolved') // App 清空当前计划(任务恢复 running)
  } catch (e) {
    emit('error', e instanceof Error ? e.message : String(e))
  } finally {
    fetching.value = false
  }
}
</script>

<template>
  <div v-if="plan" class="plan-panel" data-testid="plan-panel">
    <div class="head">
      <strong>等待计划审批</strong>
    </div>
    <div class="section">
      <span class="label">计划修改文件</span>
      <ul data-testid="plan-files">
        <li v-for="f in plan.files ?? []" :key="f">{{ f }}</li>
        <li v-if="!(plan.files ?? []).length" class="empty">(无)</li>
      </ul>
    </div>
    <div class="section">
      <span class="label">计划运行命令</span>
      <ul data-testid="plan-commands">
        <li v-for="c in plan.commands ?? []" :key="c">{{ c }}</li>
        <li v-if="!(plan.commands ?? []).length" class="empty">(无)</li>
      </ul>
    </div>
    <div class="approval-actions">
      <button class="ok" data-testid="plan-approve-btn" :disabled="fetching" @click="decide(true)">
        批准计划
      </button>
      <button class="no" data-testid="plan-deny-btn" :disabled="fetching" @click="decide(false)">
        拒绝
      </button>
    </div>
  </div>
</template>

<style scoped>
.plan-panel {
  margin: 8px 12px;
  padding: 10px 12px;
  border: 1px solid #8e44ad;
  border-left: 4px solid #8e44ad;
  border-radius: 4px;
  background: #f9f0ff;
}
.head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.section {
  margin-top: 6px;
}
.label {
  font-size: 13px;
  color: #555;
}
.section ul {
  margin: 2px 0;
  padding-left: 20px;
  font-family: monospace;
  font-size: 12px;
}
.empty {
  color: #999;
}
</style>
