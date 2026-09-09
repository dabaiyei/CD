<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import {
  BookOpenText,
  Boxes,
  Camera,
  Check,
  Clapperboard,
  Download,
  FileText,
  Film,
  History,
  Image,
  Layers3,
  LockKeyhole,
  LoaderCircle,
  Play,
  Sparkles,
  Square,
  WandSparkles,
} from 'lucide-vue-next'

import BaseDialog from '@/components/BaseDialog.vue'
import VideoConcatButton from '@/components/VideoConcatButton.vue'
import { apiBlob } from '@/lib/api'
import { mediaPreview } from '@/lib/mediaPreview'
import { useToastStore } from '@/stores/toast'
import type { AssetItem, Chapter, DirectorWorkflowDetail, ScriptVersion, StoryboardShot, StoryboardVersionDetail, VideoClip } from '@/types'

const props = defineProps<{
  chapter: Chapter
  scripts: ScriptVersion[]
  assets: AssetItem[]
  storyboard: StoryboardVersionDetail | null
  videoResolution?: string | null
  aspectRatio?: string | null
  busyShotIds?: string[]
  busyVideoPromptShotIds?: string[]
  videoAction?: 'video' | 'batchVideo' | 'videoPrompt' | ''
  videoPromptTaskActive?: boolean
  automaticWorkflow?: DirectorWorkflowDetail | null
  automationAction?: 'start' | 'stop' | ''
  automationLocked?: boolean
}>()
const emit = defineEmits<{
  openAssets: []
  queueVideoPrompts: [shotIds: string[]]
  queueBatchVideos: [shotIds: string[]]
  queueShotVideo: [shot: StoryboardShot]
  startAutomation: []
  stopAutomation: []
}>()
const tab = ref<'source' | 'script' | 'assets' | 'storyboard' | 'video'>('source')
const selectedVideoShotId = ref('')
const selectedVideoShotIds = ref<string[]>([])
const playingVideoUrl = ref('')
const videoPage = ref(0)
const videoPageSize = 12
const videoShotGrid = ref<HTMLElement | null>(null)
watch(videoPage, async () => { await nextTick(); videoShotGrid.value?.scrollTo({ left: 0 }) })
const videoPageCount = computed(() => Math.max(1, Math.ceil(storyboardShots.value.length / videoPageSize)))
const visibleVideoShots = computed(() => storyboardShots.value.slice(videoPage.value * videoPageSize, (videoPage.value + 1) * videoPageSize))
const downloadingVideos = ref(false)
const automationConfirmOpen = ref(false)
const toast = useToastStore()
const automationStageOrder = [
  'script_adapting',
  'script_reviewing',
  'script_repairing',
  'asset_extracting',
  'ready_for_asset_images',
  'asset_preparing',
  'storyboard_generating',
  'storyboard_reviewing',
  'storyboard_repairing',
  'video_prompt_generating',
  'video_generating',
  'ready_for_video',
] as const
const automationStageLabels: Record<string, string> = {
  script_adapting: '正在改编剧本',
  script_reviewing: '正在审核剧本',
  awaiting_script_decision: '正在选择剧本修复路线',
  script_repairing: '正在修复剧本',
  asset_extracting: '正在提取资产',
  ready_for_asset_images: '正在准备资产图片',
  asset_preparing: '正在生成资产提示词与图片',
  storyboard_generating: '正在制作分镜',
  storyboard_reviewing: '正在审核分镜',
  awaiting_storyboard_decision: '正在选择分镜修复路线',
  storyboard_repairing: '正在修复分镜',
  video_prompt_generating: '正在生成视频提示词',
  video_generating: '正在生成镜头视频',
  ready_for_video: '视频生成完成',
  failed: '全自动制作失败',
  cancelled: '全自动制作已停止',
}

