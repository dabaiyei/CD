<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowRight,
  Camera,
  Coins,
  Eye,
  EyeOff,
  Gift,
  ImagePlus,
  LoaderCircle,
  LockKeyhole,
  Mail,
  Sparkles,
  UserRound,
  UsersRound,
  X,
} from 'lucide-vue-next'

import ThemeToggle from '@/components/ThemeToggle.vue'
import { api, setToken } from '@/lib/api'
import { useAuthStore } from '@/stores/auth'
import type { InvitationRegistrationInfo } from '@/types'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const code = computed(() => String(route.params.code || ''))
const invitation = ref<InvitationRegistrationInfo | null>(null)
const loading = ref(true)
const submitting = ref(false)
const shown = ref(false)
const succeeded = ref(false)
const loadError = ref('')
const formError = ref('')
const email = ref('')
const displayName = ref('')
const password = ref('')
const showPassword = ref(false)
const avatar = ref<File | null>(null)
const avatarPreview = ref('')
const avatarInput = ref<HTMLInputElement | null>(null)
let redirectTimer: ReturnType<typeof setTimeout> | null = null

function formatCredits(value: string): string {
  return Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

function chooseAvatar(event: Event): void {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  formError.value = ''
  if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) {
    formError.value = '头像仅支持 JPG、PNG 或 WebP 图片'
    return
  }
  if (file.size > 8 * 1024 * 1024) {
    formError.value = '头像图片不能超过 8 MB'
    return
  }
  if (avatarPreview.value) URL.revokeObjectURL(avatarPreview.value)
  avatar.value = file
  avatarPreview.value = URL.createObjectURL(file)
}

function removeAvatar(): void {
  if (avatarPreview.value) URL.revokeObjectURL(avatarPreview.value)
  avatar.value = null
  avatarPreview.value = ''
  if (avatarInput.value) avatarInput.value.value = ''
}

async function loadInvitation(): Promise<void> {
  loading.value = true
  try {
    invitation.value = await api<InvitationRegistrationInfo>(`/invitations/${encodeURIComponent(code.value)}`)
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '邀请链接当前不可用'
  } finally {
    loading.value = false
    await nextTick()
    requestAnimationFrame(() => { shown.value = true })
  }
}

async function submit(): Promise<void> {
  formError.value = ''
  submitting.value = true
  try {
    const body = new FormData()
    body.set('email', email.value)
    body.set('display_name', displayName.value)
    body.set('password', password.value)
    if (avatar.value) body.set('avatar', avatar.value)
    const token = await api<{ access_token: string }>(`/invitations/${encodeURIComponent(code.value)}/register`, { method: 'POST', body })
    setToken(token.access_token)
    await auth.refreshSession()
    succeeded.value = true
    redirectTimer = setTimeout(() => void router.replace('/workspace'), 1100)
  } catch (error) {
    formError.value = error instanceof Error ? error.message : '注册失败，请稍后重试'
  } finally {
    submitting.value = false
  }
}

onMounted(() => void loadInvitation())
onBeforeUnmount(() => {
  if (redirectTimer) clearTimeout(redirectTimer)
  if (avatarPreview.value) URL.revokeObjectURL(avatarPreview.value)
})
</script>

