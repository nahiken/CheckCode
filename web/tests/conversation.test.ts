// web/tests/conversation.test.ts —— 对话流组件渲染单测。
import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import ConversationStream from '../src/components/ConversationStream.vue'
import ReportView from '../src/components/ReportView.vue'

function ev(event: string, data: Record<string, unknown> = {}) {
  return { event, data }
}

describe('ConversationStream', () => {
  it('渲染思考/文本/工具/验证四类事件', async () => {
    const wrapper = mount(ConversationStream, {
      props: {
        events: [
          ev('thinking', { content: '先读文件' }),
          ev('assistant', { content: '我来看代码' }),
          ev('tool_exec_start', { tools: ['read_file'] }),
          ev('tool_result', { content: '{ok}', is_error: false }),
          ev('verify', { round: 1, passed: true }),
        ],
      },
    })
    const text = wrapper.text()
    expect(text).toContain('先读文件')
    expect(text).toContain('我来看代码')
    expect(text).toContain('read_file')
    expect(text).toContain('{ok}')
    expect(text).toContain('验证第1轮')
    expect(text).toContain('通过')
  })

  it('工具失败渲染 error 样式', async () => {
    const wrapper = mount(ConversationStream, {
      props: {
        events: [
          ev('tool_exec_start', { tools: ['write_file'] }),
          ev('tool_result', { content: 'boom', is_error: true }),
        ],
      },
    })
    expect(wrapper.find('.tool.error').exists()).toBe(true)
  })

  it('并发工具乱序:tool_result 按 tool_call_id 挂到对应卡片', async () => {
    const wrapper = mount(ConversationStream, {
      props: {
        events: [
          ev('tool_exec_start', { tool_call_id: 'call-1', tools: ['read_file'] }),
          ev('tool_exec_start', { tool_call_id: 'call-2', tools: ['write_file'] }),
          // 乱序到达:call-2 的结果先到
          ev('tool_result', { tool_call_id: 'call-2', content: '写入成功', is_error: false }),
          ev('tool_result', { tool_call_id: 'call-1', content: '文件内容', is_error: false }),
        ],
      },
    })
    const cards = wrapper.findAll('.tool')
    // 每张卡片结果正确:卡片 1 是 read_file + 文件内容,卡片 2 是 write_file + 写入成功
    expect(cards[0].text()).toContain('read_file')
    expect(cards[0].text()).toContain('文件内容')
    expect(cards[0].text()).not.toContain('写入成功')
    expect(cards[1].text()).toContain('write_file')
    expect(cards[1].text()).toContain('写入成功')
    expect(cards[1].text()).not.toContain('文件内容')
  })
})

describe('ReportView', () => {
  it('渲染 Diff 与剩余风险', () => {
    const wrapper = mount(ReportView, {
      props: {
        report: {
          passed: true,
          diff: '+def add(a,b):',
          changed_files: ['math_utils.py'],
          remaining_risk: '低:所有验证命令通过',
        },
        taskId: 't1',
      },
    })
    expect(wrapper.text()).toContain('通过')
    expect(wrapper.text()).toContain('math_utils.py')
    expect(wrapper.text()).toContain('+def add(a,b):')
    expect(wrapper.text()).toContain('低:所有验证命令通过')
  })

  it('无报告时显示占位', () => {
    const wrapper = mount(ReportView, { props: { report: null, taskId: null } })
    expect(wrapper.text()).toContain('任务完成后显示')
  })

  it('回滚按钮触发 rollback 请求', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      json: async () => ({ status: 'rolled_back', message: '已回滚到 abc' }),
    })
    const wrapper = mount(ReportView, {
      props: {
        report: { passed: true, diff: '+x', changed_files: [], remaining_risk: '低' },
        taskId: 't1',
      },
      global: { provide: {} },
      attrs: {
        onRolledback: () => {},
      },
    })
    // 注入 fetchMock:通过全局 fetch 替换
    const origFetch = globalThis.fetch
    globalThis.fetch = fetchMock as unknown as typeof fetch
    try {
      await wrapper.find('[data-testid="rollback-btn"]').trigger('click')
      await new Promise((r) => setTimeout(r, 20))
      expect(fetchMock).toHaveBeenCalledWith('/api/tasks/t1/rollback', { method: 'POST' })
      expect(wrapper.text()).toContain('已回滚')
    } finally {
      globalThis.fetch = origFetch
    }
  })

  it('计划被拒的任务不显示回滚按钮(无分支可回滚)', () => {
    // 审计 B7:计划被拒从未创建分支,回滚的 `git branch -f` 会凭空把它建出来。
    const wrapper = mount(ReportView, {
      props: {
        report: { plan_rejected: true, changed_files: [], diff: '', remaining_risk: '拒绝' },
        taskId: 't1',
      },
    })
    expect(wrapper.find('[data-testid="rollback-btn"]').exists()).toBe(false)
  })

  it('只读分析任务不显示回滚按钮', () => {
    const wrapper = mount(ReportView, {
      props: {
        report: { readonly: true, changed_files: [], diff: '', remaining_risk: '只读' },
        taskId: 't1',
      },
    })
    expect(wrapper.find('[data-testid="rollback-btn"]').exists()).toBe(false)
  })
})
