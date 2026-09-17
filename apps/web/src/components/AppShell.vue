<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ChevronDown,
  Clapperboard,
  Coins,
  BrainCircuit,
  Camera,
  LayoutGrid,
  Boxes,
  MessageSquareText,
  Download,
  LogOut,
  ShieldCheck,
  Sparkles,
} from 'lucide-vue-next'
import {
  PopoverContent,
  PopoverPortal,
  PopoverRoot,
  PopoverTrigger,
} from 'reka-ui'

import { useAuthStore } from '@/stores/auth'
import { useActivityStore } from '@/stores/activity'
import { useTheme } from '@/lib/theme'
import ActivityCenter from '@/components/ActivityCenter.vue'
import AvatarCropDialog from '@/components/AvatarCropDialog.vue'
import ThemeToggle from '@/components/ThemeToggle.vue'
import PwaInstallPrompt from '@/components/PwaInstallPrompt.vue'
import LiquidGlass from '@/components/LiquidGlass.vue'
import type { User } from '@/types'
import { usePwaInstall } from '@/lib/pwa'
import { api } from '@/lib/api'

const auth = useAuthStore()
const activity = useActivityStore()
const route = useRoute()
const router = useRouter()
const { theme } = useTheme()
const pwa = usePwaInstall()
const accountMenuOpen = ref(false)
const avatarEditorOpen = ref(false)
const backgroundBlur = ref(0)
const savingAppearance = ref(false)
const appearanceStatus = ref('')
watch(() => auth.session?.user.background_blur, value => {
  backgroundBlur.value = value ?? 0
}, { immediate: true })

async function saveAppearance(): Promise<void> {
  if (savingAppearance.value || !auth.session) return
  const userId = auth.session.user.id
  savingAppearance.value = true
  appearanceStatus.value = '保存中…'
  try {
    const updated = await api<User>('/auth/me/appearance', {
      method: 'PATCH',
      body: JSON.stringify({ background_blur: backgroundBlur.value }),
    })
    if (auth.session?.user.id === userId) {
      auth.session.user.background_blur = updated.background_blur
      appearanceStatus.value = '已保存'
    }
  } catch {
    backgroundBlur.value = auth.session?.user.background_blur ?? 0
    appearanceStatus.value = '保存失败，请重试'
  } finally {
    savingAppearance.value = false
  }
}

const currentSectionImage = computed(() => {
  const light = theme.value === 'light'
  if (route.name === 'director') return light ? '/covers/rjbg.webp' : '/covers/default-project-city-v2-wide.webp'
  if (route.path.startsWith('/marketplace/skill')) return light ? '/covers/skills-lab-light.webp' : '/covers/skills-lab-v2.webp'
  if (route.path.startsWith('/marketplace/template')) return light ? '/covers/rjbg.webp' : '/covers/default-project-campus-v2-wide.webp'
  if (route.path.startsWith('/marketplace/material')) return light ? '/covers/asset-studio-light.webp' : '/covers/asset-studio-v2.webp'
  if (route.path.startsWith('/skills')) return light ? '/covers/skills-lab-light.webp' : '/covers/skills-lab-v2.webp'
  if (route.path.startsWith('/admin')) return light ? '/covers/admin-console-light.webp' : '/covers/admin-console-v2.webp'
  return light ? '/covers/rjbg.webp' : '/covers/studio-hero-v2.webp'
})

const navItems = computed(() => [
  { label: 'Agent', icon: MessageSquareText, to: '/workspace', active: route.path.startsWith('/workspace') },
  { label: '项目', icon: LayoutGrid, to: '/projects', active: route.path === '/projects' || route.name === 'director' },
  { label: '资产库', icon: Boxes, to: '/assets', active: route.name === 'asset-library' },
  { label: 'Skills', icon: BrainCircuit, to: '/skills', active: route.path.startsWith('/skills') },
  { label: '广场', icon: Sparkles, to: '/marketplace/skill', active: route.path.startsWith('/marketplace') },
  ...(auth.isAdmin
    ? [{ label: '管理', icon: ShieldCheck, to: '/admin/overview', active: route.path.startsWith('/admin') }]
    : []),
])

onMounted(() => {
  activity.start()
})

onUnmounted(() => {
  activity.stop()
})

async function logout(): Promise<void> {
  accountMenuOpen.value = false
  await auth.logout()
  await router.push('/login')
}

function openAvatarEditor(): void {
  accountMenuOpen.value = false
  avatarEditorOpen.value = true
}

function handleAvatarUpdated(user: User): void {
  if (auth.session) auth.session.user = user
}

async function installApp(): Promise<void> {
  accountMenuOpen.value = false
  await pwa.requestInstall()
}
</script>

