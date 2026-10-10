<script setup lang="ts">
/**
 * 数据看板（FR-DASH-001~003，analyst 及以上）。
 *
 * 与问答页的本质区别：这里的数走**确定性的模板 SQL**，不经过大模型。
 * 看板上的一个数每次刷新都必须是同一个，而同一句 SQL 在数仓上的结果天然如此。
 * 所以这个页面里没有任何"AI"痕迹：选了维度和指标，点查询，出图。
 *
 * 入口的角色控制有两层（路由 meta + 后端 403），此处不再自己判一次——
 * 页面级再判一次只会让"到底哪一层拦的"更难查，而它带来的安全感是假的。
 */
import { Refresh, Search } from '@element-plus/icons-vue'
import { computed, onMounted, ref } from 'vue'

import { fetchDimensions, fetchMetrics } from '@/api/dashboard'
import type { DashboardMetricsResponse, DashboardMetricOut } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import LoadingBlock from '@/components/LoadingBlock.vue'
import ResultViewer from '@/components/ResultViewer.vue'
import SqlBlock from '@/components/SqlBlock.vue'

const loading = ref(false)
const bootstrapping = ref(true)
const result = ref<DashboardMetricsResponse | null>(null)

const dimensions = ref<{ name: string; alias: string | null; kind: 'time' | 'category' }[]>([])
const metrics = ref<DashboardMetricOut[]>([])

const selectedDimensions = ref<string[]>([])
const selectedMetrics = ref<string[]>([])
const dateRange = ref<[string, string] | null>(null)

/**
 * 默认视图的推荐。
 *
 * 后端**没有**为看板返回图表推荐（`DashboardMetricsResponse` 里没有这个字段），
 * 这与问答链路不同——那边由 `recommend_visualization` 节点算。
 * 这里按 `visualization.py` 的同一条朴素规则给个默认值：单一时间维度看趋势、
 * 单一普通维度看对比、多维度退回表格。
 *
 * 它只是**初始视图**，用户可以随时切：一个默认值判错，代价是一次点击；
 * 而默认停在表格上，代价是"看板页明明有 ECharts 却看不到图"。
 */
const defaultChartType = computed<'line' | 'bar' | 'table'>(() => {
  const data = result.value
  if (!data || data.dimensions.length !== 1) {
    return 'table'
  }
  return data.dimensions[0]?.kind === 'time' ? 'line' : 'bar'
})

/** 时间维度排前面：看板最常问的是趋势，而趋势的轴一定是时间。 */
const orderedDimensions = computed(() =>
  [...dimensions.value].sort((a, b) => {
    if (a.kind === b.kind) {
      return 0
    }
    return a.kind === 'time' ? -1 : 1
  }),
)

const canQuery = computed(() => selectedDimensions.value.length > 0)

function dimensionLabel(item: { name: string; alias: string | null; kind: string }): string {
  const suffix = item.kind === 'time' ? '（时间）' : ''
  return `${item.alias || item.name}${suffix}`
}

async function bootstrap() {
  bootstrapping.value = true
  try {
    const data = await fetchDimensions()
    dimensions.value = data.dimensions
    metrics.value = data.metrics
    // 默认选中第一个时间维度与全部指标：进页面就该有东西可看，
    // 而不是一片空表单等着用户自己拼
    const firstTime = data.dimensions.find((item) => item.kind === 'time')
    selectedDimensions.value = firstTime ? [firstTime.name] : []
    selectedMetrics.value = data.metrics.map((item) => item.name)
  } finally {
    bootstrapping.value = false
  }
}

async function query() {
  if (!canQuery.value) {
    return
  }
  loading.value = true
  result.value = null
  try {
    result.value = await fetchMetrics({
      dimension: selectedDimensions.value.join(','),
      // 全选等于不筛，就不传了——留着会让 URL 里挂一长串等于没筛的指标名
      metrics:
        selectedMetrics.value.length && selectedMetrics.value.length < metrics.value.length
          ? selectedMetrics.value.join(',')
          : undefined,
      start: dateRange.value?.[0],
      end: dateRange.value?.[1],
    })
  } finally {
    loading.value = false
  }
}

function reset() {
  selectedDimensions.value = []
  selectedMetrics.value = []
  dateRange.value = null
  result.value = null
}

onMounted(async () => {
  await bootstrap()
  await query()
})
</script>

