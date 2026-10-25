// web/tests/tasklist-approval.test.ts —— M9 W1 组件测试:
// TaskList awaiting_approval 行「允许/拒绝」、done/error 行「恢复」,以及 App 对
// approval SSE 事件的订阅(收到 approval → 状态 awaiting_approval → 面板渲染)。
import { describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import TaskList from '../src/components/TaskList.vue'
import App from '../src/App.vue'
import type { TaskSummary } from '../src/api/sse'

function taskListFetch(data: TaskSummary[]) {
  return vi.fn().mockResolvedValue({ json: async () => data })
}

const awaitingTasks: TaskSummary[] = [
  {
    task_id: 't-await',
    status: 'awaiting_approval',
    error: null,
    task: '修复 math_utils.py',
    pending_approvals: [
      { approval_id: 'ap-1', kind: 'tool', tool_name: 'write_file', reason: '越权写文件需确认', plan: null },
    ],
  },
]

describe('TaskList 审批/恢复行内操作', () => {
  it('awaiting_approval 行渲染待审批工具与允许/拒绝按钮', async () => {
    const wrapper = mount(TaskList, {
      props: { fetchImpl: taskListFetch(awaitingTasks) as unknown as typeof fetch },
    })
    await flushPromises()
    expect(wrapper.text()).toContain('awaiting_approval')
    expect(wrapper.text()).toContain('write_file')
    expect(wrapper.find('[data-testid="approve-btn"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="deny-btn"]').exists()).toBe(true)
  })

  it('点允许 → POST approve → 刷新列表', async () => {
    const approveMock = vi.fn().mockResolvedValue({ json: async () => ({ status: 'resolved' }) })
    const listMock = vi.fn()
      .mockResolvedValueOnce({ json: async () => awaitingTasks }) // 初始
      .mockResolvedValueOnce({ json: async () => [{ task_id: 't-await', status: 'running', error: null }] }) // 刷新后
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (String(url).includes('/approvals/')) return approveMock()
      return listMock()
    })
    const wrapper = mount(TaskList, {
      props: { fetchImpl: fetchMock as unknown as typeof fetch },
    })
    await flushPromises()
    await wrapper.find('[data-testid="approve-btn"]').trigger('click')
    await flushPromises()
    expect(approveMock).toHaveBeenCalledTimes(1)
    // 列表已刷新(不再是 awaiting)
    expect(wrapper.text()).not.toContain('awaiting_approval')
    expect(wrapper.text()).toContain('running')
  })

  it('done 行渲染恢复按钮,点恢复 → POST resume → emit resumed', async () => {
    const resumeMock = vi.fn().mockResolvedValue({
      json: async () => ({ task_id: 't-done', status: 'resumed', sse_url: '/api/tasks/t-done/events' }),
    })
    const listMock = vi.fn().mockResolvedValue({
      json: async () => [{ task_id: 't-done', status: 'done', error: null }],
    })
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (String(url).includes('/resume')) return resumeMock()
      return listMock()
    })
    const wrapper = mount(TaskList, {
      props: { fetchImpl: fetchMock as unknown as typeof fetch },
    })
    await flushPromises()
    expect(wrapper.find('[data-testid="resume-btn"]').exists()).toBe(true)
    await wrapper.find('[data-testid="resume-btn"]').trigger('click')
    await flushPromises()
    expect(resumeMock).toHaveBeenCalledTimes(1)
    expect(wrapper.emitted('resumed')).toBeTruthy()
  })
})

describe('App:approval SSE 事件驱动审批面板', () => {
  it('收到 approval 事件 → 状态 awaiting_approval + 面板渲染允许/拒绝', async () => {
    // App 不把 fetchImpl 下传,子组件(TaskForm/TaskList/订阅)fallback 到全局 fetch。
    // stub globalThis.fetch 按 URL 分派:createTask/SSE/listTasks。
    const fetchMock = vi.fn((input: RequestInfo, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? 'GET'
      if (method === 'POST' && url === '/api/tasks') {
        return Promise.resolve({ json: async () => ({ task_id: 't1', sse_url: '/api/tasks/t1/events' }) })
      }
      if (method === 'GET' && url === '/api/tasks') {
        return Promise.resolve({ json: async () => [] })
      }
      if (method === 'GET' && url.includes('/events')) {
        // SSE:先发 approval 事件(非终止,流保持打开)→ 面板出现
        const body = new ReadableStream({
          start(controller) {
            controller.enqueue(new TextEncoder().encode(
              'data: {"event":"approval","data":{"approval_id":"ap-1","tool_name":"write_file","reason":"确认","args":{"path":"math_utils.py"}}}\n\n',
            ))
          },
        })
        return Promise.resolve({ ok: true, body, status: 200 })
      }
      return Promise.resolve({ json: async () => ({}) })
    })

    const origFetch = globalThis.fetch
    globalThis.fetch = fetchMock as unknown as typeof fetch
    const wrapper = mount(App)
    try {
      await wrapper.find('[data-testid="task-input"]').setValue('修复')
      await wrapper.find('form').trigger('submit')
      await new Promise((r) => setTimeout(r, 80))

      expect(wrapper.find('[data-testid="status"]').text()).toContain('awaiting_approval')
      expect(wrapper.find('[data-testid="approval-panel"]').exists()).toBe(true)
      expect(wrapper.text()).toContain('write_file')
    } finally {
      globalThis.fetch = origFetch
      wrapper.unmount()
    }
  })
})
