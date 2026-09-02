<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import type { RouteLocationRaw } from 'vue-router'
import { useRoute, useRouter } from 'vue-router'
import { gsap } from 'gsap'
import {
  ArrowRight,
  Clapperboard,
  Eye,
  EyeOff,
  LoaderCircle,
  LockKeyhole,
  Mail,
  Sparkles,
} from 'lucide-vue-next'

import ThemeToggle from '@/components/ThemeToggle.vue'
import { api, ApiError } from '@/lib/api'
import { useAuthStore } from '@/stores/auth'
import type { PlatformBranding } from '@/types'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const pageRoot = ref<HTMLElement | null>(null)
const loginCard = ref<HTMLElement | null>(null)
const errorPanel = ref<HTMLElement | null>(null)
const email = ref('')
const password = ref('')
const showPassword = ref(false)
const errorMessage = ref('')
const defaultBackgroundVideoUrl = '/videos/login-background.mp4'
const backgroundVideoUrl = ref(defaultBackgroundVideoUrl)
const backgroundFallbackUsed = ref(false)

let mediaContext: ReturnType<typeof gsap.matchMedia> | null = null
let feedbackTimeline: gsap.core.Timeline | null = null
let exitTimeline: gsap.core.Timeline | null = null
let cardRotateX: ReturnType<typeof gsap.quickTo> | null = null
let cardRotateY: ReturnType<typeof gsap.quickTo> | null = null

function destinationAfterLogin(): RouteLocationRaw {
  const requested = typeof route.query.redirect === 'string' ? route.query.redirect : ''
  const isLocalPath = requested.startsWith('/') && !requested.startsWith('//')
  const canOpenRequested = isLocalPath && (!requested.startsWith('/admin') || auth.isAdmin)
  if (canOpenRequested) return requested
  return auth.isAdmin ? '/admin/overview' : '/workspace'
}

function showLoginError(): void {
  void nextTick(() => {
    if (!loginCard.value || !errorPanel.value) return
    feedbackTimeline?.kill()
    feedbackTimeline = gsap
      .timeline({ defaults: { overwrite: 'auto' } })
      .fromTo(
        errorPanel.value,
        { autoAlpha: 0, y: -8, filter: 'blur(4px)' },
        { autoAlpha: 1, y: 0, filter: 'blur(0px)', duration: 0.24, ease: 'power2.out' },
      )
      .to(loginCard.value, { x: -7, duration: 0.07, ease: 'power1.inOut' }, 0)
      .to(loginCard.value, { x: 6, duration: 0.08, ease: 'power1.inOut' })
      .to(loginCard.value, { x: 0, duration: 0.1, ease: 'power2.out' })
  })
}

async function submit(): Promise<void> {
  errorMessage.value = ''
  try {
    await auth.login(email.value, password.value)
    if (loginCard.value) {
      exitTimeline?.kill()
      await new Promise<void>((resolve) => {
        exitTimeline = gsap.timeline({ onComplete: resolve }).to(loginCard.value, {
          autoAlpha: 0,
          y: -12,
          scale: 0.985,
          filter: 'blur(5px)',
          duration: 0.2,
          ease: 'power2.in',
        })
      })
    }
    await router.replace(destinationAfterLogin())
  } catch (error) {
    errorMessage.value = error instanceof ApiError ? error.message : '登录失败，请稍后重试'
    showLoginError()
  }
}

function handleCardPointerMove(event: PointerEvent): void {
  if (!loginCard.value || !cardRotateX || !cardRotateY) return
  const bounds = loginCard.value.getBoundingClientRect()
  const xRatio = (event.clientX - bounds.left) / bounds.width - 0.5
  const yRatio = (event.clientY - bounds.top) / bounds.height - 0.5
  cardRotateX(-yRatio * 2.4)
  cardRotateY(xRatio * 2.8)
}

function resetCardTilt(): void {
  cardRotateX?.(0)
  cardRotateY?.(0)
}

async function loadBranding(): Promise<void> {
  try {
    const branding = await api<PlatformBranding>('/public/branding')
    if (branding.login_background_video_url) backgroundVideoUrl.value = branding.login_background_video_url
  } catch {
    backgroundVideoUrl.value = defaultBackgroundVideoUrl
  }
}

