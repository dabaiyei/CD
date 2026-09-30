<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Copy, Upload, Play, RefreshCw, Download, X, Plus, Search, Archive, Pencil, Film, CheckCheck, LayoutGrid } from 'lucide-vue-next'
import BaseDialog from '@/components/BaseDialog.vue'
import UiSelect from '@/components/UiSelect.vue'
import LiquidGlass from '@/components/LiquidGlass.vue'
import ReplicaReferences from '@/components/ReplicaReferences.vue'
import ReplicaProduction from '@/components/ReplicaProduction.vue'
import { api, apiUpload, ApiError } from '@/lib/api'
import type { AITask } from '@/types'
import { copyPlan, taskPlan, type ReplicaPlan as Plan, type ReplicaReference } from '@/lib/replicaPlan'

type Model = { id: string; name: string; type: string; is_default: boolean; capabilities: Record<string, unknown> }
type Transcript = { language: string; passages: { text: string; start_seconds?: number; end_seconds?: number }[] }
type ReplicaTask = AITask & { result_payload: { production?: unknown; production_documents?: Record<string,string>; transcription?: Transcript; transcription_skipped?: string; plan?: Plan; media_url?: string; source_url?: string; scene_frames?: Record<string, {url:string}>; clips?: Record<string, { url?: string; status?: string; error?: string; attempts?: number; prompt?: string; shot_indices?: number[] }> } | null }
const creating = ref(false)
const section = ref('plan')
const search = ref('')
const filter = ref('all')
const showArchived = ref(false)
const managing = ref(false)
const checked = ref<string[]>([])
const listLimit = ref(60)
const renameOpen = ref(false)
const renameText = ref('')
const notice = ref('')
const displayName = (task: AITask) => String(task.request_payload.display_name || task.request_payload.filename || '参考视频')
const visibleTasks = computed(() => tasks.value.filter(t =>
  Boolean(t.request_payload.archived) === showArchived.value &&
  (filter.value === 'all' || (filter.value === 'active' ? active(t) : t.status === filter.value)) &&
  (displayName(t) + taskTitle(t)).toLowerCase().includes(search.value.trim().toLowerCase())
))
const stats = computed(() => [
  { label: '进行中', value: tasks.value.filter(active).length, icon: Play },
  { label: '已完成', value: tasks.value.filter(t => t.status === 'succeeded').length, icon: CheckCheck },
  { label: '待处理', value: tasks.value.filter(t => ['failed','cancelled'].includes(t.status)).length, icon: RefreshCw },
])
watch([search, filter, showArchived, managing], () => { checked.value = [] })
async function metadata(task: ReplicaTask, values: Record<string, unknown>) {
  return api<ReplicaTask>(`/video-replicas/${task.id}`, { method: 'PATCH', body: JSON.stringify(values) })
}
async function renameTask() {
  if (!selected.value || !renameText.value.trim()) return
  busy.value = true
  try { await metadata(selected.value, { display_name: renameText.value.trim() }); renameOpen.value = false; await refresh() }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '重命名失败' }
  finally { busy.value = false }
}
async function batchAction(action: 'retry' | 'archive') {
  const targets = visibleTasks.value.filter(t => checked.value.includes(t.id) &&
    (action === 'retry' ? ['failed','cancelled'].includes(t.status) : !active(t)))
  if (!targets.length || busy.value) return
  busy.value = true; error.value = ''; notice.value = ''
  const failures: string[] = []
  let completed = 0
  for (const task of targets) {
    try {
      if (action === 'retry') await api(`/tasks/${task.id}/retry`, { method: 'POST' })
      else await metadata(task, { archived: !showArchived.value })
      completed++
    } catch { failures.push(displayName(task)) }
  }
  notice.value = `已处理 ${completed} 项${failures.length ? `，${failures.length} 项失败：${failures.join('、')}` : ''}`
  checked.value = []
  try { await refresh() } catch { error.value = '刷新失败，请稍后重试' }
  finally { busy.value = false }
}
const models = ref<Model[]>([])
const tasks = ref<ReplicaTask[]>([])
const available = ref(false)
const pipelineReady = ref(false)
const loaded = ref(false)
const busy = ref(false)
const error = ref('')
const brief = ref('保留原视频的镜头结构、动作节奏和运镜，复刻为新的画面。')
const transcript = ref('')
const autoTranscribe = ref(true)
const transcriptionLanguage = ref('zh')
const sourceUrl = ref('')
const file = ref<File | null>(null)
const localPreview = ref('')
const textModel = ref('')
const videoModel = ref('')
const imageModel = ref('')
type GenerationLayout = { version: number; units: { start:number; end:number; duration:number; shot_indices:number[]; reference_indices:number[]; continues_previous:boolean; prepare_frame:boolean; use_motion_reference:boolean }[]; warnings:string[]; source_seconds:number; output_seconds:number }
const layout = ref<GenerationLayout | null>(null)
const layoutKey = ref('')
const renderOptions = computed(() => ({
  plan: plan.value, video_model_id: videoModel.value, image_model_id: imageModel.value || null, text_model_id: textModel.value || null,
  aspect_ratio: ratio.value, resolution: resolution.value, audio: audio.value,
  use_reference_frame: useFrame.value, use_reference_video: useVideo.value,
  references: references.value.map(({kind,id,purpose,role,target,shot_indices}) => ({kind,id,purpose,role:role || 'character',target:target || '',shot_indices:shot_indices || []})),
}))
const optionsKey = computed(() => selectedId.value + JSON.stringify(renderOptions.value))
const layoutCurrent = computed(() => layout.value && layoutKey.value === optionsKey.value)
async function previewLayout() {
  if (!pipelineReady.value || !selected.value || !plan.value || busy.value) return
  normalizeModelOptions()
  const id = selectedId.value, key = optionsKey.value
  busy.value = true; error.value = ''
  try {
    const result = await api<GenerationLayout>(`/video-replicas/${id}/render-plan`, {method:'POST',body:JSON.stringify(renderOptions.value)})
    if (id === selectedId.value && key === optionsKey.value) { layout.value = result; layoutKey.value = key }
  } catch(cause) { error.value = cause instanceof Error ? cause.message : '编排检查失败' }
  finally { busy.value = false }
}
async function saveDraft() {
  if (!pipelineReady.value || !selected.value || !plan.value || busy.value) return
  busy.value = true; error.value = ''
  try { await metadata(selected.value, {editor_draft:renderOptions.value}); notice.value = '方案、参考图绑定和参数已保存'; await refresh() }
  catch(cause) { error.value = cause instanceof Error ? cause.message : '草稿保存失败' }
  finally { busy.value = false }
}
function restoreOptions(task: ReplicaTask) {
  const saved = (task.request_payload.editor_draft || task.request_payload.options) as Record<string, any> | undefined
  if (!saved) { textModel.value = String(task.request_payload.text_model_id || textModel.value); return }
  videoModel.value = saved.video_model_id || videoModel.value
  textModel.value = saved.text_model_id || String(task.request_payload.text_model_id || textModel.value)
  imageModel.value = saved.image_model_id || imageModel.value
  ratio.value = saved.aspect_ratio || '16:9'; resolution.value = saved.resolution || '720p'
  audio.value = saved.audio || 'source'
  useFrame.value = saved.use_reference_frame ?? true; useVideo.value = saved.use_reference_video ?? true
}