const storyboardShots = computed(() => props.storyboard?.shots ?? [])
const storyboardClips = computed(() => props.storyboard?.video_clips ?? [])
const activeScript = computed(() => props.scripts.find((item) => item.is_active) ?? props.scripts[0] ?? null)
const readyAssets = computed(() => props.assets.filter((item) => Boolean(item.media_url)))
const assetsById = computed(() => new Map(props.assets.map((asset) => [asset.id, asset])))
const shotAssetsById = computed(() => new Map(
  storyboardShots.value.map((shot) => [
    shot.id,
    shot.asset_ids.flatMap((assetId) => {
      const asset = assetsById.value.get(assetId)
      return asset ? [asset] : []
    }),
  ]),
))
const shotFallbackImages = computed(() => new Map(
  storyboardShots.value.map((shot) => [
    shot.id,
    shot.reference_image_url
      || shotAssetsById.value.get(shot.id)?.find((asset) => Boolean(asset.media_url))?.media_url
      || '',
  ]),
))
const busyShotIdSet = computed(() => new Set(props.busyShotIds ?? []))
const busyVideoPromptShotIdSet = computed(() => new Set(props.busyVideoPromptShotIds ?? []))
const clipsByShot = computed(() => {
  const rows = new Map<string, VideoClip[]>()
  storyboardClips.value.forEach((clip) => {
    const list = rows.get(clip.shot_id) ?? []
    list.push(clip)
    rows.set(clip.shot_id, list)
  })
  rows.forEach((list) => list.sort((a, b) => b.version - a.version))
  return rows
})
const activeClips = computed(() => {
  const rows = new Map<string, VideoClip>()
  storyboardClips.value.forEach((clip) => {
    const current = rows.get(clip.shot_id)
    if (
      clip.is_active
      || (!current && ['queued', 'generating', 'failed'].includes(clip.status))
      || (current && !current.is_active && clip.version > current.version)
    ) {
      rows.set(clip.shot_id, clip)
    }
  })
  return rows
})
const readyVideoCount = computed(() => [...activeClips.value.values()].filter((clip) => clip.is_active && clip.status === 'ready').length)
const shotsWithPromptCount = computed(() => storyboardShots.value.filter((shot) => Boolean(shot.video_prompt.trim())).length)
const selectedVideoShot = computed(() => (
  storyboardShots.value.find((shot) => shot.id === selectedVideoShotId.value)
  ?? storyboardShots.value[0]
  ?? null
))
const selectedVideoShotAssets = computed(() => (
  selectedVideoShot.value ? shotAssets(selectedVideoShot.value) : []
))
const allVideoShotsSelected = computed(() => Boolean(storyboardShots.value.length)
  && storyboardShots.value.every((shot) => selectedVideoShotIds.value.includes(shot.id)))
const selectedVideoShots = computed(() => {
  const shots = storyboardShots.value
  return selectedVideoShotIds.value.length
    ? shots.filter((shot) => selectedVideoShotIds.value.includes(shot.id))
    : shots
})
const videoPromptEligibleShots = computed(() => selectedVideoShots.value.filter(
  (shot) => !busyVideoPromptShotIdSet.value.has(shot.id) && !busyShotIdSet.value.has(shot.id),
))
const selectedVideoUrl = computed(() => activeClips.value.get(selectedVideoShot.value?.id || '')?.media_url || '')
watch([selectedVideoUrl, tab], () => { playingVideoUrl.value = '' })
watch(() => props.storyboard?.version.id, () => { videoPage.value = 0 })
watch(videoPageCount, (count) => { videoPage.value = Math.min(videoPage.value, count - 1) })
const videoEligibleShots = computed(() => selectedVideoShots.value.filter(
  (shot) => !busyShotIdSet.value.has(shot.id)
    && !busyVideoPromptShotIdSet.value.has(shot.id)
    && Boolean(shot.video_prompt.trim())
    && !(activeClips.value.get(shot.id)?.is_active && activeClips.value.get(shot.id)?.status === 'ready'),
))
const downloadableVideoClips = computed(() => storyboardShots.value
  .map((shot) => activeClips.value.get(shot.id))
  .filter((clip): clip is VideoClip => Boolean(clip?.is_active && clip.status === 'ready' && clip.media_url)))
