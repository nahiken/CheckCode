// web/src/api/sse.ts —— SSE 事件流消费层。
//
// 职责:订阅 /api/tasks/{id}/events,把 SSE 事件解析为结构化的 WorkbenchEvent,
// 按事件类型分发(对话流/Diff/验证报告各自消费)。可注入 fetch 便于测试 mock。
//
// 设计(要点:前端如何增量渲染 Tool Use 流):
// - 后端事件 = AgentLoop.StepKind + 任务级(verify/resume/report/error)。
// - 本层只做「解析 + 分发」,不关心具体渲染;组件订阅 onEvent 回调。
// - 用 fetch 流(非 EventSource)解析:后端返回的是纯 data: 帧,
//   EventSource 也够,但 fetch 流可在测试中完全 mock,且支持非 GET 场景。

import { z } from 'zod'

export interface WorkbenchEvent {
  event: string
  data: Record<string, unknown>
}

export type EventHandler = (ev: WorkbenchEvent) => void

export interface SseOptions {
  fetchImpl?: typeof fetch
  signal?: AbortSignal
}

/** createTask 响应 schema(Zod 校验:响应结构不符 → 抛错,前端捕获显示)。 */
const CreateTaskResponse = z.object({
  task_id: z.string(),
  sse_url: z.string(),
  error: z.string().optional(),
})/** 解析一段 SSE 文本为事件列表(按 \n\n 分帧,过滤非 data 行)。 */
export function parseSseChunk(chunk: string): WorkbenchEvent[] {
  const frames = chunk.split('\n\n')
  const out: WorkbenchEvent[] = []
  for (const frame of frames) {
    const dataLines = frame
      .split('\n')
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trimStart())
    if (dataLines.length === 0) continue
    try {
      const payload = JSON.parse(dataLines.join('\n')) as WorkbenchEvent
      if (payload && typeof payload.event === 'string') out.push(payload)
    } catch {
      // 忽略解析失败帧(保持流不中断)
    }
  }
  return out
}

/**
 * 订阅任务 SSE 流。onEvent 每收到一个事件回调;返回关闭函数。
 * 内部用 fetch 流逐块读,遇到 report/error 终止事件即停止。
 */
export function subscribeTaskEvents(
  taskId: string,
  onEvent: EventHandler,
  opts: SseOptions = {},
): () => void {
  const fetchImpl = opts.fetchImpl ?? globalThis.fetch
  const controller = new AbortController()
  const signal = opts.signal ?? controller.signal
  let cancelled = false

  void (async () => {
    try {
      const resp = await fetchImpl(`/api/tasks/${taskId}/events`, {
        headers: { Accept: 'text/event-stream' },
        signal,
      })
      if (!resp.ok || !resp.body) {
        onEvent({ event: 'error', data: { message: `HTTP ${resp.status}` } })
        return
      }
      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        // 只解析「以 \n\n 结束的完整帧」;未终止的尾部保留到下一块(避免重复解析)
        let sep = buffer.indexOf('\n\n')
        while (sep >= 0) {
          const frame = buffer.slice(0, sep + 2)
          buffer = buffer.slice(sep + 2)
          for (const ev of parseSseChunk(frame)) {
            onEvent(ev)
            if (ev.event === 'report' || ev.event === 'error') return
          }
          sep = buffer.indexOf('\n\n')
        }
      }
    } catch (err) {
      if (!cancelled) {
        const msg = err instanceof Error ? err.message : String(err)
        onEvent({ event: 'error', data: { message: msg } })
      }
    }
  })()

  return () => {
    cancelled = true
    controller.abort()
  }
}

/**
 * 创建任务:POST /api/tasks,返回 {task_id, sse_url}。
 * opts.task/strict/auto_approve 随请求体提交(M9 W1:strict+非 auto_approve → 浏览器审批)。
 * auto_approve 默认 **false**(安全默认:审批是缺省姿态,自动放行需显式请求),
 * 与后端 api/app.py 的 _TaskRequest 默认值保持一致。
 */
