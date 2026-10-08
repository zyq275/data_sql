import { http } from './http'

export interface HealthPayload {
  status: string
  app: string
  version: string
  debug: boolean
}

/**
 * 存活探针挂在根路径 /health，不带 /api/v1 前缀（TDD 7.2）。
 */
export async function fetchHealth(): Promise<HealthPayload> {
  const { data } = await http.get<HealthPayload>('/health', { baseURL: '/' })
  return data
}
