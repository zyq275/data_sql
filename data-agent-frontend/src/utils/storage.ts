/**
 * 令牌的持久化读写。
 *
 * 单独成一个模块、而不是放在 auth store 里，是为了打断循环依赖：
 * `api/http.ts` 需要在每个请求上取令牌，而 auth store 又依赖 `api/auth.ts`，
 * 后者再依赖 http —— 三者绕成一个环，谁先加载谁就拿到 undefined。
 * 这里只放纯函数，http 与 store 都从它取，环就断了。
 */

import type { TokenOut } from '@/api/types'

const ACCESS_KEY = 'da.access_token'
const REFRESH_KEY = 'da.refresh_token'

export function getAccessToken(): string | null {
  return localStorage.getItem(ACCESS_KEY)
}

export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_KEY)
}

export function setTokens(tokens: TokenOut): void {
  localStorage.setItem(ACCESS_KEY, tokens.access_token)
  localStorage.setItem(REFRESH_KEY, tokens.refresh_token)
}

export function clearTokens(): void {
  localStorage.removeItem(ACCESS_KEY)
  localStorage.removeItem(REFRESH_KEY)
}