const ratio = ref('16:9')
const resolution = ref('720p')
const audio = ref('source')
const useFrame = ref(true)
const useVideo = ref(true)
const selectedId = ref('')
const plan = ref<Plan | null>(null)
const references = ref<ReplicaReference[]>([])
const drafts = new Map<string, { plan: Plan | null; references: ReplicaReference[]; options?: Record<string, any> }>()
let selectionVersion = 0
let refreshVersion = 0
const analysisId = computed(() => selected.value?.task_type === 'video_replica_analysis' ? selected.value.id : String(selected.value?.request_payload.analysis_id || ''))
const taskTitle = (task: AITask) => ({video_replica_analysis:'参考分析',video_replica_image:'参考图生成',video_replica_edit:'工程修改',video_replica_export:'工程导出',video_replica_speech:'配音素材'}[task.task_type] || '视频复刻')
const uploadPercent = ref(0)
let controller: AbortController | undefined
let timer: ReturnType<typeof setTimeout> | undefined
let disposed = false
const options = (type: string) => models.value.filter(m => m.type === type).map(m => ({ value: m.id, label: m.name }))
const selected = computed(() => tasks.value.find(t => t.id === selectedId.value))
const hasProduction = computed(() => !!selected.value?.result_payload?.production ||
  selected.value?.task_type === 'video_replica_render' && selected.value?.status === 'succeeded')
