<script setup lang="ts">
/**
 * 登录页（分栏式）。
 *
 * 左栏是品牌与能力说明，右栏是表单。左栏在窄屏隐藏而不是缩小——
 * 挤到 320px 宽的营销文字比没有它更难看，而登录表单必须始终完整可见。
 *
 * 表单校验只做**格式与必填**，长度按后端的约束来（用户名 1-128、密码 6-32，
 * PRD FR-AUTH-001）。不做"密码强度"之类的额外校验：这里是登录不是注册，
 * 拦住老用户的旧密码只会让人登不进来——真正的判断在后端。
 */
import { Lock, User } from '@element-plus/icons-vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const formRef = ref<FormInstance>()
const loading = ref(false)
const form = reactive({ username: '', password: '' })

const rules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { max: 128, message: '用户名最长 128 个字符', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, max: 32, message: '密码长度 6-32 位', trigger: 'blur' },
  ],
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

  loading.value = true
  try {
    await auth.login(form.username, form.password)
    ElMessage.success('登录成功')
    // 回到被拦下的那一页；没有来源就去问答页
    const redirect = route.query.redirect
    await router.replace(typeof redirect === 'string' && redirect ? redirect : { name: 'chat' })
  } catch (error) {
    // 错误提示已由 http 拦截器统一弹出（分不清用户名错还是密码错是后端有意为之：
    // 40100 的 message 是"用户名或密码错误"这类不区分两者的文案）
    form.password = ''
    void error
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login">
    <section class="login__intro">
      <div class="login__intro-inner">
        <div class="login__brand">
          <span class="login__mark">数</span>
          <span class="login__brand-text">数据通达</span>
        </div>
        <h1 class="login__headline">用一句话<br />问出你要的数据</h1>
        <p class="login__desc">
          自然语言提问，自动理解指代、检索口径、生成并校验 SQL，
          结果附带 SQL 与自然语言解释，可看图、可导出。
        </p>
        <ul class="login__points">
          <li>多轮追问，指代自动补全</li>
          <li>结果可溯源：SQL 与解释一并给出</li>
          <li>按角色管控数据权限与看板入口</li>
        </ul>
      </div>
    </section>

    <section class="login__panel">
      <div class="login__form-wrap">
        <h2 class="login__title">登录</h2>
        <p class="login__subtitle">请使用系统分配的账号登录</p>

        <el-form
          ref="formRef"
          :model="form"
          :rules="rules"
          size="large"
          label-position="top"
          @submit.prevent="submit"
        >
          <el-form-item label="用户名" prop="username">
            <el-input v-model="form.username" :prefix-icon="User" placeholder="请输入用户名" />
          </el-form-item>
          <el-form-item label="密码" prop="password">
            <el-input
              v-model="form.password"
              type="password"
              show-password
              :prefix-icon="Lock"
              placeholder="请输入密码"
              @keyup.enter="submit"
            />
          </el-form-item>
          <el-button
            type="primary"
            size="large"
            class="login__submit"
            :loading="loading"
            @click="submit"
          >
            登录
          </el-button>
        </el-form>
      </div>
    </section>
  </div>
</template>

<style scoped>
.login {
  display: flex;
  height: 100vh;
}

.login__intro {
  /* 主色深压一档做底：左侧是背景，右侧表单才是主体，
     同色同亮度会让眼睛不知道该落哪儿 */
  flex: 1 1 52%;
  display: flex;
  align-items: center;
  background: linear-gradient(
    150deg,
    var(--da-color-primary) 0%,
    var(--da-color-primary-active) 100%
  );
  color: var(--da-color-text-inverse);
  padding: var(--da-space-6);
}

.login__intro-inner {
  max-width: 420px;
  margin: 0 auto;
}

.login__brand {
  display: flex;
  align-items: center;
  gap: var(--da-space-2);
  margin-bottom: var(--da-space-6);
}

.login__mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: var(--da-radius-sm);
  background: rgba(255, 255, 255, 0.18);
  font-weight: 600;
}

.login__brand-text {
  font-size: var(--da-font-lg);
  font-weight: 600;
}

.login__headline {
  margin: 0 0 var(--da-space-4);
  font-size: var(--da-font-display);
  line-height: var(--da-line-height-tight);
}

.login__desc {
  margin: 0 0 var(--da-space-5);
  font-size: var(--da-font-base);
  line-height: var(--da-line-height-base);
  color: rgba(255, 255, 255, 0.86);
}

.login__points {
  margin: 0;
  padding-left: var(--da-space-5);
  font-size: var(--da-font-sm);
  line-height: 2;
  color: rgba(255, 255, 255, 0.8);
}

.login__panel {
  flex: 1 1 48%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--da-color-bg-card);
  padding: var(--da-space-6);
  overflow-y: auto;
}

.login__form-wrap {
  width: 100%;
  max-width: 360px;
}

.login__title {
  margin: 0;
  font-size: var(--da-font-xl);
  font-weight: 600;
}

.login__subtitle {
  margin: var(--da-space-1) 0 var(--da-space-5);
  font-size: var(--da-font-sm);
  color: var(--da-color-text-secondary);
}

.login__submit {
  width: 100%;
  margin-top: var(--da-space-2);
}

@media (max-width: 860px) {
  .login__intro {
    display: none;
  }

  .login__panel {
    flex: 1 1 100%;
  }
}
</style>
