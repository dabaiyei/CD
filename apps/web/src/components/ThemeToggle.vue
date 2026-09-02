<script setup lang="ts">
import { Moon, Sun } from 'lucide-vue-next'

import { useTheme } from '@/lib/theme'

const { isDark, toggleTheme } = useTheme()
</script>

<template>
  <button
    class="theme-toggle icon-button"
    type="button"
    :aria-label="isDark ? '切换到浅色主题' : '切换到深色主题'"
    :aria-pressed="!isDark"
    :title="isDark ? '切换到浅色主题' : '切换到深色主题'"
    @click="toggleTheme"
  >
    <Sun v-if="isDark" :size="16" />
    <Moon v-else :size="16" />
  </button>
</template>
