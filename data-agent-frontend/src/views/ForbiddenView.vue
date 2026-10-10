<script setup lang="ts">
/**
 * 403 页。
 *
 * 路由守卫发现角色不够时跳这里（阶段 12 验收项"数据看板页仅 analyst 及以上可见入口，
 * viewer 不可进入"）。**不静默跳回首页**：用户点了菜单（或收到一个链接）却被弹回
 * 另一个页面，会以为是自己点错了或页面坏了；明确说"你没权限"，才是可理解的反馈。
 */
import { useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
</script>

<template>
  <div class="forbidden">
    <el-result icon="warning" title="403" sub-title="当前角色无权访问该页面">
      <template #extra>
        <p class="forbidden__hint">
          当前账号角色为
          <el-tag size="small" effect="plain">{{ auth.role || '未知' }}</el-tag>
          ，该页面需要更高权限。如需访问，请联系管理员调整角色。
        </p>
        <div class="forbidden__actions">
          <el-button type="primary" @click="router.push({ name: 'chat' })">返回问答页</el-button>
          <el-button @click="router.back()">返回上一页</el-button>
        </div>
      </template>
    </el-result>
  </div>
</template>

<style scoped>
.forbidden {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 60vh;
}

.forbidden__hint {
  margin: 0 0 var(--da-space-4);
  color: var(--da-color-text-secondary);
}

.forbidden__actions {
  display: flex;
  gap: var(--da-space-2);
  justify-content: center;
}
</style>
