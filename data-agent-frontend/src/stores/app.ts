import { defineStore } from 'pinia'
import { ref } from 'vue'

/**
 * 应用级状态。阶段 1 仅存后端连通性，用于验证前后端打通；
 * 会话、用户等状态在阶段 3/12 按模块建独立 store。
 */
export const useAppStore = defineStore('app', () => {
  const backendReachable = ref<boolean | null>(null)

  function setBackendReachable(reachable: boolean) {
    backendReachable.value = reachable
  }

  return { backendReachable, setBackendReachable }
})
