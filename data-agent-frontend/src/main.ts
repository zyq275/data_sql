import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import { onUnauthorized } from './api/http'
import router from './router'
import { useAuthStore } from './stores/auth'
// 样式加载顺序有语义，不要调换：
//   1. EP 自己的样式（定义一套默认的 --el-* 变量）；
//   2. 设计 Token（我们的 --da-*，并把 --el-* 映射过去）；
//   3. 基础样式与工具类（消费 Token）。
// 把 Token 提到 EP 之前，映射会被 EP 的默认值覆盖，全局就只有默认蓝了。
import 'element-plus/dist/index.css'
import './styles/tokens.css'
import './styles/base.css'

const app = createApp(App)

app.use(createPinia())
app.use(router)
app.use(ElementPlus, { locale: zhCn })

// 令牌彻底失效时的去向，在这里接上而不是让 api/http.ts 直接 import router：
// router → 守卫 → auth store → api/auth → api/http，静态 import 会绕成一个环。
// 在这里注册，依赖方向就是单向的（见 api/http.ts 的 onUnauthorized 说明）。
onUnauthorized(() => {
  const auth = useAuthStore()
  auth.logout()
  void router.replace({ name: 'login' })
})

app.mount('#app')
