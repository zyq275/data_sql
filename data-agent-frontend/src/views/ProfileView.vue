<script setup lang="ts">
/**
 * 个人设置（FR-AUTH-006）：展示当前用户信息（复用 FR-AUTH-003）、改密（复用 FR-AUTH-004）、登出。
 *
 * 改密成功后**不强制重新登录**：后端改密使旧密码失效，但当前 access token 仍然有效，
 * 把人当场踢出去只会让人怀疑"是不是没改成功"。这里给出明确反馈，让用户自己决定
 * 何时登出。要不要让旧令牌立刻失效属于后端的会话策略，不由前端代劳。
 */
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { computed, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'
import { ROLE_LABELS } from '@/utils/role'

const auth = useAuthStore()
const router = useRouter()

const formRef = ref<FormInstance>()
const submitting = ref(false)
const form = reactive({ oldPassword: '', newPassword: '', confirmPassword: '' })

const roleLabel = computed(() => (auth.role ? ROLE_LABELS[auth.role] : '—'))

/** 角色能做什么，写清楚而不只给一个词：新用户看到"分析师"不知道比自己多了什么。 */
const ROLE_DESC: Record<string, string> = {
  viewer: '可以提问取数、进行多轮会话、查看本人历史',
  analyst: '查看者权限，另可使用数据看板',
  admin: '分析师权限，另可管理用户、数据权限与评测集',
}

const rules: FormRules = {
  oldPassword: [{ required: true, message: '请输入当前密码', trigger: 'blur' }],
  newPassword: [
    { required: true, message: '请输入新密码', trigger: 'blur' },
    { min: 6, max: 32, message: '密码长度 6-32 位', trigger: 'blur' },
  ],
  confirmPassword: [
    { required: true, message: '请再次输入新密码', trigger: 'blur' },
    {
      validator: (_rule, value: string, callback) => {
        if (value !== form.newPassword) {
          callback(new Error('两次输入的新密码不一致'))
          return
        }
        callback()
      },
      trigger: 'blur',
    },
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
  submitting.value = true
  try {
    await auth.changePassword(form.oldPassword, form.newPassword)
    ElMessage.success('密码已修改，请用新密码登录后续会话')
    form.oldPassword = ''
    form.newPassword = ''
    form.confirmPassword = ''
    formEl.clearValidate()
  } finally {
    submitting.value = false
  }
}

async function logout() {
  auth.logout()
  await router.replace({ name: 'login' })
}
</script>

<template>
  <div class="da-page profile">
    <div class="da-page__header">
      <div>
        <h2 class="da-page__title">个人设置</h2>
        <p class="da-page__subtitle">账号信息与密码管理</p>
      </div>
    </div>

    <el-row :gutter="16">
      <el-col :xs="24" :md="12">
        <div class="da-card profile__card">
          <h3 class="profile__card-title">账号信息</h3>
          <el-descriptions v-if="auth.user" :column="1" border size="small">
            <el-descriptions-item label="用户名">
              <span class="da-mono">{{ auth.user.username }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="显示名">
              {{ auth.user.display_name || '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="角色">
              <el-tag size="small" effect="plain">{{ roleLabel }}</el-tag>
              <span class="profile__role-desc">{{ ROLE_DESC[auth.user.role] }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="账号状态">
              <el-tag
                size="small"
                :type="auth.user.is_active === false ? 'danger' : 'success'"
                effect="plain"
              >
                {{ auth.user.is_active === false ? '已禁用' : '正常' }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="用户 ID">
              <span class="da-mono da-text-sm">{{ auth.user.id }}</span>
            </el-descriptions-item>
          </el-descriptions>
          <EmptyState v-else description="未获取到账号信息" hint="请重新登录" />

          <div class="profile__logout">
            <el-button @click="logout">退出登录</el-button>
          </div>
        </div>
      </el-col>

      <el-col :xs="24" :md="12">
        <div class="da-card profile__card">
          <h3 class="profile__card-title">修改密码</h3>
          <el-alert
            class="profile__tip"
            type="info"
            :closable="false"
            show-icon
            title="修改成功后旧密码立即失效；当前登录状态不受影响。"
          />
          <el-form
            ref="formRef"
            :model="form"
            :rules="rules"
            label-position="top"
            @submit.prevent="submit"
          >
            <el-form-item label="当前密码" prop="oldPassword">
              <el-input v-model="form.oldPassword" type="password" show-password />
            </el-form-item>
            <el-form-item label="新密码" prop="newPassword">
              <el-input v-model="form.newPassword" type="password" show-password />
            </el-form-item>
            <el-form-item label="确认新密码" prop="confirmPassword">
              <el-input v-model="form.confirmPassword" type="password" show-password />
            </el-form-item>
            <el-button type="primary" :loading="submitting" @click="submit">保存新密码</el-button>
          </el-form>
        </div>
      </el-col>
    </el-row>

    <p class="da-text-sm da-text-secondary">
      会话由服务端签发，登出后本地令牌即被清除；如需在其它设备上强制下线，请联系管理员。
    </p>
  </div>
</template>

<style scoped>
.profile__card {
  padding: var(--da-space-5);
  height: 100%;
}

.profile__card-title {
  margin: 0 0 var(--da-space-4);
  font-size: var(--da-font-md);
  font-weight: 600;
}

.profile__role-desc {
  margin-left: var(--da-space-2);
  font-size: var(--da-font-sm);
  color: var(--da-color-text-secondary);
}

.profile__logout {
  margin-top: var(--da-space-4);
}

.profile__tip {
  margin-bottom: var(--da-space-4);
}
</style>
