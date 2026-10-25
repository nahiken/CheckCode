<!-- web/src/components/ConversationStream.vue —— Agent 对话流渲染。
按事件类型分别渲染:thinking(思考)/assistant(文本)/tool_exec_start+tool_result(工具卡片)/verify(验证结果)。
设计:从 props.events **派生**展示状态(computed),不做本地可变副本——增量事件自然生效,无重复消费。 -->
<script setup lang="ts">
import { computed } from 'vue'
import type { WorkbenchEvent } from '../api/sse'

const props = defineProps<{ events: WorkbenchEvent[] }>()

const thinking = computed(() =>
  props.events.filter((e) => e.event === 'thinking').map((e) => String(e.data.content ?? '')),
)

const messages = computed(() =>
  props.events.filter((e) => e.event === 'assistant').map((e) => String(e.data.content ?? '')),
)

const tools = computed(() => {
  const out: Array<{ id: string; name: string; result: string; isError: boolean }> = []
  let seq = 0
  for (const e of props.events) {
    if (e.event === 'tool_exec_start') {
      // 后端发 tools: string[](工具名列表);每个工具一个卡片,以 tool_call_id 建键
      const names = Array.isArray(e.data.tools) ? e.data.tools.map(String) : []
      if (names.length === 0 && typeof e.data.tool === 'string') names.push(e.data.tool)
      for (const name of names) {
        const id = typeof e.data.tool_call_id === 'string' ? e.data.tool_call_id : `tool-${seq++}`
        out.push({ id, name, result: '', isError: false })
      }
    } else if (e.event === 'tool_result') {
      // 按 tool_call_id 配对(而非位置):并发工具乱序到达也挂对卡片
      const tid = e.data.tool_call_id
      const target =
        typeof tid === 'string'
          ? out.find((t) => t.id === tid)
          : out[out.length - 1]
      if (target) {
        target.result = typeof e.data.content === 'string' ? e.data.content : String(e.data.content ?? '')
        target.isError = e.data.is_error === true
      }
    }
  }
  return out
})

const verifies = computed(() =>
  props.events
    .filter((e) => e.event === 'verify')
    .map((e) => {
      const r = e.data.result as Record<string, unknown> | undefined
      return {
        round: typeof e.data.round === 'number' ? e.data.round : 0,
        passed: e.data.passed === true,
        output: (r && typeof r.output === 'string' ? r.output : '').slice(0, 400),
      }
    }),
)

defineExpose({ thinking, messages, tools, verifies })
</script>

<template>
  <div class="conversation" data-testid="conversation">
    <div v-for="(t, i) in thinking" :key="'think-' + i" class="thinking">
      <strong>思考:</strong> {{ t }}
    </div>
    <div v-for="(m, i) in messages" :key="'msg-' + i" class="assistant">
      {{ m }}
    </div>
    <div v-for="t in tools" :key="t.id" class="tool" :class="{ error: t.isError }">
      <strong>工具:</strong> {{ t.name }}
      <pre v-if="t.result">{{ t.result }}</pre>
    </div>
    <div v-for="(v, i) in verifies" :key="'verify-' + i" class="verify" :class="{ pass: v.passed }">
      <strong>验证第{{ v.round }}轮:</strong> {{ v.passed ? '通过' : '失败' }}
      <pre v-if="!v.passed">{{ v.output }}</pre>
    </div>
  </div>
</template>

<style scoped>
.conversation {
  padding: 12px;
  font-family: monospace;
  font-size: 13px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.thinking {
  color: #8e44ad;
  border-left: 3px solid #8e44ad;
  padding-left: 8px;
}
.assistant {
  color: #2c3e50;
}
.tool {
  border-left: 3px solid #2980b9;
  padding-left: 8px;
}
.tool.error {
  border-left-color: #c0392b;
}
.tool pre {
  white-space: pre-wrap;
  background: #f5f5f5;
  padding: 6px;
  border-radius: 4px;
  max-height: 200px;
  overflow-y: auto;
}
.verify {
  border-left: 3px solid #f39c12;
  padding-left: 8px;
}
.verify.pass {
  border-left-color: #27ae60;
}
</style>