const automaticWorkflowState = computed(() => props.automaticWorkflow?.workflow ?? null)
const automationVisible = computed(() => Boolean(automaticWorkflowState.value?.automation_mode))
const automationRunning = computed(() => Boolean(
  automationVisible.value
  && ['running', 'waiting_user'].includes(automaticWorkflowState.value?.status ?? ''),
))
const automationStageLabel = computed(() => {
  const workflow = automaticWorkflowState.value
  if (!workflow) return 'AI 全自动制作'
  if (workflow.status === 'completed') return '本章全自动制作完成'
  return automationStageLabels[workflow.stage] ?? workflow.last_message ?? '正在推进本章制作'
})
const automationProgress = computed(() => {
  const workflow = automaticWorkflowState.value
  if (!workflow) return 0
  if (workflow.status === 'completed') return 100
  const stageIndex = automationStageOrder.indexOf(workflow.stage as typeof automationStageOrder[number])
  return stageIndex < 0 ? 4 : Math.max(4, Math.round(((stageIndex + 0.35) / automationStageOrder.length) * 100))
})
const automationCompletedChildren = computed(() => (
  props.automaticWorkflow?.child_runs.filter((child) => child.status === 'succeeded').length ?? 0
))
const chapterStatusText = computed(() => ({
  uninitialized: '尚未创作',
  analyzing: '正在分析',
  analyzed: '分析完成',
  scripting: '正在改编',
  reviewing: '正在审核',
  assets: '资产阶段',
  storyboard: '分镜阶段',
  video: '视频阶段',
  completed: '已完成',
}[props.chapter.status]))
const emptyStageContent = computed(() => ({
  source: {
    title: '章节原文',
    description: '原始内容会显示在这里。',
  },
  script: {
    title: '还没有剧本版本',
    description: '可在右侧与导演 Agent 协作，生成或整理本章剧本。',
  },
  assets: {
    title: '还没有本章资产',
    description: '完成剧本后可提取人物、场景和道具资产。',
  },
  storyboard: {
    title: '还没有分镜版本',
    description: '分镜会基于当前剧本和已定稿资产建立。',
  },
  video: {
    title: '还没有可生产的镜头',
    description: '建立分镜后可在这里编写提示词并生成镜头视频。',
  },
}[tab.value]))

function shotAssets(shot: StoryboardShot): AssetItem[] {
  return shotAssetsById.value.get(shot.id) ?? []
}

function shotFallbackImage(shot: StoryboardShot): string {
  return shotFallbackImages.value.get(shot.id) ?? ''
}

function assetTypeText(asset: AssetItem): string {
  return {
    character: '人物',
    scene: '场景',
    prop: '道具',
    material: '素材',
    audio: '音频',
  }[asset.asset_type]
}

function clipStatusText(clip?: VideoClip): string {
  if (!clip) return '待生成'
  return {
    queued: '排队中',
    generating: '生成中',
    ready: '已完成',
    failed: '失败',
    cancelled: '已取消',
  }[clip.status]
}

function shotVideoStatus(shot: StoryboardShot): string {
  if (busyShotIdSet.value.has(shot.id)) return '视频中'
  if (busyVideoPromptShotIdSet.value.has(shot.id)) return '提示词中'
  const clip = activeClips.value.get(shot.id)
  if (clip) return clipStatusText(clip)
  return shot.video_prompt ? '待生成' : '缺提示词'
}

function selectVideoShot(shot: StoryboardShot): void {
  selectedVideoShotId.value = shot.id
}

function toggleVideoShotSelection(shotId: string): void {
  selectedVideoShotIds.value = selectedVideoShotIds.value.includes(shotId)
    ? selectedVideoShotIds.value.filter((id) => id !== shotId)
    : [...selectedVideoShotIds.value, shotId]
}

function toggleAllVideoShots(): void {
  const ids = storyboardShots.value.map((shot) => shot.id)
  selectedVideoShotIds.value = allVideoShotsSelected.value ? [] : ids
}

function queueVideoPrompts(): void {
  if (!props.storyboard) return
  emit('queueVideoPrompts', videoPromptEligibleShots.value.map((shot) => shot.id))
}