const active = (task: AITask) => ['queued', 'running'].includes(task.status)
const statusLabel: Record<string, string> = { queued: '排队中', running: '进行中', succeeded: '已完成', failed: '失败', cancelled: '已停止' }
const video = computed(() => models.value.find(m => m.id === videoModel.value))
const ratios = computed(() => ((video.value?.capabilities.aspect_ratios as string[]) || ['16:9', '9:16', '1:1']).map(value => ({ value, label: value })))
const resolutions = computed(() => {
  const caps = video.value?.capabilities
  const groups = caps?.duration_resolution_map as { resolutions: string[] }[] | undefined
  const values = [...new Set(groups?.flatMap(g => g.resolutions) || caps?.resolutions as string[] || ['720p', '1080p'])]
  return values.map(value => ({ value, label: value }))
})
function normalizeModelOptions() {
  if (!ratios.value.some(r => r.value === ratio.value)) ratio.value = ratios.value[0]?.value || '16:9'
  if (!resolutions.value.some(r => r.value === resolution.value)) resolution.value = resolutions.value[0]?.value || '720p'
}
function chooseFile(event: Event) {
  const chosen = (event.target as HTMLInputElement).files?.[0]
  if (!chosen) return
  if (chosen.size > 100 * 1024 * 1024) { error.value = '参考视频不能超过 100MB'; return }
  if (localPreview.value) URL.revokeObjectURL(localPreview.value)
  file.value = chosen
  sourceUrl.value = ''
  localPreview.value = URL.createObjectURL(chosen)
  error.value = ''
}
function clearFile() {
  if (localPreview.value) URL.revokeObjectURL(localPreview.value)
  localPreview.value = ''; file.value = null
}
async function selectTask(task: ReplicaTask) {
  if (selectedId.value) drafts.set(selectedId.value, { plan: selected.value?.status === 'succeeded' && plan.value ? copyPlan(plan.value) : null, references: references.value.map(r => ({ ...r, shot_indices: [...(r.shot_indices || [])] })), options: JSON.parse(JSON.stringify(renderOptions.value)) })
  const version = ++selectionVersion
  creating.value = false
  section.value = task.task_type === 'video_replica_analysis' ? 'plan' : 'output'
  selectedId.value = task.id
  layout.value = null; layoutKey.value = ''
  const draft = drafts.get(task.id)
  restoreOptions(draft?.options ? { ...task, request_payload: { ...task.request_payload, editor_draft: draft.options } } : task)
  plan.value = draft?.plan ? copyPlan(draft.plan) : taskPlan(task)
  references.value = draft?.references.map(r => ({ ...r })) || ((task.request_payload.editor_reference_snapshots || task.request_payload.reference_snapshots) as ReplicaReference[] || []).map(r => ({ ...r }))
  try {
    const detail = await api<ReplicaTask>(`/tasks/${task.id}`)
    if (disposed || version !== selectionVersion) return
    tasks.value = tasks.value.map(t => t.id === detail.id ? detail : t)
    if (!draft?.plan) {
      plan.value = taskPlan(detail)
      restoreOptions(detail)
      references.value = ((detail.request_payload.editor_reference_snapshots || detail.request_payload.reference_snapshots) as ReplicaReference[] || []).map(r => ({...r,shot_indices:[...(r.shot_indices || [])]}))
    }
  } catch (cause) { if (version === selectionVersion) error.value = cause instanceof Error ? cause.message : '加载任务失败' }
}
async function openAnalysis() {
  try {
    const task = await api<ReplicaTask>(`/tasks/${analysisId.value}`)
    if (!tasks.value.some(t => t.id === task.id)) tasks.value.unshift(task)
    await selectTask(task)
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '加载分析失败' }
}
async function productionTaskCreated(task: AITask) {
  await refresh()
  if (!tasks.value.some(t=>t.id===task.id)) tasks.value.unshift(task as ReplicaTask)
  await selectTask(task as ReplicaTask)
}
async function refresh() {
  const version = ++refreshVersion
  const wasActive = selected.value && active(selected.value)
  const rows = await api<ReplicaTask[]>(`/video-replicas?limit=${listLimit.value}`)
  if (disposed || version !== refreshVersion) return
  if (selected.value && !rows.some(t => t.id === selectedId.value)) {
    // A task can be outside the first page, or actually removed. Never keep a
    // stale failed card indefinitely just because it was previously selected.
    const missingId = selectedId.value
    try {
      const current = await api<ReplicaTask>(`/tasks/${missingId}`)
      if (disposed || version !== refreshVersion) return
      rows.push(current)
    } catch (cause) {
      if (!(cause instanceof ApiError) || cause.status !== 404) throw cause
      if (disposed || version !== refreshVersion) return
      if (selectedId.value === missingId) {
        drafts.delete(missingId); selectedId.value = ''; plan.value = null
        error.value = '该复刻任务已不存在，请选择其他作品。'
      }
    }
  }
  tasks.value = rows
  if (!selectedId.value && !creating.value && rows[0]) selectTask(rows[0])
  if (selected.value && (!plan.value || wasActive || active(selected.value))) plan.value = taskPlan(selected.value)
}
async function poll() {
  try { await refresh() } catch (cause) { if (!disposed) error.value = String(cause instanceof Error ? cause.message : cause) }
  if (!disposed) timer = setTimeout(poll, tasks.value.some(active) ? 4000 : 12000)
}
async function analyze() {
  error.value = ''; busy.value = true; uploadPercent.value = 0
  controller = new AbortController()
  try {
    const body = new FormData()
    body.set('brief', brief.value); body.set('text_model_id', textModel.value); body.set('transcript', transcript.value)
    body.set('auto_transcribe', String(autoTranscribe.value)); body.set('transcription_language', transcriptionLanguage.value)
    if (file.value) body.set('file', file.value)
    else body.set('source_url', sourceUrl.value)
    const result = await apiUpload<ReplicaTask>('/video-replicas', body, controller.signal, value => { uploadPercent.value = value })
    await refresh(); selectTask(result)
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '提交失败' }
  finally { busy.value = false }
}
async function render() {
  normalizeModelOptions()
  if (!plan.value || !selected.value || !layoutCurrent.value) return
  error.value = ''; busy.value = true
  try {
    const result = await api<ReplicaTask>(`/video-replicas/${selected.value.id}/render`, { method: 'POST', body: JSON.stringify(renderOptions.value) })
    await refresh(); selectTask(result)
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '提交失败' }
  finally { busy.value = false }
}
async function taskAction(task: ReplicaTask, action: 'retry' | 'cancel') {
  if (busy.value) return
  busy.value = true; error.value = ''
  try { await api(`/tasks/${task.id}/${action}`, { method: 'POST' }); await refresh() }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '操作失败' }
  finally { busy.value = false }
}
onMounted(async () => {
  try {
    const config = await api<{ available: boolean; models: Model[]; replica_pipeline_version?: number }>('/video-replicas/config')
    if (disposed) return
    available.value = config.available; models.value = config.models
    pipelineReady.value = (config.replica_pipeline_version || 1) >= 2
    textModel.value = models.value.find(m => m.type === 'text' && m.is_default)?.id || options('text')[0]?.value || ''
    videoModel.value = models.value.find(m => m.type === 'video' && m.is_default)?.id || options('video')[0]?.value || ''
    imageModel.value = models.value.find(m => m.type === 'image' && m.is_default)?.id || options('image')[0]?.value || ''
    normalizeModelOptions()
    await poll()
    if (!tasks.value.length) creating.value = true
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '加载失败' }
  finally { loaded.value = true }
})
onBeforeUnmount(() => { disposed = true; clearTimeout(timer); controller?.abort(); clearFile() })
</script>

