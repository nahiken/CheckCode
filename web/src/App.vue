<!-- web/src/App.vue —— 工作台主视图:任务表单 + 任务列表 + 审批面板 + 对话流 + Diff/报告。
M9 W1:SSE 订阅统一由 App 持有(新建/列表选择/resume 都经 App 一处订阅)。
approval SSE 事件 → 渲染 ApprovalPanel;点允许/拒绝 resolve 后恢复 running。 -->
<script setup lang="ts">
import { computed, onUnmounted, ref } from 'vue'
import TaskForm from './components/TaskForm.vue'
import TaskList from './components/TaskList.vue'
import ApprovalPanel from './components/ApprovalPanel.vue'
import PlanApprovalPanel from './components/PlanApprovalPanel.vue'
import ConversationStream from './components/ConversationStream.vue'
import ReportView from './components/ReportView.vue'
import { subscribeTaskEvents, type WorkbenchEvent } from './api/sse'

const events = ref<WorkbenchEvent[]>([])
const report = ref<Record<string, unknown> | null>(null)
const taskId = ref<string | null>(null)
const status = ref('idle')
// 当前任务的待审批项(收到 approval SSE 事件即置入;resolved 后清空)
const currentApproval = ref<{
  approval_id: string
  tool_name: string
  args?: Record<string, unknown>
  reason?: string
} | null>(null)
// 当前任务的待审批计划(M10:plan_approval SSE 事件;resolved 后清空)
const currentPlan = ref<{
  approval_id: string
  files?: string[]
  commands?: string[]
} | null>(null)
// 当前订阅句柄:切换任务(resume/列表选择)先关旧订阅,避免串流/重复
let stopCurrent = (() => {}) as () => void

// 列表侧刷新(ref 引用 TaskList 的 refresh)
const taskList = ref<{ refresh: () => Promise<void> } | null>(null)

const isAwaiting = computed(() => currentApproval.value !== null)
const isAwaitingPlan = computed(() => currentPlan.value !== null)

function handleEvent(ev: WorkbenchEvent) {
  events.value.push(ev)
  const data = ev.data as Record<string, unknown>
  if (ev.event === 'report') {
    report.value = data
    status.value = 'done'
    currentApproval.value = null
    currentPlan.value = null
  } else if (ev.event === 'approval') {
    currentApproval.value = {
      approval_id: String(data.approval_id ?? ''),
      tool_name: String(data.tool_name ?? ''),
      args: (data.args as Record<string, unknown> | undefined) ?? {},
      reason: typeof data.reason === 'string' ? data.reason : '',
    }
    currentPlan.value = null // 计划已批准进入写阶段 → 清掉计划面板,避免残留
    status.value = 'awaiting_approval'
  } else if (ev.event === 'plan_approval') {
    currentPlan.value = {
      approval_id: String(data.approval_id ?? ''),
      files: (data.files as string[] | undefined) ?? [],
      commands: (data.commands as string[] | undefined) ?? [],
    }
    currentApproval.value = null // 计划门先于逐工具门;清掉可能残留的工具面板
    status.value = 'awaiting_plan_approval'
  } else if (ev.event === 'resume') {
    status.value = 'resumed'
  } else if (ev.event === 'error' && !report.value) {
    status.value = 'error'
    currentApproval.value = null
    currentPlan.value = null
  }
}

/** 订阅指定任务 SSE:关旧订阅 → 清空当前视图 → 订阅实时流(含历史回放)。 */
function subscribe(id: string) {
  stopCurrent()
  stopCurrent = () => {}
  events.value = []
  report.value = null
  currentApproval.value = null
  currentPlan.value = null
  taskId.value = id
  stopCurrent = subscribeTaskEvents(id, handleEvent)
}

function onTaskCreated(id: string) {
  status.value = 'created'
  subscribe(id)
}

function onTaskSelected(id: string) {
  status.value = 'viewing'
  subscribe(id)
}

function onApprovalResolved() {
  currentApproval.value = null
  status.value = 'running'
}

function onPlanResolved() {
  currentPlan.value = null
  status.value = 'running'
}

function onListError(msg: string) {
  status.value = `错误: ${msg}`
}

// ReportView 回滚完成 → 更新状态 + 刷新任务列表(该任务转 rolled_back,可恢复)
async function onRolledBack(msg: string) {
  status.value = `已回滚: ${msg}`
  report.value = null
  await taskList.value?.refresh()
}

onUnmounted(stopCurrent)
</script>

<template>
  <main class="workbench">
    <header>
      <h1>WhyCode 工作台</h1>
      <span class="status" data-testid="status">{{ status }}</span>
    </header>
    <TaskForm @created="onTaskCreated" />
    <section class="layout">
      <TaskList
        ref="taskList"
        class="sidebar"
        @select="onTaskSelected"
        @resumed="onTaskCreated"
      />
      <div class="content">
        <PlanApprovalPanel
          v-if="isAwaitingPlan && taskId"
          :task-id="taskId"
          :plan="currentPlan"
          @resolved="onPlanResolved"
          @error="onListError"
        />
        <ApprovalPanel
          v-if="isAwaiting && taskId"
          :task-id="taskId"
          :approval="currentApproval"
          @resolved="onApprovalResolved"
          @error="onListError"
        />
        <ConversationStream :events="events" />
        <ReportView
          :report="report"
          :task-id="taskId"
          @rolledback="onRolledBack"
        />
      </div>
    </section>
  </main>
</template>

<style>
body {
  margin: 0;
  font-family: system-ui, sans-serif;
  background: #fafafa;
}
.workbench {
  max-width: 1200px;
  margin: 0 auto;
}
header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px;
  border-bottom: 1px solid #e0e0e0;
}
.columns {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.columns > div {
  background: #fff;
  border: 1px solid #e0e0e0;
  border-radius: 6px;
  min-height: 300px;
}
</style>
