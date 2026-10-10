<script setup lang="ts">
/**
 * ECharts 容器。
 *
 * 只负责三件事：建实例、把 option 喂进去、跟着容器尺寸变化 resize。
 * 第三件最容易漏——侧边栏折叠、窗口缩放、tab 切换都会改容器宽度，
 * 不 resize 的话图会停在旧尺寸上，一半被裁掉，看起来像"图表渲染坏了"。
 *
 * 用 ResizeObserver 而不是监听 window 的 resize：容器因布局变化而不因窗口变化时
 * （折叠侧边栏就是典型），window 上根本不会触发事件。
 */
import type { EChartsOption } from 'echarts'
import { onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'

import { echarts } from '@/utils/echarts'

const props = withDefaults(
  defineProps<{
    option: EChartsOption | null
    height?: string
  }>(),
  { height: '320px' },
)

/** 实例类型从 init 推导，不额外 import 类型：echarts 各入口导出的类型名不一致。 */
type EChartsInstance = ReturnType<typeof echarts.init>

const container = ref<HTMLDivElement | null>(null)
// shallowRef：ECharts 实例内部有大量自引用，深层响应式代理会拖慢渲染，
// 严重时还会因为代理破坏了内部对象身份而报错
const chart = shallowRef<EChartsInstance | null>(null)
let observer: ResizeObserver | null = null

function render() {
  if (!chart.value) {
    return
  }
  // notMerge = true：切换图表类型时，旧的 series 数量与新的不同
  // （比如折线两条、饼图一条），合并会让上一张图的残留系列留在画布上
  chart.value.setOption(props.option ?? {}, true)
}

onMounted(() => {
  if (!container.value) {
    return
  }
  chart.value = echarts.init(container.value)
  render()
  observer = new ResizeObserver(() => chart.value?.resize())
  observer.observe(container.value)
})

watch(() => props.option, render, { deep: false })

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
  chart.value?.dispose()
  chart.value = null
})
</script>

<template>
  <div ref="container" class="chart" :style="{ height }" />
</template>

<style scoped>
.chart {
  width: 100%;
}
</style>
