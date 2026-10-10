/**
 * 把结果集渲染成 ECharts 配置。
 *
 * 后端只推荐**类型**（bar / line / pie / table），不告诉我们哪列是维度、哪列是度量
 * ——`app/agent/visualization.py` 的 `Recommendation` 就两个字段：`chart_type` 和
 * `message`。所以轴的判定要在前端重做一遍：按"这列的值是不是数字"来分。
 *
 * 与后端保持同一条判据是刻意的：后端判"该不该画折线"靠的也是值域，
 * 两边用同一套朴素规则，页面上就很少出现"推荐了折线图但画出来是空图"。
 *
 * 另外，**图表切换不是重新问一次**：用户点柱状图/折线图/饼图只是在渲染同一份
 * 已经拿到的 rows。重新提问会再花十几秒和一筆 token，而用户只是想换个看法。
 */

import type { EChartsOption } from 'echarts'

import type { ChartType, ResultSet } from '@/api/types'

/**
 * 从设计 Token 读颜色，避免在 JS 里再写一份色值（改主题时只改 tokens.css）。
 *
 * 第二个参数是**兜底值**，只在 CSS 变量读不到时生效——要么 SSR（没有 `window`），
 * 要么调用发生在 tokens.css 生效之前。所以本文件里会出现字面色值，
 * 那不是"绕过 Token 写死颜色"，是读不到时的最后一次机会：
 * 兜底值取的是 tokens.css 里的同一个值，即便它腐坏了，图表也只是配色回到默认，
 * 不会变成一坨黑。改 tokens.css 时顺手对一眼这里的兜底值。
 */
export function tokenColor(name: string, fallback: string): string {
  if (typeof window === 'undefined') {
    return fallback
  }
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return value || fallback
}

export function chartPalette(): string[] {
  return [
    tokenColor('--da-chart-1', '#315efb'),
    tokenColor('--da-chart-2', '#17a673'),
    tokenColor('--da-chart-3', '#d98f00'),
    tokenColor('--da-chart-4', '#7c5cff'),
    tokenColor('--da-chart-5', '#12a5b8'),
    tokenColor('--da-chart-6', '#e5484d'),
  ]
}

const NUMERIC_RE = /^-?\d+(\.\d+)?([eE][+-]?\d+)?$/

function isNumericValue(value: unknown): boolean {
  if (typeof value === 'number') {
    return Number.isFinite(value)
  }
  if (typeof value === 'string') {
    return NUMERIC_RE.test(value.trim())
  }
  return false
}

function toNumber(value: unknown): number | null {
  if (typeof value === 'number') {
    return Number.isFinite(value) ? value : null
  }
  if (typeof value === 'string' && NUMERIC_RE.test(value.trim())) {
    return Number(value)
  }
  return null
}

/** 采样上限：只看前 50 行判类型。全表扫一遍在大结果集上是白费，而列的类型不会变。 */
const SAMPLE_ROWS = 50

function numericColumns(columns: string[], rows: unknown[][]): Set<number> {
  const sample = rows.slice(0, SAMPLE_ROWS)
  const numeric = new Set<number>()
  for (let index = 0; index < columns.length; index += 1) {
    let seen = 0
    let allNumeric = true
    for (const row of sample) {
      const value = row[index]
      if (value === null || value === undefined || value === '') {
        continue
      }
      seen += 1
      if (!isNumericValue(value)) {
        allNumeric = false
        break
      }
    }
    // 一个非空值都没有的列不当度量：空列画出来是一条没有意义的零线
    if (seen > 0 && allNumeric) {
      numeric.add(index)
    }
  }
  return numeric
}

/** 维度列的展示值。数字维度（比如年份）也照原样显示，不补千分位。 */
function labelOf(value: unknown): string {
  if (value === null || value === undefined) {
    return '—'
  }
  return String(value)
}

export interface ChartSeries {
  name: string
  data: (number | null)[]
}

export interface ChartModel {
  labels: string[]
  series: ChartSeries[]
}

/**
 * 从结果集里拆出"轴 + 若干序列"。
 *
 * 返回 null 表示这份结果画不成图（没有可用的度量列），调用方应退回表格。
 */