<template>
  <main class="replica-page">
    <header class="replica-heading">
      <div><span class="replica-eyebrow">CREATIVE STUDIO / 视频创作</span><h1>视频复刻<span class="replica-brand">Hypit</span></h1><p>解构镜头语言，让灵感成为你的作品。</p></div>
      <button class="replica-primary replica-new" @click="creating = true"><Plus :size="18" />新建复刻</button>
    </header>
    <p v-if="error" class="replica-error" role="alert">{{ error }}</p>
    <p v-if="notice" class="replica-hint" role="status">{{ notice }}</p>
    <p v-if="loaded && !available" class="replica-error">本地复刻工具尚未准备好，请安装视频复刻模块依赖。</p>
    <p v-if="loaded && !pipelineReady" class="replica-error" role="status">后端尚未加载新版复刻流程，请重启 API 和任务 Worker 后刷新页面。新版人物绑定、生成编排和方案保存暂未启用。</p>
    <div class="replica-overview"><div v-for="item in stats" :key="item.label"><component :is="item.icon" :size="19" /><span>{{ item.label }}<strong>{{ item.value }}</strong></span></div><small>已加载 {{ tasks.length }} 项任务</small></div>
    <div class="replica-layout">
      <LiquidGlass as="aside" class="replica-panel replica-library" intensity="subtle">
        <div class="replica-section-head"><h2><LayoutGrid :size="17" />任务空间</h2><button :aria-pressed="managing" @click="managing = !managing">{{ managing ? '完成' : '管理' }}</button></div>
        <label class="replica-search"><Search :size="16" /><input v-model="search" type="search" placeholder="搜索已加载任务" aria-label="搜索任务" /></label>
        <div class="replica-filters"><UiSelect v-model="filter" :options="[{value:'all',label:'所有状态'},{value:'active',label:'进行中'},{value:'succeeded',label:'已完成'},{value:'failed',label:'失败'},{value:'cancelled',label:'已停止'}]" /><button :aria-pressed="showArchived" :class="{ 'is-active': showArchived }" @click="showArchived = !showArchived"><Archive :size="16" />{{ showArchived ? '已归档' : '归档箱' }}</button></div>
        <div v-if="managing" class="replica-batch">
          <label><input type="checkbox" :checked="visibleTasks.length > 0 && checked.length === visibleTasks.length" @change="checked = checked.length === visibleTasks.length ? [] : visibleTasks.map(t => t.id)" />全选 · {{ checked.length }}</label>
          <button :disabled="busy || !visibleTasks.some(t => checked.includes(t.id) && ['failed','cancelled'].includes(t.status))" @click="batchAction('retry')">重试失败项</button>
          <button :disabled="busy || !visibleTasks.some(t => checked.includes(t.id) && !active(t))" @click="batchAction('archive')">{{ showArchived ? '移出归档' : '归档已结束项' }}</button>
        </div>
        <div class="replica-task-list">
          <div v-for="task in visibleTasks" :key="task.id" class="replica-task-row" :class="{ selected: !creating && selectedId === task.id }">
            <label v-if="managing" class="replica-task-check"><input v-model="checked" type="checkbox" :value="task.id" :aria-label="'选择 ' + displayName(task)" /></label>
            <button class="replica-task-item" @click="selectTask(task)"><span class="replica-task-top"><span class="replica-task-kind">{{ taskTitle(task) }}</span><span class="replica-dot" :data-status="task.status">{{ statusLabel[task.status] }}</span></span><strong>{{ displayName(task) }}</strong><span class="replica-task-meta">{{ new Date(task.created_at).toLocaleString() }}<span v-if="active(task)">{{ task.progress }}%</span></span><progress v-if="active(task)" :value="task.progress" max="100" /></button>
          </div>
          <p v-if="!visibleTasks.length" class="replica-list-empty">{{ loaded ? '没有匹配的任务' : '正在加载任务…' }}</p>
        </div>
        <button v-if="tasks.length >= listLimit && listLimit < 300" class="replica-load" @click="listLimit = Math.min(300, listLimit + 60); refresh().catch(() => error = '加载失败')">加载更多任务</button>
        <small class="replica-hint">归档仅整理列表，作品与参考资料仍会保留。</small>
      </LiquidGlass>
      <LiquidGlass v-if="creating" as="section" class="replica-panel replica-create" intensity="subtle">
        <div class="replica-section-head"><div><span class="replica-eyebrow">START WITH A REFERENCE</span><h2>从一段好视频开始</h2></div><button v-if="selected" aria-label="返回工作台" @click="creating = false"><X :size="18" /></button></div>
        <div v-if="file" class="replica-source"><video :src="localPreview" controls preload="metadata" /><button type="button" aria-label="移除参考视频" @click="clearFile"><X :size="18" /></button><small>{{ file.name }}</small></div>
        <label v-else class="replica-upload"><Upload :size="26" /><strong>上传参考视频</strong><small>最长 3 分钟 · 最大 100MB</small><input type="file" accept="video/*" @change="chooseFile" /></label>
        <label v-if="!file">或使用视频直链<input v-model="sourceUrl" type="url" placeholder="https://…/video.mp4" /></label>
        <label>复刻要求<textarea v-model="brief" rows="4" maxlength="6000" placeholder="保留哪些镜头？替换人物、产品、台词或画风？" /></label>
        <label class="replica-checkbox"><input v-model="autoTranscribe" type="checkbox" />自动转写原片对白</label>
        <label v-if="autoTranscribe">原片语言<UiSelect v-model="transcriptionLanguage" :options="[{ value: 'zh', label: '中文' }, { value: 'en', label: '英语' }]" /></label>
        <details><summary>台词与补充资料（可选）</summary><textarea v-model="transcript" rows="4" maxlength="10000" placeholder="可填写台词纠正或目标台词，AI 会结合原片转写与画面分析。" /></details>
        <label>分析模型<UiSelect v-model="textModel" :options="options('text')" placeholder="选择支持视觉的模型" /></label>
        <button class="replica-primary" :disabled="busy || !available || !textModel || !brief.trim() || (!file && !sourceUrl.trim())" @click="analyze"><Upload :size="17" />{{ busy ? `正在提交 ${uploadPercent}%` : '分析参考视频' }}</button>
        <small class="replica-hint">先分析并检查方案，再生成新视频。生成使用所选模型平台的额度。</small>
      </LiquidGlass>
      <LiquidGlass v-else as="section" class="replica-panel replica-work" intensity="subtle">
        <div class="replica-section-head"><h2><Film :size="18" />{{ selected ? displayName(selected) : '创作工作台' }}</h2><button v-if="selected" aria-label="重命名任务" title="重命名任务" @click="renameText = displayName(selected); renameOpen = true"><Pencil :size="16" /></button></div>
        <div v-if="!selected" class="replica-empty"><Copy :size="34" /><p>上传视频后，这里会出现镜头分析与复刻方案。</p></div>
        <template v-else>
          <div class="replica-status"><strong>{{ taskTitle(selected) }}</strong><span>{{ statusLabel[selected.status] }} · {{ selected.progress }}%</span></div>
          <p>{{ selected.latest_message }}</p>
          <progress v-if="active(selected)" :value="selected.progress" max="100" />
          <p v-if="selected.error_message" class="replica-error">{{ selected.error_message }}</p>
          <nav class="replica-tabs" aria-label="工作台分区"><button v-for="tab in [{id:'plan',label:'镜头方案'}, ...(selected.task_type === 'video_replica_analysis' ? [{id:'settings',label:'参考与生成'}] : []), {id:'output',label:'成果与进度'}, ...(hasProduction ? [{id:'production',label:'制作工程'}] : [])]" :key="tab.id" :aria-pressed="section === tab.id" :class="{ 'is-active': section === tab.id }" @click="section = tab.id">{{ tab.label }}</button></nav>
          <ReplicaProduction v-if="hasProduction" v-show="section === 'production'" :key="selectedId" :task-id="selectedId" :models="models" @created="productionTaskCreated" />
          <details v-if="section === 'plan' && selected.result_payload?.production_documents"><summary>全片理解与改编方案</summary><p class="replica-summary">{{ selected.result_payload.production_documents['TREATMENT.md'] }}</p><p class="replica-hint">分析先阅读整片，再按动作、叙事和疑问加密取样；检查范围不等于视频生成边界。</p></details>
          <details v-if="section === 'plan' && selected.result_payload?.transcription" class="replica-transcript">
            <summary>原片对白 · {{ selected.result_payload.transcription.passages.length }} 段</summary>
            <small class="replica-hint">自动识别可能存在误差，未对齐的内容会明确标注。</small>
            <div class="replica-transcript-list"><p v-for="(passage, index) in selected.result_payload.transcription.passages" :key="index"><time>{{ passage.start_seconds != null && passage.end_seconds != null ? `${passage.start_seconds.toFixed(2)} — ${passage.end_seconds.toFixed(2)} 秒` : '未对齐时间' }}</time><span>{{ passage.text }}</span></p></div>
            <p v-if="!selected.result_payload.transcription.passages.length">未识别到对白。</p>
          </details>
          <small v-else-if="selected.result_payload?.transcription_skipped" class="replica-hint">{{ selected.result_payload.transcription_skipped }}</small>
          <div class="replica-actions"><button v-if="active(selected)" :disabled="busy" @click="taskAction(selected, 'cancel')">停止任务</button><button v-else-if="['failed','cancelled'].includes(selected.status)" :disabled="busy" @click="taskAction(selected, 'retry')"><RefreshCw :size="16" />继续未完成部分</button></div>
          <button v-if="selected.task_type !== 'video_replica_analysis' && analysisId" @click="openAnalysis">返回对应分析与参考图</button>
          <template v-if="section === 'output' && selected.result_payload?.media_url"><img v-if="selected.task_type === 'video_replica_image'" v-image-preview="selected.result_payload.media_url" class="replica-final" :src="selected.result_payload.media_url" alt="AI 参考图" /><template v-else-if="selected.task_type === 'video_replica_speech'"><audio :src="selected.result_payload.media_url" controls preload="metadata" /><a class="replica-primary" :href="selected.result_payload.media_url" download="配音.wav"><Download :size="17" />下载配音</a></template><template v-else><video class="replica-final" :src="selected.result_payload.media_url" controls preload="metadata" /><a class="replica-primary" :href="selected.result_payload.media_url" download="复刻视频.mp4"><Download :size="17" />下载成片</a></template></template>
          <template v-if="plan && section === 'plan'">
            <p class="replica-summary">{{ plan.summary }}</p>
            <p v-if="selected.task_type === 'video_replica_analysis' && selected.status !== 'succeeded'" class="replica-hint">以下为已保存的部分分析，完成全部分析后可开始复刻。</p>
            <div class="replica-shots" :key="selectedId"><details v-for="(shot, index) in plan.shots" :key="index" :open="index === 0"><summary><span>分析镜头 {{ index + 1 }}</span><time>{{ shot.start.toFixed(1) }} — {{ shot.end.toFixed(1) }} 秒</time></summary><p>{{ shot.observation }}</p><div v-if="selected.task_type === 'video_replica_analysis' && selected.status === 'succeeded'" class="replica-options"><label>场景标识<input v-model="shot.scene_id" maxlength="100" placeholder="同一场景保持一致" /></label><label>与前镜关系<UiSelect :model-value="shot.boundary || 'auto'" @update:model-value="shot.boundary = $event as 'auto' | 'cut' | 'continuous'" :options="[{value:'auto',label:'自动合并（允许切机位）'},{value:'continuous',label:'连续动作（跨段需尾帧）'},{value:'cut',label:'独立生成 / 自然切镜'}]" /></label></div><label>目标动作与提示词<textarea v-model="shot.prompt" :readonly="selected.task_type !== 'video_replica_analysis' || selected.status !== 'succeeded'" rows="5" maxlength="6000" /></label></details></div>
          </template>
          <div v-show="section === 'settings'" v-if="selected.task_type === 'video_replica_analysis'">
            <ReplicaReferences :key="selectedId" v-model="references" :analysis-id="selectedId" :tasks="tasks" :models="models" :shots="plan?.shots" @refresh="refresh" />
            <div class="replica-options"><label>视频模型<UiSelect v-model="videoModel" :options="options('video')" @update:model-value="normalizeModelOptions" /></label><label>画幅<UiSelect v-model="ratio" :options="ratios" /></label><label>分辨率<UiSelect v-model="resolution" :options="resolutions" /></label><label>声音<UiSelect v-model="audio" :options="[{ value: 'source', label: '保留原片声音' }, { value: 'generated', label: '生成新的声音' }, { value: 'silent', label: '无声' }]" /></label></div>
            <label class="replica-checkbox"><input v-model="useFrame" type="checkbox" />未绑定自定义图片时，使用原片画面作为参考帧</label>
            <label class="replica-checkbox"><input v-model="useVideo" type="checkbox" />模型支持时，传入原视频片段参考动作和运镜</label>
            <label>改编提示词模型<UiSelect v-model="textModel" :options="options('text')" placeholder="选择支持图片理解的模型" /></label>
            <label>场景参考图模型（首帧视频模型替换人物时使用）<UiSelect v-model="imageModel" :options="options('image')" placeholder="选择支持多图图生图的模型" /></label>
            <small class="replica-hint">人物图片约束外观，原视频约束动作。仅支持首帧的视频模型会先生成换好人物的场景图；会调用所选图片模型。镜头可在同一次视频请求中切换机位。</small>
            <div class="replica-actions"><button :disabled="!pipelineReady || busy || !plan || selected.status !== 'succeeded'" @click="saveDraft">保存方案与绑定</button><button :disabled="!pipelineReady || busy || !plan || selected.status !== 'succeeded'" @click="previewLayout">预览生成编排</button></div>
            <section v-if="layoutCurrent && layout" class="replica-generation-plan"><h3>{{ plan?.shots.length }} 个分析镜头 → {{ layout.units.length }} 次视频生成</h3><small>原片 {{ layout.source_seconds.toFixed(1) }} 秒 · 预计输出 {{ layout.output_seconds.toFixed(1) }} 秒；最终以生成视频实际时长为准。</small><article v-for="(unit,n) in layout.units" :key="n"><strong>生成段 {{ n+1 }} · {{ unit.duration }} 秒</strong><p>镜头 {{ unit.shot_indices.join('、') }} / 原片 {{ unit.start.toFixed(1) }}–{{ unit.end.toFixed(1) }} 秒</p><small>{{ unit.use_motion_reference ? '原视频动作参考' : '文字动作指导' }} · {{ unit.reference_indices.length }} 张自定义参考{{ unit.prepare_frame ? ' · 先生成目标场景图' : '' }}{{ unit.continues_previous ? ' · 使用上一段尾帧' : '' }}</small></article><p v-for="warning in layout.warnings" :key="warning" class="replica-hint">{{ warning }}</p></section>
            <p v-else class="replica-hint">请先预览生成编排。修改镜头、图片或参数后，需要重新检查。</p>
            <button class="replica-primary" :disabled="busy || selected.status !== 'succeeded' || !videoModel || !plan || !layoutCurrent" @click="render"><Play :size="17" />开始复刻 · {{ layoutCurrent ? layout?.units.length : '—' }} 个生成段</button>
          </div>
          <button v-if="section === 'plan' && plan && selected.task_type === 'video_replica_analysis'" class="replica-primary" @click="section = 'settings'">下一步 · 参考与生成</button>
          <p v-if="section === 'output' && !selected.result_payload?.media_url && !selected.result_payload?.clips" class="replica-list-empty">生成的作品将在这里显示，可随时切换任务查看进度。</p>
          <div v-if="section === 'output' && selected.result_payload?.scene_frames" class="replica-clips"><article v-for="(frame,key) in selected.result_payload.scene_frames" :key="key" class="replica-clip"><header><strong>生成段 {{ Number(key) + 1 }} · 目标场景首帧</strong></header><img v-image-preview="frame.url" :src="frame.url" alt="已替换主体的场景首帧" loading="lazy" class="replica-frame-preview" /></article></div>
          <div v-if="section === 'output' && selected.result_payload?.clips" class="replica-clips"><article v-for="(clip, key) in selected.result_payload.clips" :key="key" class="replica-clip"><header><strong>片段 {{ Number(key) + 1 }}</strong><small>{{ clip.status === 'retrying' ? '正在重试' : statusLabel[clip.status || ''] || '准备中' }}</small></header><video v-if="clip.url" :src="clip.url" controls preload="none" /><p v-if="clip.error" class="replica-error">{{ clip.error }}</p><small v-if="clip.attempts">累计尝试 {{ clip.attempts }} 次</small><details v-if="clip.prompt"><summary>查看实际生成提示词</summary><p class="replica-summary">{{ clip.prompt }}</p></details><a v-if="clip.url" :href="clip.url" download><Download :size="14" />下载片段</a></article></div>
        </template>
      </LiquidGlass>
    </div>
    <BaseDialog v-model:open="renameOpen" title="重命名任务" description="给作品一个便于查找的名字。"><label class="replica-rename">任务名称<input v-model="renameText" maxlength="120" @keydown.enter.prevent="!busy && renameTask()" /></label><template #footer><button :disabled="busy || !renameText.trim()" @click="renameTask">保存名称</button></template></BaseDialog>
  </main>
