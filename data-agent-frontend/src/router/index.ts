import { createRouter, createWebHistory } from 'vue-router'

import type { Role } from '@/api/types'
import { useAuthStore } from '@/stores/auth'

declare module 'vue-router' {
  interface RouteMeta {
    title?: string
    /** 不需要登录（只有登录页）。 */
    public?: boolean
    /**
     * 进入所需的最低角色（PRD 4.1）。
     *
     * 这是**体验层的拦截**，不是安全边界：真正的拒绝在后端依赖上（40300）。
     * 放在这里是为了让 viewer 不会点进一个注定 403 的页面——
     * 也正因如此，改这里不构成越权风险。
     */
    requiresRole?: Role
  }
}

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { title: '登录', public: true },
    },
    {
      path: '/',
      component: () => import('@/layouts/DefaultLayout.vue'),
      children: [
        { path: '', redirect: { name: 'chat' } },
        {
          path: 'chat',
          name: 'chat',
          component: () => import('@/views/ChatView.vue'),
          meta: { title: '智能问答' },
        },
        {
          // 会话落在地址栏里，刷新和"把链接发给同事"都能回到同一轮对话。
          // 这正是"重开会话时能加载历史消息"那条验收项的落点：重开 = 带 id 进来。
          path: 'chat/:conversationId',
          name: 'chat-conversation',
          component: () => import('@/views/ChatView.vue'),
          meta: { title: '智能问答' },
        },
        {
          path: 'history',
          name: 'history',
          component: () => import('@/views/HistoryView.vue'),
          meta: { title: '查询历史' },
        },
        {
          path: 'dashboard',
          name: 'dashboard',
          component: () => import('@/views/DashboardView.vue'),
          meta: { title: '数据看板', requiresRole: 'analyst' },
        },
        {
          path: 'profile',
          name: 'profile',
          component: () => import('@/views/ProfileView.vue'),
          meta: { title: '个人设置' },
        },
        {
          path: 'admin/users',
          name: 'admin-users',
          component: () => import('@/views/UsersView.vue'),
          meta: { title: '用户管理', requiresRole: 'admin' },
        },
        {
          path: 'admin/permissions',
          name: 'admin-permissions',
          component: () => import('@/views/PermissionView.vue'),
          meta: { title: '权限管理', requiresRole: 'admin' },
        },
        {
          path: 'admin/eval',
          name: 'admin-eval',
          component: () => import('@/views/EvalView.vue'),
          meta: { title: '评测看板', requiresRole: 'admin' },
        },
        {
          path: '403',
          name: 'forbidden',
          component: () => import('@/views/ForbiddenView.vue'),
          meta: { title: '无权访问' },
        },
        {
          path: ':pathMatch(.*)*',
          name: 'not-found',
          component: () => import('@/views/NotFoundView.vue'),
          meta: { title: '页面不存在' },
        },
      ],
    },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()

  if (to.meta.public) {
    // 已经登录的人再打开登录页（点后退、点书签、手敲地址），直接送回问答页。
    // 这里不能只看 store 里的 user：刷新页面后 user 是空的、令牌还在，
    // 用 restore() 才能问出"这台机器到底还有没有登录态"。
    if (to.name === 'login' && (await auth.restore())) {
      // 带 redirect 就还回那一页，与登录成功后走的是同一套逻辑。
      // 排除指回登录页的情况：手敲 /login?redirect=/login 会把自己重定向到自己，
      // vue-router 只会抛一个"疑似无限重定向"的错误，不如这里就挡掉。
      const redirect = to.query.redirect
      if (typeof redirect === 'string' && redirect && !redirect.startsWith('/login')) {
        return redirect
      }
      return { name: 'chat' }
    }
    return true
  }

  // 冷启动（刷新页面）时 user 是空的、令牌还在，先拿令牌换一次用户信息。
  // restore 只在第一次真正请求，之后直接返回缓存结果，所以每次导航不会多打接口。
  const ok = await auth.restore()
  if (!ok) {
    // 带上来源路径：登录后回到用户原本想去的页面，而不是一律跳问答页
    return { name: 'login', query: to.fullPath === '/' ? {} : { redirect: to.fullPath } }
  }

  const required = to.meta.requiresRole
  if (required && !auth.atLeast(required)) {
    return { name: 'forbidden' }
  }
  return true
})

router.afterEach((to) => {
  const title = to.meta.title
  document.title = title ? `${title} · 数据通达` : '数据通达'
})

export default router
