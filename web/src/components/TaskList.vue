<!-- web/src/components/TaskList.vue —— 任务列表(历史任务概览 + 行内审批/恢复)。
M9 W1:
- awaiting_approval 行:展示待审批工具 + 允许/拒绝按钮(点后刷新)。
- done/error/rolled_back 行:恢复按钮 → POST resume → 通知 App 订阅实时流。
提供 refresh()(App 在创建/回滚后调用刷新,行内操作后也自查)。 -->
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { approvePlan, approveTask, listTasks, resumeTask, type TaskSummary } from '../api/sse'

const emit = defineEmits<{
  (e: 'select', taskId: string): void
  (e: 'resumed', taskId: string): void
  (e: 'approval-resolved'): void
}>()

const props = withDefaults(defineProps<{ fetchImpl?: typeof fetch }>(), {})

const tasks = ref<TaskSummary[]>([])
const error = ref('')

async function refresh() {
  try {
    tasks.value = await listTasks({ fetchImpl: props.fetchImpl })
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

onMounted(refresh)

async function onApprove(taskId: string, approvalId: string, approve: boolean) {
  error.value = ''
  try {
    await approveTask(taskId, approvalId, approve, { fetchImpl: props.fetchImpl })
    await refresh()
    emit('approval-resolved')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

/** 计划审批(M10):POST plan-approvals/{id}(与逐工具审批不同端点)。 */
async function onApprovePlan(taskId: string, approvalId: string, approve: boolean) {
  error.value = ''
  try {
    await approvePlan(taskId, approvalId, approve, { fetchImpl: props.fetchImpl })
    await refresh()
    emit('approval-resolved')
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

async function onResume(taskId: string) {
  error.value = ''
  try {
    const res = await resumeTask(taskId, { fetchImpl: props.fetchImpl })
    if (res.error) {
      error.value = res.error
      return
    }
    emit('resumed', taskId) // App 订阅该任务实时流
    await refresh()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

defineExpose({ refresh })

const RECOVERABLE = new Set(['done', 'error', 'rolled_back'])
</script>

<template>
  <div class="task-list" data-testid="task-list">
    <h3>任务列表</h3>
    <p v-if="error" class="error">{{ error }}</p>
    <ul v-if="tasks.length">
      <li v-for="t in tasks" :key="t.task_id" class="task-row">
        <button class="row-main" data-testid="task-item" @click="emit('select', t.task_id)">
          <span class="tid">{{ t.task_id.slice(0, 8) }}</span>
          <span class="stat" :class="t.status">{{ t.status }}</span>
          <span v-if="t.task" class="desc">{{ t.task }}</span>
        </button>
        <!-- awaiting_plan_approval:待审批计划 + 批准/拒绝(M10) -->
        <div v-if="t.status === 'awaiting_plan_approval'" class="row-actions">
          <div v-for="p in t.pending_plan_approvals" :key="p.approval_id" class="approval">
            <span class="tool-name">计划</span>
            <span v-if="p.plan?.files" class="reason">{{ (p.plan.files as string[]).join(', ') }}</span>
            <button
              class="ok"
              data-testid="plan-approve-btn"
              @click.stop="onApprovePlan(t.task_id, p.approval_id, true)"
            >批准</button>
            <button
              class="no"
              data-testid="plan-deny-btn"
              @click.stop="onApprovePlan(t.task_id, p.approval_id, false)"
            >拒绝</button>
          </div>
        </div>
        <!-- awaiting_approval:待审批工具 + 允许/拒绝 -->
        <div v-else-if="t.status === 'awaiting_approval'" class="row-actions">
          <div v-for="p in t.pending_approvals" :key="p.approval_id" class="approval">
            <span class="tool-name">{{ p.tool_name }}</span>
            <span v-if="p.reason" class="reason">{{ p.reason }}</span>
            <button
              class="ok"
              data-testid="approve-btn"
              @click.stop="onApprove(t.task_id, p.approval_id, true)"
            >允许</button>
            <button
              class="no"
              data-testid="deny-btn"
              @click.stop="onApprove(t.task_id, p.approval_id, false)"
            >拒绝</button>
          </div>
        </div>
        <!-- 可恢复状态:恢复按钮 -->
        <div v-else-if="RECOVERABLE.has(t.status)" class="row-actions">
          <button class="resume" data-testid="resume-btn" @click.stop="onResume(t.task_id)">
            恢复
          </button>
        </div>
      </li>
    </ul>
    <p v-else class="empty">暂无任务</p>
  </div>
</template>

<style scoped>
.task-list {
  padding: 12px;
}
.task-list ul {
  list-style: none;
  padding: 0;
  margin: 0;
}
.task-list li {
  margin: 4px 0;
}
.row-main {
  width: 100%;
  text-align: left;
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 6px 10px;
  cursor: pointer;
  border: 1px solid #e0e0e0;
  border-radius: 4px;
  background: #fff;
}
.row-main:hover {
  background: #f0f0f0;
}
.tid {
  font-family: monospace;
  font-size: 12px;
  color: #666;
}
.stat {
  font-size: 12px;
  border-radius: 3px;
  padding: 1px 6px;
  color: #fff;
  background: #999;
}
.stat.awaiting_approval,
.stat.awaiting_plan_approval {
  background: #e67e22;
}
.stat.running,
.stat.created {
  background: #2980b9;
}
.stat.done {
  background: #27ae60;
}
.stat.error,
.stat.rolled_back {
  background: #c0392b;
}
.desc {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #444;
}
.row-actions {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  padding: 4px 10px;
  align-items: center;
  background: #fafafa;
  border-left: 1px solid #e0e0e0;
  border-right: 1px solid #e0e0e0;
  border-bottom: 1px solid #e0e0e0;
  border-radius: 0 0 4px 4px;
}
.approval {
  display: flex;
  gap: 6px;
  align-items: center;
  font-size: 12px;
}
.tool-name {
  font-family: monospace;
  color: #2c3e50;
}
.reason {
  color: #888;
}
.row-actions button {
  font-size: 12px;
  padding: 2px 10px;
  cursor: pointer;
  border-radius: 3px;
  border: 1px solid #ccc;
  background: #fff;
}
button.ok {
  color: #27ae60;
  border-color: #27ae60;
}
button.no {
  color: #c0392b;
  border-color: #c0392b;
}
button.resume {
  color: #2980b9;
  border-color: #2980b9;
}
.error {
  color: #c0392b;
}
.empty {
  color: #999;
}
</style>
