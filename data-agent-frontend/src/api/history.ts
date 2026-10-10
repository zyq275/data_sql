import { api } from './http'
import type { HistoryDetail, HistoryItem, HistoryStatus, PageData, PageQuery } from './types'

export interface HistoryQuery extends PageQuery {
  status?: HistoryStatus | null
  keyword?: string | null
}

/** GET /history —— 本人查询历史，可按状态与关键字筛选。 */
export function listHistory(query: HistoryQuery = {}): Promise<PageData<HistoryItem>> {
  // 空串要剔掉：后端 `keyword` 是 `str | None`，传 `keyword=` 会当成"按空串模糊匹配"，
  // 语义上等价于不过滤，但会白白让缓存与日志里多一个参数
  const params: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== '') {
      params[key] = value
    }
  }
  return api.get<PageData<HistoryItem>>('/history', { params })
}

/** GET /history/{id} —— 含 SQL、结果预览与解释（S3 结果溯源）。 */
export function getHistory(id: string): Promise<HistoryDetail> {
  return api.get<HistoryDetail>(`/history/${encodeURIComponent(id)}`)
}

/** DELETE /history/{id} —— 删除一条历史。 */
export function deleteHistory(id: string): Promise<null> {
  return api.delete<null>(`/history/${encodeURIComponent(id)}`)
}
