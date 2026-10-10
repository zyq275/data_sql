<script setup lang="ts">
/**
 * 统一的空状态占位（阶段 12 验收项"空数据有占位"）。
 *
 * 各页面用同一个组件而不是各自 `v-if="!list.length"` 塞一句文字：
 * 空状态的形状（图标、留白、有没有操作按钮）一旦各写各的，
 * 同一个应用里会出现五六种"没有数据"的样子，"统一"这条验收项就没法核对了。
 *
 * `description` 要写**为什么空**，不是写"暂无数据"。空历史与空搜索结果
 * 该说的话不同：前者要引导去问第一个问题，后者要建议换个关键词。
 */
withDefaults(
  defineProps<{
    description?: string
    /** 次要说明，用于补充"接下来能做什么"。 */
    hint?: string
    imageSize?: number
  }>(),
  { description: '暂无数据', hint: '', imageSize: 96 },
)
</script>

<template>
  <div class="empty">
    <el-empty :image-size="imageSize" :description="description">
      <template v-if="hint" #description>
        <p class="empty__description">{{ description }}</p>
        <p class="empty__hint">{{ hint }}</p>
      </template>
      <slot />
    </el-empty>
  </div>
</template>

<style scoped>
.empty {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--da-space-6) 0;
}

.empty__description {
  margin: 0;
  color: var(--da-color-text-regular);
}

.empty__hint {
  margin: var(--da-space-1) 0 0;
  font-size: var(--da-font-sm);
  color: var(--da-color-text-secondary);
}
</style>
