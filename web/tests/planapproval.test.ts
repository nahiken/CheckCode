// web/tests/planapproval.test.ts —— M10 计划审批面板测试:
// PlanApprovalPanel 渲染计划文件/命令 + 批准/拒绝 POST plan-approvals,
// 以及 App 对 plan_approval SSE 事件的订阅(收到 → 面板渲染)。
import { describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import PlanApprovalPanel from '../src/components/PlanApprovalPanel.vue'
import App from '../src/App.vue'

describe('PlanApprovalPanel', () => {
  const plan = {
    approval_id: 'pl-1',
    files: ['math_utils.py', 'app.py'],
    commands: ['python -m pytest tests/ -q'],
  }

  it('渲染计划文件与命令', () => {
    const wrapper = mount(PlanApprovalPanel, {
      props: { taskId: 't1', plan },
    })
    expect(wrapper.find('[data-testid="plan-panel"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="plan-files"]').text()).toContain('math_utils.py')
    expect(wrapper.find('[data-testid="plan-files"]').text()).toContain('app.py')
    expect(wrapper.find('[data-testid="plan-commands"]').text()).toContain('pytest')
  })

  it('点批准 → POST plan-approvals approve=true → emit resolved', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ json: async () => ({ status: 'resolved' }) })
    const wrapper = mount(PlanApprovalPanel, {
      props: { taskId: 't1', plan, fetchImpl: fetchMock as unknown as typeof fetch },
    })
    await wrapper.find('[data-testid="plan-approve-btn"]').trigger('click')
    await flushPromises()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const url = String(fetchMock.mock.calls[0][0])
    expect(url).toContain('/api/tasks/t1/plan-approvals/pl-1')
    expect(wrapper.emitted('resolved')).toBeTruthy()
  })

  it('点拒绝 → POST approve=false', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ json: async () => ({ status: 'resolved' }) })
    const wrapper = mount(PlanApprovalPanel, {
      props: { taskId: 't1', plan, fetchImpl: fetchMock as unknown as typeof fetch },
    })
    await wrapper.find('[data-testid="plan-deny-btn"]').trigger('click')
    await flushPromises()
    const body = JSON.parse(String(fetchMock.mock.calls[0][1].body))
    expect(body.approve).toBe(false)
  })
})

describe('App:plan_approval SSE 事件驱动计划面板', () => {
  it('收到 plan_approval 事件 → 状态 awaiting_plan_approval + 面板渲染', async () => {
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
        const body = new ReadableStream({
          start(controller) {
            controller.enqueue(new TextEncoder().encode(
              'data: {"event":"plan_approval","data":{"approval_id":"pl-1","files":["math_utils.py"],"commands":["pytest"]}}\n\n',
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

      expect(wrapper.find('[data-testid="status"]').text()).toContain('awaiting_plan_approval')
      expect(wrapper.find('[data-testid="plan-panel"]').exists()).toBe(true)
      expect(wrapper.text()).toContain('math_utils.py')
    } finally {
      globalThis.fetch = origFetch
      wrapper.unmount()
    }
  })

  it('plan_approval 后收到 approval → 计划面板消失,工具面板出现(不残留)', async () => {
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
        const body = new ReadableStream({
          start(controller) {
            const enc = new TextEncoder()
            controller.enqueue(enc.encode(
              'data: {"event":"plan_approval","data":{"approval_id":"pl-1","files":["math_utils.py"],"commands":["pytest"]}}\n\n',
            ))
            controller.enqueue(enc.encode(
              'data: {"event":"approval","data":{"approval_id":"ap-1","tool_name":"write_file","reason":"越权","args":{}}}\n\n',
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

      // 已进入工具审批 → 计划面板已清空
      expect(wrapper.find('[data-testid="status"]').text()).toContain('awaiting_approval')
      expect(wrapper.find('[data-testid="plan-panel"]').exists()).toBe(false)
      expect(wrapper.find('[data-testid="approval-panel"]').exists()).toBe(true)
    } finally {
      globalThis.fetch = origFetch
      wrapper.unmount()
    }
  })
})
