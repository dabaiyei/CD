<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import Cropper from 'cropperjs'
import 'cropperjs/dist/cropper.css'
import {
  Check,
  ImagePlus,
  LoaderCircle,
  RefreshCcw,
  RotateCcw,
  RotateCw,
  Trash2,
  Upload,
  ZoomIn,
  ZoomOut,
} from 'lucide-vue-next'

import BaseDialog from '@/components/BaseDialog.vue'
import { api } from '@/lib/api'
import { useToastStore } from '@/stores/toast'
import type { User } from '@/types'

const props = defineProps<{
  open: boolean
  avatarUrl: string | null
  displayName: string
}>()

const emit = defineEmits<{
  'update:open': [value: boolean]
  updated: [user: User]
}>()

const toast = useToastStore()
const fileInput = ref<HTMLInputElement | null>(null)
const cropImage = ref<HTMLImageElement | null>(null)
const sourceUrl = ref('')
const sourceName = ref('')
const busy = ref(false)
const dragging = ref(false)
const deleteArmed = ref(false)
let cropper: Cropper | null = null
let objectUrl: string | null = null
let cleanupTimer: number | null = null

watch(
  () => props.open,
  (open) => {
    if (cleanupTimer !== null) {
      window.clearTimeout(cleanupTimer)
      cleanupTimer = null
    }
    if (open) {
      resetSelection()
      deleteArmed.value = false
      return
    }
    cleanupTimer = window.setTimeout(() => {
      resetSelection()
      cleanupTimer = null
    }, 180)
  },
)

onBeforeUnmount(() => {
  if (cleanupTimer !== null) window.clearTimeout(cleanupTimer)
  resetSelection()
})

function destroyCropper(): void {
  cropper?.destroy()
  cropper = null
}

function revokeObjectUrl(): void {
  if (objectUrl) URL.revokeObjectURL(objectUrl)
  objectUrl = null
}

function resetSelection(): void {
  destroyCropper()
  revokeObjectUrl()
  sourceUrl.value = ''
  sourceName.value = ''
  dragging.value = false
  deleteArmed.value = false
  if (fileInput.value) fileInput.value.value = ''
}

function chooseFile(): void {
  if (!busy.value) fileInput.value?.click()
}

function validateFile(file: File): string | null {
  if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) {
    return '仅支持 JPG、PNG 或 WebP 图片'
  }
  if (file.size > 8 * 1024 * 1024) return '头像图片不能超过 8 MB'
  if (!file.size) return '图片文件为空'
  return null
}

async function loadFile(file: File): Promise<void> {
  const error = validateFile(file)
  if (error) {
    toast.show('无法使用这张图片', { message: error, tone: 'error' })
    return
  }

  destroyCropper()
  revokeObjectUrl()
  objectUrl = URL.createObjectURL(file)
  sourceUrl.value = objectUrl
  sourceName.value = file.name
  deleteArmed.value = false
  await nextTick()
}

function onFileChange(event: Event): void {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (file) void loadFile(file)
}

function onDrop(event: DragEvent): void {
  dragging.value = false
  const file = event.dataTransfer?.files?.[0]
  if (file) void loadFile(file)
}

function initializeCropper(): void {
  if (!cropImage.value || !sourceUrl.value) return
  destroyCropper()
  cropper = new Cropper(cropImage.value, {
    aspectRatio: 1,
    viewMode: 1,
    dragMode: 'move',
    autoCropArea: 0.84,
    background: false,
    center: false,
    guides: false,
    highlight: false,
    cropBoxMovable: false,
    cropBoxResizable: false,
    toggleDragModeOnDblclick: false,
    responsive: true,
    restore: false,
    preview: '.avatar-cropper__preview',
  })
}

function zoom(ratio: number): void {
  cropper?.zoom(ratio)
}

function rotate(degrees: number): void {
  cropper?.rotate(degrees)
}

function resetCrop(): void {
  cropper?.reset()
}

function croppedBlob(): Promise<Blob> {
  return new Promise((resolve, reject) => {
    const canvas = cropper?.getCroppedCanvas({
      width: 512,
      height: 512,
      imageSmoothingEnabled: true,
      imageSmoothingQuality: 'high',
      fillColor: '#ffffff',
    })
    if (!canvas) {
      reject(new Error('请先选择需要裁剪的图片'))
      return
    }
    canvas.toBlob(
      (blob) => (blob ? resolve(blob) : reject(new Error('头像裁剪失败，请更换图片重试'))),
      'image/webp',
      0.9,
    )
  })
}

