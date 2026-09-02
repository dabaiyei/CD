<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ArrowRight, Clapperboard, Eye, EyeOff, LoaderCircle } from 'lucide-vue-next'

import { ApiError } from '@/lib/api'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const tenant = ref('demo')
const email = ref('creator@cineforge.local')
const password = ref('Creator123!')
const showPassword = ref(false)
const errorMessage = ref('')

function useAccount(kind: 'creator' | 'admin'): void {
  if (kind === 'admin') {
    email.value = 'admin@cineforge.local'
    password.value = 'Admin123!'
  } else {
    email.value = 'creator@cineforge.local'
    password.value = 'Creator123!'
  }
  errorMessage.value = ''
}

async function submit(): Promise<void> {
  errorMessage.value = ''
  try {
    await auth.login(tenant.value, email.value, password.value)
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/workspace'
    await router.replace(redirect)
  } catch (error) {
    errorMessage.value = error instanceof ApiError ? error.message : '登录失败，请稍后重试'
  }
}
</script>

<template>
  <main class="login-page">
    <section class="login-visual" aria-label="影视创作现场">
      <div class="login-visual__brand">
        <span class="brand__mark"><Clapperboard :size="21" /></span>
        <span>CineForge</span>
      </div>
      <div class="login-visual__caption">
        <span class="eyebrow eyebrow--light">AI SHORT DRAMA STUDIO</span>
        <h1>让每一帧<br />都服务于故事</h1>
        <p>从原文到成片的智能创作工作台</p>
      </div>
    </section>

    <section class="login-panel">
      <form class="login-form" @submit.prevent="submit">
        <header>
          <span class="login-mobile-brand"><Clapperboard :size="20" /> CineForge</span>
          <h2>进入创作台</h2>
          <p>继续你的短剧项目</p>
        </header>

        <div class="account-segment" aria-label="选择演示账号">
          <button type="button" :class="{ active: email.startsWith('creator') }" @click="useAccount('creator')">
            创作账号
          </button>
          <button type="button" :class="{ active: email.startsWith('admin') }" @click="useAccount('admin')">
            管理账号
          </button>
        </div>

        <label class="field">
          <span>租户</span>
          <input v-model.trim="tenant" autocomplete="organization" required />
        </label>
        <label class="field">
          <span>邮箱</span>
          <input v-model.trim="email" type="email" autocomplete="username" required />
        </label>
        <label class="field">
          <span>密码</span>
          <span class="password-field">
            <input
              v-model="password"
              :type="showPassword ? 'text' : 'password'"
              autocomplete="current-password"
              required
            />
            <button type="button" class="icon-button icon-button--small" title="显示或隐藏密码" @click="showPassword = !showPassword">
              <EyeOff v-if="showPassword" :size="17" />
              <Eye v-else :size="17" />
            </button>
          </span>
        </label>

        <p v-if="errorMessage" class="form-error" role="alert">{{ errorMessage }}</p>

        <button class="button button--primary login-submit" type="submit" :disabled="auth.loading">
          <LoaderCircle v-if="auth.loading" class="spin" :size="18" />
          <template v-else>
            <span>登录</span>
            <ArrowRight :size="18" />
          </template>
        </button>
      </form>
    </section>
  </main>
</template>

