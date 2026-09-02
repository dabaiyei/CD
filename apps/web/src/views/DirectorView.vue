<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowLeft,
  ArrowDownToLine,
  ArrowUpFromLine,
  AudioLines,
  BadgeCheck,
  BookOpenText,
  BrainCircuit,
  Boxes,
  Camera,
  Check,
  CircleCheckBig,
  ChevronRight,
  Clapperboard,
  Clock3,
  Download,
  CopyPlus,
  FilePenLine,
  FilePlus2,
  FileText,
  Film,
  FolderOpen,
  GitBranchPlus,
  GripHorizontal,
  Headphones,
  History,
  Image,
  Layers3,
  ListTree,
  LoaderCircle,
  MessageSquareText,
  MessageSquareWarning,
  PackageSearch,
  MapPinned,
  Mic2,
  Pause,
  Pencil,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  Search,
  ShieldCheck,
  Sparkles,
  Target,
  Trash2,
  TriangleAlert,
  Upload,
  UserRoundCog,
  UsersRound,
  Volume2,
  WandSparkles,
} from 'lucide-vue-next'

import BaseDialog from '@/components/BaseDialog.vue'
import AgentChatPanel from '@/components/AgentChatPanel.vue'
import AssetLibraryWorkbench from '@/components/AssetLibraryWorkbench.vue'
import ChapterFinishingPanel from '@/components/ChapterFinishingPanel.vue'
import DirectorChapterCanvas from '@/components/DirectorChapterCanvas.vue'
import { api, getToken } from '@/lib/api'
import { useActivityStore } from '@/stores/activity'
import { useProjectsStore } from '@/stores/projects'
import { useToastStore } from '@/stores/toast'
import type {
  AITask,
  AssetExtraction,
  AssetItem,
  AssetRevision,
  AssetType,
  Chapter,
  ChapterAnalysis,
  ChapterStatus,
  DialogueLine,
  DialogueVersion,
  DialogueVersionDetail,
  DirectorWorkflowDetail,
  DubbingOptions,
  PricingRule,
  Project,
  ProjectFileDetail,
  ProjectFileItem,
  ProjectFileKind,
  ScriptVersion,
  ScriptReview,
  ScriptReviewResult,
  SourceMode,
  StoryboardShot,
  StoryboardVersion,
  StoryboardVersionDetail,
  VideoClip,
  VoiceBinding,
} from '@/types'

type WorkflowStep = 'source' | 'script' | 'review' | 'assets' | 'storyboard' | 'video' | 'audio' | 'finish'

const route = useRoute()
const router = useRouter()
const projects = useProjectsStore()
const activity = useActivityStore()
const toast = useToastStore()
const project = ref<Project | null>(null)
const pricingRules = ref<PricingRule[]>([])
const chapters = ref<Chapter[]>([])
const analyses = ref<ChapterAnalysis[]>([])
const selectedAnalysisId = ref('')
const files = ref<ProjectFileItem[]>([])
const selectedChapterId = ref('')
const directorWorkspace = ref<HTMLElement | null>(null)
const directorSplitRatio = ref(0.36)
const directorSplitDragging = ref(false)
const loading = ref(true)
const importOpen = ref(false)
const fileLibraryOpen = ref(false)
const importing = ref(false)
const importMode = ref<SourceMode>('novel')
const inputMode = ref<'upload' | 'paste'>('upload')
const uploadFile = ref<File | null>(null)
const pastedText = ref('')
const sourceName = ref('')
const fileInput = ref<HTMLInputElement | null>(null)
const selectedFile = ref<ProjectFileDetail | null>(null)
const fileDraft = ref('')
const fileNameDraft = ref('')
const newFileKind = ref<'memory' | 'other'>('memory')
const fileSearch = ref('')
const fileSaving = ref(false)
const newFileMode = ref(false)
const fileDeleteTarget = ref<ProjectFileItem | null>(null)
const agentOpen = ref(false)
const assetLibraryOpen = ref(false)
const assetEditorOpen = ref(false)
const assetScope = ref<'project' | 'global'>('project')
const assetTypeFilter = ref<'all' | AssetType>('all')
const projectAssets = ref<AssetItem[]>([])
const globalAssets = ref<AssetItem[]>([])
const assetSaving = ref(false)
const assetRevisionLoading = ref(false)
const assetRestoring = ref(false)
const assetRevisions = ref<AssetRevision[]>([])
const selectedAssetRevisionId = ref('')
const assetDeleteTarget = ref<AssetItem | null>(null)
const selectedAssetIds = ref<string[]>([])
const assetExtractions = ref<AssetExtraction[]>([])
const assetAction = ref<'extract' | 'prompt' | 'image' | ''>('')
const assetForm = reactive({
  id: '',
  asset_type: 'character' as AssetType,
  parent_asset_id: '',
  is_derivative: false,
  name: '',
  description: '',
  generation_prompt: '',
})
const activeWorkflow = ref<WorkflowStep>('source')
const scripts = ref<ScriptVersion[]>([])
const scriptsLoading = ref(false)
const selectedScriptId = ref('')
const scriptEditorOpen = ref(false)
const scriptSaving = ref(false)
const scriptActivateTarget = ref<ScriptVersion | null>(null)
const scriptReviews = ref<ScriptReview[]>([])
const scriptReviewLoading = ref(false)
const scriptReviewAction = ref<'approved' | 'changes_requested' | ''>('')
const scriptReviewNotes = ref('')
const storyboards = ref<StoryboardVersion[]>([])
const storyboardDetail = ref<StoryboardVersionDetail | null>(null)
const selectedStoryboardId = ref('')
const selectedVideoShotId = ref('')
const selectedVideoShotIds = ref<string[]>([])
const storyboardLoading = ref(false)
const storyboardAction = ref<'generate' | 'activate' | 'video' | 'batchVideo' | 'videoPrompt' | 'save' | ''>('')
const shotEditorOpen = ref(false)
const editingShot = ref<StoryboardShot | null>(null)
const dialogues = ref<DialogueVersion[]>([])
const dialogueDetail = ref<DialogueVersionDetail | null>(null)
const selectedDialogueId = ref('')
const dubbingOptions = ref<DubbingOptions>({ tts_models: [], character_assets: [], voice_bindings: [] })
const dubbingLoading = ref(false)
const dubbingAction = ref<'extract' | 'activate' | 'binding' | 'audio' | 'line' | ''>('')
const voiceBindingOpen = ref(false)
const bindingCharacter = ref<AssetItem | null>(null)
const dialogueEditorOpen = ref(false)
const editingDialogueLine = ref<DialogueLine | null>(null)
const playingLineId = ref('')
const legacyWorkspaceVisible = false
let audioPlayer: HTMLAudioElement | null = null
let directorSplitPointerId: number | null = null
let directorSplitStartY = 0
let directorSplitStartRatio = 0.36
const directorSplitDefault = 0.36
const directorSplitHandleHeight = 40
const directorCanvasMinHeight = 148
const directorAgentMinHeight = 220
const voiceForm = reactive({
  tts_model_id: '',
  provider_voice_id: '',
  provider_voice_name: '',
  style: '自然对白',
  instructions: '',
  enabled: true,
})
const dialogueLineForm = reactive({ speaker: '', text: '', emotion: '', direction: '' })
const shotForm = reactive({
  title: '',
  shot_type: '中景',
  duration_seconds: '5',
  scene_description: '',
  action_description: '',
  dialogue: '',
  image_prompt: '',
  video_prompt: '',
})
const agentMode = ref<'analysis' | 'script'>('analysis')
const scriptForm = reactive({
  title: '',
  content: '',
  review_notes: '',
  status: 'draft' as ScriptVersion['status'],
  activate: true,
})

const projectId = computed(() => String(route.params.id))
const directorWorkspaceStyle = computed(() => ({
  '--director-chapter-height': `${(directorSplitRatio.value * 100).toFixed(2)}%`,
}))
const directorSplitPercent = computed(() => Math.round(directorSplitRatio.value * 100))
const selectedChapter = computed(
  () => chapters.value.find((item) => item.id === selectedChapterId.value) ?? chapters.value[0],
)
const selectedScript = computed(
  () => scripts.value.find((item) => item.id === selectedScriptId.value) ?? scripts.value[0] ?? null,
)
const selectedAnalysis = computed(
  () => analyses.value.find((item) => item.id === selectedAnalysisId.value) ?? analyses.value[0] ?? null,
)
const activeScript = computed(() => scripts.value.find((item) => item.is_active) ?? null)
const selectedScriptFormallyApproved = computed(() => Boolean(
  selectedScript.value?.status === 'approved'
  && scriptReviews.value[0]?.decision === 'approved',
))
const chapterAgentPrompt = computed(() =>
  selectedChapter.value
    ? `当前工作章节是《${selectedChapter.value.title}》。请读取本章原文、项目记忆、视觉手册和导演手册，并根据我的要求推进创作。`
    : '',
)
const filteredFiles = computed(() => {
  const keyword = fileSearch.value.trim().toLowerCase()
  return keyword ? files.value.filter((item) => item.name.toLowerCase().includes(keyword)) : files.value
})
const visibleAssets = computed(() => {
  const rows = assetScope.value === 'project' ? projectAssets.value : globalAssets.value
  return assetTypeFilter.value === 'all' ? rows : rows.filter((item) => item.asset_type === assetTypeFilter.value)
})
const derivableAssetTypes: AssetType[] = ['character', 'scene', 'prop']
const assetFormSupportsDerivatives = computed(() => derivableAssetTypes.includes(assetForm.asset_type))
const assetFormParentCandidates = computed(() => {
  const rows = assetScope.value === 'project' ? projectAssets.value : globalAssets.value
  return rows.filter((asset) => (
    asset.id !== assetForm.id
    && asset.asset_type === assetForm.asset_type
    && !asset.parent_asset_id
  ))
})
const assetFormHasChildren = computed(() => {
  if (!assetForm.id) return false
  const rows = assetScope.value === 'project' ? projectAssets.value : globalAssets.value
  return rows.some((asset) => asset.parent_asset_id === assetForm.id)
})
const assetFormValid = computed(() => Boolean(
  assetForm.name.trim()
  && (!assetForm.is_derivative || assetForm.parent_asset_id),
))
const selectedAssetRevision = computed(() => (
  assetRevisions.value.find((revision) => revision.id === selectedAssetRevisionId.value)
  ?? assetRevisions.value[0]
  ?? null
))
const selectedAssets = computed(() => projectAssets.value.filter((asset) => selectedAssetIds.value.includes(asset.id)))
const visibleProjectAssets = computed(() => visibleAssets.value.filter((asset) => asset.scope === 'project'))
const allVisibleAssetsSelected = computed(
  () => Boolean(visibleProjectAssets.value.length)
    && visibleProjectAssets.value.every((asset) => selectedAssetIds.value.includes(asset.id)),
)
const projectAssetTasks = computed(() => activity.tasks.filter(
  (task) => task.project_id === projectId.value
    && ['chapter_asset_extraction', 'asset_prompt_generation', 'asset_image_generation'].includes(task.task_type),
))
const projectChapterAiTasks = computed(() => activity.tasks.filter(
  (task) => task.project_id === projectId.value
    && ['chapter_analysis_generation', 'chapter_script_generation'].includes(task.task_type),
))
const activeChapterAiTasks = computed(() => projectChapterAiTasks.value.filter(
  (task) => task.status === 'queued' || task.status === 'running',
))
const chapterAnalysisTask = computed(() => activeChapterAiTasks.value.find(
  (task) => task.task_type === 'chapter_analysis_generation'
    && task.request_payload.chapter_id === selectedChapter.value?.id,
) ?? null)
const chapterScriptTask = computed(() => activeChapterAiTasks.value.find(
  (task) => task.task_type === 'chapter_script_generation'
    && task.request_payload.chapter_id === selectedChapter.value?.id,
) ?? null)
const activeProjectAssetTasks = computed(() => projectAssetTasks.value.filter(
  (task) => task.status === 'queued' || task.status === 'running',
))
const extractionTask = computed(() => activeProjectAssetTasks.value.find(
  (task) => task.task_type === 'chapter_asset_extraction'
    && task.request_payload.chapter_id === selectedChapter.value?.id,
) ?? null)
const busyAssetIds = computed(() => {
  const ids = new Set<string>()
  activeProjectAssetTasks.value.forEach((task) => {
    if (task.task_type === 'asset_prompt_generation' && Array.isArray(task.request_payload.asset_ids)) {
      task.request_payload.asset_ids.forEach((id) => typeof id === 'string' && ids.add(id))
    }
    const assetId = task.request_payload.asset_id
    if (task.task_type === 'asset_image_generation' && typeof assetId === 'string') ids.add(assetId)
  })
  return ids
})
const promptEligibleAssets = computed(() => selectedAssets.value.filter(
  (asset) => !busyAssetIds.value.has(asset.id) && asset.status !== 'generating',
))
const imageEligibleAssets = computed(() => selectedAssets.value.filter(
  (asset) => !busyAssetIds.value.has(asset.id)
    && asset.asset_type !== 'audio'
    && Boolean(asset.generation_prompt.trim()),
))
const latestExtraction = computed(() => assetExtractions.value[0] ?? null)
function price(taskType: string, fallback = 0): string {
  return Number(
    pricingRules.value.find((rule) => rule.task_type === taskType)?.unit_cost ?? fallback,
  ).toFixed(2)
}
const activeStoryboard = computed(() => storyboards.value.find((item) => item.is_active) ?? null)
const projectStoryboardTasks = computed(() => activity.tasks.filter(
  (task) => task.project_id === projectId.value
    && [
      'chapter_storyboard_generation',
      'shot_video_prompt_generation',
      'shot_video_generation',
    ].includes(task.task_type),
))
const activeStoryboardTasks = computed(() => projectStoryboardTasks.value.filter(
  (task) => task.status === 'queued' || task.status === 'running',
))
const storyboardGenerationTask = computed(() => activeStoryboardTasks.value.find(
  (task) => task.task_type === 'chapter_storyboard_generation'
    && task.request_payload.chapter_id === selectedChapter.value?.id,
) ?? null)
const videoPromptTask = computed(() => activeStoryboardTasks.value.find(
  (task) => task.task_type === 'shot_video_prompt_generation'
    && task.request_payload.storyboard_version_id === storyboardDetail.value?.version.id,
) ?? null)
const storyboardPipelineTask = computed(() => storyboardGenerationTask.value ?? activeProjectAssetTasks.value[0])
const storyboardPipelineLabel = computed(() => {
  const task = storyboardPipelineTask.value
  if (!task) return ''
  if (task.task_type === 'asset_prompt_generation') return '正在补资产提示词'
  if (task.task_type === 'asset_image_generation') return '正在生成资产图'
  return 'AI 正在规划'
})
const busyShotIds = computed(() => new Set(
  activeStoryboardTasks.value
    .filter((task) => task.task_type === 'shot_video_generation')
    .map((task) => task.request_payload.shot_id)
    .filter((id): id is string => typeof id === 'string'),
))
const busyVideoPromptShotIds = computed(() => {
  const ids = new Set<string>()
  activeStoryboardTasks.value
    .filter((task) => task.task_type === 'shot_video_prompt_generation')
    .forEach((task) => {
      const shotIds = task.request_payload.shot_ids
      if (Array.isArray(shotIds)) {
        shotIds.forEach((id) => typeof id === 'string' && ids.add(id))
      }
    })
  return ids
})
const activeClips = computed(() => {
  const clips = new Map<string, VideoClip>()
  storyboardDetail.value?.video_clips.forEach((clip) => {
    const current = clips.get(clip.shot_id)
    if (clip.is_active || (!current && ['queued', 'generating', 'failed'].includes(clip.status))) {
      clips.set(clip.shot_id, clip)
    }
  })
  return clips
})
const clipsByShot = computed(() => {
  const rows = new Map<string, VideoClip[]>()
  storyboardDetail.value?.video_clips.forEach((clip) => {
    const list = rows.get(clip.shot_id) ?? []
    list.push(clip)
    rows.set(clip.shot_id, list)
  })
  rows.forEach((list) => {
    list.sort((a, b) => b.version - a.version)
  })
  return rows
})
const readyVideoCount = computed(() => [...activeClips.value.values()].filter((clip) => clip.is_active && clip.status === 'ready').length)
const selectedVideoShot = computed(() => (
  storyboardDetail.value?.shots.find((shot) => shot.id === selectedVideoShotId.value)
  ?? storyboardDetail.value?.shots[0]
  ?? null
))
const selectedVideoShotAssets = computed(() => (
  selectedVideoShot.value ? shotAssets(selectedVideoShot.value) : []
))
const selectedVideoShotReferences = computed(() => {
  const rows = selectedVideoShotAssets.value
    .filter((asset) => Boolean(asset.media_url))
    .map((asset) => ({
      id: asset.id,
      name: asset.name,
      url: asset.media_url || '',
      type: asset.asset_type,
    }))
  if (selectedVideoShot.value?.reference_image_url) {
    rows.push({
      id: `${selectedVideoShot.value.id}-reference`,
      name: '分镜参考图',
      url: selectedVideoShot.value.reference_image_url,
      type: 'material' as AssetType,
    })
  }
  return rows
})
const allVideoShotsSelected = computed(() => Boolean(storyboardDetail.value?.shots.length)
  && storyboardDetail.value!.shots.every((shot) => selectedVideoShotIds.value.includes(shot.id)))
