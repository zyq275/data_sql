<script setup lang="ts">
/**
 * 评测看板（FR-EVAL-001~004，仅管理员）。
 *
 * 三件事：管评测集、发起运行、看运行趋势与逐条判定。
 *
 * **趋势读的是运行记录，不是实时重算**（常见坑表里点名的一条）：
 * 每次运行落一条 `eval_run`，看板把它画出来。实时重算的话，昨天 88%、今天 91%
 * 这种变化就永远看不到——你只会看到"当前是 91%"，而不知道它是变好了还是变坏了。
 *
 * 运行是**后台任务**：POST /eval/run 返回的是 `running` 记录，所以发起之后要轮询，
 * 不能就地等结果。
 */
import { Delete, Plus, Refresh, VideoPlay } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'

import { deleteCase, getRun, importCases, listCases, listRuns, runEval } from '@/api/eval'
import type { EvalCaseOut, EvalRunDetail, EvalRunOut, ExpectedResult, ResultSet } from '@/api/types'
import EChart from '@/components/EChart.vue'
import EmptyState from '@/components/EmptyState.vue'
import LoadingBlock from '@/components/LoadingBlock.vue'
import { buildChartOption } from '@/utils/chart'
import { formatDateTime, formatDuration, formatRate, statusLabel } from '@/utils/labels'

const activeTab = ref<'runs' | 'cases'>('runs')

// ---- 运行记录 ----

const runsLoading = ref(false)
const runs = ref<EvalRunOut[]>([])
const runsTotal = ref(0)
const runsQuery = reactive({ page: 1, page_size: 20 })

const detailOpen = ref(false)
const detailLoading = ref(false)
const detail = ref<EvalRunDetail | null>(null)

let pollTimer: number | null = null

const hasRunning = computed(() => runs.value.some((run) => run.status === 'running'))

/**
 * 趋势图：把运行记录拼成一个"结果集"再交给同一个图表构造器。
 *
 * 复用 `buildChartOption` 而不是手写一份 option，是为了让看板的折线图和问答页的
 * 长成同一个样子（同一个调色板、同一个网格留白）。手写第二份 option 的代价
 * 不是多写几行，而是两处的视觉细节会慢慢分叉。
 */
const trendResult = computed<ResultSet | null>(() => {
  // 后端按时间倒序返回，画趋势要从旧到新
  const ordered = [...runs.value].reverse()
  if (ordered.length < 2) {
    return null
  }
  return {
    columns: ['运行时间', '准确率', '召回命中率', '执行成功率'],
    rows: ordered.map((run) => [
      run.name || formatDateTime(run.created_at),
      run.accuracy,
      run.recall_hit_rate,
      run.execute_success_rate,
    ]),
    row_count: ordered.length,
    truncated: false,
  }
})

const trendOption = computed(() =>
  trendResult.value ? buildChartOption(trendResult.value, 'line') : null,
)

async function loadRuns() {
  runsLoading.value = true
  try {
    const page = await listRuns({ page: runsQuery.page, page_size: runsQuery.page_size })
    runs.value = page.items
    runsTotal.value = page.total
  } finally {
    runsLoading.value = false
  }
}

async function openRunDetail(run: EvalRunOut) {
  detailOpen.value = true
  detailLoading.value = true
  detail.value = null
  try {
    detail.value = await getRun(run.id)
  } finally {
    detailLoading.value = false
  }
}

const starting = ref(false)

async function startRun() {
  starting.value = true
  try {
    await runEval({})
    ElMessage.success('评测已发起，正在后台运行')
    await loadRuns()
    schedulePoll()
  } finally {
    starting.value = false
  }
}

/** 有运行中的任务时轮询。只在这个页面活着时轮询，离开就停——没人看的轮询是白烧。 */
function schedulePoll() {
  clearPoll()
  if (!hasRunning.value) {
    return
  }
  pollTimer = window.setTimeout(async () => {
    await loadRuns()
    schedulePoll()
  }, 3000)
}

function clearPoll() {
  if (pollTimer !== null) {
    window.clearTimeout(pollTimer)
    pollTimer = null
  }
}

// ---- 评测集 ----

const casesLoading = ref(false)
const cases = ref<EvalCaseOut[]>([])
const casesTotal = ref(0)
const casesQuery = reactive({ page: 1, page_size: 20 })

const importVisible = ref(false)
const importing = ref(false)
const importText = ref('')

const importTemplate = `[
  {
    "query_text": "去年华东区的销售额是多少？",
    "expected_sql": "SELECT SUM(amount) FROM fact_sales ...",
    "expected_result": { "columns": ["销售额"], "rows": [[1234567]] }
  }
]`

