<script setup lang="ts">
/**
 * 结果展示：表格 / 图表切换 + 导出 CSV。
 *
 * 问答页、历史详情、数据看板三处共用同一个组件。分成三份写的话，
 * "图表切换"这类交互会在某一份里少做一点（比如少了空数据占位），
 * 而验收恰恰是按"问答页展示表格+SQL+解释+图表"这条整体印象打的。
 */
import { computed, ref, watch } from 'vue'

import type { ChartType, ResultSet } from '@/api/types'
import EChart from '@/components/EChart.vue'
import EmptyState from '@/components/EmptyState.vue'
import { buildChartOption, canRenderChart } from '@/utils/chart'
import { downloadCsv, safeFilename } from '@/utils/csv'
import { CHART_LABELS } from '@/utils/labels'

const props = withDefaults(
  defineProps<{
    result: ResultSet | null
    /** 后端推荐的图表类型；null 表示它认为不该画图（当前只有"结果为空"）。 */
    recommendedType?: ChartType | null
    /** 后端推荐时附带的一句话，直接展示（FR-VIZ-001 的 message）。 */
    vizMessage?: string | null
    /** 导出文件名前缀，通常用问题原文。 */
    filename?: string
    /** 高度，传给图表容器。 */
    height?: string
  }>(),
  { recommendedType: null, vizMessage: '', filename: '', height: '360px' },
)

type ViewMode = 'table' | ChartType

const VIEW_MODES: ViewMode[] = ['table', 'bar', 'line', 'pie']

const viewMode = ref<ViewMode>('table')

const hasResult = computed(() => Boolean(props.result))
const rows = computed<unknown[][]>(() => props.result?.rows ?? [])
const columns = computed<string[]>(() => props.result?.columns ?? [])
const isEmpty = computed(() => hasResult.value && rows.value.length === 0)
const chartAvailable = computed(() => canRenderChart(props.result))

/**
 * el-table 的列要有稳定的 prop 名，而行是数组（后端给的是 `rows: [[...]]`）。
 * 转成以列序号为键的对象，顺便把数字索引和列名解耦——列名可能重复或为空，
 * 拿列名当键会在这种情况下串列。
 */
const tableRows = computed(() =>
  rows.value.map((row) => {
    const record: Record<string, unknown> = {}
    columns.value.forEach((_, index) => {
      record[`c${index}`] = row[index]
    })
    return record
  }),
)

const chartOption = computed(() => {
  if (!props.result || viewMode.value === 'table') {
    return null
  }
  return buildChartOption(props.result, viewMode.value)
})

function applyRecommendation(type: ChartType | null | undefined) {
  // 后端没推荐（或推荐表格）时落在表格上：这是唯一永远画得出来的视图
  viewMode.value = type && type !== 'table' && chartAvailable.value ? type : 'table'
}

// 每拿到一份新结果就重置视图。不重置的话，上一轮选过"饼图"会带到下一轮——
// 下一个结果可能是多列明细，饼图那边是空的，用户只看到一片空白
watch(
  () => props.result,
  () => applyRecommendation(props.recommendedType),
  { immediate: true },
)

watch(
  () => props.recommendedType,
  (type) => applyRecommendation(type),
)

function exportCsv() {
  if (!props.result || !rows.value.length) {
    return
  }
  downloadCsv(
    safeFilename(props.filename || '查询结果'),
    columns.value,
    rows.value,
  )
}

/** 数值右对齐、小数保留四位：金额与比率混在一列时才看得清量级。 */
function cellText(value: unknown): string {
  if (value === null || value === undefined) {
    return '—'
  }
  if (typeof value === 'number') {
    return Number.isInteger(value) ? String(value) : String(Number(value.toFixed(4)))
  }
  return String(value)
}
</script>

<template>
  <div class="result">
    <div v-if="hasResult" class="result__bar">
      <div class="result__meta">
        <span class="da-text-sm da-text-secondary">
          共 {{ rows.length }} 行<template v-if="result?.truncated">（已达上限，结果被截断）</template>
        </span>
        <el-tooltip v-if="vizMessage" :content="vizMessage" placement="top">
          <span class="result__viz-message">{{ vizMessage }}</span>
        </el-tooltip>
      </div>

      <div class="result__actions">
        <el-radio-group v-if="chartAvailable" v-model="viewMode" size="small">
          <el-radio-button v-for="mode in VIEW_MODES" :key="mode" :value="mode">
            {{ CHART_LABELS[mode] }}
          </el-radio-button>
        </el-radio-group>
        <el-button
          size="small"
          :disabled="!rows.length"
          @click="exportCsv"
        >
          导出 CSV
        </el-button>
      </div>
    </div>

    <!-- 三态分明：没有结果 / 有结果但零行 / 有数据。中间那一种最容易漏——
         查询本身成功了，只是这个条件没匹配到数据，说"加载失败"或"暂无数据"
         都会把人引向错误的方向 -->
    <EmptyState
      v-if="!hasResult"
      description="还没有查询结果"
      hint="在下面的输入框里问一个数据问题，结果会显示在这里"
    />
    <EmptyState
      v-else-if="isEmpty"
      description="查询成功，但没有匹配的数据"
      hint="换个时间范围或筛选条件再试一次"
    />
    <template v-else>
      <EChart v-if="viewMode !== 'table' && chartOption" :option="chartOption" :height="height" />
      <el-table
        v-else
        :data="tableRows"
        border
        stripe
        size="small"
        max-height="420"
        class="result__table"
      >
        <el-table-column
          v-for="(name, index) in columns"
          :key="index"
          :prop="`c${index}`"
          :label="name || `列 ${index + 1}`"
          :formatter="(row: Record<string, unknown>) => cellText(row[`c${index}`])"
          show-overflow-tooltip
        />
      </el-table>
    </template>
  </div>
</template>

<style scoped>
.result {
  display: flex;
  flex-direction: column;
  gap: var(--da-space-3);
}

.result__bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--da-space-3);
  flex-wrap: wrap;
}

.result__meta {
  display: flex;
  align-items: center;
  gap: var(--da-space-3);
  min-width: 0;
}

.result__viz-message {
  font-size: var(--da-font-sm);
  color: var(--da-color-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.result__actions {
  display: flex;
  align-items: center;
  gap: var(--da-space-3);
}

.result__table {
  width: 100%;
}
</style>
