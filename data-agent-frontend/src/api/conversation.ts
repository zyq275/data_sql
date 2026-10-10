import { api } from './http'
import type { ConversationDetailOut, ConversationOut, PageData, PageQuery } from './types'

/** POST /conversations —— 新建会话（FR-CONV-001）。 */
export function createConversation(title?: string): Promise<ConversationOut> {
  return api.post<ConversationOut>('/conversations', title ? { title } : {})
}

/** GET /conversations —— 会话列表，按更新时间倒序。 */
export function listConversations(query: PageQuery = {}): Promise<PageData<ConversationOut>> {
  return api.get<PageData<ConversationOut>>('/conversations', { params: query })
}

/**
 * GET /conversations/{id} —— 会话详情，含全部消息。
 *
 * 重开会话时靠它把历史消息铺回页面（验收项"重开会话时能加载历史消息"）。
 */
export function getConversation(id: string): Promise<ConversationDetailOut> {
  return api.get<ConversationDetailOut>(`/conversations/${encodeURIComponent(id)}`)
}

/** DELETE /conversations/{id} —— 删除会话及其消息。 */
export function deleteConversation(id: string): Promise<null> {
  return api.delete<null>(`/conversations/${encodeURIComponent(id)}`)
}