</template>

<style scoped>
.replica-frame-preview{width:100%;max-height:220px;object-fit:contain;border-radius:12px;cursor:zoom-in}
.replica-generation-plan{padding:16px;background:var(--glass-inset);border-radius:18px;margin:16px 0}.replica-generation-plan h3{font-size:15px;margin:0 0 10px}.replica-generation-plan small{font-size:12px;line-height:1.8;color:var(--ink-secondary)}.replica-generation-plan article{padding:14px 0;border-bottom:1px solid var(--glass-edge)}.replica-generation-plan strong{font-size:13px}.replica-generation-plan article p{font-size:12px;line-height:1.6}.replica-actions{flex-wrap:wrap}

.replica-transcript-list{max-height:280px;overflow-y:auto;overscroll-behavior:contain}.replica-transcript-list p{display:flex;flex-direction:column;gap:6px;line-height:1.7;overflow-wrap:anywhere}.replica-transcript time{font-size:12px;color:var(--text-secondary);font-variant-numeric:tabular-nums}.replica-transcript-list span{white-space:pre-wrap}
.replica-page{--text-primary:var(--ink);--text-secondary:var(--ink-secondary);--border-color:var(--glass-edge);--bg-input:var(--glass-inset);--accent-color:var(--brand);width:100%;max-width:1400px;margin:0 auto;padding:24px;min-height:0;overflow-y:auto;color:var(--ink);box-sizing:border-box}
.replica-heading{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:24px}.replica-heading h1{display:flex;gap:10px;align-items:center;font-size:26px;margin:0}.replica-heading p,.replica-heading>span,.replica-hint{color:var(--text-secondary);font-size:13px}.replica-layout{display:grid;grid-template-columns:minmax(280px,360px) minmax(0,1fr);gap:20px;align-items:start}
.replica-panel{padding:22px;border-radius:24px;min-width:0}.replica-panel h2,.replica-history h2{font-size:16px;margin:0 0 20px}.replica-panel label{display:flex;flex-direction:column;gap:8px;margin:16px 0;font-size:13px}.replica-page input:not([type=checkbox]),.replica-page textarea{width:100%;box-sizing:border-box;border:1px solid var(--border-color, #ffffff30);border-radius:12px;padding:12px;background:var(--bg-input, #80808010);color:inherit;font:inherit;min-width:0}.replica-page textarea{resize:vertical;line-height:1.65}.replica-upload{position:relative;align-items:center;justify-content:center;min-height:170px;border:1px dashed var(--border-color, #80808060);border-radius:16px;cursor:pointer}.replica-upload input{position:absolute;inset:0;opacity:0;cursor:pointer}.replica-source{position:relative}.replica-source video,.replica-final{display:block;width:100%;max-height:420px;border-radius:16px;background:#000}.replica-source button{position:absolute;right:8px;top:8px}.replica-source small{display:block;margin-top:8px;overflow-wrap:anywhere}
.replica-page button,.replica-page a.replica-primary{display:inline-flex;align-items:center;justify-content:center;gap:8px;min-height:44px;border-radius:12px;padding:10px 16px;border:1px solid var(--border-color, #80808040);background:var(--bg-input, #80808018);color:inherit;font:inherit;cursor:pointer;text-decoration:none}.replica-page button:active{transform:scale(.96)}.replica-page button:disabled{opacity:.5;cursor:not-allowed}.replica-page .replica-primary{background:var(--accent-color,#3978ee);color:white;width:100%;box-sizing:border-box;margin-top:16px;border:0}.replica-hint{display:block;margin-top:10px;line-height:1.6}.replica-error{padding:12px;border-radius:12px;background:#dc535318;color:var(--text-primary);overflow-wrap:anywhere;line-height:1.6}.replica-empty{min-height:320px;display:grid;align-content:center;justify-items:center;text-align:center;gap:12px;color:var(--text-secondary)}.replica-status{display:flex;justify-content:space-between;gap:12px;font-variant-numeric:tabular-nums}.replica-work>p{font-size:13px;line-height:1.7;overflow-wrap:anywhere}.replica-work progress{width:100%;height:6px;accent-color:#3978ee}.replica-actions{display:flex;gap:12px;margin:12px 0}.replica-summary{white-space:pre-wrap}.replica-shots{max-height:480px;overflow-y:auto;overscroll-behavior:contain;padding-right:4px}.replica-page details{border-bottom:1px solid var(--border-color,#80808030);padding:10px 0}.replica-page summary{min-height:44px;cursor:pointer;align-content:center;font-size:14px}.replica-shots summary time{float:right;font-size:12px;font-variant-numeric:tabular-nums}.replica-shots details p{font-size:13px;line-height:1.7;color:var(--text-secondary)}.replica-options{display:grid;grid-template-columns:1fr 1fr;gap:0 16px}.replica-panel .replica-checkbox{flex-direction:row;align-items:center;min-height:44px}.replica-checkbox input{width:20px;height:20px}.replica-clips{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;margin-top:20px}.replica-clips video{width:100%;border-radius:12px;max-height:160px}.replica-history{margin-top:28px}.replica-history-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}.replica-history-grid button{flex-direction:column;align-items:start;text-align:left;min-width:0;padding:16px}.replica-history-grid strong{width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px}.replica-history-grid span{font-size:12px;color:var(--text-secondary)}.replica-history-grid .selected{border-color:#3978ee}
@media(max-width:760px){.replica-page{padding:16px 12px}.replica-layout{grid-template-columns:minmax(0,1fr);gap:16px}.replica-panel{padding:16px;border-radius:20px}.replica-heading{align-items:start}.replica-heading h1{font-size:22px}.replica-heading>span{font-size:10px;white-space:nowrap}.replica-options{gap:0 10px}.replica-shots{max-height:55dvh}.replica-empty{min-height:160px}.replica-history-grid{grid-template-columns:minmax(0,1fr)}}

/* Studio surfaces share the site's light/dark glass tokens. */
.replica-clip{padding:12px;border-radius:18px;background:var(--glass-inset);min-width:0}.replica-clip header{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:12px;font-size:12px}.replica-clip small{font-size:11px;color:var(--ink-secondary)}.replica-clip a{display:flex;align-items:center;gap:6px;min-height:44px;color:var(--ink);font-size:12px}.replica-clip .replica-error{font-size:12px;max-height:140px;overflow:auto}
.replica-page{max-width:1560px;padding:28px clamp(14px,3vw,44px);font-variant-numeric:tabular-nums}
.replica-heading h1{font-size:30px;letter-spacing:-.8px;text-wrap:balance;margin-top:8px}
.replica-eyebrow{font-size:10px;font-weight:700;letter-spacing:2px;color:var(--ink-secondary)}
.replica-brand{font-size:11px;letter-spacing:.6px;font-weight:500;padding:5px 9px;border-radius:7px;background:var(--glass-inset);color:var(--ink-secondary)}
.replica-heading .replica-new{width:auto;margin:0;flex-shrink:0;box-shadow:0 6px 20px color-mix(in srgb,var(--brand) 22%,transparent)}
.replica-overview{display:flex;align-items:center;gap:32px;margin:0 0 24px;padding:18px 24px;background:linear-gradient(110deg,color-mix(in srgb,var(--brand) 9%,transparent),var(--glass-inset));border-radius:18px}
.replica-overview>div{display:flex;align-items:center;gap:12px;color:var(--ink-secondary)}.replica-overview span{font-size:12px;display:flex;gap:20px;align-items:center}.replica-overview strong{font-size:23px;font-weight:600;color:var(--ink)}.replica-overview>small{margin-left:auto;color:var(--ink-secondary)}
.replica-layout{grid-template-columns:310px minmax(0,1fr);gap:24px}
.replica-panel{padding:24px;box-shadow:0 8px 32px #00000008,0 1px 3px #00000005}
.replica-library{padding:16px}.replica-section-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:18px}
.replica-section-head h2{display:flex;align-items:center;gap:9px;margin:0;font-size:16px;overflow-wrap:anywhere}.replica-section-head>button{flex-shrink:0;padding:8px 12px;background:transparent;border:0}
.replica-section-head .replica-eyebrow{display:block;margin-bottom:8px}
.replica-panel .replica-search{display:flex;flex-direction:row;align-items:center;background:var(--glass-inset);border-radius:12px;padding-left:12px;margin:12px 0}.replica-search input{border:0!important;background:transparent!important}
.replica-filters{display:grid;grid-template-columns:1fr auto;gap:8px;margin-bottom:12px;min-width:0}.replica-filters button{padding:8px;font-size:12px}
.replica-task-list{max-height:62dvh;overflow-y:auto;overscroll-behavior:contain;scrollbar-width:thin;padding:2px}
.replica-task-row{display:flex;align-items:center;border-radius:14px;margin-bottom:6px;border:1px solid transparent}.replica-task-row:hover{background:var(--glass-inset)}.replica-task-row.selected{background:color-mix(in srgb,var(--brand) 9%,transparent);border-color:color-mix(in srgb,var(--brand) 25%,transparent)}
.replica-page .replica-task-item{flex:1;min-width:0;display:flex;align-items:stretch;flex-direction:column;text-align:left;border:0;background:transparent;padding:14px 12px;gap:10px}.replica-task-item strong{overflow:hidden;white-space:nowrap;text-overflow:ellipsis;font-size:13px;font-weight:600}.replica-task-top,.replica-task-meta{display:flex;justify-content:space-between;gap:8px;font-size:10px;color:var(--ink-secondary)}.replica-task-kind{letter-spacing:.4px}
.replica-dot{display:flex;gap:4px;align-items:center}.replica-dot:before{content:'';width:5px;height:5px;border-radius:50%;background:var(--ink-secondary)}.replica-dot[data-status=running]:before{background:var(--brand)}.replica-dot[data-status=succeeded]:before{background:#2aa881}.replica-dot[data-status=failed]:before{background:#e47c62}
.replica-task-item progress{width:100%;height:3px;accent-color:var(--brand)}
.replica-panel .replica-task-check{margin:0;width:44px;min-height:44px;align-items:center;justify-content:center;flex-shrink:0}.replica-task-check input,.replica-batch input{width:18px;height:18px;accent-color:var(--brand)}
.replica-batch{display:flex;flex-wrap:wrap;gap:6px;padding:10px 0;border-bottom:1px solid var(--glass-edge)}.replica-batch label{width:100%;flex-direction:row;align-items:center;margin:0;min-height:44px}.replica-batch button{font-size:11px;padding:8px}
.replica-list-empty{padding:30px 12px;text-align:center;line-height:1.7;color:var(--ink-secondary);font-size:13px}.replica-load{width:100%}
.replica-tabs{display:flex;gap:8px;padding:6px;background:var(--glass-inset);border-radius:16px;margin:22px 0}.replica-tabs button{flex:1;background:transparent;border:0;font-size:13px;padding:8px;white-space:nowrap}
.replica-page .is-active{background:color-mix(in srgb,var(--brand) 12%,var(--glass-inset));color:var(--ink);box-shadow:0 2px 8px #00000008}
.replica-status{font-size:12px;color:var(--ink-secondary)}.replica-summary{padding:16px 18px;background:var(--glass-inset);border-radius:14px;line-height:1.8;font-size:13px}
.replica-shots{max-height:none;overflow:visible;padding:0}.replica-shots details{padding:12px 0}.replica-shots summary{display:flex;justify-content:space-between;align-items:center;gap:10px}.replica-shots summary>span{font-weight:600}
.replica-create{max-width:850px;width:100%;box-sizing:border-box}.replica-create .replica-upload{min-height:210px;background:radial-gradient(ellipse at center,color-mix(in srgb,var(--brand) 8%,transparent),transparent)}
.replica-rename{display:flex;flex-direction:column;gap:12px}.replica-rename input{padding:12px;border-radius:12px;border:1px solid var(--glass-edge);background:var(--glass-inset);color:var(--ink);width:100%;box-sizing:border-box}
.replica-page button:focus-visible,.replica-page input:focus-visible,.replica-page textarea:focus-visible{outline:2px solid var(--brand);outline-offset:3px}
@media(max-width:1000px){.replica-layout{grid-template-columns:260px minmax(0,1fr);gap:16px}.replica-panel{padding:18px}.replica-library{padding:12px}}
@media(max-width:760px){.replica-page{padding:18px 12px}.replica-heading{align-items:center}.replica-heading h1{font-size:25px}.replica-heading p{font-size:12px;max-width:210px;line-height:1.7}.replica-eyebrow{font-size:8px;letter-spacing:1px}.replica-heading .replica-new{font-size:12px;padding:10px}.replica-overview{gap:12px;flex-wrap:wrap;padding:14px;margin-bottom:16px}.replica-overview>div{flex:1;gap:6px}.replica-overview span{gap:8px;font-size:11px}.replica-overview strong{font-size:20px}.replica-overview>small{width:100%;margin-left:0;font-size:10px}.replica-layout{grid-template-columns:minmax(0,1fr)}.replica-task-list{max-height:240px}.replica-library .replica-section-head{margin-bottom:4px}.replica-work{min-width:0}.replica-options{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}.replica-shots summary{flex-wrap:wrap}.replica-status{flex-wrap:wrap}.replica-tabs{gap:2px}.replica-tabs button{font-size:12px}.replica-brand{display:none}}
</style>