export interface CreateTaskOptions {
  fetchImpl?: typeof fetch
  task?: string
  strict?: boolean
  autoApprove?: boolean
}

export async function createTask(
  task: string,
  opts: CreateTaskOptions = {},
): Promise<{ task_id: string; sse_url: string; error?: string }> {
  const fetchImpl = opts.fetchImpl ?? globalThis.fetch
  const resp = await fetchImpl('/api/tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      task,
      strict: opts.strict ?? false,
      auto_approve: opts.autoApprove ?? false,
    }),
  })
  const parsed = CreateTaskResponse.safeParse(await resp.json())
  if (!parsed.success) {
    throw new Error('创建任务响应格式无效')
  }
  return parsed.data
}

/** 回滚任务修改:POST /api/tasks/{id}/rollback。 */
export async function rollbackTask(
  taskId: string,
  opts: { fetchImpl?: typeof fetch } = {},
): Promise<{ status: string; message?: string; error?: string }> {
  const fetchImpl = opts.fetchImpl ?? globalThis.fetch
  const resp = await fetchImpl(`/api/tasks/${taskId}/rollback`, { method: 'POST' })
  return (await resp.json()) as { status: string; message?: string; error?: string }
}

/** 审批决策(允许/拒绝):POST /api/tasks/{id}/approvals/{approvalId}。 */
export async function approveTask(
  taskId: string,
  approvalId: string,
  approve: boolean,
  opts: { fetchImpl?: typeof fetch } = {},
): Promise<{ status: string; approved?: boolean; error?: string }> {
  const fetchImpl = opts.fetchImpl ?? globalThis.fetch
  const resp = await fetchImpl(`/api/tasks/${taskId}/approvals/${approvalId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ approve }),
  })
  return (await resp.json()) as { status: string; approved?: boolean; error?: string }
}

/** 计划审批决策(M10):POST /api/tasks/{id}/plan-approvals/{approvalId}。 */
export async function approvePlan(
  taskId: string,
  approvalId: string,
  approve: boolean,
  opts: { fetchImpl?: typeof fetch } = {},
): Promise<{ status: string; approved?: boolean; error?: string }> {
  const fetchImpl = opts.fetchImpl ?? globalThis.fetch
  const resp = await fetchImpl(`/api/tasks/${taskId}/plan-approvals/${approvalId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ approve }),
  })
  return (await resp.json()) as { status: string; approved?: boolean; error?: string }
}

/** 恢复失败任务:POST /api/tasks/{id}/resume(复用仓库 + 会话,后台续跑)。 */
export async function resumeTask(
  taskId: string,
  opts: { fetchImpl?: typeof fetch } = {},
): Promise<{ task_id: string; status?: string; error?: string; sse_url?: string }> {
  const fetchImpl = opts.fetchImpl ?? globalThis.fetch
  const resp = await fetchImpl(`/api/tasks/${taskId}/resume`, { method: 'POST' })
  return (await resp.json()) as { task_id: string; status?: string; error?: string; sse_url?: string }
}

/** 进行中的审批概要(awaiting 行据此渲染允许/拒绝按钮)。 */
export interface PendingApproval {
  approval_id: string
  kind: 'tool' | 'plan'
  // 逐工具审批:工具名;计划审批为 null(被审对象在 plan 字段)
  tool_name: string | null
  reason: string
  // 计划审批的被审计划;逐工具审批为 null
  plan: { files?: string[]; commands?: string[] } | null
}

/** 任务列表:GET /api/tasks。 */
export interface TaskSummary {
  task_id: string
  status: string
  error: string | null
  task?: string
  pending_approvals?: PendingApproval[]
  // M10:计划审批摘要(awaiting_plan_approval 行据此渲染批准/拒绝)
  pending_plan_approvals?: PendingApproval[]
}

export async function listTasks(
  opts: { fetchImpl?: typeof fetch } = {},
): Promise<TaskSummary[]> {
  const fetchImpl = opts.fetchImpl ?? globalThis.fetch
  const resp = await fetchImpl('/api/tasks')
  const data = await resp.json()
  return Array.isArray(data) ? (data as TaskSummary[]) : []
}
