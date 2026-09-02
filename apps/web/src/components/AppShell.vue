<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ChevronDown,
  Clapperboard,
  Coins,
  BrainCircuit,
  LayoutGrid,
  LogOut,
  Settings2,
  ShieldCheck,
} from 'lucide-vue-next'
import { PopoverContent, PopoverPortal, PopoverRoot, PopoverTrigger } from 'reka-ui'

import { useAuthStore } from '@/stores/auth'
import { useActivityStore } from '@/stores/activity'
import ActivityCenter from '@/components/ActivityCenter.vue'
import ThemeToggle from '@/components/ThemeToggle.vue'

const auth = useAuthStore()
const activity = useActivityStore()
const route = useRoute()
const router = useRouter()
const accountMenuOpen = ref(false)

const currentSection = computed(() => {
  if (route.name === 'director') return { eyebrow: 'PRODUCTION', title: '导演制作台' }
  if (route.path.startsWith('/skills')) return { eyebrow: 'CAPABILITIES', title: '我的 Skills' }
  if (route.path.startsWith('/admin')) return { eyebrow: 'ADMINISTRATION', title: '系统管理' }
  return { eyebrow: 'STUDIO', title: '项目创作台' }
})

const navItems = computed(() => [
  { label: '创作台', icon: LayoutGrid, to: '/workspace', active: route.path.startsWith('/workspace') || route.name === 'director' },
  { label: '我的 Skills', icon: BrainCircuit, to: '/skills', active: route.path.startsWith('/skills') },
  ...(auth.isAdmin
    ? [{ label: '管理', icon: ShieldCheck, to: '/admin/overview', active: route.path.startsWith('/admin') }]
    : []),
])

onMounted(() => activity.start())
onUnmounted(() => activity.stop())

async function logout(): Promise<void> {
  accountMenuOpen.value = false
  await auth.logout()
  await router.push('/login')
}
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <RouterLink class="brand" to="/workspace" aria-label="CineForge 创作台">
        <span class="brand__mark"><Clapperboard :size="20" /></span>
        <span class="brand__name"><strong>CineForge</strong><small>Production OS</small></span>
      </RouterLink>

      <span class="sidebar__label">Workspace</span>
      <nav class="primary-nav" aria-label="主导航">
        <RouterLink
          v-for="item in navItems"
          :key="item.to"
          class="nav-item"
          :class="{ 'nav-item--active': item.active }"
          :to="item.to"
        >
          <component :is="item.icon" :size="19" stroke-width="1.8" />
          <span>{{ item.label }}</span>
        </RouterLink>
      </nav>

      <div class="sidebar__footer">
        <RouterLink v-if="auth.isAdmin" class="nav-item" to="/admin/overview">
          <Settings2 :size="19" />
          <span>系统设置</span>
        </RouterLink>
        <div class="studio-status"><i></i><span>Studio online</span></div>
      </div>
    </aside>

    <section class="shell-main">
      <header class="topbar">
        <div class="topbar__context">
          <span>{{ currentSection.eyebrow }}</span>
          <strong>{{ currentSection.title }}</strong>
        </div>
        <div class="topbar__actions">
          <ThemeToggle />
          <div class="credit-pill" title="当前积分">
            <Coins :size="16" />
            <span class="tabular-nums">{{ auth.session?.credit_balance ?? '0' }}</span>
          </div>
          <ActivityCenter />
          <div class="account-menu">
            <PopoverRoot v-model:open="accountMenuOpen">
              <PopoverTrigger as-child>
                <button class="account-menu__trigger" type="button" aria-label="账户菜单" title="账户菜单">
                  <span class="avatar">{{ auth.session?.user.display_name.slice(0, 1) }}</span>
                  <span class="account-menu__name">{{ auth.session?.user.display_name }}</span>
                  <ChevronDown class="account-menu__chevron" :size="14" />
                </button>
              </PopoverTrigger>
              <PopoverPortal>
                <PopoverContent class="account-popover" :side-offset="9" align="end">
                  <div class="account-popover__identity">
                    <span class="avatar">{{ auth.session?.user.display_name.slice(0, 1) }}</span>
                    <span>
                      <strong>{{ auth.session?.user.display_name }}</strong>
                      <small>{{ auth.isAdmin ? '管理员账户' : '创作者账户' }}</small>
                    </span>
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
