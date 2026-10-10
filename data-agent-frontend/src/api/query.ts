/**
 * 问答接口的 SSE 客户端（TDD 7.3）。
 *
 * **为什么不用 `EventSource`**：浏览器原生的 EventSource 只支持 GET，
 * 而后端的问答接口是 POST —— 问题原文要走请求体（见 `app/api/v1/query.py` 的说明）。
 * 把问题塞进 query string 能迁就 EventSource，代价是问题原文进浏览器历史与访问日志。
 * 所以这里用 `fetch` + `ReadableStream` 自己解析 SSE 帧：
 * 帧格式简单（`event:` 行 + `data:` 行 + 空行分隔），代价换来的是请求体和请求头都能自己控。
 *
 * 后端每一帧的 `data` 里都带 `type` 字段，与 `event:` 行同值。解析时以 `data.type`
 * 为准、`event:` 行只作为兜底 —— 单条帧被日志捞出来时不必回看上一行也知道是什么事件。
 */

import { ApiError, OK_CODE, notifySessionExpired } from './http'
import { getAccessToken } from '@/utils/storage'
import type {
  ApiResponse,
  DoneEvent,
  ProgressEvent,
  QueryRequest,
  ResultSet,
  StreamErrorEvent,
  VizEvent,
} from './types'

export interface QueryHandlers {
  onProgress?: (event: ProgressEvent) => void
  onSql?: (sql: string) => void
  onResult?: (result: ResultSet) => void
  onExplain?: (text: string) => void
  onViz?: (viz: VizEvent) => void
  /** 终端错误。后端保证一次问答最多推一条（error 只由服务层推）。 */
  onError?: (error: StreamErrorEvent) => void
  onDone?: (done: DoneEvent) => void
}

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? '/api/v1'

/**
 * 解析 SSE 帧文本。
 *
 * 按 SSE 规范，同名 `data:` 多行要以换行拼接；后端的 JSON 是单行，
 * 但反序列化时用得上这条兜底 —— 将来若某个字段里带了换行（比如解释文本被
 * 直接写在 data 里），少这一条就会把一帧解析成半截 JSON。
 */
function parseFrame(block: string): { event: string; data: string } | null {
  let event = ''
  const dataLines: string[] = []
  for (const rawLine of block.split('\n')) {
    const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine
    if (line.startsWith(':')) {
      continue // 注释行（心跳），忽略
    }
    if (line.startsWith('event:')) {
      event = line.slice(6).trim()
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).replace(/^ /, ''))
    }
  }
  if (!dataLines.length) {
    return null
  }
  return { event, data: dataLines.join('\n') }
}

function dispatch(payload: Record<string, unknown>, handlers: QueryHandlers): void {
  const type = (payload.type as string) ?? ''
  switch (type) {
    case 'progress':
      handlers.onProgress?.({
        step: String(payload.step ?? ''),
        status: payload.status === 'done' ? 'done' : 'start',
      })
      break
    case 'sql':
      handlers.onSql?.(String(payload.sql ?? ''))
      break
    case 'result':
      handlers.onResult?.({
        columns: (payload.columns as string[]) ?? [],
        rows: (payload.rows as unknown[][]) ?? [],
        row_count: Number(payload.row_count ?? 0),
        truncated: Boolean(payload.truncated),
      })
      break
    case 'explain':
      handlers.onExplain?.(String(payload.text ?? ''))
      break
    case 'viz':
      handlers.onViz?.({
        chart_type: (payload.chart_type as VizEvent['chart_type']) ?? null,
        message: String(payload.message ?? ''),
      })
      break
    case 'error':
      handlers.onError?.({
        code: Number(payload.code ?? 0),
        message: String(payload.message ?? '查询失败'),
      })
      break
    case 'done':
      handlers.onDone?.({
        correction_count: Number(payload.correction_count ?? 0),
        row_count: Number(payload.row_count ?? 0),
      })
      break
    default:
      // 不认识的事件类型不报错：后端加了新事件时，老前端应当忽略它继续跑，
      // 而不是把一次成功的问答变成一次失败
      break
  }
}

/** 用户主动终止时抛这个，调用方据此区分"取消"与"出错"（取消不该弹错误提示）。 */
export class QueryAbortedError extends Error {
  constructor() {
    super('已取消')
    this.name = 'QueryAbortedError'
  }
}

/**
 * 发起一次问答并消费事件流。
 *
 * `signal` 用于中止：用户点"停止生成"、或组件卸载、或直接点了新会话。
 * 中止后 fetch 的读取循环会抛 AbortError，这里转成 `QueryAbortedError` 抛出。
 */
export async function streamQuery(
  payload: QueryRequest,
  handlers: QueryHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const token = getAccessToken()
  let response: Response
  try {
    response = await fetch(`${API_BASE}/query`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(payload),
      signal,
    })
  } catch (error) {
    if ((error as Error)?.name === 'AbortError') {
      throw new QueryAbortedError()
    }
    throw new ApiError('无法连接后端服务，请确认服务已启动', 0)
  }

  if (!response.ok) {
    // 开流之前的失败走的是正常信封（TDD 12.2）：会话不存在是 404，未登录是 401。
    // 这些错误必须在界面上是"一次失败的请求"，而不是"一条奇怪的流"。
    const body = (await response.json().catch(() => null)) as ApiResponse<unknown> | null
    const message = body?.message ?? `请求失败（HTTP ${response.status}）`
    if (response.status === 401) {
      notifySessionExpired(message)
    }
    throw new ApiError(message, body?.code ?? 0, response.status)
  }

  if (!response.body) {
    throw new ApiError('后端未返回事件流', 0)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) {
        break
      }
      buffer += decoder.decode(value, { stream: true })

      // 帧以空行分隔。**必须按空行切，不能按单个换行**：
      // 一帧内部本来就有换行（event 行与 data 行），按换行切会把每一行都当成一帧，
      // 于是 event 行被解析成"没有 data 的帧"而被丢掉，data 行则丢失了它的类型。
      let boundary = buffer.indexOf('\n\n')
      while (boundary !== -1) {
        const block = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        const frame = parseFrame(block)
        if (frame) {
          try {
            dispatch(JSON.parse(frame.data) as Record<string, unknown>, handlers)
          } catch {
            // 单帧解析失败不该掐断整条流：后面还有 sql / result / done。
            // 后端是 JSON 编码（json.dumps），真出现半截 JSON 说明流被截断，
            // 那时循环会在下一轮 read 里自然结束。
          }
        }
        boundary = buffer.indexOf('\n\n')
      }
    }
  } catch (error) {
    if ((error as Error)?.name === 'AbortError') {
      throw new QueryAbortedError()
    }
    throw error
  } finally {
    reader.releaseLock()
  }
}

export { OK_CODE }
