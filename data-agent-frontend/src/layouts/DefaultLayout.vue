<script setup lang="ts"></script>

<template>
  <el-container class="layout">
    <el-header class="layout__header">
      <span class="layout__brand">数据通达</span>
      <span class="layout__subtitle">Text-to-SQL 智能数据问答</span>
    </el-header>
    <el-main class="layout__main">
      <router-view />
    </el-main>
  </el-container>
</template>

<style scoped>
.layout {
  min-height: 100vh;
}

.layout__header {
  display: flex;
  align-items: baseline;
  gap: 12px;
  border-bottom: 1px solid var(--el-border-color-light);
}

.layout__brand {
  font-size: 18px;
  font-weight: 600;
}

.layout__subtitle {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.layout__main {
  padding: 24px;
}
</style>