async function loadCases() {
  casesLoading.value = true
  try {
    const page = await listCases({ page: casesQuery.page, page_size: casesQuery.page_size })
    cases.value = page.items
    casesTotal.value = page.total
  } finally {
    casesLoading.value = false
  }
}

function openImport() {
  importText.value = ''
  importVisible.value = true
}

async function submitImport() {
  let parsed: unknown
  try {
    parsed = JSON.parse(importText.value)
  } catch {
    // 就地给出 JSON 错误，而不是让后端返回 40000 再猜：这是本地能判断的事
    ElMessage.error('不是合法的 JSON，请检查格式（注意中文引号）')
    return
  }
  if (!Array.isArray(parsed) || !parsed.length) {
    ElMessage.error('需要一个非空数组，每个元素是一条评测样本')
    return
  }

  const payload = parsed.map((item) => {
    const record = item as Record<string, unknown>
    return {
      query_text: String(record.query_text ?? ''),
      expected_sql: (record.expected_sql as string | null) ?? null,
      expected_result: (record.expected_result as ExpectedResult | null) ?? null,
    }
  })
  const invalid = payload.findIndex((item) => !item.query_text)
  if (invalid !== -1) {
    ElMessage.error(`第 ${invalid + 1} 条缺少 query_text`)
    return
  }

  importing.value = true
  try {
    const result = await importCases(payload)
    ElMessage.success(`已导入 ${result.imported} 条样本`)
    importVisible.value = false
    await loadCases()
  } finally {
    importing.value = false
  }
}

