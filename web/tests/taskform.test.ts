// web/tests/taskform.test.ts —— 任务表单 + 创建流程单测(fetch mock)。
import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import TaskForm from '../src/components/TaskForm.vue'
import { createTask } from '../src/api/sse'

describe('createTask', () => {
  it('POST /api/tasks 返回 task_id', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      json: async () => ({ task_id: 'abc123', sse_url: '/api/tasks/abc123/events' }),
    })
    const res = await createTask('修复', { fetchImpl: fetchMock as unknown as typeof fetch })
    expect(res.task_id).toBe('abc123')
    expect(fetchMock).toHaveBeenCalledWith('/api/tasks', expect.objectContaining({ method: 'POST' }))
  })

  it('createTask 默认带 auto_approve=false(安全默认:需人工审批)+ strict=false', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      json: async () => ({ task_id: 'abc', sse_url: '/api/tasks/abc/events' }),
    })
    await createTask('修复', { fetchImpl: fetchMock as unknown as typeof fetch })
    const [, init] = fetchMock.mock.calls[0]
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      task: '修复', strict: false, auto_approve: false,
    })
  })

  it('createTask 可指定 autoApprove=true(显式跳过人工审批)', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      json: async () => ({ task_id: 'abc', sse_url: '/api/tasks/abc/events' }),
    })
    await createTask('修复', { fetchImpl: fetchMock as unknown as typeof fetch, strict: true, autoApprove: true })
    const [, init] = fetchMock.mock.calls[0]
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      task: '修复', strict: true, auto_approve: true,
    })
  })
})

describe('TaskForm', () => {
  it('空输入时按钮禁用', () => {
    const wrapper = mount(TaskForm)
    const btn = wrapper.find('button')
    expect(btn.attributes('disabled')).toBeDefined()
  })

  it('提交后 emit created(task_id)(SSE 订阅由 App 统一持有)', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      json: async () => ({ task_id: 't1', sse_url: '/api/tasks/t1/events' }),
    })
    const wrapper = mount(TaskForm, {
      props: { fetchImpl: fetchMock as unknown as typeof fetch },
    })
    await wrapper.find('input').setValue('修复 bug')
    await wrapper.find('form').trigger('submit')
    await new Promise((r) => setTimeout(r, 20))
    expect(wrapper.emitted('created')).toBeTruthy()
    expect(wrapper.emitted('created')![0][0]).toBe('t1')
  })

  it('带严格/自动放行控件:勾上自动放行时请求体 auto_approve=true', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      json: async () => ({ task_id: 't2', sse_url: '/api/tasks/t2/events' }),
    })
    const wrapper = mount(TaskForm, {
      props: { fetchImpl: fetchMock as unknown as typeof fetch },
    })
    await wrapper.find('[data-testid="strict-box"]').setValue(true)
    await wrapper.find('[data-testid="autoapprove-box"]').setValue(true)
    await wrapper.find('input').setValue('修复')
    await wrapper.find('form').trigger('submit')
    await new Promise((r) => setTimeout(r, 20))
    const [, init] = fetchMock.mock.calls[0]
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      task: '修复', strict: true, auto_approve: true,
    })
  })
})