const selectedVideoShots = computed(() => {
  const shots = storyboardDetail.value?.shots ?? []
  return selectedVideoShotIds.value.length
    ? shots.filter((shot) => selectedVideoShotIds.value.includes(shot.id))
    : shots
})
const videoPromptEligibleShots = computed(() => selectedVideoShots.value.filter(
  (shot) => !busyVideoPromptShotIds.value.has(shot.id),
))
const videoEligibleShots = computed(() => selectedVideoShots.value.filter(
  (shot) => !busyShotIds.value.has(shot.id)
    && Boolean(shot.video_prompt.trim())
    && !(activeClips.value.get(shot.id)?.is_active && activeClips.value.get(shot.id)?.status === 'ready'),
))
const downloadableVideoClips = computed(() => selectedVideoShots.value
  .map((shot) => activeClips.value.get(shot.id))
  .filter((clip): clip is VideoClip => Boolean(clip?.is_active && clip.status === 'ready' && clip.media_url)))
const projectDubbingTasks = computed(() => activity.tasks.filter(
  (task) => task.project_id === projectId.value
    && ['chapter_dialogue_extraction', 'dialogue_tts_generation'].includes(task.task_type),
))
const activeDubbingTasks = computed(() => projectDubbingTasks.value.filter(
  (task) => task.status === 'queued' || task.status === 'running',
))
const dialogueExtractionTask = computed(() => activeDubbingTasks.value.find(
  (task) => task.task_type === 'chapter_dialogue_extraction'
    && task.request_payload.chapter_id === selectedChapter.value?.id,
) ?? null)
const busyDialogueLineIds = computed(() => new Set(
  activeDubbingTasks.value
    .filter((task) => task.task_type === 'dialogue_tts_generation')
    .map((task) => task.request_payload.dialogue_line_id)
    .filter((id): id is string => typeof id === 'string'),
))
const activeAudioClips = computed(() => {
  const clips = new Map<string, DialogueVersionDetail['audio_clips'][number]>()
  dialogueDetail.value?.audio_clips.forEach((clip) => {
    const current = clips.get(clip.dialogue_line_id)
    if (clip.is_active || (!current && ['queued', 'generating', 'failed'].includes(clip.status))) {
      clips.set(clip.dialogue_line_id, clip)
    }
  })
  return clips
})
const readyAudioCount = computed(() => [...activeAudioClips.value.values()].filter(
  (clip) => clip.is_active && clip.status === 'ready',
).length)
const eligibleDialogueLines = computed(() => dialogueDetail.value?.lines.filter(
  (line) => Boolean(bindingForLine(line)) && !busyDialogueLineIds.value.has(line.id),
) ?? [])
const projectAssetTaskSignature = computed(() => projectAssetTasks.value
  .map((task) => `${task.id}:${task.status}:${task.updated_at}`)
  .join('|'))
const storyboardTaskSignature = computed(() => projectStoryboardTasks.value
  .map((task) => `${task.id}:${task.status}:${task.updated_at}`)
  .join('|'))
const dubbingTaskSignature = computed(() => projectDubbingTasks.value
  .map((task) => `${task.id}:${task.status}:${task.updated_at}`)
  .join('|'))
const chapterAiTaskSignature = computed(() => projectChapterAiTasks.value
  .map((task) => `${task.id}:${task.status}:${task.updated_at}`)
  .join('|'))
const chapterStatusLabel: Record<ChapterStatus, string> = {
  uninitialized: '未初始化',
  analyzing: '分析中',
  analyzed: '分析完成',
  scripting: '剧本创作',
  reviewing: '审核中',
  assets: '资产提取',
  storyboard: '分镜制作',
  video: '视频生成',
  completed: '已完成',
}
const fileKindLabel: Record<ProjectFileKind, string> = {
  source: '原始内容',
  memory: '项目记忆',
  analysis: '章节分析',
  script: '剧本',
  asset: '资产',
  storyboard: '分镜',
  video: '视频',
  audio: '音频',
  other: '其它',
}
const workflow: Array<{ id: WorkflowStep; label: string; icon: typeof BookOpenText }> = [
  { id: 'source', label: '原文', icon: BookOpenText },
  { id: 'script', label: '剧本', icon: FilePenLine },
  { id: 'review', label: '审核', icon: Check },
  { id: 'assets', label: '资产', icon: PackageSearch },
  { id: 'storyboard', label: '分镜', icon: Layers3 },
  { id: 'video', label: '视频', icon: Film },
  { id: 'audio', label: '配音', icon: Mic2 },
  { id: 'finish', label: '成片', icon: Clapperboard },
]
const scriptStatusLabel: Record<ScriptVersion['status'], string> = {
  draft: '草稿',
  reviewing: '待审核',
  approved: '已通过',
}
const assetTypes: Array<{ value: 'all' | AssetType; label: string; icon: typeof UsersRound }> = [
  { value: 'all', label: '全部', icon: Boxes },
  { value: 'character', label: '人物', icon: UsersRound },
  { value: 'scene', label: '场景', icon: MapPinned },
  { value: 'prop', label: '道具', icon: PackageSearch },
  { value: 'material', label: '素材', icon: Image },
  { value: 'audio', label: '音频', icon: Headphones },
]
const assetTypeLabel: Record<AssetType, string> = { character: '人物', scene: '场景', prop: '道具', material: '素材', audio: '音频' }
const assetStatusLabel = { extracted: '待生成提示词', prompt_ready: '提示词就绪', generating: '生成中', ready: '已完成', failed: '生成失败' }
const assetRevisionChangeLabel: Record<string, string> = {
  manual_create: '手工创建',
  manual_update: '手工修订',
  manual_extraction: '手工提取',
  ai_extraction: 'AI 提取',
  ai_prompt_generation: 'AI 提示词',
  image_generation: '图片生成',
  library_copy: '资产库复制',
  restore: '历史恢复',
  migration_snapshot: '历史基线',
}
const assetTypeIcon = { character: UsersRound, scene: MapPinned, prop: PackageSearch, material: Image, audio: Headphones }

function directorSplitStorageKey(): string {
  return `cineforge:director-split:v2:${projectId.value}`
}

function directorSplitBounds(): { available: number; min: number; max: number } {
  const workspace = directorWorkspace.value
  if (!workspace) return { available: 1, min: 0.2, max: 0.68 }
  const style = window.getComputedStyle(workspace)
  const verticalPadding = Number.parseFloat(style.paddingTop) + Number.parseFloat(style.paddingBottom)
  const available = Math.max(1, workspace.clientHeight - verticalPadding)
  const min = Math.min(0.45, directorCanvasMinHeight / available)
  const max = Math.max(min, Math.min(0.72, (available - directorSplitHandleHeight - directorAgentMinHeight) / available))
  return { available, min, max }
}

function clampDirectorSplit(value: number): number {
  const { min, max } = directorSplitBounds()
  return Math.min(max, Math.max(min, value))
}

function loadDirectorSplit(): void {
  const stored = Number(window.localStorage.getItem(directorSplitStorageKey()))
  directorSplitRatio.value = Number.isFinite(stored) && stored > 0
    ? Math.min(0.72, Math.max(0.18, stored))
    : directorSplitDefault
}

function saveDirectorSplit(): void {
  window.localStorage.setItem(directorSplitStorageKey(), directorSplitRatio.value.toFixed(4))
}

function startDirectorSplit(event: PointerEvent): void {
  if (event.pointerType === 'mouse' && event.button !== 0) return
  directorSplitPointerId = event.pointerId
  directorSplitStartY = event.clientY
  directorSplitStartRatio = directorSplitRatio.value
  directorSplitDragging.value = true
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
  document.body.classList.add('director-resizing')
  event.preventDefault()
}

function moveDirectorSplit(event: PointerEvent): void {
  if (!directorSplitDragging.value || directorSplitPointerId !== event.pointerId) return
  const { available } = directorSplitBounds()
  directorSplitRatio.value = clampDirectorSplit(
    directorSplitStartRatio + (event.clientY - directorSplitStartY) / available,
  )
  event.preventDefault()
}

function finishDirectorSplit(event?: PointerEvent): void {
  if (event && directorSplitPointerId !== event.pointerId) return
  directorSplitDragging.value = false
  directorSplitPointerId = null
  document.body.classList.remove('director-resizing')
  saveDirectorSplit()
}

function resetDirectorSplit(): void {
  directorSplitRatio.value = clampDirectorSplit(directorSplitDefault)
  saveDirectorSplit()
}

function adjustDirectorSplit(event: KeyboardEvent): void {
  const { available, min, max } = directorSplitBounds()
  const step = (event.shiftKey ? 40 : 12) / available
  if (event.key === 'ArrowUp') directorSplitRatio.value = Math.max(min, directorSplitRatio.value - step)
  else if (event.key === 'ArrowDown') directorSplitRatio.value = Math.min(max, directorSplitRatio.value + step)
  else if (event.key === 'Home') directorSplitRatio.value = min
  else if (event.key === 'End') directorSplitRatio.value = max
  else if (event.key === 'Enter') resetDirectorSplit()
  else return
  saveDirectorSplit()
  event.preventDefault()
}

onMounted(() => {
  loadDirectorSplit()
  void loadWorkspace()
})

onBeforeUnmount(() => {
  document.body.classList.remove('director-resizing')
})

watch(projectId, loadDirectorSplit)

watch(selectedChapterId, async (chapterId) => {
  stopAudio()
  activeWorkflow.value = 'source'
  selectedAssetIds.value = []
  selectedVideoShotId.value = ''
  selectedVideoShotIds.value = []
  await Promise.all([
    loadAnalyses(chapterId),
    loadScripts(chapterId),
    loadAssetExtractions(chapterId),
    loadStoryboards(chapterId),
    loadDialogues(chapterId),
  ])
})

watch(assetScope, (scope) => {
  if (scope === 'global') selectedAssetIds.value = []
})

watch([selectedScriptId, activeWorkflow], ([scriptId, workflowStep]) => {
  if (workflowStep === 'review' && scriptId) void loadScriptReviews(scriptId)
})

watch(storyboardDetail, (detail) => {
  const shots = detail?.shots ?? []
  if (!shots.some((shot) => shot.id === selectedVideoShotId.value)) {
    selectedVideoShotId.value = shots[0]?.id ?? ''
  }
  const availableIds = new Set(shots.map((shot) => shot.id))
  selectedVideoShotIds.value = selectedVideoShotIds.value.filter((id) => availableIds.has(id))
})

watch(projectAssetTaskSignature, async () => {
  await Promise.allSettled([
    refreshAssets(),
    refreshChapters(),
    selectedChapterId.value ? loadAssetExtractions(selectedChapterId.value) : Promise.resolve(),
    api<ProjectFileItem[]>(`/projects/${projectId.value}/files`).then((rows) => { files.value = rows }),
  ])
})

watch(storyboardTaskSignature, async () => {
  if (!selectedChapterId.value) return
  await Promise.allSettled([
    loadStoryboards(selectedChapterId.value),
    refreshChapters(),
    api<ProjectFileItem[]>(`/projects/${projectId.value}/files`).then((rows) => { files.value = rows }),
  ])
})

watch(dubbingTaskSignature, async () => {
  if (!selectedChapterId.value) return
  await Promise.allSettled([
    loadDialogues(selectedChapterId.value),
    loadDubbingOptions(),
    api<ProjectFileItem[]>(`/projects/${projectId.value}/files`).then((rows) => { files.value = rows }),
  ])
})

watch(chapterAiTaskSignature, async () => {
  if (!selectedChapterId.value) return
  await Promise.allSettled([
    loadAnalyses(selectedChapterId.value),
    loadScripts(selectedChapterId.value),
    refreshChapters(),
    api<ProjectFileItem[]>(`/projects/${projectId.value}/files`).then((rows) => { files.value = rows }),
  ])
  const latestScriptTask = projectChapterAiTasks.value.find(
    (task) => task.task_type === 'chapter_script_generation'
      && task.status === 'succeeded'
      && task.request_payload.chapter_id === selectedChapterId.value,
  )
  const generatedScriptId = latestScriptTask?.result_payload?.script_version_id
  if (typeof generatedScriptId === 'string' && scripts.value.some((item) => item.id === generatedScriptId)) {
    selectedScriptId.value = generatedScriptId
    activeWorkflow.value = 'review'
  }
})

