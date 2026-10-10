<script setup lang="ts">
/**
 * 统一的加载状态（阶段 12 验收项"加载有状态"）。
 *
 * 为什么不用 `v-loading`：它是盖在内容上的一层遮罩，内容为空时遮罩底下什么都没有，
 * 用户看到的是一片白加一个转圈，不知道将来会出现几行什么。骨架屏先把版式占住，
 * 布局不会在数据到达时跳一下——列表页尤其明显，那种"抖一下"很廉价。
 *
 * 用 el-skeleton 的 rows 而不是自己写灰条：它的动画与深浅色都跟 Element Plus
 * 保持一致，自己写的灰条遇到深色主题就不对了。
 */
withDefaults(
  defineProps<{
    rows?: number
    /** 是否带一块标题条，用于"上面一个标题、下面一段内容"的页面区块。 */
    title?: boolean
  }>(),
  { rows: 4, title: false },
)
</script>

<template>
  <div class="loading">
    <el-skeleton animated>
      <template #template>
        <div class="loading__body">
          <el-skeleton-item v-if="title" variant="h3" class="loading__title" />
          <el-skeleton-item v-for="index in rows" :key="index" variant="text" />
        </div>
      </template>
    </el-skeleton>
  </div>
</template>

<style scoped>
.loading {
  padding: var(--da-space-4) 0;
}

.loading__body {
  display: flex;
  flex-direction: column;
  gap: var(--da-space-3);
}

.loading__title {
  width: 30%;
  margin-bottom: var(--da-space-1);
}
</style>
