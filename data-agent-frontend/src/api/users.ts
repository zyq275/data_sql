import { api } from './http'
import type { PageData, PageQuery, Role, UserCreateRequest, UserOut } from './types'

export interface UserQuery extends PageQuery {
  keyword?: string | null
}

/** GET /admin/users —— 用户列表（管理员，FR-ADMIN-001）。 */
export function listUsers(query: UserQuery = {}): Promise<PageData<UserOut>> {
  const params: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== '') {
      params[key] = value
    }
  }
  return api.get<PageData<UserOut>>('/admin/users', { params })
}

/** POST /admin/users —— 新建用户。用户名唯一，重名由后端返 400。 */
export function createUser(payload: UserCreateRequest): Promise<UserOut> {
  return api.post<UserOut>('/admin/users', payload)
}

/** PUT /admin/users/{id}/role —— 改角色（FR-ADMIN-002）。 */
export function updateUserRole(id: string, role: Role): Promise<UserOut> {
  return api.put<UserOut>(`/admin/users/${encodeURIComponent(id)}/role`, { role })
}

/**
 * PUT /admin/users/{id}/status —— 启用/禁用（FR-ADMIN-003）。
 *
 * 后端禁止管理员修改或禁用本人（BR-AUTH-003），界面上也要挡住，
 * 否则用户点下去只会吃一个 403。
 */
export function updateUserStatus(id: string, isActive: boolean): Promise<UserOut> {
  return api.put<UserOut>(`/admin/users/${encodeURIComponent(id)}/status`, { is_active: isActive })
}