<template>
  <main class="invite-register-page">
    <video class="invite-register-page__video" autoplay muted loop playsinline preload="metadata" poster="/covers/login-studio-v2.webp" aria-hidden="true">
      <source src="/videos/login-background.mp4" type="video/mp4" />
    </video>
    <div class="invite-register-page__veil" aria-hidden="true"></div>
    <div class="invite-register-page__grain" aria-hidden="true"></div>
    <ThemeToggle class="invite-register-page__theme" />

    <header class="invite-brand"><span><Gift :size="19" /></span><strong>CineForge</strong><i></i><small>PRIVATE INVITATION</small></header>

    <section class="invite-register-copy t-stagger" :class="{ 'is-shown': shown }">
      <span class="invite-register-copy__eyebrow t-stagger-line t-stagger-line--1"><Sparkles :size="14" />CREATOR ACCESS</span>
      <h1 class="t-stagger-line t-stagger-line--2">你的下一部作品，<br />从这里入场。</h1>
      <p class="t-stagger-line t-stagger-line--3">加入一体化 AI 短剧创作现场，让剧本、视觉资产与镜头语言在同一个空间协同生长。</p>
      <div v-if="invitation" class="invite-register-copy__perks t-stagger-line t-stagger-line--4">
        <span><Coins :size="16" /><strong>{{ formatCredits(invitation.initial_credits) }}</strong> 初始积分</span>
        <span><UsersRound :size="16" /><strong>{{ invitation.remaining_registrations }}</strong> 个剩余席位</span>
      </div>
    </section>

    <section class="invite-register-stage">
      <div v-if="loading" class="invite-acrylic invite-state"><LoaderCircle class="spin" :size="28" /><strong>正在确认邀请</strong><p>请稍候，正在为你打开创作入口。</p></div>
      <div v-else-if="loadError" class="invite-acrylic invite-state invite-state--error"><span><X :size="24" /></span><strong>这条邀请暂时无法使用</strong><p>{{ loadError }}</p><RouterLink to="/login">返回登录</RouterLink></div>
      <div v-else-if="succeeded" class="invite-acrylic invite-state invite-state--success">
        <span class="t-success-check" data-state="in" aria-hidden="true"><svg width="52" height="52" viewBox="0 0 48 48" fill="none"><path d="M13 25L21 33L36 16" stroke="currentColor" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" /></svg></span>
        <strong>欢迎加入创作现场</strong><p>账号已经建立，正在进入你的创作台。</p>
      </div>
      <form v-else class="invite-acrylic invite-form-card" @submit.prevent="submit">
        <header><span>INVITATION ACCEPTED</span><h2>创建你的账号</h2><p>{{ invitation?.tenant_name }} · {{ invitation?.invitation_name }}</p></header>

        <div class="invite-avatar">
          <button type="button" title="选择头像" @click="avatarInput?.click()">
            <img v-if="avatarPreview" :src="avatarPreview" alt="头像预览" />
            <ImagePlus v-else :size="24" />
            <span><Camera :size="13" /></span>
          </button>
          <div><strong>个人头像</strong><small>可选，支持 JPG、PNG、WebP</small></div>
          <button v-if="avatar" class="invite-avatar__remove" type="button" title="移除头像" @click="removeAvatar"><X :size="15" /></button>
          <input ref="avatarInput" class="sr-only" type="file" accept="image/jpeg,image/png,image/webp" @change="chooseAvatar" />
        </div>

        <label class="invite-glass-field"><span>邮箱</span><div><Mail :size="18" /><input v-model.trim="email" required type="email" autocomplete="email" inputmode="email" maxlength="255" placeholder="name@example.com" /></div></label>
        <label class="invite-glass-field"><span>名称</span><div><UserRound :size="18" /><input v-model.trim="displayName" required autocomplete="name" maxlength="80" placeholder="你希望被怎样称呼" /></div></label>
        <label class="invite-glass-field"><span>密码</span><div><LockKeyhole :size="18" /><input v-model="password" required :type="showPassword ? 'text' : 'password'" minlength="8" maxlength="128" autocomplete="new-password" placeholder="至少 8 位" /><button type="button" :title="showPassword ? '隐藏密码' : '显示密码'" @click="showPassword = !showPassword"><EyeOff v-if="showPassword" :size="17" /><Eye v-else :size="17" /></button></div></label>

        <p v-if="formError" class="invite-form-error" role="alert">{{ formError }}</p>
        <button class="invite-register-submit" type="submit" :disabled="submitting"><LoaderCircle v-if="submitting" class="spin" :size="19" /><template v-else><span>接受邀请并进入</span><ArrowRight :size="19" /></template></button>
        <small class="invite-login-link">已有账号？<RouterLink to="/login">直接登录</RouterLink></small>
      </form>
    </section>

    <footer class="invite-register-footer"><span>AI SHORT DRAMA PRODUCTION</span><i></i><span>2026</span></footer>
  </main>
</template>

