<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import {
  Check,
  CircleAlert,
  CloudUpload,
  FileVideo2,
  Globe2,
  Link2,
  LoaderCircle,
  MonitorPlay,
  Upload,
} from 'lucide-vue-next'

import { api } from '@/lib/api'
import { useToastStore } from '@/stores/toast'
import type { PlatformBranding } from '@/types'

type SourceMode = 'url' | 'upload'

const MAX_VIDEO_BYTES = 300 * 1024 * 1024
const DEFAULT_VIDEO_URL = '/videos/login-background.mp4'

const toast = useToastStore()
const branding = ref<PlatformBranding | null>(null)
const mode = ref<SourceMode>('url')
const url = ref('')
const selectedFile = ref<File | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
const loading = ref(true)
const saving = ref(false)
const uploading = ref(false)
const dragging = ref(false)
const videoFailed = ref(false)
const saved = ref(false)
const successReplay = ref(0)
let savedTimer: ReturnType<typeof setTimeout> | null = null

const previewUrl = computed(() => branding.value?.login_background_video_url || DEFAULT_VIDEO_URL)
const sourceLabel = computed(() => {
  if (branding.value?.login_background_video_source === 'upload') return '平台上传文件'
  if (branding.value?.login_background_video_source === 'url') return '外部视频 URL'
  return '系统默认视频'
})
const selectedFileSize = computed(() => {
  if (!selectedFile.value) return ''
  return `${(selectedFile.value.size / 1024 / 1024).toFixed(1)} MB`
})

function showSaved(): void {
  saved.value = false
  successReplay.value += 1
  requestAnimationFrame(() => {
    saved.value = true
    if (savedTimer) clearTimeout(savedTimer)
    savedTimer = setTimeout(() => { saved.value = false }, 1800)
  })
}

function useBranding(value: PlatformBranding): void {
  branding.value = value
  videoFailed.value = false
  if (value.login_background_video_source === 'url') {
    mode.value = 'url'
    url.value = value.login_background_video_url
  } else if (value.login_background_video_source === 'upload') {
    mode.value = 'upload'
  }
}

