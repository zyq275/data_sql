/**
 * 统一 axios 实例（阶段 3 起持续使用，TDD 7.1 / 12.1）。
 *
 * 这一层做三件事，且只做这三件：
 *   1. **拆信封**：后端一律返回 `{code, message, data}`，业务代码只该看见 `data`。
 *      不拆的话，每个调用点都要写一遍 `res.data.data`，且迟早有人忘了判断 code。
 *   2. **带令牌**：每个请求自动附 `Authorization: Bearer`。
 *   3. **续期**：access token 过期（业务码 40101）时凭 refresh token 静默换新并重放原请求。
 *
 * **只有 40101 触发续期**，不是"任何 401 都去 refresh"：
 *   - 40100 是凭据不对（登录页输错密码就是这个），去 refresh 会把用户刚输错的密码
 *     变成"莫名其妙的登录状态变化"；
 *   - 40102 是令牌本身无效或用户已不存在，refresh 也救不回来，只能回登录页。
 * 三者的分工写在 `app/core/exceptions.py`，这里按它的语义对齐。
 */

import axios, { type AxiosError, type AxiosRequestConfig } from 'axios'
import { ElMessage } from 'element-plus'

import { clearTokens, getAccessToken, getRefreshToken, setTokens } from '@/utils/storage'
import type { ApiResponse, TokenOut } from './types'

/** 业务/传输错误，携带后端业务码，供调用方按码分支（而不是靠文案匹配）。 */
export class ApiError extends Error {
  readonly code: number
  readonly status: number | undefined

  constructor(message: string, code: number, status?: number) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
  }
}

export const OK_CODE = 0

/** 令牌过期（core/exceptions.py 的 TokenExpiredError）。 */
const CODE_TOKEN_EXPIRED = 40101

let unauthorizedHandler: (() => void) | null = null

/**
 * 注册"令牌彻底失效"时的处理（清状态 + 回登录页）。
 *
 * 用注册回调而不是在这里 `import router`：router 的守卫要用 auth store，
 * store 要用 api/auth，api/auth 要用这个文件——静态 import 会成环。
 * 由 `main.ts` 在 router 建好之后注册，依赖方向就变成单向的。
 */
export function onUnauthorized(handler: () => void): void {
  unauthorizedHandler = handler
}

export const http = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL ?? '/api/v1',
  timeout: 30000,
})

/** 信封判定：探针 `/health` 不套信封，不能把它当 `data` 拆掉。 */
function isEnvelope(body: unknown): body is ApiResponse<unknown> {
  return (
    typeof body === 'object' &&
    body !== null &&
    'code' in body &&
    'message' in body &&
    'data' in body
  )
}

http.interceptors.request.use((config) => {
  const token = getAccessToken()
  if (token) {
    config.headers.set('Authorization', `Bearer ${token}`)
  }
  return config
})

/**
 * 续期中的那一个请求。
 *
 * 并发场景下（页面一进来同时发三四个请求）令牌往往一起过期，若各续各的，
 * 就会有多个 refresh 同时在飞——后端的 refresh token 若是单次有效，
 * 先回来的那个会把后到的全部作废，症状是"刷新页面偶尔被踢回登录页"。
 * 共享同一个 Promise，让它们等同一份结果。
 */
let refreshing: Promise<string> | null = null

async function refreshAccessToken(): Promise<string> {
  const refreshToken = getRefreshToken()
  if (!refreshToken) {
    throw new ApiError('登录状态已失效，请重新登录', CODE_TOKEN_EXPIRED, 401)
  }
  // 用裸 axios 而不是 http：走 http 会再进一次拦截器，令牌过期时无限递归
  const { data } = await axios.post<ApiResponse<TokenOut>>(
    `${http.defaults.baseURL}/auth/refresh`,
    { refresh_token: refreshToken },
    { timeout: 10000 },
  )
  if (!data || data.code !== OK_CODE || !data.data) {
    throw new ApiError(data?.message ?? '登录状态已失效', data?.code ?? CODE_TOKEN_EXPIRED, 401)
  }
  setTokens(data.data)
  return data.data.access_token
}