function handleBackgroundVideoError(): void {
  if (backgroundFallbackUsed.value || backgroundVideoUrl.value === defaultBackgroundVideoUrl) return
  backgroundFallbackUsed.value = true
  backgroundVideoUrl.value = defaultBackgroundVideoUrl
}

onMounted(() => {
  void loadBranding()
  if (!pageRoot.value) return
  mediaContext = gsap.matchMedia()
  mediaContext.add(
    {
      desktop: '(min-width: 860px)',
      reduceMotion: '(prefers-reduced-motion: reduce)',
      canHover: '(hover: hover) and (pointer: fine)',
    },
    (context) => {
      const conditions = context.conditions as {
        desktop: boolean
        reduceMotion: boolean
        canHover: boolean
      }
      const cardOffset = conditions.desktop ? 52 : 28
      const duration = conditions.reduceMotion ? 0.01 : 0.72
      const timeline = gsap.timeline({ defaults: { ease: 'power3.out' } })

      timeline
        .fromTo('.login-backdrop', { autoAlpha: 0 }, { autoAlpha: 1, duration: duration * 0.9 })
        .fromTo(
          '.cinematic-login__brand',
          { autoAlpha: 0, y: -14 },
          { autoAlpha: 1, y: 0, duration: duration * 0.72 },
          0.08,
        )
        .fromTo(
          '.login-copy__stagger',
          { autoAlpha: 0, y: 18, filter: conditions.reduceMotion ? 'none' : 'blur(7px)' },
          {
            autoAlpha: 1,
            y: 0,
            filter: 'blur(0px)',
            duration: duration * 0.82,
            stagger: conditions.reduceMotion ? 0 : 0.085,
          },
          0.14,
        )
        .fromTo(
          '.acrylic-login-card',
          {
            autoAlpha: 0,
            x: conditions.desktop ? cardOffset : 0,
            y: conditions.desktop ? 0 : cardOffset,
            scale: 0.97,
            filter: conditions.reduceMotion ? 'none' : 'blur(10px)',
          },
          {
            autoAlpha: 1,
            x: 0,
            y: 0,
            scale: 1,
            filter: 'blur(0px)',
            duration,
            ease: 'power4.out',
          },
          0.18,
        )
        .fromTo(
          '.login-form__stagger',
          { autoAlpha: 0, y: 12 },
          {
            autoAlpha: 1,
            y: 0,
            duration: duration * 0.62,
            stagger: conditions.reduceMotion ? 0 : 0.065,
          },
          0.46,
        )

      if (!conditions.reduceMotion && conditions.canHover && loginCard.value) {
        cardRotateX = gsap.quickTo(loginCard.value, 'rotationX', {
          duration: 0.45,
          ease: 'power3.out',
        })
        cardRotateY = gsap.quickTo(loginCard.value, 'rotationY', {
          duration: 0.45,
          ease: 'power3.out',
        })
      }
    },
    pageRoot.value,
  )
})

onUnmounted(() => {
  feedbackTimeline?.kill()
  exitTimeline?.kill()
  mediaContext?.revert()
})
</script>

