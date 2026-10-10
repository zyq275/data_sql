/**
 * 面向界面的文案与格式化。
 *
 * 节点名到中文标签的映射放在前端而不是让后端在 progress 事件里带中文：
 * 后端推的是**节点标识**，它在日志、链路追踪（阶段 11 的 `node.<name>` span）
 * 和 SSE 里必须是同一个字符串；一旦为了界面好看而改成中文，三处的对应关系就断了，
 * "日志里那个 node.filter_table 在页面上叫什么"要靠人脑记。
 */

/** 图上的节点顺序，用于把进度渲染成一条有序流水线（app/agent/graph.py）。 */
export const NODE_SEQUENCE = [
  'resolve_reference',
  'extract_keywords',
  'recall_column',
  'recall_value',
  'recall_metric',
  'merge_retrieved_info',
  'filter_table',
  'filter_metric',
  'add_extra_context',
  'generate_sql',
  'validate_sql',
  'correct_sql',
  'execute_sql',
  'generate_explain',
  'recommend_visualization',
] as const

export const NODE_LABELS: Record<string, string> = {
  resolve_reference: '理解指代',
  extract_keywords: '提取关键词',
  recall_column: '召回字段',
  recall_value: '召回取值',
  recall_metric: '召回指标',
  merge_retrieved_info: '合并召回结果',
  filter_table: '筛选数据表',
  filter_metric: '筛选指标',
  add_extra_context: '补充上下文',
  generate_sql: '生成 SQL',
  validate_sql: '校验 SQL',
  correct_sql: '纠正 SQL',
  execute_sql: '执行查询',
  generate_explain: '生成解释',
  recommend_visualization: '推荐图表',
}

export function nodeLabel(step: string): string {
  return NODE_LABELS[step] ?? step
}

/** 进度流水线里每一步的展示序号（1 起）。认识的名字排在前面，不认识的排在末尾。 */
export function nodeOrder(step: string): number {
  const index = (NODE_SEQUENCE as readonly string[]).indexOf(step)
  return index === -1 ? NODE_SEQUENCE.length + 1 : index + 1
}

export const CHART_LABELS: Record<string, string> = {
  table: '表格',
  bar: '柱状图',
  line: '折线图',
  pie: '饼图',
}

export const STATUS_LABELS: Record<string, string> = {
  success: '成功',
  failed: '失败',
  running: '运行中',
}

export function statusLabel(status: string | null | undefined): string {
  if (!status) {
    return '未知'
  }
  return STATUS_LABELS[status] ?? status
}

/** 时间戳 → `YYYY-MM-DD HH:mm`。后端给的是 ISO 串，直接给用户看太长。 */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return '—'
  }
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return value
  }
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`
  )
}

/** 耗时 → `1.2s` / `830ms`。超过一秒改看秒，毫秒数在几百的位数上人读不出量级。 */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) {
    return '—'
  }
  if (ms < 1000) {
    return `${Math.round(ms)}ms`
  }
  return `${(ms / 1000).toFixed(1)}s`
}

/** 比率（0~1）→ 百分比。后端存的是小数，不是百分数。 */
export function formatRate(value: number | null | undefined): string {
  if (value === null || value === undefined) {
    return '—'
  }
  return `${(value * 100).toFixed(1)}%`
}
