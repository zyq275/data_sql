/**
 * 角色与权限范围（PRD 4.1）。
 *
 * 前端的角色判断**只用来决定入口是否显示**，不是安全边界——
 * 真正的拦截在后端的 `require_min_role` / `require_admin` 依赖上。
 * 写在这里的作用是：viewer 不会看到一个点进去注定 403 的菜单项。
 * 所以这里漏判的后果是"某个入口不该显示却显示了"，不是"越权拿到了数据"。
 */

import type { Role } from '@/api/types'

export const ROLE_LABELS: Record<Role, string> = {
  viewer: '查看者',
  analyst: '分析师',
  admin: '管理员',
}

/** 由宽到窄的排序依据：analyst 及以上 = rank >= 1。 */
const ROLE_RANK: Record<Role, number> = {
  viewer: 0,
  analyst: 1,
  admin: 2,
}

/** 该角色是否达到 `min` 级别（含）。 */
export function roleAtLeast(role: Role | null | undefined, min: Role): boolean {
  if (!role) {
    return false
  }
  return ROLE_RANK[role] >= ROLE_RANK[min]
}

export const ALL_ROLES: Role[] = ['viewer', 'analyst', 'admin']