<template>
  <div
    class="app-shell app-shell--top-navigation app-shell--glass-system"
    :style="{ '--user-background-blur': `${backgroundBlur}px` }"
    :class="{ 'app-shell--agent-home': route.name === 'workspace', 'app-shell--glass-workspace': route.name === 'workspace' || route.name === 'projects' }"
  >
    <section class="shell-main">
      <LiquidGlass as="header" class="topbar topbar--scene" intensity="subtle">
        <div class="topbar__navigation">
          <RouterLink class="topbar-brand" to="/workspace" aria-label="CineForge 创作台">
            <span class="topbar-brand__mark"><Clapperboard :size="19" /></span>
            <span class="topbar-brand__copy"><strong>CineForge</strong><small>Production OS</small></span>
          </RouterLink>
          <nav class="topbar-nav" aria-label="主导航">
            <RouterLink
              v-for="item in navItems"
              :key="item.to"
              class="topbar-nav__item"
              :class="{ 'topbar-nav__item--active': item.active }"
              :to="item.to"
            >
              <component :is="item.icon" :size="16" stroke-width="1.9" />
              <span>{{ item.label }}</span>
            </RouterLink>
          </nav>
        </div>
        <div class="topbar__actions" aria-label="账户与系统菜单">
          <div class="topbar-tool" data-tooltip="切换主题">
            <ThemeToggle />
          </div>
          <div class="credit-pill" title="当前积分" aria-label="当前积分">
            <Coins :size="16" />
            <span class="credit-pill__copy"><small>积分</small><strong class="tabular-nums">{{ auth.session?.credit_balance ?? '0' }}</strong></span>
          </div>
          <div class="topbar-tool topbar-tool--activity" data-tooltip="任务与通知">
            <ActivityCenter />
          </div>
          <span class="topbar__divider" aria-hidden="true"></span>
          <div class="account-menu">
            <PopoverRoot v-model:open="accountMenuOpen">
              <PopoverTrigger as-child>
                <button class="account-menu__trigger" type="button" aria-label="账户菜单" title="账户菜单">
                  <span class="avatar">
                    <img
                      v-if="auth.session?.user.avatar_url"
                      class="avatar__image"
                      :src="auth.session.user.avatar_url"
                      :alt="`${auth.session.user.display_name}的头像`"
                    />
                    <template v-else>{{ auth.session?.user.display_name.slice(0, 1) }}</template>
                  </span>
                  <span class="account-menu__copy">
                    <strong class="account-menu__name">{{ auth.session?.user.display_name }}</strong>
                    <small>{{ auth.isAdmin ? '平台管理员' : '创作者' }}</small>
                  </span>
                  <ChevronDown class="account-menu__chevron" :size="14" />
                </button>
              </PopoverTrigger>
              <PopoverPortal>
                <PopoverContent
                  class="account-popover account-popover--scene t-dropdown"
                  data-origin="top-right"
                  :style="{ '--account-cover': `url(${currentSectionImage})` }"
                  :side-offset="10"
                  align="end"
                >
                  <div class="account-popover__hero">
                    <div class="account-popover__identity">
                      <div class="account-popover__avatar-control">
                        <span class="avatar account-popover__avatar">
                          <img
                            v-if="auth.session?.user.avatar_url"
                            class="avatar__image"
                            :src="auth.session.user.avatar_url"
                            :alt="`${auth.session.user.display_name}的头像`"
                          />
                          <template v-else>{{ auth.session?.user.display_name.slice(0, 1) }}</template>
                        </span>
                        <button type="button" title="编辑头像" aria-label="编辑头像" @click="openAvatarEditor">
                          <Camera :size="13" />
                        </button>
                      </div>
                      <span>
                        <strong>{{ auth.session?.user.display_name }}</strong>
                        <small>{{ auth.isAdmin ? '平台管理员' : '创作者账户' }}</small>
                      </span>
                    </div>
                    <span class="account-popover__online"><i></i>ONLINE</span>
                  </div>
                  <div class="account-popover__summary">
                    <div><Coins :size="15" /><span><small>可用积分</small><strong class="tabular-nums">{{ auth.session?.credit_balance ?? '0' }}</strong></span></div>
                    <div><component :is="auth.isAdmin ? ShieldCheck : Clapperboard" :size="15" /><span><small>当前身份</small><strong>{{ auth.isAdmin ? '管理员' : '创作者' }}</strong></span></div>
                  </div>
                  <div class="account-appearance">
                    <div class="account-appearance__label">
                      <label for="account-background-blur">背景虚化强度</label>
                      <output for="account-background-blur">{{ backgroundBlur }}</output>
                    </div>
                    <input id="account-background-blur" v-model.number="backgroundBlur"
                      type="range" min="0" max="30" step="1" :disabled="savingAppearance"
                      :aria-valuetext="backgroundBlur === 0 ? '不虚化' : `${backgroundBlur} 像素`"
                      @input="appearanceStatus = ''" @change="saveAppearance" />
                    <div class="account-appearance__hints"><span>清晰</span><span role="status">{{ appearanceStatus }}</span><span>柔和</span></div>
                  </div>
                  <button v-if="pwa.canInstall.value" class="account-popover__install" type="button" @click="installApp">
                    <Download :size="16" />
                    <span>安装到桌面</span>
                  </button>
                  <button class="account-popover__logout" type="button" @click="logout">
                    <LogOut :size="16" />
                    <span>退出登录</span>
                  </button>
                </PopoverContent>
              </PopoverPortal>
            </PopoverRoot>
          </div>
        </div>
      </LiquidGlass>

      <main class="page-content"><slot /></main>
    </section>

    <AvatarCropDialog
      v-if="auth.session"
      v-model:open="avatarEditorOpen"
      :avatar-url="auth.session.user.avatar_url"
      :display-name="auth.session.user.display_name"
      @updated="handleAvatarUpdated"
    />

    <PwaInstallPrompt />

    <nav class="mobile-nav" aria-label="移动端主导航">
      <RouterLink
        v-for="item in navItems"
        :key="item.to"
        class="mobile-nav__item"
        :class="{ 'mobile-nav__item--active': item.active }"
        :to="item.to"
      >
        <component :is="item.icon" :size="20" />
        <span>{{ item.label }}</span>
      </RouterLink>
    </nav>
  </div>
</template>