async function removeCase(item: EvalCaseOut) {
  try {
    await ElMessageBox.confirm('删除这条评测样本？', '删除样本', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  await deleteCase(item.id)
  ElMessage.success('已删除')
  await loadCases()
}

const summary = computed(() => {
  const latest = runs.value.find((run) => run.status === 'success')
  if (!latest) {
    return null
  }
  return latest
})

onMounted(async () => {
  await Promise.all([loadRuns(), loadCases()])
  schedulePoll()
})

onBeforeUnmount(clearPoll)
</script>

<template>
  <div class="da-page">
    <div class="da-page__header">
      <div>
        <h2 class="da-page__title">评测看板</h2>
        <p class="da-page__subtitle">在与线上相同的 Agent 图上回归，但不落任何线上数据</p>
      </div>
      <el-button type="primary" :icon="VideoPlay" :loading="starting" @click="startRun">
        发起评测
      </el-button>
    </div>

    <!-- 最新一次成功运行的三个关键指标。评审看板时最先问的就是这三个数 -->
    <div v-if="summary" class="eval__kpis">
      <div class="da-card eval__kpi">
        <span class="eval__kpi-label">准确率</span>
        <span class="eval__kpi-value">{{ formatRate(summary.accuracy) }}</span>
        <span class="eval__kpi-hint">目标 ≥ 90%</span>
      </div>
      <div class="da-card eval__kpi">
        <span class="eval__kpi-label">召回命中率</span>
        <span class="eval__kpi-value">{{ formatRate(summary.recall_hit_rate) }}</span>
        <span class="eval__kpi-hint">目标 ≥ 90%</span>
      </div>
      <div class="da-card eval__kpi">
        <span class="eval__kpi-label">执行成功率</span>
        <span class="eval__kpi-value">{{ formatRate(summary.execute_success_rate) }}</span>
        <span class="eval__kpi-hint">目标 ≥ 95%</span>
      </div>
      <div class="da-card eval__kpi">
        <span class="eval__kpi-label">平均耗时</span>
        <span class="eval__kpi-value">{{ formatDuration(summary.avg_duration_ms) }}</span>
        <span class="eval__kpi-hint">{{ summary.total_cases }} 条用例</span>
      </div>
    </div>

    <el-tabs v-model="activeTab" class="da-card eval__tabs">
      <el-tab-pane label="运行记录" name="runs">
        <div class="eval__toolbar">
          <span class="da-text-sm da-text-secondary">共 {{ runsTotal }} 次运行</span>
          <el-tag v-if="hasRunning" size="small" type="warning" effect="plain">
            有运行中的任务，正在自动刷新
          </el-tag>
          <div class="eval__spacer" />
          <el-button :icon="Refresh" @click="loadRuns">刷新</el-button>
        </div>

        <div v-if="trendOption" class="eval__trend">
          <div class="eval__label">指标趋势（按运行顺序）</div>
          <EChart :option="trendOption" height="300px" />
        </div>
        <el-alert
          v-else-if="!runsLoading"
          class="eval__trend-hint"
          type="info"
          :closable="false"
          show-icon
          title="至少需要两次运行才能画出趋势"
        />

        <LoadingBlock v-if="runsLoading" :rows="4" />
        <EmptyState
          v-else-if="!runs.length"
          description="还没有运行记录"
          hint="先在「评测集」导入样本，再点右上角「发起评测」"
        />
        <el-table v-else :data="runs" stripe size="small" @row-click="openRunDetail">
          <el-table-column label="名称" min-width="160">
            <template #default="{ row }">{{ row.name || '未命名运行' }}</template>
          </el-table-column>
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag
                size="small"
                :type="
                  row.status === 'success' ? 'success' : row.status === 'running' ? 'warning' : 'danger'
                "
                effect="plain"
              >
                {{ statusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="用例数" width="90">
            <template #default="{ row }">{{ row.total_cases }}</template>
          </el-table-column>
          <el-table-column label="准确率" width="100">
            <template #default="{ row }">{{ formatRate(row.accuracy) }}</template>
          </el-table-column>
          <el-table-column label="召回命中率" width="120">
            <template #default="{ row }">{{ formatRate(row.recall_hit_rate) }}</template>
          </el-table-column>
          <el-table-column label="执行成功率" width="120">
            <template #default="{ row }">{{ formatRate(row.execute_success_rate) }}</template>
          </el-table-column>
          <el-table-column label="平均耗时" width="110">
            <template #default="{ row }">{{ formatDuration(row.avg_duration_ms) }}</template>
          </el-table-column>
          <el-table-column label="时间" width="170">
            <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
          </el-table-column>
        </el-table>

        <div v-if="runsTotal > runsQuery.page_size" class="eval__pagination">
          <el-pagination
            layout="total, prev, pager, next"
            :total="runsTotal"
            :current-page="runsQuery.page"
            :page-size="runsQuery.page_size"
            @current-change="
              (page: number) => {
                runsQuery.page = page
                loadRuns()
              }
            "
          />
        </div>
      </el-tab-pane>

      <el-tab-pane label="评测集" name="cases">
        <div class="eval__toolbar">
          <span class="da-text-sm da-text-secondary">共 {{ casesTotal }} 条样本</span>
          <div class="eval__spacer" />
          <el-button :icon="Refresh" @click="loadCases">刷新</el-button>
          <el-button type="primary" :icon="Plus" @click="openImport">批量导入</el-button>
        </div>

        <LoadingBlock v-if="casesLoading" :rows="4" />
        <EmptyState
          v-else-if="!cases.length"
          description="评测集是空的"
          hint="导入 JSON 数组，每条至少要有 query_text"
        >
          <el-button type="primary" :icon="Plus" @click="openImport">批量导入</el-button>
        </EmptyState>
        <el-table v-else :data="cases" stripe size="small">
          <el-table-column label="问题" min-width="300" show-overflow-tooltip>
            <template #default="{ row }">{{ row.query_text }}</template>
          </el-table-column>
          <el-table-column label="期望 SQL" min-width="240" show-overflow-tooltip>
            <template #default="{ row }">
              <span class="da-mono da-text-sm">{{ row.expected_sql || '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="期望结果" width="110">
            <template #default="{ row }">
              <el-tag v-if="row.expected_result" size="small" effect="plain">有</el-tag>
              <span v-else class="da-text-secondary">—</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="90" align="right">
            <template #default="{ row }">
              <el-button link size="small" @click="removeCase(row)">
                <el-icon><Delete /></el-icon>
              </el-button>
            </template>
          </el-table-column>
        </el-table>

        <div v-if="casesTotal > casesQuery.page_size" class="eval__pagination">
          <el-pagination
            layout="total, prev, pager, next"
            :total="casesTotal"
            :current-page="casesQuery.page"
            :page-size="casesQuery.page_size"
            @current-change="
              (page: number) => {
                casesQuery.page = page
                loadCases()
              }
            "
          />
        </div>
      </el-tab-pane>
    </el-tabs>

    <el-drawer v-model="detailOpen" title="运行详情" size="65%">
      <LoadingBlock v-if="detailLoading" :rows="8" title />
      <div v-else-if="detail" class="eval__detail">
        <el-descriptions :column="3" border size="small">
          <el-descriptions-item label="名称">{{ detail.name || '未命名运行' }}</el-descriptions-item>
          <el-descriptions-item label="状态">{{ statusLabel(detail.status) }}</el-descriptions-item>
          <el-descriptions-item label="用例数">{{ detail.total_cases }}</el-descriptions-item>
          <el-descriptions-item label="准确率">{{ formatRate(detail.accuracy) }}</el-descriptions-item>
          <el-descriptions-item label="召回命中率">
            {{ formatRate(detail.recall_hit_rate) }}
          </el-descriptions-item>
          <el-descriptions-item label="执行成功率">
            {{ formatRate(detail.execute_success_rate) }}
          </el-descriptions-item>
        </el-descriptions>

        <el-alert
          v-if="detail.error"
          type="error"
          :closable="false"
          show-icon
          :title="detail.error"
        />

        <div v-if="detail.outcomes.length">
          <div class="eval__label">逐条判定</div>
          <el-table :data="detail.outcomes" stripe size="small" max-height="520">
            <el-table-column label="问题" min-width="220" show-overflow-tooltip>
              <template #default="{ row }">{{ row.query_text }}</template>
            </el-table-column>
            <el-table-column label="结果" width="90">
              <template #default="{ row }">
                <el-tag
                  size="small"
                  :type="row.correct ? 'success' : 'danger'"
                  effect="plain"
                >
                  {{ row.correct ? '正确' : '错误' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="召回" width="90">
              <template #default="{ row }">
                <span :class="row.recall_hit ? 'eval__ok' : 'eval__bad'">
                  {{ row.recall_hit ? '命中' : '未命中' }}
                </span>
              </template>
            </el-table-column>
            <el-table-column label="执行" width="90">
              <template #default="{ row }">
                <span :class="row.executed ? 'eval__ok' : 'eval__bad'">
                  {{ row.executed ? '成功' : '失败' }}
                </span>
              </template>
            </el-table-column>
            <el-table-column label="耗时" width="90">
              <template #default="{ row }">{{ formatDuration(row.duration_ms) }}</template>
            </el-table-column>
            <el-table-column label="说明" min-width="200" show-overflow-tooltip>
              <template #default="{ row }">{{ row.reason || '—' }}</template>
            </el-table-column>
          </el-table>
        </div>
      </div>
      <EmptyState v-else description="运行记录不存在" />
    </el-drawer>

    <el-dialog v-model="importVisible" title="批量导入评测样本" width="640px">
      <el-alert
        class="eval__import-tip"
        type="info"
        :closable="false"
        show-icon
        title="JSON 数组，每条至少要有 query_text；expected_sql 与 expected_result 可选，但至少给一个才能判定对错。"
      />
      <el-input
        v-model="importText"
        type="textarea"
        :rows="14"
        :placeholder="importTemplate"
        class="eval__import-input"
      />
      <template #footer>
        <el-button @click="importVisible = false">取消</el-button>
        <el-button type="primary" :loading="importing" @click="submitImport">导入</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.eval__kpis {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--da-space-4);
}

.eval__kpi {
  display: flex;
  flex-direction: column;
  gap: var(--da-space-1);
  padding: var(--da-space-4);
}

.eval__kpi-label {
  font-size: var(--da-font-sm);
  color: var(--da-color-text-secondary);
}

.eval__kpi-value {
  font-size: var(--da-font-display);
  font-weight: 600;
  line-height: var(--da-line-height-tight);
}

.eval__kpi-hint {
  font-size: var(--da-font-xs);
  color: var(--da-color-text-placeholder);
}

.eval__tabs {
  padding: var(--da-space-4) var(--da-space-5) var(--da-space-5);
}

.eval__toolbar {
  display: flex;
  align-items: center;
  gap: var(--da-space-3);
  margin-bottom: var(--da-space-4);
}

.eval__spacer {
  flex: 1 1 auto;
}

.eval__trend {
  margin-bottom: var(--da-space-5);
}

.eval__label {
  margin-bottom: var(--da-space-2);
  font-size: var(--da-font-sm);
  font-weight: 600;
  color: var(--da-color-text-regular);
}

.eval__trend-hint {
  margin-bottom: var(--da-space-4);
}

.eval__pagination {
  display: flex;
  justify-content: flex-end;
  margin-top: var(--da-space-4);
}

.eval__detail {
  display: flex;
  flex-direction: column;
  gap: var(--da-space-4);
}

.eval__ok {
  color: var(--da-color-success);
}

.eval__bad {
  color: var(--da-color-danger);
}

.eval__import-tip {
  margin-bottom: var(--da-space-3);
}

.eval__import-input :deep(textarea) {
  font-family: var(--da-font-family-mono);
  font-size: var(--da-font-sm);
}
</style>
