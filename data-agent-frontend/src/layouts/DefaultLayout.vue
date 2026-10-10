<script setup lang="ts">
/**
 * 主框架：左侧导航 + 顶栏用户区 + 内容区。
 *
 * 导航项**按角色过滤生成**，不是在模板里逐条 `v-if`：条目和它的角色要求写在一起，
 * 加一个页面时不会漏判角色；逐条写 v-if 的话，新页面很容易忘了加条件，
 * 表现为 viewer 看得见一个点进去 403 的菜单。
 */
import {
  ArrowDown,
  Clock,
  DataAnalysis,
  Expand,
  Fold,
  Lock,
  Setting,
  SwitchButton,
  TrendCharts,
  User,
  ChatDotRound,
} from '@element-plus/icons-vue'
import { ElMessageBox } from 'element-plus'
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import type { Role } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { ROLE_LABELS } from '@/utils/role'

interface NavItem {
  name: string
  label: string
  icon: unknown
  requiresRole?: Role
}

interface NavGroup {
  label: string
  items: NavItem[]
}

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const collapsed = ref(false)

const GROUPS: NavGroup[] = [
  {
    label: '分析',
    items: [
      { name: 'chat', label: '智能问答', icon: ChatDotRound },
      { name: 'history', label: '查询历史', icon: Clock },
      { name: 'dashboard', label: '数据看板', icon: DataAnalysis, requiresRole: 'analyst' },
    ],
  },
  {
    label: '系统管理',
    items: [
      { name: 'admin-users', label: '用户管理', icon: User, requiresRole: 'admin' },
      { name: 'admin-permissions', label: '权限管理', icon: Lock, requiresRole: 'admin' },
      { name: 'admin-eval', label: '评测看板', icon: TrendCharts, requiresRole: 'admin' },
    ],
  },
]

const visibleGroups = computed(() =>
  GROUPS.map((group) => ({
    label: group.label,
    items: group.items.filter((item) => !item.requiresRole || auth.atLeast(item.requiresRole)),
  })).filter((group) => group.items.length > 0),
)

const activeMenu = computed(() => {
  // 带会话 id 的问答路由（chat/:conversationId）要仍然高亮"智能问答"
  if (route.name === 'chat-conversation') {
    return 'chat'
  }
  return String(route.name ?? '')
})

const roleLabel = computed(() => (auth.role ? ROLE_LABELS[auth.role] : ''))

const pageTitle = computed(() => (route.meta.title as string | undefined) ?? '')

async function handleLogout() {
  try {
    await ElMessageBox.confirm('确定要退出登录吗？', '退出登录', {
      confirmButtonText: '退出',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return // 用户取消，不是错误
  }
  auth.logout()
  await router.replace({ name: 'login' })
}

function handleCommand(command: string) {
  if (command === 'profile') {
    void router.push({ name: 'profile' })
  } else if (command === 'logout') {
    void handleLogout()
  }
}
</script>

<template>
  <el-container class="layout">
    <el-aside :width="collapsed ? '64px' : 'var(--da-sider-width)'" class="layout__aside">
      <div class="brand">
        <span class="brand__mark">数</span>
        <span v-show="!collapsed" class="brand__text">数据通达</span>
      </div>

      <el-menu :default-active="activeMenu" :collapse="collapsed" router class="layout__menu">
        <template v-for="group in visibleGroups" :key="group.label">
          <!-- 折叠时分组标题没有位置显示，直接省略；展开时它承担"这些是同类"的信息 -->
          <div v-show="!collapsed" class="layout__group">{{ group.label }}</div>
          <el-menu-item
            v-for="item in group.items"
            :key="item.name"
            :index="item.name"
            :route="{ name: item.name }"
          >
            <el-icon><component :is="item.icon" /></el-icon>
            <template #title>{{ item.label }}</template>
          </el-menu-item>
        </template>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="layout__header">
        <div class="layout__header-left">
          <el-button link class="layout__collapse" @click="collapsed = !collapsed">
            <el-icon :size="18"><component :is="collapsed ? Expand : Fold" /></el-icon>
          </el-button>
          <span class="layout__title">{{ pageTitle }}</span>
        </div>

        <el-dropdown @command="handleCommand">
          <span class="layout__user">
            <el-avatar :size="28" class="layout__avatar">
              {{ auth.displayName.slice(0, 1) || '?' }}
            </el-avatar>
            <span class="layout__user-name">{{ auth.displayName }}</span>
            <el-tag size="small" type="info" effect="plain">{{ roleLabel }}</el-tag>
            <el-icon><ArrowDown /></el-icon>
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="profile">
                <el-icon><Setting /></el-icon>个人设置
              </el-dropdown-item>
              <el-dropdown-item command="logout" divided>
                <el-icon><SwitchButton /></el-icon>退出登录
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </el-header>

      <el-main class="layout__main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.layout {
  height: 100vh;
}

.layout__aside {
  display: flex;
  flex-direction: column;
  background: var(--da-color-bg-card);
  border-right: 1px solid var(--da-color-border-light);
  transition: width var(--da-transition);
  overflow: hidden;
}

.brand {
  display: flex;
  align-items: center;
  gap: var(--da-space-2);
  height: var(--da-header-height);
  padding: 0 var(--da-space-4);
  flex: 0 0 auto;
}

.brand__mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  flex: 0 0 auto;
  border-radius: var(--da-radius-sm);
  background: var(--da-color-primary);
  color: var(--da-color-text-inverse);
  font-weight: 600;
}

.brand__text {
  font-size: var(--da-font-md);
  font-weight: 600;
  white-space: nowrap;
}

.layout__menu {
  border-right: none;
  flex: 1 1 auto;
  overflow-y: auto;
}

.layout__group {
  padding: var(--da-space-4) var(--da-space-4) var(--da-space-1);
  font-size: var(--da-font-xs);
  color: var(--da-color-text-placeholder);
  letter-spacing: 0.04em;
}

.layout__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: var(--da-header-height);
  padding: 0 var(--da-space-5) 0 var(--da-space-3);
  background: var(--da-color-bg-card);
  border-bottom: 1px solid var(--da-color-border-light);
}

.layout__header-left {
  display: flex;
  align-items: center;
  gap: var(--da-space-2);
}

.layout__collapse {
  color: var(--da-color-text-secondary);
}

.layout__title {
  font-size: var(--da-font-md);
  font-weight: 600;
}

.layout__user {
  display: flex;
  align-items: center;
  gap: var(--da-space-2);
  cursor: pointer;
  outline: none;
  color: var(--da-color-text-regular);
}

.layout__avatar {
  background: var(--da-color-primary-light-9);
  color: var(--da-color-primary);
  font-size: var(--da-font-sm);
}

.layout__user-name {
  font-size: var(--da-font-base);
}

.layout__main {
  padding: var(--da-space-5);
  background: var(--da-color-bg-page);
  overflow-y: auto;
}
</style>