async function loadWorkspace(): Promise<void> {
  loading.value = true
  try {
    const [projectRow, chapterRows, fileRows, projectAssetRows, globalAssetRows, dubbingRows, pricingRows] = await Promise.all([
      projects.get(projectId.value),
      api<Chapter[]>(`/projects/${projectId.value}/chapters`),
      api<ProjectFileItem[]>(`/projects/${projectId.value}/files`),
      api<AssetItem[]>(`/projects/${projectId.value}/assets`),
      api<AssetItem[]>('/assets'),
      api<DubbingOptions>(`/projects/${projectId.value}/dubbing/options`),
      api<PricingRule[]>('/pricing'),
    ])
    project.value = projectRow
    chapters.value = chapterRows
    files.value = fileRows
    projectAssets.value = projectAssetRows
    globalAssets.value = globalAssetRows
    dubbingOptions.value = dubbingRows
    pricingRules.value = pricingRows
    if (!selectedChapterId.value && chapterRows[0]) selectedChapterId.value = chapterRows[0].id
    else if (selectedChapterId.value) await loadScripts(selectedChapterId.value)
  } catch (error) {
    toast.show('项目无法打开', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
    await router.replace('/workspace')
  } finally {
    loading.value = false
  }
}

async function refreshProjectFiles(changedProjectId: string): Promise<void> {
  if (changedProjectId !== projectId.value) return
  try {
    files.value = await api<ProjectFileItem[]>(`/projects/${projectId.value}/files`)
    if (!selectedFile.value) return
    const current = files.value.find((item) => item.id === selectedFile.value?.id)
    if (!current) {
      selectedFile.value = null
      fileDraft.value = ''
      fileNameDraft.value = ''
      return
    }
    selectedFile.value = await api<ProjectFileDetail>(
      `/projects/${projectId.value}/files/${current.id}`,
    )
    fileDraft.value = selectedFile.value.content ?? ''
    fileNameDraft.value = selectedFile.value.name
  } catch (error) {
    toast.show('项目文件同步失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  }
}

async function refreshAgentChapter(changedProjectId: string, changedChapterId: string): Promise<void> {
  if (changedProjectId !== projectId.value) return
  try {
    chapters.value = await api<Chapter[]>(`/projects/${projectId.value}/chapters`)
    if (changedChapterId === selectedChapterId.value) {
      await loadScripts(changedChapterId)
    }
  } catch (error) {
    toast.show('章节剧本同步失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  }
}

async function loadAnalyses(chapterId: string): Promise<void> {
  if (!chapterId) {
    analyses.value = []
    selectedAnalysisId.value = ''
    return
  }
  try {
    const rows = await api<ChapterAnalysis[]>(
      `/projects/${projectId.value}/chapters/${chapterId}/analyses`,
    )
    if (selectedChapterId.value !== chapterId) return
    analyses.value = rows
    selectedAnalysisId.value = rows.some((item) => item.id === selectedAnalysisId.value)
      ? selectedAnalysisId.value
      : rows[0]?.id ?? ''
  } catch (error) {
    toast.show('章节分析读取失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  }
}

async function loadScripts(chapterId: string): Promise<void> {
  if (!chapterId) {
    scripts.value = []
    selectedScriptId.value = ''
    return
  }
  scriptsLoading.value = true
  try {
    const rows = await api<ScriptVersion[]>(`/projects/${projectId.value}/chapters/${chapterId}/scripts`)
    if (selectedChapterId.value !== chapterId) return
    scripts.value = rows
    selectedScriptId.value = rows.find((item) => item.is_active)?.id ?? rows[0]?.id ?? ''
  } catch (error) {
    toast.show('剧本版本读取失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    scriptsLoading.value = false
  }
}

async function loadAssetExtractions(chapterId: string): Promise<void> {
  if (!chapterId) {
    assetExtractions.value = []
    return
  }
  try {
    const rows = await api<AssetExtraction[]>(
      `/projects/${projectId.value}/chapters/${chapterId}/asset-extractions`,
    )
    if (selectedChapterId.value === chapterId) assetExtractions.value = rows
  } catch (error) {
    toast.show('资产提取历史读取失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  }
}

async function loadStoryboards(chapterId: string): Promise<void> {
  if (!chapterId) {
    storyboards.value = []
    storyboardDetail.value = null
    selectedStoryboardId.value = ''
    return
  }
  storyboardLoading.value = true
  try {
    const rows = await api<StoryboardVersion[]>(
      `/projects/${projectId.value}/chapters/${chapterId}/storyboards`,
    )
    if (selectedChapterId.value !== chapterId) return
    storyboards.value = rows
    const targetId = rows.some((item) => item.id === selectedStoryboardId.value)
      ? selectedStoryboardId.value
      : rows.find((item) => item.is_active)?.id ?? rows[0]?.id ?? ''
    selectedStoryboardId.value = targetId
    storyboardDetail.value = targetId
      ? await api<StoryboardVersionDetail>(
        `/projects/${projectId.value}/chapters/${chapterId}/storyboards/${targetId}`,
      )
      : null
  } catch (error) {
    toast.show('分镜历史读取失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    storyboardLoading.value = false
  }
}

async function selectStoryboard(storyboardId: string): Promise<void> {
  if (!selectedChapter.value || storyboardId === selectedStoryboardId.value) return
  selectedStoryboardId.value = storyboardId
  storyboardLoading.value = true
  try {
    storyboardDetail.value = await api<StoryboardVersionDetail>(
      `/projects/${projectId.value}/chapters/${selectedChapter.value.id}/storyboards/${storyboardId}`,
    )
  } catch (error) {
    toast.show('分镜版本读取失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    storyboardLoading.value = false
  }
}

async function loadDubbingOptions(): Promise<void> {
  try {
    dubbingOptions.value = await api<DubbingOptions>(`/projects/${projectId.value}/dubbing/options`)
  } catch (error) {
    toast.show('配音配置读取失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  }
}

async function loadDialogues(chapterId: string): Promise<void> {
  if (!chapterId) {
    dialogues.value = []
    dialogueDetail.value = null
    selectedDialogueId.value = ''
    return
  }
  dubbingLoading.value = true
  try {
    const rows = await api<DialogueVersion[]>(
      `/projects/${projectId.value}/chapters/${chapterId}/dialogues`,
    )
    if (selectedChapterId.value !== chapterId) return
    dialogues.value = rows
    const targetId = rows.some((item) => item.id === selectedDialogueId.value)
      ? selectedDialogueId.value
      : rows.find((item) => item.is_active)?.id ?? rows[0]?.id ?? ''
    selectedDialogueId.value = targetId
    dialogueDetail.value = targetId
      ? await api<DialogueVersionDetail>(
        `/projects/${projectId.value}/chapters/${chapterId}/dialogues/${targetId}`,
      )
      : null
  } catch (error) {
    toast.show('台词与配音历史读取失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  } finally {
    dubbingLoading.value = false
  }
}

async function selectDialogue(dialogueId: string): Promise<void> {
  if (!selectedChapter.value || dialogueId === selectedDialogueId.value) return
  stopAudio()
  selectedDialogueId.value = dialogueId
  dubbingLoading.value = true
  try {
    dialogueDetail.value = await api<DialogueVersionDetail>(
      `/projects/${projectId.value}/chapters/${selectedChapter.value.id}/dialogues/${dialogueId}`,
    )
  } catch (error) {
    toast.show('台词版本读取失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    dubbingLoading.value = false
  }
}

async function refreshChapters(): Promise<void> {
  chapters.value = await api<Chapter[]>(`/projects/${projectId.value}/chapters`)
}

function workflowEnabled(step: WorkflowStep): boolean {
  if (step === 'source' || step === 'script') return true
  if (step === 'review') return scripts.value.length > 0
  if (step === 'assets') return Boolean(activeScript.value)
  if (step === 'storyboard') return Boolean(activeScript.value && latestExtraction.value?.is_active)
  if (step === 'video') return Boolean(activeStoryboard.value)
  if (step === 'audio') return Boolean(activeScript.value)
  if (step === 'finish') return Boolean(activeStoryboard.value)
  return false
}

function selectWorkflow(step: WorkflowStep): void {
  if (!workflowEnabled(step)) return
  activeWorkflow.value = step
}

function nextWorkflowEnabled(index: number): boolean {
  const next = workflow[index + 1]
  return next ? workflowEnabled(next.id) : false
}

function openAgent(mode: 'analysis' | 'script'): void {
  agentMode.value = mode
  agentOpen.value = true
}

async function queueChapterAnalysis(): Promise<void> {
  if (!selectedChapter.value || chapterAnalysisTask.value) return
  try {
    await api<AITask>(
      `/projects/${projectId.value}/chapters/${selectedChapter.value.id}/analyses/generate`,
      { method: 'POST' },
    )
    await activity.refresh()
    toast.show(analyses.value.length ? '新分析版本已进入队列' : '章节分析已进入队列', {
      message: '完成后会同步事件、人物、改编策略与项目分析文件',
      tone: 'success',
    })
  } catch (error) {
    toast.show('章节分析任务创建失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  }
}

async function queueScriptGeneration(): Promise<void> {
  if (!selectedChapter.value || chapterScriptTask.value) return
  try {
    await api<AITask>(
      `/projects/${projectId.value}/chapters/${selectedChapter.value.id}/scripts/generate`,
      {
        method: 'POST',
        body: JSON.stringify({
          analysis_id: selectedAnalysis.value?.id ?? null,
          base_script_version_id: selectedScript.value?.id ?? null,
        }),
      },
    )
    activeWorkflow.value = 'script'
    await activity.refresh()
    toast.show(selectedScript.value ? 'AI 剧本新版本已排队' : '首版 AI 剧本已排队', {
      message: selectedScript.value ? '将参考当前选中版本，完成后进入审核历史' : '完成后进入审核，不会自动设为生效稿',
      tone: 'success',
    })
  } catch (error) {
    toast.show('AI 剧本任务创建失败', {
      message: error instanceof Error ? error.message : undefined,
      tone: 'error',
    })
  }
}

function openScriptEditor(base?: ScriptVersion): void {
  Object.assign(scriptForm, {
    title: base?.title ?? `${selectedChapter.value?.title ?? '未命名章节'} · 短剧改编`,
    content: base?.content ?? '',
    review_notes: base?.review_notes ?? '',
    status: base?.status ?? 'draft',
    activate: true,
  })
  scriptEditorOpen.value = true
}

async function saveScriptVersion(): Promise<void> {
  if (!selectedChapter.value || !scriptForm.title.trim() || !scriptForm.content.trim()) return
  scriptSaving.value = true
  try {
    const created = await api<ScriptVersion>(
      `/projects/${projectId.value}/chapters/${selectedChapter.value.id}/scripts`,
      { method: 'POST', body: JSON.stringify(scriptForm) },
    )
    scriptEditorOpen.value = false
    await Promise.all([
      loadScripts(selectedChapter.value.id),
      api<Chapter[]>(`/projects/${projectId.value}/chapters`).then((rows) => { chapters.value = rows }),
    ])
    selectedScriptId.value = created.id
    activeWorkflow.value = created.is_active ? 'review' : 'script'
    toast.show(`剧本 v${created.version} 已保存`, {
      message: created.is_active ? '已设为当前生效版本' : '已加入版本历史',
      tone: 'success',
    })
  } catch (error) {
    toast.show('剧本版本保存失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    scriptSaving.value = false
  }
}

async function loadScriptReviews(scriptId: string): Promise<void> {
  if (!selectedChapter.value) return
  scriptReviewLoading.value = true
  scriptReviews.value = []
  scriptReviewNotes.value = ''
  try {
    const rows = await api<ScriptReview[]>(
      `/projects/${projectId.value}/chapters/${selectedChapter.value.id}/scripts/${scriptId}/reviews`,
    )
    if (selectedScriptId.value !== scriptId) return
    scriptReviews.value = rows
  } catch (error) {
    toast.show('审核记录读取失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    scriptReviewLoading.value = false
  }
}

async function submitScriptReview(decision: 'approved' | 'changes_requested'): Promise<void> {
  if (!selectedChapter.value || !selectedScript.value || scriptReviewAction.value) return
  if (decision === 'changes_requested' && !scriptReviewNotes.value.trim()) {
    toast.show('请填写退回修改的具体意见', { tone: 'error' })
    return
  }
  const target = selectedScript.value
  scriptReviewAction.value = decision
  try {
    const result = await api<ScriptReviewResult>(
      `/projects/${projectId.value}/chapters/${selectedChapter.value.id}/scripts/${target.id}/reviews`,
      {
        method: 'POST',
        body: JSON.stringify({
          decision,
          notes: scriptReviewNotes.value,
          activate: decision === 'approved' && !target.is_active,
        }),
      },
    )
    await Promise.all([
      loadScripts(selectedChapter.value.id),
      loadScriptReviews(target.id),
      loadAssetExtractions(selectedChapter.value.id),
      loadStoryboards(selectedChapter.value.id),
      loadDialogues(selectedChapter.value.id),
      refreshChapters(),
    ])
    selectedScriptId.value = target.id
    toast.show(
      decision === 'approved' ? `剧本 v${target.version} 已通过审核` : `剧本 v${target.version} 已退回修改`,
      {
        message: result.review.activated
          ? '该版本已设为生效稿，旧下游内容已进入历史'
          : '审核决策与意见已写入不可变记录',
        tone: 'success',
      },
    )
  } catch (error) {
    toast.show('剧本审核提交失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    scriptReviewAction.value = ''
  }
}

async function activateScriptVersion(): Promise<void> {
  if (!selectedChapter.value || !scriptActivateTarget.value) return
  const target = scriptActivateTarget.value
  try {
    await api(
      `/projects/${projectId.value}/chapters/${selectedChapter.value.id}/scripts/${target.id}/activate`,
      { method: 'POST' },
    )
    scriptActivateTarget.value = null
    await Promise.all([
      loadScripts(selectedChapter.value.id),
      loadAssetExtractions(selectedChapter.value.id),
      loadStoryboards(selectedChapter.value.id),
      loadDialogues(selectedChapter.value.id),
      api<Chapter[]>(`/projects/${projectId.value}/chapters`).then((rows) => { chapters.value = rows }),
    ])
    selectedScriptId.value = target.id
    toast.show(`已切换为剧本 v${target.version}`, {
      message: '旧剧本关联的资产提取与分镜版本已失效',
      tone: 'success',
    })
  } catch (error) {
    toast.show('剧本切换失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  }
}

async function queueStoryboardGeneration(): Promise<void> {
  if (!selectedChapter.value || storyboardPipelineTask.value) return
  storyboardAction.value = 'generate'
  try {
    const detail = await api<DirectorWorkflowDetail>(
      `/projects/${projectId.value}/chapters/${selectedChapter.value.id}/director-workflow/storyboard`,
      { method: 'POST' },
    )
    await Promise.all([activity.refresh(), refreshAssets()])
    toast.show('导演分镜工作流已启动', {
      message: detail.workflow.last_message || '平台会先检查资产，再生成分镜并进入审核',
      tone: 'success',
    })
  } catch (error) {
    toast.show('分镜工作流启动失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    storyboardAction.value = ''
  }
}

async function activateStoryboardVersion(): Promise<void> {
  if (!selectedChapter.value || !storyboardDetail.value || storyboardDetail.value.version.is_active) return
  storyboardAction.value = 'activate'
  try {
    await api(
      `/projects/${projectId.value}/chapters/${selectedChapter.value.id}/storyboards/${storyboardDetail.value.version.id}/activate`,
      { method: 'POST' },
    )
    await loadStoryboards(selectedChapter.value.id)
    await loadDialogues(selectedChapter.value.id)
    toast.show(`分镜 v${storyboardDetail.value?.version.version} 已生效`, { tone: 'success' })
  } catch (error) {
    toast.show('分镜切换失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    storyboardAction.value = ''
  }
}

function openShotEditor(shot: StoryboardShot): void {
  editingShot.value = shot
  Object.assign(shotForm, {
    title: shot.title,
    shot_type: shot.shot_type,
    duration_seconds: String(shot.duration_seconds),
    scene_description: shot.scene_description,
    action_description: shot.action_description,
    dialogue: shot.dialogue,
    image_prompt: shot.image_prompt,
    video_prompt: shot.video_prompt,
  })
  shotEditorOpen.value = true
}

async function saveShot(): Promise<void> {
  if (!selectedChapter.value || !storyboardDetail.value || !editingShot.value) return
  storyboardAction.value = 'save'
  try {
    await api(
      `/projects/${projectId.value}/chapters/${selectedChapter.value.id}/storyboards/${storyboardDetail.value.version.id}/shots/${editingShot.value.id}`,
      { method: 'PATCH', body: JSON.stringify({ ...shotForm, duration_seconds: Number(shotForm.duration_seconds) }) },
    )
    shotEditorOpen.value = false
    await loadStoryboards(selectedChapter.value.id)
    await loadDialogues(selectedChapter.value.id)
    toast.show('镜头已更新', { message: '旧镜头视频已保留到历史并停止生效', tone: 'success' })
  } catch (error) {
    toast.show('镜头保存失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    storyboardAction.value = ''
  }
}

async function queueShotVideo(shot: StoryboardShot): Promise<void> {
  if (!selectedChapter.value || !storyboardDetail.value || busyShotIds.value.has(shot.id)) return
  storyboardAction.value = 'video'
  try {
    await api<AITask>(
      `/projects/${projectId.value}/chapters/${selectedChapter.value.id}/storyboards/${storyboardDetail.value.version.id}/shots/${shot.id}/videos/generate`,
      { method: 'POST' },
    )
    await activity.refresh()
    toast.show(`镜头 ${String(shot.order_index).padStart(2, '0')} 已进入视频队列`, {
      message: '任务可在通知中心查看进度、失败原因与重试状态',
      tone: 'success',
    })
  } catch (error) {
    toast.show('视频任务创建失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    storyboardAction.value = ''
  }
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
  const ids = storyboardDetail.value?.shots.map((shot) => shot.id) ?? []
  selectedVideoShotIds.value = allVideoShotsSelected.value ? [] : ids
}

function downloadReadyVideos(): void {
  if (!downloadableVideoClips.value.length) return
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

async function queueVideoPrompts(overwrite = true, shotIdsOverride?: string[]): Promise<void> {
  if (!selectedChapter.value || !storyboardDetail.value) return
  const previousSelection = selectedVideoShotIds.value
  if (shotIdsOverride) selectedVideoShotIds.value = shotIdsOverride
  if (!videoPromptEligibleShots.value.length) {
    if (shotIdsOverride) selectedVideoShotIds.value = previousSelection
    return
  }
  storyboardAction.value = 'videoPrompt'
  try {
    await api<AITask>(
      `/projects/${projectId.value}/chapters/${selectedChapter.value.id}/storyboards/${storyboardDetail.value.version.id}/video-prompts/generate`,
      {
        method: 'POST',
        body: JSON.stringify({
          shot_ids: selectedVideoShotIds.value,
          overwrite,
        }),
      },
    )
    await activity.refresh()
    toast.show('镜头视频提示词任务已进入队列', {
      message: `${videoPromptEligibleShots.value.length} 个镜头会读取分镜、资产和项目手册后生成提示词`,
      tone: 'success',
    })
  } catch (error) {
    toast.show('视频提示词任务创建失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    storyboardAction.value = ''
    if (shotIdsOverride) selectedVideoShotIds.value = previousSelection
  }
}

async function queueBatchShotVideos(shotIdsOverride?: string[]): Promise<void> {
  if (!selectedChapter.value || !storyboardDetail.value) return
  const previousSelection = selectedVideoShotIds.value
  if (shotIdsOverride) selectedVideoShotIds.value = shotIdsOverride
  if (!videoEligibleShots.value.length) {
    if (shotIdsOverride) selectedVideoShotIds.value = previousSelection
    return
  }
  storyboardAction.value = 'batchVideo'
  try {
    const tasks = await api<AITask[]>(
      `/projects/${projectId.value}/chapters/${selectedChapter.value.id}/storyboards/${storyboardDetail.value.version.id}/videos/generate`,
      {
        method: 'POST',
        body: JSON.stringify({
          shot_ids: selectedVideoShotIds.value,
          only_missing: true,
        }),
      },
    )
    await activity.refresh()
    toast.show(`${tasks.length} 个镜头已进入视频队列`, {
      message: '任务会按供应商并发限制自动排队，通知中心可查看进度和失败原因',
      tone: 'success',
    })
  } catch (error) {
    toast.show('批量视频任务创建失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    storyboardAction.value = ''
    if (shotIdsOverride) selectedVideoShotIds.value = previousSelection
  }
}

function characterForSpeaker(speaker: string): AssetItem | null {
  return dubbingOptions.value.character_assets.find(
    (asset) => asset.name.trim().toLocaleLowerCase() === speaker.trim().toLocaleLowerCase(),
  ) ?? null
}

function voiceBindingForCharacter(characterId: string): VoiceBinding | null {
  return dubbingOptions.value.voice_bindings.find(
    (binding) => binding.character_asset_id === characterId && binding.enabled,
  ) ?? null
}

function bindingForLine(line: DialogueLine): VoiceBinding | null {
  const character = characterForSpeaker(line.speaker)
  return character ? voiceBindingForCharacter(character.id) : null
}

function openVoiceBinding(character: AssetItem): void {
  const binding = voiceBindingForCharacter(character.id)
  bindingCharacter.value = character
  Object.assign(voiceForm, {
    tts_model_id: binding?.tts_model_id
      ?? dubbingOptions.value.tts_models.find((model) => model.is_default)?.id
      ?? dubbingOptions.value.tts_models[0]?.id
      ?? '',
    provider_voice_id: binding?.provider_voice_id ?? '',
    provider_voice_name: binding?.provider_voice_name ?? '',
    style: binding?.style ?? '自然对白',
    instructions: binding?.instructions ?? '',
    enabled: true,
  })
  voiceBindingOpen.value = true
}

async function saveVoiceBinding(): Promise<void> {
  if (!bindingCharacter.value || !voiceForm.tts_model_id || !voiceForm.provider_voice_id.trim()) return
  dubbingAction.value = 'binding'
  try {
    await api<VoiceBinding>(`/projects/${projectId.value}/dubbing/voice-bindings`, {
      method: 'PUT',
      body: JSON.stringify({ character_asset_id: bindingCharacter.value.id, ...voiceForm }),
    })
    voiceBindingOpen.value = false
    await Promise.all([loadDubbingOptions(), selectedChapter.value ? loadDialogues(selectedChapter.value.id) : Promise.resolve()])
    toast.show(`${bindingCharacter.value.name} 的音色已绑定`, {
      message: '后续配音会锁定当前音色版本，修改后旧音频保留但停止生效',
      tone: 'success',
    })
  } catch (error) {
    toast.show('音色绑定保存失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    dubbingAction.value = ''
  }
}

async function removeVoiceBinding(character: AssetItem): Promise<void> {
  const binding = voiceBindingForCharacter(character.id)
  if (!binding) return
  try {
    await api(`/projects/${projectId.value}/dubbing/voice-bindings/${binding.id}`, { method: 'DELETE' })
    await loadDubbingOptions()
    toast.show(`${character.name} 的音色绑定已移除`, { tone: 'success' })
  } catch (error) {
    toast.show('音色绑定删除失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  }
}

async function queueDialogueExtraction(): Promise<void> {
  if (!selectedChapter.value || dialogueExtractionTask.value) return
  dubbingAction.value = 'extract'
  try {
    await api<AITask>(
      `/projects/${projectId.value}/chapters/${selectedChapter.value.id}/dialogues/generate`,
      { method: 'POST' },
    )
    await activity.refresh()
    toast.show(dialogues.value.length ? '新台词版本已进入队列' : 'AI 台词提取已进入队列', {
      message: '完成后会按角色、情绪和分镜位置建立可追溯台词表',
      tone: 'success',
    })
  } catch (error) {
    toast.show('台词提取任务创建失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    dubbingAction.value = ''
  }
}

async function activateDialogueVersion(): Promise<void> {
  if (!selectedChapter.value || !dialogueDetail.value || dialogueDetail.value.version.is_active) return
  dubbingAction.value = 'activate'
  try {
    await api(
      `/projects/${projectId.value}/chapters/${selectedChapter.value.id}/dialogues/${dialogueDetail.value.version.id}/activate`,
      { method: 'POST' },
    )
    await loadDialogues(selectedChapter.value.id)
    toast.show(`台词 v${dialogueDetail.value?.version.version} 已生效`, { tone: 'success' })
  } catch (error) {
    toast.show('台词版本切换失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    dubbingAction.value = ''
  }
}

async function queueDialogueAudio(lines: DialogueLine[]): Promise<void> {
  if (!selectedChapter.value || !dialogueDetail.value || !lines.length) return
  dubbingAction.value = 'audio'
  try {
    await api<AITask[]>(
      `/projects/${projectId.value}/chapters/${selectedChapter.value.id}/dialogues/${dialogueDetail.value.version.id}/audio/generate`,
      { method: 'POST', body: JSON.stringify({ dialogue_line_ids: lines.map((line) => line.id) }) },
    )
    await activity.refresh()
    toast.show(`${lines.length} 条台词已进入配音队列`, {
      message: '每条台词独立生成，可分别查看进度、失败原因与重试状态',
      tone: 'success',
    })
  } catch (error) {
    toast.show('配音任务创建失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    dubbingAction.value = ''
  }
}

function openDialogueLineEditor(line: DialogueLine): void {
  editingDialogueLine.value = line
  Object.assign(dialogueLineForm, {
    speaker: line.speaker,
    text: line.text,
    emotion: line.emotion,
    direction: line.direction,
  })
  dialogueEditorOpen.value = true
}

async function saveDialogueLine(): Promise<void> {
  if (!selectedChapter.value || !dialogueDetail.value || !editingDialogueLine.value) return
  dubbingAction.value = 'line'
  try {
    await api(
      `/projects/${projectId.value}/chapters/${selectedChapter.value.id}/dialogues/${dialogueDetail.value.version.id}/lines/${editingDialogueLine.value.id}`,
      { method: 'PATCH', body: JSON.stringify(dialogueLineForm) },
    )
    dialogueEditorOpen.value = false
    await loadDialogues(selectedChapter.value.id)
    toast.show('台词已修订', { message: '该台词的旧配音已转入历史版本', tone: 'success' })
  } catch (error) {
    toast.show('台词保存失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    dubbingAction.value = ''
  }
}

function stopAudio(): void {
  audioPlayer?.pause()
  audioPlayer = null
  playingLineId.value = ''
}

function toggleAudio(line: DialogueLine): void {
  const clip = activeAudioClips.value.get(line.id)
  if (!clip?.media_url) return
  if (playingLineId.value === line.id) {
    stopAudio()
    return
  }
  stopAudio()
  audioPlayer = new Audio(clip.media_url)
  playingLineId.value = line.id
  audioPlayer.addEventListener('ended', stopAudio, { once: true })
  void audioPlayer.play().catch(() => {
    stopAudio()
    toast.show('音频暂时无法播放', { tone: 'error' })
  })
}

function shotAssets(shot: StoryboardShot): AssetItem[] {
  return shot.asset_ids
    .map((assetId) => projectAssets.value.find((asset) => asset.id === assetId))
    .filter((asset): asset is AssetItem => Boolean(asset))
}

function openAssetEditor(asset?: AssetItem): void {
  Object.assign(assetForm, {
    id: asset?.id ?? '',
    asset_type: asset?.asset_type ?? 'character',
    parent_asset_id: asset?.parent_asset_id ?? '',
    is_derivative: Boolean(asset?.parent_asset_id),
    name: asset?.name ?? '',
    description: asset?.description ?? '',
    generation_prompt: asset?.generation_prompt ?? '',
  })
  assetLibraryOpen.value = false
  assetEditorOpen.value = true
  assetRevisions.value = []
  selectedAssetRevisionId.value = ''
  if (asset) void loadAssetRevisions(asset.id)
}

async function loadAssetRevisions(assetId: string, selectedId = ''): Promise<void> {
  assetRevisionLoading.value = true
  try {
    assetRevisions.value = await api<AssetRevision[]>(`/assets/${assetId}/revisions`)
    selectedAssetRevisionId.value = selectedId || assetRevisions.value[0]?.id || ''
  } catch (error) {
    toast.show('资产版本读取失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    assetRevisionLoading.value = false
  }
}

async function restoreAssetRevision(): Promise<void> {
  const revision = selectedAssetRevision.value
  if (!assetForm.id || !revision || revision.version === assetRevisions.value[0]?.version) return
  assetRestoring.value = true
  try {
    const restored = await api<AssetItem>(`/assets/${assetForm.id}/revisions/${revision.id}/restore`, {
      method: 'POST',
    })
    Object.assign(assetForm, {
      parent_asset_id: restored.parent_asset_id ?? '',
      is_derivative: Boolean(restored.parent_asset_id),
      name: restored.name,
      description: restored.description,
      generation_prompt: restored.generation_prompt,
    })
    await Promise.all([refreshAssets(), loadAssetRevisions(restored.id)])
    toast.show(`已恢复为新版本 v${restored.version}`, {
      message: `v${revision.version} 保持只读，当前资产已生成新的恢复版本`,
      tone: 'success',
    })
  } catch (error) {
    toast.show('资产版本恢复失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    assetRestoring.value = false
  }
}

function openAssetLibrary(scope: 'project' | 'global' = 'project'): void {
  assetScope.value = scope
  assetLibraryOpen.value = true
}

function toggleAssetSelection(assetId: string): void {
  selectedAssetIds.value = selectedAssetIds.value.includes(assetId)
    ? selectedAssetIds.value.filter((id) => id !== assetId)
    : [...selectedAssetIds.value, assetId]
}

function toggleVisibleAssetSelection(): void {
  const visibleIds = visibleProjectAssets.value.map((asset) => asset.id)
  if (allVisibleAssetsSelected.value) {
    selectedAssetIds.value = selectedAssetIds.value.filter((id) => !visibleIds.includes(id))
  } else {
    selectedAssetIds.value = [...new Set([...selectedAssetIds.value, ...visibleIds])]
  }
}

function isAssetBusy(assetId: string): boolean {
  return busyAssetIds.value.has(assetId)
}

async function queueAssetExtraction(): Promise<void> {
  if (!selectedChapter.value || !activeScript.value || extractionTask.value) return
  assetAction.value = 'extract'
  try {
    await api<AITask>(
      `/projects/${projectId.value}/chapters/${selectedChapter.value.id}/asset-extractions/generate`,
      { method: 'POST' },
    )
    await activity.refresh()
    toast.show('资产提取已进入队列', {
      message: '完成后会自动更新塑造资产库和项目文件',
      tone: 'success',
    })
  } catch (error) {
    toast.show('资产提取任务创建失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    assetAction.value = ''
  }
}

async function queueAssetPrompts(): Promise<void> {
  const assetIds = promptEligibleAssets.value.map((asset) => asset.id)
  if (!assetIds.length) return
  assetAction.value = 'prompt'
  try {
    await api<AITask>(`/projects/${projectId.value}/assets/prompts/generate`, {
      method: 'POST',
      body: JSON.stringify({ asset_ids: assetIds }),
    })
    await activity.refresh()
    toast.show(`${assetIds.length} 个提示词任务已排队`, { tone: 'success' })
  } catch (error) {
    toast.show('提示词任务创建失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    assetAction.value = ''
  }
}

async function queueAssetImages(): Promise<void> {
  const assetIds = imageEligibleAssets.value.map((asset) => asset.id)
  if (!assetIds.length) return
  assetAction.value = 'image'
  try {
    await api<AITask[]>(`/projects/${projectId.value}/assets/images/generate`, {
      method: 'POST',
      body: JSON.stringify({ asset_ids: assetIds }),
    })
    await Promise.all([activity.refresh(), refreshAssets()])
    toast.show(`${assetIds.length} 个资产生图任务已排队`, {
      message: '每个资产独立执行，可分别重试或取消',
      tone: 'success',
    })
  } catch (error) {
    toast.show('生图任务创建失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    assetAction.value = ''
  }
}

function selectAssetType(value: 'all' | AssetType): void {
  if (value === 'all') return
  assetForm.asset_type = value
  assetForm.parent_asset_id = ''
  assetForm.is_derivative = false
}

function setAssetRelation(isDerivative: boolean): void {
  if (isDerivative && (!assetFormSupportsDerivatives.value || assetFormHasChildren.value)) return
  assetForm.is_derivative = isDerivative
  if (!isDerivative) assetForm.parent_asset_id = ''
}

function assetParent(asset: AssetItem): AssetItem | null {
  if (!asset.parent_asset_id) return null
  const rows = asset.scope === 'project' ? projectAssets.value : globalAssets.value
  return rows.find((item) => item.id === asset.parent_asset_id) ?? null
}

function closeAssetEditor(): void {
  assetEditorOpen.value = false
  assetRevisions.value = []
  selectedAssetRevisionId.value = ''
  assetLibraryOpen.value = true
}

function confirmAssetDelete(asset: AssetItem): void {
  assetDeleteTarget.value = asset
  assetLibraryOpen.value = false
}

function closeAssetDelete(): void {
  assetDeleteTarget.value = null
  assetLibraryOpen.value = true
}

async function refreshAssets(): Promise<void> {
  const [projectRows, globalRows] = await Promise.all([
    api<AssetItem[]>(`/projects/${projectId.value}/assets`),
    api<AssetItem[]>('/assets'),
  ])
  projectAssets.value = projectRows
  globalAssets.value = globalRows
  const availableIds = new Set(projectRows.map((asset) => asset.id))
  selectedAssetIds.value = selectedAssetIds.value.filter((id) => availableIds.has(id))
}

function syncAssetRows(projectRows: AssetItem[], globalRows: AssetItem[]): void {
  projectAssets.value = projectRows
  globalAssets.value = globalRows
}

async function saveAsset(): Promise<void> {
  if (!assetFormValid.value) return
  assetSaving.value = true
  try {
    const payload = {
      asset_type: assetForm.asset_type,
      parent_asset_id: assetForm.is_derivative ? assetForm.parent_asset_id : null,
      name: assetForm.name,
      description: assetForm.description,
      generation_prompt: assetForm.generation_prompt,
    }
    if (assetForm.id) {
      await api(`/assets/${assetForm.id}`, {
        method: 'PATCH',
        body: JSON.stringify({
          name: payload.name,
          parent_asset_id: payload.parent_asset_id,
          description: payload.description,
          generation_prompt: payload.generation_prompt,
        }),
      })
    } else {
      await api(assetScope.value === 'project' ? `/projects/${projectId.value}/assets` : '/assets', {
        method: 'POST',
        body: JSON.stringify(payload),
      })
    }
    await refreshAssets()
    closeAssetEditor()
    toast.show('资产已保存', { tone: 'success' })
  } catch (error) {
    toast.show('资产保存失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    assetSaving.value = false
  }
}

async function transferAsset(asset: AssetItem): Promise<void> {
  try {
    const path = asset.scope === 'project'
      ? `/projects/${projectId.value}/assets/${asset.id}/export-global`
      : `/projects/${projectId.value}/assets/import/${asset.id}`
    await api(path, { method: 'POST' })
    await refreshAssets()
    toast.show(asset.scope === 'project' ? '已复制到全局资产库' : '已导入项目塑造资产', {
      message: asset.parent_asset_id ? '基础资产关系已自动复制或复用' : undefined,
      tone: 'success',
    })
  } catch (error) {
    toast.show('资产复制失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  }
}

async function deleteAsset(): Promise<void> {
  if (!assetDeleteTarget.value) return
  try {
    await api(`/assets/${assetDeleteTarget.value.id}`, { method: 'DELETE' })
    await refreshAssets()
    closeAssetDelete()
    toast.show('资产已删除', { tone: 'success' })
  } catch (error) {
    toast.show('资产删除失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  }
}

function chooseUpload(event: Event): void {
  uploadFile.value = (event.target as HTMLInputElement).files?.[0] ?? null
}

async function importSource(): Promise<void> {
  if (inputMode.value === 'upload' && !uploadFile.value) return
  if (inputMode.value === 'paste' && !pastedText.value.trim()) return
  importing.value = true
  try {
    const body = new FormData()
    body.set('mode', importMode.value)
    body.set('source_name', sourceName.value || '粘贴文本')
    if (inputMode.value === 'upload' && uploadFile.value) body.set('file', uploadFile.value)
    else body.set('pasted_text', pastedText.value)
    const result = await api<{ chapters: Chapter[] }>(`/projects/${projectId.value}/sources/import`, {
      method: 'POST',
      body,
    })
    toast.show(`已识别 ${result.chapters.length} 个章节`, {
      message: '章节均以未初始化状态加入导演流程',
      tone: 'success',
    })
    importOpen.value = false
    uploadFile.value = null
    pastedText.value = ''
    sourceName.value = ''
    await loadWorkspace()
    selectedChapterId.value = result.chapters[0]?.id ?? selectedChapterId.value
  } catch (error) {
    toast.show('内容导入失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    importing.value = false
  }
}

async function openFile(item: ProjectFileItem): Promise<void> {
  try {
    const detail = await api<ProjectFileDetail>(`/projects/${projectId.value}/files/${item.id}`)
    selectedFile.value = detail
    fileNameDraft.value = detail.name
    fileDraft.value = detail.content ?? ''
    newFileMode.value = false
  } catch (error) {
    toast.show('文件读取失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  }
}

function startNewFile(): void {
  selectedFile.value = null
  fileNameDraft.value = '项目记忆.md'
  fileDraft.value = ''
  newFileKind.value = 'memory'
  newFileMode.value = true
}

async function saveFile(): Promise<void> {
  if (!fileNameDraft.value.trim()) return
  fileSaving.value = true
  try {
    if (newFileMode.value) {
      const created = await api<ProjectFileDetail>(`/projects/${projectId.value}/files`, {
        method: 'POST',
        body: JSON.stringify({ name: fileNameDraft.value, kind: newFileKind.value, content: fileDraft.value }),
      })
      selectedFile.value = created
      newFileMode.value = false
    } else if (selectedFile.value) {
      selectedFile.value = await api<ProjectFileDetail>(
        `/projects/${projectId.value}/files/${selectedFile.value.id}`,
        { method: 'PUT', body: JSON.stringify({ name: fileNameDraft.value, content: fileDraft.value }) },
      )
    }
    files.value = await api<ProjectFileItem[]>(`/projects/${projectId.value}/files`)
    toast.show('项目文件已保存', { tone: 'success' })
  } catch (error) {
    toast.show('文件保存失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  } finally {
    fileSaving.value = false
  }
}

async function deleteFile(item: ProjectFileItem): Promise<void> {
  try {
    await api(`/projects/${projectId.value}/files/${item.id}?delete_chapters=true`, { method: 'DELETE' })
    files.value = files.value.filter((file) => file.id !== item.id)
    if (selectedFile.value?.id === item.id) selectedFile.value = null
    chapters.value = await api<Chapter[]>(`/projects/${projectId.value}/chapters`)
    if (!chapters.value.some((chapter) => chapter.id === selectedChapterId.value)) {
      selectedChapterId.value = chapters.value[0]?.id ?? ''
    }
    fileDeleteTarget.value = null
    fileLibraryOpen.value = true
    toast.show('项目文件已删除', { tone: 'success' })
  } catch (error) {
    toast.show('文件删除失败', { message: error instanceof Error ? error.message : undefined, tone: 'error' })
  }
}

function confirmFileDelete(item: ProjectFileItem): void {
  fileDeleteTarget.value = item
  fileLibraryOpen.value = false
}

function closeFileDelete(): void {
  fileDeleteTarget.value = null
  fileLibraryOpen.value = true
}

async function downloadFile(item: ProjectFileItem): Promise<void> {
  const response = await fetch(`/api/v1/projects/${projectId.value}/files/${item.id}/download`, {
    headers: { Authorization: `Bearer ${getToken()}` },
  })
  if (!response.ok) {
    toast.show('文件下载失败', { tone: 'error' })
    return
  }
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = item.name
  anchor.click()
  URL.revokeObjectURL(url)
}

function fileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}
</script>

<template>
  <div v-if="loading" class="page-loading"><span class="spinner"></span></div>
  <div v-else-if="project" class="director-page director-workspace">
    <header
      class="director-header director-header--studio director-header--visual"
      :style="{ '--director-cover': `url(${project.cover_url || '/covers/studio-hero-v2.webp'})` }"
    >
      <button class="icon-button" type="button" title="返回创作台" @click="router.push('/workspace')"><ArrowLeft :size="19" /></button>
      <div><span class="eyebrow">DIRECTOR WORKSPACE</span><h1>{{ project.name }}</h1></div>
      <div class="director-header__actions"><button class="button button--secondary" type="button" @click="openAssetLibrary()"><Boxes :size="17" />资产库</button><button class="button button--secondary" type="button" @click="fileLibraryOpen = true"><FolderOpen :size="17" />项目文件</button><button class="button button--primary" type="button" @click="importOpen = true"><Upload :size="17" />导入内容</button></div>
    </header>

    <div v-if="chapters.length" class="director-production">
      <aside class="chapter-rail">
        <header><div><strong>章节</strong><span class="tabular-nums">{{ chapters.length }}</span></div><button class="icon-button icon-button--small" type="button" title="继续导入" @click="importOpen = true"><Plus :size="16" /></button></header>
        <div class="chapter-list">
          <button v-for="(chapter, index) in chapters" :key="chapter.id" v-motion="{ preset: 'row', index }" class="chapter-item" :class="{ active: selectedChapter?.id === chapter.id }" type="button" @click="selectedChapterId = chapter.id"><span class="chapter-item__number tabular-nums">{{ String(chapter.order_index).padStart(2, '0') }}</span><span class="chapter-item__copy"><strong>{{ chapter.title }}</strong><small>{{ chapterStatusLabel[chapter.status] }}</small></span><ChevronRight :size="15" /></button>
        </div>
      </aside>

      <section
        v-if="selectedChapter"
        ref="directorWorkspace"
        class="director-main-workspace"
        :class="{ 'is-resizing': directorSplitDragging }"
        :style="directorWorkspaceStyle"
      >
        <DirectorChapterCanvas
          :chapter="selectedChapter"
          :scripts="scripts"
          :assets="projectAssets"
          :storyboard="storyboardDetail"
          :video-resolution="project.video_resolution"
          :aspect-ratio="project.aspect_ratio"
          :busy-shot-ids="[...busyShotIds]"
          :busy-video-prompt-shot-ids="[...busyVideoPromptShotIds]"
          :video-action="storyboardAction === 'video' || storyboardAction === 'batchVideo' || storyboardAction === 'videoPrompt' ? storyboardAction : ''"
          :video-prompt-task-active="Boolean(videoPromptTask)"
          @open-assets="openAssetLibrary()"
          @queue-video-prompts="(shotIds) => queueVideoPrompts(true, shotIds)"
          @queue-batch-videos="queueBatchShotVideos"
          @queue-shot-video="queueShotVideo"
        />

        <div
          class="director-pane-resizer"
          role="separator"
          aria-label="调整章节内容与 AI 对话区域高度"
          aria-orientation="horizontal"
          aria-valuemin="18"
          aria-valuemax="72"
          :aria-valuenow="directorSplitPercent"
          tabindex="0"
          title="拖动调整高度，双击恢复默认"
          @pointerdown="startDirectorSplit"
          @pointermove="moveDirectorSplit"
          @pointerup="finishDirectorSplit"
          @pointercancel="finishDirectorSplit"
          @lostpointercapture="finishDirectorSplit"
          @dblclick="resetDirectorSplit"
          @keydown="adjustDirectorSplit"
        >
          <span><GripHorizontal :size="17" /></span>
        </div>

        <section class="director-agent-workspace" aria-label="AI 剧本创作助手">
          <AgentChatPanel
            :projects="[project]"
            :pricing="pricingRules"
            :initial-prompt="chapterAgentPrompt"
            :chapter-id="selectedChapter.id"
            :chapter-title="selectedChapter.title"
            scene="director"
            @project-files-changed="refreshProjectFiles"
            @chapter-changed="refreshAgentChapter"
          />
        </section>
      </section>

      <main v-if="legacyWorkspaceVisible && selectedChapter" class="chapter-stage">
        <header class="chapter-stage__header"><div><span>{{ selectedChapter.source_mode === 'novel' ? '小说模式' : '剧本模式' }}</span><h2>{{ selectedChapter.title }}</h2></div><span class="chapter-status" :data-status="selectedChapter.status"><i></i>{{ chapterStatusLabel[selectedChapter.status] }}</span></header>
        <nav class="production-flow" aria-label="章节生产流程">
          <template v-for="(step, index) in workflow" :key="step.id">
            <button type="button" :class="{ active: activeWorkflow === step.id, complete: workflowEnabled(step.id) && activeWorkflow !== step.id }" :disabled="!workflowEnabled(step.id)" @click="selectWorkflow(step.id)"><span><component :is="step.icon" :size="16" /></span><small>{{ step.label }}</small></button>
            <i v-if="index < workflow.length - 1" :class="{ complete: nextWorkflowEnabled(index) }"></i>
          </template>
        </nav>

        <section v-if="activeWorkflow === 'source'" class="analysis-workbench stage-enter">
          <header class="analysis-workbench__header"><div><span class="stage-kicker">SOURCE INTELLIGENCE</span><h3>原文与章节分析</h3><p>让剧本 Agent 提取可核验事件、人物动机、核心冲突和改编策略。</p></div><div><button class="button button--secondary" type="button" @click="openAgent('analysis')"><MessageSquareText :size="16" />自由分析对话</button><button class="button button--primary" type="button" :disabled="Boolean(chapterAnalysisTask)" @click="queueChapterAnalysis"><LoaderCircle v-if="chapterAnalysisTask" class="spin" :size="16" /><RefreshCw v-else-if="analyses.length" :size="16" /><BrainCircuit v-else :size="16" />{{ chapterAnalysisTask ? (chapterAnalysisTask.status === 'running' ? 'AI 正在分析' : '等待执行') : analyses.length ? '生成新分析版本' : 'AI 深度分析' }}<small>{{ price('chapter_analysis_generation', 5) }} 积分</small></button></div></header>
          <div class="analysis-layout">
            <article class="source-reader"><header><div><BookOpenText :size="18" /><span>章节原文</span></div><span class="source-reader__count tabular-nums">{{ selectedChapter.original_content.length.toLocaleString('zh-CN') }} 字</span></header><div>{{ selectedChapter.original_content }}</div></article>
            <section class="analysis-panel">
              <nav v-if="analyses.length" class="analysis-version-tabs" aria-label="分析版本"><span><History :size="14" />分析版本</span><button v-for="item in analyses" :key="item.id" type="button" :class="{ active: selectedAnalysis?.id === item.id }" @click="selectedAnalysisId = item.id"><strong class="tabular-nums">v{{ item.version }}</strong><small>{{ new Date(item.created_at).toLocaleDateString('zh-CN') }}</small></button></nav>
              <div v-if="chapterAnalysisTask" class="analysis-processing"><span><BrainCircuit :size="25" /><i></i></span><strong>{{ chapterAnalysisTask.status === 'running' ? '正在建立章节情报图谱' : '任务已排队' }}</strong><p>AI 会读取原文与项目手册，完成后自动写入分析版本和项目文件。</p><div><i></i><i></i><i></i></div></div>
              <div v-else-if="selectedAnalysis" class="analysis-report">
                <section class="analysis-report__summary"><span><BrainCircuit :size="17" /></span><div><small>章节摘要</small><p>{{ selectedAnalysis.content.summary }}</p></div></section>
                <div class="analysis-signal-grid"><section><span><Target :size="15" />核心冲突</span><p>{{ selectedAnalysis.content.core_conflict }}</p></section><section><span><Sparkles :size="15" />开场钩子</span><p>{{ selectedAnalysis.content.opening_hook }}</p></section></div>
                <section class="analysis-strategy"><header><WandSparkles :size="15" /><strong>短剧改编策略</strong></header><p>{{ selectedAnalysis.content.adaptation_strategy }}</p></section>
                <section class="analysis-events"><header><span><ListTree :size="15" />事件链</span><strong class="tabular-nums">{{ selectedAnalysis.content.events.length }}</strong></header><ol><li v-for="(event, index) in selectedAnalysis.content.events" :key="`${event.title}-${index}`"><span class="tabular-nums">{{ String(index + 1).padStart(2, '0') }}</span><div><strong>{{ event.title }}</strong><p>{{ event.description }}</p><small v-if="event.dramatic_value">{{ event.dramatic_value }}</small></div></li></ol></section>
                <section v-if="selectedAnalysis.content.characters.length" class="analysis-characters"><header><UsersRound :size="15" /><strong>人物与动机</strong></header><div><article v-for="character in selectedAnalysis.content.characters" :key="character.name"><span><UsersRound :size="14" /></span><div><strong>{{ character.name }}</strong><small>{{ character.role }}</small><p>{{ character.motivation }}</p></div></article></div></section>
                <section v-if="selectedAnalysis.content.risks.length" class="analysis-risks"><header><TriangleAlert :size="15" /><strong>审核风险</strong></header><ul><li v-for="risk in selectedAnalysis.content.risks" :key="risk">{{ risk }}</li></ul></section>
              </div>
              <div v-else class="analysis-empty"><span><BrainCircuit :size="28" /></span><strong>原文尚未建立分析版本</strong><p>先让剧本 Agent 提取事件、人物关系和改编风险，再进入剧本创作。</p><button class="button button--primary" type="button" @click="queueChapterAnalysis"><Sparkles :size="16" />分析当前章节</button></div>
            </section>
          </div>
        </section>

        <section v-else-if="activeWorkflow === 'script' || activeWorkflow === 'review'" class="script-workbench stage-enter">
          <header class="script-workbench__header">
            <div><span class="stage-kicker">{{ activeWorkflow === 'review' ? 'SCRIPT REVIEW' : 'SCRIPT VERSIONS' }}</span><h3>{{ activeWorkflow === 'review' ? '审核与生效版本' : '剧本创作历史' }}</h3><p>每次修改创建新版本，历史内容保持可追溯。</p></div>
            <div><button class="button button--secondary" type="button" :disabled="Boolean(chapterScriptTask)" @click="queueScriptGeneration"><LoaderCircle v-if="chapterScriptTask" class="spin" :size="16" /><WandSparkles v-else :size="16" />{{ chapterScriptTask ? 'AI 生成中' : selectedScript ? 'AI 改编新版本' : 'AI 生成剧本' }}<small>{{ price('chapter_script_generation', 10) }} 积分</small></button><button class="button button--secondary" type="button" @click="openAgent('script')"><MessageSquareText :size="16" />自由协作</button><button class="button button--primary" type="button" @click="openScriptEditor(selectedScript ?? undefined)"><CopyPlus :size="16" />{{ selectedScript ? '手工创建版本' : '手工创建剧本' }}</button></div>
          </header>
          <div v-if="scriptsLoading" class="script-loading"><LoaderCircle class="spin" :size="20" />读取版本历史</div>
          <div v-else-if="scripts.length" class="script-version-layout" :class="{ 'script-version-layout--review': activeWorkflow === 'review' }">
            <aside class="script-version-list">
              <header><History :size="15" /><span>版本历史</span><strong class="tabular-nums">{{ scripts.length }}</strong></header>
              <button v-for="item in scripts" :key="item.id" type="button" :class="{ active: selectedScript?.id === item.id }" @click="selectedScriptId = item.id">
                <span class="script-version-number tabular-nums">v{{ item.version }}</span>
                <span><strong>{{ item.title }}</strong><small>{{ scriptStatusLabel[item.status] }} · {{ new Date(item.created_at).toLocaleDateString('zh-CN') }}</small></span>
                <CircleCheckBig v-if="item.is_active" :size="15" />
              </button>
            </aside>
            <article v-if="selectedScript" class="script-preview">
              <header><div><span :data-status="selectedScript.status">{{ scriptStatusLabel[selectedScript.status] }}</span><strong>v{{ selectedScript.version }} {{ selectedScript.title }}</strong></div><div><span v-if="selectedScript.is_active" class="active-version-badge"><ShieldCheck :size="14" />当前生效</span><button v-else-if="activeWorkflow !== 'review'" class="button button--secondary" type="button" @click="scriptActivateTarget = selectedScript"><Check :size="15" />设为生效</button></div></header>
              <div class="script-preview__content">{{ selectedScript.content }}</div>
              <footer v-if="selectedScript.review_notes"><strong>审核备注</strong><p>{{ selectedScript.review_notes }}</p></footer>
            </article>
            <aside v-if="activeWorkflow === 'review' && selectedScript" class="script-review-panel">
              <header><span><BadgeCheck :size="18" /></span><div><strong>审核决策台</strong><small>REVIEW CONTROL</small></div><b class="tabular-nums">v{{ selectedScript.version }}</b></header>
              <section class="script-review-state" :data-status="selectedScript.status"><span><CircleCheckBig v-if="selectedScript.status === 'approved'" :size="20" /><History v-else :size="20" /></span><div><strong>{{ selectedScript.status === 'approved' ? '已通过审核' : selectedScript.status === 'reviewing' ? '等待审核' : '草稿待完善' }}</strong><p>{{ selectedScript.is_active ? '该版本正在驱动后续资产与分镜生产。' : '提交通过后可直接设为章节生效稿。' }}</p></div></section>
              <label class="field script-review-notes"><span>本次审核意见</span><textarea v-model="scriptReviewNotes" rows="5" placeholder="记录节奏、人物动机、台词或可拍摄性意见…"></textarea></label>
              <div class="script-review-actions">
                <button class="script-review-action script-review-action--return" type="button" :disabled="Boolean(scriptReviewAction) || selectedScript.is_active || !scriptReviewNotes.trim()" @click="submitScriptReview('changes_requested')"><LoaderCircle v-if="scriptReviewAction === 'changes_requested'" class="spin" :size="17" /><MessageSquareWarning v-else :size="17" /><span><strong>退回修改</strong><small>{{ selectedScript.is_active ? '生效稿不可直接退回' : '保留版本并回到草稿' }}</small></span></button>
                <button class="script-review-action script-review-action--approve" type="button" :disabled="Boolean(scriptReviewAction) || selectedScriptFormallyApproved" @click="submitScriptReview('approved')"><LoaderCircle v-if="scriptReviewAction === 'approved'" class="spin" :size="17" /><BadgeCheck v-else :size="17" /><span><strong>{{ selectedScriptFormallyApproved ? '已正式通过' : selectedScript.is_active ? '补记通过审核' : '通过并生效' }}</strong><small>{{ selectedScript.is_active ? '写入正式审核记录' : '失效旧下游并进入生产' }}</small></span></button>
              </div>
              <section class="script-review-history"><header><span><History :size="14" />审核轨迹</span><b class="tabular-nums">{{ scriptReviews.length }}</b></header><div v-if="scriptReviewLoading" class="script-review-loading"><LoaderCircle class="spin" :size="17" />读取审核记录</div><div v-else-if="scriptReviews.length"><article v-for="(review, index) in scriptReviews" :key="review.id" :style="{ '--review-index': index }" :data-decision="review.decision"><span><BadgeCheck v-if="review.decision === 'approved'" :size="15" /><MessageSquareWarning v-else :size="15" /></span><div><header><strong>{{ review.decision === 'approved' ? '审核通过' : '退回修改' }}</strong><time>{{ new Date(review.created_at).toLocaleString('zh-CN', { hour12: false }) }}</time></header><p>{{ review.notes || '本次审核未填写补充意见' }}</p><small>{{ review.reviewer_name }}<b v-if="review.activated">已设为生效稿</b></small></div></article></div><div v-else class="script-review-empty"><ShieldCheck :size="22" /><strong>暂无正式审核记录</strong><p>提交决策后会在这里形成不可变审计轨迹。</p></div></section>
            </aside>
          </div>
          <div v-else class="script-empty"><span><FilePenLine :size="27" /></span><strong>{{ chapterScriptTask ? 'AI 正在创作首版剧本' : '还没有剧本版本' }}</strong><p>{{ chapterScriptTask ? '完成后将作为待审核版本进入历史，不会自动设为生效稿。' : '可基于章节分析生成首版，也可以手工创建。' }}</p><div v-if="!chapterScriptTask"><button class="button button--primary" type="button" @click="queueScriptGeneration"><WandSparkles :size="16" />AI 生成首版<small class="tabular-nums">{{ price('chapter_script_generation', 10) }} 积分</small></button><button class="button button--secondary" type="button" @click="openScriptEditor()"><Plus :size="16" />手工创建</button></div><LoaderCircle v-else class="spin" :size="21" /></div>
        </section>

        <section v-else-if="activeWorkflow === 'assets'" class="asset-production stage-enter">
          <header class="asset-production__header">
            <div><span class="stage-kicker">ASSET PIPELINE</span><h3>塑造资产生产线</h3><p>基于生效剧本 v{{ activeScript?.version }}《{{ activeScript?.title }}》，依次完成提取、提示词和定稿图。</p></div>
            <button class="button button--primary" type="button" :disabled="Boolean(extractionTask) || assetAction === 'extract'" @click="queueAssetExtraction">
              <LoaderCircle v-if="extractionTask || assetAction === 'extract'" class="spin" :size="16" /><WandSparkles v-else :size="16" />
              {{ extractionTask ? 'AI 正在提取' : latestExtraction ? '重新提取资产' : 'AI 提取资产' }}
              <small class="tabular-nums">{{ price('chapter_asset_extraction', 8) }} 积分</small>
            </button>
          </header>
          <div class="asset-pipeline">
            <article :data-state="extractionTask ? extractionTask.status : latestExtraction ? 'succeeded' : 'idle'">
              <span class="asset-pipeline__index tabular-nums">01</span><span class="asset-pipeline__icon"><PackageSearch :size="19" /></span>
              <div><strong>剧本资产提取</strong><small v-if="extractionTask">{{ extractionTask.status === 'running' ? '正在分析人物、场景与道具' : '等待可用执行器' }}</small><small v-else-if="latestExtraction">提取版本 v{{ latestExtraction.version }} · {{ latestExtraction.is_active ? '当前生效' : '已失效' }}</small><small v-else>尚未开始 · 消耗 {{ price('chapter_asset_extraction', 8) }} 积分</small></div>
              <CircleCheckBig v-if="latestExtraction && !extractionTask" :size="18" />
              <LoaderCircle v-else-if="extractionTask" class="spin" :size="18" />
            </article>
            <i :class="{ complete: Boolean(projectAssets.length) }"></i>
            <article :data-state="projectAssets.length ? 'succeeded' : 'idle'">
              <span class="asset-pipeline__index tabular-nums">02</span><span class="asset-pipeline__icon"><Sparkles :size="19" /></span>
              <div><strong>生成生图提示词</strong><small><span class="tabular-nums">{{ projectAssets.filter((item) => item.generation_prompt).length }}</span> / {{ projectAssets.length }} 个资产已就绪</small></div>
              <CircleCheckBig v-if="projectAssets.length && projectAssets.every((item) => item.generation_prompt)" :size="18" />
            </article>
            <i :class="{ complete: projectAssets.some((item) => item.status === 'ready') }"></i>
            <article :data-state="projectAssets.some((item) => item.status === 'ready') ? 'succeeded' : 'idle'">
              <span class="asset-pipeline__index tabular-nums">03</span><span class="asset-pipeline__icon"><Image :size="19" /></span>
              <div><strong>资产形象定稿</strong><small><span class="tabular-nums">{{ projectAssets.filter((item) => item.status === 'ready').length }}</span> 个资产已有可用图片</small></div>
              <CircleCheckBig v-if="projectAssets.some((item) => item.status === 'ready')" :size="18" />
            </article>
          </div>
          <div class="asset-production__library">
            <header><div><strong>本项目塑造资产</strong><span>{{ activeProjectAssetTasks.length ? `${activeProjectAssetTasks.length} 个任务处理中` : `${projectAssets.length} 项资产` }}</span></div><button class="button button--secondary" type="button" @click="openAssetLibrary()"><Boxes :size="15" />管理资产与批量生成</button></header>
            <div v-if="projectAssets.length" class="asset-strip">
              <button v-for="asset in projectAssets.slice(0, 5)" :key="asset.id" type="button" @click="openAssetEditor(asset)">
                <span><img v-if="asset.media_url" :src="asset.media_url" :alt="asset.name" /><component :is="assetTypeIcon[asset.asset_type]" v-else :size="20" /><i v-if="isAssetBusy(asset.id)"><LoaderCircle class="spin" :size="13" /></i></span>
                <strong>{{ asset.name }}</strong><small>{{ assetStatusLabel[asset.status] }}</small>
              </button>
            </div>
            <button v-else class="asset-production__empty" type="button" :disabled="Boolean(extractionTask)" @click="queueAssetExtraction"><PackageSearch :size="24" /><span><strong>等待提取首批资产</strong><small>AI 会建立人物、场景、道具及衍生资产 · {{ price('chapter_asset_extraction', 8) }} 积分</small></span><ChevronRight :size="17" /></button>
          </div>
        </section>

        <section v-else-if="activeWorkflow === 'storyboard' || activeWorkflow === 'video'" class="storyboard-workbench stage-enter">
          <header class="storyboard-workbench__header">
            <div><span class="stage-kicker">{{ activeWorkflow === 'video' ? 'SHOT VIDEO PRODUCTION' : 'STORYBOARD TIMELINE' }}</span><h3>{{ activeWorkflow === 'video' ? '单镜头视频生产' : '导演分镜工作台' }}</h3><p>{{ activeWorkflow === 'video' ? `${readyVideoCount} / ${storyboardDetail?.shots.length || 0} 个镜头已有生效视频` : '分镜版本独立留档，镜头编辑后自动失效旧视频。' }}</p></div>
            <button class="button button--primary" type="button" :disabled="Boolean(storyboardPipelineTask) || storyboardAction === 'generate'" @click="queueStoryboardGeneration"><LoaderCircle v-if="storyboardPipelineTask || storyboardAction === 'generate'" class="spin" :size="16" /><RefreshCw v-else-if="storyboards.length" :size="16" /><WandSparkles v-else :size="16" />{{ storyboardPipelineTask ? storyboardPipelineLabel : storyboards.length ? '生成新分镜版本' : 'AI 生成分镜' }}<small>{{ storyboardPipelineTask?.latest_message || `${price('chapter_storyboard_generation', 12)} 积分` }}</small></button>
          </header>

          <div v-if="storyboards.length" class="storyboard-version-bar">
            <span><History :size="15" />版本历史</span>
            <div><button v-for="item in storyboards" :key="item.id" type="button" :class="{ active: selectedStoryboardId === item.id }" @click="selectStoryboard(item.id)"><strong class="tabular-nums">v{{ item.version }}</strong><small>{{ item.is_active ? '当前生效' : item.invalidated_reason ? '历史版本' : '可切换' }}</small><CircleCheckBig v-if="item.is_active" :size="14" /></button></div>
            <button v-if="storyboardDetail && !storyboardDetail.version.is_active" class="button button--secondary" type="button" :disabled="storyboardAction === 'activate'" @click="activateStoryboardVersion"><Check :size="15" />设为生效</button>
          </div>

          <div v-if="storyboardLoading" class="storyboard-loading"><LoaderCircle class="spin" :size="20" />正在同步分镜与视频状态</div>
          <div v-else-if="storyboardDetail?.shots.length && activeWorkflow === 'storyboard'" class="shot-grid">
            <article v-for="(shot, index) in storyboardDetail.shots" :key="shot.id" v-motion="{ preset: 'card', index }" class="shot-card">
              <div class="shot-card__visual">
                <video v-if="activeClips.get(shot.id)?.is_active && activeClips.get(shot.id)?.media_url" :src="activeClips.get(shot.id)?.media_url || undefined" controls preload="metadata"></video>
                <img v-else-if="shot.reference_image_url" :src="shot.reference_image_url" :alt="shot.title" />
                <span v-else><Camera :size="28" /><small>等待镜头画面</small></span>
                <b class="shot-card__number tabular-nums">{{ String(shot.order_index).padStart(2, '0') }}</b>
                <i v-if="busyShotIds.has(shot.id)" class="shot-card__status"><LoaderCircle class="spin" :size="14" />{{ activeClips.get(shot.id)?.status === 'generating' ? '视频生成中' : '视频排队中' }}</i>
                <i v-else-if="activeClips.get(shot.id)?.status === 'failed'" class="shot-card__status shot-card__status--failed">生成失败</i>
              </div>
              <div class="shot-card__content">
                <header><div><strong>{{ shot.title }}</strong><small>镜头稿 v{{ shot.version }}</small></div><button class="icon-button icon-button--small" type="button" title="编辑镜头" :disabled="!storyboardDetail.version.is_active" @click="openShotEditor(shot)"><Pencil :size="14" /></button></header>
                <div class="shot-card__meta"><span><Camera :size="13" />{{ shot.shot_type }}</span><span><Clock3 :size="13" /><b class="tabular-nums">{{ shot.duration_seconds }}</b> 秒</span></div>
                <p>{{ shot.scene_description }}</p><p class="shot-card__action">{{ shot.action_description }}</p>
                <blockquote v-if="shot.dialogue"><MessageSquareText :size="14" />{{ shot.dialogue }}</blockquote>
                <div v-if="shotAssets(shot).length" class="shot-assets"><span v-for="asset in shotAssets(shot)" :key="asset.id"><img v-if="asset.media_url" :src="asset.media_url" :alt="asset.name" /><component :is="assetTypeIcon[asset.asset_type]" v-else :size="12" />{{ asset.name }}</span></div>
              </div>
              <footer><span v-if="activeClips.get(shot.id)?.is_active"><CircleCheckBig :size="14" />视频 v{{ activeClips.get(shot.id)?.version }} 已生效</span><span v-else><Film :size="14" />尚无生效视频</span><button class="button" :class="activeClips.get(shot.id)?.is_active ? 'button--secondary' : 'button--primary'" type="button" :disabled="!storyboardDetail.version.is_active || busyShotIds.has(shot.id) || !shot.video_prompt" @click="queueShotVideo(shot)"><LoaderCircle v-if="busyShotIds.has(shot.id)" class="spin" :size="15" /><RefreshCw v-else-if="activeClips.get(shot.id)?.is_active" :size="15" /><Play v-else :size="15" />{{ busyShotIds.has(shot.id) ? '处理中' : activeClips.get(shot.id)?.is_active ? '重新生成' : '生成视频' }}<small>{{ price('shot_video_generation', 60) }} 积分</small></button></footer>
            </article>
          </div>
          <section v-else-if="storyboardDetail?.shots.length && activeWorkflow === 'video'" class="video-production-console">
            <div class="video-reference-strip">
              <button
                v-for="reference in selectedVideoShotReferences"
                :key="reference.id"
                type="button"
                class="video-reference-tile"
                :title="reference.name"
              >
                <img :src="reference.url" :alt="reference.name" />
                <span>{{ reference.name }}</span>
              </button>
              <button class="video-reference-tile video-reference-tile--empty" type="button" @click="openAssetLibrary()">
                <Plus :size="22" />
                <span>添加参考</span>
              </button>
              <div class="video-model-spec">
                <span><Film :size="14" />{{ project.video_resolution || '默认分辨率' }}</span>
                <span><Camera :size="14" />{{ project.aspect_ratio }}</span>
                <span><Clock3 :size="14" />{{ selectedVideoShot?.duration_seconds || 0 }}s</span>
              </div>
            </div>

            <div class="video-stage-grid">
              <article class="video-prompt-panel">
                <header>
                  <div><span class="tabular-nums">#{{ selectedVideoShot?.order_index || 0 }}</span><strong>生成提示词</strong></div>
                  <button class="button button--primary" type="button" :disabled="!selectedVideoShot || busyVideoPromptShotIds.has(selectedVideoShot.id) || storyboardAction === 'videoPrompt'" @click="selectedVideoShot && queueVideoPrompts(true, [selectedVideoShot.id])">
                    <LoaderCircle v-if="selectedVideoShot && busyVideoPromptShotIds.has(selectedVideoShot.id)" class="spin" :size="15" />
                    <WandSparkles v-else :size="15" />
                    生成提示词
                  </button>
                </header>
                <div class="video-prompt-copy">
                  <strong>{{ selectedVideoShot?.title }}</strong>
                  <p>{{ selectedVideoShot?.video_prompt || '当前镜头还没有视频提示词，可先生成提示词再发起视频任务。' }}</p>
                </div>
              </article>

              <article class="video-output-panel">
                <header>
                  <div><span class="tabular-nums">#{{ selectedVideoShot?.order_index || 0 }}</span><strong>生成视频</strong></div>
                  <button class="button button--primary" type="button" :disabled="!selectedVideoShot || !selectedVideoShot.video_prompt || busyShotIds.has(selectedVideoShot.id) || storyboardAction === 'video'" @click="selectedVideoShot && queueShotVideo(selectedVideoShot)">
                    <LoaderCircle v-if="selectedVideoShot && busyShotIds.has(selectedVideoShot.id)" class="spin" :size="15" />
                    <Play v-else :size="15" />
                    生成视频
                  </button>
                </header>
                <div v-if="selectedVideoShot && activeClips.get(selectedVideoShot.id)?.media_url" class="video-output-preview">
                  <video :src="activeClips.get(selectedVideoShot.id)?.media_url || undefined" controls preload="metadata"></video>
                </div>
                <div v-else class="video-output-empty">
                  <Film :size="28" />
                  <strong>{{ selectedVideoShot && busyShotIds.has(selectedVideoShot.id) ? '视频任务处理中' : '等待生成视频' }}</strong>
                  <span>{{ selectedVideoShot ? activeClips.get(selectedVideoShot.id)?.error_message || '生成成功后会显示历史版本和可播放预览。' : '请选择一个镜头。' }}</span>
                </div>
                <section class="video-version-history">
                  <span><History :size="14" />历史版本（{{ selectedVideoShot ? clipsByShot.get(selectedVideoShot.id)?.length || 0 : 0 }}）</span>
                  <div v-if="selectedVideoShot && clipsByShot.get(selectedVideoShot.id)?.length">
                    <article v-for="clip in clipsByShot.get(selectedVideoShot.id)" :key="clip.id" :data-status="clip.status">
                      <video v-if="clip.media_url" :src="clip.media_url" preload="metadata"></video>
                      <Film v-else :size="18" />
                      <strong class="tabular-nums">v{{ clip.version }}</strong>
                      <small>{{ clip.status === 'ready' ? '已完成' : clip.status === 'failed' ? '失败' : clip.status === 'generating' ? '生成中' : '排队中' }}</small>
                    </article>
                  </div>
                </section>
              </article>
            </div>

            <footer class="video-shot-dock">
              <div class="video-shot-dock__toolbar">
                <button type="button" class="video-check" :aria-pressed="allVideoShotsSelected" @click="toggleAllVideoShots"><Check :size="13" />{{ allVideoShotsSelected ? '取消全选' : '全选' }}</button>
                <span><strong class="tabular-nums">{{ selectedVideoShotIds.length || storyboardDetail.shots.length }}</strong> 个镜头待处理</span>
                <div>
                  <button class="button button--secondary" type="button" :disabled="!downloadableVideoClips.length" @click="downloadReadyVideos"><Download :size="15" />批量下载视频</button>
                  <button class="button button--secondary" type="button" :disabled="!videoPromptEligibleShots.length || Boolean(storyboardAction) || Boolean(videoPromptTask)" @click="queueVideoPrompts(true)"><LoaderCircle v-if="storyboardAction === 'videoPrompt' || videoPromptTask" class="spin" :size="15" /><WandSparkles v-else :size="15" />批量生成提示词</button>
                  <button class="button button--primary" type="button" :disabled="!videoEligibleShots.length || Boolean(storyboardAction)" @click="() => queueBatchShotVideos()"><LoaderCircle v-if="storyboardAction === 'batchVideo'" class="spin" :size="15" /><Play v-else :size="15" />批量生成视频</button>
                </div>
              </div>
              <div class="video-shot-rail">
                <article
                  v-for="shot in storyboardDetail.shots"
                  :key="shot.id"
                  :class="{ active: selectedVideoShot?.id === shot.id, selected: selectedVideoShotIds.includes(shot.id) }"
                  @click="selectVideoShot(shot)"
                >
                  <button type="button" :aria-pressed="selectedVideoShotIds.includes(shot.id)" @click.stop="toggleVideoShotSelection(shot.id)"><Check :size="13" /></button>
                  <span>
                    <video v-if="activeClips.get(shot.id)?.media_url" :src="activeClips.get(shot.id)?.media_url || undefined" preload="metadata"></video>
                    <img v-else-if="shot.reference_image_url" :src="shot.reference_image_url" :alt="shot.title" />
                    <img v-else-if="shotAssets(shot)[0]?.media_url" :src="shotAssets(shot)[0]?.media_url || undefined" :alt="shot.title" />
                    <Camera v-else :size="20" />
                  </span>
                  <b class="tabular-nums">#{{ shot.order_index }}</b>
                  <small>{{ busyShotIds.has(shot.id) ? '视频中' : busyVideoPromptShotIds.has(shot.id) ? '提示词中' : activeClips.get(shot.id)?.is_active ? '已完成' : shot.video_prompt ? '待生成' : '缺提示词' }}</small>
                </article>
              </div>
            </footer>
          </section>
          <div v-else class="storyboard-empty"><span><Layers3 :size="29" /></span><strong>{{ storyboardPipelineTask ? storyboardPipelineLabel : '还没有分镜版本' }}</strong><p>{{ storyboardPipelineTask ? storyboardPipelineTask.latest_message || '平台正在推进分镜前置任务，完成后会继续生成分镜。' : '基于当前生效剧本与塑造资产生成第一版导演分镜。' }}</p><button v-if="!storyboardPipelineTask" class="button button--primary" type="button" @click="queueStoryboardGeneration"><WandSparkles :size="16" />生成首版分镜<small class="tabular-nums">{{ price('chapter_storyboard_generation', 12) }} 积分</small></button></div>
        </section>

        <section v-else-if="activeWorkflow === 'audio'" class="dubbing-workbench stage-enter">
          <header class="dubbing-workbench__header">
            <div><span class="stage-kicker">VOICE PRODUCTION</span><h3>角色对白与配音</h3><p><span class="tabular-nums">{{ readyAudioCount }}</span> / {{ dialogueDetail?.lines.length || 0 }} 条台词已有生效音频</p></div>
            <div><button class="button button--secondary" type="button" :disabled="Boolean(dialogueExtractionTask) || dubbingAction === 'extract'" @click="queueDialogueExtraction"><LoaderCircle v-if="dialogueExtractionTask || dubbingAction === 'extract'" class="spin" :size="16" /><RefreshCw v-else-if="dialogues.length" :size="16" /><AudioLines v-else :size="16" />{{ dialogueExtractionTask ? 'AI 提取中' : dialogues.length ? '提取新版本' : 'AI 提取台词' }}<small>{{ price('chapter_dialogue_extraction', 4) }} 积分</small></button><button class="button button--primary" type="button" :disabled="!dialogueDetail?.version.is_active || !eligibleDialogueLines.length || dubbingAction === 'audio'" @click="queueDialogueAudio(eligibleDialogueLines)"><LoaderCircle v-if="dubbingAction === 'audio'" class="spin" :size="16" /><Volume2 v-else :size="16" />批量生成 <span class="tabular-nums">{{ eligibleDialogueLines.length }}</span><small>{{ price('dialogue_tts_generation', 5) }} 积分/条</small></button></div>
          </header>

          <section class="voice-casting">
            <header><div><UserRoundCog :size="15" /><strong>角色选角</strong></div><span>{{ dubbingOptions.voice_bindings.filter((item) => item.enabled).length }} / {{ dubbingOptions.character_assets.length }} 已绑定</span></header>
            <div v-if="dubbingOptions.character_assets.length" class="voice-casting__rail">
              <article v-for="(character, index) in dubbingOptions.character_assets" :key="character.id" v-motion="{ preset: 'card', index }" :class="{ bound: Boolean(voiceBindingForCharacter(character.id)) }">
                <button class="voice-character" type="button" @click="openVoiceBinding(character)"><span><img v-if="character.media_url" :src="character.media_url" :alt="character.name" /><UsersRound v-else :size="19" /><i><Mic2 :size="11" /></i></span><span><strong>{{ character.name }}</strong><small>{{ voiceBindingForCharacter(character.id)?.provider_voice_name || voiceBindingForCharacter(character.id)?.provider_voice_id || '待绑定音色' }}</small></span><ChevronRight :size="15" /></button>
                <button v-if="voiceBindingForCharacter(character.id)" class="voice-binding-remove" type="button" :title="`移除${character.name}的音色`" @click="removeVoiceBinding(character)"><Trash2 :size="13" /></button>
              </article>
            </div>
            <div v-else class="voice-casting__empty"><UsersRound :size="19" /><span>塑造资产中还没有人物</span><button type="button" @click="openAssetLibrary('project')">打开资产库</button></div>
          </section>

          <div v-if="dialogues.length" class="dubbing-version-bar">
            <span><History :size="15" />台词版本</span>
            <div><button v-for="item in dialogues" :key="item.id" type="button" :class="{ active: selectedDialogueId === item.id }" @click="selectDialogue(item.id)"><strong class="tabular-nums">v{{ item.version }}</strong><small>{{ item.is_active ? '当前生效' : item.invalidated_reason ? '历史版本' : '可切换' }}</small><CircleCheckBig v-if="item.is_active" :size="14" /></button></div>
            <button v-if="dialogueDetail && !dialogueDetail.version.is_active" class="button button--secondary" type="button" :disabled="dubbingAction === 'activate'" @click="activateDialogueVersion"><Check :size="15" />设为生效</button>
          </div>

          <div v-if="!dubbingOptions.tts_models.length" class="dubbing-alert"><TriangleAlert :size="18" /><div><strong>当前租户未配置可用 TTS 模型</strong><span>可先提取和编辑台词，配音生成暂不可用。</span></div></div>
          <div v-if="dubbingLoading" class="storyboard-loading"><LoaderCircle class="spin" :size="20" />正在同步台词与音频状态</div>
          <div v-else-if="dialogueDetail?.lines.length" class="dialogue-list">
            <article v-for="(line, index) in dialogueDetail.lines" :key="line.id" v-motion="{ preset: 'row', index }" class="dialogue-row" :class="{ playing: playingLineId === line.id }">
              <div class="dialogue-row__order tabular-nums">{{ String(line.order_index).padStart(2, '0') }}</div>
              <div class="dialogue-row__avatar"><img v-if="characterForSpeaker(line.speaker)?.media_url" :src="characterForSpeaker(line.speaker)?.media_url || undefined" :alt="line.speaker" /><UsersRound v-else :size="18" /></div>
              <div class="dialogue-row__copy"><header><strong>{{ line.speaker }}</strong><span>{{ line.emotion || '自然' }}</span><small v-if="line.shot_id">已匹配分镜</small><button class="icon-button icon-button--small" type="button" title="编辑台词" :disabled="!dialogueDetail.version.is_active" @click="openDialogueLineEditor(line)"><Pencil :size="13" /></button></header><p>{{ line.text }}</p><small v-if="line.direction">{{ line.direction }}</small></div>
              <div class="dialogue-row__voice"><span :data-ready="Boolean(bindingForLine(line))"><Mic2 :size="13" />{{ bindingForLine(line)?.provider_voice_name || bindingForLine(line)?.provider_voice_id || '未绑定' }}</span><small v-if="activeAudioClips.get(line.id)?.is_active">配音 v{{ activeAudioClips.get(line.id)?.version }} · {{ activeAudioClips.get(line.id)?.duration_seconds || '--' }}s</small><small v-else-if="activeAudioClips.get(line.id)?.status === 'failed'" class="failed">{{ activeAudioClips.get(line.id)?.error_message || '生成失败' }}</small><small v-else>台词稿 v{{ line.version }}</small></div>
              <div class="dialogue-row__actions">
                <button v-if="activeAudioClips.get(line.id)?.is_active" class="audio-play-button" type="button" :aria-label="playingLineId === line.id ? '暂停配音' : '播放配音'" :aria-pressed="playingLineId === line.id" @click="toggleAudio(line)"><span class="audio-play-button__play"><Play :size="16" /></span><span class="audio-play-button__pause"><Pause :size="16" /></span></button>
                <button class="button" :class="activeAudioClips.get(line.id)?.is_active ? 'button--secondary' : 'button--primary'" type="button" :disabled="!dialogueDetail.version.is_active || !bindingForLine(line) || busyDialogueLineIds.has(line.id) || dubbingAction === 'audio'" @click="queueDialogueAudio([line])"><LoaderCircle v-if="busyDialogueLineIds.has(line.id)" class="spin" :size="14" /><RefreshCw v-else-if="activeAudioClips.get(line.id)?.is_active" :size="14" /><Volume2 v-else :size="14" />{{ busyDialogueLineIds.has(line.id) ? '处理中' : activeAudioClips.get(line.id)?.is_active ? '重配' : '生成' }}</button>
              </div>
            </article>
          </div>
          <div v-else class="storyboard-empty"><span><AudioLines :size="29" /></span><strong>{{ dialogueExtractionTask ? 'AI 正在提取对白' : '还没有台词版本' }}</strong><p>{{ dialogueExtractionTask ? '完成后会自动建立角色、情绪和分镜映射。' : '从当前生效剧本提取首版可配音台词。' }}</p><button v-if="!dialogueExtractionTask" class="button button--primary" type="button" @click="queueDialogueExtraction"><WandSparkles :size="16" />提取首版台词</button></div>
        </section>
        <ChapterFinishingPanel
          v-else-if="activeWorkflow === 'finish'"
          :project-id="project.id"
          :chapter-id="selectedChapter.id"
          :chapter-title="selectedChapter.title"
          :resolution="project.video_resolution"
          :aspect-ratio="project.aspect_ratio"
        />
      </main>

    </div>

    <section v-else class="director-empty-state"><div class="director-empty-state__visual"><img :src="project.cover_url || '/covers/login-studio.jpg'" :alt="project.name" /><span><Clapperboard :size="24" /></span></div><div><span class="eyebrow">START PRODUCTION</span><h2>导入第一份创作内容</h2><p>从小说或剧本中识别章节，建立可追踪的短剧生产流程。</p><div><button class="button button--primary" type="button" @click="importOpen = true"><Upload :size="17" />导入 TXT / EPUB</button><button class="button button--secondary" type="button" @click="inputMode = 'paste'; importOpen = true"><FileText :size="17" />粘贴文本</button></div></div></section>

    <BaseDialog :open="importOpen" title="导入创作内容" description="导入小说或剧本并自动识别章节" wide @update:open="importOpen = $event">
      <div class="source-importer"><div class="mode-segment"><button :class="{ active: importMode === 'novel' }" type="button" @click="importMode = 'novel'"><BookOpenText :size="18" /><span><strong>小说</strong><small>按章节识别原文</small></span></button><button :class="{ active: importMode === 'script' }" type="button" @click="importMode = 'script'"><Clapperboard :size="18" /><span><strong>剧本</strong><small>按集识别内容</small></span></button></div><div class="input-mode-tabs"><button :class="{ active: inputMode === 'upload' }" type="button" @click="inputMode = 'upload'"><Upload :size="15" />上传文件</button><button :class="{ active: inputMode === 'paste' }" type="button" @click="inputMode = 'paste'"><FileText :size="15" />粘贴文本</button></div><button v-if="inputMode === 'upload'" class="source-dropzone" type="button" @click="fileInput?.click()"><input ref="fileInput" class="sr-only" type="file" accept=".txt,.epub,text/plain,application/epub+zip" @change="chooseUpload" /><span><Upload :size="23" /></span><strong>{{ uploadFile?.name || '选择 TXT 或 EPUB 文件' }}</strong><small>{{ uploadFile ? fileSize(uploadFile.size) : 'TXT 最大 10 MB，EPUB 最大 30 MB' }}</small></button><div v-else class="paste-editor"><label class="field"><span>来源名称</span><input v-model="sourceName" placeholder="例如：雾港来信原著" /></label><label class="field"><span>原始内容</span><textarea v-model="pastedText" rows="13" placeholder="在这里粘贴完整小说或剧本内容，系统将自动识别章节…"></textarea></label></div></div>
      <template #footer><button class="button button--ghost" type="button" @click="importOpen = false">取消</button><button class="button button--primary" type="button" :disabled="importing || (inputMode === 'upload' ? !uploadFile : !pastedText.trim())" @click="importSource"><LoaderCircle v-if="importing" class="spin" :size="17" /><WandSparkles v-else :size="17" />识别并创建章节</button></template>
    </BaseDialog>

    <BaseDialog :open="fileLibraryOpen" title="项目文件库" description="项目文件可供用户与 AI 读取和编辑" wide @update:open="fileLibraryOpen = $event">
      <div class="file-workbench"><aside class="file-browser"><header><label><Search :size="15" /><input v-model="fileSearch" placeholder="搜索项目文件" /></label><button class="icon-button icon-button--small" type="button" title="新建文件" @click="startNewFile"><FilePlus2 :size="16" /></button></header><div><button v-for="item in filteredFiles" :key="item.id" class="file-row" :class="{ active: selectedFile?.id === item.id }" type="button" @click="openFile(item)"><span><BrainCircuit v-if="item.kind === 'memory'" :size="16" /><FileText v-else :size="16" /></span><span><strong>{{ item.name }}</strong><small>{{ fileKindLabel[item.kind] }} · {{ fileSize(item.size_bytes) }}</small></span></button></div></aside><section class="file-editor-panel"><template v-if="selectedFile || newFileMode"><header><input v-model="fileNameDraft" :disabled="!newFileMode && !selectedFile?.editable" /><div v-if="newFileMode" class="file-kind-toggle" aria-label="文件用途"><button type="button" :class="{ active: newFileKind === 'memory' }" :aria-pressed="newFileKind === 'memory'" @click="newFileKind = 'memory'"><BrainCircuit :size="14" />项目记忆</button><button type="button" :class="{ active: newFileKind === 'other' }" :aria-pressed="newFileKind === 'other'" @click="newFileKind = 'other'"><FileText :size="14" />普通文档</button></div><div v-else><button v-if="selectedFile" class="icon-button icon-button--small" type="button" title="下载文件" @click="downloadFile(selectedFile)"><Download :size="16" /></button><button v-if="selectedFile" class="icon-button icon-button--small icon-button--danger" type="button" title="删除文件" @click="confirmFileDelete(selectedFile)"><Trash2 :size="16" /></button></div></header><textarea v-model="fileDraft" :disabled="!newFileMode && !selectedFile?.editable" spellcheck="false"></textarea></template><div v-else class="file-editor-empty"><FolderOpen :size="30" /><strong>选择一个项目文件</strong><span>或新建文档供项目 Agent 使用</span></div></section></div>
      <template #footer><span class="dialog-selection-count">{{ files.length }} 个项目文件</span><button class="button button--ghost" type="button" @click="fileLibraryOpen = false">关闭</button><button class="button button--primary" type="button" :disabled="(!selectedFile && !newFileMode) || (!newFileMode && !selectedFile?.editable) || fileSaving" @click="saveFile"><LoaderCircle v-if="fileSaving" class="spin" :size="17" /><Save v-else :size="17" />保存文件</button></template>
    </BaseDialog>

    <BaseDialog :open="Boolean(fileDeleteTarget)" title="确认删除项目文件" description="删除后无法从项目文件库恢复" @update:open="!$event && closeFileDelete()">
      <div class="danger-confirm"><span><Trash2 :size="22" /></span><div><strong>删除“{{ fileDeleteTarget?.name }}”</strong><p v-if="fileDeleteTarget?.kind === 'source'">来源文件关联的章节、剧本版本和后续生产历史也会一并删除。</p><p v-else>该文件将不再提供给项目 Agent 读取。</p></div></div>
      <template #footer><button class="button button--ghost" type="button" @click="closeFileDelete">取消</button><button class="button button--danger" type="button" @click="fileDeleteTarget && deleteFile(fileDeleteTarget)"><Trash2 :size="17" />确认删除</button></template>
    </BaseDialog>

    <BaseDialog :open="scriptEditorOpen" :title="scripts.length ? '创建剧本新版本' : '创建首版剧本'" description="版本保存后不可覆盖，可继续创建后续版本" wide @update:open="scriptEditorOpen = $event">
      <form id="script-version-form" class="script-editor" @submit.prevent="saveScriptVersion">
        <div class="script-editor__meta"><label class="field"><span>版本标题</span><input v-model="scriptForm.title" required maxlength="255" placeholder="例如：第 1 集 · 雨夜重逢" /></label><div class="field"><span>审核状态</span><div class="script-status-picker"><button v-for="status in (['draft', 'reviewing', 'approved'] as const)" :key="status" type="button" :class="{ active: scriptForm.status === status }" @click="scriptForm.status = status"><FilePenLine v-if="status === 'draft'" :size="15" /><History v-else-if="status === 'reviewing'" :size="15" /><CircleCheckBig v-else :size="15" />{{ scriptStatusLabel[status] }}</button></div></div></div>
        <label class="field script-editor__content"><span>剧本正文</span><textarea v-model="scriptForm.content" required rows="18" placeholder="场次、地点、时间、人物、动作与台词…"></textarea></label>
        <label class="field"><span>审核备注</span><textarea v-model="scriptForm.review_notes" rows="3" placeholder="记录待调整内容、审核意见或版本变化"></textarea></label>
        <button class="activation-toggle" type="button" role="switch" :aria-checked="scriptForm.activate" @click="scriptForm.activate = !scriptForm.activate"><span><i></i></span><span><strong>保存后设为当前生效版本</strong><small>切换后，依赖旧剧本的资产提取和分镜将标记为失效。</small></span></button>
      </form>
      <template #footer><span class="dialog-selection-count">{{ scriptForm.content.length.toLocaleString('zh-CN') }} 字符</span><button class="button button--ghost" type="button" @click="scriptEditorOpen = false">取消</button><button class="button button--primary" type="submit" form="script-version-form" :disabled="scriptSaving || !scriptForm.title.trim() || !scriptForm.content.trim()"><LoaderCircle v-if="scriptSaving" class="spin" :size="17" /><Save v-else :size="17" />保存新版本</button></template>
    </BaseDialog>

    <BaseDialog :open="Boolean(scriptActivateTarget)" title="切换生效剧本" description="确认章节后续生产所使用的剧本版本" @update:open="!$event && (scriptActivateTarget = null)">
      <div class="version-switch-confirm"><span><History :size="22" /></span><div><strong>切换为 v{{ scriptActivateTarget?.version }}《{{ scriptActivateTarget?.title }}》</strong><p>当前生效版本关联的资产提取与分镜会标记为失效，历史记录仍会保留。</p></div></div>
      <template #footer><button class="button button--ghost" type="button" @click="scriptActivateTarget = null">取消</button><button class="button button--primary" type="button" @click="activateScriptVersion"><Check :size="17" />确认切换</button></template>
    </BaseDialog>

    <BaseDialog :open="shotEditorOpen" :title="`编辑镜头 ${String(editingShot?.order_index || 0).padStart(2, '0')}`" description="保存会创建镜头新修订并失效旧视频" wide @update:open="shotEditorOpen = $event">
      <form id="shot-editor-form" class="shot-editor" @submit.prevent="saveShot"><div class="shot-editor__meta"><label class="field"><span>镜头标题</span><input v-model="shotForm.title" required maxlength="255" /></label><label class="field"><span>景别</span><input v-model="shotForm.shot_type" required maxlength="80" /></label><label class="field"><span>时长（秒）</span><input v-model="shotForm.duration_seconds" required type="number" min="1" max="30" step="0.5" /></label></div><div class="shot-editor__grid"><label class="field"><span>场景描述</span><textarea v-model="shotForm.scene_description" rows="5"></textarea></label><label class="field"><span>动作与运镜</span><textarea v-model="shotForm.action_description" rows="5"></textarea></label></div><label class="field"><span>台词</span><textarea v-model="shotForm.dialogue" rows="3"></textarea></label><div class="shot-editor__grid"><label class="field"><span>首帧图片提示词</span><textarea v-model="shotForm.image_prompt" rows="6"></textarea></label><label class="field"><span>视频生成提示词</span><textarea v-model="shotForm.video_prompt" rows="6"></textarea></label></div></form>
      <template #footer><span class="dialog-selection-count">镜头稿 v{{ editingShot?.version || 1 }}</span><button class="button button--ghost" type="button" @click="shotEditorOpen = false">取消</button><button class="button button--primary" type="submit" form="shot-editor-form" :disabled="storyboardAction === 'save' || !shotForm.title.trim() || !shotForm.video_prompt.trim()"><LoaderCircle v-if="storyboardAction === 'save'" class="spin" :size="17" /><Save v-else :size="17" />保存镜头</button></template>
    </BaseDialog>

    <BaseDialog :open="assetLibraryOpen" title="资产库" :description="`${project.name} · 塑造资产与租户共享资产`" workbench @update:open="assetLibraryOpen = $event">
      <AssetLibraryWorkbench
        :project-id="projectId"
        :project-name="project.name"
        :project-assets="projectAssets"
        :global-assets="globalAssets"
        :pricing="pricingRules"
        @close="assetLibraryOpen = false"
        @assets-changed="syncAssetRows"
      />
      <div v-if="false" class="asset-workbench">
        <header class="asset-toolbar"><div class="asset-scope-tabs"><button :class="{ active: assetScope === 'project' }" type="button" @click="assetScope = 'project'"><WandSparkles :size="16" /><span><strong>塑造资产</strong><small>{{ projectAssets.length }} 项 · AI 可读取</small></span></button><button :class="{ active: assetScope === 'global' }" type="button" @click="assetScope = 'global'"><Boxes :size="16" /><span><strong>全局资产库</strong><small>{{ globalAssets.length }} 项 · 租户共享</small></span></button></div><button class="button button--primary" type="button" @click="openAssetEditor()"><Plus :size="16" />新建资产</button></header>
        <div v-if="assetScope === 'project'" class="asset-batchbar">
          <button class="asset-select-all" type="button" :aria-pressed="allVisibleAssetsSelected" @click="toggleVisibleAssetSelection"><span><Check :size="13" /></span>{{ allVisibleAssetsSelected ? '取消全选' : '选择当前分类' }}</button>
          <span class="asset-batchbar__count"><strong class="tabular-nums">{{ selectedAssetIds.length }}</strong> 项已选择</span>
          <div>
            <button class="button button--secondary" type="button" :disabled="!promptEligibleAssets.length || Boolean(assetAction)" @click="queueAssetPrompts"><LoaderCircle v-if="assetAction === 'prompt'" class="spin" :size="15" /><Sparkles v-else :size="15" />生成提示词 <span class="tabular-nums">{{ promptEligibleAssets.length }}</span><small>{{ price('asset_prompt_generation', 2) }} 积分/项</small></button>
            <button class="button button--primary" type="button" :disabled="!imageEligibleAssets.length || Boolean(assetAction)" @click="queueAssetImages"><LoaderCircle v-if="assetAction === 'image'" class="spin" :size="15" /><Image v-else :size="15" />生成图片 <span class="tabular-nums">{{ imageEligibleAssets.length }}</span><small>{{ price('asset_image_generation', 20) }} 积分/项</small></button>
          </div>
        </div>
        <nav class="asset-type-filter" aria-label="资产类型"><button v-for="type in assetTypes" :key="type.value" :class="{ active: assetTypeFilter === type.value }" type="button" @click="assetTypeFilter = type.value"><component :is="type.icon" :size="15" />{{ type.label }}</button></nav>
        <div class="asset-grid"><article v-for="(asset, index) in visibleAssets" :key="asset.id" v-motion="{ preset: 'card', index }" class="asset-card" :class="{ selected: selectedAssetIds.includes(asset.id), busy: isAssetBusy(asset.id), derivative: Boolean(asset.parent_asset_id) }"><div class="asset-card__visual"><img v-if="asset.media_url" :src="asset.media_url || undefined" :alt="asset.name" /><component :is="assetTypeIcon[asset.asset_type]" v-else :size="25" /><span>{{ assetTypeLabel[asset.asset_type] }}</span><button v-if="asset.scope === 'project'" class="asset-card__select" type="button" :aria-label="`选择${asset.name}`" :aria-pressed="selectedAssetIds.includes(asset.id)" @click="toggleAssetSelection(asset.id)"><Check :size="14" /></button><i v-if="isAssetBusy(asset.id)" class="asset-card__busy"><LoaderCircle class="spin" :size="17" />任务处理中</i></div><div class="asset-card__body"><header><strong>{{ asset.name }}</strong><span :data-status="asset.status">{{ isAssetBusy(asset.id) ? '任务处理中' : assetStatusLabel[asset.status] }}</span></header><p>{{ asset.description || '暂无资产说明' }}</p><small v-if="asset.parent_asset_id"><GitBranchPlus :size="12" />衍生自 {{ assetParent(asset)?.name || '基础资产' }}</small></div><footer><button type="button" @click="transferAsset(asset)"><ArrowUpFromLine v-if="asset.scope === 'project'" :size="14" /><ArrowDownToLine v-else :size="14" />{{ asset.scope === 'project' ? '导出全局' : '导入项目' }}</button><button class="icon-button icon-button--small" type="button" title="编辑资产" @click="openAssetEditor(asset)"><Pencil :size="14" /></button><button class="icon-button icon-button--small icon-button--danger" type="button" title="删除资产" @click="confirmAssetDelete(asset)"><Trash2 :size="14" /></button></footer></article><div v-if="!visibleAssets.length" class="asset-empty"><Boxes :size="28" /><strong>这里还没有资产</strong><span>{{ assetScope === 'project' ? '从剧本提取，或从全局资产库导入' : '创建可供租户内项目复用的资产' }}</span></div></div>
      </div>
    </BaseDialog>

    <BaseDialog :open="assetEditorOpen" :title="assetForm.id ? (assetForm.is_derivative ? '编辑衍生资产' : '编辑基础资产') : (assetForm.is_derivative ? '新建衍生资产' : '新建资产')" description="建立清晰的资产谱系，所有修订均可追溯并恢复为新版本" wide @update:open="!$event && closeAssetEditor()">
      <div class="asset-editor-layout" :class="{ 'asset-editor-layout--history': assetForm.id }">
      <form id="asset-form" class="asset-form" @submit.prevent="saveAsset">
        <div class="asset-kind-picker"><button v-for="type in assetTypes.filter((item) => item.value !== 'all')" :key="type.value" :class="{ active: assetForm.asset_type === type.value }" type="button" :disabled="Boolean(assetForm.id)" @click="selectAssetType(type.value)"><component :is="type.icon" :size="16" />{{ type.label }}</button></div>
        <section class="asset-relation-field">
          <header><div><span>资产关系</span><small>人物、场景和道具可以建立一级衍生形态</small></div><strong>{{ assetForm.is_derivative ? '衍生资产' : '基础资产' }}</strong></header>
          <div class="asset-relation-toggle" role="group" aria-label="资产关系">
            <button type="button" :aria-pressed="!assetForm.is_derivative" @click="setAssetRelation(false)"><span><PackageSearch :size="17" /></span><span><strong>基础资产</strong><small>独立形象与一致性源头</small></span><CircleCheckBig :size="15" /></button>
            <button type="button" :aria-pressed="assetForm.is_derivative" :disabled="!assetFormSupportsDerivatives || assetFormHasChildren" @click="setAssetRelation(true)"><span><GitBranchPlus :size="17" /></span><span><strong>衍生资产</strong><small>造型、状态或空间变体</small></span><CircleCheckBig :size="15" /></button>
          </div>
          <div v-if="assetForm.is_derivative" class="asset-parent-picker">
            <span>选择基础资产</span>
            <div v-if="assetFormParentCandidates.length">
              <button v-for="candidate in assetFormParentCandidates" :key="candidate.id" type="button" :aria-pressed="assetForm.parent_asset_id === candidate.id" @click="assetForm.parent_asset_id = candidate.id"><span><img v-if="candidate.media_url" :src="candidate.media_url" :alt="candidate.name" /><component :is="assetTypeIcon[candidate.asset_type]" v-else :size="18" /></span><span><strong>{{ candidate.name }}</strong><small>{{ assetTypeLabel[candidate.asset_type] }}基础资产</small></span><CircleCheckBig :size="15" /></button>
            </div>
            <aside v-else><TriangleAlert :size="17" /><span><strong>暂无可用基础资产</strong><small>请先在当前资产库创建同类型基础资产</small></span></aside>
          </div>
          <aside v-else-if="!assetFormSupportsDerivatives" class="asset-relation-note"><ShieldCheck :size="16" />素材与音频保持独立资产，不建立衍生关系</aside>
          <aside v-else-if="assetFormHasChildren" class="asset-relation-note"><ShieldCheck :size="16" />该基础资产已有衍生项，需先调整子项关系才能改变层级</aside>
        </section>
        <label class="field"><span>资产名称</span><input v-model="assetForm.name" required placeholder="例如：女主角林遥" /></label><label class="field"><span>资产说明</span><textarea v-model="assetForm.description" rows="4" placeholder="外形、身份、叙事用途等基础信息"></textarea></label><label class="field"><span>生图提示词</span><textarea v-model="assetForm.generation_prompt" rows="6" placeholder="生成提示词后，资产才会进入提示词就绪状态"></textarea></label>
      </form>
      <aside v-if="assetForm.id" class="asset-history">
        <header><span><History :size="16" /></span><div><strong>版本历史</strong><small>旧版本只读，恢复会生成新版本</small></div><b class="tabular-nums">{{ assetRevisions.length }}</b></header>
        <div v-if="assetRevisionLoading" class="asset-history__loading"><LoaderCircle class="spin" :size="18" />正在读取版本</div>
        <template v-else-if="selectedAssetRevision">
          <div class="asset-history__rail">
            <button v-for="revision in assetRevisions" :key="revision.id" type="button" :aria-pressed="selectedAssetRevision.id === revision.id" @click="selectedAssetRevisionId = revision.id">
              <span class="tabular-nums">v{{ revision.version }}</span><span><strong>{{ assetRevisionChangeLabel[revision.change_type] || revision.change_type }}</strong><small>{{ new Date(revision.created_at).toLocaleString('zh-CN', { hour12: false }) }}</small></span><CircleCheckBig :size="15" />
            </button>
          </div>
          <section :key="selectedAssetRevision.id" class="asset-history__preview">
            <div><img v-if="selectedAssetRevision.media_url" :src="selectedAssetRevision.media_url" :alt="selectedAssetRevision.name" /><component :is="assetTypeIcon[assetForm.asset_type]" v-else :size="24" /></div>
            <header><span><strong>{{ selectedAssetRevision.name }}</strong><small>{{ assetStatusLabel[selectedAssetRevision.status] }}</small></span><b class="tabular-nums">v{{ selectedAssetRevision.version }}</b></header>
            <p>{{ selectedAssetRevision.description || '此版本没有资产说明' }}</p>
            <details v-if="selectedAssetRevision.generation_prompt"><summary>查看生图提示词</summary><p>{{ selectedAssetRevision.generation_prompt }}</p></details>
          </section>
          <button class="asset-history__restore" type="button" :disabled="assetRestoring || selectedAssetRevision.version === assetRevisions[0]?.version" @click="restoreAssetRevision"><LoaderCircle v-if="assetRestoring" class="spin" :size="16" /><RotateCcw v-else :size="16" /><span><strong>{{ selectedAssetRevision.version === assetRevisions[0]?.version ? '当前生效版本' : '恢复为新版本' }}</strong><small>{{ selectedAssetRevision.version === assetRevisions[0]?.version ? '选择较早版本可执行恢复' : `基于 v${selectedAssetRevision.version} 创建下一版本` }}</small></span></button>
        </template>
      </aside>
      </div>
      <template #footer><span class="dialog-selection-count"><strong v-if="assetForm.id" class="tabular-nums">v{{ assetRevisions[0]?.version || '—' }}</strong> {{ assetScope === 'project' ? '项目塑造资产' : '租户全局资产' }}</span><button class="button button--ghost" type="button" @click="closeAssetEditor">取消</button><button class="button button--primary" type="submit" form="asset-form" :disabled="assetSaving || !assetFormValid"><LoaderCircle v-if="assetSaving" class="spin" :size="17" /><Save v-else :size="17" />保存资产</button></template>
    </BaseDialog>

    <BaseDialog :open="voiceBindingOpen" :title="`角色音色 · ${bindingCharacter?.name || ''}`" description="绑定租户 TTS 模型与平台音色标识" wide @update:open="voiceBindingOpen = $event">
      <form id="voice-binding-form" class="voice-binding-form" @submit.prevent="saveVoiceBinding">
        <section class="voice-binding-identity"><span><img v-if="bindingCharacter?.media_url" :src="bindingCharacter.media_url" :alt="bindingCharacter.name" /><UsersRound v-else :size="25" /></span><div><small>CHARACTER VOICE</small><strong>{{ bindingCharacter?.name }}</strong><p>{{ bindingCharacter?.description || '暂无角色说明' }}</p></div></section>
        <div class="field"><span>配音模型</span><div v-if="dubbingOptions.tts_models.length" class="tts-model-picker"><button v-for="model in dubbingOptions.tts_models" :key="model.id" type="button" :class="{ active: voiceForm.tts_model_id === model.id }" @click="voiceForm.tts_model_id = model.id"><span><AudioLines :size="17" /></span><span><strong>{{ model.name }}</strong><small>{{ model.model_id }}</small></span><CircleCheckBig :size="15" /></button></div><div v-else class="tts-model-empty"><TriangleAlert :size="17" />管理员尚未配置可用 TTS 模型</div></div>
        <div class="voice-binding-grid"><label class="field"><span>平台音色 ID</span><input v-model="voiceForm.provider_voice_id" required maxlength="255" placeholder="例如：voice_linyao_01" /></label><label class="field"><span>显示名称</span><input v-model="voiceForm.provider_voice_name" maxlength="255" placeholder="例如：林遥 · 克制女声" /></label></div>
        <label class="field"><span>表演风格</span><input v-model="voiceForm.style" maxlength="255" placeholder="自然对白、电影旁白、紧张克制…" /></label>
        <label class="field"><span>音色与表演指令</span><textarea v-model="voiceForm.instructions" rows="5" placeholder="音区、语速、停顿、气息与情绪边界"></textarea></label>
      </form>
      <template #footer><button class="button button--ghost" type="button" @click="voiceBindingOpen = false">取消</button><button class="button button--primary" type="submit" form="voice-binding-form" :disabled="dubbingAction === 'binding' || !voiceForm.tts_model_id || !voiceForm.provider_voice_id.trim()"><LoaderCircle v-if="dubbingAction === 'binding'" class="spin" :size="17" /><Save v-else :size="17" />保存音色</button></template>
    </BaseDialog>

    <BaseDialog :open="dialogueEditorOpen" :title="`编辑台词 ${String(editingDialogueLine?.order_index || 0).padStart(2, '0')}`" description="保存后旧配音自动转入历史" wide @update:open="dialogueEditorOpen = $event">
      <form id="dialogue-line-form" class="dialogue-line-form" @submit.prevent="saveDialogueLine"><div class="voice-binding-grid"><label class="field"><span>说话角色</span><input v-model="dialogueLineForm.speaker" required maxlength="160" /></label><label class="field"><span>情绪</span><input v-model="dialogueLineForm.emotion" maxlength="120" placeholder="自然、警惕、克制…" /></label></div><label class="field"><span>台词正文</span><textarea v-model="dialogueLineForm.text" required rows="6"></textarea></label><label class="field"><span>表演指导</span><textarea v-model="dialogueLineForm.direction" rows="4" placeholder="语速、停顿、重音和气息"></textarea></label></form>
      <template #footer><span class="dialog-selection-count">台词稿 v{{ editingDialogueLine?.version || 1 }}</span><button class="button button--ghost" type="button" @click="dialogueEditorOpen = false">取消</button><button class="button button--primary" type="submit" form="dialogue-line-form" :disabled="dubbingAction === 'line' || !dialogueLineForm.speaker.trim() || !dialogueLineForm.text.trim()"><LoaderCircle v-if="dubbingAction === 'line'" class="spin" :size="17" /><Save v-else :size="17" />保存台词</button></template>
    </BaseDialog>

    <BaseDialog :open="Boolean(assetDeleteTarget)" title="确认删除资产" @update:open="!$event && closeAssetDelete()"><div class="danger-confirm"><span><Trash2 :size="22" /></span><div><strong>删除“{{ assetDeleteTarget?.name }}”</strong><p>已存在于其它资产库的复制版本不会被删除。</p></div></div><template #footer><button class="button button--ghost" type="button" @click="closeAssetDelete">取消</button><button class="button button--danger" type="button" @click="deleteAsset"><Trash2 :size="17" />确认删除</button></template></BaseDialog>
  </div>
</template>
