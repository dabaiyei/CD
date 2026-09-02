import { createApp } from 'vue'

import App from '@/App.vue'
import { router } from '@/router'
import { pinia } from '@/stores'
import { useAuthStore } from '@/stores/auth'
import { motionDirective } from '@/lib/motion'
import '@/styles.css'

window.addEventListener('cineforge:auth-expired', () => {
  const auth = useAuthStore(pinia)
  auth.clearSession()
  if (router.currentRoute.value.name !== 'login') {
    void router.replace({ name: 'login', query: { reason: 'expired' } })
  }
})

createApp(App).directive('motion', motionDirective).use(pinia).use(router).mount('#app')