<template>
  <main ref="pageRoot" class="cinematic-login">
    <video
      :key="backgroundVideoUrl"
      class="cinematic-login__video"
      autoplay
      muted
      loop
      playsinline
      preload="metadata"
      poster="/covers/login-studio-v2.webp"
      aria-hidden="true"
      @error="handleBackgroundVideoError"
    >
      <source :src="backgroundVideoUrl" />
    </video>
    <div class="login-backdrop" aria-hidden="true"></div>
    <div class="cinematic-login__texture" aria-hidden="true"></div>

    <header class="cinematic-login__brand">
      <span class="brand-film-mark"><Clapperboard :size="20" /></span>
      <span class="brand-wordmark">CineForge</span>
      <span class="brand-divider" aria-hidden="true"></span>
      <span class="brand-edition">AI Studio</span>
    </header>

    <ThemeToggle class="cinematic-login__theme" />

    <section class="login-copy" aria-label="CineForge AI 短剧创作平台">
      <div class="login-copy__eyebrow login-copy__stagger">
        <Sparkles :size="14" />
        AI NARRATIVE STUDIO
      </div>
      <h1 class="login-copy__stagger">
        <span>每一帧，</span>
        <span>从故事开始。</span>
      </h1>
      <p class="login-copy__stagger">让剧本、资产、分镜与成片，在同一个创作现场自然发生。</p>
      <div class="login-copy__meta login-copy__stagger" aria-label="制作流程">
        <span>剧本</span><i></i><span>分镜</span><i></i><span>成片</span>
      </div>
    </section>

    <section class="login-stage" aria-label="账号登录">
      <form
        ref="loginCard"
        class="acrylic-login-card"
        @submit.prevent="submit"
        @pointermove="handleCardPointerMove"
        @pointerleave="resetCardTilt"
      >
        <div class="acrylic-login-card__edge" aria-hidden="true"></div>
        <header class="login-form__header login-form__stagger">
          <h2>欢迎回来</h2>
          <p>登录后继续你的下一幕创作</p>
        </header>

        <label class="glass-field login-form__stagger">
          <span>邮箱</span>
          <span class="glass-field__control">
            <Mail :size="18" aria-hidden="true" />
            <input
              v-model.trim="email"
              type="email"
              autocomplete="username"
              inputmode="email"
              placeholder="name@example.com"
              required
              autofocus
            />
          </span>
        </label>

        <label class="glass-field login-form__stagger">
          <span>密码</span>
          <span class="glass-field__control">
            <LockKeyhole :size="18" aria-hidden="true" />
            <input
              v-model="password"
              :type="showPassword ? 'text' : 'password'"
              autocomplete="current-password"
              placeholder="输入登录密码"
              required
            />
            <button
              type="button"
              class="password-visibility"
              :aria-label="showPassword ? '隐藏密码' : '显示密码'"
              :title="showPassword ? '隐藏密码' : '显示密码'"
              @click="showPassword = !showPassword"
            >
              <EyeOff v-if="showPassword" :size="17" />
              <Eye v-else :size="17" />
            </button>
          </span>
        </label>

        <p v-if="errorMessage" ref="errorPanel" class="glass-form-error" role="alert">
          {{ errorMessage }}
        </p>

        <button
          class="cinematic-login-submit login-form__stagger"
          type="submit"
          :disabled="auth.loading"
        >
          <LoaderCircle v-if="auth.loading" class="spin" :size="19" />
          <template v-else>
            <span>进入创作现场</span>
            <ArrowRight :size="19" />
          </template>
        </button>
      </form>
    </section>

    <footer class="cinematic-login__footer">
      <span>AI SHORT DRAMA PRODUCTION</span>
      <span class="cinematic-login__footer-line"></span>
      <span>2026</span>
    </footer>
  </main>
</template>

<style scoped>
.cinematic-login {
  position: relative;
  display: grid;
  min-height: 100vh;
  min-height: 100dvh;
  grid-template-columns: minmax(0, 1fr) minmax(380px, 520px);
  overflow: hidden;
  color: #f8faf9;
  background: #111613 url('/covers/login-studio-v2.webp') center / cover no-repeat;
  isolation: isolate;
  -webkit-font-smoothing: antialiased;
  perspective: 1400px;
}

.cinematic-login__video {
  position: absolute;
  z-index: -3;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
  object-position: center;
  outline: 1px solid rgb(255 255 255 / 10%);
  outline-offset: -1px;
}

.login-backdrop {
  position: absolute;
  z-index: -2;
  inset: 0;
  background:
    linear-gradient(90deg, rgb(7 11 9 / 76%) 0%, rgb(7 11 9 / 34%) 46%, rgb(7 11 9 / 58%) 100%),
    linear-gradient(0deg, rgb(6 9 8 / 74%) 0%, transparent 52%);
  pointer-events: none;
}

.cinematic-login__texture {
  position: absolute;
  z-index: -1;
  inset: 0;
  opacity: 0.16;
  background-image: repeating-linear-gradient(90deg, rgb(255 255 255 / 3%) 0, rgb(255 255 255 / 3%) 1px, transparent 1px, transparent 4px);
  mix-blend-mode: soft-light;
  pointer-events: none;
}

