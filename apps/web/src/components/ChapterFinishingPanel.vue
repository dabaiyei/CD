<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import {
  AudioWaveform,
  Check,
  CircleCheckBig,
  Clock3,
  Download,
  Film,
  Gauge,
  Layers3,
  LoaderCircle,
  Music2,
  Play,
  Plus,
  Radio,
  RefreshCw,
  SlidersHorizontal,
  Sparkles,
  TriangleAlert,
  Upload,
  Volume2,
  Waves,
} from 'lucide-vue-next'

import { api } from '@/lib/api'
import { useActivityStore } from '@/stores/activity'
import { useToastStore } from '@/stores/toast'
import type { AITask, CompositionStatus, CompositionVersion, FinishingOptions, ProjectFileItem } from '@/types'

const props = defineProps<{
  projectId: string
  chapterId: string
  chapterTitle: string
  resolution: string
  aspectRatio: string
}>()

const activity = useActivityStore()
const toast = useToastStore()
const loading = ref(false)
const action = ref<'prepare' | 'render' | 'activate' | 'upload' | ''>('')
const compositions = ref<CompositionVersion[]>([])
const options = ref<FinishingOptions>({ audio_files: [] })
const selectedId = ref('')
const audioInput = ref<HTMLInputElement | null>(null)
const form = reactive({
  title: '',
  background_music_file_id: '',
  environment_audio_file_ids: [] as string[],
  dialogue_volume: 1,
  background_music_volume: 0.22,
  environment_volume: 0.35,
  fade_seconds: 0.35,
  fps: 24,
})

const selected = computed(() => compositions.value.find((item) => item.id === selectedId.value) ?? null)
const renderTasks = computed(() => activity.tasks.filter(
  (task) => task.project_id === props.projectId
    && task.task_type === 'chapter_composition_render'
    && task.request_payload.chapter_id === props.chapterId,
))
const activeRenderTask = computed(() => renderTasks.value.find(
  (task) => task.request_payload.composition_id === selected.value?.id
    && ['queued', 'running'].includes(task.status),
) ?? null)
const taskSignature = computed(() => renderTasks.value
  .map((task) => `${task.id}:${task.status}:${task.updated_at}`)
  .join('|'))
const totalDuration = computed(() => Number(selected.value?.duration_seconds || 0))

const statusLabel: Record<CompositionStatus, string> = {
  draft: '待渲染',
  rendering: '渲染中',
  ready: '成片就绪',
  failed: '渲染失败',
  stale: '源版本已更新',
}

onMounted(load)
watch(() => props.chapterId, load)
watch(taskSignature, load)

