import { api } from './http'
import type { ChangePasswordRequest, TokenOut, UserOut } from './types'

/** POST /auth/login —— 用户名 + 密码换双令牌（FR-AUTH-001）。 */
export function login(username: string, password: string): Promise<TokenOut> {
  return api.post<TokenOut>('/auth/login', { username, password })
}

/** GET /auth/me —— 当前用户（FR-AUTH-003）。 */
export function fetchMe(): Promise<UserOut> {
  return api.get<UserOut>('/auth/me')
}

/** PUT /auth/password —— 改密（FR-AUTH-004）。原密码错误后端返 400。 */
export function changePassword(payload: ChangePasswordRequest): Promise<null> {
  return api.put<null>('/auth/password', payload)
}
