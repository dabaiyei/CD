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

const demoTimestamp = '2026-09-02T10:00:00+08:00'
const demoScript = computed<ScriptVersion>(() => ({
  id: 'demo-script-v1',
  project_id: props.chapter.project_id,
  chapter_id: props.chapter.id,
  version: 1,
  title: '第 1 集：寒门少年的选择',
  content: `【场 1】韩家庄练武场 / 夜 / 外

风雪压住远山。十六岁的韩宇将最后一块铁石推回木架，掌心已经磨出血痕。

韩宇（喘息）：再来一次。

远处传来家族子弟的笑声。韩宇没有回头，只把更重的铁石抱进怀里。

【场 2】偏院 / 夜 / 内

昏黄灯火下，韩子枫把一只生锈铁盒推到桌前。盒中那本无字古籍泛起微光。

韩子枫：这是你母亲留下的。想见她，就走到阴阳境。

韩宇抬眼。窗外风雪骤停，古籍在他掌中自行翻开第一页。`,
  status: 'approved',
  review_notes: '演示版本：强化前三分钟钩子，保留父子关系与无字古籍悬念。',
  is_active: true,
  created_at: demoTimestamp,
  updated_at: demoTimestamp,
}))
const demoAssets = computed<AssetItem[]>(() => ([
  {
    id: 'demo-asset-hanyu', project_id: props.chapter.project_id, scope: 'project', asset_type: 'character', parent_asset_id: null,
    name: '韩宇', description: '十六岁寒门少年，克制、倔强，练武服带有明显磨损。', generation_prompt: '少年武者，冬夜练武场，克制坚毅',
    media_url: '/covers/second-farewell.jpg', status: 'ready', asset_metadata: {}, version: 1, created_at: demoTimestamp, updated_at: demoTimestamp,
  },
  {
    id: 'demo-asset-father', project_id: props.chapter.project_id, scope: 'project', asset_type: 'character', parent_asset_id: null,
    name: '韩子枫', description: '曾经的家族天才，重伤后气质沉静，目光仍有锋芒。', generation_prompt: '中年武者，旧伤，昏黄灯火，沉静威严',
    media_url: '/covers/mist-harbor.jpg', status: 'ready', asset_metadata: {}, version: 1, created_at: demoTimestamp, updated_at: demoTimestamp,
  },
  {
    id: 'demo-asset-yard', project_id: props.chapter.project_id, scope: 'project', asset_type: 'scene', parent_asset_id: null,
    name: '韩家偏院', description: '风雪中的简陋院落，与远处繁华主宅形成强烈对照。', generation_prompt: '古代偏院，深冬夜景，灯笼，冷暖对比',
    media_url: '/covers/changan-night.jpg', status: 'ready', asset_metadata: {}, version: 1, created_at: demoTimestamp, updated_at: demoTimestamp,
  },
  {
    id: 'demo-asset-book', project_id: props.chapter.project_id, scope: 'project', asset_type: 'prop', parent_asset_id: null,
    name: '无字古籍', description: '母亲留下的神秘典籍，靠近韩宇时会浮现微光。', generation_prompt: '无字古籍，暗金纹路，微弱灵光，旧铁盒',
    media_url: null, status: 'prompt_ready', asset_metadata: {}, version: 1, created_at: demoTimestamp, updated_at: demoTimestamp,
  },
]))
const demoStoryboard = computed<StoryboardVersionDetail>(() => {
  const storyboardId = 'demo-storyboard-v1'
  const shots: StoryboardShot[] = [
    {
      id: 'demo-shot-1', storyboard_version_id: storyboardId, order_index: 1, title: '风雪练武场', shot_type: '大全景', duration_seconds: '4.0',
      scene_description: '风雪笼罩韩家庄，练武场只剩韩宇一人。', action_description: '镜头缓慢推进，韩宇从雪地中站起。', dialogue: '',
      image_prompt: '古代练武场，暴雪夜，孤独少年，电影宽银幕', video_prompt: '低机位缓慢推进，雪粒横向掠过，少年起身看向铁石。',
      asset_ids: ['demo-asset-hanyu', 'demo-asset-yard'], reference_image_url: '/covers/changan-night.jpg', version: 1, created_at: demoTimestamp, updated_at: demoTimestamp,
    },
    {
      id: 'demo-shot-2', storyboard_version_id: storyboardId, order_index: 2, title: '血痕与铁石', shot_type: '特写', duration_seconds: '3.0',
      scene_description: '粗粝铁石压在木架边，少年手掌渗出血痕。', action_description: '韩宇收紧手指，再次握住铁石。', dialogue: '韩宇：再来一次。',
      image_prompt: '少年带血手掌，粗粝铁石，冷色电影光', video_prompt: '微距特写，手指逐渐收紧，血珠沿掌纹滑落。',
      asset_ids: ['demo-asset-hanyu'], reference_image_url: '/covers/second-farewell.jpg', version: 1, created_at: demoTimestamp, updated_at: demoTimestamp,
    },
    {
      id: 'demo-shot-3', storyboard_version_id: storyboardId, order_index: 3, title: '父亲的铁盒', shot_type: '中近景', duration_seconds: '5.0',
      scene_description: '偏院内灯火昏黄，父子隔桌而坐。', action_description: '韩子枫将生锈铁盒推向韩宇。', dialogue: '韩子枫：这是你母亲留下的。',
      image_prompt: '古代父子，木桌，旧铁盒，暖色烛光', video_prompt: '侧面双人构图，镜头随铁盒横移，最终停在少年眼神。',
      asset_ids: ['demo-asset-hanyu', 'demo-asset-father', 'demo-asset-yard', 'demo-asset-book'], reference_image_url: '/covers/mist-harbor.jpg', version: 1, created_at: demoTimestamp, updated_at: demoTimestamp,
    },
    {
      id: 'demo-shot-4', storyboard_version_id: storyboardId, order_index: 4, title: '古籍苏醒', shot_type: '俯拍特写', duration_seconds: '4.5',
      scene_description: '无字古籍在韩宇掌中自行翻开。', action_description: '暗金纹路依次亮起，风雪声骤然停止。', dialogue: '',
      image_prompt: '神秘古籍，暗金灵光，少年双手，俯拍', video_prompt: '',
      asset_ids: ['demo-asset-hanyu', 'demo-asset-book'], reference_image_url: null, version: 1, created_at: demoTimestamp, updated_at: demoTimestamp,
    },
  ]
  return {
    version: {
      id: storyboardId, project_id: props.chapter.project_id, chapter_id: props.chapter.id, script_version_id: demoScript.value.id,
      version: 1, is_active: true, invalidated_reason: null, created_at: demoTimestamp, updated_at: demoTimestamp,
    },
    shots,
    video_clips: [{
      id: 'demo-clip-1', storyboard_version_id: storyboardId, shot_id: 'demo-shot-1', model_id: 'demo-video-model', version: 1,
      status: 'generating', media_url: null, provider_job_id: null, error_message: null, is_active: false, invalidated_reason: null,
      created_at: demoTimestamp, updated_at: demoTimestamp,
    }],
  }
})
const previewScripts = computed(() => props.scripts.length ? props.scripts : [demoScript.value])
const previewAssets = computed(() => props.assets.length ? props.assets : demoAssets.value)
const previewStoryboard = computed(() => props.storyboard ?? demoStoryboard.value)
const demoStageActive = computed(() => (
  (tab.value === 'script' && !props.scripts.length)
  || (tab.value === 'assets' && !props.assets.length)
  || ((tab.value === 'storyboard' || tab.value === 'video') && !props.storyboard)
))

