<script setup lang="ts">
import { computed, ref, watch } from 'vue'
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
  LoaderCircle,
  Play,
  Sparkles,
  WandSparkles,
} from 'lucide-vue-next'

import type { AssetItem, Chapter, ScriptVersion, StoryboardShot, StoryboardVersionDetail, VideoClip } from '@/types'

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
}>()
const emit = defineEmits<{
  openAssets: []
  queueVideoPrompts: [shotIds: string[]]
  queueBatchVideos: [shotIds: string[]]
  queueShotVideo: [shot: StoryboardShot]
}>()
const tab = ref<'source' | 'script' | 'assets' | 'storyboard' | 'video'>('source')
const selectedVideoShotId = ref('')
const selectedVideoShotIds = ref<string[]>([])

const activeScript = computed(() => props.scripts.find((item) => item.is_active) ?? props.scripts[0] ?? null)
const readyAssets = computed(() => props.assets.filter((item) => Boolean(item.media_url)))
const busyShotIdSet = computed(() => new Set(props.busyShotIds ?? []))
const busyVideoPromptShotIdSet = computed(() => new Set(props.busyVideoPromptShotIds ?? []))
const clipsByShot = computed(() => {
  const rows = new Map<string, VideoClip[]>()
  props.storyboard?.video_clips.forEach((clip) => {
    const list = rows.get(clip.shot_id) ?? []
    list.push(clip)
    rows.set(clip.shot_id, list)
  })
  rows.forEach((list) => list.sort((a, b) => b.version - a.version))
  return rows
})
const activeClips = computed(() => {
  const rows = new Map<string, VideoClip>()
  props.storyboard?.video_clips.forEach((clip) => {
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
const shotsWithPromptCount = computed(() => props.storyboard?.shots.filter((shot) => Boolean(shot.video_prompt.trim())).length ?? 0)
const selectedVideoShot = computed(() => (
  props.storyboard?.shots.find((shot) => shot.id === selectedVideoShotId.value)
  ?? props.storyboard?.shots[0]
  ?? null
))
const selectedVideoShotAssets = computed(() => (
  selectedVideoShot.value ? shotAssets(selectedVideoShot.value) : []
))
const allVideoShotsSelected = computed(() => Boolean(props.storyboard?.shots.length)
  && props.storyboard!.shots.every((shot) => selectedVideoShotIds.value.includes(shot.id)))
const selectedVideoShots = computed(() => {
  const shots = props.storyboard?.shots ?? []
  return selectedVideoShotIds.value.length
    ? shots.filter((shot) => selectedVideoShotIds.value.includes(shot.id))
    : shots
})
const videoPromptEligibleShots = computed(() => selectedVideoShots.value.filter(
  (shot) => !busyVideoPromptShotIdSet.value.has(shot.id),
))
const videoEligibleShots = computed(() => selectedVideoShots.value.filter(
  (shot) => !busyShotIdSet.value.has(shot.id)
    && Boolean(shot.video_prompt.trim())
    && !(activeClips.value.get(shot.id)?.is_active && activeClips.value.get(shot.id)?.status === 'ready'),
))
const downloadableVideoClips = computed(() => selectedVideoShots.value
  .map((shot) => activeClips.value.get(shot.id))
  .filter((clip): clip is VideoClip => Boolean(clip?.is_active && clip.status === 'ready' && clip.media_url)))
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

function shotAssets(shot: StoryboardShot): AssetItem[] {
  return props.assets.filter((asset) => shot.asset_ids.includes(asset.id))
}

function shotFallbackImage(shot: StoryboardShot): string {
  return shot.reference_image_url || shotAssets(shot).find((asset) => Boolean(asset.media_url))?.media_url || ''
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
  const ids = props.storyboard?.shots.map((shot) => shot.id) ?? []
  selectedVideoShotIds.value = allVideoShotsSelected.value ? [] : ids
}

function selectedShotIdsForQueue(): string[] {
  return selectedVideoShotIds.value.length
    ? selectedVideoShotIds.value
    : (props.storyboard?.shots.map((shot) => shot.id) ?? [])
}

function queueVideoPrompts(): void {
  emit('queueVideoPrompts', selectedShotIdsForQueue())
}

function queueSelectedVideoPrompt(): void {
  if (!selectedVideoShot.value) return
  emit('queueVideoPrompts', [selectedVideoShot.value.id])
}

function queueBatchVideos(): void {
  emit('queueBatchVideos', selectedShotIdsForQueue())
}

function downloadReadyVideos(): void {
  downloadableVideoClips.value.forEach((clip) => {
    if (!clip.media_url) return
    const link = document.createElement('a')
    link.href = clip.media_url
    link.download = `shot-${clip.shot_id}-v${clip.version}.mp4`
    document.body.appendChild(link)
    link.click()
    link.remove()
  })
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
    const shots = props.storyboard?.shots ?? []
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
      </div>
    </header>

    <nav class="director-canvas__tabs" aria-label="章节产物">
      <button type="button" :aria-pressed="tab === 'source'" @click="tab = 'source'"><BookOpenText :size="15" />原文</button>
      <button type="button" :aria-pressed="tab === 'script'" :disabled="!activeScript" @click="tab = 'script'"><FileText :size="15" />剧本<span class="tabular-nums">{{ scripts.length }}</span></button>
      <button type="button" :aria-pressed="tab === 'assets'" :disabled="!assets.length" @click="tab = 'assets'"><Boxes :size="15" />资产<span class="tabular-nums">{{ assets.length }}</span></button>
      <button type="button" :aria-pressed="tab === 'storyboard'" :disabled="!storyboard" @click="tab = 'storyboard'"><Layers3 :size="15" />分镜<span class="tabular-nums">{{ storyboard?.shots.length || 0 }}</span></button>
      <button type="button" :aria-pressed="tab === 'video'" :disabled="!storyboard" @click="tab = 'video'"><Film :size="15" />生成视频<span class="tabular-nums">{{ readyVideoCount }}</span></button>
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

    <section v-else-if="tab === 'assets'" class="director-asset-board">
      <header><div><strong>本章塑造资产</strong><span>{{ readyAssets.length }} / {{ assets.length }} 已定稿</span></div><button type="button" @click="emit('openAssets')"><Boxes :size="15" />管理资产</button></header>
      <div>
        <article v-for="asset in assets" :key="asset.id">
          <div><img v-if="asset.media_url" :src="asset.media_url" :alt="asset.name" /><span v-else><Image :size="22" /></span><i :data-ready="Boolean(asset.media_url)">{{ asset.media_url ? '已定稿' : asset.generation_prompt ? '待生图' : '待提示词' }}</i></div>
          <strong>{{ asset.name }}</strong><small>{{ asset.asset_type === 'character' ? '人物' : asset.asset_type === 'scene' ? '场景' : asset.asset_type === 'prop' ? '道具' : '素材' }}</small>
        </article>
      </div>
    </section>

    <section v-else-if="tab === 'storyboard' && storyboard" class="director-storyboard-board">
      <header><div><strong>导演分镜 v{{ storyboard.version.version }}</strong><span>{{ storyboard.shots.length }} 个镜头</span></div><span><Sparkles :size="14" />已通过资产校验</span></header>
      <div>
        <article v-for="shot in storyboard.shots" :key="shot.id">
          <div><img v-if="shot.reference_image_url" :src="shot.reference_image_url" :alt="shot.title" /><span v-else><Camera :size="24" /></span><b class="tabular-nums">{{ String(shot.order_index).padStart(2, '0') }}</b></div>
          <header><strong>{{ shot.title }}</strong><small>{{ shot.shot_type }} · {{ shot.duration_seconds }}s</small></header>
          <p>{{ shot.action_description || shot.scene_description }}</p>
        </article>
      </div>
    </section>

    <section v-else-if="tab === 'video' && storyboard" class="director-video-board">
      <header>
        <div>
          <strong>镜头视频生产</strong>
          <span><b class="tabular-nums">{{ readyVideoCount }}</b> / {{ storyboard.shots.length }} 已完成 · <b class="tabular-nums">{{ shotsWithPromptCount }}</b> 个镜头已有提示词</span>
        </div>
        <div class="director-video-board__actions">
          <button type="button" class="button button--secondary" :disabled="!downloadableVideoClips.length" @click="downloadReadyVideos"><Download :size="15" />下载已完成</button>
          <button type="button" class="button button--secondary" :disabled="!videoPromptEligibleShots.length || videoAction === 'videoPrompt' || videoPromptTaskActive" @click="queueVideoPrompts">
            <LoaderCircle v-if="videoAction === 'videoPrompt' || videoPromptTaskActive" class="spin" :size="15" />
            <WandSparkles v-else :size="15" />
            批量提示词
          </button>
          <button type="button" class="button button--primary" :disabled="!videoEligibleShots.length || Boolean(videoAction)" @click="queueBatchVideos">
            <LoaderCircle v-if="videoAction === 'batchVideo'" class="spin" :size="15" />
            <Play v-else :size="15" />
            批量生成视频
          </button>
        </div>
      </header>

      <section v-if="selectedVideoShot" class="director-video-focus">
        <div class="director-video-focus__preview">
          <video v-if="activeClips.get(selectedVideoShot.id)?.media_url" :src="activeClips.get(selectedVideoShot.id)?.media_url || undefined" controls preload="metadata"></video>
          <img v-else-if="shotFallbackImage(selectedVideoShot)" :src="shotFallbackImage(selectedVideoShot)" :alt="selectedVideoShot.title" />
          <span v-else><Camera :size="28" />等待参考图</span>
          <i :data-status="activeClips.get(selectedVideoShot.id)?.status || 'idle'">{{ shotVideoStatus(selectedVideoShot) }}</i>
        </div>
        <article>
          <header>
            <div>
              <small class="tabular-nums">SHOT {{ String(selectedVideoShot.order_index).padStart(2, '0') }} · {{ selectedVideoShot.duration_seconds }}s</small>
              <strong>{{ selectedVideoShot.title }}</strong>
            </div>
            <div class="director-video-focus__buttons">
              <button type="button" class="button button--secondary" :disabled="busyVideoPromptShotIdSet.has(selectedVideoShot.id) || videoAction === 'videoPrompt' || videoPromptTaskActive" @click="queueSelectedVideoPrompt">
                <LoaderCircle v-if="busyVideoPromptShotIdSet.has(selectedVideoShot.id) || videoAction === 'videoPrompt'" class="spin" :size="15" />
                <WandSparkles v-else :size="15" />
                {{ selectedVideoShot.video_prompt ? '重写提示词' : '生成提示词' }}
              </button>
              <button type="button" class="button button--primary" :disabled="!selectedVideoShot.video_prompt || busyShotIdSet.has(selectedVideoShot.id) || videoAction === 'video'" @click="emit('queueShotVideo', selectedVideoShot)">
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
                <span><img v-if="asset.media_url" :src="asset.media_url" :alt="asset.name" /><Image v-else :size="17" /></span>
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
        </article>
      </section>

      <div class="director-video-shot-grid">
        <article
          v-for="shot in storyboard.shots"
          :key="shot.id"
          :class="{ active: selectedVideoShot?.id === shot.id, selected: selectedVideoShotIds.includes(shot.id) }"
          @click="selectVideoShot(shot)"
        >
          <button type="button" :aria-pressed="selectedVideoShotIds.includes(shot.id)" :title="selectedVideoShotIds.includes(shot.id) ? '取消选择' : '选择镜头'" @click.stop="toggleVideoShotSelection(shot.id)"><Check :size="13" /></button>
          <div>
            <video v-if="activeClips.get(shot.id)?.media_url" :src="activeClips.get(shot.id)?.media_url || undefined" preload="metadata"></video>
            <img v-else-if="shotFallbackImage(shot)" :src="shotFallbackImage(shot)" :alt="shot.title" />
            <span v-else><Camera :size="20" /></span>
            <i :data-status="activeClips.get(shot.id)?.status || 'idle'">{{ shotVideoStatus(shot) }}</i>
          </div>
          <header><strong>{{ shot.title }}</strong><small class="tabular-nums">#{{ shot.order_index }} · {{ shot.duration_seconds }}s</small></header>
          <p>{{ shot.video_prompt ? '视频提示词已准备' : '缺少视频提示词' }}</p>
        </article>
      </div>

      <footer class="director-video-board__footer">
        <button type="button" class="video-check" :aria-pressed="allVideoShotsSelected" @click="toggleAllVideoShots"><Check :size="13" />{{ allVideoShotsSelected ? '取消全选' : '全选镜头' }}</button>
        <span>未手动选择时默认处理全部镜头；进入队列后可在通知中心查看排队、进行中、完成和失败。</span>
      </footer>
    </section>

    <section v-else class="director-canvas__placeholder">
      <span><Sparkles :size="22" /></span><strong>产物正在准备</strong><p>导演 Agent 完成当前阶段后会自动出现在这里。</p>
    </section>
  </main>
</template>