async function load(): Promise<void> {
  if (!props.chapterId) return
  loading.value = true
  try {
    const [rows, finishingOptions] = await Promise.all([
      api<CompositionVersion[]>(`/projects/${props.projectId}/chapters/${props.chapterId}/compositions`),
      api<FinishingOptions>(`/projects/${props.projectId}/finishing/options`),
    ])
    compositions.value = rows
    options.value = finishingOptions
    if (!rows.some((item) => item.id === selectedId.value)) {
      selectedId.value = rows.find((item) => item.is_active)?.id ?? rows[0]?.id ?? ''
    }
    if (!form.title) form.title = `${props.chapterTitle} 成片`
  } catch (error) {
    toast.show('成片工作区加载失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    loading.value = false
  }
}

function toggleEnvironmentTrack(id: string): void {
  const index = form.environment_audio_file_ids.indexOf(id)
  if (index >= 0) form.environment_audio_file_ids.splice(index, 1)
  else form.environment_audio_file_ids.push(id)
}

async function prepareComposition(): Promise<void> {
  action.value = 'prepare'
  try {
    const created = await api<CompositionVersion>(
      `/projects/${props.projectId}/chapters/${props.chapterId}/compositions`,
      { method: 'POST', body: JSON.stringify(form) },
    )
    compositions.value = [created, ...compositions.value]
    selectedId.value = created.id
    toast.show('合成清单已锁定', { message: '镜头、配音和混音参数已保存为不可变版本', tone: 'success' })
  } catch (error) {
    toast.show('合成清单创建失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    action.value = ''
  }
}

async function renderComposition(): Promise<void> {
  if (!selected.value) return
  action.value = 'render'
  try {
    await api<AITask>(
      `/projects/${props.projectId}/chapters/${props.chapterId}/compositions/${selected.value.id}/render`,
      { method: 'POST' },
    )
    await activity.refresh()
    await load()
    toast.show('章节成片已进入渲染队列', { message: '可以离开页面，任务进度会持续保存', tone: 'success' })
  } catch (error) {
    toast.show('渲染任务创建失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    action.value = ''
  }
}

async function activateComposition(): Promise<void> {
  if (!selected.value) return
  action.value = 'activate'
  try {
    const updated = await api<CompositionVersion>(
      `/projects/${props.projectId}/chapters/${props.chapterId}/compositions/${selected.value.id}/activate`,
      { method: 'POST' },
    )
    compositions.value = compositions.value.map((item) => ({
      ...item,
      is_active: item.id === updated.id,
    }))
    toast.show('已设为本章生效成片', { tone: 'success' })
  } catch (error) {
    toast.show('成片切换失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    action.value = ''
  }
}

async function uploadTrack(event: Event): Promise<void> {
  const file = (event.target as HTMLInputElement).files?.[0]
  if (!file) return
  action.value = 'upload'
  const body = new FormData()
  body.append('file', file)
  try {
    const created = await api<ProjectFileItem>(`/projects/${props.projectId}/finishing/audio`, {
      method: 'POST',
      body,
    })
    options.value.audio_files = [created, ...options.value.audio_files]
    form.background_music_file_id = created.id
    toast.show('音轨已加入项目', { message: created.name, tone: 'success' })
  } catch (error) {
    toast.show('音轨上传失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    action.value = ''
    if (audioInput.value) audioInput.value.value = ''
  }
}

function durationLabel(seconds: number): string {
  const minutes = Math.floor(seconds / 60)
  const remain = Math.round(seconds % 60)
  return `${String(minutes).padStart(2, '0')}:${String(remain).padStart(2, '0')}`
}

function shotWidth(duration: string): string {
  return `${Math.max(86, Number(duration) * 23)}px`
}
</script>

<template>
  <section class="finishing-workbench stage-enter">
    <header class="finishing-header">
      <div><span class="stage-kicker">FINAL ASSEMBLY</span><h3>章节合成与交付</h3><p>冻结生产版本、编排音轨并生成可下载的章节成片</p></div>
      <div class="finishing-header__spec"><span><Gauge :size="15" />{{ resolution }}</span><span><Film :size="15" />{{ aspectRatio }}</span><span><Clock3 :size="15" />{{ durationLabel(totalDuration) }}</span></div>
    </header>

    <div v-if="compositions.length" class="finishing-version-bar">
      <span><Layers3 :size="15" />成片版本</span>
      <div><button v-for="item in compositions" :key="item.id" type="button" :class="{ active: item.id === selectedId }" @click="selectedId = item.id"><strong class="tabular-nums">v{{ item.version }}</strong><small>{{ statusLabel[item.status] }}</small><CircleCheckBig v-if="item.is_active" :size="14" /></button></div>
      <button v-if="selected?.status === 'ready' && !selected.is_active" class="button button--secondary" type="button" :disabled="action === 'activate'" @click="activateComposition"><Check :size="15" />设为生效</button>
    </div>

    <div v-if="loading" class="finishing-loading"><LoaderCircle class="spin" :size="20" />正在同步成片清单</div>
    <template v-else-if="selected">
      <section class="finishing-preview" :class="`is-${selected.status}`">
        <div class="finishing-player">
          <video v-if="selected.output_url" :src="selected.output_url" controls preload="metadata"></video>
          <div v-else class="finishing-player__empty"><span><LoaderCircle v-if="selected.status === 'rendering'" class="spin" :size="30" /><Film v-else :size="30" /></span><strong>{{ selected.status === 'rendering' ? '渲染节点正在编码' : statusLabel[selected.status] }}</strong><small>{{ activeRenderTask?.status === 'queued' ? '任务排队中，资源释放后自动开始' : selected.error_message || selected.invalidated_reason || '清单已经准备好，可以开始渲染' }}</small></div>
          <span class="finishing-player__badge" :data-status="selected.status"><i></i>{{ statusLabel[selected.status] }}</span>
        </div>
        <aside><span class="stage-kicker">DELIVERY MASTER</span><h4>{{ selected.title }}</h4><dl><div><dt>总时长</dt><dd class="tabular-nums">{{ durationLabel(Number(selected.duration_seconds)) }}</dd></div><div><dt>输出规格</dt><dd>{{ selected.resolution }} · {{ selected.aspect_ratio }}</dd></div><div><dt>帧率</dt><dd class="tabular-nums">{{ selected.fps }} FPS</dd></div><div><dt>镜头</dt><dd class="tabular-nums">{{ selected.timeline_manifest.shots.length }}</dd></div></dl><div class="finishing-preview__actions"><a v-if="selected.output_url" class="button button--primary" :href="selected.output_url" :download="`${selected.title}.mp4`"><Download :size="16" />下载成片</a><button v-if="['draft', 'failed'].includes(selected.status)" class="button button--primary" type="button" :disabled="Boolean(action) || Boolean(activeRenderTask)" @click="renderComposition"><LoaderCircle v-if="action === 'render'" class="spin" :size="16" /><Play v-else :size="16" />开始渲染</button><button v-if="selected.status === 'failed'" class="button button--secondary" type="button" @click="renderComposition"><RefreshCw :size="15" />重新排队</button></div></aside>
      </section>

      <section class="chapter-timeline">
        <header><div><Radio :size="15" /><strong>版本锁定时间线</strong></div><span class="tabular-nums">{{ selected.timeline_manifest.shots.length }} SHOTS · {{ durationLabel(totalDuration) }}</span></header>
        <div class="timeline-scroll">
          <div class="timeline-ruler"><span v-for="second in Math.max(1, Math.ceil(totalDuration / 5))" :key="second" :style="{ width: '115px' }">{{ durationLabel((second - 1) * 5) }}</span></div>
          <div class="timeline-track timeline-track--video"><label><Film :size="14" />画面</label><div><article v-for="shot in selected.timeline_manifest.shots" :key="shot.shot_id" :style="{ width: shotWidth(shot.duration_seconds) }"><video v-if="shot.media_url" :src="shot.media_url" muted preload="metadata"></video><span class="tabular-nums">{{ String(shot.order_index).padStart(2, '0') }}</span><strong>{{ shot.title }}</strong><small>{{ shot.duration_seconds }}s</small></article></div></div>
          <div class="timeline-track timeline-track--voice"><label><AudioWaveform :size="14" />对白</label><div><template v-for="shot in selected.timeline_manifest.shots" :key="shot.shot_id"><article :style="{ width: shotWidth(shot.duration_seconds) }" :class="{ empty: !shot.dialogue_clips.length }"><span v-if="shot.dialogue_clips.length"><Waves :size="13" /><b>{{ shot.dialogue_clips.map((clip) => clip.speaker).join(' · ') }}</b></span><small>{{ shot.dialogue_clips.length ? `${shot.dialogue_clips.length} 条配音` : '静音镜头' }}</small></article></template></div></div>
          <div v-if="selected.timeline_manifest.background_music" class="timeline-track timeline-track--music"><label><Music2 :size="14" />配乐</label><div><article class="full" :style="{ width: `${Math.max(480, totalDuration * 23)}px` }"><Volume2 :size="14" /><strong>{{ selected.timeline_manifest.background_music.name }}</strong><span class="wave-bars"><i v-for="bar in 22" :key="bar"></i></span></article></div></div>
        </div>
      </section>

      <div v-if="selected.timeline_manifest.warnings.length" class="finishing-warnings"><TriangleAlert :size="17" /><div><strong>清单包含 {{ selected.timeline_manifest.warnings.length }} 项提示</strong><span>{{ selected.timeline_manifest.warnings.join('；') }}</span></div></div>
    </template>

    <section class="mix-console">
      <header><div><SlidersHorizontal :size="16" /><span><strong>新建合成版本</strong><small>当前设置会在创建后锁定</small></span></div><button class="button button--secondary" type="button" :disabled="action === 'upload'" @click="audioInput?.click()"><LoaderCircle v-if="action === 'upload'" class="spin" :size="15" /><Upload v-else :size="15" />上传音轨<input ref="audioInput" class="sr-only" type="file" accept="audio/*,.mp3,.wav,.m4a,.aac,.ogg,.flac" @change="uploadTrack" /></button></header>
      <div class="mix-console__body">
        <label class="finishing-title-field"><span>版本名称</span><div><Sparkles :size="15" /><input v-model="form.title" maxlength="255" /></div></label>
        <div class="track-picker"><span>背景配乐</span><div><button type="button" :class="{ active: !form.background_music_file_id }" @click="form.background_music_file_id = ''"><span><Plus :size="16" /></span><strong>不使用配乐</strong><small>仅保留对白</small></button><button v-for="track in options.audio_files" :key="track.id" type="button" :class="{ active: form.background_music_file_id === track.id }" @click="form.background_music_file_id = track.id"><span><Music2 :size="16" /></span><strong>{{ track.name }}</strong><small>{{ (track.size_bytes / 1024 / 1024).toFixed(1) }} MB</small><CircleCheckBig :size="14" /></button></div></div>
        <div v-if="options.audio_files.length" class="environment-picker"><span>环境音层</span><div><button v-for="track in options.audio_files" :key="track.id" type="button" :class="{ active: form.environment_audio_file_ids.includes(track.id) }" @click="toggleEnvironmentTrack(track.id)"><Waves :size="14" />{{ track.name }}<Check :size="13" /></button></div></div>
        <div class="mixer-sliders"><label><span><AudioWaveform :size="14" />对白<strong class="tabular-nums">{{ Math.round(form.dialogue_volume * 100) }}%</strong></span><input v-model.number="form.dialogue_volume" type="range" min="0" max="2" step="0.05" /></label><label><span><Music2 :size="14" />配乐<strong class="tabular-nums">{{ Math.round(form.background_music_volume * 100) }}%</strong></span><input v-model.number="form.background_music_volume" type="range" min="0" max="1" step="0.01" /></label><label><span><Waves :size="14" />环境<strong class="tabular-nums">{{ Math.round(form.environment_volume * 100) }}%</strong></span><input v-model.number="form.environment_volume" type="range" min="0" max="1" step="0.01" /></label></div>
      </div>
      <footer><span><CircleCheckBig :size="14" />创建清单不消耗 AI 积分</span><button class="button button--primary" type="button" :disabled="Boolean(action) || !form.title.trim()" @click="prepareComposition"><LoaderCircle v-if="action === 'prepare'" class="spin" :size="16" /><Layers3 v-else :size="16" />锁定新版本</button></footer>
    </section>
  </section>
</template>
