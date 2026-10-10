<script setup lang="ts">
/**
 * 查询历史：本人过往的每一次提问（FR-HIST-001~003）。
 *
 * 复用 ResultViewer 而不是自己再画一遍结果——历史详情里的表格与问答页的表格
 * 是同一份数据（后端两边都存 `{columns, rows, row_count, truncated}`），
 * 视觉上不一致会让人觉得"历史里的数字和当时看到的不一样"。
 */
import { Refresh, Search } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { onMounted, reactive, ref } from 'vue'

import { deleteHistory, getHistory, listHistory } from '@/api/history'
import type { HistoryDetail, HistoryItem, HistoryStatus } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import LoadingBlock from '@/components/LoadingBlock.vue'
import ResultViewer from '@/components/ResultViewer.vue'
import SqlBlock from '@/components/SqlBlock.vue'
import { formatDateTime, formatDuration, statusLabel } from '@/utils/labels'

const loading = ref(false)
const items = ref<HistoryItem[]>([])
const total = ref(0)

const query = reactive({
  page: 1,
  page_size: 20,
  keyword: '',
  status: '' as HistoryStatus | '',
})

const detailOpen = ref(false)
const detailLoading = ref(false)
const detail = ref<HistoryDetail | null>(null)

async function load() {
  loading.value = true
  try {
    const page = await listHistory({
      page: query.page,
      page_size: query.page_size,
      keyword: query.keyword || null,
      status: query.status || null,
    })
    items.value = page.items
    total.value = page.total
  } finally {
    loading.value = false
  }
}

function search() {
  query.page = 1
  void load()
}

async function openDetail(item: HistoryItem) {
  detailOpen.value = true
  detailLoading.value = true
  detail.value = null
  try {
    detail.value = await getHistory(item.id)
  } finally {
    detailLoading.value = false
  }
}

async function remove(item: HistoryItem) {
  try {
    await ElMessageBox.confirm('删除这条查询记录？', '删除历史', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  await deleteHistory(item.id)
  ElMessage.success('已删除')
  // 删掉当前页最后一条时往前翻一页，否则会停在一个空白页上
  if (items.value.length === 1 && query.page > 1) {
    query.page -= 1
  }
  await load()
}

function onPageChange(page: number) {
  query.page = page
  void load()
}

onMounted(load)
</script>

<template>
  <div class="da-page">
    <div class="da-page__header">
      <div>
        <h2 class="da-page__title">查询历史</h2>
        <p class="da-page__subtitle">仅显示本人发起的查询</p>
      </div>
    </div>

    <div class="da-card history__filters">
      <el-input
        v-model="query.keyword"
        :prefix-icon="Search"
        placeholder="按问题或 SQL 模糊匹配"
        clearable
        class="history__keyword"
        @keyup.enter="search"
        @clear="search"
      />
      <el-select v-model="query.status" placeholder="全部状态" clearable class="history__status">
        <el-option label="成功" value="success" />
        <el-option label="失败" value="failed" />
      </el-select>
      <el-button type="primary" :icon="Search" @click="search">查询</el-button>
      <el-button :icon="Refresh" @click="load">刷新</el-button>
    </div>

    <div class="da-card history__body">
      <LoadingBlock v-if="loading" :rows="6" />
      <EmptyState
        v-else-if="!items.length"
        description="没有匹配的查询记录"
        hint="去问答页问一个问题，记录会自动出现在这里"
      >
        <router-link :to="{ name: 'chat' }">
          <el-button type="primary">去提问</el-button>
        </router-link>
      </EmptyState>

      <template v-else>
        <el-table :data="items" stripe size="small" @row-click="openDetail">
          <el-table-column label="问题" min-width="280" show-overflow-tooltip>
            <template #default="{ row }">{{ row.query_text || '—' }}</template>
          </el-table-column>
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag
                size="small"
                :type="row.status === 'success' ? 'success' : 'danger'"
                effect="plain"
              >
                {{ statusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="耗时" width="100">
            <template #default="{ row }">{{ formatDuration(row.duration_ms) }}</template>
          </el-table-column>
          <el-table-column label="时间" width="170">
            <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="140" align="right">
            <template #default="{ row }">
              <el-button link size="small" @click.stop="openDetail(row)">详情</el-button>
              <el-button link size="small" @click.stop="remove(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>

        <div class="history__pagination">
          <el-pagination
            layout="total, prev, pager, next"
            :total="total"
            :current-page="query.page"
            :page-size="query.page_size"
            @current-change="onPageChange"
          />
        </div>
      </template>
    </div>

    <el-drawer v-model="detailOpen" title="查询详情" size="60%">
      <LoadingBlock v-if="detailLoading" :rows="8" title />
      <div v-else-if="detail" class="history__detail">
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="问题">{{ detail.query_text || '—' }}</el-descriptions-item>
          <el-descriptions-item label="状态">{{ statusLabel(detail.status) }}</el-descriptions-item>
          <el-descriptions-item label="耗时">
            {{ formatDuration(detail.duration_ms) }}
          </el-descriptions-item>
          <el-descriptions-item label="时间">
            {{ formatDateTime(detail.created_at) }}
          </el-descriptions-item>
        </el-descriptions>

        <SqlBlock :sql="detail.generated_sql" />

        <div v-if="detail.explain_text">
          <div class="history__label">结果解读</div>
          <p class="history__explain">{{ detail.explain_text }}</p>
        </div>

        <div>
          <div class="history__label">结果</div>
          <!-- 历史上没有落图表推荐，用户在这里自己切；数据本身是全的 -->
          <ResultViewer
            :result="detail.result_preview"
            :filename="detail.query_text || 'history'"
            height="320px"
          />
        </div>
      </div>
      <EmptyState v-else description="记录不存在或已被删除" />
    </el-drawer>
  </div>
</template>

<style scoped>
.history__filters {
  display: flex;
  align-items: center;
  gap: var(--da-space-3);
  padding: var(--da-space-4);
  flex-wrap: wrap;
}

.history__keyword {
  width: 320px;
}

.history__status {
  width: 140px;
}

.history__body {
  padding: var(--da-space-4);
}

.history__pagination {
  display: flex;
  justify-content: flex-end;
  margin-top: var(--da-space-4);
}

.history__detail {
  display: flex;
  flex-direction: column;
  gap: var(--da-space-4);
}

.history__label {
  margin-bottom: var(--da-space-2);
  font-size: var(--da-font-sm);
  font-weight: 600;
  color: var(--da-color-text-regular);
}

.history__explain {
  margin: 0;
  padding: var(--da-space-3) var(--da-space-4);
  background: var(--da-color-bg-soft);
  border-left: 3px solid var(--da-color-primary-light-5);
  border-radius: var(--da-radius-sm);
}
</style>
