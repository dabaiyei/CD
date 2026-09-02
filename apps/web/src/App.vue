<script setup lang="ts">
import { RouterView } from 'vue-router'
import type { RouteLocationNormalizedLoaded } from 'vue-router'

import AppShell from '@/components/AppShell.vue'
import ToastViewport from '@/components/ToastViewport.vue'
import { cancelRouteMotion, enterRoute, leaveRoute } from '@/lib/motion'

function routeMotionKey(route: RouteLocationNormalizedLoaded): string {
  if (route.name === 'director') return `director:${String(route.params.id ?? '')}`
  if (route.name === 'invite-register') return `invite:${String(route.params.code ?? '')}`
  if (route.name === 'marketplace') return `marketplace:${String(route.params.kind ?? '')}`
  return String(route.name ?? route.path)
}
</script>

<template>
  <RouterView v-slot="{ Component, route }">
    <Transition
      v-if="route.meta.public"
      :css="false"
      mode="out-in"
      @enter="enterRoute"
      @leave="leaveRoute"
      @enter-cancelled="cancelRouteMotion"
      @leave-cancelled="cancelRouteMotion"
    >
      <component :is="Component" :key="routeMotionKey(route)" />
    </Transition>
    <AppShell v-else>
      <Transition
        :css="false"
        mode="out-in"
        @enter="enterRoute"
        @leave="leaveRoute"
        @enter-cancelled="cancelRouteMotion"
        @leave-cancelled="cancelRouteMotion"
      >
        <component :is="Component" :key="routeMotionKey(route)" />
      </Transition>
    </AppShell>
  </RouterView>
  <ToastViewport />
</template>
