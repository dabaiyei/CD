<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { Maximize2, RotateCw, Check, LoaderCircle, AlertCircle } from 'lucide-vue-next'
import { useTheme } from '@/lib/theme'
const frame = ref<HTMLIFrameElement>()
const stage = ref<HTMLElement>()
const status = ref('loading')
const error = ref('')
const frameKey = ref(0)
// Plugin handoffs stay in the fragment, outside request URLs and server logs.
const agentParams = new URLSearchParams(location.hash.replace(/^#/, ''))
const frameUrl = new URL('/canvas-app/index.html', location.origin)
const mode = new URLSearchParams(location.search).get('mode')
const canvasQuery = new URLSearchParams(mode ? { mode } : {})
frameUrl.hash = `/canvas${canvasQuery.size ? `?${canvasQuery}` : ''}${agentParams.has('agentUrl') || agentParams.has('agentToken') ? `#${agentParams}` : ''}`
const frameSource = frameUrl.href
if (agentParams.has('agentUrl') || agentParams.has('agentToken')) history.replaceState(history.state, '', location.pathname + location.search)
const availableHeight = ref('calc(100dvh - 114px)')
let headerObserver: ResizeObserver | undefined
const { theme } = useTheme()
function receive(event: MessageEvent) {
  if (event.origin !== location.origin || event.source !== frame.value?.contentWindow || event.data?.source !== 'cineforge-canvas') return
  status.value = event.data.status
  if (event.data.status === 'error') error.value = event.data.message || '画布操作失败，请重试'
  else if (event.data.status === 'saved') error.value = ''
}
function syncTheme() { frame.value?.contentWindow?.postMessage({ source: 'cineforge-host', theme: theme.value }, location.origin) }
function reload() { status.value = 'loading'; error.value = ''; frameKey.value++ }
async function fullscreen() {
  try { if (document.fullscreenElement) await document.exitFullscreen(); else await stage.value?.requestFullscreen() }
  catch { error.value = '当前浏览器不支持全屏，可旋转设备以扩大画布' }
}
watch(theme, syncTheme)
function resize() {
  if (!stage.value || document.fullscreenElement === stage.value) return
  const bottom = innerWidth <= 700 ? 8 : 18
  const viewportBottom = visualViewport?.height || innerHeight
  const nav = document.querySelector('.mobile-nav')?.getBoundingClientRect()
  const visibleBottom = nav && nav.height && nav.top > 0 && nav.top < viewportBottom ? nav.top : viewportBottom
  availableHeight.value = `${Math.max(80, visibleBottom - stage.value.getBoundingClientRect().top - bottom)}px`
}
onMounted(() => {
  window.addEventListener('message', receive)
  window.addEventListener('resize', resize)
  window.addEventListener('fullscreenchange', resize)
  visualViewport?.addEventListener('resize', resize)
  headerObserver = new ResizeObserver(resize)
  const header = document.querySelector('.topbar')
  if (header) headerObserver.observe(header)
  resize()
})
onUnmounted(() => {
  window.removeEventListener('message', receive)
  window.removeEventListener('resize', resize)
  window.removeEventListener('fullscreenchange', resize)
  visualViewport?.removeEventListener('resize', resize)
  headerObserver?.disconnect()
})
</script>
<template>
  <section ref="stage" class="infinite-canvas-workspace" :style="{ '--canvas-height': availableHeight }">
    <div class="canvas-statusbar">
      <span class="canvas-status"><AlertCircle v-if="error" :size="14" /><LoaderCircle v-else-if="status === 'loading' || status === 'saving'" :size="14" class="canvas-spinner" /><Check v-else :size="14" />
        {{ error || (status === 'loading' ? '正在打开画布' : status === 'saving' ? '正在保存' : '已连接 · 自动保存到当前账号') }}</span>
      <div><button aria-label="刷新画布" title="刷新画布" @click="reload"><RotateCw :size="16" /></button><button aria-label="全屏画布" title="全屏画布" @click="fullscreen"><Maximize2 :size="16" /></button></div>
    </div>
    <iframe :key="frameKey" ref="frame" :src="frameSource" title="无限画布创作工作区" allow="clipboard-write; fullscreen" @load="syncTheme" />
  </section>
</template>
<style scoped>
:global(.app-shell.app-shell--infinite-canvas),:global(.app-shell.app-shell--infinite-canvas .shell-main) { height:100dvh;min-height:0;overflow:hidden; }
:global(.app-shell.app-shell--infinite-canvas .shell-main) { display:flex;flex-direction:column; }
:global(.app-shell.app-shell--infinite-canvas .page-content) { flex:1;min-height:0;padding:0;overflow:hidden; }
.infinite-canvas-workspace { height:var(--canvas-height);min-height:0;margin:14px 24px 18px;border-radius:20px;overflow:hidden;display:flex;flex-direction:column;background:var(--surface);box-shadow:0 0 0 1px rgb(128 148 155 / 20%),0 18px 60px rgb(0 0 0 / 12%); }
.infinite-canvas-workspace:fullscreen { height:100dvh;margin:0;border-radius:0; }
.canvas-statusbar { display:flex;justify-content:space-between;align-items:center;gap:12px;padding:4px 12px;min-height:38px;color:var(--muted);background:var(--surface-subtle);font-size:12px; }
.canvas-status { display:flex;gap:7px;align-items:center;min-width:0;overflow-wrap:anywhere; }
.canvas-statusbar>div { display:flex;gap:4px;flex-shrink:0; }
.canvas-statusbar button { display:grid;place-items:center;width:36px;height:36px;border:0;border-radius:10px;background:transparent;color:inherit;cursor:pointer; }
.canvas-statusbar button:hover { background:var(--surface-strong); }
.infinite-canvas-workspace iframe { width:100%;height:100%;flex:1;min-height:0;border:0; }
.canvas-spinner { animation:canvas-spin 1s linear infinite; }
@keyframes canvas-spin { to { transform:rotate(360deg); } }
@media(max-width:700px) { .infinite-canvas-workspace { margin:8px;border-radius:14px; }.canvas-statusbar { padding:2px 8px; }.canvas-statusbar button { width:44px;height:44px; } }
@media(prefers-reduced-motion:reduce) { .canvas-spinner { animation:none; } }
</style>
