import { computed, readonly, ref } from 'vue'

export type Theme = 'dark' | 'light'

const STORAGE_KEY = 'cineforge-theme'
const currentTheme = ref<Theme>('dark')

function applyTheme(theme: Theme): void {
  currentTheme.value = theme
  document.documentElement.dataset.theme = theme
  document.documentElement.style.colorScheme = theme
}

export function initializeTheme(): void {
  const stored = window.localStorage.getItem(STORAGE_KEY)
  applyTheme(stored === 'light' || stored === 'dark' ? stored : 'dark')
}

export function useTheme() {
  const isDark = computed(() => currentTheme.value === 'dark')

  function setTheme(theme: Theme): void {
    window.localStorage.setItem(STORAGE_KEY, theme)
    applyTheme(theme)
  }

  function toggleTheme(): void {
    setTheme(isDark.value ? 'light' : 'dark')
  }

  return {
    theme: readonly(currentTheme),
    isDark,
    setTheme,
    toggleTheme,
  }
}
