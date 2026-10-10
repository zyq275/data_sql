<script setup lang="ts">
/**
 * 数据权限管理（FR-PERM-001~004，阶段 9；仅管理员）。
 *
 * 两个页签回答的是两个不同的问题，缺一不可：
 *   - **权限映射**：我配了什么（可增删改）；
 *   - **生效范围**：按网关的口径算出这个角色**实际**能看见什么。
 * 只有前者时，一个什么都没配的角色看起来像"什么都没配好"，实际上它是
 * `unrestricted` ——不受限。这两者的差别在出了问题时要排查很久，
 * 所以这里把 `unrestricted` 显式地显示出来，而不是让人从空白去猜。
 */
import { Plus, Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import {
  createPermission,
  deletePermission,
  fetchRoleScope,
  listPermissions,
  listResources,
  updatePermission,
} from '@/api/permission'
import type {
  PermissionOut,
  PermissionPayload,
  ResourceOut,
  Role,
  RoleScopeOut,
} from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import LoadingBlock from '@/components/LoadingBlock.vue'
import { ALL_ROLES, ROLE_LABELS } from '@/utils/role'

const activeTab = ref<'mappings' | 'scope'>('mappings')

// ---- 页签一：权限映射 ----

const loading = ref(false)
const items = ref<PermissionOut[]>([])
const total = ref(0)
const query = reactive({ page: 1, page_size: 20, role: '' as Role | '' })

const catalog = ref<ResourceOut[]>([])
const catalogLoading = ref(false)

const dialogVisible = ref(false)
const submitting = ref(false)
const editingId = ref<string | null>(null)
const formRef = ref<FormInstance>()
const form = reactive<PermissionPayload>({
  role: 'viewer',
  resource_type: 'table',
  resource_id: '',
})

const rules: FormRules = {
  role: [{ required: true, message: '请选择角色', trigger: 'change' }],
  resource_type: [{ required: true, message: '请选择资源类型', trigger: 'change' }],
  resource_id: [{ required: true, message: '请选择具体的表或字段', trigger: 'change' }],
}

/** 当前类型下可选的资源。resource_id 是 uuid5，人认不出来，必须给名字对照。 */
const resourceOptions = computed(() =>
  catalog.value.filter((item) => item.resource_type === form.resource_type),
)

async function loadCatalog() {
  if (catalog.value.length) {
    return
  }
  catalogLoading.value = true
  try {
    const data = await listResources()
    catalog.value = [...data.tables, ...data.columns]
  } finally {
    catalogLoading.value = false
  }
}

async function load() {
  loading.value = true
  try {
    const page = await listPermissions({
      page: query.page,
      page_size: query.page_size,
      role: query.role || null,
    })
    items.value = page.items
    total.value = page.total
  } finally {
    loading.value = false
  }
}

function search() {
  query.page = 1
  void load()
}

async function openCreate() {
  editingId.value = null
  form.role = 'viewer'
  form.resource_type = 'table'
  form.resource_id = ''
  await loadCatalog()
  dialogVisible.value = true
}

async function openEdit(item: PermissionOut) {
  editingId.value = item.id
  form.role = item.role
  form.resource_type = item.resource_type
  form.resource_id = item.resource_id
  await loadCatalog()
  dialogVisible.value = true
}

async function submit() {
  const formEl = formRef.value
  if (!formEl) {
    return
  }
  const valid = await formEl.validate().catch(() => false)
  if (!valid) {
    return
  }
  submitting.value = true
  try {
    const payload: PermissionPayload = { ...form }
    if (editingId.value) {
      await updatePermission(editingId.value, payload)
      ElMessage.success('权限映射已更新')
    } else {
      await createPermission(payload)
      ElMessage.success('权限映射已新增')
    }
    dialogVisible.value = false
    await load()
    // 生效范围可能已经变了，清掉缓存让用户切过去时看到的是最新的
    scope.value = null
  } finally {
    submitting.value = false
  }
}

async function remove(item: PermissionOut) {
  const name = item.resource_name || item.resource_id
  try {
    await ElMessageBox.confirm(
      `删除后，${ROLE_LABELS[item.role]} 将重新看到「${name}」。确定删除？`,
      '删除权限映射',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  await deletePermission(item.id)
  ElMessage.success('已删除')
  scope.value = null
  await load()
}

// ---- 页签二：生效范围 ----

const scopeRole = ref<Role>('viewer')
const scopeLoading = ref(false)
const scope = ref<RoleScopeOut | null>(null)

async function loadScope(role: Role) {
  scopeRole.value = role
  scopeLoading.value = true
  scope.value = null
  try {
    scope.value = await fetchRoleScope(role)
  } finally {
    scopeLoading.value = false
  }
}

/** 有权限的表 → 允许的字段；值为 null 表示整表放行。 */
const scopeTables = computed(() => Object.entries(scope.value?.tables ?? {}))

function onTabChange(name: string | number) {
  if (name === 'scope' && !scope.value) {
    void loadScope(scopeRole.value)
  }
}

onMounted(load)
</script>

<template>
  <div class="da-page">
    <div class="da-page__header">
      <div>
        <h2 class="da-page__title">权限管理</h2>
        <p class="da-page__subtitle">配的是"不能看什么"：未配置的资源默认可见</p>
      </div>
    </div>

    <el-tabs v-model="activeTab" class="da-card perm__tabs" @tab-change="onTabChange">
      <el-tab-pane label="权限映射" name="mappings">
        <div class="perm__toolbar">
          <el-select
            v-model="query.role"
            placeholder="全部角色"
            clearable
            class="perm__role-filter"
            @change="search"
          >
            <el-option
              v-for="role in ALL_ROLES"
              :key="role"
              :label="ROLE_LABELS[role]"
              :value="role"
            />
          </el-select>
          <el-button :icon="Refresh" @click="load">刷新</el-button>
          <div class="perm__spacer" />
          <el-button type="primary" :icon="Plus" @click="openCreate">新增映射</el-button>
        </div>

        <LoadingBlock v-if="loading" :rows="5" />
        <EmptyState
          v-else-if="!items.length"
          description="没有任何权限限制"
          hint="当前所有角色都能看到全部表与字段；需要屏蔽敏感数据时点「新增映射」"
        >
          <el-button type="primary" :icon="Plus" @click="openCreate">新增映射</el-button>
        </EmptyState>

        <template v-else>
          <el-table :data="items" stripe size="small">
            <el-table-column label="角色" width="120">
              <template #default="{ row }">
                <el-tag size="small" effect="plain">{{ ROLE_LABELS[row.role as Role] }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="类型" width="100">
              <template #default="{ row }">
                {{ row.resource_type === 'table' ? '数据表' : '字段' }}
              </template>
            </el-table-column>
            <el-table-column label="资源" min-width="200">
              <template #default="{ row }">
                {{ row.resource_name || row.resource_id }}
              </template>
            </el-table-column>
            <el-table-column label="所属表" min-width="160">
              <template #default="{ row }">{{ row.table_name || '—' }}</template>
            </el-table-column>
            <el-table-column label="操作" width="140" align="right">
              <template #default="{ row }">
                <el-button link size="small" @click="openEdit(row)">编辑</el-button>
                <el-button link size="small" @click="remove(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>

          <div class="perm__pagination">
            <el-pagination
              layout="total, prev, pager, next"
              :total="total"
              :current-page="query.page"
              :page-size="query.page_size"
              @current-change="
                (page: number) => {
                  query.page = page
                  load()
                }
              "
            />
          </div>
        </template>
      </el-tab-pane>

      <el-tab-pane label="生效范围" name="scope">
        <div class="perm__toolbar">
          <el-radio-group :model-value="scopeRole" @change="(role: Role) => loadScope(role)">
            <el-radio-button v-for="role in ALL_ROLES" :key="role" :value="role">
              {{ ROLE_LABELS[role] }}
            </el-radio-button>
          </el-radio-group>
          <el-button :icon="Refresh" @click="loadScope(scopeRole)">重新计算</el-button>
        </div>

        <LoadingBlock v-if="scopeLoading" :rows="5" title />

        <template v-else-if="scope">
          <el-alert
            v-if="scope.unrestricted"
            type="warning"
            :closable="false"
            show-icon
            title="该角色不受限"
            description="没有任何权限映射命中这个角色，因此它可以查询全部表与字段。"
          />
          <el-alert
            v-else
            type="success"
            :closable="false"
            show-icon
            title="该角色受限"
            description="下面是按网关口径算出的实际可见范围。"
          />

          <div v-if="!scope.unrestricted" class="perm__scope">
            <div class="perm__scope-block">
              <div class="perm__scope-label">可见的表与字段（{{ scopeTables.length }} 张）</div>
              <EmptyState
                v-if="!scopeTables.length"
                :image-size="64"
                description="没有任何表可见"
                hint="这个角色现在问不出任何数据"
              />
              <ul v-else class="perm__table-list">
                <li v-for="[table, columns] in scopeTables" :key="table" class="perm__table-item">
                  <span class="da-mono">{{ table }}</span>
                  <el-tag v-if="columns === null" size="small" type="success" effect="plain">
                    整表可见
                  </el-tag>
                  <span v-else class="perm__columns">
                    <el-tag
                      v-for="column in columns"
                      :key="column"
                      size="small"
                      effect="plain"
                      class="perm__column-tag"
                    >
                      {{ column }}
                    </el-tag>
                  </span>
                </li>
              </ul>
            </div>

            <div class="perm__scope-block">
              <div class="perm__scope-label">禁入的表（{{ scope.forbidden_tables.length }}）</div>
              <div v-if="scope.forbidden_tables.length" class="perm__tags">
                <el-tag
                  v-for="name in scope.forbidden_tables"
                  :key="name"
                  size="small"
                  type="danger"
                  effect="plain"
                >
                  {{ name }}
                </el-tag>
              </div>
              <p v-else class="da-text-sm da-text-secondary">无</p>
            </div>

            <div class="perm__scope-block">
              <div class="perm__scope-label">禁入的字段（{{ scope.forbidden_columns.length }}）</div>
              <div v-if="scope.forbidden_columns.length" class="perm__tags">
                <el-tag
                  v-for="name in scope.forbidden_columns"
                  :key="name"
                  size="small"
                  type="danger"
                  effect="plain"
                >
                  {{ name }}
                </el-tag>
              </div>
              <p v-else class="da-text-sm da-text-secondary">无</p>
            </div>

            <div class="perm__scope-block">
              <div class="perm__scope-label">可用指标（{{ scope.metrics.length }}）</div>
              <div v-if="scope.metrics.length" class="perm__tags">
                <el-tag
                  v-for="name in scope.metrics"
                  :key="name"
                  size="small"
                  effect="plain"
                >
                  {{ name }}
                </el-tag>
              </div>
              <p v-else class="da-text-sm da-text-secondary">无</p>
            </div>
          </div>
        </template>

        <EmptyState v-else description="选择一个角色查看生效范围" />
      </el-tab-pane>
    </el-tabs>

    <el-dialog
      v-model="dialogVisible"
      :title="editingId ? '编辑权限映射' : '新增权限映射'"
      width="480px"
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top">
        <el-form-item label="角色" prop="role">
          <el-select v-model="form.role" class="perm__full">
            <el-option
              v-for="role in ALL_ROLES"
              :key="role"
              :label="ROLE_LABELS[role]"
              :value="role"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="资源类型" prop="resource_type">
          <el-radio-group
            v-model="form.resource_type"
            @change="
              () => {
                // 换类型后原来的 id 一定不属于新类型，留着会提交一个驴唇不对马嘴的组合
                form.resource_id = ''
              }
            "
          >
            <el-radio-button value="table">数据表</el-radio-button>
            <el-radio-button value="column">字段</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="资源" prop="resource_id">
          <el-select
            v-model="form.resource_id"
            filterable
            :loading="catalogLoading"
            placeholder="按名称搜索"
            class="perm__full"
          >
            <el-option
              v-for="item in resourceOptions"
              :key="item.id"
              :label="
                form.resource_type === 'table'
                  ? `${item.name}${item.table_alias ? `（${item.table_alias}）` : ''}`
                  : `${item.table_name}.${item.name}`
              "
              :value="item.id"
            />
          </el-select>
        </el-form-item>
        <el-alert
          type="info"
          :closable="false"
          :title="
            editingId
              ? '保存后立即按新范围生效。'
              : '取消勾选即授权：这里配的是「不允许」，未配置的资源默认可见。'
          "
        />
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.perm__tabs {
  padding: var(--da-space-4) var(--da-space-5) var(--da-space-5);
}

.perm__toolbar {
  display: flex;
  align-items: center;
  gap: var(--da-space-3);
  margin-bottom: var(--da-space-4);
  flex-wrap: wrap;
}

.perm__spacer {
  flex: 1 1 auto;
}

.perm__role-filter {
  width: 160px;
}

.perm__pagination {
  display: flex;
  justify-content: flex-end;
  margin-top: var(--da-space-4);
}

.perm__scope {
  display: flex;
  flex-direction: column;
  gap: var(--da-space-5);
  margin-top: var(--da-space-4);
}

.perm__scope-label {
  margin-bottom: var(--da-space-2);
  font-size: var(--da-font-sm);
  font-weight: 600;
  color: var(--da-color-text-regular);
}

.perm__table-list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: var(--da-space-2);
}

.perm__table-item {
  display: flex;
  align-items: baseline;
  gap: var(--da-space-3);
  flex-wrap: wrap;
  padding: var(--da-space-2) var(--da-space-3);
  background: var(--da-color-bg-soft);
  border-radius: var(--da-radius-sm);
}

.perm__columns {
  display: flex;
  flex-wrap: wrap;
  gap: var(--da-space-1);
}

.perm__tags {
  display: flex;
  flex-wrap: wrap;
  gap: var(--da-space-1);
}

.perm__full {
  width: 100%;
}
</style>
