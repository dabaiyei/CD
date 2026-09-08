<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { Film, LoaderCircle } from 'lucide-vue-next'
import { api, apiBlob } from '@/lib/api'
import { useToastStore } from '@/stores/toast'
import type { AITask } from '@/types'

const props = defineProps<{ projectId: string; chapterId: string; storyboardId: string; count: number }>()
const toast = useToastStore()
const busy = ref(false)
const progress = ref(0)
const base = computed(() => `/projects/${props.projectId}/chapters/${props.chapterId}/storyboards/${props.storyboardId}/videos/concat`)
let timer: ReturnType<typeof setTimeout> | undefined
let generation = 0
function reset() { generation++; clearTimeout(timer); busy.value = false; progress.value = 0 }
watch(base, reset)
onBeforeUnmount(reset)

async function download(path: string, id: string) {
  const result = await apiBlob(`${path}/${id}/download`)
  const url = URL.createObjectURL(result.blob)
  const link = document.createElement('a')
  link.href = url
  link.download = result.filename || '拼接视频.mp4'
  document.body.appendChild(link)
  link.click()
  link.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000)
}
function failed(error: unknown, version: number) {
  if (generation !== version) return
  busy.value = false
  toast.show('拼接视频下载未完成', { tone: 'error', message: error instanceof Error ? error.message : '请重试' })
}
async function poll(path: string, id: string, version: number): Promise<void> {
  try {
    const task = await api<AITask>(`/tasks/${id}`)
    if (generation !== version) return
    progress.value = task.progress || 0
    if (task.status === 'succeeded') {
      await download(path, id)
      if (generation === version) {
        busy.value = false
        toast.show('拼接视频已开始下载', { tone: 'success' })
      }
    } else if (task.status === 'failed' || task.status === 'cancelled') {
      throw new Error(task.error_message || '视频拼接任务已停止')
    } else timer = setTimeout(() => { void poll(path, id, version) }, 2500)
  } catch (error) { failed(error, version) }
}
async function start() {
  if (busy.value || !props.count) return
  busy.value = true
  progress.value = 0
  const version = ++generation
  const path = base.value
  try {
    const task = await api<{ id: string }>(path, { method: 'POST' })
    if (version !== generation) return
    await poll(path, task.id, version)
  } catch (error) { failed(error, version) }
}
</script>

<template>
  <button class="button button--secondary video-concat-button" type="button" :disabled="busy || !count"
    :aria-busy="busy" title="按镜头顺序拼接已完成的生效视频，保留原声；未完成镜头不包含在内" @click="start">
    <LoaderCircle v-if="busy" class="spin" :size="15" /><Film v-else :size="15" />
    <span aria-live="polite">{{ busy ? (progress ? `正在拼接 ${progress}%` : '等待拼接') : '下载拼接视频' }}</span>
  </button>
</template>

<style scoped>
.video-concat-button { min-height: 40px; font-variant-numeric: tabular-nums; transition: transform 160ms ease, opacity 160ms ease; }
.video-concat-button:active:not(:disabled) { transform: scale(.96); }
@media (prefers-reduced-motion: reduce) { .video-concat-button { transition: none; } .video-concat-button:active:not(:disabled) { transform: none; } }
</style>
