// @vitest-environment node
// web/tests/sse-http.test.ts —— MSW 接口测试:真实 HTTP 层契约。
// PHASED_PLAN「Vitest + MSW 接口测试」:用 MSW node 拦截 fetch,验证
// createTask/listTasks/subscribeTaskEvents 与后端 HTTP 契约一致(不经组件)。
// 注意:此文件用 node 环境(纯 HTTP/stream,无 DOM)。jsdom 的 AbortController
// 不是 MSW 内部 undici 的 AbortSignal → fetch 校验失败;node 全局与其一致。
import { describe, expect, it, beforeAll, afterAll, afterEach } from 'vitest'
import { setupServer } from 'msw/node'
import { handlers, sseEvents, API_ORIGIN } from './msw-handlers'
import { approveTask, createTask, listTasks, resumeTask, subscribeTaskEvents } from '../src/api/sse'

const server = setupServer(...handlers)

beforeAll(() => server.listen())
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

// node 的 fetch 需要绝对 URL(无 location.origin);前端生产走浏览器相对路径,
// 测试用同源绝对 URL 等效。通过 fetchImpl 注入,保证 MSW 拦截生效。
const fetchImpl = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> =>
  fetch(input.toString().startsWith('http') ? input : `${API_ORIGIN}${input}`, init)

describe('MSW 接口测试(HTTP 契约)', () => {
  it('createTask:POST /api/tasks → 202 + task_id + sse_url(Zod 校验)', async () => {
    const res = await createTask('修复 math_utils', { fetchImpl })
    expect(res.task_id).toBe('msw-1')
    expect(res.sse_url).toBe('/api/tasks/msw-1/events')
  })

  it('listTasks:GET /api/tasks → 任务数组', async () => {
    const tasks = await listTasks({ fetchImpl })
    expect(tasks).toEqual([{ task_id: 'msw-1', status: 'done', error: null }])
  })

  it('subscribeTaskEvents:SSE 流经真实 HTTP → 逐事件回调直到 report', async () => {
    const seen: string[] = []
    await new Promise<void>((resolve) => {
      subscribeTaskEvents('msw-1', (ev) => {
        seen.push(ev.event)
        if (ev.event === 'report') resolve()
      }, { fetchImpl })
    })
    expect(seen).toEqual(sseEvents.map((e) => e.event))
  })

  it('approveTask:POST /api/tasks/:id/approvals/:aid → resolved', async () => {
    const res = await approveTask('msw-1', 'ap-1', true, { fetchImpl })
    expect(res).toEqual({ status: 'resolved', approved: true })
  })

  it('resumeTask:POST /api/tasks/:id/resume → resumed + sse_url', async () => {
    const res = await resumeTask('msw-1', { fetchImpl })
    expect(res.status).toBe('resumed')
    expect(res.sse_url).toBe('/api/tasks/msw-1/events')
  })
})