async function saveAvatar(): Promise<void> {
  if (!cropper || busy.value) return
  busy.value = true
  try {
    const blob = await croppedBlob()
    const form = new FormData()
    form.append('file', blob, 'avatar.webp')
    const user = await api<User>('/auth/me/avatar', { method: 'PUT', body: form })
    emit('updated', user)
    emit('update:open', false)
    toast.show('头像已更新', { tone: 'success' })
  } catch (error) {
    toast.show('头像保存失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  } finally {
    busy.value = false
  }
}

async function deleteAvatar(): Promise<void> {
  if (!deleteArmed.value) {
    deleteArmed.value = true
    return
  }
  busy.value = true
  try {
    const user = await api<User>('/auth/me/avatar', { method: 'DELETE' })
    emit('updated', user)
    emit('update:open', false)
    toast.show('头像已移除', { tone: 'success' })
  } catch (error) {
    toast.show('头像删除失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <BaseDialog
    :open="open"
    title="编辑个人头像"
    description="选择图片并调整取景范围，保存后将在所有设备同步显示。"
    wide
    @update:open="emit('update:open', $event)"
  >
    <input
      ref="fileInput"
      class="sr-only"
      type="file"
      accept="image/jpeg,image/png,image/webp"
      @change="onFileChange"
    />

    <div class="avatar-editor">
      <section
        class="avatar-editor__stage"
        :class="{ 'avatar-editor__stage--dragging': dragging }"
        @dragenter.prevent="dragging = true"
        @dragover.prevent="dragging = true"
        @dragleave.prevent="dragging = false"
        @drop.prevent="onDrop"
      >
        <template v-if="sourceUrl">
          <img ref="cropImage" :src="sourceUrl" :alt="sourceName" @load="initializeCropper" />
        </template>
        <button v-else class="avatar-editor__empty" type="button" @click="chooseFile">
          <span class="avatar-editor__empty-icon"><ImagePlus :size="26" /></span>
          <span><strong>选择一张头像图片</strong><small>点击选择或拖放到这里</small></span>
          <em>JPG · PNG · WEBP，最大 8 MB</em>
        </button>
        <div v-if="dragging" class="avatar-editor__drop-hint">
          <Upload :size="24" />
          <span>松开即可载入图片</span>
        </div>
      </section>

      <aside class="avatar-editor__sidebar">
        <div class="avatar-editor__preview-card">
          <span class="avatar-editor__label">头像预览</span>
          <div class="avatar-cropper__preview-shell">
            <div v-if="sourceUrl" class="avatar-cropper__preview"></div>
            <img v-else-if="avatarUrl" :src="avatarUrl" :alt="`${displayName}的头像`" />
            <span v-else>{{ displayName.slice(0, 1) }}</span>
          </div>
          <small>{{ sourceUrl ? '拖动画面调整主体位置' : '当前头像' }}</small>
        </div>

        <div v-if="sourceUrl" class="avatar-editor__controls">
          <span class="avatar-editor__label">画面调整</span>
          <div class="avatar-editor__control-row">
            <button type="button" title="缩小" aria-label="缩小" @click="zoom(-0.1)"><ZoomOut :size="18" /></button>
            <button type="button" title="放大" aria-label="放大" @click="zoom(0.1)"><ZoomIn :size="18" /></button>
            <button type="button" title="向左旋转" aria-label="向左旋转" @click="rotate(-90)"><RotateCcw :size="18" /></button>
            <button type="button" title="向右旋转" aria-label="向右旋转" @click="rotate(90)"><RotateCw :size="18" /></button>
            <button type="button" title="重置" aria-label="重置裁剪" @click="resetCrop"><RefreshCcw :size="18" /></button>
          </div>
        </div>

        <button class="button button--secondary avatar-editor__replace" type="button" :disabled="busy" @click="chooseFile">
          <Upload :size="16" />
          <span>{{ sourceUrl ? '换一张图片' : '上传新头像' }}</span>
        </button>

        <div v-if="avatarUrl" class="avatar-editor__delete-zone">
          <button
            v-if="!deleteArmed"
            class="avatar-editor__delete"
            type="button"
            :disabled="busy"
            @click="deleteAvatar"
          >
            <Trash2 :size="15" />
            <span>移除当前头像</span>
          </button>
          <div v-else class="avatar-editor__delete-confirm">
            <span>确认移除当前头像？</span>
            <div>
              <button type="button" :disabled="busy" @click="deleteArmed = false">取消</button>
              <button type="button" :disabled="busy" @click="deleteAvatar">确认移除</button>
            </div>
          </div>
        </div>
      </aside>
    </div>

    <template #footer>
      <button class="button button--ghost" type="button" :disabled="busy" @click="emit('update:open', false)">
        取消
      </button>
      <button class="button button--primary" type="button" :disabled="!sourceUrl || busy" @click="saveAvatar">
        <LoaderCircle v-if="busy" class="spin" :size="16" />
        <Check v-else :size="16" />
        <span>{{ busy ? '正在保存' : '保存头像' }}</span>
      </button>
    </template>
  </BaseDialog>
</template>

<style scoped>
.avatar-editor {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 210px;
  gap: 18px;
  min-height: 430px;
}

.avatar-editor__stage {
  position: relative;
  min-width: 0;
  min-height: 430px;
  overflow: hidden;
  border-radius: 10px;
  background:
    linear-gradient(45deg, rgb(255 255 255 / 3%) 25%, transparent 25%, transparent 75%, rgb(255 255 255 / 3%) 75%),
    linear-gradient(45deg, rgb(255 255 255 / 3%) 25%, transparent 25%, transparent 75%, rgb(255 255 255 / 3%) 75%),
    #111512;
  background-position: 0 0, 12px 12px;
  background-size: 24px 24px;
  box-shadow: inset 0 0 0 1px rgb(255 255 255 / 8%), 0 12px 34px rgb(0 0 0 / 14%);
  transition-property: box-shadow, scale;
  transition-duration: 180ms;
}

.avatar-editor__stage--dragging {
  scale: 0.995;
  box-shadow: inset 0 0 0 2px var(--brand), 0 12px 34px rgb(0 0 0 / 18%);
}

.avatar-editor__stage > img {
  display: block;
  max-width: 100%;
}

.avatar-editor__empty {
  display: grid;
  width: 100%;
  height: 100%;
  min-height: 430px;
  place-content: center;
  justify-items: center;
  gap: 14px;
  padding: 32px;
  border: 0;
  color: rgb(255 255 255 / 72%);
  background: transparent;
  cursor: pointer;
  text-align: center;
}

.avatar-editor__empty:active .avatar-editor__empty-icon {
  scale: 0.96;
}

.avatar-editor__empty-icon {
  display: inline-flex;
  width: 58px;
  height: 58px;
  align-items: center;
  justify-content: center;
  border-radius: 18px;
  color: #fff;
  background: rgb(255 255 255 / 10%);
  box-shadow: inset 0 0 0 1px rgb(255 255 255 / 10%);
  transition-property: scale, background-color;
  transition-duration: 180ms;
}

.avatar-editor__empty:hover .avatar-editor__empty-icon {
  background: rgb(255 255 255 / 16%);
}

.avatar-editor__empty strong,
.avatar-editor__empty small {
  display: block;
}

.avatar-editor__empty strong {
  color: #fff;
  font-size: 15px;
}

.avatar-editor__empty small {
  margin-top: 5px;
  color: rgb(255 255 255 / 54%);
  font-size: 12px;
}

.avatar-editor__empty em {
  color: rgb(255 255 255 / 34%);
  font-size: 10px;
  font-style: normal;
}

.avatar-editor__drop-hint {
  position: absolute;
  z-index: 5;
  inset: 12px;
  display: grid;
  place-content: center;
  justify-items: center;
  gap: 10px;
  border-radius: 8px;
  color: #fff;
  background: rgb(8 127 122 / 88%);
  pointer-events: none;
  backdrop-filter: blur(10px);
}

.avatar-editor__sidebar {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 14px;
}

.avatar-editor__preview-card {
  display: grid;
  justify-items: center;
  gap: 10px;
  padding: 16px;
  border-radius: 10px;
  background: var(--surface-subtle);
  box-shadow: var(--shadow-border);
}

.avatar-editor__label {
  justify-self: start;
  color: var(--ink-tertiary);
  font-size: 10px;
  font-weight: 700;
}

.avatar-cropper__preview-shell {
  display: flex;
  width: 112px;
  height: 112px;
  overflow: hidden;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  color: #fff;
  background: #3d4855;
  font-size: 34px;
  font-weight: 700;
  outline: 1px solid rgb(255 255 255 / 10%);
  outline-offset: -1px;
  box-shadow: 0 8px 28px rgb(0 0 0 / 20%);
}

:global(:root[data-theme='light']) .avatar-cropper__preview-shell {
  outline-color: rgb(0 0 0 / 10%);
}

.avatar-cropper__preview-shell img,
.avatar-cropper__preview {
  width: 100%;
  height: 100%;
}

.avatar-cropper__preview-shell img {
  object-fit: cover;
}

.avatar-editor__preview-card small {
  color: var(--ink-tertiary);
  font-size: 10px;
  text-align: center;
}

.avatar-editor__controls {
  display: grid;
  gap: 8px;
}

.avatar-editor__control-row {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 5px;
}

.avatar-editor__control-row button {
  display: inline-flex;
  min-width: 0;
  height: 40px;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 0;
  border-radius: 7px;
  color: var(--ink-secondary);
  background: var(--surface);
  box-shadow: var(--shadow-border);
  cursor: pointer;
  transition-property: color, background-color, box-shadow, scale;
  transition-duration: 150ms;
}

.avatar-editor__control-row button:hover {
  color: var(--ink);
  box-shadow: var(--shadow-hover);
}

.avatar-editor__control-row button:active {
  scale: 0.96;
}

.avatar-editor__replace {
  width: 100%;
}

.avatar-editor__delete-zone {
  margin-top: auto;
}

.avatar-editor__delete {
  display: inline-flex;
  width: 100%;
  min-height: 40px;
  align-items: center;
  justify-content: center;
  gap: 7px;
  padding: 0 12px;
  border: 0;
  border-radius: 7px;
  color: var(--danger);
  background: transparent;
  cursor: pointer;
  font-size: 11px;
  transition-property: background-color, scale;
  transition-duration: 150ms;
}

.avatar-editor__delete:hover {
  background: var(--danger-soft);
}

.avatar-editor__delete:active {
  scale: 0.96;
}

.avatar-editor__delete-confirm {
  display: grid;
  gap: 9px;
  padding: 11px;
  border-radius: 9px;
  color: var(--danger);
  background: var(--danger-soft);
  font-size: 11px;
}

.avatar-editor__delete-confirm > div {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
}

.avatar-editor__delete-confirm button {
  min-height: 40px;
  border: 0;
  border-radius: 6px;
  color: var(--ink-secondary);
  background: var(--surface);
  cursor: pointer;
}

.avatar-editor__delete-confirm button:last-child {
  color: #fff;
  background: var(--danger);
}

:deep(.cropper-container) {
  width: 100% !important;
  height: 430px !important;
}

:deep(.cropper-view-box),
:deep(.cropper-face) {
  border-radius: 50%;
}

:deep(.cropper-view-box) {
  outline: 2px solid rgb(255 255 255 / 92%);
  outline-color: rgb(255 255 255 / 92%);
  box-shadow: 0 0 0 9999px rgb(0 0 0 / 54%), 0 0 28px rgb(0 0 0 / 30%);
}

:deep(.cropper-line),
:deep(.cropper-point) {
  display: none;
}

@media (max-width: 720px) {
  .avatar-editor {
    grid-template-columns: 1fr;
    min-height: 0;
  }

  .avatar-editor__stage,
  .avatar-editor__empty {
    min-height: min(390px, 48dvh);
  }

  .avatar-editor__sidebar {
    display: grid;
    grid-template-columns: 122px minmax(0, 1fr);
    align-items: start;
  }

  .avatar-editor__preview-card {
    grid-row: span 3;
    padding: 12px 8px;
  }

  .avatar-cropper__preview-shell {
    width: 88px;
    height: 88px;
  }

  .avatar-editor__delete-zone {
    margin-top: 0;
  }

  :deep(.cropper-container) {
    height: min(390px, 48dvh) !important;
  }
}

@media (max-width: 460px) {
  .avatar-editor__sidebar {
    grid-template-columns: 1fr;
  }

  .avatar-editor__preview-card {
    grid-row: auto;
  }
}

@media (prefers-reduced-motion: reduce) {
  .avatar-editor__stage,
  .avatar-editor__empty-icon,
  .avatar-editor__control-row button,
  .avatar-editor__delete {
    transition-duration: 0.01ms;
  }
}
</style>
