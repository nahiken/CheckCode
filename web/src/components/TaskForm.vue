<!-- web/src/components/TaskForm.vue —— 创建任务表单。
只负责:填任务 + 模式开关 → POST /api/tasks → emit 'created'(SSE 订阅由 App 统一持有,
新建/列表选择/resume 都在 App 一处订阅,避免多订阅串流)。M9 W1:加严格模式/自动放行开关——
关掉自动放行即进入浏览器审批(policy decision="ask" 时任务在审批面板等人工允许/拒绝)。 -->
<script setup lang="ts">
import { ref } from 'vue'
import { createTask } from '../api/sse'

const emit = defineEmits<{
  (e: 'created', taskId: string): void
  (e: 'error', message: string): void
}>()

// 可注入 fetch(测试 mock;生产走全局 fetch)
const props = withDefaults(defineProps<{ fetchImpl?: typeof fetch }>(), {})

const task = ref('')
const strict = ref(false)
// M9 W1 默认 False(安全默认):审批是缺省姿态,勾上「自动放行」才跳过人工
const autoApprove = ref(false)
const taskId = ref('')
const submitting = ref(false)
const error = ref('')

async function onSubmit() {
  if (!task.value.trim() || submitting.value) return
  submitting.value = true
  error.value = ''
  try {
    const res = await createTask(task.value, {
      fetchImpl: props.fetchImpl,
      strict: strict.value,
      autoApprove: autoApprove.value,
    })
    if (res.error) {
      error.value = res.error
      emit('error', res.error)
      return
    }
    taskId.value = res.task_id
    emit('created', res.task_id) // App 据此订阅 SSE(单一订阅点)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
    emit('error', error.value)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <form class="task-form" @submit.prevent="onSubmit">
    <input
      v-model="task"
      data-testid="task-input"
      placeholder="输入任务描述,例如:修复 math_utils.py"
      :disabled="submitting"
    />
    <button type="submit" :disabled="submitting || !task.trim()">
      {{ submitting ? '运行中…' : '运行任务' }}
    </button>
    <label class="opt" title="严格模式:先 Planner 生成计划,写操作逐项审批">
      <input v-model="strict" data-testid="strict-box" type="checkbox" /> 严格
    </label>
    <label class="opt" title="自动放行:默认关闭 → 越权/写操作弹审批面板人工允许/拒绝;勾上才跳过人工">
      <input v-model="autoApprove" data-testid="autoapprove-box" type="checkbox" /> 自动放行
    </label>
    <span v-if="taskId" class="task-id">task: {{ taskId }}</span>
  </form>
  <p v-if="error" class="error" data-testid="task-error">{{ error }}</p>
</template>

<style scoped>
.task-form {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 12px;
}
.task-form input {
  flex: 1;
  padding: 8px;
}
.task-form button {
  padding: 8px 16px;
  cursor: pointer;
  white-space: nowrap;
}
.opt {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 13px;
  color: #444;
  cursor: pointer;
  white-space: nowrap;
}
.task-id {
  font-size: 12px;
  color: #666;
}
.error {
  color: #c0392b;
  padding: 0 12px;
}
</style>