.cinematic-login__brand {
  position: absolute;
  top: clamp(22px, 4vw, 42px);
  left: clamp(22px, 4.5vw, 72px);
  display: flex;
  align-items: center;
  gap: 11px;
  min-height: 44px;
  text-shadow: 0 2px 18px rgb(0 0 0 / 35%);
}

.brand-film-mark {
  display: grid;
  width: 40px;
  height: 40px;
  place-items: center;
  border-radius: 8px;
  color: #fff;
  background: rgb(218 88 66 / 92%);
  box-shadow: 0 14px 34px rgb(142 43 29 / 30%), inset 0 0 0 1px rgb(255 255 255 / 24%);
}

.brand-wordmark { font-size: 17px; font-weight: 760; letter-spacing: 0; }
.brand-divider { width: 1px; height: 15px; background: rgb(255 255 255 / 28%); }
.brand-edition { color: rgb(255 255 255 / 58%); font-size: 10px; font-weight: 700; text-transform: uppercase; }

.cinematic-login__theme {
  position: absolute;
  z-index: 4;
  top: clamp(24px, 4vw, 44px);
  right: clamp(22px, 3vw, 42px);
  width: 42px;
  height: 42px;
  border: 0;
  color: #fff;
  background: rgb(13 18 16 / 44%);
  box-shadow: 0 0 0 1px rgb(255 255 255 / 14%), 0 12px 34px rgb(0 0 0 / 16%);
  backdrop-filter: blur(18px) saturate(140%);
  transition-property: color, background-color, box-shadow, scale;
  transition-duration: 180ms;
  transition-timing-function: ease-out;
}

.cinematic-login__theme:hover { background: rgb(255 255 255 / 16%); box-shadow: 0 0 0 1px rgb(255 255 255 / 22%), 0 16px 38px rgb(0 0 0 / 20%); }
.cinematic-login__theme:active { scale: 0.96; }

.login-copy {
  align-self: end;
  max-width: 720px;
  margin: 0 0 clamp(88px, 11vh, 136px) clamp(28px, 6vw, 104px);
  padding-right: 40px;
  text-shadow: 0 4px 26px rgb(0 0 0 / 30%);
}

.login-copy__eyebrow {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: 30px;
  padding: 0 10px;
  border-radius: 6px;
  color: #ffd7cf;
  background: rgb(217 86 64 / 18%);
  box-shadow: inset 0 0 0 1px rgb(255 185 172 / 22%);
  backdrop-filter: blur(12px);
  font-size: 10px;
  font-weight: 760;
}

.login-copy h1 {
  display: flex;
  flex-direction: column;
  margin-top: 22px;
  font-size: clamp(48px, 5.8vw, 88px);
  font-weight: 760;
  line-height: 0.98;
  letter-spacing: 0;
  text-wrap: balance;
}

.login-copy h1 span:last-child { color: rgb(255 255 255 / 78%); }

.login-copy > p {
  max-width: 570px;
  margin-top: 24px;
  color: rgb(255 255 255 / 68%);
  font-size: 15px;
  line-height: 1.8;
  text-wrap: pretty;
}

.login-copy__meta { display: flex; align-items: center; gap: 11px; margin-top: 26px; color: rgb(255 255 255 / 52%); font-size: 10px; font-weight: 680; }
.login-copy__meta i { width: 18px; height: 1px; background: rgb(255 255 255 / 26%); }

.login-stage {
  display: flex;
  min-width: 0;
  align-items: center;
  justify-content: center;
  padding: 104px 48px 72px 12px;
}

.acrylic-login-card {
  position: relative;
  width: min(410px, 100%);
  padding: 34px;
  overflow: hidden;
  border: 0;
  border-radius: 8px;
  color: #f6f8f7;
  background: rgb(15 20 18 / 56%);
  box-shadow: 0 0 0 1px rgb(255 255 255 / 14%), 0 28px 80px rgb(0 0 0 / 32%), inset 0 1px 0 rgb(255 255 255 / 12%), inset 0 -1px 0 rgb(255 255 255 / 4%);
  backdrop-filter: blur(34px) saturate(150%);
  transform-style: preserve-3d;
  transform-origin: center;
  will-change: transform, opacity, filter;
}