async function loadBranding(): Promise<void> {
  loading.value = true
  try {
    useBranding(await api<PlatformBranding>('/admin/branding'))
  } catch (error) {
    toast.show('登录视觉配置加载失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  } finally {
    loading.value = false
  }
}

function setMode(value: SourceMode): void {
  mode.value = value
  selectedFile.value = null
  dragging.value = false
}

function validateFile(file: File): string | null {
  const suffix = file.name.toLowerCase().split('.').pop()
  if (!['video/mp4', 'video/webm'].includes(file.type) && !['mp4', 'webm'].includes(suffix || '')) return '仅支持 MP4 或 WebM 视频'
  if (file.size === 0) return '不能上传空视频文件'
  if (file.size > MAX_VIDEO_BYTES) return '登录背景视频不能超过 300 MB'
  return null
}

function selectFile(file: File | undefined): void {
  if (!file) return
  const error = validateFile(file)
  if (error) {
    toast.show('无法使用该视频', { message: error, tone: 'error' })
    return
  }
  selectedFile.value = file
}

function handleFileInput(event: Event): void {
  selectFile((event.target as HTMLInputElement).files?.[0])
}

function handleDrop(event: DragEvent): void {
  dragging.value = false
  selectFile(event.dataTransfer?.files?.[0])
}

async function saveUrl(): Promise<void> {
  const normalized = url.value.trim()
  try {
    const parsed = new URL(normalized)
    if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error()
  } catch {
    toast.show('视频地址无效', { message: '请输入完整的 HTTP 或 HTTPS 地址', tone: 'error' })
    return
  }
  saving.value = true
  try {
    useBranding(await api<PlatformBranding>('/admin/branding/login-background/url', {
      method: 'PUT',
      body: JSON.stringify({ url: normalized }),
    }))
    showSaved()
    toast.show('登录背景已更新', { message: '登录页将在下次打开时使用新视频', tone: 'success' })
  } catch (error) {
    toast.show('视频地址保存失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  } finally {
    saving.value = false
  }
}

async function uploadVideo(): Promise<void> {
  if (!selectedFile.value) {
    fileInput.value?.click()
    return
  }
  uploading.value = true
  try {
    const body = new FormData()
    body.set('file', selectedFile.value)
    useBranding(await api<PlatformBranding>('/admin/branding/login-background/upload', {
      method: 'POST',
      body,
    }))
    selectedFile.value = null
    if (fileInput.value) fileInput.value.value = ''
    showSaved()
    toast.show('背景视频上传完成', { message: '文件已保存并立即设为登录背景', tone: 'success' })
  } catch (error) {
    toast.show('背景视频上传失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  } finally {
    uploading.value = false
  }
}

onMounted(() => void loadBranding())
onBeforeUnmount(() => {
  if (savedTimer) clearTimeout(savedTimer)
})
</script>

<template>
  <section class="admin-section branding-console">
    <header class="section-heading branding-heading">
      <div>
        <h2>登录视觉</h2>
        <p>管理登录页与邀请注册页共用的全屏动态背景</p>
      </div>
      <span class="branding-live"><i></i>当前生效</span>
    </header>

    <div v-if="loading" class="branding-loading" aria-label="正在加载登录视觉配置">
      <LoaderCircle class="spin" :size="22" />
    </div>

    <div v-else class="branding-layout">
      <section class="branding-preview" aria-label="登录背景视频预览">
        <video
          :key="previewUrl"
          class="branding-preview__video"
          :src="previewUrl"
          autoplay
          muted
          loop
          playsinline
          preload="metadata"
          poster="/covers/login-studio-v2.webp"
          @error="videoFailed = true"
          @loadeddata="videoFailed = false"
        ></video>
        <div class="branding-preview__shade" aria-hidden="true"></div>
        <div class="branding-preview__identity">
          <span><MonitorPlay :size="20" /></span>
          <div><strong>登录页实时预览</strong><small>{{ sourceLabel }}</small></div>
        </div>
        <div v-if="videoFailed" class="branding-preview__error">
          <CircleAlert :size="19" />
          <span><strong>视频暂时无法播放</strong><small>请检查外链访问权限或重新上传文件</small></span>
        </div>
      </section>

      <section class="branding-editor">
        <div class="branding-editor__intro">
          <span><FileVideo2 :size="20" /></span>
          <div><h3>背景视频来源</h3><p>新配置保存后立即对所有未登录用户生效</p></div>
        </div>

        <div class="branding-source-tabs" role="tablist" aria-label="背景视频来源">
          <button type="button" role="tab" :aria-selected="mode === 'url'" @click="setMode('url')">
            <Globe2 :size="17" /><span>外部 URL</span>
          </button>
          <button type="button" role="tab" :aria-selected="mode === 'upload'" @click="setMode('upload')">
            <CloudUpload :size="17" /><span>上传文件</span>
          </button>
        </div>

        <div v-if="mode === 'url'" class="branding-source-panel" role="tabpanel">
          <label class="branding-url-field">
            <span>视频地址</span>
            <div><Link2 :size="18" /><input v-model="url" type="url" inputmode="url" placeholder="https://example.com/login-background.mp4" /></div>
          </label>
          <p class="branding-helper">支持可公开访问的 HTTP/HTTPS 视频地址，建议使用 MP4 H.264 编码以获得更广的设备兼容性。</p>
          <button class="button button--primary branding-submit" type="button" :disabled="saving || !url.trim()" @click="saveUrl">
            <LoaderCircle v-if="saving" class="spin" :size="17" />
            <Check v-else :size="17" />
            {{ saving ? '正在保存' : '保存并启用' }}
          </button>
        </div>

        <div v-else class="branding-source-panel" role="tabpanel">
          <input ref="fileInput" class="sr-only" type="file" accept="video/mp4,video/webm,.mp4,.webm" @change="handleFileInput" />
          <button
            class="branding-dropzone"
            :class="{ 'is-dragging': dragging, 'has-file': selectedFile }"
            type="button"
            @click="fileInput?.click()"
            @dragenter.prevent="dragging = true"
            @dragover.prevent="dragging = true"
            @dragleave.prevent="dragging = false"
            @drop.prevent="handleDrop"
          >
            <span><FileVideo2 v-if="selectedFile" :size="23" /><Upload v-else :size="23" /></span>
            <strong>{{ selectedFile?.name || '选择或拖入视频文件' }}</strong>
            <small>{{ selectedFile ? selectedFileSize : 'MP4 / WebM，最大 300 MB' }}</small>
          </button>
          <button class="button button--primary branding-submit" type="button" :disabled="uploading || !selectedFile" @click="uploadVideo">
            <LoaderCircle v-if="uploading" class="spin" :size="17" />
            <CloudUpload v-else :size="17" />
            {{ uploading ? '正在上传并保存' : '上传并启用' }}
          </button>
        </div>

        <Transition name="branding-saved">
          <div v-if="saved" :key="successReplay" class="branding-save-state" role="status">
            <span class="t-success-check" data-state="in" aria-hidden="true">
              <svg width="22" height="22" viewBox="0 0 48 48" fill="none"><path d="M13 25L21 33L36 16" stroke="currentColor" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" /></svg>
            </span>
            新背景已生效
          </div>
        </Transition>
      </section>
    </div>
  </section>
</template>

<style scoped>
.branding-console { display: grid; min-width: 0; gap: 16px; }
.branding-heading { align-items: center; }
.branding-live { display: inline-flex; min-height: 36px; align-items: center; gap: 7px; padding: 0 12px; border-radius: 18px; color: var(--success); background: var(--success-soft); box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--success) 18%, transparent); font-size: 10px; font-weight: 750; }
.branding-live i { width: 7px; height: 7px; border-radius: 50%; background: currentColor; box-shadow: 0 0 0 4px color-mix(in srgb, var(--success) 14%, transparent); }
.branding-loading { display: grid; min-height: 420px; place-items: center; color: var(--brand); }
.branding-layout { display: grid; min-width: 0; grid-template-columns: minmax(0, 1.48fr) minmax(330px, .72fr); gap: 16px; }
.branding-preview { position: relative; min-width: 0; min-height: 520px; overflow: hidden; border-radius: 8px; background: #0d1512; box-shadow: var(--shadow-border), 0 18px 48px rgb(0 0 0 / 16%); }
.branding-preview__video { width: 100%; height: 100%; object-fit: cover; outline: 1px solid rgb(0 0 0 / 10%); outline-offset: -1px; }
.branding-preview__shade { position: absolute; inset: 0; pointer-events: none; background: linear-gradient(180deg, rgb(6 15 12 / 8%) 42%, rgb(5 12 10 / 76%) 100%); }
.branding-preview__identity { position: absolute; right: 18px; bottom: 18px; left: 18px; display: flex; min-width: 0; align-items: center; gap: 10px; color: #fff; }
.branding-preview__identity>span { display: inline-flex; width: 42px; height: 42px; flex: 0 0 auto; align-items: center; justify-content: center; border-radius: 7px; background: rgb(255 255 255 / 13%); box-shadow: inset 0 0 0 1px rgb(255 255 255 / 14%); backdrop-filter: blur(14px); }
.branding-preview__identity strong,.branding-preview__identity small { display: block; }
.branding-preview__identity strong { font-size: 13px; }
.branding-preview__identity small { margin-top: 3px; color: rgb(255 255 255 / 66%); font-size: 10px; }
.branding-preview__error { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; gap: 10px; padding: 24px; color: #ffe6df; background: rgb(29 12 8 / 72%); backdrop-filter: blur(10px); }
.branding-preview__error strong,.branding-preview__error small { display: block; }
.branding-preview__error small { margin-top: 4px; color: rgb(255 230 223 / 68%); }
.branding-editor { position: relative; display: flex; min-width: 0; flex-direction: column; gap: 14px; padding: 18px; border-radius: 8px; background: var(--surface); box-shadow: var(--shadow-border); }
.branding-editor__intro { display: flex; align-items: center; gap: 10px; }
.branding-editor__intro>span { display: inline-flex; width: 42px; height: 42px; flex: 0 0 auto; align-items: center; justify-content: center; border-radius: 7px; color: var(--brand); background: var(--brand-soft); }
.branding-editor__intro h3 { color: var(--ink); font-size: 14px; }
.branding-editor__intro p { margin-top: 3px; color: var(--ink-tertiary); font-size: 10px; }
.branding-source-tabs { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 4px; padding: 4px; border-radius: 10px; background: var(--surface-strong); }
.branding-source-tabs button { display: inline-flex; min-height: 42px; align-items: center; justify-content: center; gap: 7px; border: 0; border-radius: 6px; color: var(--ink-tertiary); background: transparent; cursor: pointer; transition-property: color, background-color, box-shadow, scale; transition-duration: var(--duration-fast); transition-timing-function: var(--ease-smooth-out); }
.branding-source-tabs button:active { scale: .96; }
.branding-source-tabs button[aria-selected='true'] { color: var(--ink); background: var(--surface); box-shadow: var(--shadow-border); }
.branding-source-panel { display: flex; flex: 1; min-width: 0; flex-direction: column; gap: 12px; }
.branding-url-field { display: grid; gap: 7px; }
.branding-url-field>span { color: var(--ink-secondary); font-size: 10px; font-weight: 700; }
.branding-url-field>div { display: flex; min-height: 48px; min-width: 0; align-items: center; gap: 9px; padding: 0 12px; border-radius: 7px; color: var(--ink-tertiary); background: var(--surface-subtle); box-shadow: inset 0 0 0 1px var(--line); transition-property: color, background-color, box-shadow; transition-duration: var(--duration-fast); }
.branding-url-field>div:focus-within { color: var(--brand); background: var(--surface); box-shadow: inset 0 0 0 1px var(--brand), 0 0 0 3px color-mix(in srgb, var(--brand) 12%, transparent); }
.branding-url-field input { min-width: 0; min-height: 0; flex: 1; padding: 0; border: 0; outline: 0; color: var(--ink); background: transparent; box-shadow: none; }
.branding-helper { color: var(--ink-tertiary); font-size: 10px; line-height: 1.65; }
.branding-dropzone { display: flex; min-height: 178px; flex: 1; align-items: center; justify-content: center; flex-direction: column; gap: 8px; padding: 18px; border: 0; border-radius: 8px; color: var(--ink-secondary); background: var(--surface-subtle); box-shadow: inset 0 0 0 1px var(--line); cursor: pointer; transition-property: color, background-color, box-shadow, scale; transition-duration: var(--duration-fast); transition-timing-function: var(--ease-smooth-out); }
.branding-dropzone:hover,.branding-dropzone.is-dragging { color: var(--brand); background: var(--brand-soft); box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--brand) 50%, transparent); }
.branding-dropzone:active { scale: .96; }
.branding-dropzone>span { display: inline-flex; width: 48px; height: 48px; align-items: center; justify-content: center; border-radius: 8px; color: var(--brand); background: var(--surface); box-shadow: var(--shadow-border); }
.branding-dropzone strong { max-width: 100%; overflow: hidden; color: var(--ink); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.branding-dropzone small { color: var(--ink-tertiary); font-size: 10px; font-variant-numeric: tabular-nums; }
.branding-submit { min-height: 44px; margin-top: auto; }
.branding-save-state { position: absolute; right: 18px; bottom: 74px; display: inline-flex; min-height: 40px; align-items: center; gap: 7px; padding: 0 12px; border-radius: 7px; color: var(--success); background: var(--success-soft); box-shadow: var(--shadow-border); font-size: 10px; font-weight: 750; }
.branding-saved-enter-active { transition-property: opacity, transform, filter; transition-duration: var(--duration-fast); transition-timing-function: var(--ease-smooth-out); }
.branding-saved-leave-active { transition-property: opacity, transform; transition-duration: var(--duration-quick); transition-timing-function: ease-in; }
.branding-saved-enter-from { opacity: 0; transform: translateY(8px); filter: blur(3px); }
.branding-saved-leave-to { opacity: 0; transform: translateY(-6px); }
:global(:root[data-theme='dark']) .branding-preview__video { outline-color: rgb(255 255 255 / 10%); }
@media (max-width: 980px) { .branding-layout { grid-template-columns: minmax(0, 1fr); } .branding-preview { min-height: min(54vw, 460px); } }
@media (max-width: 620px) { .branding-heading { align-items: flex-start; } .branding-live { min-height: 32px; } .branding-preview { min-height: 260px; } .branding-preview__identity { right: 12px; bottom: 12px; left: 12px; } .branding-editor { padding: 14px; } .branding-save-state { right: 14px; bottom: 70px; } }
@media (prefers-reduced-motion: reduce) { .branding-source-tabs button,.branding-url-field>div,.branding-dropzone,.branding-saved-enter-active,.branding-saved-leave-active { transition-duration: .01ms !important; } }
</style>
