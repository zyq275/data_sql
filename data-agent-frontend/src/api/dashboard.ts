import { api } from './http'
import type { DashboardDimensionsResponse, DashboardMetricsResponse } from './types'

/**
 * GET /dashboard/dimensions —— 当前角色可用的维度与指标（analyst 及以上）。
 *
 * 界面必须先取这一份再来拼查询：维度名与指标名都来自知识库配置，
 * 前端写死的话，配置一改界面就发出后端不认的维度，表现为一片 40000。
 */
export function fetchDimensions(): Promise<DashboardDimensionsResponse> {
  return api.get<DashboardDimensionsResponse>('/dashboard/dimensions')
}

export interface MetricsQuery {
  /** 聚合维度，逗号分隔可多级（如 `year,month`）。 */
  dimension: string
  /** 指标名，逗号分隔；不传则用当前角色的全部可用指标。 */
  metrics?: string
  /** 起始日期 YYYY-MM-DD。 */
  start?: string
  /** 结束日期 YYYY-MM-DD。 */
  end?: string
}

/** GET /dashboard/metrics —— 按维度聚合出指标趋势（FR-DASH-001）。 */
export function fetchMetrics(query: MetricsQuery): Promise<DashboardMetricsResponse> {
  const params: Record<string, unknown> = { dimension: query.dimension }
  for (const key of ['metrics', 'start', 'end'] as const) {
    const value = query[key]
    if (value) {
      params[key] = value
    }
  }
  return api.get<DashboardMetricsResponse>('/dashboard/metrics', { params })
}
