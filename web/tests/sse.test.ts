// web/tests/sse.test.ts —— SSE 解析与订阅逻辑单测。
import { describe, expect, it, vi } from 'vitest'
import { parseSseChunk, subscribeTaskEvents } from '../src/api/sse'

describe('parseSseChunk', () => {
  it('解析单条 data 帧', () => {
    const evs = parseSseChunk('data: {"event":"thinking","data":{"content":"分析"}}\n\n')
    expect(evs).toHaveLength(1)
    expect(evs[0].event).toBe('thinking')
    expect(evs[0].data.content).toBe('分析')
  })

  it('解析多条帧(空行分隔)', () => {
    const evs = parseSseChunk(
      'data: {"event":"a"}\n\ndata: {"event":"b","data":{}}\n\n',
    )
    expect(evs.map((e) => e.event)).toEqual(['a', 'b'])
  })

  it('忽略非 data 行与注释', () => {
    const evs = parseSseChunk(': keepalive\n\ndata: {"event":"c"}\n\n')
    expect(evs.map((e) => e.event)).toEqual(['c'])
  })

  it('忽略损坏 JSON 帧(流不中断)', () => {
    const evs = parseSseChunk('data: {broken\n\ndata: {"event":"d"}\n\n')
    expect(evs.map((e) => e.event)).toEqual(['d'])
  })
})

describe('subscribeTaskEvents', () => {
  it('解析流式块并回调,report 后停止', async () => {
    const onEvent = vi.fn()
    // mock fetch:先给两帧,再给 report 帧
    const body = new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('data: {"event":"thinking"}\n\ndata: {"event":"assistant","data":{"content":"hi"}}\n\n'))
        controller.enqueue(new TextEncoder().encode('data: {"event":"report","data":{"passed":true}}\n\n'))
        controller.close()
      },
    })
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, body, status: 200 })
    await new Promise<void>((resolve) => {
      subscribeTaskEvents('t1', (ev) => {
        onEvent(ev)
        if (ev.event === 'report') resolve()
      }, { fetchImpl: fetchMock as unknown as typeof fetch })
    })
    expect(onEvent).toHaveBeenCalledTimes(3)
    expect(onEvent.mock.calls.map((c) => c[0].event)).toEqual([
      'thinking',
      'assistant',
      'report',
    ])
  })

  it('HTTP 错误 → error 事件', async () => {
    const onEvent = vi.fn()
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, body: null, status: 500 })
    await new Promise<void>((resolve) => {
      subscribeTaskEvents('t1', (ev) => {
        onEvent(ev)
        resolve()
      }, { fetchImpl: fetchMock as unknown as typeof fetch })
    })
    expect(onEvent).toHaveBeenCalledTimes(1)
    expect(onEvent.mock.calls[0][0].event).toBe('error')
  })
})