.acrylic-login-card__edge {
  position: absolute;
  top: 0;
  right: 18%;
  left: 18%;
  height: 1px;
  background: linear-gradient(90deg, transparent, rgb(255 255 255 / 72%), transparent);
  box-shadow: 0 0 20px rgb(255 255 255 / 24%);
  pointer-events: none;
}

.login-form__header { margin-bottom: 28px; }
.login-form__header h2 { margin: 0; color: #fff; font-size: 30px; line-height: 1.2; letter-spacing: 0; text-wrap: balance; }
.login-form__header p { margin-top: 9px; color: rgb(255 255 255 / 54%); font-size: 13px; text-wrap: pretty; }
.glass-field { display: block; margin-top: 17px; }
.glass-field > span:first-child { display: block; margin-bottom: 8px; color: rgb(255 255 255 / 62%); font-size: 11px; font-weight: 650; }

.glass-field__control {
  display: grid;
  min-height: 50px;
  grid-template-columns: 20px minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  padding: 0 13px;
  border-radius: 6px;
  color: rgb(255 255 255 / 45%);
  background: rgb(255 255 255 / 7%);
  box-shadow: inset 0 0 0 1px rgb(255 255 255 / 10%), 0 8px 26px rgb(0 0 0 / 7%);
  transition-property: color, background-color, box-shadow, transform;
  transition-duration: 180ms;
  transition-timing-function: ease-out;
}

.glass-field__control:focus-within {
  color: #f19a89;
  background: rgb(255 255 255 / 10%);
  box-shadow: inset 0 0 0 1px rgb(236 122 99 / 68%), 0 0 0 4px rgb(218 88 66 / 12%), 0 12px 30px rgb(0 0 0 / 9%);
  transform: translateY(-1px);
}

.glass-field__control input {
  width: 100%;
  min-width: 0;
  height: 48px;
  padding: 0;
  border: 0;
  outline: 0;
  color: #fff;
  background: transparent;
  box-shadow: none;
  font: inherit;
  font-size: 13px;
}

.glass-field__control input::placeholder { color: rgb(255 255 255 / 30%); }

.password-visibility {
  display: grid;
  width: 40px;
  height: 40px;
  place-items: center;
  margin-right: -8px;
  border: 0;
  border-radius: 5px;
  color: rgb(255 255 255 / 46%);
  background: transparent;
  cursor: pointer;
  transition-property: color, background-color, scale;
  transition-duration: 150ms;
  transition-timing-function: ease-out;
}

.password-visibility:hover { color: #fff; background: rgb(255 255 255 / 8%); }
.password-visibility:active { scale: 0.96; }

.glass-form-error {
  margin-top: 14px;
  padding: 10px 12px;
  border-radius: 6px;
  color: #ffd5ce;
  background: rgb(171 57 40 / 22%);
  box-shadow: inset 0 0 0 1px rgb(255 135 115 / 20%);
  font-size: 11px;
  line-height: 1.55;
  text-wrap: pretty;
}

.cinematic-login-submit {
  display: flex;
  width: 100%;
  min-height: 50px;
  align-items: center;
  justify-content: center;
  gap: 9px;
  margin-top: 24px;
  padding: 0 14px 0 16px;
  border: 0;
  border-radius: 6px;
  color: #fff;
  background: #d95c45;
  box-shadow: 0 0 0 1px rgb(255 255 255 / 13%), 0 14px 36px rgb(124 39 25 / 30%), inset 0 1px 0 rgb(255 255 255 / 22%);
  cursor: pointer;
  font: inherit;
  font-size: 13px;
  font-weight: 720;
  transition-property: background-color, box-shadow, transform, opacity;
  transition-duration: 170ms;
  transition-timing-function: ease-out;
}

.cinematic-login-submit:hover:not(:disabled) { background: #e3664f; box-shadow: 0 0 0 1px rgb(255 255 255 / 18%), 0 18px 42px rgb(124 39 25 / 38%), inset 0 1px 0 rgb(255 255 255 / 26%); transform: translateY(-2px); }
.cinematic-login-submit:active:not(:disabled) { transform: translateY(0) scale(0.96); }
.cinematic-login-submit:disabled { cursor: wait; opacity: 0.7; }

.cinematic-login__footer {
  position: absolute;
  right: clamp(22px, 3vw, 42px);
  bottom: 28px;
  display: flex;
  align-items: center;
  gap: 10px;
  color: rgb(255 255 255 / 34%);
  font-size: 9px;
  font-weight: 650;
}

.cinematic-login__footer-line { width: 28px; height: 1px; background: rgb(255 255 255 / 20%); }

:global(:root[data-theme='light'] .acrylic-login-card) {
  color: #18201d;
  background: rgb(249 250 249 / 72%);
  box-shadow: 0 0 0 1px rgb(255 255 255 / 72%), 0 30px 84px rgb(16 24 20 / 24%), inset 0 1px 0 rgb(255 255 255 / 88%);
}

:global(:root[data-theme='light'] .login-form__header h2),
:global(:root[data-theme='light'] .glass-field__control input) { color: #17201d; }
:global(:root[data-theme='light'] .login-form__header p),
:global(:root[data-theme='light'] .glass-field > span:first-child) { color: rgb(23 32 29 / 60%); }

:global(:root[data-theme='light'] .glass-field__control) {
  color: rgb(23 32 29 / 42%);
  background: rgb(255 255 255 / 54%);
  box-shadow: inset 0 0 0 1px rgb(23 32 29 / 10%), 0 8px 24px rgb(23 32 29 / 5%);
}

:global(:root[data-theme='light'] .glass-field__control:focus-within) {
  color: #bd503b;
  background: rgb(255 255 255 / 78%);
  box-shadow: inset 0 0 0 1px rgb(202 83 61 / 60%), 0 0 0 4px rgb(202 83 61 / 10%), 0 12px 30px rgb(23 32 29 / 7%);
}

:global(:root[data-theme='light'] .glass-field__control input::placeholder) { color: rgb(23 32 29 / 30%); }
:global(:root[data-theme='light'] .password-visibility) { color: rgb(23 32 29 / 44%); }
:global(:root[data-theme='light'] .password-visibility:hover) { color: #17201d; background: rgb(23 32 29 / 6%); }
:global(:root[data-theme='light'] .glass-form-error) { color: #9b3827; background: rgb(198 69 47 / 10%); box-shadow: inset 0 0 0 1px rgb(177 58 39 / 16%); }

@media (max-width: 1040px) {
  .cinematic-login { grid-template-columns: minmax(0, 1fr) minmax(360px, 450px); }
  .login-copy { margin-left: 44px; }
  .login-copy h1 { font-size: clamp(46px, 6vw, 68px); }
  .login-stage { padding-right: 30px; }
}

@media (max-width: 859px) {
  .cinematic-login {
    display: flex;
    min-height: 100dvh;
    align-items: center;
    justify-content: center;
    padding: 94px 20px 52px;
    background-position: 58% center;
  }

  .cinematic-login__video { object-position: 58% center; }
  .login-backdrop { background: rgb(6 10 8 / 62%); }
  .cinematic-login__brand { top: 18px; left: 20px; }
  .brand-divider, .brand-edition { display: none; }
  .cinematic-login__theme { top: 19px; right: 20px; }
  .login-copy, .cinematic-login__footer { display: none; }
  .login-stage { display: block; width: min(430px, 100%); padding: 0; }
  .acrylic-login-card { width: 100%; padding: 30px 26px; background: rgb(15 20 18 / 62%); backdrop-filter: blur(28px) saturate(145%); }
}

@media (max-width: 420px) {
  .cinematic-login { padding: 86px 14px 28px; }
  .brand-film-mark { width: 38px; height: 38px; }
  .acrylic-login-card { padding: 26px 20px; }
  .login-form__header { margin-bottom: 24px; }
  .login-form__header h2 { font-size: 27px; }
}

@media (prefers-reduced-motion: reduce) {
  .acrylic-login-card { will-change: auto; }
  .cinematic-login-submit, .glass-field__control, .password-visibility, .cinematic-login__theme { transition-duration: 0.01ms; }
}
</style>