function queueSelectedVideoPrompt(): void {
  if (!props.storyboard || !selectedVideoShot.value) return
  emit('queueVideoPrompts', [selectedVideoShot.value.id])
}

function queueBatchVideos(): void {
  if (!props.storyboard) return
  emit('queueBatchVideos', videoEligibleShots.value.map((shot) => shot.id))
}

function openAutomationConfirmation(): void {
  if (automationRunning.value || props.automationAction) return
  automationConfirmOpen.value = true
}

function confirmAutomationStart(): void {
  if (props.automationAction) return
  automationConfirmOpen.value = false
  emit('startAutomation')
}

async function downloadReadyVideos(): Promise<void> {
  if (!props.storyboard || downloadingVideos.value || !downloadableVideoClips.value.length) return
  downloadingVideos.value = true
  try {
    const result = await apiBlob(
      `/projects/${props.chapter.project_id}/chapters/${props.chapter.id}/storyboards/${props.storyboard.version.id}/videos/download`,
    )
    const url = URL.createObjectURL(result.blob)
    const link = document.createElement('a')
    link.href = url
    link.download = result.filename || `${props.chapter.title}-镜头视频.zip`
    document.body.appendChild(link)
    link.click()
    link.remove()
    window.setTimeout(() => URL.revokeObjectURL(url), 0)
    toast.show('镜头视频压缩包已开始下载', {
      message: `包含 ${downloadableVideoClips.value.length} 个已完成镜头`,
      tone: 'success',
    })
  } catch (error) {
    toast.show('视频打包下载失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  } finally {
    downloadingVideos.value = false
  }
}

watch(
  () => props.chapter.id,
  () => {
    tab.value = activeScript.value ? 'script' : 'source'
    selectedVideoShotId.value = ''
    selectedVideoShotIds.value = []
  },
  { immediate: true },
)

watch(
  () => props.storyboard?.version.id,
  () => {
    const shots = storyboardShots.value
    selectedVideoShotId.value = shots[0]?.id ?? ''
    selectedVideoShotIds.value = []
  },
  { immediate: true },
)
</script>

<template>
  <main class="director-canvas">
    <header class="director-canvas__header">
      <div>
        <span>{{ chapter.source_mode === 'novel' ? '小说章节' : '剧本章节' }}</span>
        <h2>{{ chapter.title }}</h2>
      </div>
      <div class="director-canvas__meta">
        <span class="chapter-status" :data-status="chapter.status"><i></i>{{ chapterStatusText }}</span>
        <small><Check :size="13" />AI 自动编排</small>
        <button
          v-if="!automationRunning"
          class="director-automation-trigger"
          type="button"
          :disabled="Boolean(automationAction)"
          @click="openAutomationConfirmation"
        >
          <LoaderCircle v-if="automationAction === 'start'" class="spin" :size="15" />
          <Sparkles v-else :size="15" />
          {{ automationAction === 'start' ? '正在启动' : 'AI 全自动' }}
        </button>
      </div>
    </header>

    <section
      v-if="automationVisible"
      class="director-automation-bar"
      :data-status="automaticWorkflowState?.status"
      role="status"
      aria-live="polite"
    >
      <span class="director-automation-bar__icon">
        <LoaderCircle v-if="automationRunning" class="spin" :size="17" />
        <Check v-else-if="automaticWorkflowState?.status === 'completed'" :size="17" />
        <Square v-else :size="16" />
      </span>
      <div class="director-automation-bar__copy">
        <span><strong>{{ automationStageLabel }}</strong><small class="tabular-nums">{{ automationCompletedChildren }} / {{ automaticWorkflow?.child_runs.length || 0 }} 子任务</small></span>
        <p>{{ automaticWorkflowState?.last_error || automaticWorkflowState?.last_message || '系统会自动完成剧本、资产、分镜和镜头视频。' }}</p>
        <i aria-hidden="true"><span :style="{ width: `${automationProgress}%` }"></span></i>
      </div>
      <span v-if="automationRunning" class="director-automation-bar__lock"><LockKeyhole :size="13" />章节已锁定</span>
      <button v-if="automationRunning" type="button" :disabled="automationAction === 'stop'" @click="emit('stopAutomation')">
        <LoaderCircle v-if="automationAction === 'stop'" class="spin" :size="14" />
        <Square v-else :size="13" fill="currentColor" />
        {{ automationAction === 'stop' ? '停止中' : '停止' }}
      </button>
    </section>

    <nav class="director-canvas__tabs" aria-label="章节产物">
      <button type="button" :aria-pressed="tab === 'source'" @click="tab = 'source'"><BookOpenText :size="15" />原文</button>
      <button type="button" :aria-pressed="tab === 'script'" @click="tab = 'script'"><FileText :size="15" />剧本<span class="tabular-nums">{{ scripts.length }}</span></button>
      <button type="button" :aria-pressed="tab === 'assets'" @click="tab = 'assets'"><Boxes :size="15" />资产<span class="tabular-nums">{{ assets.length }}</span></button>
      <button type="button" :aria-pressed="tab === 'storyboard'" @click="tab = 'storyboard'"><Layers3 :size="15" />分镜<span class="tabular-nums">{{ storyboardShots.length }}</span></button>
      <button type="button" :aria-pressed="tab === 'video'" @click="tab = 'video'"><Film :size="15" />生成视频<span class="tabular-nums">{{ readyVideoCount }}</span></button>
    </nav>

    <section v-if="tab === 'source'" class="director-source-document">
      <header><BookOpenText :size="16" /><strong>章节原文</strong><span class="tabular-nums">{{ chapter.original_content.length.toLocaleString('zh-CN') }} 字</span></header>
      <article><p>{{ chapter.original_content }}</p></article>
    </section>

    <section v-else-if="tab === 'script' && activeScript" class="director-script-document">
      <header>
        <div><span><Clapperboard :size="16" /></span><div><small>当前生效剧本 · v{{ activeScript.version }}</small><strong>{{ activeScript.title }}</strong></div></div>
        <span :data-status="activeScript.status"><Check :size="13" />{{ activeScript.status === 'approved' ? '审核通过' : '审核中' }}</span>
      </header>
      <article><p>{{ activeScript.content }}</p></article>
      <footer v-if="activeScript.review_notes"><strong>版本说明</strong><p>{{ activeScript.review_notes }}</p></footer>
    </section>

    <section v-else-if="tab === 'assets' && assets.length" class="director-asset-board">
      <header><div><strong>本章塑造资产</strong><span>{{ readyAssets.length }} / {{ assets.length }} 已定稿</span></div><button type="button" :disabled="automationLocked" @click="emit('openAssets')"><LockKeyhole v-if="automationLocked" :size="15" /><Boxes v-else :size="15" />{{ automationLocked ? '全自动运行中' : '管理资产' }}</button></header>
      <div>
        <article v-for="asset in assets" :key="asset.id">
          <div><img v-if="asset.media_url" :src="asset.media_url" :alt="asset.name" /><span v-else><Image :size="22" /></span><i :data-ready="Boolean(asset.media_url)">{{ asset.media_url ? '已定稿' : asset.generation_prompt ? '待生图' : '待提示词' }}</i></div>
          <strong>{{ asset.name }}</strong><small>{{ asset.asset_type === 'character' ? '人物' : asset.asset_type === 'scene' ? '场景' : asset.asset_type === 'prop' ? '道具' : '素材' }}</small>
        </article>
      </div>
    </section>

    <section v-else-if="tab === 'storyboard' && storyboard" class="director-storyboard-board">
      <header><div><strong>导演分镜 v{{ storyboard.version.version }}</strong><span>{{ storyboardShots.length }} 个镜头</span></div><span><Sparkles :size="14" />已通过资产校验</span></header>
      <div>
        <article v-for="shot in storyboardShots" :key="shot.id">
          <div><img v-if="shot.reference_image_url" :src="shot.reference_image_url" :alt="shot.title" /><span v-else><Camera :size="24" /></span><b class="tabular-nums">{{ String(shot.order_index).padStart(2, '0') }}</b></div>
          <header><strong>{{ shot.title }}</strong><small>{{ shot.shot_type }} · {{ shot.duration_seconds }}s</small></header>
          <p>{{ shot.action_description || shot.scene_description }}</p>
        </article>
      </div>
    </section>

    <section v-else-if="tab === 'video' && storyboardShots.length" class="director-video-board">
      <header>
        <div>
          <strong>镜头视频生产</strong>
          <span><b class="tabular-nums">{{ readyVideoCount }}</b> / {{ storyboardShots.length }} 已完成 · <b class="tabular-nums">{{ shotsWithPromptCount }}</b> 个镜头已有提示词</span>
        </div>
        <div class="director-video-board__actions">
          <button type="button" class="button button--secondary" :disabled="!downloadableVideoClips.length || downloadingVideos" @click="downloadReadyVideos"><LoaderCircle v-if="downloadingVideos" class="spin" :size="15" /><Download v-else :size="15" />{{ downloadingVideos ? '正在打包' : '下载已完成' }}</button>
          <VideoConcatButton v-if="storyboard" :project-id="chapter.project_id" :chapter-id="chapter.id" :storyboard-id="storyboard.version.id" :count="downloadableVideoClips.length" />
          <button type="button" class="button button--secondary" :disabled="automationLocked || !videoPromptEligibleShots.length || videoAction === 'videoPrompt' || videoPromptTaskActive" @click="queueVideoPrompts">
            <LoaderCircle v-if="videoAction === 'videoPrompt' || videoPromptTaskActive" class="spin" :size="15" />
            <WandSparkles v-else :size="15" />
            批量提示词
          </button>
          <button type="button" class="button button--primary" :disabled="automationLocked || !videoEligibleShots.length || Boolean(videoAction)" @click="queueBatchVideos">
            <LoaderCircle v-if="videoAction === 'batchVideo'" class="spin" :size="15" />
            <Play v-else :size="15" />
            批量生成视频
          </button>
        </div>
      </header>

      <section v-if="selectedVideoShot" class="director-video-focus">
        <div class="director-video-focus__preview">
          <video v-if="playingVideoUrl && playingVideoUrl === selectedVideoUrl" :key="playingVideoUrl" :src="playingVideoUrl" controls playsinline autoplay preload="none"></video>
          <img v-else-if="shotFallbackImage(selectedVideoShot)" :src="mediaPreview(shotFallbackImage(selectedVideoShot), 640)" :alt="selectedVideoShot.title" decoding="async" />
          <span v-else><Camera :size="28" />等待参考图</span>
          <button v-if="selectedVideoUrl && !playingVideoUrl" type="button" class="director-video-play-trigger" @click="playingVideoUrl = selectedVideoUrl"><Play :size="24" />播放视频</button>
          <i :data-status="activeClips.get(selectedVideoShot.id)?.status || 'idle'">{{ shotVideoStatus(selectedVideoShot) }}</i>
        </div>
        <div class="director-video-focus__detail">
          <header>
            <div>
              <small class="tabular-nums">SHOT {{ String(selectedVideoShot.order_index).padStart(2, '0') }} · {{ selectedVideoShot.duration_seconds }}s</small>
              <strong>{{ selectedVideoShot.title }}</strong>
            </div>
            <div class="director-video-focus__buttons">
              <button type="button" class="button button--secondary" :disabled="automationLocked || busyVideoPromptShotIdSet.has(selectedVideoShot.id) || busyShotIdSet.has(selectedVideoShot.id) || videoAction === 'videoPrompt' || videoPromptTaskActive" @click="queueSelectedVideoPrompt">
                <LoaderCircle v-if="busyVideoPromptShotIdSet.has(selectedVideoShot.id) || videoAction === 'videoPrompt'" class="spin" :size="15" />
                <WandSparkles v-else :size="15" />
                {{ selectedVideoShot.video_prompt ? '重写提示词' : '生成提示词' }}
              </button>
              <button type="button" class="button button--primary" :disabled="automationLocked || !selectedVideoShot.video_prompt || busyShotIdSet.has(selectedVideoShot.id) || busyVideoPromptShotIdSet.has(selectedVideoShot.id) || videoAction === 'video'" @click="emit('queueShotVideo', selectedVideoShot)">
                <LoaderCircle v-if="busyShotIdSet.has(selectedVideoShot.id)" class="spin" :size="15" />
                <Play v-else :size="15" />
                {{ activeClips.get(selectedVideoShot.id)?.is_active ? '重新生成' : '生成本镜头' }}
              </button>
            </div>
          </header>
          <div class="director-video-source-grid">
            <section>
              <small>镜头内容</small>
              <p>{{ selectedVideoShot.scene_description || '暂无场景描述' }}</p>
              <p>{{ selectedVideoShot.action_description || '暂无动作描述' }}</p>
              <blockquote v-if="selectedVideoShot.dialogue">{{ selectedVideoShot.dialogue }}</blockquote>
            </section>
            <section>
              <small>首帧提示词</small>
              <p>{{ selectedVideoShot.image_prompt || '暂无首帧提示词' }}</p>
            </section>
          </div>
          <section class="director-video-references">
            <header>
              <small>资产图片参考</small>
              <span class="tabular-nums">{{ selectedVideoShotAssets.filter((asset) => asset.media_url).length }} / {{ selectedVideoShotAssets.length }} 可用</span>
            </header>
            <div v-if="selectedVideoShotAssets.length">
              <article v-for="asset in selectedVideoShotAssets" :key="asset.id">
                <span><img v-if="asset.media_url" :src="mediaPreview(asset.media_url)" :alt="asset.name" loading="lazy" decoding="async" /><Image v-else :size="17" /></span>
                <div><strong>{{ asset.name }}</strong><small>{{ assetTypeText(asset) }} · {{ asset.media_url ? '已作为参考图' : '缺少图片，生成视频前建议先定稿' }}</small></div>
              </article>
            </div>
            <p v-else>当前镜头没有绑定资产；会按文生视频模式生成提示词。</p>
          </section>
          <section class="director-video-final-prompt">
            <header>
              <small>最终视频提示词</small>
              <span>{{ selectedVideoShot.video_prompt ? '已写入镜头，可直接生成视频' : '尚未生成' }}</span>
            </header>
            <p>{{ selectedVideoShot.video_prompt || '点击“生成提示词”后，系统会调用管理员配置的视频提示词生成 skill，读取当前镜头内容、首帧、资产图片参考、项目视觉/导演手册和目标视频模型能力，生成最终可执行提示词。' }}</p>
          </section>
          <footer>
            <span><Film :size="14" />{{ videoResolution || '默认分辨率' }}</span>
            <span><Camera :size="14" />{{ aspectRatio || '默认比例' }}</span>
            <span><History :size="14" />{{ clipsByShot.get(selectedVideoShot.id)?.length || 0 }} 个历史版本</span>
          </footer>
        </div>
      </section>

      <div ref="videoShotGrid" class="director-video-shot-grid">
        <article
          v-for="shot in visibleVideoShots"
          :key="shot.id"
          v-memo="[shot.version, shotFallbackImage(shot), shot.video_prompt, selectedVideoShot?.id === shot.id, selectedVideoShotIds.includes(shot.id), busyShotIdSet.has(shot.id), busyVideoPromptShotIdSet.has(shot.id), activeClips.get(shot.id)?.id, activeClips.get(shot.id)?.status]"
          :class="{ active: selectedVideoShot?.id === shot.id, selected: selectedVideoShotIds.includes(shot.id) }"
          @click="selectVideoShot(shot)"
        >
          <button type="button" :aria-pressed="selectedVideoShotIds.includes(shot.id)" :title="selectedVideoShotIds.includes(shot.id) ? '取消选择' : '选择镜头'" @click.stop="toggleVideoShotSelection(shot.id)"><Check :size="13" /></button>
          <div>
            <img v-if="shotFallbackImage(shot)" :src="mediaPreview(shotFallbackImage(shot))" :alt="shot.title" loading="lazy" decoding="async" />
            <span v-else><Camera :size="20" /></span>
            <i :data-status="activeClips.get(shot.id)?.status || 'idle'">{{ shotVideoStatus(shot) }}</i>
          </div>
          <header><strong>{{ shot.title }}</strong><small class="tabular-nums">#{{ shot.order_index }} · {{ shot.duration_seconds }}s</small></header>
          <p>{{ shot.video_prompt ? '视频提示词已准备' : '缺少视频提示词' }}</p>
        </article>
      </div>

      <footer class="director-video-board__footer">
        <nav v-if="videoPageCount > 1" class="director-video-pagination" aria-label="镜头分页">
          <button type="button" class="button button--secondary" :disabled="videoPage === 0" @click="videoPage--">上一页</button>
          <span>{{ videoPage + 1 }} / {{ videoPageCount }}</span>
          <button type="button" class="button button--secondary" :disabled="videoPage + 1 >= videoPageCount" @click="videoPage++">下一页</button>
        </nav>
        <button type="button" class="video-check" :aria-pressed="allVideoShotsSelected" @click="toggleAllVideoShots"><Check :size="13" />{{ allVideoShotsSelected ? '取消全选' : '全选镜头' }}</button>
        <span>未手动选择时默认处理全部镜头；进入队列后可在通知中心查看排队、进行中、完成和失败。</span>
      </footer>
    </section>

    <section v-else class="director-canvas__placeholder">
      <span><Sparkles :size="22" /></span><strong>{{ emptyStageContent.title }}</strong><p>{{ emptyStageContent.description }}</p>
    </section>
  </main>

  <BaseDialog
    v-model:open="automationConfirmOpen"
    title="启动 AI 全自动制作"
    description="确认后系统将接管当前章节的完整生产流程"
  >
    <div class="automation-confirm">
      <div class="automation-confirm__intro">
        <span><Sparkles :size="22" /></span>
        <div>
          <strong>确认制作“{{ chapter.title }}”吗？</strong>
          <p>AI 将根据项目手册和当前章节内容，自动编排并执行以下阶段。</p>
        </div>
      </div>

      <ol class="automation-confirm__stages" aria-label="全自动制作阶段">
        <li><span><Clapperboard :size="17" /></span><div><strong>剧本</strong><small>改编、审核与必要修复</small></div></li>
        <li><span><Boxes :size="17" /></span><div><strong>资产</strong><small>提取、提示词与图片生成</small></div></li>
        <li><span><Layers3 :size="17" /></span><div><strong>分镜</strong><small>制作、审核与必要修复</small></div></li>
        <li><span><Film :size="17" /></span><div><strong>视频</strong><small>提示词与镜头视频生成</small></div></li>
      </ol>

      <div class="automation-confirm__notice">
        <LockKeyhole :size="17" />
        <p>运行期间当前章节会被锁定，相关模型任务将按实际调用消耗积分；失败任务按系统规则退回积分。</p>
      </div>
    </div>

    <template #footer>
      <button class="button button--ghost" type="button" :disabled="Boolean(automationAction)" @click="automationConfirmOpen = false">取消</button>
      <button class="button button--primary" type="button" :disabled="Boolean(automationAction)" @click="confirmAutomationStart">
        <LoaderCircle v-if="automationAction === 'start'" class="spin" :size="17" />
        <Sparkles v-else :size="17" />
        {{ automationAction === 'start' ? '正在启动' : '确认启动' }}
      </button>
    </template>
  </BaseDialog>
</template>

<style scoped>
.director-video-play-trigger { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; gap: 8px; border: 0; color: white; background: rgb(0 0 0 / 20%); cursor: pointer; font: inherit; }
.director-video-pagination { display: flex; align-items: center; gap: 10px; font-variant-numeric: tabular-nums; }
@media (hover: none) {
  .director-video-shot-grid article > button { backdrop-filter: none; -webkit-backdrop-filter: none; }
  .director-video-shot-grid article:hover { transform: none; }
}
</style>
