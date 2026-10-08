<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { onMounted, ref } from 'vue'

import { fetchHealth, type HealthPayload } from '@/api/system'
import { useAppStore } from '@/stores/app'

const appStore = useAppStore()
const loading = ref(false)
const health = ref<HealthPayload | null>(null)

async function checkBackend() {
  loading.value = true
  try {
    health.value = await fetchHealth()
    appStore.setBackendReachable(true)
  } catch (error) {
    health.value = null
    appStore.setBackendReachable(false)
    ElMessage.error('后端探针调用失败，请确认后端已启动')
    console.error(error)
  } finally {
    loading.value = false
  }
}

onMounted(checkBackend)
</script>

<template>
  <div class="home">
    <el-alert
      title="阶段 1 骨架"
      type="info"
      :closable="false"
      description="问答页、历史页、看板页、权限管理页等在阶段 12 落地。此页仅用于验证前端可启动、代理可通、后端探针可达。"
      show-icon
    />

    <el-card class="home__card">
      <template #header>后端连通性</template>
      <el-space direction="vertical" alignment="flex-start" :size="12">
        <el-button type="primary" :loading="loading" @click="checkBackend">
          重新检测
        </el-button>
        <el-descriptions v-if="health" :column="1" border>
          <el-descriptions-item label="服务">{{ health.app }}</el-descriptions-item>
          <el-descriptions-item label="版本">{{ health.version }}</el-descriptions-item>
          <el-descriptions-item label="状态">{{ health.status }}</el-descriptions-item>
          <el-descriptions-item label="debug">{{ health.debug }}</el-descriptions-item>
        </el-descriptions>
        <el-empty v-else description="未获取到探针响应" />
      </el-space>
    </el-card>
  </div>
</template>

<style scoped>
.home__card {
  margin-top: 16px;
}
</style>
