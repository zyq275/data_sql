import { api } from './http'
import type {
  EvalCaseCreate,
  EvalCaseImportResult,
  EvalCaseOut,
  EvalRunDetail,
  EvalRunOut,
  EvalRunRequest,
  PageData,
  PageQuery,
} from './types'

/** GET /eval/cases —— 评测集列表（管理员，FR-EVAL-001）。 */
export function listCases(query: PageQuery = {}): Promise<PageData<EvalCaseOut>> {
  return api.get<PageData<EvalCaseOut>>('/eval/cases', { params: query })
}

/**
 * POST /eval/cases —— 批量导入评测样本。
 *
 * 一次一批而不是一条一个请求：导入的是 JSON 数组，逐条发意味着中途失败会留下
 * 半份评测集，而"跑了一半的评测集"给出的准确率是错的、且看不出来。
 * 后端对超过 500 条的批次直接拒收（MAX_IMPORT_CASES）。
 */
export function importCases(cases: EvalCaseCreate[]): Promise<EvalCaseImportResult> {
  return api.post<EvalCaseImportResult>('/eval/cases', { cases })
}

/** DELETE /eval/cases/{id} —— 删除单条样本。 */
export function deleteCase(id: string): Promise<null> {
  return api.delete<null>(`/eval/cases/${encodeURIComponent(id)}`)
}

/**
 * POST /eval/run —— 发起评测。
 *
 * 返回的是 `running` 记录，真正的执行在后台（BR-EVAL-001：跑同一张图但不落线上数据）。
 * 所以调用方拿到响应后要轮询运行记录，不能就地等结果。
 */
export function runEval(payload: EvalRunRequest = {}): Promise<EvalRunOut> {
  return api.post<EvalRunOut>('/eval/run', payload)
}

/** GET /eval/runs —— 运行记录，看板按它画趋势（不实时重算）。 */
export function listRuns(query: PageQuery = {}): Promise<PageData<EvalRunOut>> {
  return api.get<PageData<EvalRunOut>>('/eval/runs', { params: query })
}

/** GET /eval/runs/{id} —— 运行详情，含逐条用例的判定结果。 */
export function getRun(id: string): Promise<EvalRunDetail> {
  return api.get<EvalRunDetail>(`/eval/runs/${encodeURIComponent(id)}`)
}