const activeScript = computed(() => previewScripts.value.find((item) => item.is_active) ?? previewScripts.value[0] ?? null)
const readyAssets = computed(() => previewAssets.value.filter((item) => Boolean(item.media_url)))
const busyShotIdSet = computed(() => new Set(props.busyShotIds ?? []))
const busyVideoPromptShotIdSet = computed(() => new Set(props.busyVideoPromptShotIds ?? []))
const clipsByShot = computed(() => {
  const rows = new Map<string, VideoClip[]>()
  previewStoryboard.value.video_clips.forEach((clip) => {
    const list = rows.get(clip.shot_id) ?? []
    list.push(clip)
    rows.set(clip.shot_id, list)
  })
  rows.forEach((list) => list.sort((a, b) => b.version - a.version))
  return rows
})
const activeClips = computed(() => {
  const rows = new Map<string, VideoClip>()
  previewStoryboard.value.video_clips.forEach((clip) => {
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
const shotsWithPromptCount = computed(() => previewStoryboard.value.shots.filter((shot) => Boolean(shot.video_prompt.trim())).length)
const selectedVideoShot = computed(() => (
  previewStoryboard.value.shots.find((shot) => shot.id === selectedVideoShotId.value)
  ?? previewStoryboard.value.shots[0]
  ?? null
))
const selectedVideoShotAssets = computed(() => (
  selectedVideoShot.value ? shotAssets(selectedVideoShot.value) : []
))
const allVideoShotsSelected = computed(() => Boolean(previewStoryboard.value.shots.length)
  && previewStoryboard.value.shots.every((shot) => selectedVideoShotIds.value.includes(shot.id)))
const selectedVideoShots = computed(() => {
  const shots = previewStoryboard.value.shots
  return selectedVideoShotIds.value.length
    ? shots.filter((shot) => selectedVideoShotIds.value.includes(shot.id))
    : shots
})
const videoPromptEligibleShots = computed(() => selectedVideoShots.value.filter(
  (shot) => !busyVideoPromptShotIdSet.value.has(shot.id) && !busyShotIdSet.value.has(shot.id),
))
const videoEligibleShots = computed(() => selectedVideoShots.value.filter(
  (shot) => !busyShotIdSet.value.has(shot.id)
    && !busyVideoPromptShotIdSet.value.has(shot.id)
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
  return previewAssets.value.filter((asset) => shot.asset_ids.includes(asset.id))
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
  const ids = previewStoryboard.value.shots.map((shot) => shot.id)
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
  () => previewStoryboard.value.version.id,
  () => {
    const shots = previewStoryboard.value.shots
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
        <span v-if="demoStageActive" class="director-preview-badge">演示预览</span>
        <span class="chapter-status" :data-status="chapter.status"><i></i>{{ chapterStatusText }}</span>
        <small><Check :size="13" />AI 自动编排</small>
      </div>
    </header>

    <nav class="director-canvas__tabs" aria-label="章节产物">
      <button type="button" :aria-pressed="tab === 'source'" @click="tab = 'source'"><BookOpenText :size="15" />原文</button>
      <button type="button" :aria-pressed="tab === 'script'" @click="tab = 'script'"><FileText :size="15" />剧本<span class="tabular-nums">{{ previewScripts.length }}</span></button>
      <button type="button" :aria-pressed="tab === 'assets'" @click="tab = 'assets'"><Boxes :size="15" />资产<span class="tabular-nums">{{ previewAssets.length }}</span></button>
      <button type="button" :aria-pressed="tab === 'storyboard'" @click="tab = 'storyboard'"><Layers3 :size="15" />分镜<span class="tabular-nums">{{ previewStoryboard.shots.length }}</span></button>
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

    <section v-else-if="tab === 'assets'" class="director-asset-board">
      <header><div><strong>本章塑造资产</strong><span>{{ readyAssets.length }} / {{ previewAssets.length }} 已定稿</span></div><button type="button" @click="emit('openAssets')"><Boxes :size="15" />管理资产</button></header>
      <div>
        <article v-for="asset in previewAssets" :key="asset.id">
          <div><img v-if="asset.media_url" :src="asset.media_url" :alt="asset.name" /><span v-else><Image :size="22" /></span><i :data-ready="Boolean(asset.media_url)">{{ asset.media_url ? '已定稿' : asset.generation_prompt ? '待生图' : '待提示词' }}</i></div>
          <strong>{{ asset.name }}</strong><small>{{ asset.asset_type === 'character' ? '人物' : asset.asset_type === 'scene' ? '场景' : asset.asset_type === 'prop' ? '道具' : '素材' }}</small>
        </article>
      </div>
    </section>

    <section v-else-if="tab === 'storyboard'" class="director-storyboard-board">
      <header><div><strong>导演分镜 v{{ previewStoryboard.version.version }}</strong><span>{{ previewStoryboard.shots.length }} 个镜头</span></div><span><Sparkles :size="14" />已通过资产校验</span></header>
      <div>
        <article v-for="shot in previewStoryboard.shots" :key="shot.id">
          <div><img v-if="shot.reference_image_url" :src="shot.reference_image_url" :alt="shot.title" /><span v-else><Camera :size="24" /></span><b class="tabular-nums">{{ String(shot.order_index).padStart(2, '0') }}</b></div>
          <header><strong>{{ shot.title }}</strong><small>{{ shot.shot_type }} · {{ shot.duration_seconds }}s</small></header>
          <p>{{ shot.action_description || shot.scene_description }}</p>
        </article>
      </div>
    </section>

    <section v-else-if="tab === 'video'" class="director-video-board">
      <header>
        <div>
          <strong>镜头视频生产</strong>
          <span><b class="tabular-nums">{{ readyVideoCount }}</b> / {{ previewStoryboard.shots.length }} 已完成 · <b class="tabular-nums">{{ shotsWithPromptCount }}</b> 个镜头已有提示词</span>
        </div>
        <div class="director-video-board__actions">
          <button type="button" class="button button--secondary" :disabled="!downloadableVideoClips.length" @click="downloadReadyVideos"><Download :size="15" />下载已完成</button>
          <button type="button" class="button button--secondary" :disabled="demoStageActive || !videoPromptEligibleShots.length || videoAction === 'videoPrompt' || videoPromptTaskActive" @click="queueVideoPrompts">
            <LoaderCircle v-if="videoAction === 'videoPrompt' || videoPromptTaskActive" class="spin" :size="15" />
            <WandSparkles v-else :size="15" />
            批量提示词
          </button>
          <button type="button" class="button button--primary" :disabled="demoStageActive || !videoEligibleShots.length || Boolean(videoAction)" @click="queueBatchVideos">
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
              <button type="button" class="button button--secondary" :disabled="demoStageActive || busyVideoPromptShotIdSet.has(selectedVideoShot.id) || busyShotIdSet.has(selectedVideoShot.id) || videoAction === 'videoPrompt' || videoPromptTaskActive" @click="queueSelectedVideoPrompt">
                <LoaderCircle v-if="busyVideoPromptShotIdSet.has(selectedVideoShot.id) || videoAction === 'videoPrompt'" class="spin" :size="15" />
                <WandSparkles v-else :size="15" />
                {{ selectedVideoShot.video_prompt ? '重写提示词' : '生成提示词' }}
              </button>
              <button type="button" class="button button--primary" :disabled="demoStageActive || !selectedVideoShot.video_prompt || busyShotIdSet.has(selectedVideoShot.id) || busyVideoPromptShotIdSet.has(selectedVideoShot.id) || videoAction === 'video'" @click="emit('queueShotVideo', selectedVideoShot)">
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
          v-for="shot in previewStoryboard.shots"
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
      <span><Sparkles :size="22" /></span><strong>{{ emptyStageContent.title }}</strong><p>{{ emptyStageContent.description }}</p>
    </section>
  </main>
</template>