<style scoped>
.invite-register-page { position: relative; display: grid; min-height: 100vh; min-height: 100dvh; grid-template-columns: minmax(0, 1fr) minmax(390px, 520px); overflow: hidden; padding: 76px clamp(28px, 5vw, 84px) 58px; color: #f7faf8; background: #101713 url('/covers/login-studio-v2.webp') center / cover no-repeat; isolation: isolate; -webkit-font-smoothing: antialiased; }
.invite-register-page__video, .invite-register-page__veil, .invite-register-page__grain { position: absolute; inset: 0; width: 100%; height: 100%; }
.invite-register-page__video { z-index: -4; object-fit: cover; }
.invite-register-page__veil { z-index: -3; background: linear-gradient(90deg, rgb(7 13 10 / 74%) 0%, rgb(8 14 11 / 46%) 48%, rgb(7 11 9 / 68%) 100%); }
.invite-register-page__grain { z-index: -2; opacity: .13; pointer-events: none; background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 180 180' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='.42'/%3E%3C/svg%3E"); mix-blend-mode: soft-light; }
.invite-register-page__theme { position: absolute; top: 24px; right: 28px; z-index: 3; }
.invite-brand { position: absolute; top: 24px; left: clamp(28px, 5vw, 84px); display: flex; align-items: center; gap: 10px; letter-spacing: 0; }
.invite-brand>span { display: inline-flex; width: 34px; height: 34px; align-items: center; justify-content: center; border-radius: 7px; color: #c5f3d8; background: rgb(225 255 237 / 12%); box-shadow: inset 0 0 0 1px rgb(255 255 255 / 14%); backdrop-filter: blur(12px); }
.invite-brand strong { font-size: 16px; }
.invite-brand i { width: 1px; height: 15px; background: rgb(255 255 255 / 28%); }
.invite-brand small { color: rgb(255 255 255 / 58%); font-size: 9px; }
.invite-register-copy { align-self: center; max-width: 690px; padding-right: clamp(28px, 6vw, 110px); }
.invite-register-copy__eyebrow { display: inline-flex; align-items: center; gap: 8px; color: #b8eccd; font-size: 11px; font-weight: 750; }
.invite-register-copy h1 { margin: 19px 0 18px; font-size: clamp(42px, 5vw, 72px); font-weight: 620; line-height: 1.08; letter-spacing: 0; text-wrap: balance; text-shadow: 0 12px 42px rgb(0 0 0 / 28%); }
.invite-register-copy>p { max-width: 570px; margin: 0; color: rgb(243 250 246 / 70%); font-size: 14px; line-height: 1.85; text-wrap: pretty; }
.invite-register-copy__perks { display: flex; gap: 10px; margin-top: 26px; }
.invite-register-copy__perks span { display: inline-flex; min-height: 40px; align-items: center; gap: 7px; padding: 0 13px; border-radius: 7px; color: rgb(249 255 251 / 76%); background: rgb(233 255 242 / 9%); box-shadow: inset 0 0 0 1px rgb(255 255 255 / 11%); backdrop-filter: blur(14px); font-size: 11px; }
.invite-register-copy__perks strong { color: #fff; font-size: 14px; font-variant-numeric: tabular-nums; }
.invite-register-stage { display: flex; align-items: center; justify-content: flex-end; }
.invite-acrylic { width: min(100%, 470px); border-radius: 8px; background: linear-gradient(145deg, rgb(245 252 248 / 17%), rgb(209 229 218 / 8%)); box-shadow: inset 0 0 0 1px rgb(255 255 255 / 18%), 0 34px 90px rgb(0 0 0 / 36%); backdrop-filter: blur(28px) saturate(118%); }
.invite-form-card { display: grid; gap: 15px; padding: 28px; }
.invite-form-card>header span { color: #b6e9ca; font-size: 9px; font-weight: 750; }
.invite-form-card>header h2 { margin: 6px 0 4px; font-size: 25px; font-weight: 620; letter-spacing: 0; }
.invite-form-card>header p { margin: 0; overflow: hidden; color: rgb(245 252 248 / 60%); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
.invite-avatar { display: grid; grid-template-columns: 58px minmax(0, 1fr) 40px; align-items: center; gap: 11px; padding: 10px; border-radius: 7px; background: rgb(255 255 255 / 6%); box-shadow: inset 0 0 0 1px rgb(255 255 255 / 9%); }
.invite-avatar>button:first-child { position: relative; display: inline-flex; width: 58px; height: 58px; align-items: center; justify-content: center; overflow: hidden; border: 0; border-radius: 50%; color: rgb(255 255 255 / 66%); background: rgb(6 16 11 / 25%); box-shadow: inset 0 0 0 1px rgb(255 255 255 / 15%); }
.invite-avatar img { width: 100%; height: 100%; object-fit: cover; outline: 1px solid rgb(255 255 255 / 10%); }
.invite-avatar>button:first-child span { position: absolute; right: 0; bottom: 0; display: inline-flex; width: 22px; height: 22px; align-items: center; justify-content: center; border-radius: 50%; color: #102019; background: #c7f2d8; }
.invite-avatar strong, .invite-avatar small { display: block; }
.invite-avatar small { margin-top: 4px; color: rgb(245 252 248 / 52%); font-size: 9px; }
.invite-avatar__remove { display: inline-flex; width: 40px; height: 40px; align-items: center; justify-content: center; border: 0; border-radius: 6px; color: rgb(255 255 255 / 62%); background: transparent; }
.invite-avatar__remove:hover { color: #fff; background: rgb(255 255 255 / 8%); }
.invite-glass-field { display: grid; gap: 6px; }
.invite-glass-field>span { color: rgb(248 252 249 / 72%); font-size: 10px; font-weight: 650; }
.invite-glass-field>div { display: flex; min-height: 46px; align-items: center; gap: 10px; padding: 0 12px; border-radius: 7px; color: rgb(245 252 248 / 54%); background: rgb(4 13 8 / 18%); box-shadow: inset 0 0 0 1px rgb(255 255 255 / 11%); transition-property: box-shadow, background-color; transition-duration: var(--duration-fast); }
.invite-glass-field>div:focus-within { color: #c6f2d7; background: rgb(4 13 8 / 28%); box-shadow: inset 0 0 0 1px rgb(190 241 210 / 64%), 0 0 0 3px rgb(157 229 185 / 10%); }
.invite-glass-field input { min-width: 0; flex: 1; border: 0; outline: 0; color: #fff; background: transparent; font-size: 12px; }
.invite-glass-field input::placeholder { color: rgb(245 252 248 / 34%); }
.invite-glass-field button { display: inline-flex; width: 40px; height: 40px; align-items: center; justify-content: center; border: 0; color: inherit; background: transparent; }
.invite-form-error { margin: 0; padding: 10px 12px; border-radius: 6px; color: #ffd2d2; background: rgb(137 35 35 / 22%); box-shadow: inset 0 0 0 1px rgb(255 142 142 / 18%); font-size: 10px; line-height: 1.5; }
.invite-register-submit { display: inline-flex; min-height: 48px; align-items: center; justify-content: center; gap: 10px; border: 0; border-radius: 7px; color: #102219; background: #c9f3d9; box-shadow: 0 14px 32px rgb(5 26 15 / 28%); font-size: 12px; font-weight: 760; transition-property: transform, box-shadow, background-color; transition-duration: var(--duration-fast); }
.invite-register-submit:hover:not(:disabled) { background: #e1f9ea; box-shadow: 0 17px 38px rgb(5 26 15 / 35%); transform: translateY(-1px); }
.invite-register-submit:active:not(:disabled) { transform: scale(.96); }
.invite-register-submit:disabled { cursor: wait; opacity: .68; }
.invite-login-link { color: rgb(245 252 248 / 48%); text-align: center; }
.invite-login-link a { color: #c8f2d8; }
.invite-state { display: flex; min-height: 340px; flex-direction: column; align-items: center; justify-content: center; gap: 9px; padding: 36px; text-align: center; }
.invite-state>span:not(.t-success-check) { display: inline-flex; width: 52px; height: 52px; align-items: center; justify-content: center; border-radius: 50%; color: #ffb9b9; background: rgb(131 36 36 / 28%); }
.invite-state strong { font-size: 21px; }
.invite-state p { max-width: 330px; margin: 0; color: rgb(245 252 248 / 58%); font-size: 11px; line-height: 1.7; }
.invite-state a { margin-top: 10px; color: #c8f2d8; }
.invite-state--success .t-success-check { color: #c9f3d9; }
.invite-register-footer { position: absolute; right: clamp(28px, 5vw, 84px); bottom: 24px; left: clamp(28px, 5vw, 84px); display: flex; align-items: center; gap: 10px; color: rgb(255 255 255 / 37%); font-size: 8px; }
.invite-register-footer i { width: 45px; height: 1px; background: rgb(255 255 255 / 20%); }
@media (max-width: 880px) { .invite-register-page { display: block; overflow-y: auto; padding: 96px 18px 70px; } .invite-register-page__veil { background: rgb(6 12 9 / 62%); } .invite-brand { left: 18px; } .invite-register-copy { max-width: 560px; margin: 0 auto 28px; padding: 0; text-align: center; } .invite-register-copy h1 { margin: 14px 0 12px; font-size: clamp(34px, 9vw, 48px); } .invite-register-copy>p { margin: 0 auto; } .invite-register-copy__perks { justify-content: center; margin-top: 18px; } .invite-register-stage { justify-content: center; } .invite-register-footer { right: 18px; left: 18px; } }
@media (max-width: 520px) { .invite-register-page__theme { top: 20px; right: 16px; } .invite-brand small, .invite-brand i { display: none; } .invite-register-copy>p { display: none; } .invite-register-copy__perks { flex-wrap: wrap; } .invite-form-card { gap: 13px; padding: 20px 16px; } .invite-form-card>header h2 { font-size: 22px; } .invite-avatar { grid-template-columns: 52px minmax(0, 1fr) 40px; } .invite-avatar>button:first-child { width: 52px; height: 52px; } }
@media (prefers-reduced-motion: reduce) { .invite-register-submit, .invite-glass-field>div { transition: none !important; } .invite-register-page__video { display: none; } }
</style>
