<script setup lang="ts">
/**
 * 用户管理（FR-ADMIN-001~004，仅管理员）。
 *
 * 界面上挡住"修改/禁用本人"（BR-AUTH-003）：后端会返回 40300，但让用户点到一个
 * 注定被拒的按钮再弹错误，不如一开始就不给这个按钮——这是一条业务规则，
 * 不是一次输入错误。
 */
import { Plus, Refresh, Search } from '@element-plus/icons-vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import { createUser, listUsers, updateUserRole, updateUserStatus } from '@/api/users'
import type { Role, UserOut } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import LoadingBlock from '@/components/LoadingBlock.vue'
import { useAuthStore } from '@/stores/auth'
import { ALL_ROLES, ROLE_LABELS } from '@/utils/role'

const auth = useAuthStore()

const loading = ref(false)
const users = ref<UserOut[]>([])
const total = ref(0)
const query = reactive({ page: 1, page_size: 20, keyword: '' })

const dialogVisible = ref(false)
const submitting = ref(false)
const formRef = ref<FormInstance>()
const form = reactive({ username: '', password: '', role: 'viewer' as Role, display_name: '' })

const rules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { max: 128, message: '用户名最长 128 个字符', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入初始密码', trigger: 'blur' },
    { min: 6, max: 32, message: '密码长度 6-32 位', trigger: 'blur' },
  ],
  role: [{ required: true, message: '请选择角色', trigger: 'change' }],
}

/** 本人不可改角色、不可禁用（BR-AUTH-003）。 */
function isSelf(user: UserOut): boolean {
  return user.id === auth.user?.id
}

const activeCount = computed(() => users.value.filter((item) => item.is_active !== false).length)

async function load() {
  loading.value = true
  try {
    const page = await listUsers({
      page: query.page,
      page_size: query.page_size,
      keyword: query.keyword || null,
    })
    users.value = page.items
    total.value = page.total
  } finally {
    loading.value = false
  }
}

function search() {
  query.page = 1
  void load()
}

/**
 * 用服务端返回的整条记录替换列表里的那一行。
 *
 * **不原地改 `row`**：表格行是 el-table 经作用域插槽交出来的，它内部另存了一份
 * 数据引用，原地改那个对象不保证能触发本组件的重渲染——症状是"改成功了但界面没变"，
 * 而它只在某些 el-table 版本/配置下出现。整体替换数组元素是可靠的：
 * 数组本身是响应式的，赋值必定触发。
 */
function replaceUser(updated: UserOut) {
  const index = users.value.findIndex((item) => item.id === updated.id)
  if (index !== -1) {
    users.value[index] = updated
  }
}

async function changeRole(user: UserOut, role: Role) {
  try {
    replaceUser(await updateUserRole(user.id, role))
    ElMessage.success(`已把 ${user.username} 的角色改为${ROLE_LABELS[role]}`)
  } catch {
    // 失败要让下拉框回到原值：`model-value` 绑的是列表里的那条记录，
    // 不回滚的话界面显示的是"改成功了"，而服务端并没有
    await load()
  }
}

async function toggleStatus(user: UserOut, isActive: boolean) {
  try {
    replaceUser(await updateUserStatus(user.id, isActive))
    ElMessage.success(isActive ? '账号已启用' : '账号已禁用')
  } catch {
    await load()
  }
}

function openCreate() {
  form.username = ''
  form.password = ''
  form.role = 'viewer'
  form.display_name = ''
  dialogVisible.value = true
}

async function submitCreate() {
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
    await createUser({
      username: form.username,
      password: form.password,
      role: form.role,
      display_name: form.display_name || null,
    })
    ElMessage.success('用户已创建')
    dialogVisible.value = false
    await load()
  } finally {
    submitting.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="da-page">
    <div class="da-page__header">
      <div>
        <h2 class="da-page__title">用户管理</h2>
        <p class="da-page__subtitle">
          共 {{ total }} 个账号，本页 {{ activeCount }} 个启用中
        </p>
      </div>
      <el-button type="primary" :icon="Plus" @click="openCreate">新建用户</el-button>
    </div>

    <div class="da-card users__filters">
      <el-input
        v-model="query.keyword"
        :prefix-icon="Search"
        placeholder="按用户名或显示名搜索"
        clearable
        class="users__keyword"
        @keyup.enter="search"
        @clear="search"
      />
      <el-button type="primary" :icon="Search" @click="search">查询</el-button>
      <el-button :icon="Refresh" @click="load">刷新</el-button>
    </div>

    <div class="da-card users__body">
      <LoadingBlock v-if="loading" :rows="6" />
      <EmptyState v-else-if="!users.length" description="没有匹配的用户" hint="换个关键词试试" />
      <template v-else>
        <el-table :data="users" stripe size="small">
          <el-table-column label="用户名" min-width="140">
            <template #default="{ row }">
              <span class="da-mono">{{ row.username }}</span>
              <el-tag v-if="isSelf(row)" size="small" effect="plain" class="users__self">本人</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="显示名" min-width="140">
            <template #default="{ row }">{{ row.display_name || '—' }}</template>
          </el-table-column>
          <el-table-column label="角色" width="160">
            <template #default="{ row }">
              <el-select
                :model-value="row.role"
                size="small"
                :disabled="isSelf(row)"
                @change="(value: Role) => changeRole(row, value)"
              >
                <el-option
                  v-for="role in ALL_ROLES"
                  :key="role"
                  :label="ROLE_LABELS[role]"
                  :value="role"
                />
              </el-select>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="120">
            <template #default="{ row }">
              <el-switch
                :model-value="row.is_active !== false"
                :disabled="isSelf(row)"
                inline-prompt
                active-text="启用"
                inactive-text="禁用"
                @change="(value: boolean) => toggleStatus(row, value)"
              />
            </template>
          </el-table-column>
          <el-table-column label="用户 ID" min-width="240" show-overflow-tooltip>
            <template #default="{ row }">
              <span class="da-mono da-text-sm da-text-secondary">{{ row.id }}</span>
            </template>
          </el-table-column>
        </el-table>

        <div class="users__pagination">
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
    </div>

    <el-dialog v-model="dialogVisible" title="新建用户" width="460px">
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top">
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" placeholder="登录名，全局唯一" />
        </el-form-item>
        <el-form-item label="显示名" prop="display_name">
          <el-input v-model="form.display_name" placeholder="可选，界面上的展示名" />
        </el-form-item>
        <el-form-item label="初始密码" prop="password">
          <el-input v-model="form.password" type="password" show-password placeholder="6-32 位" />
        </el-form-item>
        <el-form-item label="角色" prop="role">
          <el-select v-model="form.role" class="users__role-select">
            <el-option
              v-for="role in ALL_ROLES"
              :key="role"
              :label="ROLE_LABELS[role]"
              :value="role"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submitCreate">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
/* 允许换行：搜索框定宽 320px，窗口一窄就会把两个按钮挤出卡片外
   （历史页那一条筛选栏已经这么写了，这里跟上） */
.users__filters {
  display: flex;
  align-items: center;
  gap: var(--da-space-3);
  padding: var(--da-space-4);
  flex-wrap: wrap;
}

.users__keyword {
  width: 320px;
}

.users__body {
  padding: var(--da-space-4);
}

.users__self {
  margin-left: var(--da-space-2);
}

.users__pagination {
  display: flex;
  justify-content: flex-end;
  margin-top: var(--da-space-4);
}

.users__role-select {
  width: 100%;
}
</style>