function refreshOnce(): Promise<string> {
  if (!refreshing) {
    refreshing = refreshAccessToken().finally(() => {
      refreshing = null
    })
  }
  return refreshing
}

/**
 * 清令牌、提示、回登录页。
 *
 * 导出是因为 SSE 走的是 `fetch` 而不是 axios（见 `api/query.ts`），
 * 绕不过这个拦截器，但它同样会遇到 401——两条链路必须收敛到同一处处理，
 * 否则会出现"普通请求能自动回登录页、流式请求却卡在页面上"这种不一致。
 */
export function notifySessionExpired(message: string): void {
  clearTokens()
  ElMessage.error(message)
  unauthorizedHandler?.()
}

http.interceptors.response.use(
  // 成功路径**不在这里拆信封**：axios 的拦截器签名要求返回 AxiosResponse，
  // 返回 data 就得靠 as 断言把类型骗过去，而骗过去的类型会一路传播到每个调用点。
  // 拆信封放在下面的 unwrap 里，那里返回值类型是真的。
  null,
  async (error: AxiosError<ApiResponse<unknown>>) => {
    const status = error.response?.status
    const body = error.response?.data
    const code = body?.code ?? 0

    if (!error.response) {
      const message = error.code === 'ECONNABORTED' ? '请求超时，请稍后重试' : '网络异常，请检查后端服务'
      ElMessage.error(message)
      return Promise.reject(new ApiError(message, 0))
    }

    const original = error.config as (AxiosRequestConfig & { _retried?: boolean }) | undefined
    const isRefreshCall = original?.url?.includes('/auth/refresh') ?? false

    if (status === 401 && code === CODE_TOKEN_EXPIRED && original && !original._retried && !isRefreshCall) {
      original._retried = true
      try {
        const token = await refreshOnce()
        original.headers = { ...original.headers, Authorization: `Bearer ${token}` }
        return http.request(original)
      } catch {
        notifySessionExpired('登录状态已失效，请重新登录')
        return Promise.reject(new ApiError('登录状态已失效', CODE_TOKEN_EXPIRED, 401))
      }
    }

    const message = body?.message ?? '请求失败'

    if (status === 401 && !isRefreshCall) {
      // 40100/40102：凭据不对或令牌无效，续期无意义
      notifySessionExpired(message)
      return Promise.reject(new ApiError(message, code, status))
    }
    if (status === 403) {
      ElMessage.error(message)
      return Promise.reject(new ApiError(message, code, status))
    }

    ElMessage.error(message)
    return Promise.reject(new ApiError(message, code, status))
  },
)

/**
 * 拆信封：`{code, message, data}` → `data`。
 *
 * 非信封的响应（探针 `/health`）原样返回——它本来就没有要拆的壳。
 * 2xx 却带非 0 业务码这种组合后端目前不会产生，仍在这里拦一道：
 * 放过去的话，调用点拿到的是 `null`，错误表现为后续的 `undefined` 崩溃，
 * 离真正的原因隔了好几层。
 */
function unwrap<T>(body: unknown, status: number): T {
  if (!isEnvelope(body)) {
    return body as T
  }
  if (body.code !== OK_CODE) {
    throw new ApiError(body.message, body.code, status)
  }
  return body.data as T
}

/** 请求方法：返回值就是信封里的 `data`，不是 AxiosResponse。 */
export const api = {
  get: async <T>(url: string, config?: AxiosRequestConfig): Promise<T> => {
    const response = await http.get<unknown>(url, config)
    return unwrap<T>(response.data, response.status)
  },
  post: async <T>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T> => {
    const response = await http.post<unknown>(url, data, config)
    return unwrap<T>(response.data, response.status)
  },
  put: async <T>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T> => {
    const response = await http.put<unknown>(url, data, config)
    return unwrap<T>(response.data, response.status)
  },
  delete: async <T>(url: string, config?: AxiosRequestConfig): Promise<T> => {
    const response = await http.delete<unknown>(url, config)
    return unwrap<T>(response.data, response.status)
  },
}

export default http
