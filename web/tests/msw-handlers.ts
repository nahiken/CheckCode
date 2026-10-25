// web/tests/msw-handlers.ts —— MSW 接口测试 handler 定义。
// 覆盖 WhyCode 后端 HTTP 契约:任务创建 / 任务列表 / SSE 事件流 / 审批 / 恢复。
// 注意:MSW node 拦截的 fetch 要求绝对 URL(node 的 undici 无 origin)。
// 前端源码用浏览器相对路径(/api/tasks),测试传入同源绝对 URL(等效)。
import { http, HttpResponse } from 'msw'

export const API_ORIGIN = 'http://localhost:8000'

// 会话级 SSE 事件序列:每订阅一次发一轮事件(thinking → assistant → report)
export const sseEvents = [
  { event: 'thinking', data: { content: '分析' } },
  { event: 'assistant', data: { content: '我来修' } },
  { event: 'report', data: { passed: true, diff: '+def add(a,b):', changed_files: ['math_utils.py'], remaining_risk: '低:所有验证命令通过' } },
]

export const handlers = [
  http.post(`${API_ORIGIN}/api/tasks`, async () => {
    return HttpResponse.json({ task_id: 'msw-1', sse_url: '/api/tasks/msw-1/events' }, { status: 202 })
  }),

  http.get(`${API_ORIGIN}/api/tasks`, () => {
    return HttpResponse.json([{ task_id: 'msw-1', status: 'done', error: null }])
  }),

  // SSE 流:纯 data: 帧,以 \n\n 分隔
  http.get(`${API_ORIGIN}/api/tasks/:id/events`, () => {
    const body = sseEvents.map((ev) => `data: ${JSON.stringify(ev)}\n\n`).join('')
    return new HttpResponse(body, {
      status: 200,
      headers: { 'Content-Type': 'text/event-stream' },
    })
  }),

  // 审批决策(允许/拒绝):返回 resolved
  http.post(`${API_ORIGIN}/api/tasks/:id/approvals/:aid`, () => {
    return HttpResponse.json({ status: 'resolved', approved: true })
  }),

  // 计划审批决策(M10):返回 resolved
  http.post(`${API_ORIGIN}/api/tasks/:id/plan-approvals/:aid`, () => {
    return HttpResponse.json({ status: 'resolved', approved: true })
  }),

  // 恢复任务:后台续跑,返回 sse_url
  http.post(`${API_ORIGIN}/api/tasks/:id/resume`, () => {
    return HttpResponse.json({ task_id: 'msw-1', status: 'resumed', sse_url: '/api/tasks/msw-1/events' })
  }),
]
