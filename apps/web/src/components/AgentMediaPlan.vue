<script setup lang="ts">
import { computed } from 'vue'
import { Check, Image, Video, TriangleAlert, Clock3, Pause } from 'lucide-vue-next'
import { mediaPlanView } from '@/lib/mediaPlanView'

const props = defineProps<{ plan: unknown; task?: unknown }>()
const view = computed(() => mediaPlanView(props.plan, props.task))
</script>

<template>
  <section v-if="view" class="agent-media-plan" aria-label="连续创作进度">
    <header><strong>{{ view.title }}</strong><span>{{ view.mode }}</span></header>
    <ol>
      <li v-for="step in view.steps" :key="step.index" :data-state="step.status">
        <span class="agent-media-plan__marker" aria-hidden="true">
          <Check v-if="step.status === 'done'" :size="15" />
          <TriangleAlert v-else-if="step.status === 'failed'" :size="15" />
          <Pause v-else-if="step.status === 'paused'" :size="14" />
          <span v-else>{{ step.index + 1 }}</span>
        </span>
        <div><b><Video v-if="step.type === 'video'" :size="14" /><Image v-else :size="14" />{{ step.title }}</b>
          <small v-if="step.specs">{{ step.specs }}</small>
          <small v-if="step.dependency">{{ step.dependency }}</small>
        </div>
        <span class="agent-media-plan__state"><Clock3 v-if="step.status === 'queued'" :size="12" />{{ step.label }}</span>
      </li>
    </ol>
    <footer aria-live="polite" role="status">
      <span v-if="view.detail">{{ view.detail }}</span>
      <span v-else>{{ view.done }} / {{ view.total }} 步已完成</span>
      <small v-if="view.failed">已完成结果会保留，重试只处理未完成步骤。</small>
    </footer>
  </section>
</template>

<style scoped>
.agent-media-plan { box-sizing: border-box; width: min(100%, 560px); margin: 16px auto; padding: 14px; border-radius: 18px; background: var(--glass-inset, var(--surface)); color: var(--ink); box-shadow: inset 0 0 0 1px var(--glass-edge, var(--line)); font-size: 13px; font-variant-numeric: tabular-nums; }
header { display: flex; gap: 10px; align-items: center; justify-content: space-between; margin-bottom: 12px; }
header strong { font-size: 13px; text-wrap: balance; }
header > span { color: var(--muted); font-size: 11px; flex-shrink: 0; }
ol { list-style: none; padding: 0; margin: 0; display: grid; gap: 8px; }
li { display: grid; grid-template-columns: 26px minmax(0, 1fr) auto; align-items: start; gap: 9px; padding: 10px; border-radius: 12px; }
li[data-state="running"], li[data-state="queued"] { background: color-mix(in srgb, var(--accent, #448aff) 10%, transparent); }
.agent-media-plan__marker { width: 24px; height: 24px; display: grid; place-items: center; border-radius: 50%; background: color-mix(in srgb, var(--ink) 7%, transparent); font-size: 11px; }
li[data-state="done"] .agent-media-plan__marker { color: #20866e; background: color-mix(in srgb, #36b996 14%, transparent); }
li[data-state="failed"] .agent-media-plan__marker { color: var(--danger, #d96060); }
b { display: flex; align-items: center; gap: 6px; font-weight: 500; line-height: 24px; }
small { display: block; color: var(--muted); font-size: 11px; line-height: 1.6; overflow-wrap: anywhere; }
.agent-media-plan__state { display: flex; align-items: center; gap: 4px; color: var(--muted); font-size: 11px; line-height: 24px; white-space: nowrap; }
footer { display: grid; gap: 3px; margin-top: 10px; padding: 10px 4px 0; border-top: 1px solid var(--line); font-size: 12px; line-height: 1.6; overflow-wrap: anywhere; }
@media (max-width: 600px) { .agent-media-plan { padding: 12px; margin: 12px 0; } li { padding: 8px 4px; gap: 7px; } }
</style>