<template>
  <div class="da-page">
    <div class="da-page__header">
      <div>
        <h2 class="da-page__title">数据看板</h2>
        <p class="da-page__subtitle">确定性的聚合查询，不经过大模型，结果可复核</p>
      </div>
    </div>

    <div class="da-card dash__filters">
      <LoadingBlock v-if="bootstrapping" :rows="2" />
      <template v-else>
        <div class="dash__field">
          <label class="dash__label">维度（可多级，如 年 + 月）</label>
          <el-select
            v-model="selectedDimensions"
            multiple
            collapse-tags
            collapse-tags-tooltip
            placeholder="选择分组维度"
            class="dash__select"
          >
            <el-option
              v-for="item in orderedDimensions"
              :key="item.name"
              :label="dimensionLabel(item)"
              :value="item.name"
            />
          </el-select>
        </div>

        <div class="dash__field">
          <label class="dash__label">指标</label>
          <el-select
            v-model="selectedMetrics"
            multiple
            collapse-tags
            collapse-tags-tooltip
            placeholder="默认全部指标"
            class="dash__select"
          >
            <el-option
              v-for="item in metrics"
              :key="item.name"
              :label="item.alias || item.name"
              :value="item.name"
            >
              <span>{{ item.alias || item.name }}</span>
              <span v-if="item.description" class="dash__option-desc">
                {{ item.description }}
              </span>
            </el-option>
          </el-select>
        </div>

        <div class="dash__field">
          <label class="dash__label">时间范围</label>
          <el-date-picker
            v-model="dateRange"
            type="daterange"
            value-format="YYYY-MM-DD"
            start-placeholder="开始日期"
            end-placeholder="结束日期"
            class="dash__date"
          />
        </div>

        <div class="dash__actions">
          <el-button type="primary" :icon="Search" :disabled="!canQuery" @click="query">
            查询
          </el-button>
          <el-button :icon="Refresh" @click="reset">重置</el-button>
        </div>
      </template>
    </div>

    <div class="da-card dash__body">
      <LoadingBlock v-if="loading" :rows="8" title />

      <EmptyState
        v-else-if="!result"
        description="选择维度与指标后点「查询」"
        hint="例如按「年」看销售额趋势"
      />

      <div v-else class="dash__result">
        <el-alert
          v-if="!result.unrestricted"
          type="warning"
          :closable="false"
          show-icon
          title="当前角色的数据权限已生效"
          description="结果已按你的可见范围裁剪，数字可能小于全量口径。"
        />

        <ResultViewer
          :result="result"
          :recommended-type="defaultChartType"
          filename="数据看板"
          height="380px"
        />

        <div v-if="result.sql">
          <!-- 看板把拼好的 SQL 一并返回（TDD 7.2），展示它是为了"能复核"：
               看到数之后能立刻知道它是怎么算出来的，不必去问开发 -->
          <div class="dash__sql">
            <div class="dash__label">本次使用的聚合 SQL</div>
            <SqlBlock :sql="result.sql" collapsible />
          </div>
        </div>

        <div v-if="result.dimensions.length" class="dash__meta">
          <span class="da-text-sm da-text-secondary">
            维度：{{ result.dimensions.map((item) => item.alias || item.name).join(' · ') }}
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.dash__filters {
  display: flex;
  align-items: flex-end;
  gap: var(--da-space-4);
  padding: var(--da-space-4);
  flex-wrap: wrap;
}

.dash__field {
  display: flex;
  flex-direction: column;
  gap: var(--da-space-1);
}

.dash__label {
  font-size: var(--da-font-sm);
  color: var(--da-color-text-secondary);
}

.dash__select {
  width: 260px;
}

.dash__date {
  width: 260px;
}

.dash__actions {
  display: flex;
  gap: var(--da-space-2);
  margin-left: auto;
}

.dash__body {
  padding: var(--da-space-5);
  min-height: 320px;
}

.dash__result {
  display: flex;
  flex-direction: column;
  gap: var(--da-space-4);
}

.dash__sql {
  display: flex;
  flex-direction: column;
}

.dash__option-desc {
  margin-left: var(--da-space-2);
  font-size: var(--da-font-xs);
  color: var(--da-color-text-placeholder);
}

.dash__meta {
  display: flex;
  gap: var(--da-space-3);
}
</style>
