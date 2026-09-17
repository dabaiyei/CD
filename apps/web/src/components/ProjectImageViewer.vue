<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import VueEasyLightbox from 'vue-easy-lightbox'
import { DialogRoot, DialogPortal, DialogContent, DialogTitle, DialogDescription } from 'reka-ui'
import { ZoomIn, ZoomOut, RotateCcw, RotateCw, Download, X, Maximize } from 'lucide-vue-next'
import { imageViewer } from '@/lib/imageViewer'

const route = useRoute()
const resetKey = ref(0)
let trigger: HTMLElement | null = null
watch(imageViewer, (value) => { if (value) trigger = value.trigger })
watch(() => route.fullPath, () => { imageViewer.value = null })
function close() { imageViewer.value = null }
function restoreFocus(event: Event) {
  event.preventDefault()
  if (trigger?.isConnected) trigger.focus({ preventScroll: true })
}
</script>

<template>
  <DialogRoot :open="Boolean(imageViewer)" @update:open="!$event && close()">
    <DialogPortal>
      <DialogContent class="project-image-viewer" @close-auto-focus="restoreFocus">
        <DialogTitle class="sr-only">{{ imageViewer?.title || '图片预览' }}</DialogTitle>
        <DialogDescription class="sr-only">拖动查看图片，滚轮或双指缩放，可使用底部工具栏旋转。按 Escape 关闭。</DialogDescription>
        <VueEasyLightbox v-if="imageViewer" :key="resetKey" :visible="true" :imgs="[{ src: imageViewer.src, title: imageViewer.title, alt: imageViewer.title }]" :scroll-disabled="true" :esc-disabled="true" @hide="close">
          <template #onerror>
            <div class="project-image-viewer__error" role="alert">
              <strong>图片加载失败</strong>
              <span>请检查网络或重试加载原图</span>
              <button type="button" @click="resetKey++">重新加载</button>
            </div>
          </template>
          <template #toolbar="{ toolbarMethods }">
            <div class="project-image-viewer__tools" role="toolbar" aria-label="图片操作" @pointerdown.stop @click.stop>
              <button type="button" title="缩小" aria-label="缩小" @click="toolbarMethods.zoomOut"><ZoomOut :size="20" /></button>
              <button type="button" title="放大" aria-label="放大" @click="toolbarMethods.zoomIn"><ZoomIn :size="20" /></button>
              <button type="button" title="向左旋转" aria-label="向左旋转" @click="toolbarMethods.rotateLeft"><RotateCcw :size="20" /></button>
              <button type="button" title="向右旋转" aria-label="向右旋转" @click="toolbarMethods.rotateRight"><RotateCw :size="20" /></button>
              <button type="button" title="重置视图" aria-label="重置视图" @click="resetKey++"><Maximize :size="20" /></button>
              <a :href="imageViewer.src" :download="imageViewer.title" target="_blank" rel="noopener" title="下载原图" aria-label="下载原图"><Download :size="20" /></a>
              <button type="button" title="关闭预览" aria-label="关闭预览" @click="close"><X :size="20" /></button>
            </div>
          </template>
        </VueEasyLightbox>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
</template>

<style>
.image-preview-trigger { cursor: zoom-in; }
.image-preview-trigger:focus-visible { outline: 2px solid var(--accent, #498eff); outline-offset: -2px; }
.project-image-viewer { position: fixed; inset: 0; z-index: 10000; outline: none; pointer-events: auto; }
.project-image-viewer .vel-modal { position: absolute; inset: 0; background: rgb(7 10 16 / 94%); }
.project-image-viewer .vel-img-title { padding-inline: 20px; overflow-wrap: anywhere; }
.project-image-viewer .vel-img-wrapper { max-height: calc(100dvh - 160px); }
.project-image-viewer__error { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; color: #fff; }
.project-image-viewer__error button { min-height: 44px; padding: 0 18px; border: 0; border-radius: 12px; background: #303846; color: #fff; cursor: pointer; }
.project-image-viewer__tools { position: absolute; bottom: max(20px, env(safe-area-inset-bottom)); left: 50%; transform: translateX(-50%); display: flex; flex-wrap: wrap; justify-content: center; width: max-content; padding: 6px; gap: 3px; max-width: calc(100vw - 16px); border-radius: 18px; background: #20252e; box-shadow: 0 0 0 1px rgb(255 255 255 / 12%), 0 8px 32px rgb(0 0 0 / 35%); }
.project-image-viewer__tools button, .project-image-viewer__tools a { display: grid; place-items: center; width: 44px; height: 44px; flex-shrink: 0; border: 0; border-radius: 12px; background: transparent; color: #fff; cursor: pointer; }
.project-image-viewer__tools :is(button, a):hover { background: rgb(255 255 255 / 12%); }
.project-image-viewer__tools :is(button, a):focus-visible { outline: 2px solid #81b8ff; }
.project-image-viewer__tools :is(button, a):active { scale: .96; }
</style>
