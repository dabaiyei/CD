import { createRouter, createWebHistory } from 'vue-router'

import { pinia } from '@/stores'
import { useAuthStore } from '@/stores/auth'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('@/views/LoginView.vue'), meta: { public: true } },
    { path: '/', redirect: '/workspace' },
    { path: '/workspace', name: 'workspace', component: () => import('@/views/WorkspaceView.vue') },
    { path: '/skills', name: 'user-skills', component: () => import('@/views/UserSkillsView.vue') },
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
  if (to.meta.public) return auth.isAuthenticated ? '/workspace' : true
  if (!auth.isAuthenticated) return { name: 'login', query: { redirect: to.fullPath } }
  if (to.meta.admin && !auth.isAdmin) return '/workspace'
  return true
})
