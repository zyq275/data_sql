<script setup lang="ts">
/**
 * SQL 代码块（FR-QA 的"结果溯源"：看到结果的同时能看到它是怎么查出来的）。
 *
 * 复制按钮不是装饰：界面上显示的 SQL 是要被拿去改一改、贴到自己的客户端里跑一遍的
 * （S3 结果溯源的实际用法就是"我核对一下这个数怎么来的"）。让人用鼠标划选一段
 * 带缩进的 SQL 很容易漏掉首行或末尾的分号。
 */
import { ElMessage } from 'element-plus'
import { computed, ref } from 'vue'

const props = defineProps<{
  sql: string | null | undefined
  collapsible?: boolean
}>()

const collapsed = ref(false)

const text = computed(() => (props.sql ?? '').trim())

async function copy() {
  if (!text.value) {
    return
  }
  try {
    await navigator.clipboard.writeText(text.value)
    ElMessage.success('SQL 已复制')
  } catch {
    // 非 HTTPS 或无剪贴板权限时走这条。不静默失败——用户按了按钮就该有反馈
    ElMessage.warning('复制失败，请手动选择文本')
  }
}
</script>

<template>
  <div v-if="text" class="sql">
    <div class="sql__bar">
      <span class="sql__label">生成的 SQL</span>
      <div class="sql__actions">
        <el-button v-if="collapsible" link size="small" @click="collapsed = !collapsed">
          {{ collapsed ? '展开' : '收起' }}
        </el-button>
        <el-button link size="small" @click="copy">复制</el-button>
      </div>
    </div>
    <pre v-show="!collapsed" class="da-code">{{ text }}</pre>
  </div>
</template>

<style scoped>
.sql__bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--da-space-2);
}

.sql__label {
  font-size: var(--da-font-sm);
  font-weight: 600;
  color: var(--da-color-text-regular);
}

.sql__actions {
  display: flex;
  gap: var(--da-space-2);
}
</style>
