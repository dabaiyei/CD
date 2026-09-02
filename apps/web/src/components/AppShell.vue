<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { gsap } from 'gsap'
import { useRoute, useRouter } from 'vue-router'
import {
  ChevronDown,
  Clapperboard,
  Coins,
  BrainCircuit,
  LayoutGrid,
  LogOut,
  ShieldCheck,
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
import ThemeToggle from '@/components/ThemeToggle.vue'

const auth = useAuthStore()
const activity = useActivityStore()
const route = useRoute()
const router = useRouter()
const { theme } = useTheme()
const accountMenuOpen = ref(false)
const topbarRef = ref<HTMLElement | null>(null)

const currentSectionImage = computed(() => {
  const light = theme.value === 'light'
  if (route.name === 'director') return light ? '/covers/studio-hero-light.webp' : '/covers/default-project-city-v2-wide.webp'
  if (route.path.startsWith('/skills')) return light ? '/covers/skills-lab-light.webp' : '/covers/skills-lab-v2.webp'
  if (route.path.startsWith('/admin')) return light ? '/covers/admin-console-light.webp' : '/covers/admin-console-v2.webp'
  return light ? '/covers/studio-hero-light.webp' : '/covers/studio-hero-v2.webp'
})

const navItems = computed(() => [
  { label: '创作台', icon: LayoutGrid, to: '/workspace', active: route.path.startsWith('/workspace') || route.name === 'director' },
  { label: '我的 Skills', icon: BrainCircuit, to: '/skills', active: route.path.startsWith('/skills') },
  ...(auth.isAdmin
    ? [{ label: '管理', icon: ShieldCheck, to: '/admin/overview', active: route.path.startsWith('/admin') }]
    : []),
])

onMounted(() => {
  activity.start()
})

onUnmounted(() => {
  activity.stop()
  if (topbarRef.value) gsap.killTweensOf(topbarRef.value)
})

function moveTopbarScene(event: PointerEvent): void {
  const topbar = topbarRef.value
  if (!topbar || window.matchMedia('(prefers-reduced-motion: reduce), (pointer: coarse)').matches) return
  const bounds = topbar.getBoundingClientRect()
  const x = ((event.clientX - bounds.left) / bounds.width - 0.5) * 20
  const y = ((event.clientY - bounds.top) / bounds.height - 0.5) * 10
  gsap.to(topbar, {
    '--masthead-x': `${x}px`,
    '--masthead-y': `${y}px`,
    duration: 0.72,
    ease: 'power3.out',
    overwrite: 'auto',
  })
}

function resetTopbarScene(): void {
  if (!topbarRef.value) return
  gsap.to(topbarRef.value, {
    '--masthead-x': '0px',
    '--masthead-y': '0px',
    duration: 0.9,
    ease: 'power3.out',
    overwrite: 'auto',
  })
}

async function logout(): Promise<void> {
  accountMenuOpen.value = false
  await auth.logout()
  await router.push('/login')
}
</script>

<template>
  <div class="app-shell app-shell--top-navigation">
    <section class="shell-main">
      <header
        ref="topbarRef"
        class="topbar topbar--scene"
        :style="{ '--topbar-image': `url(${currentSectionImage})` }"
        @pointermove="moveTopbarScene"
        @pointerleave="resetTopbarScene"
      >
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
                  <span class="avatar">{{ auth.session?.user.display_name.slice(0, 1) }}</span>
                  <span class="account-menu__copy">
                    <strong class="account-menu__name">{{ auth.session?.user.display_name }}</strong>
                    <small>{{ auth.isAdmin ? '平台管理员' : '创作者' }}</small>
                  </span>
                  <ChevronDown class="account-menu__chevron" :size="14" />
                </button>
              </PopoverTrigger>
              <PopoverPortal>
                <PopoverContent
                  class="account-popover account-popover--scene"
                  :style="{ '--account-cover': `url(${currentSectionImage})` }"
                  :side-offset="10"
                  align="end"
                >
                  <div class="account-popover__hero">
                    <div class="account-popover__identity">
                      <span class="avatar account-popover__avatar">{{ auth.session?.user.display_name.slice(0, 1) }}</span>
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
                  <button class="account-popover__logout" type="button" @click="logout">
                    <LogOut :size="16" />
                    <span>退出登录</span>
                  </button>
                </PopoverContent>
              </PopoverPortal>
            </PopoverRoot>
          </div>
        </div>
      </header>

      <main class="page-content"><slot /></main>
    </section>

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
