<script setup lang="ts">
import { Download, Image as ImageIcon, Video, X } from 'lucide-vue-next'
import {
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogOverlay,
  DialogPortal,
  DialogRoot,
  DialogTitle,
} from 'reka-ui'

import type { AgentGeneratedMedia } from '@/types'

defineProps<{
  open: boolean
  media: AgentGeneratedMedia | null
}>()

const emit = defineEmits<{ 'update:open': [value: boolean] }>()

function mediaModeLabel(mode: string | null): string {
  return {
    text_to_image: '文生图',
    image_to_image: '参考生图',
    text_to_video: '文生视频',
    image_to_video: '图生视频',
    first_frame: '首帧参考',
    first_last_frame: '首尾帧参考',
    full_reference: '全参考生成',
    multi_shot: '多图参考',
  }[mode || ''] || mode || ''
}
</script>

<template>
  <DialogRoot :open="open" @update:open="emit('update:open', $event)">
    <DialogPortal>
      <DialogOverlay class="dialog-overlay agent-media-viewer__overlay" />
      <DialogContent v-if="media" class="agent-media-viewer t-modal">
        <DialogTitle class="sr-only">{{ media.name }}</DialogTitle>
        <DialogDescription class="sr-only">
          {{ media.mime_type.startsWith('video/') ? '视频生成结果预览' : '图片生成结果预览' }}
        </DialogDescription>

        <header class="agent-media-viewer__header">
          <span class="agent-media-viewer__kind">
            <Video v-if="media.mime_type.startsWith('video/')" :size="17" />
            <ImageIcon v-else :size="17" />
          </span>
          <div>
            <strong>{{ media.name }}</strong>
            <small>
              {{ media.model_name }} · {{ media.resolution }} · {{ media.aspect_ratio }}
              <template v-if="media.duration_seconds"> · {{ media.duration_seconds }} 秒</template>
              <template v-if="media.generation_mode"> · {{ mediaModeLabel(media.generation_mode) }}</template>
            </small>
          </div>
          <a :href="media.media_url" :download="media.name" title="下载媒体">
            <Download :size="17" />
          </a>
          <DialogClose title="关闭预览"><X :size="19" /></DialogClose>
        </header>

        <div class="agent-media-viewer__stage">
          <video
            v-if="media.mime_type.startsWith('video/')"
            :src="media.media_url"
            controls
            preload="metadata"
            playsinline
          ></video>
          <img v-else :src="media.media_url" :alt="media.name" />
        </div>
      </DialogContent>
    </DialogPortal>
  </DialogRoot>
</template>

<style scoped>
.agent-media-viewer {
  position: fixed;
  z-index: 191;
  inset: 24px;
  display: grid;
  min-width: 0;
  grid-template-rows: 56px minmax(0, 1fr);
  overflow: hidden;
  border-radius: 14px;
  color: #f7f8f8;
  background: rgb(12 15 17 / 96%);
  box-shadow: 0 28px 100px rgb(0 0 0 / 42%);
}

.agent-media-viewer__overlay {
  background: rgb(7 9 11 / 72%);
  backdrop-filter: blur(10px);
}

.agent-media-viewer__header {
  display: grid;
  min-width: 0;
  grid-template-columns: 34px minmax(0, 1fr) 40px 40px;
  align-items: center;
  gap: 7px;
  padding: 8px 10px 8px 12px;
  border-bottom: 1px solid rgb(255 255 255 / 9%);
  background: rgb(20 24 27 / 92%);
}

.agent-media-viewer__kind,
.agent-media-viewer__header > a,
.agent-media-viewer__header > button {
  display: inline-flex;
  width: 40px;
  height: 40px;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: 8px;
}

.agent-media-viewer__kind {
  width: 34px;
  height: 34px;
  color: #ffb8a8;
  background: rgb(255 255 255 / 8%);
}

.agent-media-viewer__header > div {
  min-width: 0;
}

.agent-media-viewer__header strong,
.agent-media-viewer__header small {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.agent-media-viewer__header strong {
  font-size: 12px;
  font-weight: 650;
}

.agent-media-viewer__header small {
  margin-top: 2px;
  color: rgb(255 255 255 / 52%);
  font-size: 9px;
  font-variant-numeric: tabular-nums;
}

.agent-media-viewer__header > a,
.agent-media-viewer__header > button {
  color: rgb(255 255 255 / 70%);
  background: transparent;
  cursor: pointer;
  transition-property: color, background-color, scale;
  transition-duration: var(--duration-quick);
  transition-timing-function: var(--ease-smooth-out);
}

.agent-media-viewer__header > a:hover,
.agent-media-viewer__header > button:hover {
  color: #fff;
  background: rgb(255 255 255 / 9%);
}

.agent-media-viewer__header > a:active,
.agent-media-viewer__header > button:active {
  scale: .96;
}

.agent-media-viewer__stage {
  display: flex;
  min-width: 0;
  min-height: 0;
  align-items: center;
  justify-content: center;
  overflow: auto;
  padding: 20px;
}

.agent-media-viewer__stage img,
.agent-media-viewer__stage video {
  display: block;
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  border-radius: 8px;
  outline: 1px solid rgb(255 255 255 / 10%);
  outline-offset: -1px;
  box-shadow: 0 18px 60px rgb(0 0 0 / 36%);
}

@media (max-width: 760px) {
  .agent-media-viewer {
    inset: 0;
    grid-template-rows: 58px minmax(0, 1fr);
    border-radius: 0;
  }

  .agent-media-viewer__header {
    padding-inline: 9px 6px;
  }

  .agent-media-viewer__stage {
    padding: 10px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .agent-media-viewer,
  .agent-media-viewer__overlay,
  .agent-media-viewer__header > a,
  .agent-media-viewer__header > button {
    animation: none !important;
    transition: none !important;
  }
}
</style>
