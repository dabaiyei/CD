import { createRouter, createWebHistory } from 'vue-router'

import { pinia } from '@/stores'
import { useAuthStore } from '@/stores/auth'

function defaultAuthenticatedRoute(isAdmin: boolean): string {
  return isAdmin ? '/admin/overview' : '/workspace'
}

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('@/views/LoginView.vue'), meta: { public: true } },
    {
      path: '/invite/:code',
      name: 'invite-register',
      component: () => import('@/views/InviteRegisterView.vue'),
      meta: { public: true, allowAuthenticated: true },
    },
    { path: '/', redirect: '/workspace' },
    { path: '/workspace', name: 'workspace', component: () => import('@/views/WorkspaceView.vue') },
    { path: '/skills', name: 'user-skills', component: () => import('@/views/UserSkillsView.vue') },
    {
      path: '/marketplace/:kind(skill|template|material)',
      name: 'marketplace',
      component: () => import('@/views/MarketplaceView.vue'),
    },
    {
      path: '/projects/:id/director',
      name: 'director',
      component: () => import('@/views/DirectorView.vue'),
    },
    {
      path: '/admin/:section?',
      name: 'admin',
      component: () => import('@/views/AdminView.vue'),
      meta: { admin: true },
    },
    { path: '/:pathMatch(.*)*', redirect: '/workspace' },
  ],
  scrollBehavior: () => ({ top: 0 }),
})

router.beforeEach(async (to) => {
  const auth = useAuthStore(pinia)
  await auth.restore()
  if (to.meta.public) {
    if (to.meta.allowAuthenticated) return true
    return auth.isAuthenticated ? defaultAuthenticatedRoute(auth.isAdmin) : true
  }
  if (!auth.isAuthenticated) return { name: 'login', query: { redirect: to.fullPath } }
  if (to.meta.admin && !auth.isAdmin) return '/workspace'
  return true
})
