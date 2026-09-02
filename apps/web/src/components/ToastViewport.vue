<script setup lang="ts">
import { CheckCircle2, CircleAlert, Info, X } from 'lucide-vue-next'

import { enterToast, leaveToast } from '@/lib/motion'
import { useToastStore } from '@/stores/toast'

const toast = useToastStore()
</script>

<template>
  <div class="toast-viewport" aria-live="polite" aria-atomic="false">
    <TransitionGroup :css="false" @enter="enterToast" @leave="leaveToast">
      <article v-for="item in toast.messages" :key="item.id" class="toast" :data-tone="item.tone">
        <CheckCircle2 v-if="item.tone === 'success'" :size="18" aria-hidden="true" />
        <CircleAlert v-else-if="item.tone === 'error'" :size="18" aria-hidden="true" />
        <Info v-else :size="18" aria-hidden="true" />
        <div class="toast__copy">
          <strong>{{ item.title }}</strong>
          <p v-if="item.message">{{ item.message }}</p>
        </div>
        <button class="icon-button icon-button--small" type="button" title="关闭" @click="toast.dismiss(item.id)">
          <X :size="16" />
        </button>
      </article>
    </TransitionGroup>
  </div>
</template>
