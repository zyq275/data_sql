/**
 * 后端契约的前端副本（TDD 7.1 / 7.2 / 7.3）。
 *
 * 逐个字段对着 `app/schemas/*.py` 抄，不是照着文档草稿写的——
 * 文档给的是示意，实际字段名以 schema 为准（比如分页里叫 `page_size` 而不是 `size`）。
 * 抄错一个字段名的症状是"运行时才发现是 undefined"，TS 在接口边界上是帮不上忙的，
 * 所以这里宁可啰嗦也要写全。
 */

/** 统一响应外壳（core/response.py）。 */
export interface ApiResponse<T> {
  code: number
  message: string
  data: T | null
}

/** 分页数据体（core/response.py 的 PageData）。 */
export interface PageData<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

/** 统一分页入参，与后端各列表接口的 Query 参数一致。 */
export interface PageQuery {
  page?: number
  page_size?: number
}

// ---- 认证与用户（schemas/auth.py、schemas/user.py）----

export type Role = 'viewer' | 'analyst' | 'admin'

export interface TokenOut {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
}

export interface UserOut {
  id: string
  username: string
  role: Role
  display_name: string | null
  is_active: boolean | null
}

export interface UserCreateRequest {
  username: string
  password: string
  role: Role
  display_name?: string | null
}

export interface ChangePasswordRequest {
  old_password: string
  new_password: string
}

// ---- 会话与消息（schemas/conversation.py）----

/** 结果集：SSE 的 `result` 事件与落库的 `result_preview` 是同一个形状。 */
export interface ResultSet {
  columns: string[]
  rows: unknown[][]
  row_count: number
  truncated: boolean
}

export interface ConversationOut {
  id: string
  title: string | null
  created_at: string
  updated_at: string
}

export interface MessageOut {
  id: string
  role: string
  query_text: string | null
  generated_sql: string | null
  result_preview: ResultSet | null
  explain_text: string | null
  created_at: string
}

export interface ConversationDetailOut extends ConversationOut {
  messages: MessageOut[]
}

// ---- 问答（schemas/query.py + TDD 7.3）----

export interface QueryRequest {
  query: string
  session_id?: string | null
}

/** 进度事件。`step` 是节点名（英文），`status` 只有 start / done。 */
export interface ProgressEvent {
  step: string
  status: 'start' | 'done'
}

export interface VizEvent {
  chart_type: ChartType | null
  message: string
}

/** 图表类型：与 app/agent/visualization.py 的四个常量一一对应。 */
export type ChartType = 'bar' | 'line' | 'pie' | 'table'

export interface DoneEvent {
  correction_count: number
  row_count: number
}

export interface StreamErrorEvent {
  code: number
  message: string
}

// ---- 历史（schemas/history.py）----

export type HistoryStatus = 'success' | 'failed'

export interface HistoryItem {
  id: string
  session_id: string | null
  query_text: string | null
  status: HistoryStatus | null
  duration_ms: number | null
  created_at: string
}

export interface HistoryDetail extends HistoryItem {
  generated_sql: string | null
  result_preview: ResultSet | null
  explain_text: string | null
}

// ---- 数据权限（schemas/permission.py）----

export type ResourceType = 'table' | 'column'

export interface PermissionOut {
  id: string
  role: Role
  resource_type: ResourceType
  resource_id: string
  resource_name: string | null
  table_name: string | null
}

export interface PermissionPayload {
  role: Role
  resource_type: ResourceType
  resource_id: string
}

export interface ResourceOut {
  id: string
  resource_type: ResourceType
  name: string
  table_name: string
  table_alias: string | null
  column_alias: string | null
  description: string | null
}

export interface ResourceCatalogResponse {
  tables: ResourceOut[]
  columns: ResourceOut[]
}

export interface RoleScopeOut {
  role: Role
  unrestricted: boolean
  /** 有权限的表 → 允许的字段列表；值为 null 表示该表整表放行。 */
  tables: Record<string, string[] | null>
  metrics: string[]
  forbidden_tables: string[]
  forbidden_columns: string[]
}

// ---- 数据看板（schemas/dashboard.py）----

export interface DashboardDimensionOut {
  name: string
  alias: string | null
  table: string
  kind: 'time' | 'category'
}

export interface DashboardMetricOut {
  name: string
  alias: string | null
  description: string | null
}

export interface DashboardDimensionsResponse {
  dimensions: DashboardDimensionOut[]
  metrics: DashboardMetricOut[]
}

export interface DashboardMetricsResponse {
  dimensions: DashboardDimensionOut[]
  columns: string[]
  rows: unknown[][]
  row_count: number
  truncated: boolean
  metrics: string[]
  sql: string
  /** 当前角色是否不受限；false 时看板数据已按数据权限裁剪（阶段 9）。 */
  unrestricted: boolean
}

// ---- 评测（schemas/eval.py）----

export interface ExpectedResult {
  columns: string[]
  rows: unknown[][]
}

export interface EvalCaseCreate {
  query_text: string
  expected_sql?: string | null
  expected_result?: ExpectedResult | null
}

export interface EvalCaseOut {
  id: string
  query_text: string
  expected_sql: string | null
  expected_result: ExpectedResult | null
  created_at: string
}

export interface EvalCaseImportResult {
  imported: number
  items: EvalCaseOut[]
}

export interface EvalRunRequest {
  name?: string | null
  case_ids?: string[] | null
}

export interface EvalCaseOutcome {
  case_id: string
  query_text: string
  generated_sql: string | null
  correct: boolean | null
  recall_hit: boolean | null
  executed: boolean | null
  duration_ms: number | null
  reason: string | null
}

export type EvalRunStatus = 'running' | 'success' | 'failed'

export interface EvalRunOut {
  id: string
  name: string | null
  total_cases: number
  accuracy: number | null
  recall_hit_rate: number | null
  execute_success_rate: number | null
  avg_duration_ms: number | null
  status: EvalRunStatus | null
  created_at: string
}

export interface EvalRunDetail extends EvalRunOut {
  outcomes: EvalCaseOutcome[]
  error: string | null
}
