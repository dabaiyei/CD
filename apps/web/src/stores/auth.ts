import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { api, setToken } from '@/lib/api'
import type { UserSession } from '@/types'

export const useAuthStore = defineStore('auth', () => {
  const session = ref<UserSession | null>(null)
  const loading = ref(false)
  const initialized = ref(false)

  const isAuthenticated = computed(() => Boolean(session.value))
  const isAdmin = computed(() => session.value?.user.role === 'admin')

  async function login(tenant: string, email: string, password: string): Promise<void> {
    loading.value = true
    try {
      const token = await api<{ access_token: string }>('/auth/login', {
        method: 'POST',
        body: JSON.stringify({ tenant, email, password }),
      })
      setToken(token.access_token)
      session.value = await api<UserSession>('/auth/me')
      initialized.value = true
    } catch (error) {
      setToken(null)
      session.value = null
      throw error
    } finally {
      loading.value = false
    }
  }

  async function restore(): Promise<void> {
    if (initialized.value) return
    try {
      session.value = await api<UserSession>('/auth/me')
    } catch {
      setToken(null)
      session.value = null
    } finally {
      initialized.value = true
    }
  }

  async function refreshSession(): Promise<void> {
    session.value = await api<UserSession>('/auth/me')
  }

  function clearSession(): void {
    setToken(null)
    session.value = null
    initialized.value = true
  }

  async function logout(): Promise<void> {
    try {
      await api('/auth/logout', { method: 'POST' })
    } finally {
      clearSession()
    }
  }

  return { session, loading, initialized, isAuthenticated, isAdmin, login, restore, refreshSession, clearSession, logout }
})
