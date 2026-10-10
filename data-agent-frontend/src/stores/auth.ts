/**
 * 登录态。
 *
 * 令牌是**事实来源在 localStorage**（`utils/storage.ts`），store 里的 `user` 只是
 * 一份缓存：刷新页面后令牌还在，但 user 没了，所以路由守卫每次冷启动都要调一次
 * `/auth/me` 把它补回来。反过来把 user 当事实来源、令牌只当附属品是不行的——
 * 令牌是后端唯一认的东西。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { changePassword as changePasswordApi, fetchMe, login as loginApi } from '@/api/auth'
import type { Role, UserOut } from '@/api/types'
import { clearTokens, getAccessToken, setTokens } from '@/utils/storage'
import { roleAtLeast } from '@/utils/role'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<UserOut | null>(null)
  /** 冷启动时是否正在用令牌换用户信息（守卫要等它出结果再决定放行还是跳登录）。 */
  const loading = ref(false)
  /** 是否已经尝试过恢复登录态。避免守卫每次导航都打一次 /auth/me。 */
  const restored = ref(false)

  const isAuthenticated = computed(() => Boolean(user.value))
  const role = computed<Role | null>(() => user.value?.role ?? null)
  const displayName = computed(() => user.value?.display_name || user.value?.username || '')

  function atLeast(min: Role): boolean {
    return roleAtLeast(role.value, min)
  }

  async function login(username: string, password: string): Promise<void> {
    const tokens = await loginApi(username, password)
    setTokens(tokens)
    user.value = await fetchMe()
    restored.value = true
  }

  /**
   * 用本地令牌换当前用户。
   *
   * 失败必须清令牌：令牌签名对但用户已被删除/禁用时，`/auth/me` 返回 401，
   * 此时留着一份换不到用户的令牌，会让守卫误判为"已登录"而放进业务页，
   * 然后每个接口各自 401 —— 症状是"进去了但页面到处报错"，比直接回登录页难查得多。
   */
  async function restore(): Promise<boolean> {
    if (restored.value) {
      return isAuthenticated.value
    }
    if (!getAccessToken()) {
      restored.value = true
      return false
    }
    loading.value = true
    try {
      user.value = await fetchMe()
      return true
    } catch {
      logout()
      return false
    } finally {
      loading.value = false
      restored.value = true
    }
  }

  function logout(): void {
    clearTokens()
    user.value = null
    restored.value = true
  }

  async function changePassword(oldPassword: string, newPassword: string): Promise<void> {
    await changePasswordApi({ old_password: oldPassword, new_password: newPassword })
  }

  return {
    user,
    loading,
    restored,
    isAuthenticated,
    role,
    displayName,
    atLeast,
    login,
    restore,
    logout,
    changePassword,
  }
})
