import { api } from './http'
import type {
  PageData,
  PageQuery,
  PermissionOut,
  PermissionPayload,
  ResourceCatalogResponse,
  Role,
  RoleScopeOut,
} from './types'

export interface PermissionQuery extends PageQuery {
  role?: Role | null
}

/** GET /permissions —— 权限映射列表（管理员，FR-PERM-001）。 */
export function listPermissions(query: PermissionQuery = {}): Promise<PageData<PermissionOut>> {
  return api.get<PageData<PermissionOut>>('/permissions', { params: query })
}

/** GET /permissions/resources —— 可授权资源目录（表 + 字段）。 */
export function listResources(): Promise<ResourceCatalogResponse> {
  return api.get<ResourceCatalogResponse>('/permissions/resources')
}

/**
 * GET /permissions/roles/{role}/scope —— 某角色**实际生效**的范围。
 *
 * 与列表接口回答的不是同一个问题：列表说"配了什么"，这里说"算出来是什么"。
 * 一个角色没有任何配置时它返回 `unrestricted=true`，界面据此显示"未设限"
 * 而不是"什么都没配好"。
 */
export function fetchRoleScope(role: Role): Promise<RoleScopeOut> {
  return api.get<RoleScopeOut>(`/permissions/roles/${encodeURIComponent(role)}/scope`)
}

/** POST /permissions —— 新增一条映射。 */
export function createPermission(payload: PermissionPayload): Promise<PermissionOut> {
  return api.post<PermissionOut>('/permissions', payload)
}

/** PUT /permissions/{id} —— 修改映射（三个字段整体替换）。 */
export function updatePermission(id: string, payload: PermissionPayload): Promise<PermissionOut> {
  return api.put<PermissionOut>(`/permissions/${encodeURIComponent(id)}`, payload)
}

/** DELETE /permissions/{id} —— 删除映射，该资源对该角色恢复可见。 */
export function deletePermission(id: string): Promise<null> {
  return api.delete<null>(`/permissions/${encodeURIComponent(id)}`)
}
