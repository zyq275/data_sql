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
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import type { Role } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { peekInflight } from '@/utils/inflight'
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

/**
 * 要保活的路由组件名——`keep-alive` 的 include 按**组件名**匹配，不是路由名。
 *
 * 只放问答页：它是唯一一个"组件一卸载，正在跑的东西就没了"的页面
 * （ChatView 的 `onBeforeUnmount` 会掐断正在流的那条连接）。
 */
const KEEP_ALIVE = ['ChatView']

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

onMounted(() => {
  // 上一次加载要是死在流式中间（刷新把那一轮掐断了），这里把人送回那一轮所在的会话。
  //
  // 为什么由框架层来做：提示与"重新提问"都在问答页（那里才有会话与提问框），
  // 而刷新时用户完全可能停在别的页面（比如提问中切到查询历史再刷新）。
  // 没有这一步，提示要么弹不出来，要么等到几小时后他随手点进问答页才莫名冒出来。
  //
  // 只 `peek`（读而不擦）不 `take`：痕迹要留给问答页去弹提示——这一侧看的时候
  // 顺手擦掉的话，问答页挂载起来就什么都不剩了。
  const lost = peekInflight()
  if (!lost?.conversationId) {
    return
  }
  if (route.params.conversationId === lost.conversationId) {
    return // 已经停在那条会话上，问答页挂载后会自己弹
  }
  void router.replace({
    name: 'chat-conversation',
    params: { conversationId: lost.conversationId },
  })
})

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
        <!--
          只保活问答页。一轮问答是一条长连接，ChatView 卸载时会主动掐断它
          （见它的 `onBeforeUnmount(abortStreaming)`）——切到别的页面再回来，
          那一轮就没了。保活之后切页面只是 deactivate，组件与它手里的事件流都还在。

          其余页面**故意不保活**：它们进页面就重新拉一次数据，缓存一份只会让人
          看到过期的数字，而"这块数字是刚才那一眼的"在界面上完全看不出来。
        -->
        <router-view v-slot="{ Component }">
          <keep-alive :include="KEEP_ALIVE">
            <component :is="Component" />
          </keep-alive>
        </router-view>
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
  /* 折叠是"改变尺寸"那一档，用 slow；跟颜色类反馈的 fast/base 分开 */
  transition: width var(--da-duration-slow) var(--da-ease-out);
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
  background: linear-gradient(135deg, var(--da-color-primary), var(--da-color-primary-active));
  color: var(--da-color-text-inverse);
  font-weight: 600;
  box-shadow: var(--da-shadow-sm);
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
  /* 悬停底色与选中文字色都走 EP 暴露的变量（不覆盖它的类名，理由见 tokens.css）。
   *
   * 悬停底色**刻意偏离** EP 的默认值：它默认也是 `--el-color-primary-light-9`，
   * 和下面选中的底色一模一样——鼠标划过某一项时，那一项看起来"被选中了"。
   * 悬停用中性灰，选中才用主色，两种状态才分得开。 */
  --el-menu-hover-bg-color: var(--da-color-bg-hover);
  --el-menu-active-color: var(--da-color-primary);
}

/*
 * 菜单项做成圆角胶囊 + 选中指示条。
 *
 * EP 的垂直菜单默认只在选中项上改**文字颜色**，一列灰字里那点蓝很弱，
 * 切页面时"我现在在哪"要靠仔细看才认得出来。而 EP 没有暴露"选中项背景"的变量
 * （`--el-menu-active-color` 只管文字色），所以这一处只能落到它的类名上。
 */
.layout__menu :deep(.el-menu-item) {
  height: 40px;
  line-height: 40px;
  margin: 2px var(--da-space-2);
  border-radius: var(--da-radius-md);
  transition:
    background-color var(--da-duration-fast) var(--da-ease-out),
    color var(--da-duration-fast) var(--da-ease-out);
}

.layout__menu :deep(.el-menu-item.is-active) {
  position: relative;
  background: var(--da-color-primary-light-9);
  font-weight: 600;
}

/* 指示条独立于底色：底色在浅色屏上可能被看漏，一条实心竖条不会。
   折叠态（64px 宽）下它没有容身之处，直接隐藏。 */
.layout__menu :deep(.el-menu-item.is-active)::before {
  content: '';
  position: absolute;
  left: 0;
  top: 50%;
  transform: translateY(-50%);
  width: 3px;
  height: 18px;
  border-radius: var(--da-radius-pill);
  background: var(--da-color-primary);
}

.layout__menu.el-menu--collapse :deep(.el-menu-item.is-active)::before {
  display: none;
}

.layout__menu.el-menu--collapse :deep(.el-menu-item) {
  margin: 2px var(--da-space-1);
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

/* 折叠按钮：EP 的 link 按钮默认只改文字色，鼠标停上去几乎没反应。
 * 给它一块方形底色，让它看起来是一个"按钮"而不是一行灰字。 */
.layout__collapse {
  width: 30px;
  height: 30px;
  padding: 0;
  color: var(--da-color-text-secondary);
  border-radius: var(--da-radius-sm);
  transition:
    color var(--da-duration-fast) var(--da-ease-out),
    background-color var(--da-duration-fast) var(--da-ease-out);
}

.layout__collapse:hover {
  color: var(--da-color-primary);
  background-color: var(--da-color-bg-hover);
}

.layout__title {
  font-size: var(--da-font-md);
  font-weight: 600;
}

/* 用户区整块是一个下拉触发器，但视觉上它只是一排字，看不出可以点。
 * 补一圈悬停底色（胶囊形），点与不点的界线就有了。 */
.layout__user {
  display: flex;
  align-items: center;
  gap: var(--da-space-2);
  cursor: pointer;
  outline: none;
  color: var(--da-color-text-regular);
  padding: var(--da-space-1) var(--da-space-2);
  border-radius: var(--da-radius-pill);
  transition: background-color var(--da-duration-fast) var(--da-ease-out);
}

.layout__user:hover {
  background-color: var(--da-color-bg-hover);
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