export function buildChartModel(result: ResultSet, chartType: ChartType): ChartModel | null {
  const { columns, rows } = result
  if (!columns.length || !rows.length) {
    return null
  }

  const numeric = numericColumns(columns, rows)
  const dimensionIndexes = columns.map((_, index) => index).filter((index) => !numeric.has(index))
  let measureIndexes = columns.map((_, index) => index).filter((index) => numeric.has(index))

  if (!measureIndexes.length) {
    return null
  }

  // 轴列：第一列非数字的；整张表都是数字时退化成第一列（比如"按年份的销售额"
  // 里年份被读成了数字）——那时第一列本来就是分组依据。
  const axisIndex = dimensionIndexes[0] ?? 0
  if (measureIndexes.includes(axisIndex)) {
    measureIndexes = measureIndexes.filter((index) => index !== axisIndex)
  }

  // 折线图的时间轴常由多列拼成（year + quarter，见 visualization.py 的说明）。
  // 那种情况下只取第一列当轴，会出现"2024、2024、2024…"这样一格一格的重复标签，
  // 走势图直接失去可读性。所以折线图把**全部维度列拼成一个标签**。
  const axisColumns =
    chartType === 'line' && dimensionIndexes.length > 1 ? dimensionIndexes : [axisIndex]

  const labels = rows.map((row) => axisColumns.map((index) => labelOf(row[index])).join('-'))

  const series: ChartSeries[] = measureIndexes.map((index) => ({
    name: columns[index] ?? `列 ${index + 1}`,
    data: rows.map((row) => toNumber(row[index])),
  }))

  if (!series.length) {
    return null
  }
  return { labels, series }
}

/** 生成 ECharts 配置。`chartType` 为 table 时不该走到这里（表格由 el-table 渲染）。 */
export function buildChartOption(result: ResultSet, chartType: ChartType): EChartsOption | null {
  const model = buildChartModel(result, chartType)
  if (!model) {
    return null
  }

  const palette = chartPalette()
  const textColor = tokenColor('--da-color-text-secondary', '#7b8496')
  const gridColor = tokenColor('--da-color-border-light', '#eef1f6')

  if (chartType === 'pie') {
    // 饼图只画第一个度量：把它摊成扇区的是"占比"，多个度量叠在一张饼上没有意义
    const first = model.series[0]!
    return {
      color: palette,
      tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
      legend: { bottom: 0, textStyle: { color: textColor } },
      series: [
        {
          type: 'pie',
          radius: ['40%', '68%'],
          center: ['50%', '45%'],
          avoidLabelOverlap: true,
          // 扇区之间留一道底色描边，否则相邻两块颜色接近时糊成一片
          itemStyle: { borderColor: tokenColor('--da-color-bg-card', '#ffffff'), borderWidth: 1 },
          label: { color: textColor },
          data: model.labels.map((label, index) => ({
            name: label,
            value: first.data[index] ?? 0,
          })),
        },
      ],
    }
  }

  return {
    color: palette,
    tooltip: { trigger: 'axis' },
    legend: { top: 0, textStyle: { color: textColor } },
    grid: { left: 8, right: 16, bottom: 8, top: 40, containLabel: true },
    xAxis: {
      type: 'category',
      data: model.labels,
      // 标签多了就斜排：横着排会互相压住，最后变成一团糊
      axisLabel: { color: textColor, rotate: model.labels.length > 8 ? 30 : 0 },
      axisLine: { lineStyle: { color: gridColor } },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: textColor },
      splitLine: { lineStyle: { color: gridColor } },
    },
    series: model.series.map((item) => ({
      name: item.name,
      type: chartType === 'line' ? 'line' : 'bar',
      smooth: chartType === 'line',
      // 折线图打点：只有几个点时，光靠线看不出数据落在哪儿
      symbolSize: 6,
      barMaxWidth: 40,
      data: item.data,
    })),
  }
}

/** 结果集里有哪些标题栏，供"图表切换"按钮组显示（没有可画的度量就不显示图）。 */
export function canRenderChart(result: ResultSet | null): boolean {
  if (!result || !result.rows.length) {
    return false
  }
  return buildChartModel(result, 'bar') !== null
}
